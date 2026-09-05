import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dradar import (
    cli,
    codebuddy_provider,
    docker_runtime,
    doctor,
    fleet,
    pending,
    provider_config,
    run_plans,
    runloop,
)
from dradar.api_client import ApiClient, ApiError


BATCH_ID = "12345678123456781234567812345678"
RUN_CODE = "run_very_high_entropy_example_123456789"
PLAN_TOKEN = "drp_plan_scoped_secret"


def _envelope(
    status="started",
    *,
    interaction="notify",
    decision_required=False,
    user_message="已在这台设备开始运行。无需操作。",
    agent_action="monitor",
    error_code=None,
    retryable=False,
    choices=None,
    **extra,
):
    return {
        "status": status,
        "interaction": interaction,
        "decision_required": decision_required,
        "user_message": user_message,
        "agent_action": agent_action,
        "error_code": error_code,
        "retryable": retryable,
        "choices": list(choices or []),
        **extra,
    }


def _plan(
    *,
    mode="auto",
    concurrency=None,
    task_count=2,
    refill=False,
    refill_to=None,
    max_tasks=None,
    harness="codex",
    provider=None,
    points_tier="pro-20x",
):
    assignments = []
    for index in range(task_count):
        item = {
            "assignment_id": f"assignment-{index}",
            "task_id": f"task-{index}",
            "model": "gpt-5.4",
            "effort": "high",
        }
        if provider:
            item["provider"] = provider
        assignments.append(item)
    return {
        "schema_version": 1,
        "plan_id": "plan_test_123456",
        "plan_version": 1,
        "batch_id": BATCH_ID,
        "benchmark_id": "deep-swe",
        "harness": harness,
        "points_tier": points_tier,
        "assignments": assignments,
        "concurrency": {"mode": mode, "value": concurrency},
        "refill": {
            "enabled": refill,
            "refill_to": refill_to,
            "max_tasks": max_tasks,
        },
        "locale": "zh-CN",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def _server_response(plan, envelope=None, **extra):
    return {
        "schema_version": 1,
        "plan": plan,
        "state": {"devices": []},
        "envelope": envelope or _envelope(),
        **extra,
    }


def _state(tmp_path, plan):
    path = tmp_path / f"plan-{plan['plan_id']}.json"
    state = {
        "schema_version": 1,
        "credential_kind": "run_plan_v1",
        "server": "https://api.codexradar.com",
        "token": PLAN_TOKEN,
        "run_code_hash": run_plans._run_code_digest(RUN_CODE),
        "plan": plan,
        "plan_id": plan["plan_id"],
        "benchmark": plan["benchmark_id"],
        "batch_id": plan["batch_id"],
        "logical_session_id": "drl_logical_session_123456",
        "identity": {"nickname": "测试用户", "concurrent_limit": 8},
        "limits": {
            "account_concurrency": 8,
            "account_claim_limit": 8,
            "plan_task_limit": max(
                len(plan["assignments"]), int(plan["refill"].get("max_tasks") or 0),
            ),
        },
        "pending_decision": None,
        "pending_local_capacity": None,
        "intent_generation": 0,
        "pending_recheck_generation": None,
        "authorized_concurrency": None,
    }
    run_plans._atomic_json(path, state)
    return path, state


def _args(
    *, concurrency=None, decision_token=None, scope=None, upload_only=False,
    recheck_generation=None, docker_install_token=None,
):
    return SimpleNamespace(
        plan=RUN_CODE,
        server="https://api.codexradar.com",
        concurrency=concurrency,
        decision_token=decision_token,
        scope=scope,
        upload_only=upload_only,
        recheck_generation=recheck_generation,
        docker_install_token=docker_install_token,
        json=True,
    )


def _snapshot(*, available=4, auto_workers=4, account_limit=8):
    facts = {
        "safe_total": available,
        "reserved_by_other_runs": 0,
        "available": available,
        "auto_workers": auto_workers,
        "docker_cpus": 8,
        "docker_memory_gib": 32.0,
        "disk_limit": 8,
        "account_limit": account_limit,
        "held_tasks": 2,
        "automatic_cap": 4,
    }
    facts["digest"] = "capacity-snapshot-1"
    return facts


def _capacity_error(*, requested, available, original_mode):
    if available:
        envelope = _envelope(
            status=("capacity_changed" if original_mode == "auto" else "decision_required"),
            interaction=("notify" if original_mode == "auto" else "confirm"),
            decision_required=original_mode == "fixed",
            user_message="可用数量刚刚发生变化。",
            agent_action=(
                "retry_with_available_concurrency"
                if original_mode == "auto" else "ask_user"
            ),
            error_code="concurrency_capacity_reserved",
            retryable=True,
            choices=(
                [{"id": "lower_concurrency", "label": f"改为 {available} 个"},
                 {"id": "cancel", "label": "取消"}]
                if original_mode == "fixed" else []
            ),
        )
    else:
        envelope = _envelope(
            status="waiting",
            user_message="当前没有空余位置；我会等待后重试。",
            agent_action="wait_and_retry",
            error_code="concurrency_capacity_reserved",
            retryable=True,
        )
    payload = {
        "detail": "safe capacity detail",
        "code": "concurrency_capacity_reserved",
        "requested_concurrency": requested,
        "available_concurrency": available,
        "original_concurrency_mode": original_mode,
        "limiting_scope": "account",
        "account_concurrency": 8,
        "account_concurrency_in_use": 8 - available,
        "plan_concurrency": 8,
        "plan_concurrency_in_use": 8 - available,
        "envelope": envelope,
    }
    return ApiError(
        "unsafe transport message",
        status_code=409,
        code="concurrency_capacity_reserved",
        payload=payload,
    )


def _stale_decision_error(code="decision_invalid_or_state_changed"):
    return ApiError(
        "unsafe stale decision detail",
        status_code=409,
        code=code,
        payload={"detail": "unsafe stale decision detail", "code": code},
    )


class FakeClient:
    def __init__(self, starts=None, progress=None, stops=None):
        self.starts = list(starts or [])
        self.progress_results = list(progress or [])
        self.stop_results = list(stops or [])
        self.start_calls = []
        self.progress_calls = []
        self.stop_calls = []

    def whoami(self):
        return {"concurrent_limit": 8, "claim_limit": 8}

    @staticmethod
    def _next(values):
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def start_run_plan(self, **kwargs):
        self.start_calls.append(kwargs)
        return self._next(self.starts)

    def run_plan_progress(self, plan_id):
        self.progress_calls.append(plan_id)
        return self._next(self.progress_results)

    def stop_run_plan(self, **kwargs):
        self.stop_calls.append(kwargs)
        return self._next(self.stop_results)


def _prepare_run(
    monkeypatch,
    tmp_path,
    *,
    plan,
    client,
    snapshot=None,
    environment_issue=None,
):
    path, state = _state(tmp_path, plan)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        doctor, "plan_environment_issue", lambda _plan: environment_issue,
    )
    if snapshot is not None:
        monkeypatch.setattr(
            run_plans,
            "_capacity_snapshot",
            lambda _client, _plan, _limits=None: snapshot,
        )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: None)
    return path, state


def test_cli_parses_user_intent_run_progress_and_stop_commands(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_run_plan", lambda args: seen.append(("run", args)) or 0)
    monkeypatch.setattr(
        cli, "cmd_progress_plan", lambda args: seen.append(("progress", args)) or 0,
    )
    monkeypatch.setattr(cli, "cmd_stop_plan", lambda args: seen.append(("stop", args)) or 0)

    assert cli.main([
        "run", "--plan", RUN_CODE,
        "--server", "https://api.claudecoderadar.com",
        "--upload-only", "--json",
    ]) == 0
    assert cli.main([
        "progress", "--plan", RUN_CODE,
        "--server", "https://api.claudecoderadar.com", "--json",
    ]) == 0
    assert cli.main([
        "stop", "--plan", RUN_CODE,
        "--server", "https://api.claudecoderadar.com",
        "--scope", "all-devices", "--decision-token", "drd_once", "--json",
    ]) == 0
    assert cli.main([
        "run", "--plan", RUN_CODE,
        "--server", "https://api.claudecoderadar.com",
        "--recheck-generation", "7", "--json",
    ]) == 0
    assert cli.main([
        "run", "--plan", RUN_CODE,
        "--server", "https://api.claudecoderadar.com",
        "--docker-install-token", "drdi_once", "--json",
    ]) == 0

    assert seen[0][1].upload_only is True
    assert seen[0][1].server == "https://api.claudecoderadar.com"
    assert seen[1][1].plan == RUN_CODE
    assert seen[2][1].scope == "all-devices"
    assert seen[2][1].decision_token == "drd_once"
    assert seen[3][1].recheck_generation == 7
    assert seen[4][1].docker_install_token == "drdi_once"


def test_exchange_keeps_run_code_out_of_state_and_uses_private_files(
    tmp_path, monkeypatch,
):
    plan = _plan()
    captured = {}

    class ExchangeClient:
        def __init__(self, server, token):
            captured["server"] = server
            captured["constructor_token"] = token

        def exchange_run_plan(self, **kwargs):
            captured["exchange"] = kwargs
            return {
                "schema_version": 1,
                "plan_access_token": PLAN_TOKEN,
                "access_expires_at": "2099-01-01T00:00:00Z",
                "plan": plan,
                "identity": {"nickname": "测试用户", "concurrent_limit": 8},
                "limits": {"account_concurrency": 8, "account_claim_limit": 8},
                "envelope": _envelope(status="resolved", agent_action="check_environment"),
            }

    monkeypatch.setattr(run_plans, "ApiClient", ExchangeClient)

    path, state = run_plans._exchange(
        RUN_CODE, "https://api.claudecoderadar.com", home=tmp_path,
    )

    assert captured["server"] == "https://api.claudecoderadar.com"
    assert captured["constructor_token"] == ""
    assert captured["exchange"]["run_code"] == RUN_CODE
    assert captured["exchange"]["device_id"].startswith("drv_")
    assert state["token"] == PLAN_TOKEN
    contents = path.read_text()
    assert RUN_CODE not in contents
    assert state["run_code_hash"] in contents
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert run_plans._saved_state(RUN_CODE, home=tmp_path)[0] == path
    assert run_plans.stable_device(tmp_path) == (
        captured["exchange"]["device_id"], captured["exchange"]["device_name"],
    )


def test_stable_device_is_single_identity_under_concurrent_first_use(tmp_path):
    with ThreadPoolExecutor(max_workers=12) as pool:
        identities = list(pool.map(lambda _index: run_plans.stable_device(tmp_path), range(30)))

    assert len(set(identities)) == 1
    device_path = tmp_path / run_plans.PLAN_DIR / run_plans.DEVICE_FILE
    assert json.loads(device_path.read_text())["device_id"] == identities[0][0]
    if os.name != "nt":
        assert device_path.stat().st_mode & 0o777 == 0o600


def test_stable_device_first_use_is_consistent_across_processes(tmp_path):
    source = Path(__file__).parent.parent / "src"
    barrier = tmp_path / "start"
    program = (
        "import sys,time;"
        f"sys.path.insert(0,{str(source)!r});"
        "from pathlib import Path;"
        "from dradar.run_plans import stable_device;"
        f"barrier=Path({str(barrier)!r});"
        "\nwhile not barrier.exists(): time.sleep(0.005)\n"
        f"print(stable_device(Path({str(tmp_path)!r}))[0],flush=True)"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(8)
    ]
    barrier.touch()
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())

    assert len(set(outputs)) == 1
    assert outputs[0].startswith("drv_")


def test_concurrent_first_exchange_mints_one_logical_session(
    tmp_path, monkeypatch,
):
    plan = _plan()
    exchanges = []

    class ExchangeClient:
        def __init__(self, _server, token, **_kwargs):
            self.token = token

        def exchange_run_plan(self, **kwargs):
            exchanges.append(kwargs)
            time.sleep(0.05)
            return {
                "schema_version": 1,
                "plan_access_token": PLAN_TOKEN,
                "access_expires_at": "2099-01-01T00:00:00Z",
                "plan": plan,
                "identity": {"nickname": "测试用户", "concurrent_limit": 8},
                "limits": {"account_concurrency": 8, "plan_task_limit": 2},
                "envelope": _envelope(status="resolved"),
            }

    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(run_plans, "ApiClient", ExchangeClient)
    args = _args()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run_plans._state_and_client(args), range(2)))

    assert len(exchanges) == 1
    assert results[0][2]["logical_session_id"] == results[1][2]["logical_session_id"]
    assert results[0][2]["token"] == PLAN_TOKEN


