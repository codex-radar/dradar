"""Grok Build integration is subscription OAuth with native concurrency."""

from __future__ import annotations

import ast
import json
import math
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dradar.providers as providers
import dradar.runner as runner
from dradar.providers import (
    GROK_AGENT,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    GROK_MODEL,
    GROK_PROVIDER,
    grok_auth_error,
    grok_auth_path,
    grok_subscription_session,
    parse_grok_cli_version,
)
from dradar.runner import RunnerError


@pytest.fixture(autouse=True)
def isolate_grok_probe_transport(monkeypatch):
    """These tests cover result handling; Docker transport is tested separately."""
    def process(credential, root, env):
        native = root / "native-home"
        native.mkdir(exist_ok=True)
        env = dict(env, HOME=str(native), GROK_AUTH_PATH=str(credential.resolve()))
        for key in ("GROK_HOME", "GROK_AUTH", "XAI_API_KEY", "GROK_CODE_XAI_API_KEY"):
            env.pop(key, None)
        return providers.subprocess.run(
            ["/usr/bin/grok", "models"], capture_output=True, text=True,
            timeout=30, check=False, env=env,
        )
    monkeypatch.setattr(providers, "_grok_probe_process", process)


def _oauth(token: str = "access", refresh: str = "refresh") -> dict:
    return {
        "https://auth.x.ai::client": {
            "auth_mode": "oauth",
            "key": token,
            "refresh_token": refresh,
        }
    }


def test_grok_upgrade_does_not_advertise_old_server_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(providers, "grok_cli_path", lambda _env=None: Path("/grok"))
    monkeypatch.setattr(providers, "grok_auth_error", lambda *_args, **_kwargs: None)
    capabilities = providers.advertised_capabilities({})
    assert providers.GROK_CAPABILITY in capabilities
    assert providers.GROK_47_CAPABILITY in capabilities
    assert providers.GROK_LEGACY_CAPABILITY not in capabilities


def _write_auth(path: Path, payload: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or _oauth()), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _assignment(**overrides) -> dict:
    value = {
        "assignment_id": "a1",
        "task_id": "task-1",
        "agent": GROK_AGENT,
        "provider": GROK_PROVIDER,
        "model": GROK_MODEL,
        "effort": "high",
        "agent_version": GROK_CLI_VERSION,
        "est_minutes": 5,
    }
    value.update(overrides)
    return value


def test_official_grok_version_banner_is_parsed():
    assert parse_grok_cli_version(
        f"grok {GROK_CLI_VERSION} (release)\n"
    ) == GROK_CLI_VERSION
    assert parse_grok_cli_version("unexpected") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode repair")
def test_grok_cli_path_repairs_exact_0600_managed_runtime(
    tmp_path: Path,
) -> None:
    home = tmp_path / "dradar"
    managed = providers.managed_grok_cli_path(home)
    managed.parent.mkdir(parents=True)
    managed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed.chmod(0o600)

    discovered = providers.grok_cli_path({"DRADAR_HOME": str(home)})

    assert discovered == str(managed)
    assert managed.stat().st_mode & 0o777 == 0o700


def test_oauth_validator_rejects_api_key_shaped_auth(tmp_path: Path):
    path = _write_auth(
        tmp_path / "auth.json",
        {"xai": {"auth_mode": "api_key", "key": "secret"}},
    )
    assert "not a refreshable subscription OAuth" in (grok_auth_error(path) or "")


