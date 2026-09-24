"""Bounded Codex app-server account RPC, independent of models and quotas.

A protocol adapter, not an activation policy: callers must establish the
version's native writer/credential-store contract before asking it to refresh.
Only fixture processes have been validated so far. Windows pipe handling is
not activated by this POSIX implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import subprocess
import time


def _network_environment() -> dict[str, str]:
    """Preserve transport settings, never alternate provider credentials."""
    allowed = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
               'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    return {name: value for name, value in os.environ.items() if name in allowed}


class AccountRpcError(RuntimeError):
    pass


class CodexAccountRpc:
    def __init__(self, executable: Path, expected_sha256: str):
        self._executable = executable
        self._digest = expected_sha256

    def account_read(self, auth_home: Path, *, refresh: bool, timeout: float = 30) -> str:
        """Return only 'chatgpt', 'apiKey', 'other', or 'missing'.

        No raw account object, email, response or child output leaves this API.
        There is exactly one account/read, with no retry after timeout/failure.
        The caller's durable pending intent must precede a read of managed auth:
        refresh=False can still perform native proactive renewal when expired.
        """
        account = self._read_account(auth_home, refresh=refresh, timeout=timeout)
        if account is None:
            return 'missing'
        mode = account.get('type')
        return mode if mode in ('chatgpt', 'apiKey') else 'other'

    def subscription_status(self, auth_home: Path, *, expected_email: str,
                            timeout: float = 15) -> str:
        """Classify a native account read without exposing its identity or plan."""
        if not isinstance(expected_email, str) or not expected_email:
            raise AccountRpcError('invalid_rpc_request')
        account = self._read_account(auth_home, refresh=False, timeout=timeout)
        if account is None or account.get('type') != 'chatgpt':
            return 'not_chatgpt'
        email = account.get('email')
        if not isinstance(email, str) or email.casefold() != expected_email.casefold():
            return 'identity_mismatch'
        # Only plans with a documented Codex GPT-6 subscription rollout.
        if account.get('planType') not in {
            'plus', 'pro', 'team', 'business', 'enterprise', 'edu',
            'edu_plus', 'edu_pro', 'ent26',
            'enterprise_cbp_automation', 'enterprise_cbp_usage_based',
        }:
            return 'plan_ineligible'
        return 'eligible'

    def _read_account(self, auth_home: Path, *, refresh: bool,
                      timeout: float) -> dict | None:
        if os.name == 'nt':
            raise AccountRpcError('runtime_transport_unverified')
        if type(refresh) is not bool or type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise AccountRpcError('invalid_rpc_request')
        try:
            executable = self._executable
            if not executable.is_absolute() or executable.is_symlink():
                raise ValueError()
            with executable.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != self._digest:
                raise ValueError()
        except (OSError, ValueError):
            raise AccountRpcError('runtime_pin_mismatch') from None
        deadline = time.monotonic() + timeout
        # The official npm launcher is a /usr/bin/env node script on several
        # hosts. Keep only that interpreter directory, not the caller's PATH.
        node = shutil.which('node')
        runtime_path = os.defpath
        if node:
            runtime_path = str(Path(node).resolve().parent) + os.pathsep + runtime_path
        try:
            proc = subprocess.Popen([str(executable), 'app-server'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**_network_environment(), 'PATH': runtime_path, 'HOME': str(auth_home), 'CODEX_HOME': str(auth_home)},
                cwd=auth_home, start_new_session=True, bufsize=0)
        except OSError:
            raise AccountRpcError('runtime_unavailable') from None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(proc.stderr, selectors.EVENT_READ, 'stderr')
                buffer = bytearray()
                received = 0
                def send(value):
                    proc.stdin.write(json.dumps(value, separators=(',', ':')).encode() + b'\n')
                    proc.stdin.flush()
                def response(request_id):
                    nonlocal received
                    while True:
                        while b'\n' in buffer:
                            line, _, rest = buffer.partition(b'\n')
                            buffer[:] = rest
                            try:
                                item = json.loads(line)
                            except (ValueError, UnicodeError):
                                raise AccountRpcError('invalid_rpc_response') from None
                            if not isinstance(item, dict):
                                raise AccountRpcError('invalid_rpc_response')
                            if item.get('id') == request_id:
                                if 'error' in item or not isinstance(item.get('result'), dict):
                                    raise AccountRpcError('account_rpc_failed')
                                return item['result']
                            # Ignore notifications; unexpected response/request IDs
                            # indicate a protocol mismatch, not a reason to retry.
                            if 'id' in item:
                                raise AccountRpcError('unexpected_rpc_response')
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise AccountRpcError('account_rpc_timeout')
                        events = selector.select(remaining)
                        if not events:
                            raise AccountRpcError('account_rpc_timeout')
                        for key, _ in events:
                            chunk = os.read(key.fileobj.fileno(), 8192)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                if key.data == 'stdout':
                                    raise AccountRpcError('account_rpc_closed')
                                continue
                            received += len(chunk)
                            if received > 256 * 1024:
                                raise AccountRpcError('account_rpc_output_limit')
                            if key.data == 'stdout':
                                buffer.extend(chunk)
                send({'id': 1, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'dradar', 'version': '1'}}})
                response(1)
                send({'method': 'initialized', 'params': {}})
                send({'id': 2, 'method': 'account/read', 'params': {'refreshToken': refresh}})
                result = response(2)
                if 'account' not in result:
                    raise AccountRpcError('invalid_rpc_response')
                account = result['account']
                if account is None:
                    return None
                if not isinstance(account, dict):
                    raise AccountRpcError('invalid_rpc_response')
                return account
        except (OSError, ValueError):
            raise AccountRpcError('account_rpc_transport_failed') from None
        finally:
            # Kill only this dedicated process group, including helper children.
            # No fallback to an unrelated global CLI process or automatic retry.
            # Signal the dedicated group while the leader PID is still owned.
            # After wait() reaps it, never signal that numeric group again.
            cleanup_failed = False
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                cleanup_failed = True
            try:
                proc.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    cleanup_failed = True
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    cleanup_failed = True
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    stream.close()
            if cleanup_failed:
                raise AccountRpcError('runtime_cleanup_unverified') from None
