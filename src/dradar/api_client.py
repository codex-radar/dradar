"""HTTP client for the dradar dispatch server."""

import json
import os
import random
import time
import urllib.parse
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .providers import advertised_capabilities, normalize_capabilities
from .submission_intent import (
    LEGACY_UPLOAD_INTENT_VERSION,
    UPLOAD_INTENT_VERSION,
)

_RATE_LIMIT_RETRIES = 5
_DEFAULT_RETRY_AFTER_SEC = 1.0
_MAX_RETRY_AFTER_SEC = 60.0
_SUBMISSION_MAINTENANCE_RETRY_BUDGET_SEC = 360.0
_DEFAULT_MAINTENANCE_RETRY_AFTER_SEC = 1.0
_MAX_SUBMISSION_MAINTENANCE_WAIT_SEC = 60.0
CLIENT_VERSION_HEADER = "X-DRadar-Client-Version"
CLIENT_CAPABILITIES_HEADER = "X-DRadar-Capabilities"


def normalize_batch_id(value: str | None) -> str | None:
    """Validate a user-facing claim-batch UUID and normalize it to hex."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) not in {32, 36}:
        raise ValueError("batch id must be a 32-hex or canonical 36-character UUID")
    lowered = value.lower()
    try:
        parsed = uuid.UUID(lowered)
    except (ValueError, AttributeError) as exc:
        raise ValueError("batch id must be a valid UUID") from exc
    if len(lowered) == 32:
        canonical = parsed.hex
    else:
        canonical = str(parsed)
    if lowered != canonical:
        raise ValueError("batch id must use canonical UUID syntax")
    return parsed.hex


def _env_proxies_set() -> bool:
    """Any of the proxy env vars httpx honors. Passing ANY explicit transport
    to httpx.Client disables its environment-proxy mounting entirely, so the
    connect-retry transport below must stand aside on proxied machines."""
    return any(os.environ.get(k) for k in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy"))


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None,
                 code: str | None = None, retry_after: float | None = None,
                 required_capability: str | None = None,
                 payload: dict[str, Any] | None = None):
        # None means "never got a real HTTP response" (DNS/connect/timeout) —
        # callers that need to branch on a specific status (e.g. 409 vs 410)
        # must check this instead of grepping the message, which can contain
        # a server URL/port or arbitrary error text that happens to embed the
        # same digits as a status code.
        super().__init__(message)
        self.status_code = status_code
        # Stable application error code from newer servers. ``None`` is
        # expected for transport failures and older deployments; callers must
        # retain a conservative compatibility fallback for those responses.
        self.code = code
        self.retry_after = retry_after
        # A 426 can mean either that the binary is too old or that one local
        # provider is not ready. Preserve the server's exact requirement so
        # run supervision never has to infer the provider from prose.
        self.required_capability = required_capability
        # Versioned endpoints can return a complete Agent-facing envelope.
        # Keep it as structured data so callers never have to parse prose.
        self.payload = payload


class ApiClient:
    def __init__(self, server: str, token: str,
                 transport: httpx.BaseTransport | None = None,
                 capabilities: tuple[str, ...] | list[str] | set[str] | None = None,
                 benchmark_id: str | None = None,
                 batch_id: str | None = None):
        self.server = server.rstrip("/")
        self.plan_scoped = token.startswith("drp_")
        self.benchmark_id = benchmark_id
        self.batch_id = normalize_batch_id(batch_id)
        # write=None: large uploads over a slow tunnel must not hit a write
        # timeout; keep a bounded connect/read so a dead server fails fast.
        # No header at all when tokenless (pre-registration): an empty
        # "Bearer " is an illegal header value.
        # transport is a test seam (httpx.MockTransport); when none is
        # injected, default to connect-phase retries: httpx re-attempts only
        # failed connection ESTABLISHMENT (DNS blip, refused/reset before the
        # request is sent) and never re-sends a request whose bytes went out,
        # so this is safe for the POST claim/submit endpoints (no duplicate
        # side effects). EXCEPT on proxied machines: httpx mounts
        # HTTP(S)_PROXY/ALL_PROXY only when no explicit transport is passed,
        # so there we keep httpx's default transport (proxy correctness
        # beats a connect-retry nicety — a good chunk of the volunteer pool
        # reaches the server only through a proxy).
        headers = {CLIENT_VERSION_HEADER: __version__}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        active_capabilities = normalize_capabilities(
            advertised_capabilities() if capabilities is None else capabilities
        )
        # Worker-pool parents hand this exact, already-evaluated snapshot to
        # their child processes. Re-probing stateful provider runtimes in
        # every child can produce a transiently different header even though
        # the parent just passed the same server gate.
        self.capabilities = active_capabilities
        if active_capabilities:
            headers[CLIENT_CAPABILITIES_HEADER] = ",".join(active_capabilities)
        if transport is None and not _env_proxies_set():
            transport = httpx.HTTPTransport(retries=2)
        self._client = httpx.Client(
            base_url=self.server,
            headers=headers,
            timeout=httpx.Timeout(30.0, write=None, read=120.0),
            transport=transport,
        )
        # Test seams also keep the retry policy deterministic without making
        # ordinary callers care about clocks or jitter.
        self._sleep = time.sleep
        self._jitter = random.uniform
        self._monotonic = time.monotonic
        self._wall_time = time.time
        self._submission_maintenance_retry_budget = (
            _SUBMISSION_MAINTENANCE_RETRY_BUDGET_SEC
        )
        self._submission_maintenance_deadlines: dict[str, float] = {}

    def _get(self, path: str) -> dict[str, Any]:
        return self._check(self._request("GET", path))

    def _post(self, path: str, **kw) -> dict[str, Any]:
        return self._check(self._request("POST", path, **kw))

    def _benchmark_path(self, path: str) -> str:
        if not self.benchmark_id:
            return path
        separator = "&" if "?" in path else "?"
        return (path + separator + "benchmark="
                + urllib.parse.quote(self.benchmark_id, safe=""))

    @staticmethod
    def _query_path(path: str, key: str, value: str | None) -> str:
        if value is None:
            return path
        separator = "&" if "?" in path else "?"
        return path + separator + key + "=" + urllib.parse.quote(value, safe="")

    def set_batch_id(self, batch_id: str | None) -> None:
        self.batch_id = normalize_batch_id(batch_id)

    def _request(
        self, method: str, path: str, *, retry_rate_limit: bool = True, **kw,
    ) -> httpx.Response:
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                response = self._client.request(method, path, **kw)
            except httpx.HTTPError as exc:  # transport-level: connect/timeout/etc.
                raise ApiError(f"cannot reach {self.server}: {exc}") from exc
            if (
                response.status_code != 429
                or not retry_rate_limit
                or attempt == _RATE_LIMIT_RETRIES
            ):
                return response
            retry_after = self._retry_after(response)
            # A small independent offset prevents a 20-worker pool from
            # waking on the same token boundary and recreating the 429 herd.
            jitter = self._jitter(0.0, min(1.0, retry_after * 0.1))
            self._sleep(retry_after + jitter)
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            value = float(resp.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            value = _DEFAULT_RETRY_AFTER_SEC
        return min(_MAX_RETRY_AFTER_SEC, max(_DEFAULT_RETRY_AFTER_SEC, value))

    def _maintenance_retry_after(self, resp: httpx.Response) -> float:
        """Return the server-directed maintenance delay without shortening it.

        Unlike the ordinary 429 backoff, a deployment fence is a promise that
        submission writes are temporarily unavailable.  Retrying before its
        HTTP ``Retry-After`` would defeat that fence, so a large delay is not
        capped here; the separate total retry budget decides whether it can be
        waited safely.  Both RFC HTTP-date and delta-seconds forms are accepted.
        """
        raw = resp.headers.get("Retry-After", "").strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(raw)
                value = retry_at.timestamp() - self._wall_time()
            except (TypeError, ValueError, OverflowError):
                value = _DEFAULT_MAINTENANCE_RETRY_AFTER_SEC
        if not value >= 0:  # also rejects NaN without importing math
            value = _DEFAULT_MAINTENANCE_RETRY_AFTER_SEC
        return max(_DEFAULT_MAINTENANCE_RETRY_AFTER_SEC, value)

    @staticmethod
    def _is_deployment_maintenance(exc: ApiError) -> bool:
        return (
            exc.status_code == 503
            and exc.code == "deployment_maintenance"
        )

    def _post_submission_write(
        self,
        path: str,
        *,
        maintenance_key: str | None = None,
        retain_maintenance_deadline: bool = False,
        **kw,
    ) -> dict[str, Any]:
        """Retry only an explicitly fenced submission write.

        The exact form/multipart payload (including session/owner/content
        fences) is replayed; no claim or model execution is reachable from
        this helper.  All other 5xx responses remain single-attempt failures.
        """
        deadline = (
            self._submission_maintenance_deadlines.get(maintenance_key)
            if maintenance_key is not None
            else None
        )
        if deadline is None:
            deadline = (
                self._monotonic()
                + self._submission_maintenance_retry_budget
            )
        while True:
            try:
                response = self._post(path, **kw)
            except ApiError as exc:
                if not self._is_deployment_maintenance(exc):
                    if maintenance_key is not None:
                        self._submission_maintenance_deadlines.pop(
                            maintenance_key, None,
                        )
                    raise
                retry_after = exc.retry_after
                if retry_after is None:
                    # _check supplies this for the structured maintenance
                    # envelope. Fail closed if a synthetic/custom client ever
                    # violates that invariant.
                    if maintenance_key is not None:
                        self._submission_maintenance_deadlines.pop(
                            maintenance_key, None,
                        )
                    raise
                remaining = max(0.0, deadline - self._monotonic())
                if (
                    retry_after > remaining
                    or retry_after > _MAX_SUBMISSION_MAINTENANCE_WAIT_SEC
                ):
                    if maintenance_key is not None:
                        self._submission_maintenance_deadlines.pop(
                            maintenance_key, None,
                        )
                    reason = (
                        "safe single-wait limit exceeded"
                        if retry_after > _MAX_SUBMISSION_MAINTENANCE_WAIT_SEC
                        else "retry budget exhausted"
                    )
                    raise ApiError(
                        f"{exc} (deployment maintenance {reason})",
                        status_code=exc.status_code,
                        code=exc.code,
                        retry_after=retry_after,
                        required_capability=exc.required_capability,
                        payload=exc.payload,
                    ) from exc
                self._sleep(retry_after)
                continue
            if maintenance_key is not None:
                if retain_maintenance_deadline:
                    self._submission_maintenance_deadlines[
                        maintenance_key
                    ] = deadline
                else:
                    self._submission_maintenance_deadlines.pop(
                        maintenance_key, None,
                    )
            return response

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            detail: Any = resp.text
            code = None
            required_capability = None
            payload = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    payload = body
                    detail = body.get("detail", resp.text)
                    raw_code = body.get("code")
                    if isinstance(raw_code, str):
                        code = raw_code
                    raw_capability = body.get("required_capability")
                    if isinstance(raw_capability, str):
                        required_capability = raw_capability
            except (json.JSONDecodeError, ValueError):
                pass
            raise ApiError(
                f"server returned {resp.status_code}: {detail}",
                status_code=resp.status_code,
                code=code,
                retry_after=(
                    self._retry_after(resp)
                    if resp.status_code == 429
                    else (
                        self._maintenance_retry_after(resp)
                        if (
                            resp.status_code == 503
                            and code == "deployment_maintenance"
                        )
                        else None
                    )
                ),
                required_capability=required_capability,
                payload=payload,
            )
        return resp.json()

    def register(self, nickname: str) -> dict[str, Any]:
        """Self-serve signup; returns {nickname, token}. No auth required."""
        return self._post("/api/v1/register", data={"nickname": nickname})

    def rename(self, nickname: str) -> dict[str, Any]:
        return self._post("/api/v1/rename", data={"nickname": nickname})

    def my_submissions(self) -> dict[str, Any]:
        """Returns {nickname, points, submissions: [...]} — the volunteer's
        own recent history including grading status/flags the public pages
        hide. 404 on servers that predate this endpoint."""
        return self._get("/api/v1/my-submissions")

    def github_config(self) -> dict[str, Any]:
        return self._get("/api/v1/github/config")

    def github_link(self, access_token: str) -> dict[str, Any]:
        return self._post("/api/v1/github/link", data={"access_token": access_token})

    def github_whoami(self, access_token: str) -> dict[str, Any]:
        return self._post("/api/v1/github/whoami", data={"access_token": access_token})

    def whoami(self) -> dict[str, Any]:
        if self.plan_scoped:
            return self._get("/api/v1/run-plans/identity")
        return self._get("/api/v1/whoami")

    def exchange_run_plan(
        self,
        *,
        run_code: str,
        device_id: str,
        device_name: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Exchange a high-entropy invitation for one plan-scoped token.

        The invitation is deliberately sent in the JSON body so the HTTP
        exchange does not put it in a URL, access log, or Authorization
        header. The initial ``dradar --plan`` invocation may contain it in
        argv; this method never forwards it to a worker process. It is called
        on a tokenless client.
        """
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_code": run_code,
            "device_id": device_id,
        }
        if device_name:
            payload["device_name"] = device_name
        if locale:
            payload["locale"] = locale
        return self._post(
            "/api/v1/run-plans/exchange", json=payload,
            retry_rate_limit=False,
        )

    def renew_run_plan_access(self) -> dict[str, Any]:
        return self._post(
            "/api/v1/run-plans/renew", json={"schema_version": 1},
            retry_rate_limit=False,
        )

    def start_run_plan(
        self,
        *,
        plan_id: str,
        logical_session_id: str,
        concurrency_mode: str,
        concurrency: int | None = None,
        decision: str | None = None,
        decision_token: str | None = None,
        concurrency_decision_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "plan_id": plan_id,
            "logical_session_id": logical_session_id,
            "concurrency_mode": concurrency_mode,
        }
        if concurrency is not None:
            payload["concurrency"] = concurrency
        if decision is not None:
            payload["decision"] = decision
        if decision_token is not None:
            payload["decision_token"] = decision_token
        if concurrency_decision_token is not None:
            payload["concurrency_decision_token"] = concurrency_decision_token
        return self._post(
            "/api/v1/run-plans/start", json=payload,
            retry_rate_limit=False,
        )

    def request_run_plan_concurrency_decision(
        self,
        *,
        plan_id: str,
        logical_session_id: str,
        user_reply: str,
        device_concurrency_limit: int,
        device_capacity_digest: str,
    ) -> dict[str, Any]:
        return self._post(
            "/api/v1/run-plans/concurrency-decisions",
            json={
                "schema_version": 1,
                "plan_id": plan_id,
                "logical_session_id": logical_session_id,
                "user_reply": user_reply,
                "device_concurrency_limit": device_concurrency_limit,
                "device_capacity_digest": device_capacity_digest,
            },
            retry_rate_limit=False,
        )

    def run_plan_progress(self, plan_id: str) -> dict[str, Any]:
        return self._post(
            "/api/v1/run-plans/progress",
            json={"schema_version": 1, "plan_id": plan_id},
            retry_rate_limit=False,
        )

    def stop_run_plan(
        self,
        *,
        plan_id: str,
        scope: str,
        decision_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "plan_id": plan_id,
            "scope": scope,
        }
        if decision_token is not None:
            payload["decision_token"] = decision_token
        return self._post(
            "/api/v1/run-plans/stop", json=payload,
            retry_rate_limit=False,
        )

    def benchmarks(self) -> dict[str, Any]:
        """Public benchmark catalog, including optional task-pack metadata."""
        return self._get("/api/v1/benchmarks")

    def download(self, path: str, destination: Path) -> str | None:
        """Stream one authenticated immutable artifact to ``destination``.

        The caller verifies the advertised digest before using the file.  A
        same-origin relative URL is required so a compromised catalog cannot
        redirect the bearer token to another host.
        """
        parsed = urllib.parse.urlparse(path)
        if parsed.scheme or parsed.netloc or not path.startswith("/"):
            raise ApiError("server advertised an unsafe download URL")
        try:
            with self._client.stream("GET", path) as response:
                if response.status_code >= 400:
                    response.read()
                    self._check(response)
                with destination.open("xb") as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        output.write(chunk)
                return response.headers.get("X-Content-SHA256")
        except httpx.HTTPError as exc:
            raise ApiError(f"cannot download from {self.server}: {exc}") from exc

    def get_assignment(self) -> dict[str, Any]:
        """Returns {active: [dict, ...], free_pick: bool, menu: list|None, ...}
        — `active` is the whole held batch to run, in claim order. Also carries
        legacy `assignment`/`resumed` (first active lease) for older clients."""
        path = self._benchmark_path("/api/v1/assignment")
        data = self._get(self._query_path(path, "batch_id", self.batch_id))
        if self.batch_id is None:
            return data
        # Compatibility guard during a rolling server deployment: if an old
        # server ignores batch_id, filter its broader inventory locally and
        # fail closed rather than starting another campaign's assignment.
        active = data.get("active")
        if active is None:
            one = data.get("assignment")
            active = [one] if one else []
        matching = []
        for item in active:
            if not item:
                continue
            try:
                item_batch_id = normalize_batch_id(item.get("batch_id"))
            except ValueError:
                continue
            if item_batch_id == self.batch_id:
                matching.append(item)
        if not matching:
            raise ApiError(
                "server returned 404: active claim batch not found",
                status_code=404,
                code="claim_batch_not_found",
            )
        scoped = dict(data)
        scoped["active"] = matching
        scoped["assignment"] = matching[0]
        scoped["resumed"] = True
        return scoped

    def get_assignment_inventory(self) -> dict[str, Any]:
        """Read account inventory without asserting local provider readiness.

        This is only for observational commands such as ``dradar leases``.
        Run, checkout, and start paths continue to use ``get_assignment`` and
        the server's exact per-assignment capability gate.
        """
        path = self._benchmark_path("/api/v1/assignment")
        return self._get(self._query_path(path, "inventory", "true"))

    def claim_assignment(
        self,
        task_id: str,
        model: str,
        effort: str,
        *,
        refill_campaign_id: str | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Returns {assignment: dict, resumed: False}. Raises ApiError (409) if
        the cell went stale or the volunteer is already at the concurrent cap."""
        data = {"task_id": task_id, "model": model, "effort": effort}
        if self.benchmark_id:
            data["benchmark_id"] = self.benchmark_id
        if self.batch_id:
            data["batch_id"] = self.batch_id
        if refill_campaign_id:
            data["refill_campaign_id"] = refill_campaign_id
        if tier is not None:
            data["tier"] = tier
        return self._post("/api/v1/assignment/claim", data=data)

    def configure_refill_campaign(
        self,
        *,
        batch_id: str,
        harness: str,
        model: str,
        effort: str,
        refill_to: int,
        max_tasks: int,
    ) -> dict[str, Any]:
        """Idempotently create the server-authoritative multi-machine plan."""
        return self._post(
            "/api/v1/refill-campaign/configure",
            json={
                "batch_id": batch_id,
                "benchmark_id": self.benchmark_id or "deep-swe",
                "harness": harness,
                "model": model,
                "effort": effort,
                "refill_to": refill_to,
                "max_tasks": max_tasks,
            },
        )

    def refill_campaign_status(self, batch_id: str) -> dict[str, Any]:
        path = self._query_path(
            "/api/v1/refill-campaign/status", "batch_id", batch_id,
        )
        return self._get(path)

    def stop_refill_campaign(self, batch_id: str, reason: str) -> dict[str, Any]:
        return self._post(
            "/api/v1/refill-campaign/stop",
            json={"batch_id": batch_id, "reason": reason[:300]},
        )

    def suggest(self, n: int) -> dict[str, Any]:
        """Weighted-random candidate cells (server-side balanced_random_cells,
        biased toward least-tested), same primitive behind the web's 雷达随机
        推荐 button — powers `dradar go --auto` so a headless/Agent run doesn't
        need a prior web claim. Returns {cells: [menu-entry dict, ...]};
        candidates only, not yet claimed. The server applies ordinary/super
        account-specific recommendation limits."""
        return self._get(self._benchmark_path(f"/api/v1/suggest?n={n}"))

    def table(self) -> dict[str, Any]:
        """Public full-board snapshot.

        Returns the server's task/config matrix plus the live state and
        coverage metadata for every cell.  Unlike suggest() this is not
        personalized and never claims or reserves work.
        """
        return self._get(self._benchmark_path("/api/v1/table"))

    def mark_started(
        self, assignment_id: str, session_id: str | None = None,
    ) -> dict[str, Any]:
        """Confirms the trial subprocess actually started (see runner.run_trial):
        extends a free-pick claim's short initial lease out to the normal
        window. Best-effort by design — callers should swallow ApiError
        rather than let a heartbeat failure abort a real trial. 404 on
        servers that predate this endpoint or on a menu-style lease
        that never had a short window to extend in the first place."""
        return self._post(
            "/api/v1/assignment/started",
            data={"assignment_id": assignment_id, "session_id": session_id or ""},
        )

    def checkout(
        self,
        exclude_assignment_ids: set[str] | list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically check out this volunteer's next not-yet-started cell —
        the primitive that makes parallel sessions safe: N concurrent callers
        get N different cells. Returns {assignment: dict|None, held, unstarted};
        assignment None means everything held is already checked out or done.
        exclude_assignment_ids lets one CLI session avoid immediately
        re-checking-out a cell that already failed locally in that session.
        404 on servers that predate the endpoint (caller falls back to the
        legacy whole-batch flow)."""
        excluded = sorted(set(exclude_assignment_ids or ()))
        data = {"exclude_assignment_ids": ",".join(excluded),
                "session_id": session_id or ""}
        if self.benchmark_id:
            data["benchmark_id"] = self.benchmark_id
        if self.batch_id:
            data["batch_id"] = self.batch_id
        return self._post(
            "/api/v1/assignment/checkout",
            data=data,
        )

    def release_assignments(
        self,
        assignment_ids: list[str] | None = None,
        *,
        release_all: bool = False,
        force: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Immediately release held cells owned by this volunteer.

        The server requires exactly one target mode: explicit IDs or
        release_all. Running cells are protected unless force=True. The
        operation is idempotent so a lost response can be retried safely.
        """
        ids = list(dict.fromkeys(assignment_ids or ()))
        return self._post(
            "/api/v1/assignments/release",
            data={
                "assignment_ids": ",".join(ids),
                "release_all": str(release_all).lower(),
                "force": str(force).lower(),
                "request_id": request_id or "",
            },
        )

    def runner_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Best-effort lease-observation heartbeat (owner protocol v3).

        It is deliberately JSON-only and bounded; the server stores current
        state plus five-minute aggregates, never prompts, patches or command
        output.  A short timeout keeps telemetry from holding up real work.
        """
        return self._post("/api/v1/runner/heartbeat", json=payload, timeout=3.0)

    def runner_close(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Close a runner session without releasing any held lease."""
        return self._post("/api/v1/runner/close", json=payload, timeout=3.0)

    def flight_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Idempotently upload privacy-allowlisted lifecycle events."""
        return self._post(
            "/api/v1/runner/flight-events", json={"events": events}, timeout=3.0,
        )

    def mark_stopped(
        self,
        assignment_id: str,
        *,
        defer_seconds: int = 300,
        session_id: str | None = None,
        owner_epoch: int | None = None,
        resume_generation: int | None = None,
        failure_kind: str | None = None,
        failure_diagnostic: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The counterpart of mark_started: this trial died client-side
        (build flake, agent crash, abandonment) with nothing uploaded, so the
        server should stop showing the cell as 解题中. Callers use bounded
        best-effort retries and surface a final failure; current servers also
        reopen session-bound work after its exact runner is stale. New clients
        fence this transition with owner_epoch."""
        data = {
            "assignment_id": assignment_id,
            "defer_seconds": str(defer_seconds),
            "session_id": session_id or "",
        }
        if owner_epoch is not None:
            data["owner_epoch"] = str(owner_epoch)
        if resume_generation is not None:
            data["resume_generation"] = str(resume_generation)
        if failure_kind:
            data["failure_kind"] = failure_kind
        if failure_diagnostic is not None:
            data["failure_diagnostic"] = json.dumps(
                failure_diagnostic, separators=(",", ":"), sort_keys=True,
            )
        return self._post(
            "/api/v1/assignment/stopped",
            # A cross-session cooldown keeps a second `--parallel` process
            # from immediately taking the same cell that just failed here.
            # A user-initiated Ctrl-C passes zero because the process exits
            # and an immediate explicit `dradar resume` must work. Older
            # servers ignore the extra form field harmlessly.
            data=data,
        )

    def submit(
        self,
        assignment_id: str,
        nonce: str,
        patch: Path,
        trajectory: Path | None,
        result: Path | None,
        client_meta: dict[str, Any],
        outcome: str = "completed",
        session_id: str | None = None,
        owner_epoch: int | None = None,
        resume_generation: int | None = None,
        trajectory_bundle: Path | None = None,
        upload_intent_id: str | None = None,
    ) -> dict[str, Any]:
        files: list[tuple[str, tuple[str, bytes]]] = [
            ("patch", ("model.patch", patch.read_bytes())),
        ]
        if trajectory and trajectory.exists():
            files.append(("trajectory", ("trajectory.json", trajectory.read_bytes())))
        if result and result.exists():
            files.append(("result", ("result.json", result.read_bytes())))
        if trajectory_bundle and trajectory_bundle.exists():
            files.append(("trajectory_bundle", (
                "trajectory_bundle.json", trajectory_bundle.read_bytes())))
        data = {
            "assignment_id": assignment_id,
            "nonce": nonce,
            "outcome": outcome,
            "client_meta": json.dumps(client_meta),
            "session_id": session_id or "",
        }
        if owner_epoch is not None:
            data["owner_epoch"] = str(owner_epoch)
        if resume_generation is not None:
            data["resume_generation"] = str(resume_generation)
        if upload_intent_id is not None:
            data["upload_intent_id"] = upload_intent_id
        return self._post_submission_write(
            "/api/v1/submissions",
            maintenance_key=upload_intent_id,
            data=data,
            files=files,
        )

    def register_submission_upload_intent(
        self,
        assignment_id: str,
        nonce: str,
        session_id: str,
        owner_epoch: int | None,
        upload_intent_id: str,
        *,
        resume_generation: int | None = None,
        intent_version: str = UPLOAD_INTENT_VERSION,
    ) -> str:
        """Precommit one exact payload before the potentially large POST."""
        data = {
            "assignment_id": assignment_id,
            "nonce": nonce,
            "session_id": session_id,
            "upload_intent_id": upload_intent_id,
            "intent_version": intent_version,
        }
        if owner_epoch is not None:
            data["owner_epoch"] = str(owner_epoch)
        if resume_generation is not None:
            data["resume_generation"] = str(resume_generation)
        self._post_submission_write(
            "/api/v1/submission-upload-intents",
            maintenance_key=upload_intent_id,
            retain_maintenance_deadline=True,
            data=data,
        )
        return upload_intent_id

    def rebind_submission_upload_salvage(
        self,
        assignment_id: str,
        nonce: str,
        source_session_id: str,
        source_owner_epoch: int,
        expected_owner_epoch: int,
        salvage_session_id: str,
    ) -> dict[str, Any]:
        """Explicitly bind one saved completed result to an upload-only owner.

        This endpoint never starts the model.  It only succeeds after the
        server proves the source runner owned this assignment and the current
        assignment has no live runner.
        """
        return self._post(
            "/api/v1/submission-upload-salvage/rebind",
            data={
                "assignment_id": assignment_id,
                "nonce": nonce,
                "source_session_id": source_session_id,
                "source_owner_epoch": str(source_owner_epoch),
                "expected_owner_epoch": str(expected_owner_epoch),
                "salvage_session_id": salvage_session_id,
            },
        )
