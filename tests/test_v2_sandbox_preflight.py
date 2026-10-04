import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dradar.v2.sandbox_preflight import COMMAND, SandboxPreflightError, require_codex_sandbox


def test_auth_free_success():
    env = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=0, stdout='DRADAR_SANDBOX_READY\n', stderr='')))
    receipt = asyncio.run(require_codex_sandbox(env))
    assert receipt.model_calls == 0
    assert receipt.existing_auth_read is False
    assert receipt.security_policy_changed is False
    env.exec.assert_awaited_once_with(command=COMMAND, timeout_sec=30)


@pytest.mark.parametrize('rc,stdout,stderr,code', [
    (1, '', 'bwrap: No permissions to create a new namespace', 'sandbox_namespace_denied'),
    (1, 'DRADAR_SANDBOX_READY', '', 'sandbox_probe_failed'),
    (0, '', '', 'sandbox_canary_missing'),
    (None, 'DRADAR_SANDBOX_READY', '', 'sandbox_probe_failed'),
])
def test_fail_closed_no_retry(rc, stdout, stderr, code):
    env = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(return_code=rc, stdout=stdout, stderr=stderr)))
    with pytest.raises(SandboxPreflightError) as error:
        asyncio.run(require_codex_sandbox(env))
    assert error.value.code == code
    assert env.exec.await_count == 1


def test_error_is_sanitized():
    env = SimpleNamespace(exec=AsyncMock(side_effect=RuntimeError('private environment details')))
    with pytest.raises(SandboxPreflightError) as error:
        asyncio.run(require_codex_sandbox(env))
    assert error.value.code == 'sandbox_probe_unavailable'
    assert 'private environment details' not in str(error.value)