def test_subscription_session_uses_canonical_native_lock_store(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("DRADAR_HOME", str(home))
    canonical = _write_auth(grok_auth_path(), _oauth("old", "old-refresh"))

    with grok_subscription_session(tmp_path / "work") as shared:
        assert shared == canonical
        if os.name != "nt":
            assert shared.parent.stat().st_mode & 0o777 == 0o700

    assert canonical.is_file()


def test_pier_command_uses_private_adapter_without_key_in_argv(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    monkeypatch.setenv(GROK_API_KEY_ENV, "must-not-leak")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = _write_auth(tmp_path / "providers" / "grok" / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")

    cmd = runner.build_pier_command(
        _assignment(), tasks, tmp_path / "jobs", "job", home,
        provider_auth_path=auth,
        provider_cli_path=cli,
    )

    assert runner.GROK_AGENT_IMPORT_PATH in cmd
    assert f"auth_json_file={auth}" in cmd
    assert "shared_oauth=true" in cmd
    assert runner.SHARED_OAUTH_ENV_IMPORT_PATH in cmd
    assert f"grok_cli_file={cli}" in cmd
    assert f"version={GROK_CLI_VERSION}" in cmd
    assert "must-not-leak" not in " ".join(cmd)
    assert (home / runner.GROK_AGENT_MODULE_FILENAME).is_file()

    env = runner._pier_process_env(_assignment(), grok_module_dir=home)
    assert GROK_API_KEY_ENV not in env
    assert env["PYTHONPATH"] == str(home)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_all_grok_46_efforts_build_the_pinned_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effort: str,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = _write_auth(tmp_path / "providers" / "grok" / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    cmd = runner.build_pier_command(
        _assignment(effort=effort), tasks, tmp_path / "jobs", "job", tmp_path,
        provider_auth_path=auth, provider_cli_path=cli,
    )
    assert f"reasoning_effort={effort}" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4.6"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"provider": "xai-api"}, "explicitly use provider"),
        ({"model": "grok-other"}, "unsupported Grok subscription model"),
        ({"effort": "max"}, "effort must be low, medium, high, or xhigh"),
    ],
)
def test_unverified_grok_assignments_fail_before_paid_run(
    tmp_path: Path, monkeypatch, overrides: dict, message: str
):
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    assignment = _assignment(**overrides)
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    auth = _write_auth(tmp_path / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    with pytest.raises(RunnerError, match=message):
        runner.build_pier_command(
            assignment, tasks, tmp_path / "jobs", "job", tmp_path,
            provider_auth_path=auth,
            provider_cli_path=cli,
        )


def test_grok_47_assignment_builds_a_47_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = _write_auth(tmp_path / "providers" / "grok" / "auth.json")
    cli = tmp_path / "grok"
    cli.write_text("binary", encoding="utf-8")
    cmd = runner.build_pier_command(
        _assignment(model="grok-4.7"), tasks, tmp_path / "jobs", "job", tmp_path,
        provider_auth_path=auth, provider_cli_path=cli,
    )
    assert cmd[cmd.index("--model") + 1] == "grok-4.7"
    assert providers.GROK_MODELS == {"grok-4.6", "grok-4.7"}
    assert set(providers.GROK_MODEL_RUNTIME_TUPLES) == providers.GROK_MODELS
    assert len(set(providers.GROK_MODEL_RUNTIME_TUPLES.values())) == 2
    with pytest.raises(RunnerError, match="unsupported Grok subscription model"):
        runner._validate_grok_assignment(_assignment(model="grok-4.8"))


def test_pier_adapter_model_set_matches_the_cli() -> None:
    """pier_grok.py runs inside Pier and keeps its own copy of the set."""
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    module = ast.parse(source)
    node = next(
        node for node in module.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "GROK_MODELS" for t in node.targets)
    )
    assert eval(compile(ast.Expression(node.value), "pier_grok.py", "eval")) == (
        providers.GROK_MODELS
    )


def test_grok_assignment_version_is_only_a_hint() -> None:
    runner._validate_grok_assignment(_assignment(agent_version="1.0.3"))
    runner._validate_grok_assignment(_assignment(agent_version="9.9.9"))



def test_grok_adapter_primes_dynamic_46_model_catalog() -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    assert '_REMOTE_HOME = _REMOTE_USER_HOME / ".grok"' in source
    assert '"HOME": remote_user_home' in source
    assert '"GROK_HOME": remote_home' not in source
    assert 'f"models_output=$({shlex.quote(remote_cli)} models 2>&1); "' in source
    assert "EPIPE" in source
    assert "grep -Fq" in source
    assert "grok-4.6" in source
    assert "DRADAR_GROK_PREFLIGHT_FAILURE=%s" in source
    assert "preflight_kind=auth" in source
    assert "preflight_kind=network" in source
    assert "preflight_kind=unknown" in source
    assert "DRADAR_GROK_PREFLIGHT_FAILURE=catalog" in source
    assert 'f"&& printf \'%s\\\\n\' \\\"$models_output\\\" "' not in source
    assert '"grok.com"' in source
    assert '"code.grok.com"' in source
    assert "GROK_TELEMETRY_ENABLED" in source
    assert "grok-1.0.3-linux-${grok_arch}" not in source
    assert "grok-{GROK_CLI_VERSION}-linux-${{grok_arch}}" in source
    assert "GROK_LINUX_SHA256" in source
    assert "sha256sum --check --strict" in source
    assert "await environment.upload_file(self._grok_cli_file" not in source


@pytest.mark.parametrize(
    ("model", "output", "returncode", "expected"),
    [
        ("grok-4.6", "* grok-4.6 (default)\n", 0, None),
        ("grok-4.6", "* grok-4.5 (default)\n", 0, "catalog"),
        ("grok-4.7", "* grok-4.7 (default)\n  grok-4.6\n", 0, None),
        # The binaries' bundled fallback catalog lists 4.6 and 4.5 only, so a
        # slot that never fetched the live catalog must not pass for 4.7.
        ("grok-4.7", "* grok-4.6 (default)\n  grok-4.5\n", 0, "catalog"),
        ("grok-4.6", "Not authenticated; refresh=TOPSECRET\n", 1, "auth"),
        ("grok-4.6", "settings fetch failed for https://token.example\n", 1, "network"),
        ("grok-4.6", "opaque failure TOPSECRET\n", 1, "unknown"),
    ],
)
def test_grok_model_preflight_emits_only_bounded_failure_category(
    tmp_path: Path, model: str, output: str, returncode: int,
    expected: str | None,
) -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_grok_model_preflight_command"
    )
    namespace = {"shlex": shlex, "GROK_MODELS": frozenset({"grok-4.6", "grok-4.7"})}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "pier_grok.py", "exec"),
         namespace)
    fake = tmp_path / "grok fake"
    fake.write_text(
        "#!/bin/sh\n"
        + f"printf '%s' {shlex.quote(output)} >&2\n"
        + f"exit {returncode}\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)

    command = namespace["_grok_model_preflight_command"](str(fake), model)
    proc = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False,
    )

    if expected is None:
        assert proc.returncode == 0
        assert proc.stdout == ""
    else:
        assert proc.returncode == 78
        assert proc.stdout == f"DRADAR_GROK_PREFLIGHT_FAILURE={expected}\n"
    assert proc.stderr == ""
    assert "TOPSECRET" not in proc.stdout + proc.stderr
    assert "token.example" not in proc.stdout + proc.stderr


