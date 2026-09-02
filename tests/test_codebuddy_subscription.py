from __future__ import annotations

import ast
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import codebuddy_provider, pier_sitecustomize, providers
from dradar.codebuddy_provider import (
    CODEBUDDY_AGENT,
    CODEBUDDY_CAPABILITY,
    CODEBUDDY_CLI_VERSION,
    CODEBUDDY_MODEL,
    CODEBUDDY_NATIVE_EFFORTS,
    CODEBUDDY_PROVIDER,
    CODEBUDDY_RUN_CONFIG_VERSION,
    CODEBUDDY_RUNTIME_PROFILE,
    CODEBUDDY_SUPPORTED_EFFORTS,
    codebuddy_subscription_session,
    credential_status,
    import_host_login,
    managed_auth_dir,
    managed_codebuddy_home,
)
from dradar.runloop import _subscription_trial_usage
from dradar.runner import (
    RunnerError,
    _pier_process_env,
    _validate_codebuddy_assignment,
)


def _write_login(
    source_home: Path, source_auth: Path, *, token: str = "access",
) -> None:
    storage = source_home / "local_storage"
    storage.mkdir(parents=True, mode=0o700)
    source_auth.mkdir(parents=True, mode=0o700)
    opaque = storage / "entry_login.info"
    opaque.write_text('{"opaque":true}', encoding="utf-8")
    auth = source_auth / "account.info"
    auth.write_text(json.dumps({
        "auth": {
            "accessToken": token,
            "refreshToken": f"refresh-{token}",
            "lastRefreshTime": 1,
            "expiresAt": 2,
            "refreshExpiresAt": 3,
        },
        "account": {"id": "test-account"},
    }), encoding="utf-8")
    if os.name != "nt":
        os.chmod(storage, 0o700)
        os.chmod(source_auth, 0o700)
        os.chmod(opaque, 0o600)
        os.chmod(auth, 0o600)


def _valid_assignment(**updates: object) -> dict[str, object]:
    assignment: dict[str, object] = {
        "agent": CODEBUDDY_AGENT,
        "provider": CODEBUDDY_PROVIDER,
        "model": CODEBUDDY_MODEL,
        "effort": "max",
        "agent_version": CODEBUDDY_CLI_VERSION,
    }
    assignment.update(updates)
    return assignment