def test_exchange_http_contract_uses_json_body_without_authorization():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = request.url.query
        captured["authorization"] = request.headers.get("Authorization")
        captured["json"] = json.loads(request.read())
        return httpx.Response(200, json={"schema_version": 1})

    client = ApiClient(
        "https://api.codexradar.com",
        "",
        transport=httpx.MockTransport(handler),
        capabilities=(),
    )
    client.exchange_run_plan(run_code=RUN_CODE, device_id="drv_device")

    assert captured == {
        "method": "POST",
        "path": "/api/v1/run-plans/exchange",
        "query": b"",
        "authorization": None,
        "json": {
            "schema_version": 1,
            "run_code": RUN_CODE,
            "device_id": "drv_device",
        },
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://api.codexradar.com", "https://api.codexradar.com"),
        ("https://api.claudecoderadar.com/", "https://api.claudecoderadar.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ],
)
def test_server_url_accepts_two_public_sites_and_loopback(value, expected):
    assert run_plans.validate_server_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://api.codexradar.com",
        "https://user:pass@api.codexradar.com",
        "https://api.codexradar.com/path",
        "https://api.codexradar.com/?token=secret",
    ],
)
def test_server_url_fails_closed_for_unsafe_values(value):
    with pytest.raises(run_plans.RunPlanClientError) as raised:
        run_plans.validate_server_url(value)
    assert raised.value.code == "server_url_invalid"


def test_user_error_messages_do_not_expose_agent_or_scheduler_terms():
    errors = [
        run_plans.RunPlanClientError(
            "run_code_invalid", "网页复制的运行信息无效，请回网页重新复制。",
        ),
        run_plans.RunPlanClientError(
            "local_capacity_unavailable",
            "这台设备当前没有空余运行位置；请等待其他运行结束后重试。",
        ),
    ]
    forbidden = (
        "batch_id", "Fleet", "provider", "refill", "worker",
        "assignment", "leases", "运行码",
    )
    for error in errors:
        message = run_plans._local_error_response(error)["user_message"]
        assert all(term not in message for term in forbidden)


def _write_credential_state(home, *, index, expires_at, token=None):
    plan_id = f"plan_cleanup_{index:04d}"
    path = run_plans._state_path(plan_id, home)
    run_plans._atomic_json(path, {
        "schema_version": 1,
        "credential_kind": "run_plan_v1",
        "server": "https://api.codexradar.com",
        "token": token or f"drp_cleanup_secret_{index}",
        "access_expires_at": expires_at,
        "run_code_hash": run_plans._run_code_digest(f"run_cleanup_{index:04d}"),
        "plan_id": plan_id,
        "plan": {"expires_at": expires_at},
    })
    return path


def test_expired_state_is_not_reused_and_token_is_scrubbed(tmp_path):
    code = "run_cleanup_0001"
    path = _write_credential_state(
        tmp_path, index=1, expires_at="2000-01-01T00:00:00Z",
        token="drp_expired_secret",
    )

    assert run_plans._saved_state(code, home=tmp_path) is None
    payload = json.loads(path.read_text())
    assert payload["credential_kind"] == "run_plan_expired_summary_v1"
    assert "token" not in payload
    assert "drp_expired_secret" not in path.read_text()


def test_expired_credentials_used_by_active_fleet_are_preserved_but_not_reused(
    tmp_path, monkeypatch,
):
    path = _write_credential_state(
        tmp_path, index=2, expires_at="2000-01-01T00:00:00Z",
        token="drp_active_closing_secret",
    )
    monkeypatch.setattr(
        fleet, "credentials_file_in_use", lambda candidate, **_kwargs: Path(candidate) == path,
    )

    run_plans._cleanup_states(tmp_path)

    assert "drp_active_closing_secret" in path.read_text()
    assert run_plans._saved_state("run_cleanup_0002", home=tmp_path) is None


def test_inactive_plan_state_files_have_bounded_credential_and_audit_counts(tmp_path):
    for index in range(140):
        _write_credential_state(
            tmp_path, index=index, expires_at="2099-01-01T00:00:00Z",
        )

    run_plans._cleanup_states(tmp_path)

    payloads = [
        json.loads(path.read_text())
        for path in (tmp_path / run_plans.PLAN_DIR).glob("plan-*.json")
    ]
    credentials = [
        item for item in payloads if item["credential_kind"] == "run_plan_v1"
    ]
    summaries = [
        item for item in payloads
        if item["credential_kind"] == "run_plan_expired_summary_v1"
    ]
    assert len(credentials) <= run_plans.MAX_CREDENTIAL_STATES
    assert len(summaries) <= run_plans.MAX_AUDIT_SUMMARIES
    assert len(payloads) <= (
        run_plans.MAX_CREDENTIAL_STATES + run_plans.MAX_AUDIT_SUMMARIES
    )
    assert all("token" not in item for item in summaries)


def test_auto_refill_uses_safe_effective_concurrency_not_seed_count(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=2)
    client = FakeClient(starts=[_server_response(
        plan,
        _envelope(agent_action="start_runner"),
    )])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    added = []

    def add_batch(**kwargs):
        added.append(kwargs)
        return {"batch": {"workers": kwargs["workers"]}}

    monkeypatch.setattr(fleet, "add_batch", add_batch)

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "started"
    assert payload["agent_action"] == "monitor"
    assert payload["agent"]["server_status"]["agent_action"] == "start_runner"
    assert client.start_calls[0]["concurrency_mode"] == "fixed"
    assert client.start_calls[0]["concurrency"] == 4
    assert added[0]["workers"] == 4
    assert added[0]["refill"] is True
    assert added[0]["max_tasks"] == 20
    assert added[0]["batch_id"] == BATCH_ID


