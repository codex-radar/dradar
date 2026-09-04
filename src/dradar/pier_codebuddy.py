"""Credential-isolated Pier adapter for CodeBuddy Code and HY4 Preview."""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.installed.base import with_prompt_template
from pier.agents.installed.claude_code import ClaudeCode
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
try:
    from _dradar_codebuddy_runtime import (
        CODEBUDDY_CLI_VERSION,
        codebuddy_install_command,
    )
except ModuleNotFoundError:
    from dradar.codebuddy_runtime import (
        CODEBUDDY_CLI_VERSION,
        codebuddy_install_command,
    )
try:
    from _dradar_worker_events import emit_worker_registered
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered

SUPPORTED_MODEL = "hy4-preview"
SUPPORTED_EFFORTS = {"low", "high", "max"}


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_values(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    names = (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens",
    )
    parsed = {name: _nonnegative_int(value.get(name)) for name in names}
    if any(item is None for item in parsed.values()):
        return None
    return {name: int(item) for name, item in parsed.items() if item is not None}


def _codebuddy_usage_facts(events: list[dict]) -> dict[str, object]:
    """Reconcile CodeBuddy's per-response ledger with its terminal aggregate.

    CodeBuddy emits zero-usage stream fragments before the token-bearing
    response for a message, and ``num_turns`` counts stream turns rather than
    provider requests.  Its ``input_tokens`` already includes cache reads and
    cache creation; those cache fields are subsets, not additional prompt
    tokens.  DRadar therefore deduplicates by message id, reconciles the
    positive request ledger with the terminal aggregate, and bills
    ``input_tokens`` directly while retaining cache reads as the discounted
    subset.  Missing or inconsistent evidence stays explicitly incomplete.
    """

    terminal_events = [
        event for event in events
        if isinstance(event, dict)
        and event.get("type") == "result"
    ]
    terminal = terminal_events[0] if len(terminal_events) == 1 else None
    terminal_usage = _usage_values(
        terminal.get("usage") if terminal is not None else None
    )
    reasons: set[str] = set()
    if not terminal_events:
        reasons.add("terminal_aggregate_missing")
    elif len(terminal_events) > 1:
        reasons.add("terminal_aggregate_multiple")
    elif terminal_usage is None:
        reasons.add("terminal_usage_missing_or_invalid")
    names = (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens",
    )
    totals = {name: 0 for name in names}
    usage_by_message_id: dict[str, dict[str, int]] = {}
    conflicted_message_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            reasons.add("request_message_invalid")
            continue
        message_id = message.get("id")
        runtime_model = message.get("model")
        if (
            isinstance(runtime_model, str)
            and runtime_model
            and runtime_model != SUPPORTED_MODEL
        ):
            # A mismatched model is never admitted to the observed ledger.
            # Correctly identified requests in the same stream remain useful
            # evidence, but the run cannot reconcile or settle.
            reasons.add("request_model_mismatch")
            continue
        raw_usage = message.get("usage")
        usage = _usage_values(raw_usage)
        if usage is None:
            # Thinking/text stream fragments have an all-zero placeholder in
            # current CodeBuddy releases, with nullable cache counters.  They
            # are not provider responses.  A malformed record that already
            # carries a positive counter is real evidence loss and must fail
            # closed instead of being silently skipped.
            if isinstance(raw_usage, dict) and any(
                (_nonnegative_int(raw_usage.get(name)) or 0) > 0
                for name in names
            ):
                reasons.add("request_usage_invalid")
            continue
        if sum(usage.values()) == 0:
            continue
        if not isinstance(message_id, str) or not message_id:
            reasons.add("request_id_missing")
            continue
        if message_id in conflicted_message_ids:
            continue
        previous = usage_by_message_id.get(message_id)
        if previous is not None:
            if previous != usage:
                # Neither side of a conflicting identity is authoritative.
                # Remove only that request and preserve unrelated records.
                reasons.add("request_id_conflict")
                conflicted_message_ids.add(message_id)
                del usage_by_message_id[message_id]
            continue
        usage_by_message_id[message_id] = usage

    token_usage_events: list[dict[str, int]] = []
    for usage in usage_by_message_id.values():
        for name in names:
            totals[name] += usage[name]
        token_usage_events.append({
            "n_input_tokens": usage["input_tokens"],
            "n_cache_tokens": usage["cache_read_input_tokens"],
            "n_output_tokens": usage["output_tokens"],
            "cache_creation_tokens": usage["cache_creation_input_tokens"],
        })

    if terminal is not None:
        terminal_model = terminal.get("model")
        if (
            isinstance(terminal_model, str)
            and terminal_model
            and terminal_model != SUPPORTED_MODEL
        ):
            reasons.add("terminal_model_mismatch")
        if terminal.get("is_error") is True:
            reasons.add("terminal_error")
        if terminal.get("usage_is_incomplete") is True:
            reasons.add("terminal_usage_incomplete")
        if terminal.get("subtype") not in {"success", None}:
            reasons.add("terminal_status_not_success")
    if terminal_usage is not None:
        reported_total = terminal.get("total_tokens")
        if reported_total is not None:
            expected_total = (
                terminal_usage["input_tokens"]
                + terminal_usage["output_tokens"]
            )
            if _nonnegative_int(reported_total) != expected_total:
                reasons.add("terminal_total_tokens_mismatch")
    observed = bool(token_usage_events)
    if not observed:
        reasons.add("request_ledger_unavailable")
    if terminal_usage is not None and terminal_usage != totals:
        reasons.add("terminal_aggregate_mismatch")
    complete = bool(
        not reasons
        and terminal_usage is not None
        and observed
        and terminal_usage == totals
        and totals["input_tokens"] + totals["output_tokens"] > 0
    )
    selected = totals if observed else {name: 0 for name in names}
    prompt = selected["input_tokens"]
    reason_order = (
        "terminal_aggregate_missing",
        "terminal_aggregate_multiple",
        "terminal_usage_missing_or_invalid",
        "terminal_model_mismatch",
        "terminal_error",
        "terminal_usage_incomplete",
        "terminal_status_not_success",
        "terminal_total_tokens_mismatch",
        "request_model_mismatch",
        "request_message_invalid",
        "request_usage_invalid",
        "request_id_missing",
        "request_id_conflict",
        "request_ledger_unavailable",
        "terminal_aggregate_mismatch",
    )
    incomplete_reasons = [reason for reason in reason_order if reason in reasons]
    return {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "codebuddy",
        "model": SUPPORTED_MODEL,
        "complete": complete,
        "request_count": len(token_usage_events) if observed else 0,
        "n_input_tokens": prompt,
        "n_cache_tokens": selected["cache_read_input_tokens"],
        "n_output_tokens": selected["output_tokens"],
        "cache_creation_tokens": selected["cache_creation_input_tokens"],
        "token_usage_events": token_usage_events if observed else [],
        "request_usage_complete": complete,
        "request_usage_observed": observed,
        "timed_usage_complete": False,
        "usage_incomplete_reason": (
            None if complete else incomplete_reasons[0]
        ),
        "usage_incomplete_reasons": incomplete_reasons,
        "usage_evidence_tier": (
            "complete_reconciled" if complete
            else "observed_unreconciled" if observed
            else "unavailable"
        ),
        "provider_actual_cost_observed": False,
        "cost_semantics": "server-priced-api-equivalent",
    }


_install_command = codebuddy_install_command


class CodeBuddySubscription(ClaudeCode):
    """Run HY4 through a temporary copy of a CodeBuddy subscription login."""

    SUPPORTS_ATIF = True
    _REMOTE_CLI = PurePosixPath("/opt/codebuddy/bin/codebuddy")
    _REMOTE_SECRET_ROOT = PurePosixPath("/tmp/dradar-codebuddy-auth")
    _REMOTE_STORAGE = _REMOTE_SECRET_ROOT / "local_storage"
    # Configuration may contain session metadata or refreshed credentials.
    # Keep it outside /logs so Pier's artifact collector cannot upload it.
    _REMOTE_CONFIG = PurePosixPath("/tmp/dradar-codebuddy-config")
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-codebuddy-user")
    _REMOTE_SHARED_AUTH = (
        _REMOTE_USER_HOME / ".local" / "share" / "CodeBuddyExtension"
        / "Data" / "Public" / "auth"
    )
    _STREAM = PurePosixPath("/logs/agent/claude-code.txt")
    _MCP_CONFIG = PurePosixPath("/tmp/dradar-codebuddy-empty-mcp.json")
    _USAGE_FILE = "provider-usage.json"

    @staticmethod
    def name() -> str:
        return "codebuddy"

    def __init__(
        self,
        *args: Any,
        auth_dir: str,
        reasoning_effort: str,
        model_name: str | None = None,
        version: str | None = CODEBUDDY_CLI_VERSION,
        **kwargs: Any,
    ):
        login_root = Path(auth_dir)
        auth_root = login_root / "auth"
        storage_root = login_root / "local_storage"
        auth_files = sorted(auth_root.glob("*.info")) if auth_root.is_dir() else []
        storage_files = (
            sorted(storage_root.glob("entry_*.info"))
            if storage_root.is_dir() else []
        )
        files = [*auth_files, *storage_files]
        if (
            not auth_files
            or not storage_files
            or len(auth_files) > 16
            or len(storage_files) > 256
            or any(
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_nlink != 1
                or (
                    os.name != "nt"
                    and stat.S_IMODE(path.stat().st_mode) & 0o077
                )
                for path in files
            )
        ):
            raise ValueError("managed CodeBuddy login storage is missing or unsafe")
        resolved_model = model_name or SUPPORTED_MODEL
        if resolved_model != SUPPORTED_MODEL:
            raise ValueError(f"CodeBuddy adapter enables only {SUPPORTED_MODEL}")
        if reasoning_effort not in SUPPORTED_EFFORTS:
            raise ValueError("unsupported CodeBuddy reasoning effort")
        if version != CODEBUDDY_CLI_VERSION:
            raise ValueError(f"CodeBuddy adapter requires CLI {CODEBUDDY_CLI_VERSION}")
        self._auth_files = tuple(auth_files)
        self._storage_files = tuple(storage_files)
        self._reasoning_effort = reasoning_effort
        super().__init__(
            *args,
            model_name=resolved_model,
            version=version,
            reasoning_effort=reasoning_effort,
            **kwargs,
        )

    def get_version_command(self) -> str:
        return f"{self._REMOTE_CLI.as_posix()} --version"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(),
            version=CODEBUDDY_CLI_VERSION,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"test \"$({self._REMOTE_CLI.as_posix()} --version)\" = "
                f"'{CODEBUDDY_CLI_VERSION}'"
            ),
            cache_key=f"dradar-codebuddy-{CODEBUDDY_CLI_VERSION}-native-v1",
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=[
            "copilot.tencent.com",
            "tencent.sso.codebuddy.cn",
            "tencent.sso.copilot.tencent.com",
            "code.codebuddy.ai",
        ])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        emit_worker_registered(runtime="pier", context="agent", profile="codebuddy")
        del context
        remote_secret = self._REMOTE_SECRET_ROOT.as_posix()
        remote_storage = self._REMOTE_STORAGE.as_posix()
        remote_config = self._REMOTE_CONFIG.as_posix()
        remote_home = self._REMOTE_USER_HOME.as_posix()
        remote_auth = self._REMOTE_SHARED_AUTH.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        remote_stream = self._STREAM.as_posix()
        remote_mcp = self._MCP_CONFIG.as_posix()
        env = self.build_process_env({})
        for name in tuple(env):
            if name.startswith("CODEBUDDY_") or name in {
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
            }:
                env.pop(name, None)
        env.update({
            "HOME": remote_home,
            "CODEBUDDY_CONFIG_DIR": remote_config,
            "CODEBUDDY_CODE_ENABLE_TELEMETRY": "0",
            "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
            "CODEBUDDY_CODE_DISABLE_AUTO_MEMORY": "1",
            "CODEBUDDY_DISABLE_BACKGROUND_TASKS": "1",
            "CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CODEBUDDY_DISABLE_CRON": "1",
            "CODEBUDDY_DISABLE_IDE": "1",
            "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE": "1",
            "DISABLE_AUTOUPDATER": "1",
        })
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_secret)} {shlex.quote(remote_storage)} "
                f"{shlex.quote(remote_config)} {shlex.quote(remote_home)} "
                f"{shlex.quote(remote_auth)} && "
                f"chmod 700 {shlex.quote(remote_secret)} {shlex.quote(remote_storage)} "
                f"{shlex.quote(remote_config)} {shlex.quote(remote_home)} "
                f"{shlex.quote(remote_auth)} && "
                f"ln -s {shlex.quote(remote_storage)} "
                f"{shlex.quote(remote_config + '/local_storage')}"
            ),
            env=env,
        )
        for source in self._storage_files:
            await environment.upload_file(source, f"{remote_storage}/{source.name}")
        for source in self._auth_files:
            await environment.upload_file(source, f"{remote_auth}/{source.name}")
        empty_mcp = self.logs_dir / "codebuddy-empty-mcp.json"
        empty_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        if os.name != "nt":
            os.chmod(empty_mcp, 0o600)
        await environment.upload_file(empty_mcp, remote_mcp)
        targets = " ".join(
            shlex.quote(f"{remote_storage}/{source.name}")
            for source in self._storage_files
        )
        auth_targets = " ".join(
            shlex.quote(f"{remote_auth}/{source.name}")
            for source in self._auth_files
        )
        await self.exec_as_agent(
            environment,
            command=f"chmod 600 {targets} {auth_targets} {shlex.quote(remote_mcp)}",
            env=env,
        )
        prompt_name = "DRADAR_CODEBUDDY_PROMPT"
        run_env = {**env, prompt_name: instruction}
        command = (
            f"set -o pipefail; prompt=\"${prompt_name}\"; unset {prompt_name}; "
            f"printf '%s' \"$prompt\" | {remote_cli} "
            "--print --verbose --output-format stream-json --input-format text "
            f"--model {SUPPORTED_MODEL} --effort {self._reasoning_effort} "
            "--permission-mode bypassPermissions "
            "--tools Bash,Edit,Read,Write,Glob,Grep "
            f"--strict-mcp-config --mcp-config {shlex.quote(remote_mcp)} "
            "--setting-sources user "
            f"2>&1 | tee {shlex.quote(remote_stream)}"
        )
        try:
            await self.exec_as_agent(environment, command=command, env=run_env)
        finally:
            # Preserve a valid provider refresh in the private run copy.  The
            # host-side provider context validates it and promotes it only if
            # it is newer than the credential returned by concurrent workers.
            for source in self._auth_files:
                try:
                    await environment.download_file(
                        f"{remote_auth}/{source.name}", source,
                    )
                    if os.name != "nt":
                        os.chmod(source, 0o600)
                except Exception as exc:  # noqa: BLE001 - Pier backend boundary
                    self.logger.warning(
                        "Could not recover refreshed CodeBuddy auth state: %s", exc
                    )
            for source in self._storage_files:
                try:
                    await environment.download_file(
                        f"{remote_storage}/{source.name}", source,
                    )
                    if os.name != "nt":
                        os.chmod(source, 0o600)
                except Exception as exc:  # noqa: BLE001 - Pier backend boundary
                    self.logger.warning(
                        "Could not recover refreshed CodeBuddy storage state: %s", exc
                    )
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        "set +e; rm -rf /logs/agent/sessions; "
                        "mkdir -p /logs/agent/sessions; "
                        f"if [ -d {shlex.quote(remote_config + '/projects')} ]; "
                        "then cp -R "
                        f"{shlex.quote(remote_config + '/projects')} "
                        "/logs/agent/sessions/projects || true; fi; "
                        f"rm -rf {shlex.quote(remote_secret)}; "
                        f"rm -rf {shlex.quote(remote_home)}; "
                        f"rm -rf {shlex.quote(remote_config)}"
                    ),
                    env=env,
                )
            except Exception as exc:  # noqa: BLE001 - Pier backend boundary
                self.logger.warning(
                    "Could not archive CodeBuddy sessions or clean remote state: %s",
                    exc,
                )

    def populate_context_post_run(self, context: AgentContext) -> None:
        events: list[dict] = []
        stream = self.logs_dir / "claude-code.txt"
        try:
            lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        usage = _codebuddy_usage_facts(events)
        try:
            (self.logs_dir / self._USAGE_FILE).write_text(
                json.dumps(usage, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            pass

        super().populate_context_post_run(context)
        complete = usage["complete"] is True
        observed = usage["request_usage_observed"] is True
        context.cost_usd = None
        context.n_input_tokens = int(usage["n_input_tokens"]) if observed else 0
        context.n_cache_tokens = int(usage["n_cache_tokens"]) if observed else 0
        context.n_output_tokens = int(usage["n_output_tokens"]) if observed else 0

        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(trajectory, dict):
            return
        agent = trajectory.get("agent")
        if not isinstance(agent, dict):
            agent = {}
            trajectory["agent"] = agent
        agent.update({
            "name": self.name(),
            "version": CODEBUDDY_CLI_VERSION,
            "model_name": SUPPORTED_MODEL,
            "extra": {
                "provider": "codebuddy-subscription",
                "oauth": True,
                "credential_mode": "isolated-run-copy",
            },
        })
        metrics = trajectory.get("final_metrics")
        if not isinstance(metrics, dict):
            metrics = {}
            trajectory["final_metrics"] = metrics
        metrics.update({
            "total_prompt_tokens": usage["n_input_tokens"] if observed else None,
            "total_cached_tokens": usage["n_cache_tokens"] if observed else None,
            "total_completion_tokens": usage["n_output_tokens"] if observed else None,
            "total_cost_usd": None,
        })
        extra = metrics.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        extra.update({
            "billing_basis": "subscription",
            "cost_not_reported": True,
            "usage_complete": complete,
            "usage_evidence_tier": usage["usage_evidence_tier"],
            "usage_incomplete_reason": usage["usage_incomplete_reason"],
        })
        metrics["extra"] = extra
        try:
            trajectory_path.write_text(
                json.dumps(trajectory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass


__all__ = ["CodeBuddySubscription", "_codebuddy_usage_facts", "_install_command"]