def _usage_function():
    source = (Path(__file__).parents[1] / "src" / "dradar" / "pier_codebuddy.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {"_nonnegative_int", "_usage_values", "_codebuddy_usage_facts"}
    module = ast.Module(
        body=[
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in wanted
        ],
        type_ignores=[],
    )
    namespace = {"SUPPORTED_MODEL": CODEBUDDY_MODEL}
    exec(compile(module, str(source), "exec"), namespace)  # noqa: S102
    return namespace["_codebuddy_usage_facts"]


def _trajectory_function():
    source = (Path(__file__).parents[1] / "src" / "dradar" / "pier_codebuddy.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    module = ast.Module(
        body=[
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_codebuddy_trajectory_payload"
        ],
        type_ignores=[],
    )
    namespace = {
        "CODEBUDDY_CLI_VERSION": CODEBUDDY_CLI_VERSION,
        "SUPPORTED_MODEL": CODEBUDDY_MODEL,
        "uuid": uuid,
    }
    exec(compile(module, str(source), "exec"), namespace)  # noqa: S102
    return namespace["_codebuddy_trajectory_payload"]


def test_codebuddy_public_contract_is_pinned_to_three_efforts() -> None:
    assert CODEBUDDY_AGENT == "codebuddy"
    assert CODEBUDDY_PROVIDER == "codebuddy-subscription"
    assert CODEBUDDY_MODEL == "hy4-preview"
    assert CODEBUDDY_CLI_VERSION == "2.137.1"
    assert CODEBUDDY_SUPPORTED_EFFORTS == {"low", "high", "max"}
    assert CODEBUDDY_NATIVE_EFFORTS == (
        "minimal", "low", "medium", "high", "xhigh", "max",
    )
    assert CODEBUDDY_CAPABILITY == CODEBUDDY_RUN_CONFIG_VERSION
    assert CODEBUDDY_CAPABILITY == (
        "codebuddy-hy4-preview-subscription-oauth-three-effort-low-high-max-concurrent-v4"
    )
    assert CODEBUDDY_RUNTIME_PROFILE == (
        "pier-codebuddy-hy4-preview-isolated-copy-concurrent-v2"
    )


@pytest.mark.parametrize("version", ["2.137.1", "2.143.0", "2.999.0"])
def test_compatible_newer_host_cli_can_supply_login_without_changing_runtime(
    version: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codebuddy_provider, "codebuddy_version", lambda _executable: version,
    )

    assert codebuddy_provider.codebuddy_host_cli_status("/usr/bin/codebuddy") == (
        None, version,
    )
    assert codebuddy_provider.CODEBUDDY_CONTAINER_IMAGE == (
        "dradar-codebuddy:2.137.1"
    )


@pytest.mark.parametrize(
    "executable,version,expected",
    [
        (None, None, ("missing", None)),
        ("/usr/bin/codebuddy", None, ("unrecognized", None)),
        ("/usr/bin/codebuddy", "2.136.9", ("incompatible", "2.136.9")),
        ("/usr/bin/codebuddy", "1.999.0", ("incompatible", "1.999.0")),
        ("/usr/bin/codebuddy", "3.0.0", ("incompatible", "3.0.0")),
    ],
)
def test_host_cli_status_fails_closed_for_missing_unknown_or_incompatible_versions(
    executable: str | None,
    version: str | None,
    expected: tuple[str | None, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codebuddy_provider, "codebuddy_version", lambda _executable: version,
    )

    assert codebuddy_provider.codebuddy_host_cli_status(executable) == expected


def test_login_import_is_private_atomic_and_refreshable(tmp_path: Path) -> None:
    source_home = tmp_path / "host-codebuddy"
    source_auth = tmp_path / "host-auth"
    dradar_home = tmp_path / "dradar-home"
    _write_login(source_home, source_auth)

    target = import_host_login(
        source_home=source_home, source_auth=source_auth, home=dradar_home,
    )
    assert target == managed_codebuddy_home(dradar_home)
    assert credential_status(dradar_home)[0] is True
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0
        assert all(path.stat().st_mode & 0o077 == 0 for path in target.rglob("*.info"))

    work = tmp_path / "work"
    work.mkdir()
    with codebuddy_subscription_session(work, home=dradar_home) as run_login:
        assert run_login != target
        refreshed = run_login / "auth" / "account.info"
        payload = json.loads(refreshed.read_text(encoding="utf-8"))
        payload["auth"]["accessToken"] = "rotated"
        payload["auth"]["lastRefreshTime"] = 10
        payload["auth"]["expiresAt"] = 20
        payload["auth"]["refreshExpiresAt"] = 30
        refreshed.write_text(json.dumps(payload), encoding="utf-8")
        if os.name != "nt":
            os.chmod(refreshed, 0o600)
    saved = json.loads((managed_auth_dir(dradar_home) / "account.info").read_text())
    assert saved["auth"]["accessToken"] == "rotated"
    assert not (work / "codebuddy-login").exists()


def test_concurrent_sessions_keep_the_newest_oauth_refresh(tmp_path: Path) -> None:
    source_home = tmp_path / "host-codebuddy"
    source_auth = tmp_path / "host-auth"
    dradar_home = tmp_path / "dradar-home"
    _write_login(source_home, source_auth)
    import_host_login(
        source_home=source_home, source_auth=source_auth, home=dradar_home,
    )
    first_work = tmp_path / "first-work"
    second_work = tmp_path / "second-work"
    first_work.mkdir()
    second_work.mkdir()

    with codebuddy_subscription_session(
        first_work, home=dradar_home,
    ) as first_login:
        with codebuddy_subscription_session(
            second_work, home=dradar_home,
        ) as second_login:
            first = first_login / "auth" / "account.info"
            second = second_login / "auth" / "account.info"
            first_payload = json.loads(first.read_text(encoding="utf-8"))
            second_payload = json.loads(second.read_text(encoding="utf-8"))
            first_payload["auth"].update({
                "accessToken": "first-refresh", "lastRefreshTime": 10,
            })
            second_payload["auth"].update({
                "accessToken": "second-refresh", "lastRefreshTime": 20,
            })
            first.write_text(json.dumps(first_payload), encoding="utf-8")
            second.write_text(json.dumps(second_payload), encoding="utf-8")
            if os.name != "nt":
                os.chmod(first, 0o600)
                os.chmod(second, 0o600)

    saved = json.loads(
        (managed_auth_dir(dradar_home) / "account.info").read_text()
    )
    assert saved["auth"]["accessToken"] == "second-refresh"
    assert not (first_work / "codebuddy-login").exists()
    assert not (second_work / "codebuddy-login").exists()


def test_concurrent_sessions_in_one_worker_directory_are_isolated(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "host-codebuddy"
    source_auth = tmp_path / "host-auth"
    dradar_home = tmp_path / "dradar-home"
    _write_login(source_home, source_auth)
    import_host_login(
        source_home=source_home, source_auth=source_auth, home=dradar_home,
    )
    shared_work = tmp_path / "shared-work"
    shared_work.mkdir()

    with codebuddy_subscription_session(
        shared_work, home=dradar_home,
    ) as first_login:
        with codebuddy_subscription_session(
            shared_work, home=dradar_home,
        ) as second_login:
            assert first_login != second_login
            assert first_login.parent != second_login.parent
            assert first_login.parent.parent == shared_work
            assert second_login.parent.parent == shared_work
            assert (first_login / "auth" / "account.info").is_file()
            assert (second_login / "auth" / "account.info").is_file()

    assert not list(shared_work.glob(".codebuddy-session-*"))


def test_login_import_rejects_symlinked_records(tmp_path: Path) -> None:
    source_home = tmp_path / "host-codebuddy"
    source_auth = tmp_path / "host-auth"
    _write_login(source_home, source_auth)
    original = source_home / "local_storage" / "entry_login.info"
    original.unlink()
    original.symlink_to(source_auth / "account.info")
    with pytest.raises(ValueError, match="unsafe CodeBuddy storage record"):
        import_host_login(
            source_home=source_home, source_auth=source_auth,
            home=tmp_path / "dradar-home",
        )


def test_login_import_recovers_interrupted_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home = tmp_path / "host-codebuddy"
    source_auth = tmp_path / "host-auth"
    dradar_home = tmp_path / "dradar-home"
    _write_login(source_home, source_auth, token="old")
    target = import_host_login(
        source_home=source_home, source_auth=source_auth, home=dradar_home,
    )
    previous = target.with_name(f".{target.name}.previous")
    os.replace(target, previous)
    auth_source = source_auth / "account.info"
    refreshed = json.loads(auth_source.read_text(encoding="utf-8"))
    refreshed["auth"]["accessToken"] = "new"
    auth_source.write_text(json.dumps(refreshed), encoding="utf-8")

    real_replace = codebuddy_provider.os.replace

    def fail_staged_promotion(source, destination):
        source_path = Path(source)
        if (
            Path(destination) == target
            and source_path.name == target.name
            and source_path.parent.name.startswith(".codebuddy-import-")
        ):
            raise OSError("simulated staged promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(codebuddy_provider.os, "replace", fail_staged_promotion)
    with pytest.raises(OSError, match="simulated staged promotion failure"):
        import_host_login(
            source_home=source_home, source_auth=source_auth, home=dradar_home,
        )
    saved = json.loads((target / "auth" / "account.info").read_text())
    assert saved["auth"]["accessToken"] == "old"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "codebuddy-local"),
        ("model", "hy4"),
        ("effort", "xhigh"),
        ("agent_version", "2.137.0"),
    ],
)
def test_assignment_boundary_fails_closed(field: str, value: str) -> None:
    _validate_codebuddy_assignment(_valid_assignment())
    with pytest.raises(RunnerError):
        _validate_codebuddy_assignment(_valid_assignment(**{field: value}))


@pytest.mark.parametrize("effort", ["max", "high", "low"])
def test_assignment_boundary_accepts_each_public_codebuddy_effort(effort: str) -> None:
    _validate_codebuddy_assignment(_valid_assignment(effort=effort))


def test_process_environment_scrubs_every_ambient_codebuddy_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEBUDDY_API_KEY", "secret")
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", "/host/profile")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = _pier_process_env(
        _valid_assignment(), codebuddy_module_dir=Path("/adapter"),
    )
    assert all(
        not name.startswith("CODEBUDDY_")
        for name in env
        if name != "DRADAR_CODEBUDDY_SOURCE_IMAGE"
    )
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["DRADAR_CODEBUDDY_SOURCE_IMAGE"] == "dradar-codebuddy:2.137.1"