def test_active_legacy_controller_waits_before_server_admission(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    controller = fleet._initial_state("legacy-controller", None)
    controller.pop("controller_protocol_version")
    controller["status"] = "active"
    other_batch = "87654321876543218765432187654321"
    controller["batches"][other_batch] = {
        "batch_id": other_batch,
        "status": "running",
        "workers": 1,
    }
    fleet._write_state(tmp_path, controller)
    monkeypatch.setattr(fleet, "controller_is_active", lambda _home: True)
    monkeypatch.setattr(
        fleet.os,
        "kill",
        lambda *_args: pytest.fail("existing work must keep running"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "waiting"
    assert payload["error_code"] == "local_runtime_update_pending"
    assert payload["agent_action"] == "recheck_plan"
    assert payload["agent"]["schema_version"] == 1
    assert "Fleet" not in payload["user_message"]
    assert client.start_calls == []


def test_run_reports_preparing_until_local_pool_acknowledges_readiness(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=1)
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=1, auto_workers=1),
    )
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: {
            "batch": {
                "workers": kwargs["workers"],
                "status": "starting",
                "startup_status": "pending",
            },
        },
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "preparing"
    assert payload["interaction"] == "notify"
    assert payload["agent_action"] == "monitor"
    assert payload["agent"]["local_runner"]["status"] == "preparing"
    assert payload["user_message"] == (
        "这台设备正在准备运行环境，稍后会逐个运行这次选择的题目。"
        "题目尚未开始执行。无需操作。"
    )


def test_structured_local_startup_failure_stops_phantom_device_immediately(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    stopped = _server_response(
        plan, _envelope(status="stopped", agent_action="stop_runner"),
    )
    client = FakeClient(starts=[_server_response(plan)], stops=[stopped])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )

    def fail_start(**_kwargs):
        raise fleet.FleetStartupError(
            "task_environment_update_failed",
            "这台设备未能准备题目环境；已有本地文件没有被修改。",
        )

    monkeypatch.setattr(fleet, "add_batch", fail_start)

    assert run_plans.cmd_run_plan(_args()) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "task_environment_update_failed"
    assert payload["agent_action"] == "notify_only"
    assert payload["agent"]["requires_user_action"] is True
    assert "已有本地文件没有被修改" in payload["user_message"]
    assert client.stop_calls == [
        {"plan_id": plan["plan_id"], "scope": "this_device"},
    ]


def test_auto_resource_downgrade_returns_one_top_level_warn_envelope(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=2)
    server = _server_response(plan)
    client = FakeClient(starts=[server])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["interaction"] == "warn"
    assert payload["decision_required"] is False
    assert payload["agent_action"] == "monitor"
    assert payload["agent"]["selected_concurrency"] == 2
    assert payload["agent"]["server_status"]["status"] == "started"
    assert "envelope" not in payload


def test_auto_capacity_reservation_race_retries_lower_and_warns(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[
        _capacity_error(requested=4, available=2, original_mode="auto"),
        _server_response(plan),
    ])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    added = []
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: added.append(kwargs) or {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [call["concurrency"] for call in client.start_calls] == [4, 2]
    assert added[0]["workers"] == 2
    assert payload["interaction"] == "warn"
    assert payload["decision_required"] is False
    assert payload["agent"]["selected_concurrency"] == 2


def test_fixed_capacity_reservation_race_requires_one_use_lower_decision(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=4, task_count=4)
    client = FakeClient(starts=[
        _capacity_error(requested=4, available=2, original_mode="fixed"),
        _server_response(plan),
    ])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    added = []
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: added.append(kwargs) or {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    confirm = json.loads(capsys.readouterr().out)
    token = confirm["decision_token"]
    assert confirm["decision"] == "server_capacity"
    assert confirm["decision_required"] is True
    assert confirm["choices"] == [
        {"id": "use_recommended", "label": "按建议同时运行 2 道"},
        {"id": "cancel", "label": "暂不启动"},
    ]
    assert confirm["agent"]["choice_actions"]["use_recommended"]["args"] == [
        "--concurrency", "2", "--decision-token", token,
    ]
    assert all("arguments" not in choice for choice in confirm["choices"])
    assert added == []

    assert run_plans.cmd_run_plan(
        _args(concurrency=2, decision_token=token),
    ) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["status"] == "started"
    assert [call["concurrency"] for call in client.start_calls] == [4, 2]
    assert added[0]["workers"] == 2


def test_capacity_reservation_with_zero_available_waits_without_phantom_pool(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[
        _capacity_error(requested=4, available=0, original_mode="auto"),
    ])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("waiting cannot start Fleet"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "waiting"
    assert payload["agent_action"] == "recheck_plan"
    assert payload["poll_after_seconds"] == 30
    assert payload["agent"]["next_commands"] == [{
        "id": "recheck_current_plan",
        "mode": "replay_plan_command",
        "command": "run",
        "args": ["--recheck-generation", "2", "--json"],
        "inherit": ["--plan", "--server"],
        "interactive": False,
    }]
    assert payload["agent"]["server_status"]["agent_action"] == "wait_and_retry"
    assert len(client.start_calls) == 1


def test_local_zero_capacity_returns_bounded_machine_recheck_before_server_start(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("zero capacity cannot start"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_action"] == "recheck_plan"
    assert payload["error_code"] == "local_capacity_unavailable"
    assert payload["poll_after_seconds"] == 30
    assert payload["agent"]["next_commands"][0]["args"] == [
        "--recheck-generation", "2", "--json",
    ]
    assert client.start_calls == []


def test_stop_without_local_pool_invalidates_old_capacity_recheck_generation(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    stopped = _server_response(plan, _envelope(
        status="stopped",
        user_message="已停止这台设备。",
        agent_action="stop_runner",
    ))
    client = FakeClient(starts=[_server_response(plan)], stops=[stopped])
    path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    monkeypatch.setattr(fleet, "stop_batch", lambda _batch: None)
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("stale recheck cannot start"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    waiting = json.loads(capsys.readouterr().out)
    recheck_args = waiting["agent"]["next_commands"][0]["args"]
    generation = int(recheck_args[1])
    assert state["intent_generation"] == generation
    assert state["pending_recheck_generation"] == generation
    assert client.start_calls == []

    # Stop must advance the intent even though no local Fleet item exists.
    assert run_plans.cmd_stop_plan(_args(scope="this-device")) == 0
    json.loads(capsys.readouterr().out)
    assert state["intent_generation"] == generation + 1
    assert state["pending_recheck_generation"] is None

    monkeypatch.setattr(
        run_plans,
        "_capacity_snapshot",
        lambda *_args, **_kwargs: _snapshot(available=2, auto_workers=2),
    )
    assert run_plans.cmd_run_plan(
        _args(recheck_generation=generation),
    ) == 1
    stale = json.loads(capsys.readouterr().out)

    assert stale["error_code"] == "recheck_invalid_or_state_changed"
    assert stale["agent_action"] == "notify_only"
    assert "next_commands" not in stale.get("agent", {})
    assert client.start_calls == []
    assert state["intent_generation"] == generation + 1


def test_valid_capacity_recheck_never_reopens_a_completed_local_run(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[_server_response(plan)])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("completed run cannot reopen"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    waiting = json.loads(capsys.readouterr().out)
    generation = int(waiting["agent"]["next_commands"][0]["args"][1])
    monkeypatch.setattr(fleet, "batch_status", lambda _batch: {
        "status": "completed",
        "plan_id": plan["plan_id"],
        "workers": 0,
    })
    monkeypatch.setattr(
        doctor,
        "plan_environment_issue",
        lambda _plan: pytest.fail("terminal recheck stops before preflight"),
    )

    assert run_plans.cmd_run_plan(
        _args(recheck_generation=generation),
    ) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "recheck_cancelled_by_newer_state"
    assert payload["agent_action"] == "notify_only"
    assert "next_commands" not in payload.get("agent", {})
    assert client.start_calls == []
    assert state["pending_recheck_generation"] is None
    assert state["intent_generation"] == generation


def test_stop_and_old_recheck_are_linearized_by_shared_admission_lock(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[_server_response(plan)])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    monkeypatch.setattr(fleet, "stop_batch", lambda _batch: None)
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("old recheck cannot start"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    waiting = json.loads(capsys.readouterr().out)
    generation = int(waiting["agent"]["next_commands"][0]["args"][1])

    stop_entered = threading.Event()
    allow_stop = threading.Event()

    def stop_run_plan(**kwargs):
        client.stop_calls.append(kwargs)
        stop_entered.set()
        assert allow_stop.wait(timeout=3)
        return _server_response(plan, _envelope(
            status="stopped", agent_action="stop_runner",
        ))

    client.stop_run_plan = stop_run_plan
    outputs = []
    monkeypatch.setattr(
        run_plans, "_output",
        lambda _args, response: outputs.append(response) or 0,
    )
    stop_args = _args(scope="this-device")
    stop_args.json = False
    recheck_args = _args(recheck_generation=generation)
    recheck_args.json = False

    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping = pool.submit(run_plans.cmd_stop_plan, stop_args)
        assert stop_entered.wait(timeout=3)
        rechecking = pool.submit(run_plans.cmd_run_plan, recheck_args)
        time.sleep(0.05)
        assert client.start_calls == []
        allow_stop.set()
        assert stopping.result(timeout=5) == 0
        assert rechecking.result(timeout=5) == 1

    stale = next(
        item for item in outputs
        if item.get("error_code") == "recheck_invalid_or_state_changed"
    )
    assert stale["agent_action"] == "notify_only"
    assert state["pending_recheck_generation"] is None


def test_progress_cannot_restore_a_recheck_generation_after_stop(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(refill=True, max_tasks=20, task_count=4)
    client = FakeClient(starts=[_server_response(plan)])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    monkeypatch.setattr(fleet, "stop_batch", lambda _batch: None)
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("old recheck cannot start"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    waiting = json.loads(capsys.readouterr().out)
    generation = int(waiting["agent"]["next_commands"][0]["args"][1])

    progress_entered = threading.Event()
    allow_progress = threading.Event()

    def progress(_plan_id):
        client.progress_calls.append(_plan_id)
        progress_entered.set()
        assert allow_progress.wait(timeout=3)
        return _server_response(plan, _envelope(
            status="waiting", agent_action="monitor",
        ))

    client.run_plan_progress = progress
    client.stop_results = [_server_response(plan, _envelope(
        status="stopped", agent_action="stop_runner",
    ))]
    outputs = []
    monkeypatch.setattr(
        run_plans, "_output",
        lambda _args, response: outputs.append(response) or 0,
    )
    progress_args = _args()
    progress_args.json = False
    stop_args = _args(scope="this-device")
    stop_args.json = False

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(run_plans.cmd_progress_plan, progress_args)
        assert progress_entered.wait(timeout=3)
        stopping = pool.submit(run_plans.cmd_stop_plan, stop_args)
        # The slow HTTP read does not own the admission lock. Stop becomes the
        # newer intent and completes before the old progress response arrives.
        assert stopping.result(timeout=3) == 0
        assert len(client.stop_calls) == 1
        allow_progress.set()
        assert reading.result(timeout=5) == 0

    assert state["intent_generation"] == generation + 1
    assert state["pending_recheck_generation"] is None
    changed = next(
        item for item in outputs
        if item.get("error_code") == "progress_state_changed"
    )
    assert changed["agent_action"] == "monitor"
    recheck_args = _args(recheck_generation=generation)
    recheck_args.json = False
    assert run_plans.cmd_run_plan(recheck_args) == 1
    stale = outputs[-1]
    assert stale["error_code"] == "recheck_invalid_or_state_changed"
    assert state["intent_generation"] == generation + 1


def test_slow_progress_does_not_block_another_plan_start(
    tmp_path, monkeypatch,
):
    plan_a = _plan()
    plan_b = json.loads(json.dumps(_plan()))
    plan_b["plan_id"] = "plan_progress_other_123456"
    plan_b["batch_id"] = "99999999999999999999999999999999"
    path_a, state_a = _state(tmp_path, plan_a)
    path_b, state_b = _state(tmp_path, plan_b)
    client_a = FakeClient()
    client_b = FakeClient(starts=[_server_response(plan_b)])
    contexts = {
        "progress_plan_a": (path_a, state_a, client_a),
        "run_plan_b": (path_b, state_b, client_b),
    }
    progress_entered = threading.Event()
    allow_progress = threading.Event()

    def slow_progress(_plan_id):
        progress_entered.set()
        assert allow_progress.wait(timeout=3)
        return _server_response(plan_a, _envelope(
            status="running", agent_action="monitor",
        ))

    client_a.run_plan_progress = slow_progress
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda args: (args.plan, *contexts[args.plan]),
    )
    monkeypatch.setattr(doctor, "plan_environment_issue", lambda _plan: None)
    monkeypatch.setattr(
        run_plans,
        "_capacity_snapshot",
        lambda *_args, **_kwargs: _snapshot(available=2, auto_workers=2),
    )
    local = {}
    monkeypatch.setattr(fleet, "batch_status", lambda batch: local.get(batch))
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: local.setdefault(kwargs["batch_id"], {
            "status": "running",
            "plan_id": kwargs["plan_id"],
            "workers": kwargs["workers"],
        }) or {"batch": local[kwargs["batch_id"]]},
    )
    monkeypatch.setattr(run_plans, "_output", lambda _args, _response: 0)
    progress_args = _args()
    progress_args.plan = "progress_plan_a"
    progress_args.json = False
    run_args = _args()
    run_args.plan = "run_plan_b"
    run_args.json = False

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(run_plans.cmd_progress_plan, progress_args)
        assert progress_entered.wait(timeout=3)
        starting = pool.submit(run_plans.cmd_run_plan, run_args)
        assert starting.result(timeout=3) == 0
        assert len(client_b.start_calls) == 1
        allow_progress.set()
        assert reading.result(timeout=5) == 0


def test_corrupt_intent_generation_fails_closed_before_server_or_fleet(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(plan)])
    path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    state["intent_generation"] = "rolled-back-or-corrupt"
    run_plans._atomic_json(path, state)
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("corrupt state cannot start"),
    )

    assert run_plans.cmd_run_plan(_args()) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "local_state_invalid"
    assert client.start_calls == []


def test_server_stopped_start_response_never_launches_a_local_pool(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    stopped = _server_response(plan, _envelope(
        status="stopped",
        user_message="这次运行已经停止，不会再开始新题。",
        agent_action="stop_runner",
    ))
    client = FakeClient(starts=[stopped])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    stopped_batches = []
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("stopped plan cannot start"),
    )
    monkeypatch.setattr(fleet, "stop_batch", stopped_batches.append)

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stopped"
    assert payload["agent_action"] == "stop_runner"
    assert stopped_batches == [BATCH_ID]


@pytest.mark.parametrize("source", ("website", "command", "auto_plan_override"))
@pytest.mark.parametrize("local_status", (None, "stopped"))
@pytest.mark.parametrize("harness", (
    "codex", "claude-code", "dsh-minimal", "grok-build",
    "kimi-code", "zcode", "antigravity", "codebuddy",
))
def test_fixed_workers_reach_fleet_without_local_estimates(
    tmp_path, monkeypatch, capsys, source, local_status, harness,
):
    plan = _plan(
        mode="auto" if source == "auto_plan_override" else "fixed",
        concurrency=None if source == "auto_plan_override" else 5,
        task_count=5,
        harness=harness,
    )
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)

    def forbidden_probe(*_args, **_kwargs):
        pytest.fail("fixed N must not inspect static capacity or reservations")

    monkeypatch.setattr(run_plans, "_capacity_snapshot", forbidden_probe)
    monkeypatch.setattr("dradar.capacity.inspect_capacity", forbidden_probe)
    monkeypatch.setattr("dradar.capacity.docker_resources", forbidden_probe)
    monkeypatch.setattr(fleet, "reserved_workers", forbidden_probe)
    monkeypatch.setattr(fleet, "batch_status", lambda _batch: (
        {"status": local_status, "plan_id": plan["plan_id"], "workers": 1}
        if local_status else None
    ))
    added = []
    monkeypatch.setattr(fleet, "add_batch", lambda **kwargs: (
        added.append(kwargs) or
        {"batch": {"status": "running", "workers": kwargs["workers"]}}
    ))

    assert run_plans.cmd_run_plan(
        _args(concurrency=None if source == "website" else 5),
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_required"] is False
    assert payload["agent"]["selected_concurrency"] == 5
    assert client.start_calls[0]["concurrency"] == 5
    assert added[0]["workers"] == 5
    assert added[0]["refill_harness"] == harness
    assert added[0]["plan_id"] == plan["plan_id"]


@pytest.mark.parametrize("with_old_token", (False, True))
def test_upgrade_retires_old_local_resource_confirmation(
    tmp_path, monkeypatch, capsys, with_old_token,
):
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient(starts=[_server_response(plan)])
    path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
    )
    old = run_plans._local_capacity_response(
        path, state, requested=5, recommended=1,
        snapshot=_snapshot(available=1),
    )
    monkeypatch.setattr(run_plans, "_capacity_snapshot", lambda *_args: (
        pytest.fail("upgrading must not repeat the old resource estimate")
    ))
    monkeypatch.setattr(fleet, "add_batch", lambda **kwargs: {
        "batch": {"workers": kwargs["workers"]},
    })

    assert run_plans.cmd_run_plan(_args(
        concurrency=5 if with_old_token else None,
        decision_token=old["decision_token"] if with_old_token else None,
    )) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_required"] is False
    assert client.start_calls[0]["concurrency"] == 5
    assert client.start_calls[0]["decision_token"] is None
    assert state["pending_local_capacity"] is None


def test_progress_discards_old_local_estimate_barrier_without_starting(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient(progress=[_server_response(
        plan, _envelope(status="ready", agent_action="notify_only"),
    )])
    path, state = _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)
    run_plans._local_capacity_response(
        path, state, requested=5, recommended=1,
        snapshot=_snapshot(available=1),
    )

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] != "local_capacity_decision_pending"
    assert client.progress_calls == [plan["plan_id"]]
    assert client.start_calls == []
    assert run_plans._read_private_json(path)["pending_local_capacity"] is None


def test_progress_preserves_server_capacity_confirmation_and_cancel(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient(starts=[
        _capacity_error(requested=5, available=2, original_mode="fixed"),
    ])
    path, state = _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)

    assert run_plans.cmd_run_plan(_args()) == 0
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "server_capacity"
    assert decision["agent"]["choice_actions"]["cancel"] == {
        "mode": "no_command", "args": [],
    }

    assert run_plans.cmd_progress_plan(_args()) == 0
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["error_code"] == "local_capacity_decision_pending"
    assert client.progress_calls == []
    assert state["pending_local_capacity"]["decision"] == "server_capacity"


@pytest.mark.parametrize("requested,account_limit,error", (
    (0, 8, "concurrency_invalid"),
    (41, 8, "concurrency_invalid"),
    (6, 8, "concurrency_not_allowed"),
    (5, 4, "concurrency_not_allowed"),
))
def test_fixed_workers_still_enforce_count_plan_and_server_limits(
    tmp_path, monkeypatch, capsys, requested, account_limit, error,
):
    plan = _plan(mode="fixed", concurrency=5, task_count=5)
    client = FakeClient()
    monkeypatch.setattr(client, "whoami", lambda: {
        "concurrent_limit": account_limit,
    })
    _prepare_run(monkeypatch, tmp_path, plan=plan, client=client)
    monkeypatch.setattr(fleet, "add_batch", lambda **_kwargs: (
        pytest.fail("invalid or unauthorized concurrency cannot start")
    ))

    assert run_plans.cmd_run_plan(_args(concurrency=requested)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == error
    assert client.start_calls == []


def test_explicit_safe_lower_fixed_count_needs_no_redundant_confirmation(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=4, task_count=4)
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args(concurrency=2)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision_required"] is False
    assert client.start_calls[0]["concurrency"] == 2


def test_explicit_concurrency_cannot_launch_empty_workers_beyond_plan_supply(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(task_count=2, refill=False)
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=40, auto_workers=2, account_limit=40),
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("must not start Fleet"),
    )

    assert run_plans.cmd_run_plan(_args(concurrency=40)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "concurrency_not_allowed"
    assert client.start_calls == []


def test_other_device_confirmation_is_server_authoritative_and_starts_nothing(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    confirm = _envelope(
        status="decision_required",
        interaction="confirm",
        decision_required=True,
        user_message="另一台设备正在运行这次领取，是否让这台设备也一起处理？",
        agent_action="ask_user",
        decision="join_existing",
        decision_token="drd_join_once",
        choices=[
            {"id": "join_existing", "label": "一起运行"},
            {"id": "cancel", "label": "取消"},
        ],
    )
    client = FakeClient(starts=[_server_response(plan, confirm)])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("confirmation cannot start Fleet"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision"] == "join_existing"
    assert payload["decision_token"] == "drd_join_once"
    assert payload["interaction"] == "confirm"
    assert payload["agent"]["state"] == {"devices": []}
    assert payload["agent"]["choice_actions"] == {
        "join_existing": {
            "mode": "replay_current_command_with_args",
            "args": ["--decision-token", "drd_join_once"],
        },
        "cancel": {"mode": "no_command", "args": []},
    }
    assert state["pending_decision"] == {
        "command": "run", "decision": "join_existing",
    }


def test_stale_join_decision_is_rechecked_once_without_old_token_and_never_starts(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    fresh_confirm = _server_response(plan, _envelope(
        status="decision_required",
        interaction="confirm",
        decision_required=True,
        user_message="运行状态已变化，请确认是否让这台设备一起处理。",
        agent_action="ask_user",
        decision="join_existing",
        decision_token="drd_fresh_join_once",
        choices=[
            {"id": "join_existing", "label": "一起运行"},
            {"id": "cancel", "label": "取消"},
        ],
    ))
    client = FakeClient(starts=[_stale_decision_error(), fresh_confirm])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    state["pending_decision"] = {
        "command": "run", "decision": "join_existing",
    }
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **_kwargs: pytest.fail("fresh confirmation cannot start Fleet"),
    )

    assert run_plans.cmd_run_plan(
        _args(decision_token="drd_stale_join_once"),
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_required"] is True
    assert payload["decision_token"] == "drd_fresh_join_once"
    assert len(client.start_calls) == 2
    assert client.start_calls[0]["decision"] == "join_existing"
    assert client.start_calls[0]["decision_token"] == "drd_stale_join_once"
    assert client.start_calls[1]["decision"] is None
    assert client.start_calls[1]["decision_token"] is None
    assert state["pending_decision"] == {
        "command": "run", "decision": "join_existing",
    }


def test_used_join_decision_rechecks_once_and_ensures_lost_success_pool(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    already_running = _server_response(plan, _envelope(
        status="already_running",
        user_message="这台设备已经在运行，正在继续监控。无需操作。",
        agent_action="monitor",
    ))
    client = FakeClient(starts=[
        _stale_decision_error("decision_already_used"),
        already_running,
    ])
    _path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    state["pending_decision"] = {
        "command": "run", "decision": "join_existing",
    }
    added = []
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: added.append(kwargs) or {
            "batch": {"workers": kwargs["workers"]},
        },
    )

    assert run_plans.cmd_run_plan(
        _args(decision_token="drd_used_join_once"),
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "already_running"
    assert payload["agent_action"] == "monitor"
    assert len(client.start_calls) == 2
    assert client.start_calls[1]["decision"] is None
    assert client.start_calls[1]["decision_token"] is None
    assert len(added) == 1
    assert state["pending_decision"] is None


def test_join_confirmation_then_fixed_capacity_lowering_does_not_reask_join(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=4, task_count=4)
    join = _server_response(plan, _envelope(
        status="decision_required",
        interaction="confirm",
        decision_required=True,
        user_message="另一台设备正在运行，是否一起处理？",
        agent_action="ask_user",
        decision="join_existing",
        decision_token="drd_join_once",
        choices=[
            {"id": "join_existing", "label": "一起运行"},
            {"id": "cancel", "label": "取消"},
        ],
    ))
    client = FakeClient(starts=[
        join,
        _capacity_error(requested=4, available=2, original_mode="fixed"),
        _server_response(plan),
    ])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=4, auto_workers=4),
    )
    added = []
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: added.append(kwargs) or {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["decision"] == "join_existing"

    assert run_plans.cmd_run_plan(
        _args(decision_token=first["decision_token"]),
    ) == 0
    lower = json.loads(capsys.readouterr().out)
    composite = lower["decision_token"]
    assert lower["decision"] == "server_capacity"
    assert composite.startswith("drlc_")
    # The private state binds only a digest; it never stores the server token.
    state_text = next(tmp_path.glob("plan-*.json")).read_text()
    assert "drd_join_once" not in state_text

    assert run_plans.cmd_run_plan(
        _args(concurrency=2, decision_token=composite),
    ) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["decision_required"] is False
    assert client.start_calls[1]["decision"] == "join_existing"
    assert client.start_calls[2]["decision"] == "join_existing"
    assert client.start_calls[2]["decision_token"] == "drd_join_once"
    assert added[0]["workers"] == 2


def test_lost_start_response_retry_ensures_missing_local_fleet_pool(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    already_running = _server_response(
        plan,
        _envelope(
            status="already_running",
            user_message="这台设备已经在运行，正在继续监控。无需操作。",
        ),
    )
    client = FakeClient(starts=[ApiError("response lost"), already_running])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=2, auto_workers=2),
    )
    added = []
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: added.append(kwargs) or {"batch": {"workers": kwargs["workers"]}},
    )

    assert run_plans.cmd_run_plan(_args()) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["error_code"] == "service_unavailable"
    assert added == []

    assert run_plans.cmd_run_plan(_args()) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "already_running"
    assert second["agent_action"] == "monitor"
    assert second["agent"]["server_status"]["status"] == "already_running"
    assert len(added) == 1
    assert added[0]["credentials_file"].name.startswith("plan-")


@pytest.mark.parametrize("old_controller", (False, True))
def test_same_device_with_live_local_pool_is_idempotently_ensured(
    tmp_path, monkeypatch, capsys, old_controller,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(
        plan, _envelope(status="already_running"),
    )])
    path, state = _state(tmp_path, plan)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        fleet,
        "batch_status",
        lambda _batch: {
            "status": "running", "plan_id": plan["plan_id"], "workers": 2,
        },
    )
    monkeypatch.setattr(
        run_plans,
        "_capacity_snapshot",
        lambda *_args, **_kwargs: pytest.fail("idempotent monitor needs no capacity check"),
    )
    ensured = []

    def ensure(**kwargs):
        ensured.append(kwargs)
        if old_controller:
            raise fleet.FleetControllerUpdatePending()
        return {"already_active": True, "batch": {"workers": kwargs["workers"]}}

    monkeypatch.setattr(
        fleet,
        "add_batch",
        ensure,
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_running"
    assert client.start_calls[0]["concurrency"] == 2
    assert len(ensured) == 1
    assert ensured[0]["workers"] == 2


@pytest.mark.parametrize(
    "status,expected_code,expected_rc",
    [
        ("running", "local_concurrency_change_requires_restart", 1),
        ("stopping", "local_run_stopping", 1),
    ],
)
def test_active_local_pool_is_not_silently_resized_or_reactivated(
    status, expected_code, expected_rc, tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(plan)])
    path, state = _state(tmp_path, plan)
    monkeypatch.setattr(
        run_plans, "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        fleet, "batch_status",
        lambda _batch: {
            "status": status, "plan_id": plan["plan_id"], "workers": 2,
        },
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("must not resize or restart"),
    )

    assert run_plans.cmd_run_plan(_args(concurrency=1)) == expected_rc
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == expected_code
    if status == "stopping":
        assert payload["agent_action"] == "notify_only"
        assert "next_commands" not in payload.get("agent", {})
    assert client.start_calls == []


def test_stop_winning_after_server_start_is_not_replayed_as_a_new_run(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(
        starts=[_server_response(plan)],
        stops=[_server_response(plan, _envelope(
            status="stopped", agent_action="stop_runner",
        ))],
    )
    path, state = _state(tmp_path, plan)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(doctor, "plan_environment_issue", lambda _plan: None)
    monkeypatch.setattr(
        run_plans,
        "_capacity_snapshot",
        lambda *_args, **_kwargs: _snapshot(available=2, auto_workers=2),
    )
    statuses = iter([
        None,
        {"status": "stopping", "plan_id": plan["plan_id"], "workers": 2},
    ])
    monkeypatch.setattr(fleet, "batch_status", lambda _batch: next(statuses))
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("stopping cannot restart"),
    )

    assert run_plans.cmd_run_plan(_args()) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "local_run_stopping"
    assert payload["agent_action"] == "notify_only"
    assert "next_commands" not in payload.get("agent", {})
    assert len(client.start_calls) == 1
    assert client.stop_calls == [{
        "plan_id": plan["plan_id"], "scope": "this_device",
    }]


def test_orphaned_live_pool_is_counted_and_never_spawned_twice(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(
        plan, _envelope(status="already_running"),
    )])
    path, state = _state(tmp_path, plan)
    monkeypatch.setattr(
        run_plans, "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        fleet, "batch_status",
        lambda _batch: {
            "status": "orphaned", "plan_id": plan["plan_id"], "workers": 2,
        },
    )
    monkeypatch.setattr(
        fleet, "add_batch", lambda **_kwargs: pytest.fail("orphan lock forbids duplicate"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "already_running"
    assert client.start_calls[0]["concurrency"] == 2


def test_concurrent_auto_plans_share_one_atomic_local_admission_budget(
    tmp_path, monkeypatch,
):
    plan_a = _plan(task_count=4)
    plan_b = json.loads(json.dumps(_plan(task_count=4)))
    plan_b["plan_id"] = "plan_test_second_123456"
    plan_b["batch_id"] = "87654321876543218765432187654321"
    path_a, state_a = _state(tmp_path, plan_a)
    path_b, state_b = _state(tmp_path, plan_b)
    client_a = FakeClient(starts=[_server_response(plan_a)])
    client_b = FakeClient(starts=[_server_response(plan_b)])
    contexts = {
        "run_concurrent_plan_a": (path_a, state_a, client_a),
        "run_concurrent_plan_b": (path_b, state_b, client_b),
    }
    reservations = {}
    first_snapshot = threading.Event()
    outputs = {}

    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda args: (args.plan, *contexts[args.plan]),
    )
    monkeypatch.setattr(doctor, "plan_environment_issue", lambda _plan: None)
    monkeypatch.setattr(
        fleet, "batch_status", lambda batch_id: reservations.get(batch_id),
    )

    def snapshot(_client, _plan, _limits=None):
        used = sum(item["workers"] for item in reservations.values())
        if not first_snapshot.is_set():
            first_snapshot.set()
            time.sleep(0.1)
        available = max(0, 4 - used)
        result = _snapshot(
            available=available, auto_workers=min(4, available), account_limit=8,
        )
        result["digest"] = f"used-{used}"
        return result

    def add_batch(**kwargs):
        reservations[kwargs["batch_id"]] = {
            "status": "running",
            "plan_id": kwargs["plan_id"],
            "workers": kwargs["workers"],
        }
        return {"batch": reservations[kwargs["batch_id"]]}

    monkeypatch.setattr(run_plans, "_capacity_snapshot", snapshot)
    monkeypatch.setattr(fleet, "add_batch", add_batch)
    monkeypatch.setattr(
        run_plans,
        "_output",
        lambda args, response: outputs.setdefault(args.plan, response) is None and 0 or 0,
    )

    def args(code):
        return SimpleNamespace(
            plan=code, server="https://api.codexradar.com", concurrency=None,
            decision_token=None, scope=None, json=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_plans.cmd_run_plan, args("run_concurrent_plan_a"))
        assert first_snapshot.wait(timeout=2)
        second = pool.submit(run_plans.cmd_run_plan, args("run_concurrent_plan_b"))
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert results == [0, 0]
    assert sum(item["workers"] for item in reservations.values()) == 4
    assert len(client_a.start_calls) + len(client_b.start_calls) == 1
    waiting = outputs["run_concurrent_plan_b"]
    assert waiting["error_code"] == "local_capacity_unavailable"
    assert waiting["agent_action"] == "recheck_plan"
    assert waiting["poll_after_seconds"] == 30
    action = waiting["agent"]["next_commands"][0]
    assert action["args"] == ["--recheck-generation", "2", "--json"]
    assert action["inherit"] == ["--plan", "--server"]
    serialized = json.dumps(action)
    assert "decision-token" not in serialized
    assert "concurrency" not in serialized


def test_fixed_plan_keeps_workers_after_concurrent_local_reservation(
    tmp_path, monkeypatch,
):
    plan_a = _plan(task_count=3)
    plan_b = _plan(mode="fixed", concurrency=2, task_count=2)
    plan_b["plan_id"] = "plan_test_fixed_second_123456"
    plan_b["batch_id"] = "abcdefabcdefabcdefabcdefabcdefab"
    path_a, state_a = _state(tmp_path, plan_a)
    path_b, state_b = _state(tmp_path, plan_b)
    client_a = FakeClient(starts=[_server_response(plan_a)])
    client_b = FakeClient(starts=[_server_response(plan_b)])
    contexts = {
        "run_capacity_plan_a": (path_a, state_a, client_a),
        "run_capacity_plan_b": (path_b, state_b, client_b),
    }
    reservations = {}
    first_snapshot = threading.Event()
    outputs = {}
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans, "_state_and_client",
        lambda args: (args.plan, *contexts[args.plan]),
    )
    monkeypatch.setattr(doctor, "plan_environment_issue", lambda _plan: None)
    monkeypatch.setattr(fleet, "batch_status", lambda batch: reservations.get(batch))

    def snapshot(_client, plan, _limits=None):
        used = sum(item["workers"] for item in reservations.values())
        if plan["plan_id"] == plan_a["plan_id"]:
            first_snapshot.set()
            time.sleep(0.1)
        available = max(0, 4 - used)
        result = _snapshot(
            available=available,
            auto_workers=min(len(plan["assignments"]), available),
            account_limit=8,
        )
        result["digest"] = f"used-{used}"
        return result

    monkeypatch.setattr(run_plans, "_capacity_snapshot", snapshot)
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **kwargs: reservations.setdefault(kwargs["batch_id"], {
            "status": "running", "plan_id": kwargs["plan_id"],
            "workers": kwargs["workers"],
        }) and {"batch": reservations[kwargs["batch_id"]]},
    )
    monkeypatch.setattr(
        run_plans, "_output",
        lambda args, response: outputs.__setitem__(args.plan, response) or 0,
    )

    def args(code):
        return SimpleNamespace(
            plan=code, server="https://api.codexradar.com", concurrency=None,
            decision_token=None, scope=None, json=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_plans.cmd_run_plan, args("run_capacity_plan_a"))
        assert first_snapshot.wait(timeout=2)
        second = pool.submit(run_plans.cmd_run_plan, args("run_capacity_plan_b"))
        assert first.result(timeout=5) == 0
        assert second.result(timeout=5) == 0

    assert sum(item["workers"] for item in reservations.values()) == 5
    assert client_b.start_calls[0]["concurrency"] == 2
    assert outputs["run_capacity_plan_b"]["decision_required"] is False
    assert reservations[plan_b["batch_id"]]["workers"] == 2


def test_stop_linearizes_after_inflight_start_and_stops_the_new_pool(
    tmp_path, monkeypatch,
):
    plan = _plan(task_count=2)
    path, state = _state(tmp_path, plan)
    stopped = _server_response(plan, _envelope(
        status="stopped",
        user_message="已停止这台设备。",
        agent_action="stop_runner",
    ))
    client = FakeClient(starts=[_server_response(plan)], stops=[stopped])
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans, "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(doctor, "plan_environment_issue", lambda _plan: None)
    monkeypatch.setattr(
        run_plans, "_capacity_snapshot",
        lambda *_args, **_kwargs: _snapshot(available=2, auto_workers=2),
    )
    local = {}
    events = []
    add_entered = threading.Event()
    allow_add = threading.Event()
    monkeypatch.setattr(fleet, "batch_status", lambda batch: local.get(batch))

    def add_batch(**kwargs):
        add_entered.set()
        assert allow_add.wait(timeout=3)
        local[kwargs["batch_id"]] = {
            "status": "running", "plan_id": kwargs["plan_id"],
            "workers": kwargs["workers"],
        }
        events.append("add")
        return {"batch": local[kwargs["batch_id"]]}

    def stop_batch(batch_id):
        events.append("stop")
        local[batch_id]["status"] = "stopping"

    monkeypatch.setattr(fleet, "add_batch", add_batch)
    monkeypatch.setattr(fleet, "stop_batch", stop_batch)
    monkeypatch.setattr(run_plans, "_output", lambda _args, _response: 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(run_plans.cmd_run_plan, _args())
        assert add_entered.wait(timeout=3)
        stopping = pool.submit(
            run_plans.cmd_stop_plan, _args(scope="this-device"),
        )
        time.sleep(0.05)
        assert client.stop_calls == []
        allow_add.set()
        assert starting.result(timeout=5) == 0
        assert stopping.result(timeout=5) == 0

    assert events == ["add", "stop"]
    assert client.stop_calls[0]["scope"] == "this_device"


@pytest.mark.parametrize("fixed", (False, True))
def test_current_plan_environment_failure_happens_before_server_start(
    tmp_path, monkeypatch, capsys, fixed,
):
    plan = _plan(
        harness="grok-build", mode="fixed" if fixed else "auto",
        concurrency=2 if fixed else None,
    )
    client = FakeClient(starts=[_server_response(plan)])
    _prepare_run(
        monkeypatch,
        tmp_path,
        plan=plan,
        client=client,
        environment_issue={
            "error_code": "current_tool_not_ready",
            "user_message": "这次运行需要 Grok；请完成 Grok 的安装和登录后重试。",
            "agent_action": "setup_current_tool",
        },
    )
    monkeypatch.setattr(
        run_plans,
        "_capacity_snapshot",
        lambda *_args: pytest.fail("environment failure must precede capacity"),
    )

    assert run_plans.cmd_run_plan(_args()) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "current_tool_not_ready"
    assert payload["agent_action"] == "setup_current_tool"
    assert client.start_calls == []


def test_missing_docker_requires_human_choice_without_installing_or_starting(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    client = FakeClient(starts=[_server_response(plan)])
    path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        environment_issue={
            "error_code": "docker_install_confirmation_required",
            "user_message": "这台设备尚未安装可用的 Docker。是否安装推荐的 Docker 环境？",
            "agent_action": "install_docker",
            "install_required": True,
            "agent": {"requires_user_action": True},
        },
    )
    monkeypatch.setattr(
        run_plans, "_capacity_snapshot",
        lambda *_args: pytest.fail("no capacity check before install consent"),
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_required"] is True
    assert [choice["label"] for choice in payload["choices"]] == [
        "安装推荐的 Docker 环境", "暂不安装",
    ]
    assert payload["agent"]["choice_actions"]["cancel"] == {
        "mode": "no_command", "args": [],
    }
    install_action = payload["agent"]["choice_actions"]["install"]
    assert install_action["mode"] == "replay_current_command_with_args"
    assert install_action["args"][0] == "--docker-install-token"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "brew install" not in serialized
    assert "winget install" not in serialized
    assert "apt-get" not in serialized
    assert client.start_calls == []
    assert state["pending_docker_install"] is not None
    assert json.loads(path.read_text())["pending_docker_install"] is not None


def test_approved_docker_install_is_consumed_once_then_original_plan_starts_once(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(task_count=1)
    client = FakeClient(starts=[_server_response(plan)])
    path, state = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        snapshot=_snapshot(available=1, auto_workers=1),
        environment_issue={
            "error_code": "docker_install_confirmation_required",
            "user_message": "这台设备尚未安装可用的 Docker。是否安装推荐环境？",
            "agent_action": "install_docker",
            "install_required": True,
        },
    )
    local = {}
    monkeypatch.setattr(fleet, "batch_status", lambda batch: local.get(batch))
    monkeypatch.setattr(
        fleet, "add_batch",
        lambda **kwargs: {
            "batch": local.setdefault(kwargs["batch_id"], {
                "status": "running", "plan_id": kwargs["plan_id"],
                "workers": kwargs["workers"],
            }),
        },
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    prompt = json.loads(capsys.readouterr().out)
    token = prompt["agent"]["choice_actions"]["install"]["args"][1]
    calls = []

    def recovered(_plan, *, allow_docker_install=False):
        calls.append(allow_docker_install)
        return None

    monkeypatch.setattr(doctor, "plan_environment_issue", recovered)

    assert run_plans.cmd_run_plan(_args(docker_install_token=token)) == 0
    started = json.loads(capsys.readouterr().out)

    assert calls == [True]
    assert started["agent_action"] == "monitor"
    assert len(client.start_calls) == 1
    assert state["pending_docker_install"] is None
    assert json.loads(path.read_text())["pending_docker_install"] is None

    # The exact same approval is spent, so it cannot install or start twice.
    assert run_plans.cmd_run_plan(_args(docker_install_token=token)) == 1
    stale = json.loads(capsys.readouterr().out)
    assert stale["error_code"] == "docker_install_decision_invalid"
    assert len(client.start_calls) == 1


def test_docker_install_approval_fails_closed_when_original_arguments_change(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan(mode="fixed", concurrency=2)
    client = FakeClient(starts=[_server_response(plan)])
    _path, _state_value = _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        environment_issue={
            "error_code": "docker_install_confirmation_required",
            "user_message": "需要安装 Docker。",
            "agent_action": "install_docker",
            "install_required": True,
        },
    )

    assert run_plans.cmd_run_plan(_args(concurrency=1)) == 0
    prompt = json.loads(capsys.readouterr().out)
    token = prompt["agent"]["choice_actions"]["install"]["args"][1]

    assert run_plans.cmd_run_plan(
        _args(concurrency=2, docker_install_token=token),
    ) == 1
    stale = json.loads(capsys.readouterr().out)
    assert stale["error_code"] == "docker_install_decision_invalid"
    assert client.start_calls == []


def test_old_agent_can_stop_on_new_install_choice_without_unknown_command(
    tmp_path, monkeypatch, capsys,
):
    """Schema-v1 Agents can safely choose no_command and never install."""

    plan = _plan()
    client = FakeClient(starts=[])
    _prepare_run(
        monkeypatch, tmp_path, plan=plan, client=client,
        environment_issue={
            "error_code": "docker_install_confirmation_required",
            "user_message": "需要安装 Docker。",
            "agent_action": "install_docker",
            "install_required": True,
        },
    )

    assert run_plans.cmd_run_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["agent"]["choice_actions"]["cancel"]["mode"] == "no_command"
    assert client.start_calls == []


def test_install_choice_from_capacity_recheck_drops_spent_generation(tmp_path):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    state["intent_generation"] = 7
    state["pending_recheck_generation"] = None
    args = _args(recheck_generation=7)

    payload = run_plans._docker_install_response(
        path, state, args, user_message="需要安装 Docker。",
    )
    action = payload["agent"]["choice_actions"]["install"]

    assert action["mode"] == "replay_plan_command"
    assert action["command"] == "run"
    assert action["inherit"] == ["--plan", "--server"]
    assert "--recheck-generation" not in action["args"]
    assert "--docker-install-token" in action["args"]


def test_codex_plan_does_not_probe_unrelated_grok_or_kimi_credentials(
    tmp_path, monkeypatch,
):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor.runner, "_resolve_user_tool", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(doctor.runner, "codex_auth_path", lambda: auth)
    monkeypatch.setattr(
        doctor, "grok_auth_error", lambda: pytest.fail("Grok is unrelated"),
    )
    monkeypatch.setattr(
        doctor, "kimi_auth_error", lambda: pytest.fail("Kimi is unrelated"),
    )

    assert doctor.plan_environment_issue(_plan(harness="codex")) is None


@pytest.mark.parametrize(
    "providers",
    [
        ["openai", "deepseek"],
        ["deepseek", "openai"],
    ],
)
def test_codex_mixed_provider_plan_checks_every_required_capability_once(
    providers, tmp_path, monkeypatch,
):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    plan = _plan(harness="codex", task_count=len(providers))
    for assignment, provider in zip(plan["assignments"], providers):
        assignment["provider"] = provider
    calls = {"key": 0, "catalog": 0}

    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_resolve_user_tool",
        lambda name: "/usr/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(doctor.runner, "codex_auth_path", lambda: auth)

    def deepseek_key():
        calls["key"] += 1
        return "configured"

    def catalog_error():
        calls["catalog"] += 1
        return None

    monkeypatch.setattr(doctor, "deepseek_api_key", deepseek_key)
    monkeypatch.setattr(doctor, "deepseek_catalog_error", catalog_error)
    monkeypatch.setattr(
        doctor, "grok_auth_error", lambda: pytest.fail("Grok is unrelated"),
    )
    monkeypatch.setattr(
        doctor, "kimi_auth_error", lambda: pytest.fail("Kimi is unrelated"),
    )

    assert doctor.plan_environment_issue(plan) is None
    assert calls == {"key": 1, "catalog": 1}


@pytest.mark.parametrize(
    "providers",
    [
        ["openai", "deepseek"],
        ["deepseek", "openai"],
    ],
)
def test_codex_mixed_provider_plan_requires_native_and_supplemental_auth(
    providers, tmp_path, monkeypatch,
):
    missing_auth = tmp_path / "missing-auth.json"
    plan = _plan(harness="codex", task_count=len(providers))
    for assignment, provider in zip(plan["assignments"], providers):
        assignment["provider"] = provider
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_resolve_user_tool",
        lambda name: "/usr/bin/codex" if name == "codex" else None,
    )
    monkeypatch.setattr(
        doctor.runner, "codex_auth_path", lambda: missing_auth,
    )
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: "configured")
    monkeypatch.setattr(doctor, "deepseek_catalog_error", lambda: None)

    native_issue = doctor.plan_environment_issue(plan)
    assert native_issue["error_code"] == "codex_not_authenticated"
    assert native_issue["user_message"] == "当前运行工具尚未登录；请完成登录后重试。"

    missing_auth.write_text("{}")
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: None)
    supplemental_issue = doctor.plan_environment_issue(plan)
    assert supplemental_issue["error_code"] == "current_tool_not_authenticated"
    assert "provider" not in supplemental_issue["user_message"].lower()


def test_codex_plan_with_unknown_provider_fails_closed_before_probing_auth(
    monkeypatch,
):
    plan = _plan(harness="codex", provider="future-provider")
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_resolve_user_tool",
        lambda _name: pytest.fail("unsupported scope must fail before auth probes"),
    )

    issue = doctor.plan_environment_issue(plan)

    assert issue["error_code"] == "current_tool_unsupported"
    assert issue["agent_action"] == "upgrade_cli"
    assert "provider" not in issue["user_message"].lower()


def test_server_wire_dsh_harness_maps_to_local_dsh_minimal_preflight(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor.runner, "_resolve_user_tool",
        lambda name: "/usr/bin/uvx" if name == "uvx" else None,
    )
    monkeypatch.setattr(doctor, "deepseek_api_key", lambda: "configured")

    assert doctor.plan_environment_issue(_plan(harness="dsh")) is None
    recovery = doctor._plan_agent_recovery("dsh", setup_provider="deepseek")
    assert recovery["next_commands"][-1]["argv"] == [
        "dradar", "doctor", "--agent", "dsh-minimal",
    ]


def test_missing_current_tool_on_second_machine_has_actionable_issue(
    monkeypatch,
):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor, "grok_cli_path", lambda: None)
    monkeypatch.setattr(doctor, "grok_auth_error", lambda: "not signed in")

    issue = doctor.plan_environment_issue(_plan(harness="grok-build"))

    assert issue["error_code"] == "current_tool_not_ready"
    assert issue["user_message"] == "Grok 运行工具需要安装或更新；请完成准备后重试。"
    assert issue["agent_action"] == "setup_current_tool"
    assert issue["agent"]["requires_user_action"] is True
    assert [item["argv"] for item in issue["agent"]["next_commands"]] == [
        ["dradar", "provider", "setup", "grok"],
        ["dradar", "provider", "status", "grok", "--live"],
        ["dradar", "doctor", "--agent", "grok-build"],
    ]


def test_claude_plan_accepts_ready_subscription_runtime(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in {"docker", "claude"} else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor, "claude_subscription_error", lambda: None)

    assert doctor.plan_environment_issue(
        _plan(harness="claude-code", task_count=1),
    ) is None


@pytest.mark.parametrize("version", ["2.137.1", "2.143.0"])
def test_codebuddy_plan_accepts_compatible_host_login_source(
    version, monkeypatch,
):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor, "codebuddy_executable", lambda: "/usr/bin/codebuddy",
    )
    monkeypatch.setattr(
        codebuddy_provider, "codebuddy_version", lambda _executable: version,
    )
    monkeypatch.setattr(
        doctor, "codebuddy_credential_status", lambda: (True, "ready"),
    )
    monkeypatch.setattr(
        doctor, "codebuddy_runtime_image_error", lambda _docker: None,
    )

    assert doctor.plan_environment_issue(
        _plan(harness="codebuddy", task_count=1),
    ) is None


@pytest.mark.parametrize(
    "executable,version,credentials_ready,image_issue,error_code,message",
    [
        (None, None, True, None, "current_tool_not_installed", "尚未安装"),
        ("/usr/bin/codebuddy", None, True, None,
         "current_tool_version_unknown", "无法识别"),
        ("/usr/bin/codebuddy", "2.136.9", True, None,
         "current_tool_version_incompatible", "不兼容"),
        ("/usr/bin/codebuddy", "2.143.0", False, None,
         "current_tool_not_authenticated", "尚未登录"),
        ("/usr/bin/codebuddy", "2.143.0", True, "image missing",
         "current_tool_runtime_not_ready", "隔离运行环境"),
    ],
)
def test_codebuddy_plan_reports_each_readiness_failure_separately(
    executable, version, credentials_ready, image_issue, error_code, message,
    monkeypatch,
):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor, "codebuddy_executable", lambda: executable)
    monkeypatch.setattr(
        codebuddy_provider, "codebuddy_version", lambda _executable: version,
    )
    monkeypatch.setattr(
        doctor, "codebuddy_credential_status",
        lambda: (credentials_ready, "login missing"),
    )
    monkeypatch.setattr(
        doctor, "codebuddy_runtime_image_error", lambda _docker: image_issue,
    )

    issue = doctor.plan_environment_issue(
        _plan(harness="codebuddy", task_count=1),
    )

    assert issue["error_code"] == error_code
    assert message in issue["user_message"]


