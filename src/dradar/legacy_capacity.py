"""Read one reservation page using an existing identity, without recovery."""

from __future__ import annotations

import hashlib
import json
import re
import secrets

from . import identity, local_config, run_plans
from .api_client import ApiClient, ApiError, normalize_batch_id
from .providers import RUNNER_RESERVATION_CAPABILITY


class InventoryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _identifier(value, *, nullable=False, maximum=64):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{maximum}}}", value):
        raise ValueError("invalid identifier")
    return value


def _hex(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid digest")
    return value


def _cursor(value):
    return "" if value is None or value == "" else _identifier(value)


def _existing_client(args):
    """Do not use plan loaders that exchange, renew, retire, or create IDs."""
    code = getattr(args, "plan", None)
    explicit_server = getattr(args, "server", None)
    capabilities = [RUNNER_RESERVATION_CAPABILITY]
    if code is None:
        try:
            config = local_config._load_config()
        except SystemExit:
            raise InventoryError("inventory_credentials_unavailable", "原账号配置无法读取；请保留原文件。") from None
        if not isinstance(config, dict) or not isinstance(config.get("token"), str) or not config["token"]:
            raise InventoryError("inventory_credentials_unavailable", "缺少原账号凭证；库存查询不会自动注册。")
        if config["token"].startswith("drp_"):
            raise InventoryError("inventory_plan_evidence_missing", "计划凭证需要对应的原计划记录；请使用原 --plan 范围查询。")
        server = run_plans.validate_server_url(config.get("server"))
        if explicit_server and run_plans.validate_server_url(explicit_server) != server:
            raise InventoryError("server_scope_mismatch", "查询地址与原账号保存的站点不同。")
        # The default capability probe may prepare provider authentication.
        # This GET only needs protocol support; keep the saved config unchanged.
        client = identity._client({**config, "server": server, "client_capabilities": capabilities}, auto_register=False)
        return client, {"kind": "account"}, (config["token"],)

    code = run_plans._validate_run_code(code)
    digest = run_plans._run_code_digest(code)
    root = local_config.HOME / run_plans.PLAN_DIR
    matches = []
    if root.is_dir() and not root.is_symlink():
        for path in root.glob("plan-*.json"):
            state = run_plans._read_private_json(path)
            if state and isinstance(state.get("run_code_hash"), str) and secrets.compare_digest(state["run_code_hash"], digest):
                matches.append(state)
    if len(matches) != 1:
        raise InventoryError("inventory_plan_evidence_missing", "无法唯一读取原计划凭证；请保留原记录，库存查询不会 exchange。")
    state = matches[0]
    if (type(state.get("schema_version")) is not int or state["schema_version"] != 1
            or state.get("credential_kind") != "run_plan_v1"
            or not isinstance(state.get("token"), str) or not state["token"].startswith("drp_")
            or not 5 <= len(state["token"]) <= 512):
        raise InventoryError("inventory_credentials_unavailable", "原计划凭证不完整；请保留原文件。")
    server = run_plans.validate_server_url(state.get("server"))
    if explicit_server and run_plans.validate_server_url(explicit_server) != server:
        raise InventoryError("server_scope_mismatch", "查询地址与原计划保存的站点不同。")
    plan = state.get("plan")
    if not isinstance(plan, dict) or any(state.get(a) != plan.get(b) for a, b in (
        ("plan_id", "plan_id"), ("batch_id", "batch_id"), ("benchmark", "benchmark_id"),
    )):
        raise InventoryError("inventory_plan_evidence_missing", "原计划范围无法核对；请保留原文件。")
    plan_id = _identifier(state.get("plan_id"), maximum=160)
    batch = normalize_batch_id(state.get("batch_id"))
    if batch is None or not isinstance(state.get("benchmark"), str) or not state["benchmark"]:
        raise InventoryError("inventory_plan_evidence_missing", "原计划范围不完整；请保留原文件。")
    client = ApiClient(server, state["token"], benchmark_id=state["benchmark"], batch_id=batch, capabilities=capabilities)
    return client, {"kind": "plan", "plan_id": plan_id, "batch_id": batch}, (code, state["token"])


def _classification(source, target, *, quarantine=False):
    # Older servers have no classification: absence never grants an exemption.
    if "classification" in source:
        allowed = {"historical_unverified", "cleanup_required" if quarantine else "current_reservation"}
        if source["classification"] not in allowed:
            raise ValueError("unknown classification")
        target["classification"] = source["classification"]
    if not quarantine:
        if ("classification" in source) != ("counts_toward_capacity" in source):
            raise ValueError("incomplete capacity classification")
        if "counts_toward_capacity" in source:
            if type(source["counts_toward_capacity"]) is not bool:
                raise ValueError("invalid capacity classification")
            target["counts_toward_capacity"] = source["counts_toward_capacity"]


def _page(value, scope):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported inventory schema")
    reservations, quarantines = value["reservations"], value["migration_quarantines"]
    if any(not isinstance(rows, list) or len(rows) > 200 for rows in (reservations, quarantines)):
        raise ValueError("invalid inventory list")
    output = {"schema_version": 1, "read_only": True, "scope": scope,
              "exit_evidence": "not_assessed", "reservations": [], "migration_quarantines": []}
    for row in reservations:
        if not isinstance(row, dict):
            raise ValueError("invalid reservation")
        clean = {key: _identifier(row[key], nullable=key in {"plan_id", "assignment_id"})
                 for key in ("session_id", "batch_id", "plan_id", "assignment_id")}
        clean["device_id_hash"] = _hex(row["device_id_hash"], nullable=True)
        generation = row["device_generation"]
        if generation is not None and (type(generation) is not int or generation < 0):
            raise ValueError("invalid device generation")
        if (type(row["closed"]) is not bool or type(row["reservation_protocol"]) is not int
                or row["reservation_protocol"] not in (-1, 0, 1) or row["state"] != "exit_unknown"):
            raise ValueError("invalid reservation state")
        clean.update({key: row[key] for key in ("device_generation", "closed", "reservation_protocol", "state")})
        if scope["kind"] == "plan" and (clean["plan_id"] != scope["plan_id"] or clean["batch_id"] != scope["batch_id"]):
            raise ValueError("reservation outside original plan")
        _classification(row, clean)
        output["reservations"].append(clean)
    for row in quarantines:
        if not isinstance(row, dict):
            raise ValueError("invalid quarantine")
        clean = {key: _hex(row[key], nullable=key == "device_id_hash")
                 for key in ("quarantine_id", "snapshot_sha256", "device_id_hash")}
        clean["plan_id"] = _identifier(row["plan_id"], nullable=True)
        ids = row["session_ids"]
        if not isinstance(ids, list) or not ids:
            raise ValueError("missing quarantine membership")
        clean["session_ids"] = [_identifier(item) for item in ids]
        if len(set(ids)) != len(ids) or hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode()).hexdigest() != clean["snapshot_sha256"]:
            raise ValueError("quarantine snapshot mismatch")
        if scope["kind"] == "plan" and clean["plan_id"] != scope["plan_id"]:
            raise ValueError("quarantine outside original plan")
        _classification(row, clean, quarantine=True)
        output["migration_quarantines"].append(clean)
    for rows, key in ((output["reservations"], "session_id"), (output["migration_quarantines"], "quarantine_id")):
        if len({row[key] for row in rows}) != len(rows):
            raise ValueError("duplicate inventory membership")
    for cursor in ("next_after", "next_quarantine_after"):
        output[cursor] = _identifier(value[cursor], nullable=True)
    return output