def test_capability_is_advertised_only_after_every_local_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers, "codebuddy_executable", lambda _env: "/bin/codebuddy",
    )
    monkeypatch.setattr(
        codebuddy_provider, "codebuddy_version", lambda _path: "2.143.0",
    )
    monkeypatch.setattr(
        providers, "codebuddy_credential_status", lambda: (True, "ready"),
    )
    monkeypatch.setattr(
        providers, "codebuddy_runtime_image_error", lambda: None,
    )
    assert CODEBUDDY_CAPABILITY in providers.advertised_capabilities({})
    monkeypatch.setattr(
        providers, "codebuddy_runtime_image_error", lambda: "image missing",
    )
    assert CODEBUDDY_CAPABILITY not in providers.advertised_capabilities({})


def test_managed_codebuddy_home_can_follow_one_fleet_request(monkeypatch, tmp_path):
    requested = tmp_path / "request-codebuddy-home"
    monkeypatch.setenv("DRADAR_CODEBUDDY_MANAGED_HOME", str(requested))
    assert codebuddy_provider.managed_codebuddy_home() == requested


def test_runtime_image_gate_executes_the_labeled_binary_once_per_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "1" * 64
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{
                    "Id": image_id,
                    "Os": "linux",
                    "Config": {"Labels": {
                        codebuddy_provider.CODEBUDDY_IMAGE_LABEL:
                            CODEBUDDY_CLI_VERSION,
                    }},
                }]),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout=CODEBUDDY_CLI_VERSION + "\n", stderr="",
        )

    codebuddy_provider._VALIDATED_CODEBUDDY_IMAGE_IDS.clear()
    monkeypatch.setattr(codebuddy_provider.subprocess, "run", fake_run)
    assert codebuddy_provider.codebuddy_runtime_image_error("docker") is None
    assert codebuddy_provider.codebuddy_runtime_image_error("docker") is None
    assert sum(command[1] == "run" for command in calls) == 1


