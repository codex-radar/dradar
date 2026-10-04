"""Original preserved evidence on copies only; no model, claim or live writes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace
import pytest
import httpx
from dradar.v2.client import Client, ProtocolError, TransportUnknown
from dradar.v2.journal import Journal, JournalConflict
from dradar.v2.artifacts import ArtifactError
from dradar.v2.protocol import correction_receipt, result_hash
from dradar.v2.completion_correction import prepare_supplement, supplement_result
from dradar.v2.native_evidence import collection_safety
from dradar.v2.scheduler import Controller, ExecutionBlocked
from dradar.v2.presentation import assignment_view

EVIDENCE = Path(os.environ.get('DRADAR_ORIGINAL_COMPLETION_EVIDENCE', str(Path(__file__).resolve().parents[3] / 'dradar-ds0-deployment-20261003/one-real-cli295-after068-20261004/trial-state')))
AID = '77a8b6b53f13465296b268c6ff145f78'
OLDHASH = 'd539eb84d42b88f28e08fa44b2e50ede26112ed77c23e3222a49091cd7ebe7fa'
EXITHASH = 'cfcc5e32ae0be230264b5b6e61deb270bb9015453c91a97a290fa8cdc890c77e'

def preserved_state(root):
    """Fingerprint old rows and evidence, excluding new correction namespace."""
    with sqlite3.connect(root / 'journal.sqlite3') as db:
        rows = {'executions': db.execute('SELECT * FROM executions').fetchall(),
                'requests': db.execute("SELECT * FROM requests WHERE operation NOT LIKE 'completion-correction:%'").fetchall(),
                'metadata': db.execute('SELECT * FROM metadata ORDER BY key').fetchall()}
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for base in (root / 'artifacts', root / 'runtime') for p in base.rglob('*')
             if p.is_file() and 'completion-corrections' not in p.parts}
    return rows, files

class Remote:
    """Unit transport with strict operation inventory, never a grader."""
    def __init__(self, journal):
        self.journal = journal
        self.original = next(r for r in journal.requests() if r.operation == 'result:' + AID)
        self.a = copy.deepcopy(next(r for r in journal.requests() if r.operation == 'start:' + AID).response['assignment'])
        self.a['state'] = 'uncertain'
        self.run = copy.deepcopy(next(r for r in journal.requests() if r.operation == 'run:stop').response['run'])
        self.paths = []; self.bodies = []; self.accepted = None; self.drop_ack = False; self.bad_ack = False
    def receipt(self, body):
        old, new = body['original_result'], body['corrected_result']
        return {'schema_version': 2, 'server_time': '2026-10-04T16:00:00Z', 'request_id': body['request_id'],
                'status': 'submitted', 'assignment_id': AID, 'execution_id': new['execution_id'],
                'submission_id': 'synthetic-submission', 'result_sha256': new['result_sha256'], 'grading_state': 'queued',
                'replayed': self.accepted is not None,
                'completion_correction': {'schema': body['correction_schema'], 'original_request_id': old['request_id'],
                'original_result_sha256': old['result_sha256'], 'corrected_request_id': new['request_id'],
                'corrected_result_sha256': new['result_sha256'], 'exit_evidence_sha256': body['exit_evidence_sha256'],
                'authorization_ref': 'synthetic-test-only'}}
    def __call__(self, req):
        self.paths.append((req.method, req.url.path))
        if req.method == 'GET':
            if req.url.path == '/api/v2/bootstrap':
                return httpx.Response(200, json={'schema_version':2, 'server_time':'2026-10-04T16:00:00Z',
                    'account':{'account_id':self.journal.value('account')}, 'capabilities':['on-demand-v2'],
                    'limits':{'max_result_bytes':95000000}})
            if req.url.path == '/api/v2/runs/' + self.journal.value('run'):
                return httpx.Response(200,json={'schema_version':2, 'server_time':'2026-10-04T16:00:00Z', 'run':self.run, 'assignments':[self.a]})
            if req.url.path == '/api/v2/assignments/' + AID:
                return httpx.Response(200,json={'schema_version':2, 'server_time':'2026-10-04T16:00:00Z', 'assignment':self.a})
        assert req.method == 'POST' and req.url.path.endswith('/result-correction'), 'fresh scheduling or old failed upload forbidden'
        data = req.read()
        from email.parser import BytesParser
        from email.policy import default
        msg = BytesParser(policy=default).parsebytes(b'Content-Type: '+req.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+data)
        parts = {p.get_param('name',header='content-disposition'):p.get_payload(decode=True) for p in msg.iter_parts()}
        body = json.loads(parts.pop('metadata')); self.bodies.append(body)
        assert body['original_result'] == self.original.body
        assert set(parts) == {r['name'] for r in self.original.body['artifacts']}
        for rec in self.original.body['artifacts']:
            assert hashlib.sha256(parts[rec['name']]).hexdigest() == rec['sha256'] and len(parts[rec['name']]) == rec['size_bytes']
        if self.accepted is not None: assert body == self.accepted
        reply = self.receipt(body); self.accepted = body
        self.a.update(state='submitted', outcome='completed', result_sha256=reply['result_sha256'],
                      completion_correction={**reply['completion_correction'], 'original_outcome':'failed', 'accepted_outcome':'completed'})
        if self.drop_ack:
            self.drop_ack = False
            raise httpx.ReadError('synthetic ACK loss after accept')
        if self.bad_ack: reply['completion_correction']['original_result_sha256'] = '0'*64
        return httpx.Response(200,json=reply)

@pytest.fixture
def case(tmp_path):
    if not EVIDENCE.exists(): pytest.skip('explicit external original completion evidence not provided')
    root = tmp_path / 'state'; root.mkdir()
    shutil.copy2(EVIDENCE / 'journal.sqlite3', root / 'journal.sqlite3')
    shutil.copytree(EVIDENCE / 'artifacts', root / 'artifacts')
    host = root / 'runtime' / AID / 'host'; host.mkdir(parents=True)
    original_host = EVIDENCE / 'runtime' / AID / 'host'
    shutil.copytree(original_host / 'collected', host / 'collected')
    for name in ('CLEANUP.json', 'public-events.json'): shutil.copy2(original_host / name, host / name)
    j = Journal(root); remote = Remote(j)
    client = Client(j.value('server'), 'synthetic-token', j, transport=httpx.MockTransport(remote))
    yield root, j, remote, client
    client.close()

def test_exact_original_supplement_and_replay_preserve_every_frozen_byte(case):
    root,j,remote,client=case; before=preserved_state(root)
    reply=supplement_result(client,AID,OLDHASH,EXITHASH)
    request=next(r for r in j.requests() if r.operation.startswith('completion-correction:'))
    assert request.request_id != remote.original.request_id
    body=request.body; new=body['corrected_result']; old=body['original_result']
    ignore={'request_id','result_sha256','outcome','failure'}
    assert {k:v for k,v in new.items() if k not in ignore} == {k:v for k,v in old.items() if k not in ignore}
    assert new['outcome']=='completed' and new['failure'] is None and new['result_sha256']==result_hash(new)
    assert supplement_result(client,AID,OLDHASH,EXITHASH)==reply
    assert len(remote.bodies)==1 and preserved_state(root)==before
    assert j.value('local_stop')=='true' and j.execution(AID)['result_json']==json.dumps({k:v for k,v in old.items() if k!='request_id'},sort_keys=True,separators=(',',':'))

def test_ack_lost_and_restart_replay_identical_correction(case):
    root,j,remote,client=case; before=preserved_state(root); remote.drop_ack=True
    with pytest.raises(TransportUnknown):supplement_result(client,AID,OLDHASH,EXITHASH)
    first=next(r for r in j.requests() if r.operation.startswith('completion-correction:'))
    assert first.response is None
    restarted=Client(j.value('server'),'synthetic-token',Journal(root),transport=httpx.MockTransport(remote))
    try: reply=supplement_result(restarted,AID,OLDHASH,EXITHASH)
    finally:restarted.close()
    assert reply['replayed'] is True and len(remote.bodies)==2 and remote.bodies[0]==remote.bodies[1]
    assert preserved_state(root)==before

def test_request_commit_before_metadata_crash_recovers_same_id(case,monkeypatch):
    root,j,remote,client=case; before=preserved_state(root)
    import dradar.v2.completion_correction as c
    real=c._metadata
    monkeypatch.setattr(c,'_metadata',lambda *args: (_ for _ in ()).throw(OSError('synthetic crash')))
    with pytest.raises(ArtifactError):prepare_supplement(client,AID,OLDHASH,EXITHASH)
    request=next(r for r in j.requests() if r.operation.startswith('completion-correction:'))
    monkeypatch.setattr(c,'_metadata',real)
    recovered,_=prepare_supplement(client,AID,OLDHASH,EXITHASH)
    assert recovered.body_json==request.body_json and recovered.request_id==request.request_id and preserved_state(root)==before

@pytest.mark.parametrize('field,value',[('device_id','wrong-owner'),('lease_id','wrong-lease'),('owner_epoch',999),('execution_id','wrong-execution')])
def test_changed_authoritative_owner_fails_before_upload(case,field,value):
    root,j,remote,client=case;remote.a[field]=value
    with pytest.raises(ProtocolError):supplement_result(client,AID,OLDHASH,EXITHASH)
    assert not remote.bodies and len(j.requests())==len(remote.journal.requests())

@pytest.mark.parametrize('where',['cleanup','patch','output','metadata','raw','trajectory'])
def test_changed_preserved_evidence_fails_before_upload(case,where):
    root,j,remote,client=case
    paths={'cleanup':root/'runtime'/AID/'host/CLEANUP.json','output':root/'runtime'/AID/'host/collected/rules.json',
           'metadata':root/'artifacts/metadata'/f'{AID}.json','raw':root/'artifacts/raw'/AID/'runner_result',
           'patch':root/'artifacts/upload'/AID/'patch','trajectory':root/'artifacts/upload'/AID/'trajectory'}
    paths[where].write_bytes(paths[where].read_bytes()+b' ')
    with pytest.raises(ArtifactError):supplement_result(client,AID,OLDHASH,EXITHASH)
    assert not remote.bodies and not any(r.operation.startswith('completion-correction:') for r in j.requests())

def test_wrong_explicit_hash_and_active_run_and_conflicting_result_block(case):
    root,j,remote,client=case
    with pytest.raises(ArtifactError):supplement_result(client,AID,'0'*64,EXITHASH)
    with pytest.raises(ArtifactError):supplement_result(client,AID,OLDHASH,'0'*64)
    remote.run['state']='active'
    with pytest.raises(ProtocolError):supplement_result(client,AID,OLDHASH,EXITHASH)
    remote.run['state']='stopped';remote.a.update(state='submitted',result_sha256='0'*64)
    with pytest.raises(ProtocolError):supplement_result(client,AID,OLDHASH,EXITHASH)
    assert not remote.bodies

def test_delta_after_saved_request_and_changed_ack_are_rejected(case):
    root,j,remote,client=case
    request,_=prepare_supplement(client,AID,OLDHASH,EXITHASH)
    body=request.body;body.pop('request_id');body['corrected_result'].pop('request_id');body['corrected_result']['tokens']['total']+=1
    with pytest.raises(JournalConflict):j.prepare_correction(request.operation,request.path,body)
    remote.bad_ack=True
    with pytest.raises(ProtocolError):supplement_result(client,AID,OLDHASH,EXITHASH)
    assert next(r for r in j.requests() if r.operation==request.operation).response is None

@pytest.mark.parametrize('change',[' M engine/apply.py',' D data/train.tsv','?? ../secret','?? /tmp/file','?? data/train.tsv','?? path\\\\evil'])
def test_unsafe_scratch_or_tracked_change_rejected(case,change):
    root,j,remote,client=case
    runner=json.loads((root/'artifacts/upload'/AID/'runner_result').read_bytes());runner['unexpected_workspace_changes']=[change]
    with pytest.raises(ValueError):collection_safety(runner)

def test_scratch_accepted_but_missing_output_or_public_input_change_rejected(case):
    root,j,remote,client=case;runner=json.loads((root/'artifacts/upload'/AID/'runner_result').read_bytes())
    assert len(collection_safety(runner)['ignored_untracked_scratch'])==4
    for k,v in [('missing_deliverables',['rules.json']),('public_inputs_unchanged',False),('outputs',{})]:
        bad=copy.deepcopy(runner);bad[k]=v
        with pytest.raises(ValueError):collection_safety(bad)

def test_progress_honors_only_matching_accepted_correction_and_old_upload_blocked(case):
    root,j,remote,client=case
    request,_=prepare_supplement(client,AID,OLDHASH,EXITHASH)
    c=Controller.__new__(Controller);c.journal=j;c._observed_start={};c.phases={}
    c.snapshot=lambda: {'run':remote.run,'assignments':[copy.deepcopy(remote.a)]}
    c.runtime_unavailable_tasks=lambda: []
    with pytest.raises(ExecutionBlocked):c._upload(remote.a)
    supplement_result(client,AID,OLDHASH,EXITHASH)
    snap=c.progress_snapshot();a=snap['assignments'][0]
    assert a['outcome']=='completed' and a['original_reported_outcome']=='failed'
    assert assignment_view(a)['completion_correction']['original_outcome']=='failed'
    assert c._upload(remote.a) is None
    remote.a['completion_correction']['original_result_sha256']='0'*64
    with pytest.raises(ProtocolError):c.progress_snapshot()

def test_command_does_not_instantiate_runtime_or_controller(case,monkeypatch,capsys):
    root,j,remote,client=case
    from dradar.v2 import commands
    monkeypatch.setattr(commands,'runtime_config',lambda:{'server':j.value('server'),'token':'synthetic-token'})
    def forbidden(*a,**k):pytest.fail('fresh runtime or scheduler invoked')
    monkeypatch.setattr(commands,'Controller',forbidden)
    rc=commands.main(['supplement-result','--state-root',str(root),'--assignment-id',AID,
          '--original-result-sha256',OLDHASH,'--exit-evidence-sha256',EXITHASH],
          client_factory=lambda *a:client,runtime_factory=forbidden)
    reply=json.loads(capsys.readouterr().out)
    assert rc==0 and reply['model_started'] is False and reply['run_remains_stopped'] is True
    assert reply['accepted_outcome']=='completed' and reply['grading_state']=='queued' and 'reward' not in reply

def test_correction_journal_nested_id_and_body_are_durable_without_external_fixture(tmp_path):
    j=Journal(tmp_path/'synthetic-state')
    body={'device_id':'synthetic-device','correction_schema':'codex-native-completion-correction/1',
          'original_result':{'request_id':'old-request'},'corrected_result':{'outcome':'completed'},
          'exit_evidence_json':'{}','exit_evidence_sha256':'a'*64}
    request=j.prepare_correction('completion-correction:synthetic:oldhash','/api/v2/assignments/synthetic/result-correction',body)
    assert request.body['corrected_result']['request_id']==request.request_id
    assert request.body['original_result']['request_id']=='old-request'
    assert Journal(j.root).prepare_correction(request.operation,request.path,body)==request
    altered=copy.deepcopy(body);altered['exit_evidence_json']='{"changed":true}'
    with pytest.raises(JournalConflict):j.prepare_correction(request.operation,request.path,altered)
    assert j.execution('synthetic') is None

@pytest.mark.parametrize('field',['original_request_id','original_result_sha256','corrected_request_id','corrected_result_sha256','exit_evidence_sha256','authorization_ref'])
def test_correction_receipt_binding_without_external_fixture(field):
    body={'correction_schema':'codex-native-completion-correction/1','exit_evidence_sha256':'e'*64,
          'original_result':{'request_id':'old','result_sha256':'a'*64},
          'corrected_result':{'execution_id':'execution','result_sha256':'b'*64}}
    reply={'schema_version':2,'server_time':'now','request_id':'new','status':'submitted','assignment_id':'assignment',
           'execution_id':'execution','submission_id':'submission','result_sha256':'b'*64,'grading_state':'queued',
           'completion_correction':{'schema':body['correction_schema'],'original_request_id':'old',
                'original_result_sha256':'a'*64,'corrected_request_id':'new','corrected_result_sha256':'b'*64,
                'exit_evidence_sha256':'e'*64,'authorization_ref':'synthetic-approval'}}
    assert correction_receipt(reply,'new','assignment',body)==reply
    reply['completion_correction'][field]='' if field=='authorization_ref' else 'mismatch'
    with pytest.raises(ProtocolError):correction_receipt(reply,'new','assignment',body)

def test_supplement_module_has_no_runtime_or_scheduling_imports():
    import ast,inspect
    import dradar.v2.completion_correction as module
    tree=ast.parse(inspect.getsource(module))
    names=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not set(names)&{'runtime','host_runtime','scheduler','runner','runloop'}
