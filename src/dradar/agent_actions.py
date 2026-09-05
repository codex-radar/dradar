"""Closed action grammar for the trusted CLI / outer assistant boundary.

This module never executes commands. It rebuilds the small set of permitted
actions from typed values; remote command strings are not an instruction API.
Arguments containing capabilities must remain machine-only, not chat output.
"""

from __future__ import annotations

import math
import re


ACTION_CONTRACT_VERSION = 1
ACTIONS = frozenset({
    "ask_user", "monitor", "recover_upload", "recheck_plan", "notify_only",
    "start_runner", "stop_runner", "done", "stop", "retry",
    "inspect_current_run", "repair_local_environment", "setup_docker",
    "upgrade_cli", "setup_current_tool", "authenticate_current_tool",
    "install_docker", "start_docker", "select_docker_environment",
    "check_environment", "wait_and_retry", "review_failure",
})
HARNESS = frozenset({
    "codex", "dsh-minimal", "claude-code", "grok-build", "kimi-code",
    "zcode", "antigravity", "codebuddy",
})
PROVIDERS = frozenset({
    "deepseek", "claude", "grok", "kimi", "zcode", "antigravity", "codebuddy",
})


class ActionValidationError(ValueError):
    """Only fixed diagnostic text; never interpolate an untrusted value."""


def require(condition: bool, diagnostic: str) -> None:
    if not condition:
        raise ActionValidationError(diagnostic)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 4096


def positive(value: object) -> bool:
    if type(value) is int:
        return 0 < value < 2**63
    return type(value) is float and math.isfinite(value) and 0 < value < 2**63


def validate_envelope(value: dict) -> None:
    require(type(value.get("schema_version", 1)) is int and
            value.get("schema_version", 1) == 1, "协议版本不兼容")
    for key in ("decision_required", "retryable"):
        require(type(value.get(key)) is bool, "决策或重试标记类型错误")
    for key in ("status", "interaction", "user_message"):
        require(text(value.get(key)), "状态或说明缺失或类型错误")
    action = value.get("agent_action")
    require(isinstance(action, str) and action in ACTIONS, "后续动作不受支持")
    require(value.get("error_code") is None or text(value["error_code"]),
            "错误类别类型错误")
    choices = value.get("choices")
    require(isinstance(choices, list), "用户选项类型错误")
    seen = set()
    for choice in choices:
        require(isinstance(choice, dict) and text(choice.get("id")) and
                text(choice.get("label")), "用户选项缺少名称或标识")
        require(choice["id"] not in seen, "用户选项重复")
        seen.add(choice["id"])
    if value["decision_required"]:
        require(action == "ask_user" and bool(choices), "待确认状态与动作不一致")
    else:
        require(not choices and action != "ask_user", "非决策状态携带用户选项")
    poll = value.get("poll_after_seconds")
    require(poll is None or positive(poll), "等待时间无效")
    if action == "recheck_plan":
        require(positive(poll), "自动重查缺少有效等待时间")


def _argv(entry: dict) -> list[str]:
    argv = entry.get("argv")
    require(isinstance(argv, list) and bool(argv) and all(text(v) for v in argv),
            "环境检查参数类型错误")
    require(set(entry) <= {"id", "argv", "interactive", "purpose"},
            "环境检查包含未知字段")
    for key in ("id", "purpose"):
        require(key not in entry or text(entry[key]), "环境动作说明类型错误")
    require(type(entry.get("interactive")) is bool, "交互标记类型错误")
    # Return locally constructed arrays, never the caller's executable/path.
    if argv == ["codex", "login"]:
        require(entry["interactive"] is True, "登录必须由用户完成")
        return ["codex", "login"]
    if argv == ["dradar", "fleet", "status"]:
        require(entry["interactive"] is False, "状态检查交互标记错误")
        return ["dradar", "fleet", "status"]
    if (len(argv) == 4 and argv[:3] == ["dradar", "doctor", "--agent"]
            and argv[3] in HARNESS):
        require(entry["interactive"] is False, "环境检查交互标记错误")
        return ["dradar", "doctor", "--agent", argv[3]]
    if (len(argv) in {4, 5} and argv[:2] == ["dradar", "provider"]
            and argv[3] in PROVIDERS):
        if argv[2] == "setup" and len(argv) == 4:
            require(entry["interactive"] is True, "配置登录必须由用户完成")
            return ["dradar", "provider", "setup", argv[3]]
        if argv[2:] == ["status", argv[3], "--live"]:
            require(entry["interactive"] is False, "工具检查交互标记错误")
            return ["dradar", "provider", "status", argv[3], "--live"]
    raise ActionValidationError("环境动作或参数不在允许范围内")