def test_usage_reconciles_request_ledger_and_terminal_aggregate() -> None:
    facts = _usage_function()([
        {
            "type": "assistant",
            "message": {
                "id": "m1", "model": CODEBUDDY_MODEL,
                "usage": {
                    "input_tokens": 125,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 30,
                },
            },
        },
        {
            "type": "result", "subtype": "success", "is_error": False,
            "num_turns": 1, "total_tokens": 155,
            "usage": {
                "input_tokens": 125,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 30,
            },
        },
    ])
    assert facts["complete"] is True
    assert facts["request_count"] == 1
    assert facts["n_input_tokens"] == 125
    assert facts["n_cache_tokens"] == 20
    assert facts["n_output_tokens"] == 30
    assert facts["provider_actual_cost_observed"] is False
    assert facts["cost_semantics"] == "server-priced-api-equivalent"


def test_complete_codebuddy_turn_materializes_auditable_atif_trajectory() -> None:
    events = [
        {
            "type": "assistant",
            "message": {
                "id": "m1", "model": CODEBUDDY_MODEL,
                "usage": {
                    "input_tokens": 125,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 30,
                },
            },
        },
        {
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "session-real-request", "result": "Patch completed.",
            "usage": {
                "input_tokens": 125,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 30,
            },
        },
    ]
    usage = _usage_function()(events)

    trajectory = _trajectory_function()(
        events, "Implement the task.", "max", usage,
    )

    assert trajectory is not None
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["session_id"] == "session-real-request"
    assert trajectory["steps"][1]["llm_call_count"] == 1
    assert trajectory["final_metrics"] == {
        "total_prompt_tokens": 125,
        "total_cached_tokens": 20,
        "total_completion_tokens": 30,
        "total_cost_usd": None,
        "total_steps": 2,
        "extra": {
            "billing_basis": "subscription",
            "cost_not_reported": True,
            "usage_complete": True,
            "terminal_status": "success",
            "terminal_evidence": "unique-provider-result-v1",
        },
    }

    assert trajectory == _trajectory_function()(
        events, "Implement the task.", "max", usage,
    )