@pytest.mark.parametrize("missing", ["cli", "oauth"])
def test_claude_plan_fails_closed_with_actionable_setup(missing, monkeypatch):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: (
            "/usr/bin/docker" if name == "docker" else
            (None if missing == "cli" else "/usr/bin/claude")
        ),
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(
        doctor, "claude_subscription_error",
        lambda: "missing OAuth" if missing == "oauth" else None,
    )

    issue = doctor.plan_environment_issue(
        _plan(harness="claude-code", task_count=1),
    )

    assert issue["error_code"] == "current_tool_not_ready"
    assert issue["agent_action"] == "setup_current_tool"
    assert issue["agent"]["requires_user_action"] is True
    assert [item["argv"] for item in issue["agent"]["next_commands"]] == [
        ["dradar", "provider", "setup", "claude"],
        ["dradar", "provider", "status", "claude", "--live"],
        ["dradar", "doctor", "--agent", "claude-code"],
    ]


@pytest.mark.parametrize(
    "harness,old_path,new_path,version,cli_path_name,auth_name,ensure_name",
    [
        ("kimi-code", "/old/kimi", "/managed/kimi", doctor.KIMI_CLI_VERSION,
         "kimi_cli_path", "kimi_auth_error", "_ensure_kimi_cli"),
        ("grok-build", "/old/grok", "/managed/grok", doctor.GROK_CLI_VERSION,
         "grok_cli_path", "grok_auth_error", "_ensure_grok_cli"),
    ],
)
def test_plan_preflight_repairs_stale_subscription_cli_before_server_start(
    monkeypatch, harness, old_path, new_path, version,
    cli_path_name, auth_name, ensure_name,
):
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    monkeypatch.setattr(doctor, "_probe", lambda _command: True)
    monkeypatch.setattr(doctor.runner, "ensure_pier", lambda: None)
    monkeypatch.setattr(doctor, cli_path_name, lambda: old_path)
    monkeypatch.setattr(doctor, auth_name, lambda: None)
    monkeypatch.setattr(
        doctor, "_subscription_cli_version",
        lambda executable, _parser: version if executable == new_path else "old",
    )
    repaired = []
    monkeypatch.setattr(
        provider_config, ensure_name,
        lambda: repaired.append(new_path) or new_path,
    )

    assert doctor.plan_environment_issue(_plan(harness=harness)) is None
    assert repaired == [new_path]


