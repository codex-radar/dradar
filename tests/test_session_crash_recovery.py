"""Exact scope, no provider/model or real service. Actual Docker fixture is separate."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import capacity_journal as journal, session_recovery as recovery
from test_capacity_journal import SID, BID, SCOPE, ReceiptServer, event

OWNER = {"pid": 10001, "start_ticks": 123, "host_id": "host", "boot_id": "boot"}
CHILD = {**OWNER, "pid": 10002, "start_ticks": 124}
DAEMON = {"endpoint": "unix:///var/run/docker.sock", "daemon_id": "original"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery.sys, "platform", "linux")
    local = journal.CapacityJournal(tmp_path, session_id=SID, server=ReceiptServer.server)
    local.bind(BID)
    local._update(lambda s: s.update(owner_identity=OWNER))
    observe = local.begin_attempt(SCOPE)
    job = tmp_path / 'work/jobs/exact'
    job.mkdir(parents=True)
    for kind in ('entered', 'launch_pending'):
        observe(event(kind))
    observe(event('spawned', crash_recovery_supported=True, pid=CHILD['pid'], pgid=CHILD['pid'], linux_identity=CHILD,
                  job_dir=str(job), docker_identity=DAEMON))
    monkeypatch.setattr(recovery.runtime_identity, 'process_identity',
                        lambda pid: None if pid in (10001, 10002) else {**OWNER, 'pid': pid})
    monkeypatch.setattr(recovery.runtime_identity, 'docker_identity', lambda: DAEMON)
    monkeypatch.setattr(recovery.os, 'killpg', lambda *args: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
    monkeypatch.setattr(recovery.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=''))
    return tmp_path, local, ReceiptServer(lose_ack=True), job


def test_preflight_is_read_only_and_lost_ack_repeated_recovery(setup):
    home, local, server, job = setup
    before = local.path.read_bytes()
    result = recovery.recover(home, SID, server)
    assert result['status'] == 'ready' and not result['mutated']
    assert local.path.read_bytes() == before and server.calls == ['receipt']
    original = json.loads(before)
    digest = result['journal_sha256']
    for _ in range(2):
        assert recovery.recover(home, SID, server, execute=True, expected_digest=digest)['status'] == 'released'
    assert server.calls.count('close') == server.calls.count('release') == 1
    state = journal._read(local.path)
    key = next(iter(state['attempts']))
    assert state['attempts'][key]['events'][:-1] == original['attempts'][key]['events']
    assert json.loads(local.path.with_suffix('.before-recovery').read_text()) == original
    assert job.is_dir()


@pytest.mark.parametrize('missing', ['owner', 'ticks', 'daemon'])
def test_missing_original_binding_remains_unknown(setup, missing):
    home, local, server, _ = setup
    def change(s):
        if missing == 'owner':
            s.pop('owner_identity')
        else:
            spawn = next(iter(s['attempts'].values()))['events'][-1]
            spawn.pop('linux_identity' if missing == 'ticks' else 'docker_identity')
    local._update(change)
    before = local.path.read_bytes()
    with pytest.raises(journal.CapacityEvidenceError):
        recovery.recover(home, SID, server)
    assert local.path.read_bytes() == before
    assert 'close' not in server.calls


@pytest.mark.parametrize('which', ['owner', 'child', 'reused', 'boot', 'daemon', 'group'])
def test_live_reused_or_other_runtime_never_releases(setup, monkeypatch, which):
    home, local, server, _ = setup
    if which in ('owner', 'child', 'reused'):
        pid = OWNER['pid'] if which == 'owner' else CHILD['pid']
        monkeypatch.setattr(recovery.runtime_identity, 'process_identity',
                            lambda p: {**OWNER, 'pid': p} if p == pid or p not in (10001, 10002) else None)
    elif which == 'boot':
        monkeypatch.setattr(recovery.runtime_identity, 'process_identity', lambda p: {**OWNER, 'boot_id': 'other'})
    elif which == 'daemon':
        monkeypatch.setattr(recovery.runtime_identity, 'docker_identity', lambda: {**DAEMON, 'daemon_id': 'other'})
    else:
        monkeypatch.setattr(recovery.os, 'killpg', lambda *args: None, raising=False)
    with pytest.raises(journal.CapacityEvidenceError):
        recovery.recover(home, SID, server)
    assert 'close' not in server.calls


def test_cas_refuses_changed_journal(setup):
    home, local, server, _ = setup
    pre = recovery.recover(home, SID, server)
    local.bind_generation(3)
    with pytest.raises(journal.CapacityEvidenceError, match='changed'):
        recovery.recover(home, SID, server, execute=True, expected_digest=pre['journal_sha256'])
    assert 'close' not in server.calls


def test_closed_ack_lost_is_reconciled(setup):
    home, local, server, _ = setup
    close = server.runner_close
    def lost(body):
        close(body)
        from dradar.api_client import ApiError
        raise ApiError('lost close response')
    server.runner_close = lost
    pre = recovery.recover(home, SID, server)
    assert recovery.recover(home, SID, server, execute=True, expected_digest=pre['journal_sha256'])['status'] == 'released'


@pytest.mark.parametrize('running', [False, True])
def test_compose_egress_is_checked_and_unrelated_left_alone(setup, monkeypatch, running):
    home, local, server, job = setup
    rows = [
        {'Id':'a'*64, 'Mounts':[{'Type':'bind','Source':str(job/'agent')}],
         'Config':{'Labels':{'com.docker.compose.project':'exact'}}, 'HostConfig':{'RestartPolicy':{'Name':'no'}}, 'State':{'Pid':0,'Running':False,'Status':'exited'}},
        {'Id':'b'*64,'Mounts':[], 'Config':{'Labels':{'com.docker.compose.project':'exact'}},
         'HostConfig':{'RestartPolicy':{'Name':'no'}}, 'State':{'Pid':0,'Running':running,'Status':'running' if running else 'exited'}},
        {'Id':'c'*64,'Mounts':[], 'Config':{'Labels':{'com.docker.compose.project':'other'}},
         'HostConfig':{'RestartPolicy':{'Name':'no'}}, 'State':{'Pid':0,'Running':True,'Status':'running'}},
    ]
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(rows) if argv[1]=='inspect' else '\n'.join(r['Id'] for r in rows))
    monkeypatch.setattr(recovery.subprocess, 'run', run)
    with pytest.raises(journal.CapacityEvidenceError, match='container remains'):
        recovery.recover(home, SID, server)
    rows[:2] = []
    assert recovery.recover(home, SID, server)['status']=='ready'
    assert all(c[1] in ('ps','inspect') for c in calls)


def test_recovery_event_tampering_is_rejected(setup):
    home, local, server, _ = setup
    pre = recovery.recover(home, SID, server)
    recovery.recover(home, SID, server, execute=True, expected_digest=pre['journal_sha256'])
    state = json.loads(local.path.read_text())
    next(iter(state['attempts'].values()))['events'][-1]['recovery']['process_group']='unknown'
    local.path.write_text(json.dumps(state))
    with pytest.raises(journal.CapacityEvidenceError):
        journal._read(local.path)


def test_concurrent_execute_reuses_one_evidence(setup):
    from concurrent.futures import ThreadPoolExecutor
    home, local, server, _ = setup
    digest = recovery.recover(home, SID, server)['journal_sha256']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _: recovery.recover(home,SID,server,execute=True,expected_digest=digest),range(2)))
    assert all(r['status']=='released' for r in results)
    assert server.calls.count('close')==server.calls.count('release')==1


def test_docker_unavailable_leaves_original_journal(setup, monkeypatch):
    home,local,server,_=setup
    before=local.path.read_bytes()
    def unavailable(): raise OSError('unavailable')
    monkeypatch.setattr(recovery.runtime_identity,'docker_identity',unavailable)
    with pytest.raises(OSError): recovery.recover(home,SID,server)
    assert local.path.read_bytes()==before and 'close' not in server.calls


def test_egress_without_main_still_blocks_from_original_trial_directory(setup,monkeypatch):
    home,local,server,job=setup
    (job/'task__abc12345').mkdir()
    row={'Id':'a'*64,'Mounts':[], 'Config':{'Labels':{'com.docker.compose.project':'task__abc12345'}},
         'HostConfig':{'RestartPolicy':{'Name':'no'}}, 'State':{'Pid':0,'Running':True,'Status':'running'}}
    monkeypatch.setattr(recovery.subprocess,'run',lambda argv,**kw: SimpleNamespace(stdout=json.dumps([row]) if argv[1]=='inspect' else row['Id']))
    with pytest.raises(journal.CapacityEvidenceError,match='container remains'):
        recovery.recover(home,SID,server)


def test_egress_without_main_or_trial_uses_exact_compose_config(setup, monkeypatch):
    home,local,server,job=setup
    row={'Id':'a'*64,'Mounts':[], 'Config':{'Labels':{
        'com.docker.compose.project':'task__abc12345',
        'com.docker.compose.project.config_files':str(job/'compose.yaml')}},
         'State':{'Running':True,'Status':'running','Pid':321}}
    monkeypatch.setattr(recovery.subprocess,'run',lambda argv,**kw: SimpleNamespace(stdout=json.dumps([row]) if argv[1]=='inspect' else row['Id']))
    before=local.path.read_bytes()
    with pytest.raises(journal.CapacityEvidenceError,match='container remains'):
        recovery.recover(home,SID,server)
    assert local.path.read_bytes()==before and 'close' not in server.calls


@pytest.mark.parametrize('field,value', [('evidence_id','9'*32), ('device_generation',4)])
def test_original_preflight_digest_cannot_retry_drifted_release(setup, field, value):
    home,local,server,_=setup
    pre=recovery.recover(home,SID,server)
    recovery.recover(home,SID,server,execute=True,expected_digest=pre['journal_sha256'])
    state=json.loads(local.path.read_text())
    state['release_request'][field]=value
    local.path.write_text(json.dumps(state))
    calls=list(server.calls)
    with pytest.raises(journal.CapacityEvidenceError):
        recovery.recover(home,SID,server,execute=True,expected_digest=pre['journal_sha256'])
    assert server.calls==calls


def test_command_reports_lost_receipt_without_replacing_evidence(setup, monkeypatch, capsys):
    from dradar import local_config, legacy_capacity
    from dradar.api_client import ApiError
    home,local,server,_=setup
    pre=recovery.recover(home,SID,server)
    args=SimpleNamespace(recover_session=SID,execute=True,journal_sha256=pre['journal_sha256'])
    monkeypatch.setattr(local_config,'HOME',home)
    monkeypatch.setattr(legacy_capacity,'_existing_client',lambda args:(server,{},()))
    original=server.runner_session_receipt
    count=[0]
    def unavailable(*a,**kw):
        count[0]+=1
        if count[0]>=2: raise ApiError('receipt unavailable')
        return original(*a,**kw)
    monkeypatch.setattr(server,'runner_session_receipt',unavailable)
    assert recovery.cmd_recover(args)==1
    assert json.loads(capsys.readouterr().out)['status']=='unknown'
    sealed=journal._read(local.path)
    evidence=sealed['release_request']['evidence_id']
    monkeypatch.setattr(server,'runner_session_receipt',original)
    assert recovery.cmd_recover(args)==0
    assert json.loads(capsys.readouterr().out)['status']=='released'
    assert journal._read(local.path)['release_request']['evidence_id']==evidence


def test_removing_generation_cannot_bypass_persisted_request_binding(setup):
    home,local,server,_=setup
    pre=recovery.recover(home,SID,server)
    recovery.recover(home,SID,server,execute=True,expected_digest=pre['journal_sha256'])
    state=json.loads(local.path.read_text())
    del state['release_request']['device_generation']
    del state['release_request']['device_id']
    local.path.write_text(json.dumps(state))
    calls=list(server.calls)
    with pytest.raises(journal.CapacityEvidenceError):
        recovery.recover(home,SID,server,execute=True,expected_digest=pre['journal_sha256'])
    assert server.calls==calls
