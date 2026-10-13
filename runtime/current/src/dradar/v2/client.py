"""v0 HTTP transport. All writes originate from durable exact requests."""
from __future__ import annotations
import re
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
from urllib.parse import urlsplit
import httpx
from .journal import Journal, Request

class TransportUnknown(RuntimeError):
    """The server may have accepted the request; preserve it for replay."""
    def __init__(self, message, retry_after_seconds=5):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds

class ProtocolError(RuntimeError):
    pass

class RemoteError(RuntimeError):
    def __init__(self, status: int):
        message = {
            401: "DRadar 站点登录已失效（HTTP 401）；请本人使用既有 dradar login --github 恢复原雷达身份，保留原 STATE 与成果。这不是 ChatGPT 登录错误。",
            403: "DRadar 站点拒绝此操作（HTTP 403）；请核对账号权限与原运行所有权，保留原 STATE 与成果，不自动换账号。",
        }.get(status, f"v2 server rejected request (HTTP {status})")
        super().__init__(message)
        self.status = status

def server_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ("", "/") or not parts.hostname
            or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("HTTPS server or HTTP loopback required")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("invalid server port") from exc
    return value.rstrip("/")

def _path(path: str) -> str:
    if not re.fullmatch(r"/api/v2/single/(?:bootstrap|runs(?:/[A-Za-z0-9_-]+(?:/(?:claim|stop))?)?|assignments/[A-Za-z0-9_-]+(?:/(?:start|heartbeat|result|release))?)", path):
        raise ProtocolError("unknown v2 API path")
    return path

class Client:
    def __init__(self, server: str, token: str, journal: Journal, *, transport=None):
        self.server = server_url(server)
        if not isinstance(token, str) or not token or token.startswith("drp_") or any(c in token for c in "\r\n"):
            raise ValueError("existing account credential required")
        journal.bind("server", self.server)
        self.journal = journal
        self._bootstrap = None
        self.http = httpx.Client(base_url=self.server, transport=transport,
                                 headers={"Authorization": f"Bearer {token}", "X-DRadar-Protocol": "single-task-v1",
                                          "X-DRadar-Capabilities": "single-task-v1,codex-gpt6-1-sol-v1"},
                                 timeout=30, follow_redirects=False, trust_env=False)


    def close(self) -> None:
        self.http.close()


    def _response(self, response: httpx.Response) -> dict:
        if response.status_code == 429 or response.status_code >= 500:
            delay = 5.0
            header = response.headers.get("Retry-After")
            if header:
                try:
                    if header.isdigit():
                        delay = max(1.0, float(header))
                    else:
                        delay = max(1.0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
            # Jitter only adds time, so Retry-After is always a lower bound.
            delay += random.uniform(0, min(delay / 10, 1.0))
            raise TransportUnknown("server busy or write outcome unknown; preserve original request", delay)
        if not 200 <= response.status_code < 300:
            raise RemoteError(response.status_code)
        try:
            value = response.json()
        except ValueError as exc:
            raise ProtocolError("v2 response is not JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolError("v2 response object required")
        return value

    def get(self, path: str) -> dict:
        try:
            response = self.http.get(_path(path))
        except httpx.TransportError as exc:
            raise TransportUnknown("v2 state unavailable") from exc
        return self._response(response)

    def send(self, request: Request) -> dict:
        """The same local request always sends identical JSON bytes.

        Responses are transport receipts only. Scheduler validates the v0
        start authorization before setting a local launch fence.
        """
        # Opening old state remains permitted for stop/release/upload. A saved
        # journal must never become a route to a fresh retired claim or start.
        from ..harness_policy import reject_retired_combination
        if request.operation == 'run:create' or request.operation.startswith(('claim:', 'start:')):
            config = json.loads(self.journal.value('configuration') or '{}')
            if request.operation == 'run:create':
                config = request.body
            reject_retired_combination(config.get('agent', 'codex'), config.get('model'))
        if self._bootstrap is None:
            self.bootstrap()
        # Verify the caller cannot bypass the durable request store.
        prepared = self.journal.prepare(request.operation, request.path,
                    {k: v for k, v in request.body.items() if k != "request_id"}, method=request.method)
        if prepared != request:
            # A previous ACK may have arrived since this caller read its copy.
            if (prepared.request_id, prepared.body_json, prepared.path, prepared.method) != (request.request_id, request.body_json, request.path, request.method):
                raise ProtocolError("request is not the durable original")
        if prepared.response is not None:
            return prepared.response
        try:
            response = self.http.request(request.method, _path(request.path),
                content=request.body_json.encode(), headers={"Content-Type": "application/json"})
        except httpx.TransportError as exc:
            raise TransportUnknown("write ACK unknown; reconcile original request") from exc
        from .protocol import envelope
        value = envelope(self._response(response), request.request_id)
        self.journal.acknowledge(request, value)
        return value

    def mutate(self, operation: str, path: str, body: dict) -> dict:
        return self.send(self.journal.prepare(operation, _path(path), body))

    def send_result(self, request: Request, files: dict[str, Path]) -> dict:
        from .protocol import result_receipt
        if not request.path.endswith('/result') or not request.operation.startswith('result:'):
            raise ProtocolError('result route and request namespace mismatch')
        payload = request.body
        validate_receipt = result_receipt
        if self._bootstrap is None:
            self.bootstrap()
        prepared = self.journal.prepare(request.operation, request.path,
                    {k: v for k, v in request.body.items() if k != "request_id"})
        if prepared.body_json != request.body_json or prepared.request_id != request.request_id:
            raise ProtocolError("result request is not the durable original")
        if prepared.response is not None:
            return validate_receipt(prepared.response, request.request_id, request.path.split('/')[-2], request.body)
        artifacts = payload["artifacts"]
        if set(files) != {f["name"] for f in artifacts}:
            raise ProtocolError("multipart file set mismatch")
        with ExitStack() as stack:
            parts = {}
            for artifact in artifacts:
                handle = stack.enter_context(files[artifact["name"]].open("rb"))
                if hashlib.file_digest(handle, "sha256").hexdigest() != artifact["sha256"] or handle.tell() != artifact["size_bytes"]:
                    raise ProtocolError("multipart bytes changed")
                handle.seek(0)
                parts[artifact["name"]] = (artifact["name"], handle, artifact["content_type"])
            # Metadata as one text part, even when a failed result has no files.
            parts["metadata"] = (None, request.body_json, "application/json")
            try:
                response = self.http.post(_path(request.path), files=parts)
            except httpx.TransportError as exc:
                raise TransportUnknown("result ACK unknown; preserve exact upload") from exc
            value = validate_receipt(self._response(response), request.request_id, request.path.split("/")[-2], request.body)
            self.journal.acknowledge(request, value)
            return value