def test_grok_prompt_stays_headless_in_one_single_option_argv(
    tmp_path: Path,
) -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_grok_prompt_command"
    )
    namespace = {"shlex": shlex}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "pier_grok.py", "exec"),
         namespace)
    fake = tmp_path / "grok fake"
    fake.write_text(
        "#!/bin/sh\n"
        "single_count=0\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in --single=*) single_count=$((single_count + 1));; esac\n"
        "done\n"
        "[ \"$single_count\" -eq 1 ] || exit 91\n"
        "[ ! -t 0 ] || exit 92\n"
        "printf '%s\\0' \"$@\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    stream = tmp_path / "stream file"
    instruction = "- first bullet\nquote: 'single' and \"double\""
    flags = ["--model", "grok-4.6", "--reasoning-effort", "xhigh"]

    command = namespace["_grok_prompt_command"](
        str(fake), flags, instruction, str(stream),
    )
    proc = subprocess.run(
        ["bash", "-o", "pipefail", "-c", command],
        capture_output=True, check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout.split(b"\0")[:-1] == [
        part.encode() for part in [*flags, f"--single={instruction}"]
    ]
    assert stream.read_bytes() == proc.stdout

    resumed = namespace["_grok_prompt_command"](
        str(fake), flags, "continue", str(stream),
        session_id="01a01e4f-040a-71e3-a6c4-fdf6083ae20a", append=True,
    )
    resumed_proc = subprocess.run(
        ["bash", "-o", "pipefail", "-c", resumed],
        capture_output=True, check=False,
    )
    assert resumed_proc.returncode == 0
    assert b"--resume=01a01e4f-040a-71e3-a6c4-fdf6083ae20a" in resumed_proc.stdout
    assert stream.read_bytes().endswith(resumed_proc.stdout)


def test_grok_usage_keeps_cached_input_as_prompt_subset() -> None:
    source = Path(providers.__file__).with_name("pier_grok.py").read_text()
    module = ast.parse(source)
    helper = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_grok_usage_facts"
    )
    namespace = {"datetime": datetime, "timezone": timezone, "math": math}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "pier_grok.py", "exec"),
         namespace)
    first_usage = {
        "input_tokens": 300,
        "cache_read_input_tokens": 250,
        "cache_creation_input_tokens": 60,
        "output_tokens": 30,
    }
    second_usage = {
        "input_tokens": 200,
        "cache_read_input_tokens": 150,
        "cache_creation_input_tokens": 40,
        "output_tokens": 20,
    }
    official_usage = {
        "input_tokens": 500,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 1_050,
    }
    response_events = [
        {"type": "assistant", "message": {"id": "msg-1", "usage": first_usage}},
        {"type": "assistant", "message": {"id": "msg-2", "usage": second_usage}},
    ]
    terminal = {
        "type": "result",
        "subtype": "success",
        "num_turns": 2,
        "total_cost_usd": 0.00142052,
        "usage": official_usage,
    }
    facts = namespace["_grok_usage_facts"]([*response_events, terminal], "grok-4.6")
    assert facts["complete"] is True
    assert facts["n_input_tokens"] == 1_000
    assert facts["n_cache_tokens"] == 400
    assert facts["n_output_tokens"] == 50
    assert facts["cache_creation_tokens"] == 100
    assert facts["request_count"] == 2
    assert facts["request_usage_complete"] is True
    assert facts["timed_usage_complete"] is False
    assert facts["token_usage_events"] == [
        {
            "n_input_tokens": 610,
            "n_cache_tokens": 250,
            "n_output_tokens": 30,
        },
        {
            "n_input_tokens": 390,
            "n_cache_tokens": 150,
            "n_output_tokens": 20,
        },
    ]
    assert facts["subscription_reported_cost_usd"] == pytest.approx(0.00142052)

    replayed = namespace["_grok_usage_facts"]([
        response_events[0], response_events[0], response_events[1], terminal,
    ], "grok-4.6")
    assert replayed["complete"] is True
    assert replayed["request_count"] == 2
    assert replayed["n_input_tokens"] == 1_000

    incomplete = namespace["_grok_usage_facts"]([
        *response_events,
        {**terminal, "usage_is_incomplete": True},
    ], "grok-4.6")
    assert incomplete["complete"] is False

    mismatched = namespace["_grok_usage_facts"]([
        *response_events,
        {**terminal, "usage": {**official_usage, "total_tokens": 1_051}},
    ], "grok-4.6")
    assert mismatched["complete"] is False

    missing_response = namespace["_grok_usage_facts"]([
        response_events[0], terminal,
    ], "grok-4.6")
    assert missing_response["complete"] is False
    assert missing_response["request_usage_observed"] is True
    assert missing_response["request_count"] == 1
    assert missing_response["n_input_tokens"] == 610
    assert missing_response["n_cache_tokens"] == 250
    assert missing_response["n_output_tokens"] == 30

    missing_terminal = namespace["_grok_usage_facts"](response_events, "grok-4.6")
    assert missing_terminal["complete"] is False
    assert missing_terminal["request_usage_complete"] is False
    assert missing_terminal["request_usage_observed"] is True
    assert missing_terminal["usage_evidence_tier"] == "observed_unreconciled"
    assert missing_terminal["request_count"] == 2
    assert missing_terminal["n_input_tokens"] == 1_000
    assert missing_terminal["n_cache_tokens"] == 400
    assert missing_terminal["n_output_tokens"] == 50
    assert len(missing_terminal["token_usage_events"]) == 2


