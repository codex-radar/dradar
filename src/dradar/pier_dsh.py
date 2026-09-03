"""Portable Pier adapter for DeepSeek Harness' full-container minimal agent.

The adapter uses only the published Pier 0.3.0 extension interface and the
published ``@deepseek-ai/dsh`` package. Runtime model egress is restricted to
DeepSeek's API, while the task's Docker container remains the
filesystem/process isolation boundary.  The ``minimal`` persona keeps its
small root shell/editor surface while inheriting DSH's local delegation,
workflow, filesystem, background-job, goal, and todo facilities.
"""

from __future__ import annotations

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
try:
    from _dradar_worker_events import emit_worker_registered
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered

try:
    from _dradar_pier_runtime_safety import RuntimeSafety
except ModuleNotFoundError as exc:  # Local source/test import before materialization.
    if exc.name != "_dradar_pier_runtime_safety":
        raise
    from dradar.pier_runtime_safety import RuntimeSafety

DSH_VERSION = "0.1.1-rc.2"
NODE_VERSION = "22.23.2"
NODE_SHA256 = {
    "x64": "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
    "arm64": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
}
SUPPORTED_MODELS = frozenset(
    {
        "dsh-deepseek-v4-flash",
        "dsh-deepseek-v4-pro",
        "dsh-deepseek-v4-flash-vision-exp",
    }
)
RUNTIME_MODELS = {
    "dsh-deepseek-v4-flash": "deepseek-v4-flash",
    "dsh-deepseek-v4-pro": "deepseek-v4-pro",
    "dsh-deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp",
}
SUPPORTED_REASONING_EFFORTS = frozenset({"off", "high", "max"})
_ARTIFACT_ID_RE = re.compile(r"[0-9a-f]{32}")

