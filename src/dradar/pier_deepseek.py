"""Public Pier adapter that installs DeepSeek's official Codex model catalog.

The stock datacurve-pier 0.3.0 Codex agent can inject ``config.toml`` text and
an auth file, but it has no generic extra-file upload hook.  Codex runs with an
isolated ``/tmp/codex-home`` inside the task container, so a catalog path on
the host is otherwise unusable.  This narrow subclass uploads exactly one
integrity-pinned public metadata file before delegating normal execution and
trajectory collection to Pier.
"""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.network import allowlist_from_urls
from pier.agents.installed.codex import Codex
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.network import NetworkAllowlist

_CATALOG_SHA256 = (
    "b459a6e438d6a9939d01fd0dbb4693f165ed732bc8e4fd58d7145d9d94bd49a4"
)

_OFFICIAL_DEEPSEEK_BASE_URL = "https://api.deepseek.com/"


class DeepSeekCodex(Codex):
    """Stock Codex plus a fail-closed, container-local model catalog."""

    _REMOTE_MODEL_CATALOG = PurePosixPath("/tmp/codex-home/models.json")

    def network_allowlist(self) -> NetworkAllowlist:
        """Allow only the paid provider endpoint during agent execution.

        Stock Pier's Codex allowlist always includes api.openai.com as a
        default. DeepSeek does not need that endpoint, and apps, remote plugin,
        and web search are intentionally disabled for benchmark isolation.
        The allowed egress is restricted to the validated official endpoint.
        """

        return allowlist_from_urls(
            [self._provider_base_url],
            default_domains=[],
        )

    def __init__(
        self,
        *args: Any,
        model_catalog_json_file: str,
        provider_base_url: str,
        **kwargs: Any,
    ):
        if provider_base_url != _OFFICIAL_DEEPSEEK_BASE_URL:
            raise ValueError(
                f"unsupported DeepSeek provider URL: {provider_base_url!r}"
            )
        self._provider_base_url = provider_base_url
        catalog = Path(model_catalog_json_file)
        try:
            digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(
                f"DeepSeek model catalog is unreadable: {catalog}"
            ) from exc
        if digest != _CATALOG_SHA256:
            raise ValueError(
                "DeepSeek model catalog integrity check failed; reinstall or "
                "upgrade dradar before running a paid task"
            )
        self._model_catalog_json_file = catalog
        super().__init__(*args, **kwargs)

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        remote_home = self._REMOTE_CODEX_HOME.as_posix()
        env = self.build_process_env({"CODEX_HOME": remote_home})
        await self.exec_as_agent(
            environment,
            command=f"mkdir -p {shlex.quote(remote_home)}",
            env=env,
        )
        await environment.upload_file(
            self._model_catalog_json_file,
            self._REMOTE_MODEL_CATALOG.as_posix(),
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} "
                    f"{shlex.quote(self._REMOTE_MODEL_CATALOG.as_posix())}"
                ),
                env=env,
            )
        # Re-check inside the real task container, as the same user that will
        # launch Codex. This catches a failed/truncated upload or unreadable
        # ownership before any paid model request can be made.
        await self.exec_as_agent(
            environment,
            command=(
                f"sha256sum {shlex.quote(self._REMOTE_MODEL_CATALOG.as_posix())} "
                f"| grep -Fq {shlex.quote(_CATALOG_SHA256)}"
            ),
            env=env,
        )
        self.logger.info(
            "DeepSeek Codex model catalog verified in task container: %s",
            _CATALOG_SHA256,
        )
        await super().run(instruction, environment, context)


__all__ = ["DeepSeekCodex"]