def test_agent_details_always_carry_their_own_schema_version(capsys):
    response = {
        "schema_version": 1,
        **_envelope(),
        "agent": {"next_commands": [{
            "argv": ["dradar", "fleet", "status"], "interactive": False,
        }]},
    }
    assert run_plans._output(_args(), response) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["agent"]["schema_version"] == 1


def test_monitor_response_reuses_exact_cached_git_revision_offline(
    monkeypatch, capsys,
):
    class Distribution:
        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            return json.dumps({
                "url": "https://github.com/SecurityMind/dradar",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "a" * 40,
                },
            })

    monkeypatch.setattr(
        run_plans.importlib.metadata,
        "distribution",
        lambda name: Distribution() if name == "dradar" else None,
    )
    response = {
        "schema_version": 1,
        **_envelope(agent_action="monitor"),
        "agent": {
            "followup_launcher": {
                "mode": "unexpected",
                "argv_prefix": ["untrusted"],
            },
        },
    }

    assert run_plans._output(_args(), response) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent"]["followup_launcher"] == {
        "schema_version": 1,
        "mode": "uvx_offline_git_revision",
        "argv_prefix": [
            "uvx", "--offline", "--from",
            "git+https://github.com/SecurityMind/dradar@" + "a" * 40,
            "dradar",
        ],
        "interactive": False,
    }
    assert RUN_CODE not in json.dumps(payload)