_MINIMAL_PATCH = """\
# Run DSH's shipped `minimal` agent preset through a one-shot headless runner.
# Pier's disposable Docker container is the security boundary, so the agent
# keeps the base profile's filesystem, background-job, compaction, delegation,
# workflow, goal, and todo tools.  Only duplicate shell/editor rows, ambient
# skills, and network-backed Web tools stay disabled.  DSH_PERMISSION_MODE is
# separately pinned to danger-full-access/never-ask below.
- id: tool-bash
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-str-replace-editor
  disabled: true
- id: tool-web
  disabled: true
- id: web-search-deepseek
  disabled: true
- id: web
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
import { readFileSync, unlinkSync, writeFileSync } from "node:fs";
import z from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/schemastery/lib/index.mjs";
import { installModelSelection } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent/lib/index.js";
import { createUserMessage } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js";
import { SessionId } from "/opt/dsh-runtime/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-session/lib/index.js";

export const name = "dradar-minimal-headless-runner";
export const inject = [
  "agentDefaultModel",
  "agentPresets",
  "agents",
  "attachments",
  "sessions",
];
export const Config = z.object({ task: z.string().required() });

function summarize(events, firstSeq) {
  let started = false;
  let text = "";
  let reason;
  const usageByStep = new Map();
  function usageTimestamp(event) {
    const value = event.timestamp ?? event.createdAt ?? event.time ??
      event.data?.timestamp ?? event.data?.createdAt;
    if (value === undefined || value === null) return null;
    const instant = value instanceof Date ? value : new Date(value);
    return Number.isNaN(instant.getTime()) ? null : instant.toISOString();
  }
  for (const event of events) {
    if (event.seq < firstSeq) continue;
    if (event.type === "turn/start") {
      started = true;
      continue;
    }
    if (!started) continue;
    let usage;
    if (event.type === "assistant/chunk" && event.data.chunk.type === "usage") {
      usage = event.data.chunk.usage;
    } else if (event.type === "assistant/message") {
      usage = event.data.usage;
    }
    if (usage !== undefined) {
      // A finalized assistant message repeats the usage chunk for the same
      // request. Last-wins by (turn, step), matching DSH token-meter's fold.
      usageByStep.set(`${event.data.turn}:${event.data.step}`, {
        usage,
        occurredAt: usageTimestamp(event),
      });
    }
    if (event.type === "assistant/message") {
      const joined = event.data.message.content
        .filter((block) => block.type === "text")
        .map((block) => block.text)
        .join("");
      if (joined !== "") text = joined;
    }
    if (event.type === "turn/end") reason = event.data.reason;
  }
  const totals = {
    uncachedInputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    outputTokens: 0,
  };
  const requests = [];
  for (const item of usageByStep.values()) {
    const usage = item.usage;
    totals.uncachedInputTokens += usage.inputTokens;
    totals.cacheReadTokens += usage.cacheReadTokens ?? 0;
    totals.cacheWriteTokens += usage.cacheWriteTokens ?? 0;
    totals.outputTokens += usage.outputTokens;
    requests.push({
      occurredAt: item.occurredAt,
      uncachedInputTokens: usage.inputTokens,
      cacheReadTokens: usage.cacheReadTokens ?? 0,
      cacheWriteTokens: usage.cacheWriteTokens ?? 0,
      outputTokens: usage.outputTokens,
    });
  }
  return {
    text,
    reason,
    usage: { ...totals, requestCount: usageByStep.size, requests },
  };
}

async function run(ctx, task, io) {
  await ctx.get("loader")?.await();
  try {
    unlinkSync(process.env.DSH_CREDENTIALS_FILE);
  } catch (error) {
    if (!(error instanceof Error && error.code === "ENOENT")) throw error;
  }
  const agents = ctx.get("agents");
  const attachments = ctx.get("attachments");
  const defaultModel = ctx.get("agentDefaultModel");
  const presets = ctx.get("agentPresets");
  const sessions = ctx.get("sessions");
  if (!agents || !defaultModel || !presets || !sessions) return;

  const selection = defaultModel.currentSelection();
  const visionInput = selection.model === "deepseek-v4-flash-vision-exp" &&
    String(process.env.DRADAR_TASK_ID ?? "").startsWith("pompeii-adjacency-rp-");
  let imageRef = null;
  if (visionInput) {
    if (!attachments) {
      throw new Error("Vision Exp requires DSH's durable attachment service");
    }
    let imageBytes;
    try {
      imageBytes = readFileSync("/app/question.png");
    } catch (error) {
      throw new Error(`Vision Exp requires readable /app/question.png: ${error instanceof Error ? error.message : String(error)}`);
    }
    imageRef = await attachments.saveImage({
      data: imageBytes,
      mediaType: "image/png",
      name: "question.png",
    });
  }
  const preset = await presets.resolve("minimal");
  const agentOptions = {
    provider: selection.provider,
    model: selection.model,
  };
  const setup = async (agentCtx) => {
    installModelSelection(agentCtx, {
      current: selection,
      assembled: undefined,
    });
    await presets.mount(agentCtx, preset.id);
  };
  const { agent } = await agents.create({
      sessionId: SessionId(`session-${randomUUID()}`),
      meta: { cwd: process.cwd(), agentPreset: preset.id },
      agentOptions,
      setup,
    });
  writeFileSync(process.env.DSH_SESSION_ID_FILE, String(agent.session.id) + "\\n", {
    encoding: "utf8",
    mode: 0o600,
  });

  await agent.whenIdle();
  // Every run is a fresh session and receives the benchmark instruction once.
  agent.followup(createUserMessage({
    content: [
      { type: "text", text: task },
      ...(imageRef ? [{ type: "image", attachment: imageRef }] : []),
    ],
    source: { kind: "user" },
  }));
  await agent.whenIdle();
  await sessions.flush(agent.session);
  // Fold this fresh session's full event stream into the usage ledger.
  const outcome = summarize(agent.session.events, 0);
  const terminalKind = outcome.reason?.kind;
  writeFileSync(process.env.DSH_OUTCOME_FILE, JSON.stringify({
    schema: "dradar-dsh-outcome-v1",
    assignmentId: process.env.DRADAR_ASSIGNMENT_ID ?? null,
    artifactRunId: process.env.DRADAR_ARTIFACT_RUN_ID ?? null,
    taskId: process.env.DRADAR_TASK_ID ?? null,
    assignmentModel: process.env.DRADAR_ASSIGNMENT_MODEL ?? null,
    reasoningEffort: process.env.DSH_REASONING_EFFORT ?? null,
    resumed: false,
    visionInputAttached: imageRef !== null,
    visionInput: imageRef === null ? null : {
      attachmentId: String(imageRef.attachmentId),
      mediaType: imageRef.mediaType,
      bytes: imageRef.bytes,
      width: imageRef.width,
      height: imageRef.height,
      name: imageRef.name ?? null,
    },
    terminalKind: terminalKind ?? null,
    requestCount: outcome.usage.requestCount,
    agentCompleted: terminalKind === "completed",
    errorCode: terminalKind === "error"
      ? (outcome.reason?.error?.code ?? null)
      : null,
  }), { encoding: "utf8", mode: 0o600 });
  if (outcome.usage.requestCount > 0) {
    writeFileSync(process.env.DSH_USAGE_FILE, JSON.stringify({
      schema: "dsh-provider-usage-v2",
      model: selection.model,
      ...outcome.usage,
    }), { encoding: "utf8", mode: 0o600 });
  }
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
        "  ca-certificates curl g++ make python3 tar xz-utils; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl gcc-c++ make python3 tar xz; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl gcc-c++ make python3 tar xz; "
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
        "npm install --fetch-retries=5 --fetch-retry-factor=2 "
        "  --fetch-retry-mintimeout=20000 --fetch-retry-maxtimeout=120000 "
        f"  --global --prefix /opt/dsh-runtime '@deepseek-ai/dsh@{DSH_VERSION}'; "
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
    _USAGE_FILE = "dsh-usage.json"
    _OUTCOME_FILE = "dsh-outcome.json"

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
        artifact_assignment_id: str | None = None,
        artifact_run_id: str | None = None,
        artifact_task_id: str | None = None,
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
                "DSH model must use an enabled isolated dsh-* model id"
            )
        runtime_model = RUNTIME_MODELS[assignment_model]
        if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError("DSH reasoning_effort must be off, high, or max")
        for label, value in (
            ("assignment", artifact_assignment_id),
            ("artifact run", artifact_run_id),
        ):
            if value is not None and _ARTIFACT_ID_RE.fullmatch(value) is None:
                raise ValueError(f"DSH {label} id must be 32 lowercase hex characters")
        if artifact_task_id is not None and (
            not artifact_task_id
            or len(artifact_task_id) > 200
            or re.fullmatch(r"[A-Za-z0-9._-]+", artifact_task_id) is None
        ):
            raise ValueError("DSH artifact task id is invalid")
        resolved_version = version or DSH_VERSION
        if resolved_version != DSH_VERSION:
            raise ValueError(f"DSH adapter requires exact version {DSH_VERSION}")

        extra_env = dict(extra_env or {})
        reserved_env = {
            "DEEPSEEK_BASE_URL",
            "DEEPSEEK_SEARCH_BASE_URL",
            "NODE_OPTIONS",
            "NODE_USE_ENV_PROXY",
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
        self._artifact_assignment_id = artifact_assignment_id
        self._artifact_run_id = artifact_run_id
        self._artifact_task_id = artifact_task_id
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
                f"dradar-dsh-minimal-{DSH_VERSION}-node-{NODE_VERSION}-patch-v5"
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
        emit_worker_registered(runtime="pier", context="agent", profile="dsh")
        del context
        remote_home = self._REMOTE_HOME.as_posix()
        remote_config_dir = self._REMOTE_CONFIG_DIR.as_posix()
        remote_patch = self._REMOTE_PATCH.as_posix()
        remote_runner = self._REMOTE_RUNNER.as_posix()
        remote_secret_dir = self._remote_secret_dir.as_posix()
        remote_api_key = self._remote_api_key.as_posix()
        remote_credentials = self._remote_credentials.as_posix()
        stream = f"{remote_home}/{self._STREAM_FILE}"
        usage_file = f"{remote_home}/{self._USAGE_FILE}"
        outcome_file = f"{remote_home}/{self._OUTCOME_FILE}"
        session_id_file = "/logs/agent/dsh-session-id"

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
                "DSH_USAGE_FILE": usage_file,
                "DSH_OUTCOME_FILE": outcome_file,
                "DSH_SESSION_ID_FILE": session_id_file,
                "DRADAR_ASSIGNMENT_ID": self._artifact_assignment_id or "",
                "DRADAR_ARTIFACT_RUN_ID": self._artifact_run_id or "",
                "DRADAR_TASK_ID": self._artifact_task_id or "",
                "DRADAR_ASSIGNMENT_MODEL": self._assignment_model,
                # Pier routes allowlisted task traffic through HTTP(S)_PROXY.
                # Node 22 fetch only honors those variables when this is enabled.
                "NODE_USE_ENV_PROXY": "1",
            }
        )
        runtime_safety = RuntimeSafety(self.logs_dir)

        async def root_maintenance(command: str) -> None:
            await self.exec_as_root(
                environment, command=command, env=env,
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
        await root_maintenance(setup_command)
        try:
            if environment.default_user is not None:
                await root_maintenance(
                    f"chown -R {owner} {shlex.quote(remote_home)} "
                    f"&& chmod 700 {shlex.quote(remote_home)}"
                )
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
                await root_maintenance(
                    f"chown {owner} {shlex.quote(remote_api_key)} "
                    f"{shlex.quote(remote_patch)} {shlex.quote(remote_runner)} "
                    f"&& {chmod_command}"
                )
            else:
                await self.exec_as_agent(
                    environment, command=chmod_command, env=env,
                )

            python_program = (
                "import json,pathlib,sys;"
                "key=pathlib.Path(sys.argv[1]).read_text().strip();"
                "assert key and not any(c.isspace() for c in key);"
                "pathlib.Path(sys.argv[2]).write_text("
                "json.dumps({'DEEPSEEK_API_KEY':key})+'\\n')"
            )
            # Convert the raw key to DSH's 0600 credential document. DSH reads
            # it without placing the key in its environment, then the pinned
            # runner unlinks it before creating or resuming the agent.
            command = (
                "set -euo pipefail; "
                "cleanup_dsh_credentials() { "
                f"rm -f {shlex.quote(remote_api_key)} "
                f"{shlex.quote(remote_credentials)}; "
                "}; "
                "trap cleanup_dsh_credentials EXIT HUP INT TERM; "
                f"python3 -c {shlex.quote(python_program)} "
                f"{shlex.quote(remote_api_key)} {shlex.quote(remote_credentials)}; "
                f"chmod 600 {shlex.quote(remote_credentials)}; "
                f"rm -f {shlex.quote(remote_api_key)}; "
                "cd /app; "
                f"dsh --profile headless --patch {shlex.quote(remote_patch)} "
                f"{shlex.quote(instruction)} 2>&1 </dev/null | "
                f"tee {shlex.quote(stream)}"
            )
            await self.exec_as_agent(
                environment,
                command=command,
                env=env,
                cwd="/app",
            )
        finally:
            # Preserve the post-run ownership guarantee introduced after
            # root-run DSH sessions made Pier's host traversal fail.
            await runtime_safety.return_runtime_tree_to_host_owner(
                environment,
                remote_home,
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        # DSH rc.6 does not expose a stable ATIF event stream. Keep this false
        # instead of manufacturing incomplete token/cost data from stdout.
        del context


__all__ = ["DshMinimal"]
