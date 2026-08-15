from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pier")

from dradar.pier_dsh import (
    DSH_VERSION,
    NODE_SHA256,
    NODE_VERSION,
    RUNTIME_MODELS,
    SUPPORTED_MODELS,
    SUPPORTED_REASONING_EFFORTS,
    DshMinimal,
)
from dradar.providers import (
    DSH_MODELS as MAIN_FLOW_MODELS,
    DSH_SUPPORTED_EFFORTS as MAIN_FLOW_EFFORTS,
    DSH_VERSION as MAIN_FLOW_VERSION,
)


class FakeEnvironment:
    default_user = "agent"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.uploads: list[tuple[Path, str, bytes]] = []

    def agent_process_env(self, env: dict[str, str] | None) -> dict[str, str] | None:
        return env

    async def exec(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        self.uploads.append((source, target_path, source.read_bytes()))


def test_standalone_adapter_matches_main_flow_contract() -> None:
    assert DSH_VERSION == MAIN_FLOW_VERSION
    assert SUPPORTED_MODELS == frozenset(MAIN_FLOW_MODELS)
    assert SUPPORTED_REASONING_EFFORTS == MAIN_FLOW_EFFORTS


def make_key(tmp_path: Path, value: str = "test-secret-never-log") -> Path:
    key = tmp_path / "deepseek.key"
    key.write_text(value, encoding="utf-8")
    if os.name != "nt":
        key.chmod(0o600)
    return key


def make_agent(tmp_path: Path, **kwargs: object) -> DshMinimal:
    return DshMinimal(
        logs_dir=tmp_path / "logs",
        api_key_file=str(make_key(tmp_path)),
        **kwargs,
    )


def artifact_binding() -> dict[str, str]:
    return {
        "artifact_assignment_id": "a" * 32,
        "artifact_run_id": "b" * 32,
        "artifact_task_id": "httpx-streaming-json-iteration",
    }


def test_install_spec_is_fully_pinned(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    spec = agent.install_spec()
    command = " ".join(step.run for step in spec.steps)

    assert spec.agent_name == "dsh-minimal"
    assert spec.version == DSH_VERSION
    assert f"@deepseek-ai/dsh@{DSH_VERSION}" in command
    assert f"node-v{NODE_VERSION}-linux-${{node_arch}}.tar.xz" in command
    assert NODE_SHA256["x64"] in command
    assert NODE_SHA256["arm64"] in command
    assert "@latest" not in command
    assert "requires a glibc task image" in command
    assert "g++ inotify-tools make python3" in command
    assert spec.verification_command is not None
    assert f"v{NODE_VERSION}" in spec.verification_command
    assert DSH_VERSION in spec.verification_command
    assert spec.cache_key == (
        f"dradar-dsh-minimal-{DSH_VERSION}-node-{NODE_VERSION}-patch-v2"
    )
    bash = shutil.which("bash")
    if bash is not None:
        syntax = subprocess.run(
            [bash, "-n", "-c", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert syntax.returncode == 0, syntax.stderr


def test_only_deepseek_api_is_allowed_at_runtime(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    assert agent.network_allowlist().domains == ["api.deepseek.com"]


@pytest.mark.parametrize("effort", ["low", "medium", "invalid"])
def test_rejects_unsupported_reasoning_effort(tmp_path: Path, effort: str) -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        make_agent(tmp_path, reasoning_effort=effort)


def test_normalizes_supported_model_prefix(tmp_path: Path) -> None:
    agent = make_agent(
        tmp_path,
        model_name="deepseek-official/dsh-deepseek-v4-pro",
        version=None,
    )
    assert agent.model_name == "deepseek-v4-pro"
    assert agent.version() == DSH_VERSION


def test_rejects_unsupported_model_or_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DSH model"):
        make_agent(tmp_path, model_name="deepseek-chat")
    with pytest.raises(ValueError, match="exact version"):
        make_agent(tmp_path, version="0.1.0-rc.5")


@pytest.mark.parametrize(
    "extra_env",
    [
        {"DEEPSEEK_API_KEY": "wrong-channel"},
        {"DSH_MODEL": "unexpected"},
        {"DEEPSEEK_BASE_URL": "https://unexpected.invalid"},
        {"NODE_OPTIONS": "--require=/tmp/untrusted.js"},
        {"NODE_USE_ENV_PROXY": "0"},
    ],
)
def test_rejects_reserved_extra_env(tmp_path: Path, extra_env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="api_key_file"):
        make_agent(tmp_path, extra_env=extra_env)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_rejects_group_readable_key_file(tmp_path: Path) -> None:
    key = make_key(tmp_path)
    key.chmod(0o640)
    with pytest.raises(ValueError, match="permissions"):
        DshMinimal(logs_dir=tmp_path / "logs", api_key_file=str(key))


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        (model, effort)
        for model in ("dsh-deepseek-v4-flash", "dsh-deepseek-v4-pro")
        for effort in ("off", "high", "max")
    ],
)
def test_run_supports_model_effort_matrix_without_logging_secret(
    tmp_path: Path,
    model: str,
    effort: str,
) -> None:
    secret = "test-secret-never-log"
    agent = make_agent(
        tmp_path,
        model_name=model,
        reasoning_effort=effort,
        **artifact_binding(),
    )
    environment = FakeEnvironment()

    asyncio.run(
        agent.run(
            "Fix the quoted 'edge' and do not expand $HOME",
            environment,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
    )

    assert len(environment.uploads) == 3
    uploaded = {target: data for _, target, data in environment.uploads}
    patch = uploaded["/tmp/dsh-config/headless-minimal.patch.yml"].decode()
    runner = uploaded["/tmp/dsh-config/minimal-headless-runner.mjs"].decode()
    key_target = next(
        target for target in uploaded if target.endswith("/deepseek-api-key")
    )
    assert secret.encode() == uploaded[key_target]
    assert "id: agent-presets" in patch
    assert "default: minimal" in patch
    assert "includeUserRoot: false" in patch
    assert "config/agent-presets" in patch
    assert "id: tool-web\n  disabled: true" in patch
    assert "id: tool-subagent\n  disabled: true" in patch
    assert "id: system-prompt\n  disabled: true" not in patch
    assert "id: headless-runner\n  disabled: true" in patch
    assert "id: minimal-headless-runner" in patch
    assert "name: /tmp/dsh-config/minimal-headless-runner.mjs" in patch
    assert "path: !!js process.env.DSH_CREDENTIALS_FILE" in patch
    assert "watch: false" in patch
    assert 'presets.resolve("minimal")' in runner
    assert "await presets.mount(agentCtx, preset.id)" in runner
    assert "agentPreset: preset.id" in runner
    assert 'event.type === "assistant/chunk"' in runner
    assert 'usageByStep.set(`${event.data.turn}:${event.data.step}`' in runner
    assert 'schema: "dsh-provider-usage-v1"' in runner
    assert "writeFileSync(process.env.DSH_USAGE_FILE" in runner
    assert 'schema: "dradar-dsh-outcome-v1"' in runner
    assert "assignmentId: process.env.DRADAR_ASSIGNMENT_ID" in runner
    assert "artifactRunId: process.env.DRADAR_ARTIFACT_RUN_ID" in runner
    assert "writeFileSync(process.env.DSH_OUTCOME_FILE" in runner

    serialized_calls = repr(environment.calls)
    assert secret not in serialized_calls
    setup_call = next(
        call
        for call in environment.calls
        if "mkdir -p /logs/agent/dsh-home" in str(call["command"])
    )
    assert setup_call["user"] == "root"
    assert "chown agent" in str(setup_call["command"])
    dsh_call = next(
        call
        for call in environment.calls
        if "dsh --profile headless" in str(call["command"])
    )
    command = str(dsh_call["command"])
    assert "set -euo pipefail; " in command
    assert command.index(f"rm -f {key_target}") < command.index(
        "dsh --profile headless"
    )
    assert "inotifywait --quiet --event open" in command
    assert "DEEPSEEK_API_KEY" not in (dsh_call.get("env") or {})
    assert dsh_call["cwd"] == "/app"
    assert (dsh_call.get("env") or {})["DSH_MODEL"] == RUNTIME_MODELS[model]
    assert (dsh_call.get("env") or {})["DSH_REASONING_EFFORT"] == effort
    assert (dsh_call.get("env") or {})["DSH_USAGE_FILE"] == (
        "/logs/agent/dsh-home/dsh-usage.json"
    )
    assert (dsh_call.get("env") or {})["DSH_OUTCOME_FILE"] == (
        "/logs/agent/dsh-home/dsh-outcome.json"
    )
    assert (dsh_call.get("env") or {})["DRADAR_ASSIGNMENT_ID"] == "a" * 32
    assert (dsh_call.get("env") or {})["DRADAR_ARTIFACT_RUN_ID"] == "b" * 32
    assert (dsh_call.get("env") or {})["DRADAR_TASK_ID"] == (
        "httpx-streaming-json-iteration"
    )
    assert (dsh_call.get("env") or {})["NODE_USE_ENV_PROXY"] == "1"
    assert agent.SUPPORTS_ATIF is False
