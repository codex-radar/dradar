"""Small stdlib ACP v1 client for one isolated Kiro Pier prompt turn.

This file is copied into the task container. It never reads host credentials,
opens a browser, or starts a second scheduler. Pier owns the outer timeout and
task lifecycle; this process owns only its Kiro ACP child and one session.
"""

from __future__ import annotations

import json
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
    def __init__(self, cli: str, stream: Path, *, handshake_only: bool = False):
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
        self.config_drift = False
        self.handshake_only = handshake_only
        self.ignored_kiro_notifications = 0
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

    def _handshake_notification(self, method: str) -> None:
        if method != "_kiro.dev/commands/available":
            raise ACPFailure("handshake_unexpected_message")
        # Kiro documents this passive notification after session/new. It
        # neither changes config nor authorizes a tool.
        self.ignored_kiro_notifications += 1
        if self.ignored_kiro_notifications > 8:
            raise ACPFailure("handshake_notification_flood")

    def _handle_update(self, message: dict) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            if self.handshake_only:
                raise ACPFailure("handshake_malformed_update")
            return
        update = params.get("update")
        if not isinstance(update, dict):
            if self.handshake_only:
                raise ACPFailure("handshake_malformed_update")
            return
        kind = update.get("sessionUpdate")
        if self.handshake_only and kind in ("tool_call", "tool_call_update"):
            raise ACPFailure("handshake_unexpected_tool")
        if self.handshake_only and kind != "config_option_update":
            raise ACPFailure("handshake_unexpected_update")
        if params.get("sessionId") != self.session_id:
            if self.handshake_only:
                raise ACPFailure("handshake_foreign_session")
            return
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
            if self.handshake_only:
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

    def request(self, method: str, params: dict, timeout: float | None = 30) -> dict:
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
                elif self.handshake_only:
                    self._handshake_notification(message["method"])
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

    def observe_handshake(self, grace: float = 0.3) -> None:
        """Catch immediate post-ack drift without issuing a prompt or tool call."""
        deadline = time.monotonic() + grace
        while True:
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
            elif method == "_kiro.dev/commands/available" and "id" not in message:
                self._handshake_notification(method)
            else:
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
        *, handshake_only: bool = False) -> None:
    client = ACPClient(cli, stream, handshake_only=handshake_only)
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
        # Kiro 2.24.1 can return session/new before its model selector is
        # populated. The set response is the required source of truth.
        initial = created.get("configOptions")
        initial_model = next((item for item in initial
                              if isinstance(item, dict) and item.get("id") == "model"), None) if isinstance(initial, list) else None
        if initial_model is not None and not _offered(initial_model, model):
            raise ACPFailure("model_unavailable")
        selected = client.request("session/set_config_option", {
            "sessionId": sid, "configId": "model", "value": model})
        if _option(selected.get("configOptions"), "model", "model_set").get("currentValue") != model:
            raise ACPFailure("model_not_selected")
        effort_option = _option(selected.get("configOptions"), "effortLevel", "model_set")
        if not _offered(effort_option, effort):
            raise ACPFailure("effort_unavailable")
        selected = client.request("session/set_config_option", {
            "sessionId": sid, "configId": "effortLevel", "value": effort})
        if (_option(selected.get("configOptions"), "model", "effort_set").get("currentValue") != model
                or _option(selected.get("configOptions"), "effortLevel", "effort_set").get("currentValue") != effort):
            raise ACPFailure("config_not_selected")
        client.required_config = (model, effort)
        client._event("configSelected", {"sessionId": sid, "model": model, "effort": effort})
        if handshake_only:
            client.observe_handshake()
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
    if len(sys.argv) not in (6, 7) or (len(sys.argv) == 7 and sys.argv[6] != "--handshake-only"):
        print("DRADAR_KIRO_ACP=invalid_arguments", file=sys.stderr)
        return 2
    try:
        run(sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5],
            handshake_only=len(sys.argv) == 7)
    except (ACPFailure, OSError) as exc:
        code = str(exc) if isinstance(exc, ACPFailure) else "process_error"
        print("DRADAR_KIRO_ACP=" + code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
