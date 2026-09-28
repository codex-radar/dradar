"""Small stdlib ACP v1 client for one isolated Kiro Pier prompt turn.

This file is copied into the task container. It never reads host credentials,
opens a browser, or starts a second scheduler. Pier owns the outer timeout and
task lifecycle; this process owns only its Kiro ACP child and one session.
"""

from __future__ import annotations

import json
import hashlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class ACPFailure(RuntimeError):
    pass


# Observed as one-way notifications in the approved 2.24.1 handshake and
# identified against the official bundle. They advertise state/catalogs;
# they do not request client work or acknowledge model/effort selection.
PASSIVE_KIRO_NOTIFICATIONS = frozenset({
    "_kiro.dev/commands/available", "_kiro/governance/state", "_kiro/mcp/status",
    "_kiro/powers/items_changed", "_kiro/steering/documents_changed",
    "_kiro/progressive_context/items_changed", "_kiro/hooks/didChange",
    "_kiro/tools/didChange", "_kiro/sessions/changed",
})


def _option(options: object, name: str, phase: str) -> dict:
    # Only fixed protocol stages and option names reach this helper. Error
    # codes expose the failed handshake step, never provider response values.
    safe_name = {"model": "model", "effortLevel": "effort"}[name]
    if not isinstance(options, list):
        raise ACPFailure(f"config_{phase}_options_missing")
    matches = [item for item in options if isinstance(item, dict) and item.get("id") == name]
    if not matches:
        raise ACPFailure(f"config_{phase}_{safe_name}_missing")
    if len(matches) > 1:
        raise ACPFailure(f"config_{phase}_{safe_name}_duplicate")
    return matches[0]


def _offered(option: dict, value: str) -> bool:
    choices = option.get("options")
    if not isinstance(choices, list):
        return False
    flat = []
    for item in choices:
        if isinstance(item, dict):
            flat.extend(item.get("options", []) if isinstance(item.get("options"), list) else [item])
    return any(isinstance(item, dict) and item.get("value") == value for item in flat)


def _probe_pwd_check(value: object) -> tuple[str | None, dict]:
    """Classify a probe input without retaining command text or unknown fields."""
    diagnostic = {"shape": "other", "commandKey": "none", "commandIsPwd": False,
                  "fieldsAllowed": False, "background": "absent"}
    if isinstance(value, str):
        diagnostic["shape"] = "string"
        diagnostic["commandIsPwd"] = value == "pwd"
        return "probe_input_shape_unsupported", diagnostic
    if not isinstance(value, dict):
        return "probe_input_shape_unsupported", diagnostic
    diagnostic["shape"] = "object"
    command_keys = [key for key in ("command", "cmd", "command_line") if key in value]
    if len(command_keys) != 1:
        diagnostic["commandKey"] = "multiple" if command_keys else "missing"
        return "probe_object_command_key_invalid", diagnostic
    command_key = command_keys[0]
    diagnostic["commandKey"] = command_key
    diagnostic["commandIsPwd"] = value[command_key] == "pwd"
    diagnostic["fieldsAllowed"] = set(value) <= {
        command_key, "description", "timeout", "timeout_ms", "run_in_background"}
    if "run_in_background" in value:
        background = value["run_in_background"]
        diagnostic["background"] = ("false" if background is False else
                                    "true" if background is True else "invalid")
    if not diagnostic["commandIsPwd"]:
        return "probe_object_command_not_pwd", diagnostic
    if not diagnostic["fieldsAllowed"]:
        return "probe_object_fields_unsupported", diagnostic
    if diagnostic["background"] == "absent":
        return "probe_background_unspecified", diagnostic
    if diagnostic["background"] != "false":
        return "probe_background_forbidden", diagnostic
    return None, diagnostic