def _replay(entry: dict, *, action: str, token: object = None) -> dict:
    require(set(entry) <= {"id", "mode", "command", "args", "inherit", "interactive"},
            "重放动作包含未知字段")
    require(entry.get("mode") == "replay_plan_command" and
            entry.get("command") == "run" and
            entry.get("inherit") == ["--plan", "--server"] and
            entry.get("interactive") is False, "重放命令或运行范围不匹配")
    args = entry.get("args")
    require(isinstance(args, list) and all(text(v) for v in args), "重放参数类型错误")
    if action == "recover_upload":
        expected = ["--upload-only", "--json"]
    elif action == "recheck_plan":
        require(len(args) == 3 and re.fullmatch(r"[1-9][0-9]{0,18}", args[1]) is not None,
                "自动重查代次无效")
        require(int(args[1]) < 2**63, "自动重查代次超出范围")
        expected = ["--recheck-generation", args[1], "--json"]
    elif action == "install":
        require(text(token) and token.startswith("drdi_"), "安装确认凭证无效")
        expected = ["--docker-install-token", token, "--json"]
    else:
        raise ActionValidationError("重放动作不受支持")
    require(args == expected, "重放参数不在允许范围内")
    result = {"mode": "replay_plan_command", "command": "run", "args": expected,
              "inherit": ["--plan", "--server"], "interactive": False}
    if "id" in entry:
        require(text(entry["id"]), "动作标识类型错误")
        result["id"] = entry["id"]
    return result


def validate_actions(response: dict) -> dict:
    """Validate locally generated followups; does not authorize new work."""
    validate_envelope(response)
    agent = response.get("agent", {})
    require(isinstance(agent, dict), "动作信息类型错误")
    agent = dict(agent)
    require(type(agent.get("schema_version", 1)) is int and
            agent.get("schema_version", 1) == 1, "动作协议版本不兼容")
    if "requires_user_action" in agent:
        require(type(agent["requires_user_action"]) is bool, "人工操作标记类型错误")
    commands = agent.get("next_commands", [])
    require(isinstance(commands, list), "后续动作列表类型错误")
    choices = agent.get("choice_actions", {})
    require(isinstance(choices, dict), "决策动作类型错误")
    require(not (commands and response["decision_required"]), "等待确认时不得附带自动动作")
    if commands:
        replay = response["agent_action"] in {"recover_upload", "recheck_plan"}
        require(not replay or len(commands) == 1, "重放动作必须唯一")
        canonical = []
        seen_commands = set()
        for entry in commands:
            require(isinstance(entry, dict), "后续动作类型错误")
            if replay:
                canonical.append(_replay(entry, action=response["agent_action"]))
            else:
                argv = _argv(entry)
                scope = agent.get("environment_scope", {})
                require(isinstance(scope, dict), "环境运行范围类型错误")
                if argv[:2] == ["dradar", "doctor"]:
                    require(scope.get("harness") == argv[3], "环境检查工具与当前运行范围不匹配")
                if argv[:2] == ["dradar", "provider"]:
                    require(scope.get("provider") == argv[3], "工具检查与当前运行范围不匹配")
                if argv == ["codex", "login"]:
                    require(scope.get("harness") == "codex", "登录工具与当前运行范围不匹配")
                require(tuple(argv) not in seen_commands, "后续环境动作重复")
                seen_commands.add(tuple(argv))
                require(not entry["interactive"] or agent.get("requires_user_action") is True,
                        "交互动作缺少人工操作屏障")
                canonical.append({**entry, "argv": argv})
        agent["next_commands"] = canonical
        if not replay:
            agent.setdefault("requires_user_action", False)
    elif response["agent_action"] in {"recover_upload", "recheck_plan"}:
        raise ActionValidationError("恢复动作缺少精确参数")
    if choices:
        require(response["decision_required"], "非决策状态不得附带决策动作")
        require(set(choices) == {c["id"] for c in response["choices"]}, "选项与决策动作不匹配")
        canonical = {}
        for choice, entry in choices.items():
            require(isinstance(entry, dict), "决策动作类型错误")
            mode = entry.get("mode")
            token = response.get("decision_token")
            if mode == "no_command":
                require(entry == {"mode": "no_command", "args": []}, "取消动作包含参数")
                canonical[choice] = {"mode": "no_command", "args": []}
                continue
            require(choice != "cancel", "取消选项不得执行命令")
            if mode == "replay_plan_command":
                require(choice == "install", "仅安装确认允许基础命令重放")
                canonical[choice] = _replay(entry, action="install", token=token)
                continue
            require(mode == "replay_current_command_with_args" and
                    set(entry) == {"mode", "args"}, "决策动作不受支持")
            require(text(token), "决策凭证缺失")
            args = entry.get("args")
            expected = ["--decision-token", token]
            if choice == "install":
                require(token.startswith("drdi_"), "安装确认凭证无效")
                expected = ["--docker-install-token", token]
            elif choice in {"use_recommended", "keep_requested"}:
                key = "recommended_concurrency" if choice == "use_recommended" else "requested_concurrency"
                count = agent.get(key)
                require(type(count) is int and 1 <= count <= 40, "确认的并发数无效")
                expected = ["--concurrency", str(count), "--decision-token", token]
            require(args == expected, "决策参数不在允许范围内")
            canonical[choice] = {"mode": mode, "args": expected}
        agent["choice_actions"] = canonical
    elif response["decision_required"]:
        raise ActionValidationError("用户决策缺少可信动作映射")
    agent["schema_version"] = 1
    agent["action_contract_version"] = ACTION_CONTRACT_VERSION
    return agent
