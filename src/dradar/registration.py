"""A single host-local worker registration budget; no detached network work.

Only the startup path uses this adapter. Background telemetry and ApiClient's
general retry/proxy policy are unchanged. Filesystem syscalls are synchronous:
we check on either side and never grant an expired permit, but cannot promise
to interrupt a stalled kernel/filesystem call. Cleanup is a separate phase.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import time

import httpx

from . import cancellation
from .api_client import ApiError
from .flight_recorder import _checked_lock, _exclusive_file_lock

REGISTRATION_SECONDS = 36.0
HANDOFF_MARGIN_SECONDS = 1.0
CLOSE_FENCE_SECONDS = 9.0
REGISTRATION_REQUEST_SECONDS = 8.0
_TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
              httpx.ReadError, httpx.WriteError, httpx.WriteTimeout,
              httpx.RemoteProtocolError)


class RegistrationWindow:
    def __init__(self, worker_deadline, alive, *, defer_abort=False):
        now = time.monotonic()
        if (type(worker_deadline) not in (float, int)
                or not math.isfinite(worker_deadline)
                or not now < worker_deadline <= now + 120.0):
            raise ApiError("worker permission deadline is missing or expired")
        self._diagnostic_started = now
        self._diagnostic = {}
        self._diagnostic_stage = "local_state"
        self._diagnostic_ack = "not_received"
        self._diagnostic_local_substage = "unknown"
        self._diagnostic_frozen = False
        self.deadline = min(now + REGISTRATION_SECONDS,
                            worker_deadline - HANDOFF_MARGIN_SECONDS)
        self.alive = alive
        self.stage = "worker-registration"
        self.started_sent = False
        self.gate_published = False
        self.telemetry = self.api = self.assignment = None
        self._fenced = False
        self._abort_attempted = False
        # The owning runner must revoke permission and stop local execution
        # before a possibly slow close request. Standalone callers retain the
        # immediate fence on bind failure unless they explicitly take ownership.
        self._defer_abort = defer_abort

    def _error(self, message, reason):
        error = ApiError(message)
        error.registration_reason = reason
        return error

    def _observe(self, *, stage=None, ack=None, local_substage=None):
        # Diagnostics are best effort and must never change control flow.
        try:
            if stage is not None:
                if stage != self._diagnostic_stage:
                    self._diagnostic_local_substage = "unknown"
                self._diagnostic_stage = stage
            if ack is not None:
                self._diagnostic_ack = ack
            if local_substage is not None:
                self._diagnostic_local_substage = local_substage
        except Exception:
            pass

    def _snapshot(self, exc):
        try:
            if self._diagnostic_frozen:
                return
            self._diagnostic_frozen = True
            reason = getattr(exc, "registration_reason", None)
            if reason is None:
                reason = ("http_rejected" if isinstance(exc, ApiError) and exc.status_code is not None
                          else "local_state_error" if isinstance(exc, (OSError, ValueError, TypeError))
                          else "unknown")
            prior_diagnostic = self._diagnostic if isinstance(self._diagnostic, dict) else {}
            result = {
                "registration_failure_stage": self._diagnostic_stage,
                "registration_failure_reason": reason,
                "registration_ack_state": self._diagnostic_ack,
                "registration_close_state": "not_attempted",
                "registration_local_substage": self._diagnostic_local_substage,
            }
            for key in ("session_id", "worker_event_id"):
                value = prior_diagnostic.get(key)
                if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                    result[key] = value
            if isinstance(exc, ApiError) and exc.status_code is not None:
                result["ack_http_status"] = exc.status_code
                if exc.code is not None:
                    result["ack_http_code"] = exc.code
            try:
                now = time.monotonic()
                elapsed = (now - self._diagnostic_started) * 1000
                remaining = (self.deadline - now) * 1000
                if math.isfinite(now) and math.isfinite(elapsed) and 0 <= elapsed <= 120000:
                    result["registration_elapsed_ms"] = int(elapsed)
                    if math.isfinite(remaining) and remaining <= 15000:
                        result["registration_remaining_ms"] = int(max(0, remaining))
            except (ArithmeticError, TypeError, ValueError, OSError):
                pass
            self._diagnostic = result
            self._publish_diagnostic()
        except Exception:
            pass

    def _publish_diagnostic(self):
        try:
            if self.telemetry is not None:
                current = self.telemetry.worker_registration_diagnostic
                current.update(self._diagnostic)
                self.telemetry._registration_diagnostic_local.value = current
        except Exception:
            pass

    def _close_diagnostic(self, state):
        try:
            self._diagnostic["registration_close_state"] = state
            self._publish_diagnostic()
        except Exception:
            pass

    def finalize_failure_report(self, error):
        """Keep the failure snapshot; attach this window's final close outcome.

        The runner calls this after deferred abort. Failure fields describe the
        original failure, while close_state describes the later fence attempt.
        Never borrow cleanup diagnostics from another worker/session.
        """
        try:
            detail = getattr(error, "report_detail", None)
            if not isinstance(detail, dict) or not self._diagnostic_frozen:
                return
            if any(not self._diagnostic.get(key) or
                   detail.get(key) != self._diagnostic[key]
                   for key in ("session_id", "worker_event_id")):
                return
            close = self._diagnostic.get("registration_close_state")
            from .registration_diagnostics import safe_value
            if safe_value("registration_close_state", close) is not None:
                error.report_detail = {**detail, "registration_close_state": close}
        except Exception:
            pass  # Observation must never change teardown or recovery.

    def check(self):
        if cancellation.requested():
            raise KeyboardInterrupt
        if (self.telemetry is not None and
                (self.telemetry.stop_requested or self.telemetry._stop.is_set())):
            raise self._error("worker registration stopped", "stop_requested")
        if time.monotonic() >= self.deadline:
            raise self._error("worker registration budget expired", "budget_expired")
        if not self.alive():
            raise self._error("worker exited during registration", "worker_exited")

    def _client(self):
        from .write_wait_policy import headers as writer_headers
        # ApiClient exposes only environment-derived proxy/TLS configuration;
        # preserve its server, bearer/capability headers and cookies. The
        # default async transports honor env proxies/certificates and have
        # zero connection retries. Never share/close the periodic client.
        return httpx.AsyncClient(base_url=self.api.server,
                                 headers=writer_headers(self.api),
                                 cookies=self.api._client.cookies,
                                 timeout=REGISTRATION_REQUEST_SECONDS, trust_env=True)

    def _check_response(self, response):
        # Only this registration facade classifies malformed JSON/shape;
        # ApiClient's general response and retry contract is unchanged.
        try:
            result = self.api._check(response)
        except ValueError as exc:
            error = self._error("invalid registration response", "invalid_response")
            error.registration_receipt_invalid = True
            raise error from exc
        if not isinstance(result, dict):
            error = self._error("invalid registration response", "invalid_response")
            error.registration_receipt_invalid = True
            raise error
        return result

    async def _retry_delay(self, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise self._error("invalid retry budget", "budget_expired")
        until = time.monotonic() + seconds
        if until + REGISTRATION_REQUEST_SECONDS + HANDOFF_MARGIN_SECONDS > self.deadline:
            raise self._error("registration retry budget exhausted", "budget_expired")
        while time.monotonic() < until:
            self.check()
            await asyncio.sleep(min(0.025, max(0.0, until - time.monotonic())))
        self.check()

    async def _request(self, client, path, *, attempts=1, cleanup=False,
                       method="POST", raw_response=False,
                       reconciliation_spacing=0.0, **kwargs):
        state = {"uncertain": False, "status": None, "http_status": None}
        try:
            return await self._request_attempts(
                client, path, attempts=attempts, cleanup=cleanup, method=method,
                raw_response=raw_response, reconciliation_spacing=reconciliation_spacing,
                recovery_state=state, **kwargs)
        except ApiError as exc:
            if not cleanup and state["status"] in ("busy_not_executed", "unknown_unreconciled"):
                body = kwargs.get("json") or kwargs.get("data") or {}
                identity = {}
                if isinstance(body, dict):
                    for key in ("session_id", "batch_id", "assignment_id", "worker_event_id"):
                        value = body.get(key)
                        if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value):
                            identity[key] = value
                    seq = body.get("seq")
                    if type(seq) is int and 0 <= seq < 2**63:
                        identity["seq"] = seq
                    events = body.get("events")
                    if isinstance(events, list) and 0 < len(events) <= 100:
                        ids = [event.get("event_id") for event in events if isinstance(event, dict)]
                        if len(ids) == len(events) and all(isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value) for value in ids):
                            identity["event_ids"] = ids
                exc.retry_exhausted = True
                exc.write_outcome = {
                    "category": "registration", "phase": path.rsplit("/", 1)[1],
                    "status": state["status"], "request_identity": identity or "unknown",
                    "last_http_status": state["http_status"],
                    "execution_allowed": False, "next_commands": ["dradar status --json"],
                }
                print(json.dumps({"write_recovery": exc.write_outcome}, sort_keys=True))
            raise

    async def _request_attempts(self, client, path, *, attempts, cleanup,
                                method, raw_response, reconciliation_spacing,
                                recovery_state, **kwargs):
        # Only callers with an immutable, endpoint-specific operation identity
        # request multiple attempts. This is not a generic POST retry policy.
        self._last_request_replayed = False
        for attempt in range(attempts):
            attempt_started = time.monotonic()
            if not cleanup:
                self.check()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise self._error("worker registration budget expired", "budget_expired")
            if not cleanup and remaining < REGISTRATION_REQUEST_SECONDS + HANDOFF_MARGIN_SECONDS:
                raise self._error("insufficient budget for registration request", "budget_expired")
            prior_uncertain = recovery_state["uncertain"]
            recovery_state.update(uncertain=True, status="unknown_unreconciled", http_status=None)
            request_deadline = min(self.deadline, time.monotonic() + REGISTRATION_REQUEST_SECONDS)
            task = asyncio.create_task(client.request(method, path, **kwargs))
            retry_delay = None
            recovery_error = None
            try:
                while not task.done():
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._error("worker registration budget expired", "budget_expired")
                    request_remaining = request_deadline - time.monotonic()
                    if request_remaining <= 0:
                        raise httpx.ReadTimeout("registration request deadline exceeded")
                    if not cleanup:
                        self.check()
                    await asyncio.wait({task}, timeout=min(0.025, remaining, request_remaining))
                response = await task
                from .write_wait_policy import confirms_not_executed
                recovery_state["http_status"] = response.status_code
                if confirms_not_executed(response):
                    recovery_state["uncertain"] = prior_uncertain
                    recovery_state["status"] = ("unknown_unreconciled" if prior_uncertain else "busy_not_executed")
                elif 400 <= response.status_code < 500 and not prior_uncertain:
                    recovery_state.update(uncertain=False, status="rejected")
                if not cleanup:
                    self.check()
                busy = False
                if response.status_code == 503:
                    try:
                        body = response.json()
                        busy = isinstance(body, dict) and body.get("code") == "mutation_busy"
                    except ValueError:
                        pass
                if busy and attempt + 1 < attempts and not cleanup:
                    try:
                        delay = float(response.headers["Retry-After"])
                        declared = body["retry_after_seconds"]
                        if (type(declared) not in (int, float)
                                or not math.isfinite(delay) or delay < 0
                                or delay != declared):
                            raise ValueError("invalid busy retry interval")
                    except (KeyError, TypeError, ValueError):
                        raise self._error("invalid busy retry interval", "invalid_response")
                    retry_delay = delay + random.uniform(0.0, min(1.0, delay * 0.1))
                elif response.status_code >= 500 and attempt + 1 < attempts and not cleanup:
                    # Outcome is unknown. Replaying this same identity is the
                    # endpoint's reconciliation operation; no new ID or seq.
                    retry_delay = 0.0
                    recovery_error = ApiError("registration response remains unconfirmed",
                                              status_code=response.status_code)
                elif raw_response:
                    # Even a 2xx can leave the operation outcome unknown if
                    # its receipt was truncated. Reconcile the same identity
                    # before handing the response to mark_started.
                    self._check_response(response)
                    self._last_request_replayed = attempt > 0
                    return response
                else:
                    result = self._check_response(response)
                    self._last_request_replayed = attempt > 0
                    return result
            except ApiError as exc:
                if (not getattr(exc, "registration_receipt_invalid", False)
                        or attempt + 1 == attempts or cleanup):
                    raise
                retry_delay = 0.0
                recovery_error = exc
            except _TRANSIENT as exc:
                if attempt + 1 == attempts or cleanup:
                    raise self._error("registration transport failed", "transport_error") from exc
                retry_delay = 0.0
                recovery_error = self._error("registration transport failed", "transport_error")
            except httpx.HTTPError as exc:
                raise self._error("registration transport failed", "transport_error") from exc
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if retry_delay is not None:
                # Started is also the existing exact-session reconciliation
                # protocol. Space those reads/replays across the remaining
                # window instead of abandoning a still-recoverable start after
                # two fast disconnects. The caller fixes a finite attempt cap.
                retry_delay = max(retry_delay, reconciliation_spacing -
                                  (time.monotonic() - attempt_started))
                try:
                    await self._retry_delay(retry_delay)
                except ApiError as exc:
                    if (recovery_error is not None
                            and getattr(exc, "registration_reason", None) == "budget_expired"
                            and time.monotonic() < self.deadline):
                        # The remaining wall-clock window cannot fit another
                        # full spaced attempt. Keep the actual unresolved
                        # response cause; do not report that wall time expired.
                        recovery_error.retry_exhausted = True
                        raise recovery_error from exc
                    raise
        raise AssertionError("unreachable")

    def bind(self, api, telemetry, assignment):
        self.api, self.telemetry, self.assignment = api, telemetry, assignment
        try:
            telemetry._registration_diagnostic_local.value = {}
        except Exception:
            pass
        try:
            event = asyncio.run(self._bind())
            self.stage = "assignment-start"
            self._observe(stage="start_preflight")
            # Reuse managed-auth preflight and the real mark_started method on
            # a shallow API facade, replacing only its request function. No
            # periodic/global client mutation or duplicated auth policy.
            scoped = copy.copy(api)
            scoped._request = self._start_request
            scoped._check = self._check_response
            response = scoped.mark_started(assignment["assignment_id"],
                session_id=telemetry.session_id, worker_event_id=event["event_id"])
            if response.get("ok") is not True:
                raise self._error("assignment start was not confirmed", "invalid_response")
            self._observe(stage="local_state")
            if type(response.get("owner_epoch")) is int:
                assignment["owner_epoch"] = response["owner_epoch"]
            assignment["_runner_session_id"] = telemetry.session_id
            self._observe(local_substage="state_lock")
            with _checked_lock(telemetry._lock, self.check):
                previous_phase = telemetry._phase
                self._observe(local_substage="phase_transition")
                telemetry._phase = "running"
                telemetry._active_assignment_id = assignment["assignment_id"]
                telemetry._owner_epoch = assignment.get("owner_epoch")
                telemetry._progress_counter += 1
            for kind, component, attributes in (
                ("phase_changed", "heartbeat", {"previous_phase": previous_phase, "phase": "running"}),
                ("provider_started", "provider", {"provider": assignment.get("agent") or "codex"}),
            ):
                telemetry.flight_recorder.record(
                    kind, component=component, batch_id=telemetry._batch_id,
                    session_id=telemetry.session_id,
                    assignment_id=assignment["assignment_id"],
                    attributes=attributes, _registration_check=self.check)
            # The normal telemetry loop delivers these retained events; no
            # unbounded synchronous flush is added to the start handshake.
            telemetry._wake.set()
            self.check()
            return response
        except BaseException as exc:
            self._snapshot(exc)
            if not self._defer_abort:
                self.abort()
            if isinstance(exc, (OSError, ValueError, TypeError)):
                raise ApiError("worker registration local data could not be confirmed") from exc
            raise

    async def _bind(self):
        t, a = self.telemetry, self.assignment
        recorder = t.flight_recorder
        if recorder is None or not t._batch_id or t._disabled:
            raise ApiError("worker registration recorder unavailable")
        self._observe(local_substage="state_lock")
        with _checked_lock(t._send_lock, self.check):
            self.check()
            with _checked_lock(t._lock, self.check):
                batch, session, assignment_id = t._batch_id, t.session_id, a["assignment_id"]
                owner_epoch = t._owner_epoch
            if isinstance(self._diagnostic, dict):
                self._diagnostic["session_id"] = session
            else:
                self._diagnostic = {"session_id": session}
            self._observe(local_substage="recorder_event_record")
            event = recorder.record("worker_registered", component="provider",
                                    batch_id=batch, session_id=session,
                                    assignment_id=assignment_id,
                                    attributes={"provider": a.get("agent") or "codex"},
                                    _registration_check=self.check)
            diagnostic = {"session_id": session, "worker_event_id": event["event_id"]}
            if isinstance(self._diagnostic, dict):
                self._diagnostic.update(diagnostic)
            else:
                self._diagnostic = dict(diagnostic)
            t._registration_diagnostic_local.value = diagnostic
            async with self._client() as client:
                # Construct the heartbeat once. A transport failure must not
                # turn its reconciliation into a fresh heartbeat operation.
                self.check()
                payload = t._payload(registration_check=self.check)
                if (payload["session_id"], payload["batch_id"],
                        payload["active_assignment_id"], payload["phase"], payload["owner_epoch"]) != (
                        session, batch, assignment_id, "building", owner_epoch):
                    raise ApiError("worker registration identity changed")
                self._observe(stage="heartbeat")
                try:
                    response = await self._request(client, "/api/v1/runner/heartbeat",
                                                   json=payload, attempts=2)
                except ApiError as exc:
                    diagnostic["registration_result"] = (
                        "heartbeat_http_error" if exc.status_code is not None
                        else "heartbeat_transport_error")
                    if exc.status_code is not None:
                        diagnostic["ack_http_status"] = exc.status_code
                    raise
                if response.get("stop_requested") is True:
                    t._stop_requested = True
                    raise self._error("server requested worker stop", "stop_requested")
                heartbeat_replayed = (
                    self._last_request_replayed
                    and response.get("accepted") is False
                    and response.get("action") == "continue"
                    and response.get("batch_id") == batch
                )
                if response.get("accepted") is not True and not heartbeat_replayed:
                    diagnostic["registration_result"] = "heartbeat_rejected"
                    raise self._error("worker heartbeat was not accepted", "invalid_response")
                if response.get("batch_id", batch) != batch:
                    raise self._error("worker heartbeat batch changed", "invalid_response")
                self.check()
                diagnostic["registration_result"] = "flight_unknown"
                self._observe(stage="flight")
                with _checked_lock(recorder._lock, self.check):
                    with _exclusive_file_lock(recorder.lock_path, check=self.check):
                        pending = recorder._load(recorder.pending_path)
                        if not recorder._reserve_event_recovery_unlocked(pending, [event]):
                            raise self._error("worker event recovery budget exhausted", "budget_expired")
                receipt = await self._request(client, "/api/v1/runner/flight-events",
                                              attempts=2, json={"events": [event]})
                ids = receipt.get("acknowledged_event_ids")
                if (not isinstance(ids, list) or not all(recorder._valid_event_id(i) for i in ids)
                        or event["event_id"] not in ids):
                    diagnostic["registration_result"] = "flight_target_not_acknowledged"
                    raise self._error("exact worker event was not acknowledged", "invalid_response")
                self._observe(stage="ack_persist", ack="received")
                diagnostic["registration_result"] = "flight_ack_persist_error"
                self._observe(local_substage="ack_file_lock")
                with _checked_lock(recorder._lock, self.check):
                    with _exclusive_file_lock(recorder.lock_path, check=self.check):
                        self.check()
                        self._observe(local_substage="ack_file_write")
                        recorder._write_acknowledged_ids_unlocked({event["event_id"]})
                        self._observe(local_substage="pending_read")
                        pending = recorder._load(recorder.pending_path)
                        self._observe(local_substage="pending_write")
                        recorder._write(recorder.pending_path,
                                        [e for e in pending if e.get("event_id") != event["event_id"]])
                        self.check()
                        recorder._last_acknowledged_event_ids.add(event["event_id"])
                self._observe(ack="persisted")
                diagnostic["registration_result"] = "flight_target_acknowledged"
                return event

    def _start_request(self, method, path, **kwargs):
        self.check()
        kwargs.pop("retry_rate_limit", None)
        kwargs.pop("retry_transport", None)
        kwargs["timeout"] = REGISTRATION_REQUEST_SECONDS
        attempts, spacing = 2, 0.0
        if path == "/api/v1/assignment/started":
            if self.deadline - time.monotonic() < REGISTRATION_REQUEST_SECONDS + HANDOFF_MARGIN_SECONDS:
                raise self._error("insufficient budget for worker start handoff", "handoff_budget_insufficient")
            self._observe(stage="start_request")
            self.started_sent = True
            self.assignment["_registration_start_uncertain"] = True
            spacing = REGISTRATION_REQUEST_SECONDS + HANDOFF_MARGIN_SECONDS
            # One finite set of opportunities for this original session/event.
            # The extra slot makes the deadline check, rather than a fast
            # attempt-count failure, decide when no full replay can fit.
            attempts = math.ceil((self.deadline - time.monotonic()) / spacing) + 1
        async def send():
            async with self._client() as client:
                return await self._request(client, path, method=method, attempts=attempts,
                                           reconciliation_spacing=spacing,
                                           raw_response=True, **kwargs)
        return asyncio.run(send())

    def finish(self):
        self.check()
        self.gate_published = True
        self.assignment.pop("_registration_start_uncertain", None)

    def abort(self):
        if (not self.started_sent or self.gate_published or self._fenced
                or self._abort_attempted):
            return
        self._abort_attempted = True
        # Close is a durable server tombstone: it rejects a start that arrives
        # after close, and does not clear any lease itself. Only then may the
        # existing owner-fenced stop cleanup claim success.
        t = self.telemetry
        t._stop.set()
        t._wake.set()
        start_deadline = self.deadline
        if self._defer_abort:
            # Local teardown can outlast the registration window. Give the
            # owning runner one bounded close attempt after teardown without
            # granting any more time to the expired provider start permission.
            self.deadline = time.monotonic() + CLOSE_FENCE_SECONDS
        try:
            asyncio.run(self._close_fence())
        except BaseException as exc:
            reason = getattr(exc, "registration_reason", None)
            state = ("http_rejected" if isinstance(exc, ApiError) and exc.status_code is not None
                     else reason if reason in {"transport_error", "budget_expired", "invalid_response"}
                     else "unknown")
            self._close_diagnostic(state)
            # Remain explicitly uncertain; runloop quarantines instead of
            # claiming a stop before an in-flight start is fenced.
            pass
        finally:
            self.deadline = start_deadline

    async def _close_fence(self):
        t = self.telemetry
        async with self._client() as client:
            result = await self._request(client, "/api/v1/runner/close", cleanup=True,
                json={"session_id": t.session_id, "batch_id": t._batch_id,
                      "seq": t._seq + 1, "reason": "error"})
            if result.get("ok") is not True:
                raise self._error("registration close fence unconfirmed", "invalid_response")
            self._close_diagnostic("confirmed")
            self._fenced = True
            self.assignment.pop("_registration_start_uncertain", None)