def test_codebuddy_trajectory_fails_closed_without_provider_session_id() -> None:
    events = [{
        "type": "result", "subtype": "success", "is_error": False,
        "result": "Patch completed.",
    }]
    usage = {
        "complete": True, "request_count": 1,
        "n_input_tokens": 1, "n_cache_tokens": 0, "n_output_tokens": 1,
    }

    assert _trajectory_function()(events, "task", "max", usage) is None


def test_incomplete_codebuddy_turn_never_fabricates_trajectory() -> None:
    events = [{
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "session-no-request", "result": "",
        "usage": {
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
        },
    }]
    usage = _usage_function()(events)

    assert usage["request_count"] == 0
    assert _trajectory_function()(events, "task", "max", usage) is None


def test_usage_accepts_real_stream_fragments_and_ignores_num_turns() -> None:
    zero = {
        "input_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0,
    }
    nullable_zero = {
        "input_tokens": 0, "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None, "output_tokens": 0,
    }
    request = {
        "input_tokens": 8152, "cache_read_input_tokens": 768,
        "cache_creation_input_tokens": 7384, "output_tokens": 70,
    }
    facts = _usage_function()([
        {"type": "assistant", "message": {
            "id": "m1", "model": CODEBUDDY_MODEL, "usage": zero,
        }},
        {"type": "assistant", "message": {
            "id": "m1", "model": CODEBUDDY_MODEL, "usage": zero,
        }},
        {"type": "assistant", "message": {
            "id": "m1", "model": CODEBUDDY_MODEL, "usage": request,
        }},
        {"type": "assistant", "message": {
            "id": "thinking", "model": CODEBUDDY_MODEL,
            "usage": nullable_zero,
        }},
        {"type": "result", "subtype": "success", "is_error": False,
         "num_turns": 4, "usage": request},
    ])
    assert facts["complete"] is True
    assert facts["request_count"] == 1
    assert facts["n_input_tokens"] == 8152
    assert facts["n_cache_tokens"] == 768
    assert facts["n_output_tokens"] == 70


def test_usage_fails_closed_on_conflicting_positive_duplicate() -> None:
    request = {
        "input_tokens": 10, "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3, "output_tokens": 1,
    }
    facts = _usage_function()([
        {"type": "assistant", "message": {
            "id": "m1", "model": CODEBUDDY_MODEL, "usage": request,
        }},
        {"type": "assistant", "message": {
            "id": "m1", "model": CODEBUDDY_MODEL,
            "usage": {**request, "output_tokens": 2},
        }},
        {"type": "result", "subtype": "success", "usage": request},
    ])
    assert facts["complete"] is False
    assert facts["n_input_tokens"] == 0


def test_usage_fails_closed_on_mismatch_or_wrong_model() -> None:
    usage = {
        "input_tokens": 1, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 1,
    }
    for model, terminal_usage in [
        ("not-hy4", usage),
        (CODEBUDDY_MODEL, {**usage, "output_tokens": 2}),
    ]:
        facts = _usage_function()([
            {"type": "assistant", "message": {
                "id": "m1", "model": model, "usage": usage,
            }},
            {"type": "result", "subtype": "success", "num_turns": 1,
             "usage": terminal_usage},
        ])
        assert facts["complete"] is False
        assert facts["n_input_tokens"] == 0
        assert facts["token_usage_events"] == []


def test_normalized_usage_preserves_codebuddy_attestation_fields(
    tmp_path: Path,
) -> None:
    usage = _usage_function()([
        {
            "type": "assistant",
            "message": {
                "id": "m1", "model": CODEBUDDY_MODEL,
                "usage": {
                    "input_tokens": 125,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 30,
                },
            },
        },
        {
            "type": "result", "subtype": "success", "is_error": False,
            "num_turns": 1, "total_tokens": 155,
            "usage": {
                "input_tokens": 125,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 5,
                "output_tokens": 30,
            },
        },
    ])
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "provider-usage.json").write_text(
        json.dumps(usage), encoding="utf-8",
    )
    normalized = _subscription_trial_usage(
        tmp_path, {"codebuddy_cli_version": CODEBUDDY_CLI_VERSION},
    )
    assert normalized is not None
    assert normalized["request_usage_complete"] is True
    assert normalized["request_usage_observed"] is True
    assert normalized["provider_actual_cost_observed"] is False
    assert normalized["cost_semantics"] == "server-priced-api-equivalent"


