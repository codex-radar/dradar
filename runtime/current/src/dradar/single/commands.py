"""dradar run: authenticate, recommend one task, execute, submit, exit."""
import argparse
import json
from pathlib import Path
import sys
import uuid
from ..local_config import HOME, runtime_config, tasks_root_from_config
from ..scrub import scrub_text
from ..v2.journal import Journal, JournalConflict
from ..v2.client import ProtocolError, RemoteError, TransportUnknown
from ..v2.runtime import RuntimeUnavailable
from .runtime import SingleCodexRuntime
from ..v2.artifacts import ArtifactError
from ..assignment_lock import AssignmentBusy
from .client import Client
from .job import Job, JobBlocked, stop
from . import PROTOCOL


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def selection(bootstrap, supplied):
    recommendation = bootstrap.get('recommendation')
    if not isinstance(recommendation, dict):
        raise JobBlocked('Server has no eligible task recommendation for this account')
    target = {**recommendation, **supplied}
    for b in bootstrap.get('benchmarks', []):
        if b['benchmark'] == target['benchmark'] and any(
            m['model'] == target['model'] and m['effort'] == target['effort'] for m in b['models']):
            return target
    raise JobBlocked('Requested harness/library selection is unavailable; no fallback model or effort')


def main(argv=None, *, client_factory=Client, runtime_factory=SingleCodexRuntime):
    parser = argparse.ArgumentParser(prog='dradar', description='One process, one recommended task, one result; isolated candidate')
    from .. import __version__
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('schema')
    for name in ('run', 'progress', 'upload-only', 'stop'):
        p = sub.add_parser(name)
        p.add_argument('--state-root', type=Path, required=name != 'run',
                       help='original private job directory for recovery; a fresh run defaults to a new unique directory')
        p.add_argument('--server', help='must match existing site login')
        if name == 'run':
            p.add_argument('--runtime-binding', type=Path, help='existing reviewed public runtime/dependency binding; contains no credentials')
            p.add_argument('--runtime-sha256', help='SHA256 of that reviewed binding')
            p.add_argument('--tasks-root', type=Path, help='verified task package; otherwise existing configured package for recommendation')
            for field in ('benchmark', 'model', 'effort'):
                p.add_argument('--' + field, help='optional approved selection; Server chooses the concrete question')
    args = parser.parse_args(argv)
    if args.command == 'schema':
        emit({'protocol': PROTOCOL, 'candidate': True, 'commands': ['run', 'progress', 'upload-only', 'stop'],
              'tasks_per_process': 1, 'auto_refill': False, 'host_slots': False,
              'account_machine_binding': False, 'harness_auth': 'existing local authentication, separate from site login',
              'state': 'durable original requests, launch fence and result artifacts'})
        return 0
    client = None
    job = None
    state = args.state_root
    try:
        cfg = runtime_config()
        server = args.server or cfg.get('server')
        if not server or not cfg.get('token'):
            raise JobBlocked('需要已有站点登录配置；模型 harness 须另有本机认证，本命令不创建或搬运凭据')
        if args.server and cfg.get('server') and args.server.rstrip('/') != cfg['server'].rstrip('/'):
            raise JobBlocked('所选站点与已有登录不一致；不自动换账号或搬运认证')
        state = state or HOME / 'single-jobs' / uuid.uuid4().hex
        if args.command != 'run' and not (state / 'journal.sqlite3').is_file():
            raise JobBlocked('Original single-task STATE required for recovery')
        journal = Journal(state)
        emit({'status': 'state', 'state_root': str(state), 'model_started': False})
        client = client_factory(server, cfg['token'], journal)
        job = Job(client)
        if args.command == 'stop':
            reply = stop(client)
            emit({'status': reply['status'], 'model_started': False,
                  'message': '已禁止此任务新启动，并请求当前执行中断；退出仍需核对原运行证据'})
            return 0
        with job.ownership():
            if args.command == 'progress':
                emit(job.snapshot())
                return 0
            if args.command == 'upload-only':
                emit(job.upload_only())
                return 0
            bootstrap = client.bootstrap()
            supplied = {k: getattr(args, k) for k in ('benchmark', 'model', 'effort') if getattr(args, k) is not None}
            saved = journal.value('configuration')
            if saved:
                configuration = json.loads(saved)
                if any(configuration[k] != v for k, v in supplied.items()):
                    raise JournalConflict('Saved task selection cannot be changed; retain original STATE')
            else:
                configuration = selection(bootstrap, supplied)
            job.initialize(configuration)
            original = job.snapshot()['assignments']
            needs_runtime = not original or (original[0]['state']=='leased' and journal.execution(original[0]['assignment_id']) is None)
            if needs_runtime:
                tasks = args.tasks_root or tasks_root_from_config(cfg, configuration['benchmark'])
                # Bind task source only for execution; saved uploads need no model environment.
                journal.bind('tasks_root', str(tasks.expanduser().resolve()))
                job.runtime = runtime_factory(journal, tasks, host_runtime_binding=args.runtime_binding,
                                              host_runtime_sha256=args.runtime_sha256)
            emit({'status': 'selection', 'configuration': configuration, 'tasks_per_process': 1})
            reply = job.run()
            emit(reply)
            if reply['status'] == 'submitted':
                return 130 if reply.get('outcome')=='interrupted' or journal.value('local_interrupt')=='true' else 5 if reply.get('outcome')=='failed' else 0
            return 4 if reply['status'] in ('retry', 'no_work', 'stop', 'ended', 'released') else 3
    except KeyboardInterrupt:
        if client is not None and job is not None:
            try:
                stop(client)
            except Exception:
                pass
        emit({'status': 'interrupted', 'state_root': str(state),
              'message': '保留原STATE与成果；恢复只重放已保存提交，未知执行禁止自动重跑'})
        return 130
    except (JobBlocked, JournalConflict, AssignmentBusy, ProtocolError, RemoteError,
            TransportUnknown, RuntimeUnavailable, ArtifactError, OSError, ValueError) as exc:
        emit({'status': 'blocked', 'state_root': str(state), 'error_type': type(exc).__name__,
              'message': scrub_text(str(exc)), 'auto_reexecution': False})
        return 3
    finally:
        if client is not None:
            client.close()

if __name__ == '__main__':
    raise SystemExit(main())