def test_grok_live_probe_uses_native_private_home(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        native = Path(kwargs["env"]["GROK_AUTH_PATH"])
        assert native == auth.resolve()
        assert not (Path(kwargs["env"]["HOME"]) / ".grok" / "auth.json").exists()
        assert native.is_file()
        assert native.stat().st_mode & 0o777 == 0o600
        return providers.subprocess.CompletedProcess(
            cmd, 0,
            "You are logged in with grok.com.\n  * grok-4.6 (default)\n", "",
        )

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    assert providers.grok_live_error("/usr/bin/grok", auth) is None
    assert seen["cmd"] == ["/usr/bin/grok", "models"]
    assert "GROK_HOME" not in seen["env"]
    assert GROK_API_KEY_ENV not in seen["env"]


def test_grok_live_probe_native_rotation_persists_in_canonical_store(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json", _oauth("old", "old-refresh"))

    def fake_run(cmd, **kwargs):
        native = Path(kwargs["env"]["GROK_AUTH_PATH"])
        _write_auth(native, _oauth("new", "new-refresh"))
        return providers.subprocess.CompletedProcess(
            cmd, 0, "* grok-4.6 (default)\n", "",
        )

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    assert providers.grok_live_error("/usr/bin/grok", auth) is None
    assert next(iter(json.loads(auth.read_text()).values()))["refresh_token"] == (
        "new-refresh"
    )


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("outcome", ["success", "auth_failure", "timeout", "cancel"])
def test_grok_probe_never_restores_a_stale_snapshot(
    tmp_path, monkeypatch, explicit, outcome,
):
    auth = _write_auth(tmp_path / "grok" / "auth.json", _oauth("A", "RT-A"))
    monkeypatch.setattr(providers, "grok_auth_path", lambda *_: auth)

    def concurrent_writer(cmd, **kwargs):
        bound = Path(kwargs["env"]["GROK_AUTH_PATH"])
        assert bound.samefile(auth)
        # Model a committed native rotation while the catalog request is in flight.
        _write_auth(auth, _oauth("B", "RT-B"))
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30)
        if outcome == "cancel":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(
            cmd, 0, "not authenticated" if outcome == "auth_failure" else "grok-4.6", "",
        )

    monkeypatch.setattr(providers.subprocess, "run", concurrent_writer)
    args = ("/inert/grok", auth if explicit else None)
    if outcome == "cancel":
        with pytest.raises(KeyboardInterrupt):
            providers.grok_live_error(*args)
    else:
        issue = providers.grok_live_error(*args)
        assert (issue is None) == (outcome == "success")
    assert json.loads(auth.read_text()) == _oauth("B", "RT-B")


def test_grok_probe_cannot_inherit_another_inline_identity(tmp_path, monkeypatch):
    auth = _write_auth(tmp_path / "auth.json")
    for key in ("GROK_AUTH", "GROK_AUTH_PATH", "XAI_API_KEY", "GROK_CODE_XAI_API_KEY"):
        monkeypatch.setenv(key, "foreign-identity")

    def run(cmd, **kwargs):
        env = kwargs["env"]
        assert env["GROK_AUTH_PATH"] == str(auth.resolve())
        assert all(key not in env for key in ("GROK_AUTH", "XAI_API_KEY", "GROK_CODE_XAI_API_KEY"))
        return subprocess.CompletedProcess(cmd, 0, "grok-4.6", "")

    monkeypatch.setattr(providers.subprocess, "run", run)
    assert providers.grok_live_error("/inert/grok", auth) is None


def test_grok_probe_rejects_invalid_native_result_without_restoring_old_auth(tmp_path, monkeypatch):
    auth = _write_auth(tmp_path / "auth.json")

    def run(cmd, **kwargs):
        auth.write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "grok-4.6", "")

    monkeypatch.setattr(providers.subprocess, "run", run)
    assert "invalid OAuth" in providers.grok_live_error("/inert/grok", auth)
    assert auth.read_text() == "{}"