@pytest.mark.parametrize(
    "direct_url",
    [
        None,
        {"url": "https://example.com/dradar", "vcs_info": {
            "vcs": "git", "commit_id": "a" * 40,
        }},
        {"url": "https://github.com:notaport/SecurityMind/dradar", "vcs_info": {
            "vcs": "git", "commit_id": "a" * 40,
        }},
        {"url": "https://github.com:/SecurityMind/dradar", "vcs_info": {
            "vcs": "git", "commit_id": "a" * 40,
        }},
        {"url": "https://[broken", "vcs_info": {
            "vcs": "git", "commit_id": "a" * 40,
        }},
        {"url": "https://github.com/SecurityMind/dradar", "vcs_info": {
            "vcs": "git", "commit_id": "not-a-commit",
        }},
    ],
)
def test_followup_launcher_fails_closed_without_trusted_immutable_provenance(
    monkeypatch, direct_url,
):
    class Distribution:
        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            return None if direct_url is None else json.dumps(direct_url)

    monkeypatch.setattr(
        run_plans.importlib.metadata,
        "distribution", lambda _name: Distribution(),
    )

    assert run_plans._followup_launcher() is None


def test_followup_launcher_fails_closed_on_invalid_metadata_encoding(monkeypatch):
    class Distribution:
        @staticmethod
        def read_text(name):
            assert name == "direct_url.json"
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(
        run_plans.importlib.metadata,
        "distribution", lambda _name: Distribution(),
    )

    assert run_plans._followup_launcher() is None


