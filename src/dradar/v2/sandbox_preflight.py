"""Authentication-free gate for the Codex workspace-write execution path.

Run in the exact prepared environment, before starting a model turn. This gate
does not authorize changing security policy or falling back to another executor.
"""
from dataclasses import dataclass


CANARY = 'DRADAR_SANDBOX_READY'
COMMAND = r'''set -eu
probe_home=$(mktemp -d "$HOME/.dradar-sandbox-XXXXXX")
trap 'rm -rf "$probe_home"' EXIT
export CODEX_HOME="$probe_home" HOME="$probe_home"
codex sandbox -c 'sandbox_mode="workspace-write"' -- sh -c 'printf "DRADAR_SANDBOX_READY\n"'
'''


@dataclass(frozen=True)
class SandboxPreflightReceipt:
    sandbox_mode: str = 'workspace-write'
    model_calls: int = 0
    existing_auth_read: bool = False
    security_policy_changed: bool = False


class SandboxPreflightError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Codex sandbox preflight blocked: {code}; do not start a model or retry with weaker permissions')


async def require_codex_sandbox(environment):
    try:
        result = await environment.exec(command=COMMAND, timeout_sec=30)
    except Exception as exc:
        # Never expose environment errors, which may contain task/user details.
        raise SandboxPreflightError('sandbox_probe_unavailable') from None
    stdout = getattr(result, 'stdout', '') or ''
    stderr = getattr(result, 'stderr', '') or ''
    if getattr(result, 'return_code', None) != 0:
        code = 'sandbox_namespace_denied' if 'bwrap: No permissions to create a new namespace' in stderr else 'sandbox_probe_failed'
        raise SandboxPreflightError(code)
    if stdout.strip() != CANARY:
        raise SandboxPreflightError('sandbox_canary_missing')
    return SandboxPreflightReceipt()