def test_grok_rechecks_stale_native_banner_only_after_same_identity_rotation(tmp_path, monkeypatch):
    payload = _oauth("old", "old-refresh")
    next(iter(payload.values()))["user_id"] = "inert-user"
    auth = _write_auth(tmp_path / "auth.json", payload)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            next(iter(payload.values())).update(key="new", refresh_token="new-refresh")
            _write_auth(auth, payload)
            return subprocess.CompletedProcess(cmd, 0, "You are not authenticated. grok-4.6", "")
        return subprocess.CompletedProcess(cmd, 0, "You are logged in. grok-4.6", "")

    monkeypatch.setattr(providers.subprocess, "run", run)
    assert providers.grok_live_error("/inert/grok", auth) is None
    assert len(calls) == 2


def test_grok_identity_change_is_not_accepted_or_rolled_back(tmp_path, monkeypatch):
    payload = _oauth()
    next(iter(payload.values()))["user_id"] = "inert-user-A"
    auth = _write_auth(tmp_path / "auth.json", payload)

    def run(cmd, **kwargs):
        next(iter(payload.values()))["user_id"] = "inert-user-B"
        _write_auth(auth, payload)
        return subprocess.CompletedProcess(cmd, 0, "grok-4.6", "")

    monkeypatch.setattr(providers.subprocess, "run", run)
    assert "identity changed" in providers.grok_live_error("/inert/grok", auth)
    assert next(iter(json.loads(auth.read_text()).values()))["user_id"] == "inert-user-B"


