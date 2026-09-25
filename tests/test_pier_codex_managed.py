import json
import sys

import pytest

if sys.version_info < (3, 12):
    pytest.skip('Pier 0.3.0 requires Python 3.12', allow_module_level=True)
pytest.importorskip('pier')
from dradar.pier_codex_managed import CodexManaged


def consumer(tmp_path, **kwargs):
    return CodexManaged(logs_dir=tmp_path, model_name='openai/fixture', version='0.154.0',
                        managed_config_file=str(tmp_path/'unused.json'),
                        managed_bridge_file=str(tmp_path/'unused.cjs'), **kwargs)


def test_native_acceptance_receipt_never_becomes_request_proof(tmp_path, monkeypatch):
    destination=tmp_path/'status.json'
    monkeypatch.setenv('DRADAR_MANAGED_STATUS_FILE', str(destination))
    agent=consumer(tmp_path)
    payload={'schema':'dradar.managed_consumer.v1','state':'running','generation':'a'*32,
             'native_acceptance':'confirmed','request_used':'unknown','access_token':'FAKE-NEVER-EXPORT'}
    agent._retain_status(payload)
    saved=json.loads(destination.read_text())
    assert saved['request_used']=='unknown' and 'access_token' not in saved
    assert destination.stat().st_mode & 0o077 == 0
    payload['request_used']='confirmed'
    with pytest.raises(RuntimeError,match='managed status invalid'):
        agent._retain_status(payload)
    assert json.loads(destination.read_text())==saved


def test_managed_adapter_refuses_alternate_credential_environment(tmp_path):
    with pytest.raises(ValueError,match='alternate credential'):
        consumer(tmp_path, extra_env={'OPENAI_API_KEY':'fixture-only'})


def test_optional_status_write_failure_does_not_stop_provider(tmp_path, monkeypatch):
    from dradar import credential_files
    monkeypatch.setenv('DRADAR_MANAGED_STATUS_FILE',str(tmp_path/'status.json'))
    def unavailable(*args):
        raise OSError('fixture optional output unavailable')
    monkeypatch.setattr(credential_files,'atomic_private_credential',unavailable)
    consumer(tmp_path)._retain_status({'schema':'dradar.managed_consumer.v1','state':'running',
        'generation':'b'*32,'native_acceptance':'confirmed','request_used':'unknown'})


WARNING = 'WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp" (codex_home: AbsolutePathBuf("/tmp/dradar-managed-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/codex-home"))'

@pytest.mark.parametrize('output', ['codex-cli 0.154.0\n', WARNING+'\ncodex-cli 0.154.0\n', 'codex-cli 0.154.0\n'+WARNING+'\n'])
def test_version_and_known_warning_may_arrive_in_either_order(output):
    from dradar.pier_codex_managed import validate_codex_version_output
    validate_codex_version_output(output)

@pytest.mark.parametrize('output', ['', WARNING, 'codex-cli 0.154.1', 'codex-cli 0.154.0\ncodex-cli 0.154.0', 'codex-cli 0.154.0\nunknown warning', 'codex-cli 0.154.0\ncodex-cli 0.153.0', 'codex-cli 0.154.0\nWARNING: proceeding, even though we could not create PATH aliases: unknown reason'])
def test_version_output_rejects_missing_duplicate_wrong_or_unknown_lines(output):
    from dradar.pier_codex_managed import validate_codex_version_output
    with pytest.raises(RuntimeError, match='managed container version mismatch'):
        validate_codex_version_output(output)


def test_controller_failure_retrieves_finished_worker_exception(tmp_path,monkeypatch):
    import asyncio
    import gc
    from types import SimpleNamespace
    from dradar import pier_codex_managed as module
    agent=consumer(tmp_path)
    (tmp_path/'unused.cjs').write_text('fixture only')
    async def noop(*args,**kwargs):pass
    async def checked(*args,**kwargs):return SimpleNamespace(return_code=0,stdout='codex-cli 0.154.0')
    async def worker(*args,**kwargs):raise RuntimeError('fixture worker failed')
    async def metadata(*args,**kwargs):
        await asyncio.sleep(0)
        raise RuntimeError('fixture controller failed')
    monkeypatch.setattr(module,'verify_task_baseline',noop)
    monkeypatch.setattr(agent,'_session',lambda:SimpleNamespace(prepare=lambda:object()))
    monkeypatch.setattr(agent,'_publish',noop)
    monkeypatch.setattr(agent,'_generation',noop)
    monkeypatch.setattr(agent,'_checked',checked)
    monkeypatch.setattr(agent,'exec_as_agent',worker)
    monkeypatch.setattr(agent,'_metadata',metadata)
    async def check():
        loop=asyncio.get_running_loop();unhandled=[]
        loop.set_exception_handler(lambda loop,context:unhandled.append(context))
        with pytest.raises(RuntimeError,match='fixture controller failed'):
            await agent.run('fixture',object(),object())
        gc.collect();await asyncio.sleep(0)
        assert not unhandled
    asyncio.run(check())


