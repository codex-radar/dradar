"""Local, bounded plan controller; response command arrays are never executed.

The JSON API remains available to older callers. This path invokes Python
operations directly, keeps capabilities inside the process, and renders only
human-facing progress. A disconnected observer does not imply a stopped job.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sys
import time
from types import SimpleNamespace

from . import run_plans as plans

MAX_SECONDS = 24 * 60 * 60
MAX_RECOVERIES = 3
MAX_RECHECKS = 20
_COUNTS = {"total", "running", "completed", "graded", "passed", "failed", "pending", "invalid"}
_TERMINAL = {"completed", "done", "stopped", "no_remaining"}
_MONITOR = {"started", "already_running", "running", "preparing", "waiting", "stopping"}
CHOICES = {"install", "join_existing", "recover_stale", "use_recommended", "keep_requested", "cancel"}


def _base(args, **overrides):
    return SimpleNamespace(
        plan=args.plan, server=args.server, json=True, _session=True,
        concurrency=overrides.get("concurrency"),
        decision_token=overrides.get("decision_token"),
        docker_install_token=overrides.get("docker_install_token"),
        recheck_generation=overrides.get("recheck_generation"),
        upload_only=overrides.get("upload_only", False),
    )


def _public(value, args, response):
    result = str(value)
    for secret in (args.plan, getattr(args, "decision_token", None),
                   getattr(args, "docker_install_token", None), response.get("decision_token")):
        if isinstance(secret, str) and secret:
            result = result.replace(secret, "[redacted]")
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]", "", result)


def _confirmed_args(current, response, choice, *, state=None):
    """Fixed code paths, never choice_actions.args or label-derived flags."""
    token = response.get("decision_token")
    if not isinstance(token, str) or not token:
        raise plans.RunPlanClientError("session_decision_invalid", "确认信息已失效。")
    state = plans._session_local_state(current) if state is None else state
    if choice == "install" and response.get("decision") == "install_recommended_docker":
        pending = state.get("pending_docker_install") or {}
        if pending.get("token_hash") != hashlib.sha256(token.encode()).hexdigest():
            raise plans.RunPlanClientError("session_decision_invalid", "安装确认信息已失效。")
        return _base(current, concurrency=current.concurrency,
                     decision_token=current.decision_token, docker_install_token=token)
    if choice in {"use_recommended", "keep_requested"}:
        pending = state.get("pending_local_capacity") or {}
        if pending.get("token_hash") != hashlib.sha256(token.encode()).hexdigest():
            raise plans.RunPlanClientError("session_decision_invalid", "并发确认信息已失效。")
        count = pending.get("recommended" if choice == "use_recommended" else "requested")
        if type(count) is not int or not 1 <= count <= 40:
            raise plans.RunPlanClientError("session_decision_invalid", "确认的并发数无效。")
        return _base(current, concurrency=count, decision_token=token)
    pending = state.get("pending_decision") or {}
    if (pending.get("command") == "run" and
            pending.get("decision") in {"join_existing", "recover_stale"} and
            response.get("decision") == pending["decision"] and choice == pending["decision"]):
        return _base(current, concurrency=current.concurrency, decision_token=token)
    raise plans.RunPlanClientError("session_decision_unsupported", "当前选择尚不受此版本支持；没有执行额外操作。")


def _issue_confirmation(current, response, *, entry_args=None):
    """Expose a local request ID, never executable args or a decision token."""
    def issue(path, state):
        choices = response["choices"]
        if any(choice["id"] not in CHOICES for choice in choices):
            raise plans.RunPlanClientError("session_decision_unsupported", "当前确认类型尚不受支持。")
        # Verify every non-cancel path against current local authority now;
        # underlying one-use/expiry checks run again when the choice is used.
        for choice in choices:
            if choice["id"] != "cancel":
                _confirmed_args(current, response, choice["id"], state=state)
        request = "dsc_" + secrets.token_hex(16)
        record = {
            "request": request, "expires_at": time.time() + 300,
            "generation": plans._intent_generation(state),
            "server": state.get("server"), "plan_id": state.get("plan_id"),
            "batch_id": state.get("batch_id"), "decision": response.get("decision"),
            "token": response.get("decision_token"),
            "choices": [choice["id"] for choice in choices],
            "concurrency": current.concurrency, "previous_decision": current.decision_token,
            "entry_concurrency": getattr(entry_args or current, "concurrency", None),
        }
        state["pending_session_confirmation"] = record
        plans._atomic_json(path, state)
        return {"version": 1, "request": request, "choices": [
            {"id": choice["id"], "label": _public(choice["label"], current, response)}
            for choice in choices]}
    return plans._with_session_state(current, issue)


def _consume_confirmation(args):
    def consume(path, state):
        request = getattr(args, "confirmation", None)
        choice = getattr(args, "choice", None)
        record = state.get("pending_session_confirmation")
        valid = (
            isinstance(request, str) and re.fullmatch(r"dsc_[0-9a-f]{32}", request) and
            isinstance(choice, str) and choice in CHOICES and isinstance(record, dict) and
            record.get("request") == request and type(record.get("expires_at")) in (int, float) and
            math.isfinite(record["expires_at"]) and record["expires_at"] > time.time() and
            type(record.get("generation")) is int and record["generation"] == plans._intent_generation(state) and
            all(record.get(key) == state.get(key) for key in ("server", "plan_id", "batch_id")) and
            record.get("entry_concurrency") == getattr(args, "concurrency", None) and
            isinstance(record.get("choices"), list) and choice in record["choices"]
        )
        if not valid:
            raise plans.RunPlanClientError("session_confirmation_expired", "确认已过期、已使用或范围发生变化；未执行确认动作。")
        current = _base(args, concurrency=record["concurrency"], decision_token=record["previous_decision"])
        confirmed = None if choice == "cancel" else _confirmed_args(
            current, {"decision": record["decision"], "decision_token": record["token"]}, choice, state=state)
        if choice == "cancel":
            token_hash = hashlib.sha256(str(record.get("token") or "").encode()).hexdigest()
            for key in ("pending_docker_install", "pending_local_capacity"):
                pending = state.get(key)
                if isinstance(pending, dict) and pending.get("token_hash") == token_hash:
                    state[key] = None
            pending = state.get("pending_decision")
            if isinstance(pending, dict) and pending.get("command") == "run" and pending.get("decision") == record["decision"]:
                state["pending_decision"] = None
        state["pending_session_confirmation"] = None
        plans._atomic_json(path, state)
        return confirmed
    return plans._with_session_state(args, consume)


def follow_plan(args, *, run=None, progress=None, sleep=None, clock=None,
                interactive=None, read_choice=None, emit=None, max_seconds=MAX_SECONDS):
    """Keep waits/input outside admission locks. Dependency seams are test-only."""
    run = run or plans._run_plan_operation
    progress = progress or plans._progress_plan_operation
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    emit = emit or (lambda message: print(message, flush=True))
    read_choice = read_choice or input
    interactive = sys.stdin.isatty() if interactive is None else interactive
    english = getattr(args, "locale", "zh-CN") == "en-US"
    def say(zh, en):
        emit(en if english else zh)
    def pause():
        say("本地跟进已暂停；这不表示后台任务已经停止。",
            "Local follow-up is paused; this does not mean background work has stopped.")
    if getattr(args, "json", False):
        say("持续模式使用人话进度，不能同时指定 --json。", "Follow mode renders human progress and cannot be combined with --json.")
        return 2
    if any(getattr(args, name, None) for name in
           ("upload_only", "decision_token", "docker_install_token", "recheck_generation")):
        say("持续入口不能携带旧的恢复或确认参数。", "The session entry cannot inherit old recovery or decision arguments.")
        return 2
    current = _base(args, concurrency=getattr(args, "concurrency", None))
    operation = run
    deadline = clock() + max_seconds
    recoveries = retries = 0
    seen_generations = set()
    last_view = None
    last_notice = clock()
    say("正在按网页保存的范围检查环境并运行；安装、登录和跨设备加入仍需明确确认。",
        "Checking and running the saved website plan; installation, sign-in and device joining still require confirmation.")
    try:
        if getattr(args, "confirmation", None) is not None or getattr(args, "choice", None) is not None:
            current = _consume_confirmation(args)
            if current is None:
                say("已取消本次确认，不执行后续动作。", "Confirmation cancelled; no follow-up action will run.")
                pause()
                return 0
        while clock() < deadline:
            code, response = operation(current)
            plans._validate_envelope(response)
            agent = response.get("agent") or {}
            if not isinstance(agent, dict):
                raise plans.RunPlanClientError("session_response_invalid", "进度信息格式无效。")
            if (type(agent.get("schema_version", 1)) is not int or agent.get("schema_version", 1) != 1 or
                    ("requires_user_action" in agent and type(agent["requires_user_action"]) is not bool)):
                raise plans.RunPlanClientError("session_response_invalid", "进度版本或人工确认标记无效。")
            counters = agent.get("progress") or {}
            counters = {key: value for key, value in counters.items()
                        if key in _COUNTS and type(value) is int and value >= 0} if isinstance(counters, dict) else {}
            message = _public(response["user_message"], current, response)
            view = (response["status"], message, tuple(sorted(counters.items())))
            if view != last_view or clock() - last_notice >= 60:
                emit(message + (" " + ", ".join(f"{k}={v}" for k, v in sorted(counters.items())) if counters else ""))
                last_view, last_notice = view, clock()
            action = response["agent_action"]
            if response["decision_required"]:
                if not interactive:
                    confirmation = _issue_confirmation(current, response, entry_args=args)
                    say("请在对话中明确选择。收到选择后，仅重跑原固定入口并附加 --confirmation=<确认编号> 与 --choice=<所选id>；不要自动批准。",
                        "Choose explicitly in the conversation. Only after that choice, repeat the original entry with --confirmation=<request> and --choice=<selected id>; never approve automatically.")
                    emit("DRADAR_CONFIRMATION " + json.dumps(confirmation, ensure_ascii=False, separators=(",", ":")))
                    pause()
                    return 2
                choices = response["choices"]
                for index, choice in enumerate(choices, 1):
                    emit(f"{index}. " + _public(choice["label"], current, response))
                answer = read_choice("Choice / 请选择编号: ").strip()
                if not answer.isdecimal() or not 1 <= int(answer) <= len(choices):
                    say("未取得明确选择；没有执行确认动作。", "No unambiguous choice; no confirmation action was performed.")
                    pause()
                    return 2
                choice = choices[int(answer) - 1]["id"]
                if choice == "cancel":
                    say("已取消本次确认，不执行后续动作。", "Confirmation cancelled; no follow-up action will run.")
                    pause()
                    return 0
                current = _confirmed_args(current, response, choice)
                operation = run
                continue
            if agent.get("requires_user_action") is True:
                say("请在自己的终端完成提示的安装、登录或环境处理，再运行同一入口；不会自动扩大权限。",
                    "Complete the indicated installation, sign-in or environment action in your terminal, then run the same entry; permissions will not expand automatically.")
                pause()
                return 2
            if action in {"done", "stop_runner"} and response["status"] in _TERMINAL:
                return code
            if action == "monitor" and response["status"] in _MONITOR:
                current, operation = _base(args), progress
                retries = 0
            elif action == "recover_upload":
                recoveries += 1
                if recoveries > MAX_RECOVERIES:
                    say("补交重试已达到本轮上限，保留结果待稍后处理。", "Upload recovery reached this session's retry limit; results remain available.")
                    pause()
                    return 1
                current, operation = _base(args, upload_only=True), run
            elif action == "recheck_plan":
                state = plans._session_local_state(current)
                generation = agent.get("intent_generation")
                if (len(seen_generations) >= MAX_RECHECKS or
                        type(generation) is not int or generation < 1 or generation in seen_generations or
                        generation != state.get("pending_recheck_generation") or
                        generation != state.get("intent_generation")):
                    raise plans.RunPlanClientError("session_recheck_stale", "自动检查信息已失效，没有重启或扩大运行。")
                seen_generations.add(generation)
                current, operation = _base(args, recheck_generation=generation), run
            elif action == "retry" and response["retryable"] is True and retries < MAX_RECOVERIES:
                retries += 1
            else:
                # Includes unknown transitions, expiry and failures. Never turn
                # a prose instruction or response argv into executable work.
                pause()
                return code or 1
            interval = response.get("poll_after_seconds") or 45
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(remaining, max(30, min(300, interval))))
        say("本轮跟进已到时间上限，可稍后重新运行同一入口查看。", "This follow-up reached its time limit; rerun the same entry to check later.")
        pause()
        return 2
    except (KeyboardInterrupt, EOFError):
        pause()
        return 130
    except plans.RunPlanClientError as exc:
        emit(_public(exc.user_message, current, {}))
        pause()
        return 1
    except Exception as exc:
        # A controller bug or corrupt local data must not print a traceback
        # containing paths/capabilities or be misreported as a stopped job.
        say("本地跟进遇到异常（" + type(exc).__name__ + "），请联系支持查看脱敏诊断。",
            "Local follow-up failed (" + type(exc).__name__ + "); contact support for sanitized diagnostics.")
        pause()
        return 1