def cmd_legacy_inventory(args) -> int:
    """One read-only GET; cursors are returned for the caller's next query."""
    try:
        after = _cursor(getattr(args, "after", ""))
        quarantine_after = _cursor(getattr(args, "quarantine_after", ""))
    except ValueError:
        return _error(args, "inventory_cursor_invalid", "库存游标无效；请使用上页返回的原游标。")
    try:
        client, scope, secret_values = _existing_client(args)
        page = client.runner_reservations(after=after, quarantine_after=quarantine_after)
        result = _page(page, scope)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if any(secret and secret in encoded for secret in secret_values):
            raise ValueError("credential in inventory")
    except InventoryError as exc:
        return _error(args, exc.code, str(exc))
    except run_plans.RunPlanClientError as exc:
        return _error(args, exc.code, "原身份或站点信息无法核验；请保留原记录。")
    except ApiError as exc:
        if exc.status_code in (404, 426):
            return _error(args, "inventory_unsupported", "Server 未支持当前库存查询；请保留原记录，不据此判断退出或释放。")
        if exc.status_code in (401, 403):
            return _error(args, "inventory_access_denied", "原凭证未获准读取库存；请保留原记录。")
        return _error(args, "inventory_query_failed", "库存查询未完成；原占用与退出状态仍待核验。")
    except (OSError, ValueError, TypeError, KeyError, SystemExit):
        return _error(args, "inventory_unverifiable", "本地记录或 Server 库存响应无法核验；请保留原记录。")
    if getattr(args, "json", False):
        print(encoded)
    else:
        print(f"本页库存：{len(result['reservations'])} 条 reservation，{len(result['migration_quarantines'])} 组旧历史。")
        classified = sum(row.get("classification") == "historical_unverified" for row in result["reservations"])
        current = sum(row.get("classification") == "current_reservation" for row in result["reservations"])
        unspecified = sum("classification" not in row for row in result["reservations"])
        print(f"历史未核验：{classified}；当期占用：{current}；分类未提供：{unspecified}。")
        print("当期占用以 reservation 为准，不表示账号已满；库存及 closed 状态不证明物理退出或容量释放。")
        print(f"next_after={result['next_after'] or '-'}  next_quarantine_after={result['next_quarantine_after'] or '-'}")
    return 0


def _error(args, code, message):
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": 1, "read_only": True, "status": "error",
                          "error_code": code, "message": message}, ensure_ascii=False))
    else:
        print(f"{code}: {message}")
    return 1
