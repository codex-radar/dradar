"""Opt-in candidate entry: python -m dradar.v2. V1 commands stay intact."""
from __future__ import annotations
import argparse
import json
import hashlib
from pathlib import Path
import sys
import time
from ..local_config import HOME, runtime_config
from ..scrub import scrub_text
from .client import Client, ProtocolError, RemoteError, TransportUnknown
from .journal import Journal, JournalConflict
from .artifacts import ArtifactError
from ..assignment_lock import AssignmentBusy
from .presentation import run_view
from .runtime import CodexRuntime, RuntimeUnavailable
from .scheduler import Controller, ExecutionBlocked, stop_saved_run
from .selection import Catalog, questions, resolve

SCHEMA = {"schema_version": 2, "protocol": "on-demand-v2", "candidate": True,
          "commands": ["catalog", "select", "run", "progress", "upload-only", "supplement-result", "stop"],
          "selection_fields": ["benchmark", "model", "total_count", "concurrency"],
          "execution_barrier": "worker_registered_start_ack_durable_launch_intent", "entry": "python -m dradar.v2"}

def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))

def main(argv=None, *, client_factory=Client, runtime_factory=CodexRuntime, poll_seconds=2):
    parser = argparse.ArgumentParser(prog="dradar-v2", description="DRadar on-demand v2 isolated candidate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schema")
    install = sub.add_parser("install-skill")
    approval = sub.add_parser("host-approval", help="respond to one explicitly reviewed owned request")
    approval.add_argument("--state-root",required=True,type=Path)
    approval.add_argument("--assignment-id",required=True)
    approval.add_argument("--request-id",required=True)
    approval.add_argument("--decision",required=True,choices=["accept","decline"])
    install.add_argument("--expected-sha256", required=True)
    install.add_argument("--destination", type=Path, default=Path.home()/".codex/skills/dradar-v2")
    for name in SCHEMA["commands"]:
        p = sub.add_parser(name)
        p.add_argument("--state-root", required=True, type=Path, help="private v2 run directory; reuse for recovery")
        p.add_argument("--server", help="must match configured login and saved run server")
        p.add_argument("--json", action="store_true", help="structured truthful progress (currently default)")
        if name == 'supplement-result':
            p.add_argument('--assignment-id', required=True)
            p.add_argument('--original-result-sha256', required=True)
            p.add_argument('--exit-evidence-sha256', required=True)
        if name in {"select", "run"}:
            p.add_argument("--benchmark")
            p.add_argument("--model")
            p.add_argument("--effort")
            p.add_argument("--total-count", type=int)
            p.add_argument("--concurrency", type=int)
        if name == "run":
            p.add_argument("--host-runtime-binding",type=Path,help="explicit trusted host0.160 binding; no file-auth fallback")
            p.add_argument("--host-runtime-sha256",help="trusted runtime binding SHA256 from fixed release packet")
            p.add_argument("--cache-platform", help="explicit Linux platform for approved immutable base binding")
            p.add_argument("--public-base", action="append", help="explicit approved public image reference; repeatable")
            p.add_argument("--image-cache-root", type=Path, help="user-shared cache root, never an authentication/output directory")
            p.add_argument("--tasks-root", type=Path, required=True, help="preinstalled verified task source; never updated in place")
    args = parser.parse_args(argv)
    if args.command == "schema":
        from .skill_install import packaged_skill
        from .host_contract import AUTH_RUNTIME, CAPABILITY, CONFIG_VERSION, VERSION, BENCHMARK_POLICIES, EFFORTS, WIRE_CAPABILITIES, SERVER_CATALOG_VERSION, MIXED_SCHEMA, MIXED_CONFIG_VERSION, MIXED_WIRE_CAPABILITIES, PER_TASK_WIRE_CAPABILITIES
        from .mixed_pool import POOL, MEMBERS_SHA256, is_historical_pool
        emit({**SCHEMA,"local_commands":["schema","install-skill","host-approval"],"skill_sha256":hashlib.sha256(packaged_skill()).hexdigest(),"host_runtime":{"version":VERSION,"auth_runtime":AUTH_RUNTIME,"capability":CAPABILITY,"wire_capabilities":list(WIRE_CAPABILITIES),"server_catalog_version":SERVER_CATALOG_VERSION,"runtime_config_version":CONFIG_VERSION,"model":"gpt-6.1-sol","supported_efforts":list(EFFORTS),"live_validated_efforts":["low"],"max_parallel":2,"versioned_benchmark_policy_ids":BENCHMARK_POLICIES,"explicit_binding_required":True,"mixed_pool":{"benchmark":POOL,"members_sha256":MEMBERS_SHA256,"task_count":64,"readiness":"selected_assignment_only","binding_schema":MIXED_SCHEMA,"runtime_config_version":MIXED_CONFIG_VERSION,"wire_capabilities":list(PER_TASK_WIRE_CAPABILITIES),"live_validated":False}}})
        return 0
    if args.command == "install-skill":
        from .skill_install import install_skill
        try:
            emit(install_skill(args.destination,args.expected_sha256))
            return 0
        except (ValueError,OSError) as exc:
            emit({"status":"blocked","user_message":scrub_text(str(exc))})
            return 3
    if args.command == "host-approval":
        try:
            import re
            from .host_contract import private_json
            from .host_runtime import control_request
            if not re.fullmatch(r"[a-f0-9]{32}",args.assignment_id):
                raise RuntimeUnavailable("exact owned assignment required")
            journal=Journal(args.state_root)
            if journal.execution(args.assignment_id) is None:
                raise RuntimeUnavailable("original execution fence required")
            record=journal.value("host_approval:"+args.assignment_id)
            if not record:raise RuntimeUnavailable("no outstanding owned approval")
            pending=json.loads(private_json(Path(record)))
            try:ident=json.loads(args.request_id)
            except ValueError:ident=args.request_id
            if pending.get("id")!=ident or "requestApproval" not in pending.get("method",""):
                raise RuntimeUnavailable("exact outstanding request ID required")
            cfg=json.loads(private_json(journal.root/"runtime"/args.assignment_id/"host"/"controller.json"))
            result=control_request(cfg["control_socket"],{"op":"approval:respond","response":{"id":ident,"result":{"decision":args.decision}}})
            Path(record).unlink()
            emit(result);return 0
        except (ValueError,OSError,RuntimeUnavailable) as exc:
            emit({"status":"blocked","user_message":scrub_text(str(exc))});return 3
    client = None
    try:
        cfg = runtime_config()
        server = args.server or cfg.get("server")
        if not server or not cfg.get("token"):
            raise RuntimeUnavailable("请先通过既有 dradar login 登录；v2不创建另一个账号")
        if args.server and cfg.get("server") and args.server.rstrip("/") != cfg["server"].rstrip("/"):
            raise RuntimeUnavailable("所选站点与已有账号登录站点不一致")
        journal = Journal(args.state_root)
        client = client_factory(server, cfg["token"], journal)
        if args.command == 'supplement-result':
            from .completion_correction import supplement_result
            receipt = supplement_result(client, args.assignment_id, args.original_result_sha256, args.exit_evidence_sha256)
            emit({'status': 'completion_correction_accepted', 'assignment_id': receipt['assignment_id'],
                  'execution_id': receipt['execution_id'], 'submission_id': receipt['submission_id'],
                  'result_sha256': receipt['result_sha256'], 'grading_state': receipt['grading_state'],
                  'completion_correction': receipt['completion_correction'],
                  'original_outcome': 'failed', 'accepted_outcome': 'completed',
                  'model_started': False, 'run_remains_stopped': True})
            return 0
        host = args.command == 'run' and bool(args.host_runtime_binding or args.host_runtime_sha256)
        if host:
            from .host_contract import load_binding
            binding = load_binding(args.host_runtime_binding, args.host_runtime_sha256)
            from .host_contract import MIXED_SCHEMA
            if binding['schema'] == MIXED_SCHEMA:
                client.offer_bound_host_runtime(mixed=True,per_task=True)
            else:
                client.offer_bound_host_runtime()
        if args.command in {"catalog", "select", "run"}:
            bootstrap = client.bootstrap()
            if args.command == "catalog":
                emit({"schema_version": 2, "candidate": True, "benchmarks": bootstrap["benchmarks"], "limits": bootstrap["limits"]})
                return 0
            catalog = Catalog.from_bootstrap(bootstrap)
            chosen = {k: getattr(args, k) for k in ("benchmark", "model", "effort", "concurrency") if getattr(args, k) is not None}
            if args.total_count is not None: chosen["total"] = args.total_count
            if args.command == "run" and journal.value("configuration") is not None:
                saved = json.loads(journal.value("configuration"))
                inherited = {"benchmark": saved["benchmark"], "model": saved["model"], "effort": saved["effort"], "total": saved["total_count"], "concurrency": saved["concurrency"]}
                chosen = {**inherited, **chosen}
            missing = questions(chosen, catalog)
            if missing:
                if args.command == 'run' and any(q['field'] != 'benchmark' for q in missing):
                    raise RuntimeUnavailable('Existing approved Harness-model/effort and total/concurrency plan is required from the launcher or saved configuration; user claim chooses only the library, never a cell. No guessed model/effort/budget default.')
                emit({"status": "selection_required", "questions": missing})
                return 2
            from .mixed_pool import is_historical_pool
            if args.command=='run' and is_historical_pool(chosen.get('benchmark'),json.loads(journal.value('mixed_pool_scope') or 'null')):
                raise RuntimeUnavailable('旧68题范围已停止新执行；保留原STATE用stop/progress/upload-only，按当前64题新建运行')
            selected = resolve(chosen, catalog)
            from .mixed_pool import POOL
            if args.command == 'run' and selected.benchmark == POOL and (not host or binding['schema'] != MIXED_SCHEMA):
                raise RuntimeUnavailable('Current64 requires explicit trusted per-task host binding; no legacy fallback')
            if host:
                from .host_contract import require_server_library
                require_server_library(bootstrap, selected.benchmark, selected.model, selected.effort)
            configuration = {"benchmark": selected.benchmark, "model": selected.model, "effort": selected.effort,
                             "agent": "codex", "total_count": selected.total, "concurrency": selected.concurrency}
            if args.command == "select":
                emit({"status": "selection_ready", "configuration": configuration, "claims_created": 0})
                return 0
        else:
            raw = journal.value("configuration")
            if raw is None: raise RuntimeUnavailable("没有原v2运行日志，请保留原成果目录")
            configuration = json.loads(raw)
        if args.command == "stop":
            stop_saved_run(client)
            emit({"status": "stopped_claiming", "user_message": "已停止此运行的新领取和开始；原控制器将中断本批次的执行并保留成果，需核实际退出"})
            return 0
        image_options = None
        if args.command == "run" and any((args.cache_platform, args.public_base, args.image_cache_root)):
            if not all((args.cache_platform, args.public_base, args.image_cache_root)):
                raise RuntimeUnavailable("公共镜像绑定需明确platform、public-base及共享cache-root")
            image_options = {"platform": args.cache_platform, "public_references": args.public_base, "cache_root": args.image_cache_root}
        runtime = None
        if args.command == "run":
            from .host_contract import BENCHMARK_POLICIES
            host = bool(args.host_runtime_binding or args.host_runtime_sha256)
            from .mixed_pool import POOL
            if (configuration['benchmark'] in BENCHMARK_POLICIES or configuration['benchmark']==POOL) and not host:
                raise RuntimeUnavailable("版本化新题库需要明确host0.160运行绑定；不得回退旧auth路径")
            if host and (not args.host_runtime_binding or not args.host_runtime_sha256):
                raise RuntimeUnavailable("host运行绑定需完整路径与可信SHA256")
            options={"public_image_options":image_options} if image_options else {}
            factory=runtime_factory
            if host:
                from .host_contract import EFFORTS
                if configuration['model']!='gpt-6.1-sol' or configuration['effort'] not in EFFORTS:
                    raise RuntimeUnavailable('host候选保留Sol6.1所有既定档位；不替换模型或档位')
                if configuration['concurrency']>2:
                    raise RuntimeUnavailable("host模式最多2并发；不得静默改变用户选择")
                from .host_runtime import HostCodexRuntime
                factory=HostCodexRuntime if runtime_factory is CodexRuntime else runtime_factory
                options.update(host_runtime_binding=args.host_runtime_binding,host_runtime_sha256=args.host_runtime_sha256)
            runtime=factory(journal,args.tasks_root,**options)
        controller = Controller(client, runtime, configuration)
        if args.command == "progress":
            try:
                emit(run_view(controller.progress_snapshot()))
                return 0
            finally:
                controller.pool.shutdown()
        with controller.ownership():
            if args.command == "upload-only":
                receipts = controller.upload_only()
                emit({"status": "upload_recovery", "accepted_results": len(receipts), "model_started": False})
                return 0
            controller.initialize()
            emit({"status": "running", "configuration": configuration, "user_message": "按空槽逐题准备，开始确认后才执行；成果保存并获接收后补位"})
            previous = None
            try:
                while True:
                    state = controller.tick()
                    view = run_view(controller.progress_snapshot())
                    view.update(blocked=state["blocked"], unresolved=state["unresolved"], shortfall_reason=state["shortfall_reason"])
                    current = json.dumps(view, sort_keys=True)
                    if current != previous:
                        emit(view)
                        previous = current
                    if any(reason != "upload_ack_unknown" for reason in state["blocked"].values()) and not controller.futures:
                        return 3
                    if not state["accepting"] and state["settled"]:
                        return 0
                    time.sleep(poll_seconds)
            except KeyboardInterrupt:
                controller.stop()
                controller.pool.shutdown(wait=True)
                controller.upload_only()
                emit({"status": "stopped", "user_message": "已停止此运行的新领取，原任务收尾成果已保存；未知状态仍保留"})
                return 130
    except (RuntimeUnavailable, ExecutionBlocked, ProtocolError, JournalConflict, RemoteError, TransportUnknown, ArtifactError, AssignmentBusy, ValueError, OSError) as exc:
        emit({"status": "blocked", "error_type": type(exc).__name__, "user_message": scrub_text(str(exc)), "evidence_preserved": True})
        return 3
    finally:
        if client is not None: client.close()