def test_pier_dockerfile_rewrite_uses_only_reviewed_local_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_command = "echo dynamic-install-must-not-run"
    verify_command = "codebuddy --version | grep -Fx 2.137.1"
    dockerfile = tmp_path / "Dockerfile"
    dynamic_suffix = (
        "USER root\nRUN "
        + json.dumps(["/bin/bash", "-c", install_command])
        + "\n"
    )
    dockerfile.write_text(
        "FROM alpine:3.22\n" + dynamic_suffix, encoding="utf-8",
    )
    environment = SimpleNamespace(
        agent_install_spec=SimpleNamespace(
            agent_name="codebuddy",
            version=CODEBUDDY_CLI_VERSION,
            steps=[SimpleNamespace(user="root", run=install_command)],
            verification_command=verify_command,
        ),
        _agent_build_context_dir=tmp_path,
    )
    monkeypatch.setenv(
        "DRADAR_CODEBUDDY_SOURCE_IMAGE",
        f"dradar-codebuddy:{CODEBUDDY_CLI_VERSION}",
    )
    staged = []
    monkeypatch.setattr(
        pier_sitecustomize, "_materialize_codebuddy_runtime",
        lambda image, build_dir: staged.append((image, build_dir)),
    )
    pier_sitecustomize._rewrite_codebuddy_agent_dockerfile(environment)
    rewritten = dockerfile.read_text(encoding="utf-8")
    assert rewritten.startswith("FROM alpine:3.22\n")
    assert "COPY dradar-codebuddy-runtime/ /opt/codebuddy/runtime/" in rewritten
    assert "COPY --from=" not in rewritten
    assert staged == [(f"dradar-codebuddy:{CODEBUDDY_CLI_VERSION}", tmp_path)]
    assert install_command not in rewritten
    assert verify_command in rewritten


def test_codebuddy_runtime_is_staged_without_builder_pulling_local_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "cp":
            destination = Path(command[-1])
            (destination / "codebuddy").write_bytes(b"binary")
            (destination / "loader").write_bytes(b"loader")
            (destination / "lib").mkdir()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pier_sitecustomize.subprocess, "run", run)
    image = f"dradar-codebuddy:{CODEBUDDY_CLI_VERSION}"

    staged = pier_sitecustomize._materialize_codebuddy_runtime(image, tmp_path)

    assert staged == tmp_path / "dradar-codebuddy-runtime"
    assert calls[0][0:2] == ["docker", "run"]
    assert calls[0][calls[0].index("--pull") + 1] == "never"
    assert image in calls[0]
    assert calls[1][0:2] == ["docker", "cp"]
    assert calls[2][0:3] == ["docker", "rm", "-f"]


def test_adapter_and_pier_bootstrap_keep_credentials_and_tools_narrow() -> None:
    root = Path(__file__).parents[1] / "src" / "dradar"
    adapter = (root / "pier_codebuddy.py").read_text(encoding="utf-8")
    bootstrap = (root / "pier_sitecustomize.py").read_text(encoding="utf-8")
    assert "--model {SUPPORTED_MODEL} --effort {self._reasoning_effort}" in adapter
    assert "--tools Bash,Edit,Read,Write,Glob,Grep" in adapter
    assert "--strict-mcp-config" in adapter
    assert "CODEBUDDY_CONFIG_DIR" in adapter
    assert 'PurePosixPath("/tmp/dradar-codebuddy-config")' in adapter
    assert "cp -R" in adapter
    assert "/logs/agent/sessions/projects" in adapter
    assert "await environment.download_file" in adapter
    assert "COPY dradar-codebuddy-runtime/" in bootstrap
    assert "CodeBuddy source image is missing or version-mismatched" in bootstrap
    runloop = (root / "runloop.py").read_text(encoding="utf-8")
    assert '"subscription_oauth_coordination": "host-monotonic-merge-v2"' in runloop
    assert '"codebuddy_credential_mode": "isolated-run-copy-concurrent-v2"' in runloop
    assert "CodeBuddy HY4 canary assignments require the serial runner" not in runloop
