"""Portable Pier adapter for DeepSeek Harness' two-tool minimal agent.

The adapter uses only the published Pier 0.3.0 extension interface and the
published ``@deepseek-ai/dsh`` package. Runtime model egress is restricted to
DeepSeek's API, while the task's Docker container remains the
filesystem/process isolation boundary.
"""

from __future__ import annotations

import os
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

DSH_VERSION = "0.1.0-rc.6"
NODE_VERSION = "22.23.2"
NODE_SHA256 = {
    "x64": "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
    "arm64": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
}
SUPPORTED_MODELS = frozenset(
    {"dsh-deepseek-v4-flash", "dsh-deepseek-v4-pro"}
)
RUNTIME_MODELS = {
    "dsh-deepseek-v4-flash": "deepseek-v4-flash",
    "dsh-deepseek-v4-pro": "deepseek-v4-pro",
}
SUPPORTED_REASONING_EFFORTS = frozenset({"off", "high", "max"})

_MINIMAL_PATCH = """\
# Run DSH's shipped `minimal` agent preset through a one-shot headless runner.
# The headless base contributes a global coding toolset, while a preset is
# supposed to own the model-facing composition. Disable those global tools so
# the scoped shipped preset supplies exactly persistent bash and the editor.
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-jobs
  disabled: true
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: agent-instructions
  disabled: true
- id: tool-skill
  disabled: true
- id: plan-mode
  disabled: true
- id: compaction-basic
  disabled: true
- id: command-compact
  disabled: true
- id: tool-subagent-control
  disabled: true
- id: tool-subagent-list-agents
  disabled: true
- id: tool-subagent
  disabled: true
- id: tool-subagent-fork
  disabled: true
- id: tool-subagent-report
  disabled: true
- id: tool-workflow
  disabled: true
- id: tool-result-pruner
  disabled: true
- id: tool-todo
  disabled: true
- id: tool-goal
  disabled: true
- id: tool-ralph
  disabled: true
- id: tool-str-replace-editor
  disabled: true
- id: tool-web
  disabled: true
- id: web-search-deepseek
  disabled: true
- id: web
  disabled: true
- id: repeat-tool-reminder
  disabled: true

# Headless does not normally mount the agent-preset roster. Pin it to the
# read-only presets shipped by this exact DSH installation and exclude the
# per-user root, so a task cannot replace `minimal` through DSH_HOME.
- insert:
    - id: agent-presets
      name: '@deepseek-ai/dsh-agent-presets'
      config:
        default: minimal
        roots:
          - path: /opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/config/agent-presets
            trust: system
        includeUserRoot: false

# The stock headless runner creates an uncomposed agent. A patch cannot replace
# an existing row's plugin identity, so disable that row and insert this pinned
# adapter runner under its own id. It mounts `minimal` in factory setup before
# the agent is published.
- id: headless-runner
  disabled: true

- insert:
    - id: minimal-headless-runner
      name: /tmp/dsh-config/minimal-headless-runner.mjs
      inject: [headlessStartup]
      config:
        task: !!js ctx.headlessStartup.task

- id: agent-default-model
  config:
    provider: deepseek-official
    model: !!js process.env.DSH_MODEL ?? 'deepseek-v4-flash'

- id: llm-deepseek
  config:
    reasoningEffort: !!js process.env.DSH_REASONING_EFFORT ?? 'high'

# Resolve the provider key from DSH's non-environment credential service. The
# adapter sets a per-run path and removes the file as soon as DSH opens it.
- id: credentials
  config:
    path: !!js process.env.DSH_CREDENTIALS_FILE
    watch: false
"""