class ACPClient:
    def __init__(self, cli: str, stream: Path, *, handshake_only: bool = False,
                 handshake_diagnostic: bool = False):
        self.stream = stream
        self.events = stream.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [cli, "acp", "--agent-engine", "v3", "--auth-method", "cli"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, start_new_session=True,
        )
        self.incoming: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.next_id = 1
        self.session_id: str | None = None
        self.prompt_pending = False
        self.cancelled = threading.Event()
        self.cancel_sent = False
        self.unexpected_request = False
        self.required_config: tuple[str, str] | None = None
        self.registry_config: object = None
        self.registry_target: tuple[str, str] | None = None
        self.registry_model_confirmed = False
        self.config_drift = False
        self.handshake_only = handshake_only or handshake_diagnostic
        self.handshake_diagnostic = handshake_diagnostic
        self.handshake_metadata = self.handshake_only and os.environ.get("DRADAR_KIRO_HANDSHAKE_METADATA") == "1"
        self.handshake_envelopes = 0
        self.ignored_kiro_notifications = 0
        self.protocol_phase = "initialize"
        self.pending_metadata_session_ids: set[str] = set()
        self.probe_pwd_only = os.environ.get("DRADAR_KIRO_PROBE_PWD_ONLY") == "1"
        self.probe_native_local = os.environ.get("DRADAR_KIRO_PROBE_NATIVE_LOCAL") == "1"
        if self.probe_pwd_only and self.probe_native_local:
            raise ACPFailure("probe_modes_conflict")
        self.probe_tool_id: str | None = None
        self.probe_raw_input: object = None
        self.probe_tool_count = 0
        self.probe_permission_count = 0
        self.probe_completed = False
        self.local_tool_id: str | None = None
        self.local_permission_tool_id: str | None = None
        self.local_tool_count = 0
        self.local_permission_count = 0
        self.local_completed = False

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.incoming.put(line)
        finally:
            self.incoming.put(None)

    def _send(self, message: dict) -> None:
        if self.proc.poll() is not None or self.proc.stdin is None:
            raise ACPFailure("agent_exited")
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ACPFailure("agent_pipe_closed") from exc

    def _event(self, kind: str, data: dict) -> None:
        self.events.write(json.dumps({"type": kind, "data": data}, ensure_ascii=False) + "\n")
        self.events.flush()

    def _cancel(self) -> None:
        if self.session_id and self.prompt_pending and not self.cancel_sent:
            self._send({"jsonrpc": "2.0", "method": "session/cancel",
                        "params": {"sessionId": self.session_id}})
            self.cancel_sent = True

    def _observe_envelope(self, message: object) -> None:
        if not self.handshake_metadata:
            return
        self.handshake_envelopes += 1
        if self.handshake_envelopes > 96:
            raise ACPFailure("handshake_diagnostic_flood")
        def shape(value: object) -> str:
            if value is None:
                return "absent"
            if isinstance(value, dict):
                return "object"
            if isinstance(value, list):
                return "list"
            return "other"
        def digest(value: object) -> str | None:
            return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None
        obj = message if isinstance(message, dict) else {}
        params = obj.get("params")
        fields = params if isinstance(params, dict) else {}
        update = fields.get("update")
        core = update.get("sessionUpdate") if isinstance(update, dict) else None
        status = fields.get("status")
        reason = fields.get("disabledReason")
        envelope = ("request" if "id" in obj else "notification") if "method" in obj else "response"
        if not isinstance(message, dict):
            envelope = "other"
        self._event("handshakeEnvelope", {
            "phase": self.protocol_phase, "envelope": envelope,
            "methodSha256": digest(obj.get("method")), "coreKindSha256": digest(core),
            "paramsShape": shape(params), "updateShape": shape(update),
            "status": status if isinstance(status, str) and status in ("success", "failed") else "absent" if status is None else "other",
            "governance": reason if isinstance(reason, str) and reason in ("admin_disabled", "api_failure") else "absent" if reason is None else "other",
            "hasError": bool(fields.get("error")), "hasErrors": bool(fields.get("errors")),
            "responseError": "error" in obj,
            "powersShape": shape(fields.get("powers")),
            "documentsShape": shape(fields.get("documents")),
            "serversShape": shape(fields.get("servers")),
        })

    def _metadata_scope(self, params: object, *, require_session: bool = False) -> None:
        if not isinstance(params, dict):
            raise ACPFailure("handshake_metadata_shape_invalid")
        sid = params.get("sessionId")
        if sid is None and not require_session:
            return
        if not isinstance(sid, str) or not sid:
            raise ACPFailure("handshake_foreign_session")
        if self.session_id is None:
            self.pending_metadata_session_ids.add(sid)
            if len(self.pending_metadata_session_ids) > 1:
                raise ACPFailure("handshake_foreign_session")
        elif sid != self.session_id:
            raise ACPFailure("handshake_foreign_session")

    def _count_metadata(self) -> None:
        self.ignored_kiro_notifications += 1
        if self.ignored_kiro_notifications > 32:
            raise ACPFailure("handshake_notification_flood")

    def _handshake_notification(self, method: str, params: object = None) -> None:
        if not isinstance(method, str):
            self._rejected_message(method)
            raise ACPFailure("handshake_unexpected_message")
        if self.handshake_diagnostic and isinstance(method, str) and method.startswith("_"):
            # Diagnostic-only ACP extension observation. This mode cannot send
            # a prompt or grant tool permission; it does not change production
            # acceptance of any notification, nor prove it safe to ignore.
            self._rejected_message(method, kind="handshakeNotice")
            self._count_metadata()
            return
        if method not in PASSIVE_KIRO_NOTIFICATIONS:
            self._rejected_message(method)
            raise ACPFailure("handshake_unexpected_message")
        if method == "_kiro/sessions/changed":
            if not isinstance(params, dict) or not isinstance(params.get("upserted"), list) or not isinstance(params.get("deleted"), list):
                raise ACPFailure("handshake_metadata_shape_invalid")
            # The private ACP process creates one local session. Roster entries
            # bind to that session too; no global/foreign roster is accepted.
            for entry in params["upserted"]:
                self._metadata_scope(entry, require_session=True)
                if (entry.get("status") not in (None, "idle") or entry.get("provisioningFailure")
                        or entry.get("instanceStatus") == "failed" or entry.get("error") or entry.get("errors")):
                    raise ACPFailure("handshake_metadata_failed")
            if params["deleted"]:
                raise ACPFailure("handshake_metadata_failed")
        else:
            self._metadata_scope(params, require_session=method != "_kiro.dev/commands/available")
        # These method names carry both success and failure payloads in Kiro
        # 2.24.1. Never let a catalog-load failure become a successful run.
        if (params.get("error") or params.get("errors")
                or params.get("status") not in (None, "success")):
            raise ACPFailure("handshake_metadata_failed")
        catalog = {
            "_kiro/powers/items_changed": "powers",
            "_kiro/steering/documents_changed": "documents",
            "_kiro/progressive_context/items_changed": "items",
        }.get(method)
        if catalog:
            if params.get("status") != "success":
                raise ACPFailure("handshake_metadata_failed")
            if not isinstance(params.get(catalog), list):
                raise ACPFailure("handshake_metadata_shape_invalid")
        elif method in ("_kiro/hooks/didChange", "_kiro/tools/didChange"):
            field = "hooks" if method == "_kiro/hooks/didChange" else "tags"
            if not isinstance(params.get(field), list):
                raise ACPFailure("handshake_metadata_shape_invalid")
        elif method == "_kiro/mcp/status":
            if not isinstance(params.get("servers"), list):
                raise ACPFailure("handshake_metadata_shape_invalid")
        elif method == "_kiro/governance/state":
            if params.get("disabledReason") == "api_failure":
                raise ACPFailure("handshake_metadata_failed")
            # admin_disabled describes feature policy, not model failure.
            # Keep provider policy intact; this notification grants nothing.
            if (not isinstance(params.get("isEnterprise"), bool)
                    or not isinstance(params.get("features"), dict)
                    or params.get("disabledReason") not in (None, "admin_disabled")):
                raise ACPFailure("handshake_metadata_shape_invalid")
        self._count_metadata()

    def _rejected_message(self, method: object, *, kind: str = "handshakeRejected") -> None:
        # Match the hash against installed protocol definitions offline. Never
        # persist arbitrary provider strings, params, URLs or session payloads.
        self._event(kind, {
            "phase": self.protocol_phase,
            "methodType": "string" if isinstance(method, str) else "other",
            "methodSha256": hashlib.sha256(method.encode()).hexdigest() if isinstance(method, str) else None,
        })

    def _handle_update(self, message: dict) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            if self.handshake_only or not self.prompt_pending:
                raise ACPFailure("handshake_malformed_update")
            return
        update = params.get("update")
        if not isinstance(update, dict):
            if self.handshake_only or not self.prompt_pending:
                raise ACPFailure("handshake_malformed_update")
            return
        kind = update.get("sessionUpdate")
        if (self.handshake_only or not self.prompt_pending) and kind == "available_commands_update":
            # ACP's standard slash-command advertisement is not invocation.
            # No command text is copied into a prompt or executed by this client.
            self._metadata_scope(params, require_session=True)
            if not isinstance(update.get("availableCommands"), list):
                raise ACPFailure("handshake_metadata_shape_invalid")
            self._count_metadata()
            return
        if (self.handshake_only or not self.prompt_pending) and kind == "session_info_update":
            # Kiro's initialization emits a context-usage display update using
            # this core envelope. Other info variants include errors and turn
            # execution: only the source-proven context_usage variant is passive.
            self._metadata_scope(params, require_session=True)
            meta = update.get("_meta")
            info = meta.get("kiro") if isinstance(meta, dict) else None
            if (not isinstance(info, dict) or info.get("kind") != "context_usage"
                    or set(info) - {"kind", "usagePercentage", "contextUsage", "breakdown"}):
                raise ACPFailure("handshake_unexpected_update")
            percentage = info.get("usagePercentage")
            display = info.get("contextUsage")
            if (type(percentage) not in (int, float) or not 0 <= percentage <= 100
                    or not isinstance(display, dict) or set(display) != {"usagePercentage"}
                    or type(display.get("usagePercentage")) not in (int, float)
                    or display["usagePercentage"] != percentage):
                raise ACPFailure("handshake_metadata_shape_invalid")
            self._count_metadata()
            return
        if (self.handshake_only or not self.prompt_pending) and kind in ("tool_call", "tool_call_update"):
            raise ACPFailure("handshake_unexpected_tool")
        if (self.handshake_only or not self.prompt_pending) and kind != "config_option_update":
            self._rejected_message(kind, kind="handshakeUpdateRejected")
            raise ACPFailure("handshake_unexpected_update")
        if params.get("sessionId") != self.session_id:
            if self.handshake_only or not self.prompt_pending:
                raise ACPFailure("handshake_foreign_session")
            return
        if kind == "config_option_update" and self.registry_target is not None:
            self.validate_registry_update(update.get("configOptions"))
            self.registry_config = update.get("configOptions")
        if kind == "config_option_update" and self.required_config is not None:
            try:
                observed = (_option(update.get("configOptions"), "model", "update").get("currentValue"),
                            _option(update.get("configOptions"), "effortLevel", "update").get("currentValue"))
            except ACPFailure as exc:
                self.config_drift = True
                drift_reason = str(exc)
            else:
                self.config_drift = observed != self.required_config
                drift_reason = "value_mismatch" if self.config_drift else None
            if self.config_drift:
                self._event("configDrift", {"sessionId": self.session_id,
                                            "reason": drift_reason})
            elif not self.handshake_only:
                self._event("configConfirmed", {"sessionId": self.session_id})
        elif kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                self._event("sessionUpdate", {"update": {"sessionUpdate": kind, "content": content}})
        elif kind in ("tool_call", "tool_call_update"):
            if self.probe_pwd_only:
                call_id = update.get("toolCallId")
                if not isinstance(call_id, str) or not call_id:
                    self._cancel()
                    raise ACPFailure("probe_tool_id_invalid")
                if kind == "tool_call":
                    self.probe_tool_count += 1
                    if self.probe_tool_count != 1:
                        self._cancel()
                        raise ACPFailure("probe_multiple_tools")
                    self.probe_tool_id = call_id
                elif self.probe_tool_id is None:
                    self._cancel()
                    raise ACPFailure("probe_tool_update_before_start")
                if call_id != self.probe_tool_id:
                    self._cancel()
                    raise ACPFailure("probe_tool_id_mismatch")
                if update.get("kind", "execute") != "execute":
                    self._cancel()
                    raise ACPFailure("probe_tool_kind_invalid")
                if "rawInput" in update and update["rawInput"] is not None:
                    self.probe_raw_input = update["rawInput"]
                    reason, diagnostic = _probe_pwd_check(self.probe_raw_input)
                    self._event("probeInputCheck", {"phase": "tool_update",
                                                    "toolCount": self.probe_tool_count,
                                                    **diagnostic})
                    if reason:
                        self._cancel()
                        raise ACPFailure(reason)
                status = update.get("status")
                if status is not None and status not in (
                        "pending", "in_progress", "completed", "failed"):
                    self._cancel()
                    raise ACPFailure("probe_tool_status_invalid")
                if status == "failed":
                    self._cancel()
                    raise ACPFailure("probe_tool_failed")
                if self.probe_completed and status in ("pending", "in_progress"):
                    self._cancel()
                    raise ACPFailure("probe_tool_status_regressed")
                if status == "completed":
                    if self.probe_permission_count != 1:
                        self._cancel()
                        raise ACPFailure("probe_completion_before_permission")
                    self.probe_completed = True
            elif self.probe_native_local:
                # These notifications are retrospective evidence, not a
                # pre-execution command boundary. The native session verifier
                # checks the actual tool and result after the turn.
                call_id = update.get("toolCallId")
                if not isinstance(call_id, str) or not call_id:
                    self._cancel()
                    raise ACPFailure("local_tool_id_invalid")
                if kind == "tool_call":
                    self.local_tool_count += 1
                    if self.local_tool_count != 1:
                        self._cancel()
                        raise ACPFailure("local_multiple_tools_observed")
                    self.local_tool_id = call_id
                elif self.local_tool_id is None:
                    self._cancel()
                    raise ACPFailure("local_tool_update_before_start")
                if call_id != self.local_tool_id or update.get("kind", "execute") != "execute":
                    self._cancel()
                    raise ACPFailure("local_tool_mismatch")
                if self.local_permission_tool_id is not None and call_id != self.local_permission_tool_id:
                    self._cancel()
                    raise ACPFailure("local_permission_tool_mismatch")
                status = update.get("status")
                if status is not None and status not in (
                        "pending", "in_progress", "completed", "failed"):
                    self._cancel()
                    raise ACPFailure("local_tool_status_invalid")
                if status == "failed" or (self.local_completed and status in ("pending", "in_progress")):
                    self._cancel()
                    raise ACPFailure("local_tool_failed_or_regressed")
                if status == "completed":
                    self.local_completed = True
            # Retain the tool lifecycle for trajectory reconstruction, without
            # duplicating raw command arguments or output into collected logs.
            safe = {"sessionUpdate": kind}
            for key in ("toolCallId", "status", "kind"):
                if isinstance(update.get(key), str):
                    safe[key] = update[key]
            self._event("sessionUpdate", {"update": safe})

    def _handle_request(self, message: dict) -> None:
        request_id = message["id"]
        method = message.get("method")
        if method == "session/request_permission":
            if self.handshake_only or not self.prompt_pending:
                self._send({"jsonrpc": "2.0", "id": request_id,
                            "result": {"outcome": {"outcome": "cancelled"}}})
                raise ACPFailure("handshake_permission_denied")
            params = message.get("params")
            options = params.get("options") if isinstance(params, dict) else None
            choice = None
            probe_rejection = None
            if self.probe_pwd_only:
                tool = params.get("toolCall") if isinstance(params, dict) else None
                if not isinstance(tool, dict) or params.get("sessionId") != self.session_id:
                    probe_rejection = "probe_permission_shape_invalid"
                elif self.probe_tool_count != 1 or tool.get("toolCallId") != self.probe_tool_id:
                    probe_rejection = "probe_permission_tool_mismatch"
                elif tool.get("kind", "execute") != "execute":
                    probe_rejection = "probe_permission_kind_invalid"
                elif self.probe_permission_count != 0:
                    probe_rejection = "probe_multiple_permissions"
                else:
                    raw_input = tool.get("rawInput", self.probe_raw_input)
                    probe_rejection, diagnostic = _probe_pwd_check(raw_input)
                    self._event("probeInputCheck", {"phase": "permission",
                                                    "toolCount": self.probe_tool_count,
                                                    "permissionCount": self.probe_permission_count,
                                                    **diagnostic})
            elif self.probe_native_local:
                # Kiro may auto-allow its default read-only tool. If it asks,
                # grant only this local action once; this does not constrain
                # operations the provider may have auto-allowed.
                tool = params.get("toolCall") if isinstance(params, dict) else None
                if not isinstance(tool, dict) or params.get("sessionId") != self.session_id:
                    probe_rejection = "local_permission_shape_invalid"
                elif not isinstance(tool.get("toolCallId"), str) or not tool["toolCallId"]:
                    probe_rejection = "local_permission_id_invalid"
                elif tool.get("kind", "execute") != "execute":
                    probe_rejection = "local_permission_kind_invalid"
                elif self.local_completed:
                    probe_rejection = "local_permission_after_completion"
                elif self.local_permission_count or (
                        self.local_tool_id is not None
                        and tool.get("toolCallId") != self.local_tool_id):
                    probe_rejection = "local_permission_extra_or_mismatch"
                else:
                    probe_rejection, _diagnostic = _probe_pwd_check(tool.get("rawInput"))
                    if probe_rejection:
                        probe_rejection = "local_permission_not_target"
            if isinstance(options, list):
                for item in options:
                    if isinstance(item, dict) and item.get("kind") == "allow_once":
                        candidate = item.get("optionId", item.get("id"))
                        if isinstance(candidate, str) and candidate:
                            choice = candidate
                            break
            if self.cancelled.is_set() or self.unexpected_request or probe_rejection or not choice:
                outcome = {"outcome": "cancelled"}
            else:
                outcome = {"outcome": "selected", "optionId": choice}
                if self.probe_pwd_only:
                    self.probe_permission_count += 1
                if self.probe_native_local:
                    self.local_permission_count += 1
                    self.local_permission_tool_id = tool["toolCallId"]
                    self._event("localPermissionSelected", {
                        "sessionId": self.session_id,
                        "permissionCount": self.local_permission_count})
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})
            if probe_rejection:
                raise ACPFailure(probe_rejection)
            if self.unexpected_request:
                raise ACPFailure("unsupported_client_request")
            if not choice and not self.cancelled.is_set():
                raise ACPFailure("probe_allow_once_missing" if self.probe_pwd_only
                                 else "unsupported_client_request")
            return
        # No client filesystem or terminal capability was advertised. A new
        # reverse request must not be silently treated as successful work.
        self.unexpected_request = True
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Unsupported client request"}})
        raise ACPFailure("unsupported_client_request")

    def request(self, method: str, params: dict, timeout: float | None = 30) -> dict:
        if self.cancelled.is_set():
            raise ACPFailure("cancelled")
        self.protocol_phase = ({"initialize": "initialize", "session/new": "new_session",
                                "session/prompt": "prompt"}.get(method)
                               or ("model_set" if params.get("configId") == "model" else "effort_set"))
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout if timeout is not None else None
        cancelled_deadline = None
        while True:
            if self.cancelled.is_set():
                self._cancel()
                if cancelled_deadline is None:
                    cancelled_deadline = time.monotonic() + 8
                if time.monotonic() >= cancelled_deadline:
                    raise ACPFailure("cancel_timeout")
            if deadline is not None and time.monotonic() >= deadline:
                self._cancel()
                raise ACPFailure("request_timeout")
            try:
                line = self.incoming.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                raise ACPFailure("agent_exited")
            try:
                message = json.loads(line)
                self._observe_envelope(message)
            except json.JSONDecodeError as exc:
                raise ACPFailure("invalid_json_rpc") from exc
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ACPFailure("invalid_json_rpc")
            if "method" in message:
                if "id" in message:
                    self._handle_request(message)
                elif message["method"] in ("session/update", "session/notification"):
                    self._handle_update(message)
                    if self.config_drift:
                        self._cancel()
                        raise ACPFailure("config_drift")
                elif self.handshake_only or not self.prompt_pending:
                    self._handshake_notification(message["method"], message.get("params"))
                continue
            if message.get("id") != request_id:
                raise ACPFailure("unexpected_response")
            if "error" in message:
                raise ACPFailure("rpc_" + method.replace("/", "_"))
            result = message.get("result")
            if not isinstance(result, dict):
                raise ACPFailure("invalid_response")
            if self.cancelled.is_set():
                raise ACPFailure("cancelled")
            if self.unexpected_request:
                raise ACPFailure("unsupported_client_request")
            return result

    def validate_registry_update(self, options: object) -> bool:
        """Validate every update, so a later valid one cannot hide a bad one."""
        try:
            selected = _option(options, "model", "model_set")
        except ACPFailure as exc:
            if str(exc) == "config_model_set_model_missing" and not self.registry_model_confirmed:
                return False
            raise
        assert self.registry_target is not None
        model, effort = self.registry_target
        if selected.get("currentValue") != model:
            raise ACPFailure("model_not_selected")
        if not _offered(selected, model):
            raise ACPFailure("model_unavailable")
        if not _offered(_option(options, "effortLevel", "model_set"), effort):
            raise ACPFailure("effort_unavailable")
        self.registry_model_confirmed = True
        return True

    def await_model_registry(self, timeout: float = 10.0) -> list:
        """Wait once for Kiro 2.24.1's asynchronous model registry push.

        set_config_option applies the model immediately, but the official
        server omits its selector while the model registry is empty. Its
        registry manager later pushes the complete session config. Never
        infer selection from our request or issue a prompt to warm it up.
        """
        self.protocol_phase = "registry_wait"
        deadline = time.monotonic() + timeout
        updates = 0
        if self.registry_config is not None:
            if self.validate_registry_update(self.registry_config):
                return self.registry_config
        while True:
            if self.cancelled.is_set():
                raise ACPFailure("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ACPFailure("config_model_registry_timeout")
            try:
                line = self.incoming.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise ACPFailure("agent_exited")
            try:
                message = json.loads(line)
                self._observe_envelope(message)
            except json.JSONDecodeError as exc:
                raise ACPFailure("invalid_json_rpc") from exc
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ACPFailure("invalid_json_rpc")
            if "id" in message:
                raise ACPFailure("config_registry_unexpected_request")
            method = message.get("method")
            if (isinstance(method, str) and method in PASSIVE_KIRO_NOTIFICATIONS) or (
                    self.handshake_diagnostic and isinstance(method, str) and method.startswith("_")):
                self._handshake_notification(method, message.get("params"))
                continue
            params = message.get("params")
            if (method not in ("session/update", "session/notification")
                    or not isinstance(params, dict)
                    or params.get("sessionId") != self.session_id):
                raise ACPFailure("config_registry_unexpected_message")
            update = params.get("update")
            if isinstance(update, dict) and update.get("sessionUpdate") in ("available_commands_update", "session_info_update"):
                self._handle_update(message)
                continue
            if not isinstance(update, dict) or update.get("sessionUpdate") != "config_option_update":
                raise ACPFailure("config_registry_unexpected_update")
            updates += 1
            if updates > 16:
                raise ACPFailure("config_registry_update_flood")
            options = update.get("configOptions")
            if self.validate_registry_update(options):
                return options

    def observe_handshake(self, grace: float = 0.3) -> None:
        """Catch immediate post-ack drift without issuing a prompt or tool call."""
        self.protocol_phase = "final_confirmation"
        deadline = time.monotonic() + grace
        while True:
            if self.cancelled.is_set():
                raise ACPFailure("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = self.incoming.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise ACPFailure("handshake_agent_exited")
            try:
                message = json.loads(line)
                self._observe_envelope(message)
            except json.JSONDecodeError as exc:
                raise ACPFailure("invalid_json_rpc") from exc
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ACPFailure("invalid_json_rpc")
            method = message.get("method")
            if "id" in message and method:
                self._handle_request(message)
                if self.unexpected_request:
                    raise ACPFailure("unsupported_client_request")
            elif method in ("session/update", "session/notification"):
                params = message.get("params")
                if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
                    raise ACPFailure("handshake_foreign_session")
                self._handle_update(message)
                if self.config_drift:
                    raise ACPFailure("config_drift")
            elif "id" not in message and ((isinstance(method, str) and method in PASSIVE_KIRO_NOTIFICATIONS) or (
                    self.handshake_diagnostic and isinstance(method, str) and method.startswith("_"))):
                self._handshake_notification(method, message.get("params"))
            else:
                self._rejected_message(method)
                raise ACPFailure("handshake_unexpected_message")

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self._cancel()
            except ACPFailure:
                pass
            try:
                self.proc.wait(timeout=2 if self.cancel_sent else 0.2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait(timeout=2)
        self.events.close()


def run(cli: str, stream: Path, model: str, effort: str, instruction: str,
        *, handshake_only: bool = False, handshake_diagnostic: bool = False) -> None:
    handshake_only = handshake_only or handshake_diagnostic
    client = ACPClient(cli, stream, handshake_only=handshake_only,
                       handshake_diagnostic=handshake_diagnostic)
    old_term = signal.signal(signal.SIGTERM, lambda _s, _f: client.cancelled.set())
    old_int = signal.signal(signal.SIGINT, lambda _s, _f: client.cancelled.set())
    try:
        client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                      "clientInfo": {"name": "dradar-pier", "version": "1"}})
        created = client.request("session/new", {"cwd": os.getcwd(), "mcpServers": []})
        sid = created.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise ACPFailure("session_missing")
        client.session_id = sid
        if client.pending_metadata_session_ids - {sid}:
            raise ACPFailure("handshake_foreign_session")
        client.pending_metadata_session_ids.clear()
        # Kiro 2.24.1 can return session/new before its model selector is
        # populated. The set response is the required source of truth.
        initial = created.get("configOptions")
        initial_model = next((item for item in initial
                              if isinstance(item, dict) and item.get("id") == "model"), None) if isinstance(initial, list) else None
        if initial_model is not None and not _offered(initial_model, model):
            raise ACPFailure("model_unavailable")
        client.registry_config = None
        client.registry_target = (model, effort)
        selected = client.request("session/set_config_option", {
            "sessionId": sid, "configId": "model", "value": model})
        options = selected.get("configOptions")
        try:
            selected_model = _option(options, "model", "model_set")
        except ACPFailure as exc:
            if str(exc) != "config_model_set_model_missing":
                raise
            options = client.await_model_registry()
            selected_model = _option(options, "model", "model_set")
        if selected_model.get("currentValue") != model:
            raise ACPFailure("model_not_selected")
        if not _offered(selected_model, model):
            raise ACPFailure("model_unavailable")
        effort_option = _option(options, "effortLevel", "model_set")
        if not _offered(effort_option, effort):
            raise ACPFailure("effort_unavailable")
        client.registry_model_confirmed = True
        selected = client.request("session/set_config_option", {
            "sessionId": sid, "configId": "effortLevel", "value": effort})
        if (_option(selected.get("configOptions"), "model", "effort_set").get("currentValue") != model
                or _option(selected.get("configOptions"), "effortLevel", "effort_set").get("currentValue") != effort):
            raise ACPFailure("config_not_selected")
        client.required_config = (model, effort)
        client.registry_target = None
        # Consume already queued/post-ACK config updates before any inference.
        # All clients use the same bounded no-tools confirmation window.
        client.observe_handshake()
        client._event("configSelected", {"sessionId": sid, "model": model, "effort": effort})
        if handshake_only:
            client._event("configHandshake", {"sessionId": sid, "status": "selected",
                                              "ignoredKiroNotifications":
                                              client.ignored_kiro_notifications})
            return
        client.prompt_pending = True
        try:
            response = client.request("session/prompt", {
                "sessionId": sid, "prompt": [{"type": "text", "text": instruction}]}, timeout=None)
        finally:
            client.prompt_pending = False
        if response.get("stopReason") != "end_turn":
            raise ACPFailure("prompt_not_completed")
        if client.probe_pwd_only:
            if client.probe_tool_count != 1:
                raise ACPFailure("probe_tool_count_invalid")
            if client.probe_permission_count != 1:
                raise ACPFailure("probe_permission_count_invalid")
            if not client.probe_completed:
                raise ACPFailure("probe_tool_not_completed")
        if client.probe_native_local:
            if client.local_tool_count != 1 or not client.local_completed:
                raise ACPFailure("local_tool_lifecycle_incomplete")
        client._event("runFinished", {"sessionId": sid, "status": "success",
                                      "stopReason": "end_turn"})
    except ACPFailure:
        if client.handshake_metadata:
            client._event("handshakeFailure", {"phase": client.protocol_phase})
        if client.probe_pwd_only:
            client._event("probeFailureCounts", {
                "toolCount": client.probe_tool_count,
                "permissionCount": client.probe_permission_count})
        if client.probe_native_local:
            client._event("localProbeFailureCounts", {
                "toolCount": client.local_tool_count,
                "permissionCount": client.local_permission_count})
        raise
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        client.close()


def main() -> int:
    if len(sys.argv) not in (6, 7) or (len(sys.argv) == 7 and sys.argv[6] not in (
            "--handshake-only", "--handshake-diagnostic")):
        print("DRADAR_KIRO_ACP=invalid_arguments", file=sys.stderr)
        return 2
    try:
        run(sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],
            handshake_only=len(sys.argv) == 7,
            handshake_diagnostic=len(sys.argv) == 7 and sys.argv[6] == "--handshake-diagnostic")
    except (ACPFailure, OSError) as exc:
        code = str(exc) if isinstance(exc, ACPFailure) else "process_error"
        print("DRADAR_KIRO_ACP=" + code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