@pytest.mark.parametrize('model', ['gpt-6-sol', 'gpt-6-luna'])
def test_new_model_managed_adapter_requires_exact_container_runtime(tmp_path,model):
    from dradar.pier_codex_managed import validate_codex_version_output
    kwargs=dict(logs_dir=tmp_path,model_name='openai/'+model,
        managed_config_file=str(tmp_path/'unused.json'),managed_bridge_file=str(tmp_path/'unused.cjs'))
    with pytest.raises(ValueError,match='GPT-6 managed consumer'):
        CodexManaged(version='0.154.0',**kwargs)
    CodexManaged(version='0.155.1',**kwargs)
    validate_codex_version_output('codex-cli 0.155.1\n','0.155.1')
    with pytest.raises(RuntimeError,match='managed container version mismatch'):
        validate_codex_version_output('codex-cli 0.154.0\n','0.155.1')


@pytest.mark.parametrize('fault', ['normal', 'expired', 'persist_failed'])
def test_managed_readiness_shares_existing_deadline_with_parent(tmp_path, monkeypatch, fault):
    import asyncio
    import time
    from types import SimpleNamespace
    from dradar import pier_codex_managed as module, worker_events
    from dradar.registration import RegistrationWindow
    agent = consumer(tmp_path)
    (tmp_path/'unused.cjs').write_text('fixture only')
    permit = tmp_path/'permit.json'
    permit.write_text('{"schema":"dradar.managed_start.v1"}')
    permit.chmod(0o600)
    sidecar = tmp_path/'worker.jsonl'
    monkeypatch.setenv('DRADAR_MANAGED_START_PERMIT', str(permit))
    monkeypatch.setenv('DRADAR_RUNNER_SESSION_ID', 'f'*32)
    monkeypatch.setenv(worker_events.WORKER_EVENT_FILE_ENV, str(sidecar))
    material = SimpleNamespace(revision='b'*32, usable=lambda **kw: True)
    published = []
    captured = []
    async def check():
        began = time.monotonic()
        stopped = asyncio.Event()
        async def noop(*args, **kwargs): pass
        async def publish(environment, root, name, *args, **kwargs):
            published.append(name)
            if name in ('start.json', 'stop.json'): stopped.set()
        async def checked(*args, **kwargs):
            return SimpleNamespace(return_code=0, stdout='codex-cli 0.154.0')
        async def worker(*args, **kwargs):
            await stopped.wait()
            return SimpleNamespace(return_code=0)
        async def metadata(environment, root, name):
            return {'state':'ready'} if name == 'status.json' else None
        def emit(**kwargs):
            captured.append(kwargs['start_deadline'])
            assert 0 < kwargs['start_deadline'] - time.monotonic() <= 60
            assert kwargs['start_deadline'] >= began + 60
            if fault == 'persist_failed': return False
            result = worker_events.emit_worker_registered(**kwargs)
            parsed = worker_events.parse_worker_event(sidecar.read_text().splitlines()[-1])
            window = RegistrationWindow(parsed.start_deadline, lambda: True)
            assert window.deadline <= parsed.start_deadline - 1
            if fault == 'expired':
                monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: parsed.start_deadline+1))
            return result
        monkeypatch.setattr(module, 'verify_task_baseline', noop)
        monkeypatch.setattr(module, 'emit_worker_registered', emit)
        monkeypatch.setattr(agent, '_session', lambda: SimpleNamespace(prepare=lambda: material))
        monkeypatch.setattr(agent, '_publish', publish)
        monkeypatch.setattr(agent, '_generation', noop)
        monkeypatch.setattr(agent, '_checked', checked)
        monkeypatch.setattr(agent, 'exec_as_agent', worker)
        monkeypatch.setattr(agent, '_metadata', metadata)
        monkeypatch.setattr(agent, '_retain_status', lambda *args: None)
        if fault == 'normal':
            await agent.run('fixture', object(), object())
        else:
            with pytest.raises(RuntimeError, match='timeout|not persisted'):
                await agent.run('fixture', object(), object())
    asyncio.run(check())
    assert len(captured) == 1
    assert ('start.json' in published) == (fault == 'normal')