_MINIMAL_HEADLESS_RUNNER = """\
import { randomUUID } from "node:crypto";
import z from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/schemastery/lib/index.mjs";
import { installModelSelection } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js";
import { createUserMessage } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js";
import { SessionId } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js";

export const name = "dradar-minimal-headless-runner";
export const inject = [
  "agentDefaultModel",
  "agentPresets",
  "agents",
  "sessions",
];
export const Config = z.object({ task: z.string().required() });

function summarize(events, firstSeq) {
  let started = false;
  let text = "";
  let reason;
  for (const event of events) {
    if (event.seq < firstSeq) continue;
    if (event.type === "turn/start") {
      started = true;
      continue;
    }
    if (!started) continue;
    if (event.type === "assistant/message") {
      const joined = event.data.message.content
        .filter((block) => block.type === "text")
        .map((block) => block.text)
        .join("");
      if (joined !== "") text = joined;
    }
    if (event.type === "turn/end") reason = event.data.reason;
  }
  return { text, reason };
}

async function run(ctx, task, io) {
  await ctx.get("loader")?.await();
  const agents = ctx.get("agents");
  const defaultModel = ctx.get("agentDefaultModel");
  const presets = ctx.get("agentPresets");
  const sessions = ctx.get("sessions");
  if (!agents || !defaultModel || !presets || !sessions) return;

  const selection = defaultModel.currentSelection();
  const preset = await presets.resolve("minimal");
  const { agent } = await agents.create({
    sessionId: SessionId(`session-${randomUUID()}`),
    meta: { cwd: process.cwd(), agentPreset: preset.id },
    agentOptions: {
      provider: selection.provider,
      model: selection.model,
    },
    setup: async (agentCtx) => {
      installModelSelection(agentCtx, {
        current: selection,
        assembled: undefined,
      });
      await presets.mount(agentCtx, preset.id);
    },
  });

  await agent.whenIdle();
  const firstSeq = agent.session.seq;
  agent.followup(createUserMessage({
    content: [{ type: "text", text: task }],
    source: { kind: "user" },
  }));
  await agent.whenIdle();
  await sessions.flush(agent.session);
  const outcome = summarize(agent.session.events, firstSeq);
  io.stdout.write(`${outcome.text}\n`);
  if (outcome.reason?.kind === "error") {
    io.stderr.write(`dsh: ${outcome.reason.error.code}: ${outcome.reason.error.message}\n`);
  }
  io.exit(outcome.reason?.kind === "completed" ? 0 : 1);
}

export function apply(ctx, config) {
  const exit = ctx.get("appExit");
  if (!exit) {
    throw new Error("minimal-headless-runner: launcher did not provide appExit");
  }
  const io = { stdout: process.stdout, stderr: process.stderr, exit };
  run(ctx, config.task, io).catch((error) => {
    io.stderr.write(`dsh: ${error instanceof Error ? error.message : String(error)}\n`);
    io.exit(1);
  });
}
"""


def _install_command() -> str:
    x64_sha = NODE_SHA256["x64"]
    arm64_sha = NODE_SHA256["arm64"]
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'DSH minimal requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends "
        "  ca-certificates curl g++ inotify-tools make python3 tar xz-utils; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl gcc-c++ inotify-tools make python3 tar xz; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl gcc-c++ inotify-tools make python3 tar xz; "
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
        "for binary in node npm npx corepack; do "
        '  ln -sfn "${node_root}/bin/${binary}" "/usr/local/bin/${binary}"; '
        "done; "
        "mkdir -p /opt/dsh-runtime; "
        f"npm install --global --prefix /opt/dsh-runtime '@deepseek-ai/dsh@{DSH_VERSION}'; "
        "ln -sfn /opt/dsh-runtime/bin/dsh /usr/local/bin/dsh; "
        'rm -f "${node_archive}"; '
        f"test \"$(node --version)\" = 'v{NODE_VERSION}'; "
        f"test \"$(dsh --version)\" = '{DSH_VERSION}'"
    )


