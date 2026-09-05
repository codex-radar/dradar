"""Subprocess fixture for bootstrap QA, never shipped in the runtime wheel.

It runs the real entry/controller with an inert API, environment and Fleet.
No production endpoint, model credentials, Docker, or benchmark is accessed.
"""
import json
import os
from pathlib import Path
import sys


def main():
    fixture = Path(os.environ['DRADAR_FIXTURE_ROOT'])
    home = Path(os.environ['DRADAR_HOME'])
    assert (fixture / 'fixture-only').is_file() and fixture in home.parents
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / 'src'), str(root / 'tests')]
    import pytest
    from dradar import bootstrap_receipt, doctor, fleet, run_plans, run_session, session_entry
    from test_run_plans import _plan, _state, _server_response, _envelope, FakeClient
    options = dict(value[2:].split('=', 1) for value in sys.argv[1:] if value.startswith('--') and '=' in value)
    revision = options['revision']
    if os.environ['DRADAR_FIXTURE_MODE'] == 'stream':
        import time
        bootstrap_receipt.signal_ready(revision)
        print('DRADAR_STREAM_READY', flush=True)
        time.sleep(1.5)
        return 0
    plan = _plan(mode='fixed', concurrency=5, task_count=5)
    path = home / 'run-plans' / ('plan-' + plan['plan_id'] + '.json')
    if not path.exists():
        path, state = _state(home / 'run-plans', plan)
        state.update(server=options['server'], run_code_hash=run_plans._run_code_digest(options['plan']))
        run_plans._atomic_json(path, state)
    client = FakeClient(starts=[_server_response(plan, _envelope(agent_action='start_runner'))],
                        progress=[_server_response(plan, _envelope(status='completed', agent_action='done'))])
    fleet_calls = []
    def environment(_plan, allow_docker_install=False):
        if os.environ['DRADAR_FIXTURE_MODE'] == 'confirm' and not allow_docker_install:
            return {'install_required': True, 'error_code': 'docker_install_confirmation_required',
                    'user_message': 'Fixture installation requires explicit confirmation',
                    'agent_action': 'install_docker', 'agent': {'requires_user_action': True}}
        return None
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run_plans, 'HOME', home)
        patch.setattr(run_plans, '_state_and_client', lambda _args: (options['plan'], path, run_plans._read_private_json(path), client))
        patch.setattr(run_plans, '_state_in_active_fleet', lambda *_a, **_k: False)
        patch.setattr(run_plans, '_exact_pending_uploads', lambda *_a, **_k: [])
        patch.setattr(fleet, 'prepare_new_batch_runtime', lambda **_k: None)
        patch.setattr(fleet, 'batch_status', lambda _batch: None)
        patch.setattr(fleet, 'add_batch', lambda **kw: fleet_calls.append(kw['workers']) or {'batch': {'workers': kw['workers']}})
        patch.setattr(doctor, 'plan_environment_issue', environment)
        patch.setattr(run_session.time, 'sleep', lambda _: None)
        # The UV resolver is the injected boundary. It supplies a synthetic
        # installed distribution; source validation itself still runs.
        from types import SimpleNamespace
        original_distribution = session_entry.importlib.metadata.distribution
        patch.setattr(session_entry.importlib.metadata, 'distribution', lambda _name: SimpleNamespace(read_text=lambda _: json.dumps({
            'url': 'https://github.com/SecurityMind/dradar',
            'vcs_info': {'vcs': 'git', 'commit_id': revision},
        })) if _name == 'dradar' else original_distribution(_name))
        code = session_entry.main(sys.argv[1:])
    trace = {'returncode': code, 'starts': [call['concurrency'] for call in client.start_calls],
             'progress_reads': len(client.progress_calls), 'fleet_workers': fleet_calls,
             'receipt_written': Path(os.environ[bootstrap_receipt.PATH_ENV]).is_file()}
    (fixture / ('runtime-' + os.environ['DRADAR_FIXTURE_ATTEMPT'] + '.json')).write_text(json.dumps(trace))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
