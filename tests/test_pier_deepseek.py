from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

pytest.importorskip("pier")

from pier.agents.installed.codex import Codex

from dradar import pier_deepseek
from dradar.pier_deepseek import DeepSeekCodex
from dradar.providers import deepseek_catalog_path


def _agent(tmp_path: Path) -> DeepSeekCodex:
    logs_dir = tmp_path / "trial" / "agent"
    logs_dir.mkdir(parents=True, mode=0o700)
    return DeepSeekCodex(
        logs_dir=logs_dir,
        model_name="deepseek-flash",
        version="0.149.0",
        model_catalog_json_file=str(deepseek_catalog_path()),
        extra_env={},
    )


def test_constructor_is_stock_codex_with_deepseek_only_network(tmp_path: Path):
    agent = _agent(tmp_path)

    assert isinstance(agent, Codex)
    assert agent.network_allowlist().domains == ["api.deepseek.com"]
    source = inspect.getsource(pier_deepseek)
    assert "pier_checkpoint" not in source
    assert "DurableCheckpoint" not in source
    assert "checkpoint_enabled" not in source


def test_catalog_integrity_is_fail_closed(tmp_path: Path):
    catalog = tmp_path / "models.json"
    catalog.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="integrity check failed"):
        DeepSeekCodex(
            logs_dir=tmp_path / "agent",
            model_name="deepseek-flash",
            version="0.149.0",
            model_catalog_json_file=str(catalog),
            extra_env={},
        )


@pytest.mark.parametrize("default_user", ("benchmark-agent", None))
def test_catalog_is_verified_before_unchanged_stock_codex_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default_user: str | None,
) -> None:
    class Environment:
        def __init__(self) -> None:
            self.default_user = default_user
            self.uploads: list[tuple[Path, str]] = []

        async def upload_file(self, source: Path, target: str) -> None:
            self.uploads.append((Path(source), target))

    async def scenario() -> None:
        agent = _agent(tmp_path)
        environment = Environment()
        agent_commands: list[str] = []
        root_commands: list[str] = []
        delegated: list[str] = []

        async def exec_as_agent(_environment, command, **_kwargs):
            agent_commands.append(command)

        async def exec_as_root(_environment, command, **_kwargs):
            root_commands.append(command)

        async def stock_run(self, instruction, _environment, _context):
            assert self is agent
            delegated.append(instruction)

        monkeypatch.setattr(agent, "exec_as_agent", exec_as_agent)
        monkeypatch.setattr(agent, "exec_as_root", exec_as_root)
        monkeypatch.setattr(Codex, "run", stock_run)
        instruction = "BENCHMARK-PROMPT::keep this byte-for-byte unchanged"

        await agent.run(instruction, environment, object())

        assert delegated == [instruction]
        assert environment.uploads == [
            (deepseek_catalog_path(), "/tmp/codex-home/models.json")
        ]
        assert agent_commands[0] == "mkdir -p /tmp/codex-home"
        assert "sha256sum /tmp/codex-home/models.json" in agent_commands[1]
        expected_root = (
            ["chown benchmark-agent /tmp/codex-home/models.json"]
            if default_user else []
        )
        assert root_commands == expected_root

    asyncio.run(scenario())