class DshMinimal(BaseInstalledAgent):
    """Run pinned DSH headlessly with its fixed-prompt, two-tool composition."""

    SUPPORTS_ATIF = False
    _REMOTE_HOME = PurePosixPath("/logs/agent/dsh-home")
    _REMOTE_CONFIG_DIR = PurePosixPath("/tmp/dsh-config")
    _REMOTE_PATCH = _REMOTE_CONFIG_DIR / "headless-minimal.patch.yml"
    _REMOTE_RUNNER = _REMOTE_CONFIG_DIR / "minimal-headless-runner.mjs"
    _REMOTE_SECRET_ROOT = PurePosixPath("/tmp/dsh-secrets")
    _STREAM_FILE = "dsh-headless.txt"

    @staticmethod
    def name() -> str:
        return "dsh-minimal"

    def __init__(
        self,
        *args: Any,
        api_key_file: str,
        reasoning_effort: str = "high",
        model_name: str | None = None,
        version: str | None = DSH_VERSION,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        key_file = Path(api_key_file)
        try:
            key_stat = key_file.stat()
            key_bytes = key_file.read_bytes()
        except OSError as exc:
            raise ValueError("DeepSeek API key file is missing or unreadable") from exc
        if not stat.S_ISREG(key_stat.st_mode) or not key_bytes.strip():
            raise ValueError("DeepSeek API key file must be a non-empty regular file")
        if os.name != "nt" and stat.S_IMODE(key_stat.st_mode) & 0o077:
            raise ValueError(
                "DeepSeek API key file permissions must be 0600 or stricter"
            )

        assignment_model = model_name or "dsh-deepseek-v4-flash"
        for prefix in ("deepseek/", "deepseek-official/"):
            if assignment_model.startswith(prefix):
                assignment_model = assignment_model.removeprefix(prefix)
                break
        if assignment_model not in SUPPORTED_MODELS:
            raise ValueError(
                "DSH model must use an isolated dsh-deepseek-v4-flash/pro id"
            )
        runtime_model = RUNTIME_MODELS[assignment_model]
        if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError("DSH reasoning_effort must be off, high, or max")
        resolved_version = version or DSH_VERSION
        if resolved_version != DSH_VERSION:
            raise ValueError(f"DSH adapter requires exact version {DSH_VERSION}")

        extra_env = dict(extra_env or {})
        reserved_env = {
            "DEEPSEEK_BASE_URL",
            "DEEPSEEK_SEARCH_BASE_URL",
            "NODE_OPTIONS",
        }
        forbidden = sorted(
            name
            for name in extra_env
            if (
                name.upper().startswith("DSH_")
                or name.upper() in reserved_env
                or any(
                    marker in name.upper()
                    for marker in ("KEY", "PASSWORD", "SECRET", "TOKEN")
                )
            )
        )
        if forbidden:
            raise ValueError(
                "DSH reserved settings and credentials must use explicit adapter "
                "arguments and api_key_file, not agent extra_env: "
                + ", ".join(forbidden)
            )

        self._api_key_file = key_file
        self._reasoning_effort = reasoning_effort
        self._assignment_model = assignment_model
        self._dsh_model = runtime_model
        run_secret_dir = self._REMOTE_SECRET_ROOT / uuid.uuid4().hex
        self._remote_secret_dir = run_secret_dir
        self._remote_api_key = run_secret_dir / "deepseek-api-key"
        self._remote_credentials = run_secret_dir / ".credentials.yaml"
        super().__init__(
            *args,
            model_name=runtime_model,
            version=resolved_version,
            extra_env=extra_env,
            **kwargs,
        )

    def get_version_command(self) -> str:
        return "dsh --version"

    def install_spec(self) -> AgentInstallSpec:
        return AgentInstallSpec(
            agent_name=self.name(),
            version=DSH_VERSION,
            steps=[
                InstallStep(
                    user="root",
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    run=_install_command(),
                )
            ],
            verification_command=(
                f"test \"$(node --version)\" = 'v{NODE_VERSION}' && "
                f"test \"$(dsh --version)\" = '{DSH_VERSION}'"
            ),
            cache_key=(
                f"dradar-dsh-minimal-{DSH_VERSION}-node-{NODE_VERSION}-patch-v2"
            ),
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=["api.deepseek.com"])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        remote_home = self._REMOTE_HOME.as_posix()
        remote_config_dir = self._REMOTE_CONFIG_DIR.as_posix()
        remote_patch = self._REMOTE_PATCH.as_posix()
        remote_runner = self._REMOTE_RUNNER.as_posix()
        remote_secret_dir = self._remote_secret_dir.as_posix()
        remote_api_key = self._remote_api_key.as_posix()
        remote_credentials = self._remote_credentials.as_posix()
        stream = f"{remote_home}/{self._STREAM_FILE}"

        env = self.build_process_env(
            {
                "DSH_HOME": remote_home,
                "DSH_CWD": "/app",
                "DSH_PERMISSION_MODE": "danger-full-access",
                "DSH_MODEL": self._dsh_model,
                "DSH_REASONING_EFFORT": self._reasoning_effort,
                "DSH_TELEMETRY_MODE": "DISABLED",
                "DSH_TOOLS_MODE": "native",
                "DSH_CREDENTIALS_FILE": remote_credentials,
                "NODE_USE_ENV_PROXY": "1",
            }
        )
        runtime_dirs = (remote_home, remote_config_dir, remote_secret_dir)
        setup_command = "mkdir -p " + " ".join(
            shlex.quote(path) for path in runtime_dirs
        )
        if environment.default_user is not None:
            owner = shlex.quote(str(environment.default_user))
            setup_command += (
                " && chown "
                + owner
                + " "
                + " ".join(shlex.quote(path) for path in runtime_dirs)
            )
        setup_command += " && chmod 700 " + " ".join(
            shlex.quote(path) for path in runtime_dirs
        )
        # Pier bind-mounts /logs at runtime, so Dockerfile ownership does not
        # survive. Create and hand off all runtime directories after mounts.
        await self.exec_as_root(environment, command=setup_command, env=env)
        local_patch = self.logs_dir / "dsh-minimal.patch.yml"
        local_runner = self.logs_dir / "dsh-minimal-headless-runner.mjs"
        local_patch.parent.mkdir(parents=True, exist_ok=True)
        local_patch.write_text(_MINIMAL_PATCH, encoding="utf-8")
        local_runner.write_text(_MINIMAL_HEADLESS_RUNNER, encoding="utf-8")
        await environment.upload_file(self._api_key_file, remote_api_key)
        await environment.upload_file(local_patch, remote_patch)
        await environment.upload_file(local_runner, remote_runner)

        chmod_command = (
            f"chmod 600 {shlex.quote(remote_api_key)} "
            f"&& chmod 644 {shlex.quote(remote_patch)} "
            f"{shlex.quote(remote_runner)}"
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {owner} {shlex.quote(remote_api_key)} "
                    f"{shlex.quote(remote_patch)} {shlex.quote(remote_runner)} "
                    f"&& {chmod_command}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(environment, command=chmod_command, env=env)

        python_program = (
            "import json,pathlib,sys;"
            "key=pathlib.Path(sys.argv[1]).read_text().strip();"
            "assert key and not any(c.isspace() for c in key);"
            "pathlib.Path(sys.argv[2]).write_text("
            "json.dumps({'DEEPSEEK_API_KEY':key})+'\\n')"
        )
        # Convert the uploaded raw key to DSH's 0600 credential document. DSH
        # reads that document into its credential service without ever placing
        # the key in its process environment. An inotify guard unlinks it on
        # the first open, before a model can receive and invoke any tool.
        command = (
            "set -euo pipefail; "
            "credential_guard_pid=''; "
            "cleanup_dsh_credentials() { "
            f"rm -f {shlex.quote(remote_api_key)} {shlex.quote(remote_credentials)}; "
            'if [ -n "${credential_guard_pid}" ]; then '
            'kill "${credential_guard_pid}" 2>/dev/null || true; fi; }; '
            "trap cleanup_dsh_credentials EXIT HUP INT TERM; "
            f"python3 -c {shlex.quote(python_program)} "
            f"{shlex.quote(remote_api_key)} {shlex.quote(remote_credentials)}; "
            f"chmod 600 {shlex.quote(remote_credentials)}; "
            f"rm -f {shlex.quote(remote_api_key)}; "
            f"(inotifywait --quiet --event open --format '' "
            f"{shlex.quote(remote_credentials)} >/dev/null 2>&1 "
            f"&& rm -f {shlex.quote(remote_credentials)}) & "
            "credential_guard_pid=$!; "
            "cd /app; "
            f"dsh --profile headless --patch {shlex.quote(remote_patch)} "
            f"{shlex.quote(instruction)} 2>&1 </dev/null | tee {shlex.quote(stream)}"
        )
        await self.exec_as_agent(
            environment,
            command=command,
            env=env,
            cwd="/app",
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # DSH rc.6 does not expose a stable ATIF event stream. Keep this false
        # instead of manufacturing incomplete token/cost data from stdout.
        del context


__all__ = ["DshMinimal"]