def test_monitor_response_drops_untrusted_transmitted_followup_launcher(
    monkeypatch, capsys,
):
    monkeypatch.setattr(run_plans, "_followup_launcher", lambda: None)
    response = {
        "schema_version": 1,
        **_envelope(agent_action="monitor"),
        "agent": {
            "followup_launcher": {
                "mode": "unexpected",
                "argv_prefix": ["untrusted"],
            },
        },
    }

    assert run_plans._output(_args(), response) == 0
    payload = json.loads(capsys.readouterr().out)

    assert "followup_launcher" not in payload["agent"]


def test_zero_spare_capacity_asks_in_plain_language_without_internal_ids(tmp_path):
    plan = _plan(mode="fixed", concurrency=1)
    path, state = _state(tmp_path, plan)
    response = run_plans._local_capacity_response(
        path, state, requested=1, recommended=0,
        snapshot=_snapshot(available=0, auto_workers=0),
    )
    assert response["user_message"] == (
        "这台设备正在运行其他题目。继续同时启动这 1 道可能会让机器变慢，是否仍然启动？"
    )
    assert [choice["label"] for choice in response["choices"]] == [
        "仍然同时启动 1 道", "暂不启动",
    ]


@pytest.mark.parametrize(
    "harness,provider",
    [
        ("dsh-minimal", "deepseek"),
        ("grok-build", "grok"),
        ("kimi-code", "kimi"),
        ("claude-code", "claude"),
        ("zcode", "zcode"),
        ("antigravity", "antigravity"),
        ("codebuddy", "codebuddy"),
    ],
)
def test_every_optional_tool_has_versioned_nonsecret_agent_commands(
    harness, provider,
):
    recovery = doctor._plan_agent_recovery(harness, setup_provider=provider)

    assert recovery["schema_version"] == 1
    assert recovery["requires_user_action"] is True
    commands = [item["argv"] for item in recovery["next_commands"]]
    assert commands == [
        ["dradar", "provider", "setup", provider],
        ["dradar", "provider", "status", provider, "--live"],
        ["dradar", "doctor", "--agent", harness],
    ]
    serialized = json.dumps(recovery)
    assert PLAN_TOKEN not in serialized
    assert RUN_CODE not in serialized


def test_codex_login_and_docker_recovery_require_user_action(monkeypatch):
    codex = doctor._plan_agent_recovery("codex", codex_login=True)
    assert [item["argv"] for item in codex["next_commands"]] == [
        ["codex", "login"],
        ["dradar", "doctor", "--agent", "codex"],
    ]
    assert codex["next_commands"][0]["interactive"] is True

    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        docker_runtime,
        "ensure_docker",
        lambda **_kwargs: docker_runtime.Recovery(
            False, "docker_install_confirmation_required",
            "这台设备尚未安装可用的 Docker。是否安装推荐环境？",
            True, "install_confirmation", install_required=True,
        ),
    )
    issue = doctor.plan_environment_issue(_plan(harness="codex"))
    assert issue["error_code"] == "docker_install_confirmation_required"
    assert issue["agent"]["requires_user_action"] is True
    assert issue["agent"]["next_commands"] == []


@pytest.mark.parametrize("status_code", [409, 410, 429])
def test_structured_server_error_envelope_is_preserved(status_code):
    envelope = _envelope(
        status="decision_required" if status_code == 409 else "error",
        interaction="confirm" if status_code == 409 else "notify",
        decision_required=status_code == 409,
        user_message=f"safe server message {status_code}",
        agent_action="ask_user" if status_code == 409 else "stop",
        error_code=f"server_error_{status_code}",
        choices=[{"id": "cancel", "label": "取消"}] if status_code == 409 else [],
        decision="recover_stale" if status_code == 409 else None,
        decision_token="drd_server_once" if status_code == 409 else None,
    )
    exc = ApiError(
        "unsafe transport prose",
        status_code=status_code,
        code=f"server_error_{status_code}",
        payload={
            "detail": "safe detail",
            "code": f"server_error_{status_code}",
            "envelope": envelope,
        },
    )

    result = run_plans._api_error_response(exc)

    assert result["user_message"] == f"safe server message {status_code}"
    assert result["error_code"] == f"server_error_{status_code}"
    assert result["decision_required"] is (status_code == 409)
    assert "unsafe transport prose" not in json.dumps(result)


def test_progress_and_stop_reuse_saved_plan_access_without_exchange(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(status="running", user_message="正在运行 1 道题。无需操作。"),
        progress={"running": 1, "waiting": 1, "submitted": 0},
    )
    stopped = _server_response(
        plan,
        _envelope(
            status="stopped",
            user_message="已停止这台设备，其他设备不受影响。",
            agent_action="stop_runner",
        ),
    )
    client = FakeClient(progress=[progress], stops=[stopped])
    monkeypatch.setattr(
        run_plans, "_saved_state", lambda _code, **_kwargs: (path, state),
    )
    monkeypatch.setattr(
        run_plans,
        "_exchange",
        lambda *_args, **_kwargs: pytest.fail("saved commands must not exchange again"),
    )
    monkeypatch.setattr(run_plans, "ApiClient", lambda *_args, **_kwargs: client)
    stopped_batches = []
    monkeypatch.setattr(fleet, "stop_batch", stopped_batches.append)

    assert run_plans.cmd_progress_plan(_args()) == 0
    progress_payload = json.loads(capsys.readouterr().out)
    assert progress_payload["status"] == "running"
    assert progress_payload["agent"]["progress"]["running"] == 1

    assert run_plans.cmd_stop_plan(_args(scope="this-device")) == 0
    stop_payload = json.loads(capsys.readouterr().out)
    assert stop_payload["status"] == "stopped"
    assert client.stop_calls[0]["scope"] == "this_device"
    assert stopped_batches == [BATCH_ID]


def test_stale_stop_all_decision_rechecks_once_without_stopping(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    state["pending_decision"] = {
        "command": "stop", "decision": "stop_all_devices",
    }
    fresh_confirm = _server_response(plan, _envelope(
        status="decision_required",
        interaction="confirm",
        decision_required=True,
        user_message="运行状态已变化，请再次确认是否停止所有设备。",
        agent_action="ask_user",
        decision="stop_all_devices",
        decision_token="drd_fresh_stop_once",
        choices=[
            {"id": "stop_all_devices", "label": "停止所有设备"},
            {"id": "cancel", "label": "取消"},
        ],
    ))
    client = FakeClient(stops=[_stale_decision_error(), fresh_confirm])
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        fleet, "stop_batch", lambda _batch: pytest.fail("confirmation cannot stop"),
    )

    assert run_plans.cmd_stop_plan(_args(
        scope="all-devices", decision_token="drd_stale_stop_once",
    )) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_required"] is True
    assert payload["decision_token"] == "drd_fresh_stop_once"
    assert len(client.stop_calls) == 2
    assert client.stop_calls[0]["decision_token"] == "drd_stale_stop_once"
    assert client.stop_calls[1]["decision_token"] is None


