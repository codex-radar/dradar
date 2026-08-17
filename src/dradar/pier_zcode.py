"""Credential-isolated Pier adapter for ZCode with BigModel Coding Plan.

The adapter runs the official ZCode Protocol server from a digest-pinned
desktop bundle.  The Coding Plan key is uploaded as an owner-only run file,
moved into ZCode's in-memory session-secret store during ``session/create``,
and then unlinked before the model is allowed to execute a tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.trajectories import Agent, FinalMetrics, Step, Trajectory
from pier.utils.trajectory_metrics import populate_context_from_final_metrics


ZCODE_CLI_VERSION = "0.16.3"
ZCODE_CLI_SHA256 = (
    "4130592942dcaa070f898c2c0152a8345dbfacbf6efb6422b2753c626e756bf5"
)
NODE_VERSION = "22.23.2"
NODE_SHA256 = {
    "x64": "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
    "arm64": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
}
SUPPORTED_EFFORTS = frozenset({"low", "high", "max"})
_ARTIFACT_ID_RE = re.compile(r"[0-9a-f]{32}")


def _install_command() -> str:
    x64_sha = NODE_SHA256["x64"]
    arm64_sha = NODE_SHA256["arm64"]
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'ZCode requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl python3 tar xz-utils; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl python3 tar xz; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl python3 tar xz; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f"  x86_64) node_arch=x64; node_sha={x64_sha} ;; "
        f"  aarch64|arm64) node_arch=arm64; node_sha={arm64_sha} ;; "
        "  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; "
        "esac; "
        f"node_archive=/tmp/node-v{NODE_VERSION}-linux-${{node_arch}}.tar.xz; "
        f"node_url=https://nodejs.org/dist/v{NODE_VERSION}/"
        f"node-v{NODE_VERSION}-linux-${{node_arch}}.tar.xz; "
        "curl --fail --silent --show-error --location "
        '  --output "${node_archive}" "${node_url}"; '
        'printf \'%s  %s\\n\' "${node_sha}" "${node_archive}" '
        "  | sha256sum --check --strict -; "
        'tar -xJf "${node_archive}" -C /opt; '
        f"node_root=/opt/node-v{NODE_VERSION}-linux-${{node_arch}}; "
        'ln -sfn "${node_root}/bin/node" /usr/local/bin/node; '
        'rm -f "${node_archive}"; '
        f"test \"$(node --version)\" = 'v{NODE_VERSION}'; "
        "test -x \"$(command -v python3)\""
    )


_PROTOCOL_RUNNER = r'''#!/usr/bin/env python3
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


class ProtocolError(RuntimeError):
    pass


node, cli, key_path, instruction_path, effort, outcome_path, events_path, stderr_path = sys.argv[1:]
key_file = Path(key_path)
key = key_file.read_text(encoding="utf-8").strip()
if not key or any(character.isspace() for character in key):
    raise ProtocolError("Coding Plan key file is invalid")
instruction = Path(instruction_path).read_text(encoding="utf-8")
if not instruction.strip():
    raise ProtocolError("instruction is empty")


def redact(value):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED_ZCODE_CREDENTIAL]")
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {name: redact(item) for name, item in value.items()}
    return value


proc = subprocess.Popen(
    [node, cli, "app-server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    cwd="/app",
)
incoming = queue.Queue()
stderr_chunks = []


def read_stdout():
    try:
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                incoming.put({"_invalid": redact(line.rstrip())})
                continue
            incoming.put(message)
    finally:
        incoming.put({"_eof": True})


def read_stderr():
    for line in proc.stderr:
        stderr_chunks.append(redact(line))


threading.Thread(target=read_stdout, daemon=True).start()
threading.Thread(target=read_stderr, daemon=True).start()
next_id = 0
notifications = []


def send(message):
    if proc.stdin is None:
        raise ProtocolError("ZCode Protocol stdin is unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    proc.stdin.flush()


def respond_to_server(message):
    method = message.get("method")
    if method == "session/requestRuntimePreferences":
        send({
            "id": message["id"],
            "result": {
                "nativeSearchEnhancementsEnabled": False,
                "memoryEnabled": False,
                "askUserQuestionAutoResolutionEnabled": True,
                "modelContextBudgetStrategy": "preflight-v1",
            },
        })
        return
    # No browser, interactive-input, dynamic-header, or permission request is
    # expected in yolo mode with the fixed tool policy. Fail closed if the
    # runtime attempts to expand that contract.
    send({
        "id": message["id"],
        "error": {"code": -32601, "message": f"unsupported server request: {method}"},
    })


def call(method, params, *, timeout=120.0):
    global next_id
    next_id += 1
    request_id = f"client-{next_id}"
    send({"id": request_id, "method": method, "params": params})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            message = incoming.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            if proc.poll() is not None:
                raise ProtocolError(f"ZCode Protocol exited early with status {proc.returncode}")
            continue
        if message.get("_eof"):
            raise ProtocolError("ZCode Protocol connection closed")
        if "_invalid" in message:
            notifications.append(message)
            continue
        if "id" in message and "method" in message:
            respond_to_server(message)
            continue
        if message.get("id") == request_id:
            if "error" in message:
                error = redact(message["error"])
                raise ProtocolError(f"{method} failed: {error.get('message', 'unknown error')}")
            return message.get("result")
        notifications.append(redact(message))
    raise ProtocolError(f"{method} timed out")


def optional_call(method, params, *, timeout=30.0):
    try:
        return call(method, params, timeout=timeout)
    except ProtocolError as exc:
        notifications.append({"method": method, "diagnostic": str(exc)})
        return None


session_id = None
outcome = None
try:
    created = call(
        "session/create",
        {
            "workspace": {"workspacePath": "/app", "workspaceKey": "dradar-zcode"},
            "runtimeModel": {
                "revision": "dradar-zcode-glm-5.3-v1",
                "generatedAt": int(time.time() * 1000),
                "model": {"providerId": "bigmodel-coding-plan", "modelId": "glm-5.3"},
                "provider": {
                    "providerId": "bigmodel-coding-plan",
                    "kind": "anthropic",
                    "apiFormat": "anthropic-messages",
                    "label": "BigModel Coding Plan",
                    "source": "ephemeral",
                    "baseURL": "https://open.bigmodel.cn/api/anthropic",
                    "apiKey": {"source": "inline", "value": key},
                    "apiKeyRequired": True,
                    "models": [{"modelId": "glm-5.3", "label": "GLM-5.3"}],
                },
                "thoughtLevel": effort,
            },
            "thoughtLevel": effort,
            "mode": "yolo",
            "persistence": "deferred",
            "mcpServers": [],
            "toolAllowlist": [
                "Read", "Write", "Edit", "ApplyPatch", "Bash", "Glob", "Grep",
                "TodoRead", "TodoWrite",
            ],
            # ZCode normalizes deny rules such as ``Read(/tmp/...)`` to the
            # tool name before registering tools, so path-shaped rules would
            # disable the coding tool completely.  The key file is instead
            # unlinked below before the first model-controlled tool can run.
            "toolDenylist": [
                "WebFetch", "WebSearch", "web_search", "Agent", "Task", "Skill",
                "AskUserQuestion", "SendMessage", "RespondToCoordinator", "TaskOutput",
                "TaskStop", "js", "js_reset", "js_add_node_module_dir",
                "mcp__node_repl__*",
            ],
        },
        timeout=120.0,
    )
    session = created.get("session") if isinstance(created, dict) else None
    session_id = session.get("sessionId") if isinstance(session, dict) else None
    if not isinstance(session_id, str) or not session_id.startswith("sess_"):
        raise ProtocolError("session/create returned no valid session id")
    settings = created.get("settings") if isinstance(created, dict) else None
    thought = settings.get("thoughtLevel") if isinstance(settings, dict) else None
    available = {
        item.get("value") for item in thought.get("available", [])
        if isinstance(item, dict)
    } if isinstance(thought, dict) else set()
    if thought.get("current") != effort or available != {"low", "high", "max"}:
        raise ProtocolError("ZCode did not apply the requested GLM-5.3 thought level")
    projection = created.get("projection") if isinstance(created, dict) else None
    if not isinstance(projection, dict) or projection.get("contextWindow") != 1000000:
        raise ProtocolError("ZCode did not materialize the pinned GLM-5.3 model")

    # ZCode has replaced the inline value with an in-memory session-secret ref.
    # Remove the only filesystem copy before any model-controlled tool can run.
    key_file.unlink()
    call("session/send", {"sessionId": session_id, "content": instruction}, timeout=120.0)

    deadline = time.monotonic() + 90 * 60
    seen_running = False
    final_state = None
    while time.monotonic() < deadline:
        state = call("session/read", {"sessionId": session_id}, timeout=60.0)
        projection = state.get("projection") if isinstance(state, dict) else None
        status = projection.get("status") if isinstance(projection, dict) else None
        turns = projection.get("turnCount", 0) if isinstance(projection, dict) else 0
        if status not in {None, "idle"}:
            seen_running = True
        if isinstance(turns, int) and turns > 0 and status == "idle":
            final_state = state
            break
        if status in {"error", "failed", "stopped"}:
            final_state = state
            break
        time.sleep(1.0)
    if final_state is None:
        raise ProtocolError("ZCode session did not finish within 90 minutes")
    enabled_tools = set()
    state_messages = final_state.get("messages") if isinstance(final_state, dict) else None
    if isinstance(state_messages, list):
        for message in state_messages:
            info = message.get("info") if isinstance(message, dict) else None
            tools = info.get("tools") if isinstance(info, dict) else None
            if isinstance(tools, dict):
                enabled_tools.update(name for name, enabled in tools.items() if enabled is True)
    # With Bash available ZCode may select its embedded-search branch and
    # intentionally unregister the standalone Glob/Grep tools.  Read, write,
    # edit, and shell execution are the invariant coding surface.
    required_tools = {"Read", "Write", "Edit", "Bash"}
    if not required_tools.issubset(enabled_tools):
        missing = ", ".join(sorted(required_tools - enabled_tools))
        raise ProtocolError(f"ZCode coding tools were unavailable: {missing}")
    messages = optional_call("session/messages", {"sessionId": session_id})
    events = optional_call(
        "session/events", {"sessionId": session_id, "afterSeq": 0, "limit": 5000}
    )
    usage = optional_call("session/usage", {"sessionId": session_id})
    outcome = {
        "schema": "dradar-zcode-outcome-v1",
        "sessionId": session_id,
        "model": "glm-5.3",
        "reasoningEffort": effort,
        "seenRunning": seen_running,
        "state": final_state,
        "messages": messages,
        "events": events,
        "usage": usage,
        "notifications": notifications,
    }
finally:
    try:
        key_file.unlink()
    except FileNotFoundError:
        pass
    if session_id is not None and proc.poll() is None:
        optional_call("session/close", {"sessionId": session_id}, timeout=15.0)
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    stderr_text = redact("".join(stderr_chunks))
    Path(stderr_path).write_text(stderr_text, encoding="utf-8")
    os.chmod(stderr_path, 0o600)

if outcome is None:
    raise ProtocolError("ZCode produced no outcome")
safe_outcome = redact(outcome)
encoded = json.dumps(safe_outcome, ensure_ascii=False, separators=(",", ":"))
if key in encoded:
    raise ProtocolError("Coding Plan credential reached the ZCode outcome")
Path(outcome_path).write_text(encoded + "\n", encoding="utf-8")
os.chmod(outcome_path, 0o600)
Path(events_path).write_text(
    json.dumps(redact(notifications), ensure_ascii=False, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(events_path, 0o600)
print(f"ZCode session {session_id} completed with GLM-5.3 ({effort}).")
'''


class ZCodeBigModel(BaseInstalledAgent):
    """Run GLM-5.3 through ZCode's v1 stdio protocol."""

    SUPPORTS_ATIF = True
    _REMOTE_HOME = PurePosixPath("/tmp/dradar-zcode-home")
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-zcode-user")
    _REMOTE_BIN_DIR = PurePosixPath("/tmp/dradar-zcode-bin")
    _REMOTE_SECRET_ROOT = PurePosixPath("/tmp/dradar-zcode-secrets")
    _REMOTE_CLI = _REMOTE_BIN_DIR / "zcode.cjs"
    _REMOTE_RUNNER = _REMOTE_BIN_DIR / "protocol_runner.py"
    _OUTCOME_FILE = "zcode-outcome.json"
    _EVENTS_FILE = "zcode-protocol-events.json"
    _STDERR_FILE = "zcode-stderr.log"
    _STREAM_FILE = "zcode-protocol.log"

    @staticmethod
    def name() -> str:
        return "zcode"

    def __init__(
        self,
        *args: Any,
        api_key_file: str,
        zcode_cli_file: str,
        reasoning_effort: str,
        model_name: str | None = None,
        version: str | None = ZCODE_CLI_VERSION,
        **kwargs: Any,
    ):
        key_file = Path(api_key_file)
        cli_file = Path(zcode_cli_file)
        try:
            key_info = key_file.stat()
            key_value = key_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("ZCode Coding Plan key file is missing or unreadable") from exc
        if not stat.S_ISREG(key_info.st_mode) or not key_value:
            raise ValueError("ZCode Coding Plan key must be a non-empty regular file")
        if any(character.isspace() for character in key_value):
            raise ValueError("ZCode Coding Plan key must be one non-empty line")
        if os.name != "nt" and stat.S_IMODE(key_info.st_mode) & 0o077:
            raise ValueError("ZCode Coding Plan key permissions must be 0600 or stricter")
        try:
            cli_info = cli_file.stat()
            cli_digest = hashlib.sha256(cli_file.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError("Pinned ZCode CLI is missing or unreadable") from exc
        if not stat.S_ISREG(cli_info.st_mode) or cli_digest != ZCODE_CLI_SHA256:
            raise ValueError("Pinned ZCode CLI integrity check failed")
        if (model_name or "glm-5.3") != "glm-5.3":
            raise ValueError("ZCode adapter enables only glm-5.3")
        if reasoning_effort not in SUPPORTED_EFFORTS:
            raise ValueError("ZCode reasoning_effort must be low, high, or max")
        resolved_version = version or ZCODE_CLI_VERSION
        if resolved_version != ZCODE_CLI_VERSION:
            raise ValueError(f"ZCode adapter requires exact CLI {ZCODE_CLI_VERSION}")
        extra_env = dict(kwargs.get("extra_env") or {})
        forbidden = sorted(
            name for name in extra_env
            if name.upper().startswith("ZCODE")
            or any(marker in name.upper() for marker in ("KEY", "PASSWORD", "SECRET", "TOKEN"))
        )
        if forbidden:
            raise ValueError(
                "ZCode credentials and reserved settings may not use extra_env: "
                + ", ".join(forbidden)
            )
        self._api_key_file = key_file
        self._zcode_cli_file = cli_file
        self._reasoning_effort = reasoning_effort
        self._credential_value = key_value
        self._instruction = ""
        run_secret_dir = self._REMOTE_SECRET_ROOT / uuid.uuid4().hex
        self._remote_secret_dir = run_secret_dir
        self._remote_api_key = run_secret_dir / "coding-plan-key"
        super().__init__(*args, model_name="glm-5.3", version=resolved_version, **kwargs)

    def get_version_command(self) -> str:
        return "true"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(),
            version=ZCODE_CLI_VERSION,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"test \"$(node --version)\" = 'v{NODE_VERSION}' && "
                "test -x \"$(command -v python3)\""
            ),
            cache_key=(
                f"dradar-zcode-{ZCODE_CLI_VERSION}-node-{NODE_VERSION}-protocol-v1"
            ),
        )

    def network_allowlist(self) -> NetworkAllowlist:
        # The domestic Coding Plan model traffic uses open.bigmodel.cn, while
        # the official ZCode runtime also reads its control-plane metadata
        # from zcode.z.ai before starting a full agent turn.  Keep this list
        # exact: telemetry, web search, and arbitrary network access remain
        # blocked by the adapter and Pier egress proxy.
        return NetworkAllowlist(domains=["open.bigmodel.cn", "zcode.z.ai"])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        self._instruction = instruction
        remote_home = self._REMOTE_HOME.as_posix()
        remote_user_home = self._REMOTE_USER_HOME.as_posix()
        remote_bin = self._REMOTE_BIN_DIR.as_posix()
        remote_secret = self._remote_secret_dir.as_posix()
        remote_key = self._remote_api_key.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        remote_runner = self._REMOTE_RUNNER.as_posix()
        outcome = f"/logs/agent/{self._OUTCOME_FILE}"
        events = f"/logs/agent/{self._EVENTS_FILE}"
        stderr = f"/logs/agent/{self._STDERR_FILE}"
        stream = f"/logs/agent/{self._STREAM_FILE}"
        instruction_path = f"{remote_secret}/instruction.txt"
        env = self.build_process_env({
            "HOME": remote_user_home,
            "XDG_CONFIG_HOME": remote_home + "/config",
            "XDG_DATA_HOME": remote_home + "/data",
            "XDG_CACHE_HOME": remote_home + "/cache",
            "ZCODE_TELEMETRY_DISABLED": "1",
            "ZCODE_DISABLE_TELEMETRY": "1",
            "ZCODE_NO_AUTO_UPDATE": "1",
            "NODE_USE_ENV_PROXY": "1",
        })
        for name in (
            "ZCODE_API_KEY", "BIGMODEL_API_KEY", "ZHIPUAI_API_KEY",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        ):
            env.pop(name, None)
        directories = (remote_home, remote_user_home, remote_bin, remote_secret)
        setup = "mkdir -p " + " ".join(shlex.quote(item) for item in directories)
        setup += " && chmod 700 " + " ".join(shlex.quote(item) for item in directories)
        await self.exec_as_agent(environment, command=setup, env=env)

        local_runner = self.logs_dir / "zcode-protocol-runner.py"
        local_instruction = self.logs_dir / "zcode-instruction.txt"
        local_runner.parent.mkdir(parents=True, exist_ok=True)
        local_runner.write_text(_PROTOCOL_RUNNER, encoding="utf-8")
        local_instruction.write_text(instruction, encoding="utf-8")
        await environment.upload_file(self._zcode_cli_file, remote_cli)
        await environment.upload_file(local_runner, remote_runner)
        await environment.upload_file(local_instruction, instruction_path)
        await environment.upload_file(self._api_key_file, remote_key)
        targets = " ".join(
            shlex.quote(item)
            for item in (remote_cli, remote_runner, instruction_path, remote_key)
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} {targets} "
                    f"&& chmod 600 {targets}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(
                environment, command=f"chmod 600 {targets}", env=env,
            )
        version_pattern = ZCODE_CLI_VERSION.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"node {shlex.quote(remote_cli)} version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        args = " ".join(
            shlex.quote(item)
            for item in (
                "node", remote_cli, remote_key, instruction_path,
                self._reasoning_effort, outcome, events, stderr,
            )
        )
        command = (
            "set -o pipefail; "
            f"trap 'rm -f {shlex.quote(remote_key)}' EXIT HUP INT TERM; "
            f"cd /app; python3 {shlex.quote(remote_runner)} {args} "
            f"2>&1 | tee {shlex.quote(stream)}"
        )
        await self.exec_as_agent(environment, command=command, env=env, cwd="/app")

    def _redact_or_reject_credential_output(self, paths: list[Path]) -> None:
        leaked = False
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if self._credential_value not in text:
                continue
            leaked = True
            path.write_text(
                text.replace(
                    self._credential_value, "[REDACTED_ZCODE_CREDENTIAL]"
                ),
                encoding="utf-8",
            )
        if leaked:
            raise ValueError(
                "ZCode credential material reached agent output; logs were redacted "
                "and the run was rejected"
            )

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n\n".join(
                text for item in value if (text := ZCodeBigModel._text(item))
            )
        if isinstance(value, dict):
            for key in ("text", "content", "message"):
                if key in value:
                    text = ZCodeBigModel._text(value[key])
                    if text:
                        return text
        return ""

    def populate_context_post_run(self, context: AgentContext) -> None:
        paths = [
            self.logs_dir / self._OUTCOME_FILE,
            self.logs_dir / self._EVENTS_FILE,
            self.logs_dir / self._STDERR_FILE,
            self.logs_dir / self._STREAM_FILE,
        ]
        self._redact_or_reject_credential_output(paths)
        try:
            payload = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        steps = [Step(step_id=1, source="user", message=self._instruction)]
        raw_messages = payload.get("messages")
        if isinstance(raw_messages, dict):
            raw_messages = raw_messages.get("messages")
        assistant_calls = 0
        if isinstance(raw_messages, list):
            for message in raw_messages:
                if not isinstance(message, dict):
                    continue
                info = message.get("info")
                role = message.get("role") or message.get("source") or (
                    info.get("role") if isinstance(info, dict) else None
                )
                if role not in {"assistant", "agent"}:
                    continue
                text = self._text(
                    message.get("content") or message.get("message") or message.get("parts")
                )
                if not text:
                    continue
                assistant_calls += 1
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        source="agent",
                        message=text,
                        model_name="glm-5.3",
                        reasoning_effort=self._reasoning_effort,
                        llm_call_count=1,
                    )
                )
        if assistant_calls == 0:
            state = payload.get("state")
            messages = state.get("messages") if isinstance(state, dict) else None
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    info = message.get("info")
                    role = message.get("role") or (
                        info.get("role") if isinstance(info, dict) else None
                    )
                    if role != "assistant":
                        continue
                    text = self._text(message.get("content") or message.get("parts"))
                    if text:
                        assistant_calls += 1
                        steps.append(
                            Step(
                                step_id=len(steps) + 1,
                                source="agent",
                                message=text,
                                model_name="glm-5.3",
                                reasoning_effort=self._reasoning_effort,
                                llm_call_count=1,
                            )
                        )
        if assistant_calls == 0:
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        metrics = FinalMetrics(
            total_prompt_tokens=usage.get("inputTokens"),
            total_completion_tokens=usage.get("outputTokens"),
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "coding-plan",
                "cost_not_reported": True,
                "reasoning_tokens": usage.get("reasoningTokens"),
                "cache_creation_tokens": usage.get("cacheCreationTokens"),
                "cache_read_tokens": usage.get("cacheReadTokens"),
                "model_request_count": usage.get("modelRequestCount"),
                "model_error_count": usage.get("modelErrorCount"),
            },
        )
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=payload.get("sessionId") or str(uuid.uuid4()),
            agent=Agent(
                name=self.name(),
                version=ZCODE_CLI_VERSION,
                model_name="glm-5.3",
                extra={"provider": "bigmodel-coding-plan", "protocol": 1},
            ),
            steps=steps,
            final_metrics=metrics,
        )
        try:
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return
        populate_context_from_final_metrics(context, metrics)


__all__ = ["ZCodeBigModel"]
