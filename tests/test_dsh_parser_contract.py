"""Generated adapter command -> Bash -> pinned official DSH parsers, no model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("pier")
from pier.agents.installed.base import NonZeroAgentExitCodeError
from dradar.pier_dsh import DSH_VERSION, DshMinimal
from dradar import pier_dsh

FIXTURES = Path(__file__).parent / "fixtures" / "dsh-parser"


@pytest.fixture
def parser() -> tuple[str, str]:
    node, bash = shutil.which("node"), shutil.which("bash")
    if not node or not bash or not (FIXTURES / "node_modules/commander").is_dir():
        pytest.skip("Requires Node, Bash and npm ci --ignore-scripts in tests/fixtures/dsh-parser")
    assert DSH_VERSION == "0.1.2-rc.1", "Refresh the official parser contract fixtures"
    return node, bash


class ShellEnvironment:
    """Execute the unmodified task command with local paths and a parser-only dsh.

    Container maintenance/ownership is simulated. The real shell executes key
    conversion, quoting, redirects, pipefail, tee and credential cleanup. No
    container, root operation, real credential or provider is used.
    """
    default_user = None

    def __init__(self, root: Path, parser: tuple[str, str], exit_code: int = 0):
        self.root, (self.node, self.bash), self.exit_code = root, parser, exit_code
        self.completed = None
        self.command = ""

    def agent_process_env(self, env):
        return env

    async def upload_file(self, source_path, target_path):
        target = Path(target_path)
        assert target.is_relative_to(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)

    async def exec(self, **kwargs):
        command = kwargs["command"]
        if "dsh --profile headless" not in command:
            return SimpleNamespace(return_code=0, stdout="", stderr="")
        self.command = command
        # Map cd /app at the shell boundary without changing any prompt bytes.
        prefix = (
            f"dsh() {{ {shlex.quote(self.node)} {shlex.quote(str(FIXTURES / 'parse.cjs'))} \"$@\"; }}; "
            f"python3() {{ {shlex.quote(sys.executable)} \"$@\"; }}; "
            f"cd() {{ builtin cd {shlex.quote(str(self.root))}; }}; "
        )
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), **kwargs["env"]}
        if self.exit_code:
            env["DRADAR_PARSER_TEST_EXIT"] = str(self.exit_code)
        self.completed = subprocess.run(
            [self.bash, "-c", prefix + command], env=env, cwd=self.root,
            capture_output=True, text=True, timeout=15,
        )
        return SimpleNamespace(return_code=self.completed.returncode,
                               stdout=self.completed.stdout, stderr=self.completed.stderr)


def run_adapter(tmp_path, monkeypatch, parser, task, exit_code=0):
    async def inject_synthetic_files(agent, environment, files):
        for source, target in files:
            await environment.upload_file(source, target)

    async def simulated_ownership_handoff(self, environment, remote_path):
        assert Path(remote_path).is_relative_to(tmp_path)

    monkeypatch.setattr(pier_dsh, "inject_private_files", inject_synthetic_files)
    monkeypatch.setattr(pier_dsh.RuntimeSafety, "return_runtime_tree_to_host_owner", simulated_ownership_handoff)
    for attribute, relative in {
        "_REMOTE_HOME": "runtime", "_REMOTE_CONFIG_DIR": "config",
        "_REMOTE_PATCH": "config/patch.yml", "_REMOTE_RUNNER": "config/runner.mjs",
        "_REMOTE_SECRET_ROOT": "secrets",
    }.items():
        monkeypatch.setattr(DshMinimal, attribute, PurePosixPath(tmp_path / relative))
    (tmp_path / "runtime").mkdir()
    key = tmp_path / "synthetic.key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    agent = DshMinimal(logs_dir=tmp_path / "logs", api_key_file=str(key),
                       artifact_assignment_id="a" * 32, artifact_run_id="b" * 32,
                       artifact_task_id="test-task")
    environment = ShellEnvironment(tmp_path, parser, exit_code)
    error = None
    try:
        asyncio.run(agent.run(task, environment, object()))
    except NonZeroAgentExitCodeError as exc:
        error = exc
    assert environment.completed is not None
    assert not Path(agent._remote_api_key).exists()
    assert not Path(agent._remote_credentials).exists()
    return environment, error


@pytest.mark.parametrize("task", [
    "Run the tests", "-", "- Update the display", "--", "--help", "-h",
    "--profile", "--patch=other.yml", "web", "plugin",
    "line one\nline two\n", "  whitespace preserved  ",
    "single ' and double \" quotes", "中文 🙂\n- 多行",
    "$(touch SHOULD_NOT_EXIST) `touch SHOULD_NOT_EXIST` $HOME ; & | /app",
])
def test_generated_command_preserves_task(tmp_path, monkeypatch, parser, task):
    environment, error = run_adapter(tmp_path, monkeypatch, parser, task)
    assert error is None, str(error)
    parsed = json.loads(environment.completed.stdout)
    assert parsed["task"] == task
    assert parsed["service"] == "headlessStartup"
    assert parsed["invocation"]["profile"] == "headless"
    assert parsed["invocation"]["patches"] == [str(tmp_path / "config/patch.yml")]
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    assert (tmp_path / "runtime/dsh-headless.txt").read_text() == environment.completed.stdout


@pytest.mark.parametrize("task", ["", " \n\t "])
def test_empty_task_still_rejected(tmp_path, monkeypatch, parser, task):
    environment, error = run_adapter(tmp_path, monkeypatch, parser, task)
    assert error is not None
    assert "a task is required" in str(error)
    assert environment.completed.returncode != 0


def test_nonzero_is_preserved_without_sidecars(tmp_path, monkeypatch, parser):
    environment, error = run_adapter(tmp_path, monkeypatch, parser, "- valid task", 7)
    assert environment.completed.returncode == 7
    assert error is not None
    assert "exit 7" in str(error)
    assert "synthetic DSH execution failure" in str(error)
    assert "sidecar" not in str(error)
    assert not (tmp_path / "runtime/dsh-outcome.json").exists()


@pytest.mark.parametrize("args, message", [
    (["--profile"], "argument missing"),
    (["--profile", "", "task"], "--profile needs a name"),
    (["--profile", "headless", "--patch"], "argument missing"),
    (["--profile", "headless", "--patch", "", "task"], "--patch needs a path"),
    (["--profile", "headless", "--bad-option"], "unknown option"),
])
def test_real_options_still_validated(parser, args, message):
    node, _ = parser
    result = subprocess.run([node, str(FIXTURES / "parse.cjs"), *args],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert message in result.stderr