def test_progress_keeps_local_preparation_distinct_from_server_admission(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(
            status="running",
            user_message="服务端已为这台设备预留运行位置。",
            agent_action="monitor",
        ),
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "starting",
        "startup_status": "pending",
        "workers": 2,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "preparing"
    assert payload["interaction"] == "notify"
    assert payload["agent_action"] == "monitor"
    assert payload["user_message"] == (
        "这台设备正在准备运行环境，稍后最多会同时运行 2 道题。"
        "题目尚未开始执行。无需操作。"
    )
    assert payload["agent"]["server_status"]["status"] == "running"


def test_progress_surfaces_specific_pre_start_failure_without_internal_terms(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(status="running", agent_action="monitor"),
        state={"healthy_other_devices": 0, "other_healthy": []},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "failed",
        "startup_status": "failed",
        "startup_error_code": "task_environment_update_failed",
        "startup_user_message": (
            "这台设备未能准备题目环境；已有本地文件没有被修改。"
        ),
        "startup_retryable": True,
        "returncode": 1,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "paused"
    assert payload["error_code"] == "task_environment_update_failed"
    assert payload["agent"]["requires_user_action"] is True
    assert "已有本地文件没有被修改" in payload["user_message"]
    assert all(
        word not in payload["user_message"]
        for word in ("Fleet", "batch", "provider", "refill")
    )


@pytest.mark.parametrize(
    "healthy_other_devices,expected_action",
    [(1, "monitor"), (0, "notify_only")],
)
def test_progress_surfaces_local_runner_failure_without_stopping_remote_devices(
    tmp_path, monkeypatch, capsys, healthy_other_devices, expected_action,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(
            status="running",
            user_message="整体运行仍在进行。",
            agent_action="monitor",
        ),
        state={
            "healthy_other_devices": healthy_other_devices,
            "other_healthy": (
                [{"name": "另一台设备"}] if healthy_other_devices else []
            ),
        },
        progress={"running": healthy_other_devices, "waiting": 1, "submitted": 1},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "failed",
        "returncode": 7,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "local_runner_interrupted"
    assert payload["agent_action"] == expected_action
    assert payload["decision_required"] is False
    assert payload["agent"]["local_runner"] == {
        "status": "failed",
        "returncode": 7,
    }
    assert payload["agent"]["server_status"]["agent_action"] == "monitor"
    assert payload["agent"]["next_commands"] == [{
        "id": "inspect_local_runner",
        "argv": ["dradar", "fleet", "status"],
        "interactive": False,
    }]
    assert "retry_action" not in payload.get("agent", {})
    if healthy_other_devices:
        assert "其他设备仍在继续" in payload["user_message"]
    else:
        assert payload["poll_after_seconds"] is None
        assert "目前没有其他设备继续" in payload["user_message"]


def test_upload_only_replays_exact_completed_result_before_any_runner_action(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    other_batch = "550e8400e29b41d4a716446655440000"
    pending.record(tmp_path, {
        "assignment_id": "done-this-plan",
        "batch_id": BATCH_ID,
    })
    pending.record(tmp_path, {
        "assignment_id": "done-other-plan",
        "batch_id": other_batch,
    })
    client = FakeClient()
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    replayed = []

    def retry(_client, *, batch_id=None):
        replayed.append(batch_id)
        pending.remove(tmp_path, "done-this-plan")
        return ["submitted"]

    monkeypatch.setattr(runloop, "_retry_pending_uploads", retry)
    monkeypatch.setattr(
        doctor,
        "plan_environment_issue",
        lambda _plan: pytest.fail("upload-only must run before model preflight"),
    )
    monkeypatch.setattr(
        fleet,
        "add_batch",
        lambda **_kwargs: pytest.fail("upload-only must not add a local runner"),
    )

    assert run_plans.cmd_run_plan(_args(upload_only=True)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "completed"
    assert payload["agent_action"] == "done"
    assert replayed == [BATCH_ID]
    assert client.start_calls == []
    assert {entry["assignment_id"] for entry in pending.load(tmp_path)} == {
        "done-other-plan",
    }

    # No exact result left is an idempotent success, not a reason to start.
    assert run_plans.cmd_run_plan(_args(upload_only=True)) == 0
    no_op = json.loads(capsys.readouterr().out)
    assert no_op["status"] == "completed"
    assert replayed == [BATCH_ID]
    assert client.start_calls == []


def test_upload_only_argument_conflict_fails_before_plan_exchange(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: pytest.fail("conflicting arguments must not exchange a plan"),
    )

    assert run_plans.cmd_run_plan(
        _args(upload_only=True, concurrency=1),
    ) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["error_code"] == "upload_only_argument_conflict"
    assert payload["agent_action"] == "stop"


@pytest.mark.parametrize(
    "blocked,expected_action,expected_code,expected_retryable",
    [
        (False, "recover_upload", "completed_result_upload_pending", True),
        (True, "notify_only", "completed_result_review_required", False),
    ],
)
def test_upload_only_distinguishes_retryable_and_review_required_results(
    tmp_path, monkeypatch, capsys,
    blocked, expected_action, expected_code, expected_retryable,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    entry = {"assignment_id": "done", "batch_id": BATCH_ID}
    if blocked:
        entry["upload_blocked"] = "owner_superseded"
    pending.record(tmp_path, entry)
    client = FakeClient()
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(
        runloop,
        "_retry_pending_uploads",
        lambda _client, *, batch_id=None: [
            "upload-blocked" if blocked else "upload-failed"
        ],
    )

    assert run_plans.cmd_run_plan(_args(upload_only=True)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_action"] == expected_action
    assert payload["error_code"] == expected_code
    assert payload["retryable"] is expected_retryable
    assert client.start_calls == []
    assert "pending" not in payload["user_message"]
    assert "batch" not in payload["user_message"]
    assert "Fleet" not in payload["user_message"]
    if blocked:
        assert payload["agent"]["requires_user_action"] is True
    else:
        action = payload["agent"]["next_commands"][0]
        assert action["command"] == "run"
        assert action["args"] == ["--upload-only", "--json"]
        assert action["inherit"] == ["--plan", "--server"]


def test_upload_only_json_stdout_is_one_document_with_real_retry_diagnostics(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    pending.record(tmp_path, {
        "assignment_id": "done",
        "batch_id": BATCH_ID,
        "upload_blocked": "owner_superseded",
    })
    client = FakeClient()
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )

    # Use the real retry scanner and blocked-upload path: both normally print
    # human diagnostics before the final Agent response.
    assert run_plans.cmd_run_plan(_args(upload_only=True)) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["status"] == "review_required"
    assert captured.out.count("\n") == 1
    assert "checking " not in captured.out
    assert RUN_CODE not in captured.err
    assert PLAN_TOKEN not in captured.err
    assert "drp_" not in captured.err


def test_json_command_isolates_python_fd_and_child_process_stdout(
    capsys,
):
    diagnostic = f"diagnostic-{RUN_CODE}-{PLAN_TOKEN}"

    def noisy_operation():
        print(diagnostic)
        os.write(1, (diagnostic + "\n").encode())
        subprocess.run(
            [sys.executable, "-c", f"print({diagnostic!r})"],
            check=True,
        )
        return {
            "schema_version": 1,
            **_envelope(status="running", agent_action="monitor"),
        }

    assert run_plans._run_command(_args(), noisy_operation) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["status"] == "running"
    assert captured.out.count("\n") == 1
    assert diagnostic not in captured.out
    assert RUN_CODE not in captured.err
    assert PLAN_TOKEN not in captured.err


@pytest.mark.parametrize(
    "server_status,server_action,local_status",
    [
        ("running", "monitor", "interrupted"),
        ("completed", "done", "interrupted"),
        ("stopped", "done", "stopped"),
        ("incomplete", "review_failure", None),
    ],
)
def test_progress_routes_exact_completed_result_to_upload_only_recovery(
    server_status, server_action, local_status, tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    pending.record(tmp_path, {
        "assignment_id": "done",
        "batch_id": BATCH_ID,
    })
    progress = _server_response(
        plan,
        _envelope(status=server_status, agent_action=server_action),
        state={"healthy_other_devices": 0, "other_healthy": []},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    local_item = None if local_status is None else {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": local_status,
        "returncode": 1,
    }
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: local_item)

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_action"] == "recover_upload"
    assert payload["error_code"] == "completed_result_upload_pending"
    assert payload["agent"]["completed_result_count"] == 1
    assert payload["agent"]["server_status"]["agent_action"] == server_action
    if local_status is None:
        assert "local_runner" not in payload["agent"]
    assert payload["agent"]["next_commands"][0]["args"] == [
        "--upload-only", "--json",
    ]
    for forbidden in ("pending", "batch", "Fleet", "provider", "refill"):
        assert forbidden not in payload["user_message"]


def test_progress_does_not_misreport_clean_local_stop_as_upload_recovery(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(status="running", agent_action="monitor"),
        state={"other_healthy": [{"name": "另一台设备"}]},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "stopped",
        "returncode": 0,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_action"] == "monitor"
    assert payload["error_code"] is None


def test_progress_reports_completed_natural_drain_without_prestart_failure(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(
            status="completed",
            user_message="本题已完成并停止继续领取。",
            agent_action="done",
        ),
        state={"other_healthy": []},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "stopped",
        "startup_status": "ready",
        "returncode": 0,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "completed"
    assert payload["agent_action"] == "done"
    assert payload["error_code"] is None
    assert payload["user_message"] == "本题已完成并停止继续领取。"
    assert "没有题目开始执行" not in payload["user_message"]


def test_progress_keeps_terminal_server_result_without_completed_local_result(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    progress = _server_response(
        plan,
        _envelope(status="completed", agent_action="done"),
        state={"other_healthy": []},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: {
        "batch_id": BATCH_ID,
        "plan_id": plan["plan_id"],
        "status": "interrupted",
        "returncode": 1,
    })

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "completed"
    assert payload["agent_action"] == "done"
    assert payload["error_code"] is None


def test_progress_surfaces_completed_result_that_requires_review(
    tmp_path, monkeypatch, capsys,
):
    plan = _plan()
    path, state = _state(tmp_path, plan)
    pending.record(tmp_path, {
        "assignment_id": "done",
        "batch_id": BATCH_ID,
        "upload_blocked": "owner_superseded",
    })
    progress = _server_response(
        plan,
        _envelope(status="completed", agent_action="done"),
        state={"other_healthy": []},
    )
    client = FakeClient(progress=[progress])
    monkeypatch.setattr(run_plans, "HOME", tmp_path)
    monkeypatch.setattr(
        run_plans,
        "_state_and_client",
        lambda _args: (RUN_CODE, path, state, client),
    )
    monkeypatch.setattr(fleet, "batch_status", lambda _batch_id: None)

    assert run_plans.cmd_progress_plan(_args()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "review_required"
    assert payload["agent_action"] == "notify_only"
    assert payload["error_code"] == "completed_result_review_required"
    assert payload["agent"]["requires_user_action"] is True
    assert payload["agent"]["server_status"]["agent_action"] == "done"


def test_plan_scoped_capacity_uses_identity_and_exact_inventory_not_whoami(
    monkeypatch,
):
    paths = []

    def handler(request):
        paths.append(request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""))
        if request.url.path == "/api/v1/run-plans/identity":
            return httpx.Response(200, json={
                "nickname": "测试用户", "concurrent_limit": 8, "claim_limit": 6,
            })
        if request.url.path == "/api/v1/assignment":
            active = [
                {"assignment_id": f"a-{index}", "batch_id": BATCH_ID}
                for index in range(2)
            ]
            return httpx.Response(200, json={"active": active})
        return httpx.Response(404, json={"detail": "unexpected"})

    client = ApiClient(
        "https://api.codexradar.com",
        PLAN_TOKEN,
        transport=httpx.MockTransport(handler),
        capabilities=(),
        benchmark_id="deep-swe",
        batch_id=BATCH_ID,
    )
    monkeypatch.setattr("dradar.capacity.docker_resources", lambda: (16, 64.0, ()))
    monkeypatch.setattr(
        "dradar.capacity.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=200 * 1024 ** 3),
    )
    monkeypatch.setattr(fleet, "reserved_workers", lambda **_kwargs: 1)

    snapshot = run_plans._capacity_snapshot(
        client,
        _plan(refill=True, max_tasks=20),
        {
            "account_concurrency": 8,
            "account_claim_limit": 6,
            "plan_task_limit": 20,
        },
    )

    assert "/api/v1/run-plans/identity" in paths
    assert not any(path == "/api/v1/whoami" for path in paths)
    assert any(path.startswith("/api/v1/assignment?") and "batch_id=" in path for path in paths)
    assert snapshot["account_limit"] == 6
    assert snapshot["reserved_by_other_runs"] == 1
    assert snapshot["auto_workers"] == 4


@pytest.mark.parametrize("structured", (True, False))
def test_empty_exact_inventory_is_left_for_server_no_remaining_decision(
    monkeypatch, structured,
):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/v1/run-plans/identity":
            return httpx.Response(200, json={
                "nickname": "测试用户", "concurrent_limit": 4, "claim_limit": 4,
            })
        if request.url.path == "/api/v1/assignment":
            payload = {"detail": "active batch not found"}
            if structured:
                payload["code"] = "claim_batch_not_found"
            return httpx.Response(404, json=payload)
        return httpx.Response(404)

    client = ApiClient(
        "https://api.codexradar.com",
        PLAN_TOKEN,
        transport=httpx.MockTransport(handler),
        capabilities=(),
        benchmark_id="deep-swe",
        batch_id=BATCH_ID,
    )
    monkeypatch.setattr("dradar.capacity.docker_resources", lambda: (8, 32.0, ()))
    monkeypatch.setattr(
        "dradar.capacity.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024 ** 3),
    )
    monkeypatch.setattr(fleet, "reserved_workers", lambda **_kwargs: 0)

    snapshot = run_plans._capacity_snapshot(
        client,
        _plan(task_count=2),
        {"account_concurrency": 4, "plan_task_limit": 2},
    )

    assert snapshot["held_tasks"] == 0
    assert snapshot["auto_workers"] == 2
    assert "/api/v1/run-plans/identity" in paths
    assert "/api/v1/assignment" in paths


def test_invalid_plan_or_nested_schema_version_fails_closed():
    invalid = _plan()
    invalid["assignments"] = ["not-an-assignment"]
    with pytest.raises(run_plans.RunPlanClientError) as raised:
        run_plans._validate_plan(invalid)
    assert raised.value.code == "plan_response_invalid"

    invalid_tier = _plan(points_tier="operator-private-tier")
    with pytest.raises(run_plans.RunPlanClientError) as raised:
        run_plans._validate_plan(invalid_tier)
    assert raised.value.code == "plan_response_invalid"

    unsupported_plan = _plan()
    unsupported_plan["schema_version"] = 99
    with pytest.raises(run_plans.RunPlanClientError) as raised:
        run_plans._validate_plan(unsupported_plan)
    assert raised.value.code == "plan_response_invalid"

    response = _server_response(_plan())
    response["envelope"]["schema_version"] = 99
    with pytest.raises(run_plans.RunPlanClientError) as raised:
        run_plans._agent_response_from_server(response)
    assert raised.value.code == "schema_version_unsupported"