def test_provider_subprocess_env_adds_os_proxy_without_overriding_shell(
    monkeypatch,
) -> None:
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        providers.urllib.request,
        "getproxies",
        lambda: {
            "http": "http://127.0.0.1:18080",
            "https": "http://127.0.0.1:18080",
        },
    )

    env = providers.provider_subprocess_env()

    assert env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:18080"

    monkeypatch.setenv("HTTPS_PROXY", "http://explicit.example:8080")
    assert providers.provider_subprocess_env()["HTTPS_PROXY"] == (
        "http://explicit.example:8080"
    )


def test_dradar_http_proxy_is_the_authoritative_cross_platform_interface(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://ambient.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.example:8080")
    monkeypatch.setenv("DRADAR_HTTP_PROXY", "http://configured.example:43128")
    monkeypatch.setenv("DRADAR_NO_PROXY", "localhost,127.0.0.1")

    env = providers.provider_subprocess_env()

    assert env["HTTP_PROXY"] == "http://configured.example:43128"
    assert env["HTTPS_PROXY"] == "http://configured.example:43128"
    assert env["NO_PROXY"] == "localhost,127.0.0.1"


def test_grok_live_probe_rejects_unauthenticated_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    monkeypatch.setattr(
        providers.subprocess, "run",
        lambda cmd, **kwargs: providers.subprocess.CompletedProcess(
            cmd, 0,
            "You are not authenticated.\n  * grok-4.5 (default)\n", "",
        ),
    )

    issue = providers.grok_live_error("/usr/bin/grok", auth) or ""
    assert "not authenticated" in issue
    assert "dradar provider setup grok" in issue


def test_grok_live_probe_rejects_offline_builtin_catalog_as_network_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    auth = _write_auth(tmp_path / "auth.json")
    monkeypatch.setattr(
        providers.subprocess, "run",
        lambda cmd, **kwargs: providers.subprocess.CompletedProcess(
            cmd,
            0,
            "You are logged in with grok.com.\n"
            "Settings fetch failed max_attempts=3\n"
            "Default model: grok-4.5\nAvailable models:\n  * grok-4.5\n",
            "",
        ),
    )

    issue = providers.grok_live_error("/usr/bin/grok", auth) or ""
    assert "network/proxy" in issue
    assert "cannot access" not in issue


def test_native_command_unsets_inherited_container_auth_and_pins_paths(tmp_path):
    source=Path(providers.__file__).with_name("pier_grok.py").read_text()
    helper=next(node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name=="_grok_auth_command")
    namespace={"shlex":shlex}
    exec(compile(ast.Module(body=[helper],type_ignores=[]),"pier_grok.py","exec"),namespace)
    command=namespace["_grok_auth_command"](
        'test -z "${GROK_AUTH+x}" && test -z "${XAI_API_KEY+x}" && test -z "${GROK_CODE_XAI_API_KEY+x}" && test -z "${GROK_HOME+x}" && printf "%s\\n%s\\n" "$HOME" "$GROK_AUTH_PATH"',
        "/tmp/canonical home", "/tmp/canonical home/.grok/auth.json")
    env=dict(os.environ,HOME="/foreign",GROK_AUTH_PATH="/foreign/auth.json",GROK_AUTH="INERT-FOREIGN",XAI_API_KEY="INERT-KEY",GROK_CODE_XAI_API_KEY="INERT-ALIAS",GROK_HOME="/foreign/.grok")
    proc=subprocess.run(["bash","-c",command],env=env,capture_output=True,text=True,check=False)
    assert proc.returncode==0
    assert proc.stdout.splitlines()==["/tmp/canonical home","/tmp/canonical home/.grok/auth.json"]
