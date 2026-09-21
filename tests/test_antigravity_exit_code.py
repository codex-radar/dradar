"""A nonzero AGY exit must carry its numeric code in the exception message.

The runner (``_agent_command_exit_code``) and the server's runner_failure
parser both read ``Command failed (exit N):``.  Without it every AGY failure
was stored as an unknown ``agent_exception`` (#0227).
"""
import asyncio
import hashlib
import hmac
import json
import types

import pytest

pier = pytest.importorskip("pier.agents.installed.base")

from dradar import runner
from dradar.pier_antigravity import Antigravity
from private_artifact_fixture import private_trial

RUN_ID = "a" * 32


class _Env:
    def __init__(self, return_code):
        self.return_code = return_code

    def agent_process_env(self, env):
        return env

    async def exec(self, command, env=None, **_):
        key = b"k" * 32
        payload = json.dumps({"run_id": RUN_ID, "writer_stopped": True, "exported": True})
        frames = [
            json.dumps({"run_id": RUN_ID, "key": key.hex()}),
            json.dumps({"payload": payload,
                        "mac": hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()}),
        ]
        return types.SimpleNamespace(stdout="\n".join(frames), return_code=self.return_code)


def _agent(tmp_path):
    trial = tmp_path / "trial"
    private_trial(trial)
    (trial / "agent").mkdir()
    agent = Antigravity.__new__(Antigravity)
    agent._artifact_run_id = RUN_ID
    agent._artifact_base_commit = "pompeii-base"
    agent.logs_dir = trial / "agent"

    async def exec_as_agent(*_, **__):
        return None
    agent.exec_as_agent = exec_as_agent
    return agent


def _run(tmp_path, return_code):
    agent = _agent(tmp_path)
    return asyncio.run(agent._run_supervised(_Env(return_code), ["agy"], {}, "/o", "/e"))


@pytest.mark.parametrize("code", [7, 1, 255])
def test_nonzero_supervised_exit_names_its_code(tmp_path, code):
    with pytest.raises(pier.NonZeroAgentExitCodeError) as raised:
        _run(tmp_path, code)
    message = str(raised.value)
    assert message.startswith(f"Command failed (exit {code}): ")
    assert runner._agent_command_exit_code("NonZeroAgentExitCodeError", message) == code


def test_zero_supervised_exit_does_not_raise(tmp_path):
    assert _run(tmp_path, 0) is None
