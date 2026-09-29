"""Exact reviewed Mac exception is checked again without changing old work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import os
import pytest
from dradar import retained_admission as r, boundary_recovery as b, assignment_boundary
from test_historical_admission import A, OLD_BATCH, _assignment, _response, Client


def proof():
    m={'schema':'reviewed-retained-admission-v1','max_claims':1,
       'scope':{'assignment_id':A,'batch_id':OLD_BATCH,'session_ids':['c'*32],
                'owner_epoch':1,'resume_generation':0},
       'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
       'local_review':{'evidence_mapping_sha256':'a'*64,'journals':{'c'*32:'b'*64}}}
    return {'classification':m['schema'],'state':'reviewed_exception',
        'physical_exit':'operator_reviewed_legacy_evidence','result_status':'preserve_unknown',
        'manifest':m,'manifest_sha256':r._digest(m),'operation_id':'d'*32}


def test_exact_proof_keeps_personal_boundary(tmp_path,monkeypatch):
    client=Client(); row=_response(A,OLD_BATCH,1)
    row.update(admission_evidence_version=3,status='expired',has_submission=False,admission_evidence=proof())
    client.rows={A:row}
    path=assignment_boundary.prepare(tmp_path,'deep-swe',[_assignment(A,OLD_BATCH)])
    assignment_boundary.record_outcome(path,_assignment(A,OLD_BATCH),'failed')
    before=path.read_bytes(); state,digest=assignment_boundary.snapshot(path)
    calls=[]
    monkeypatch.setattr(r,'inspect',lambda *a,**kw: calls.append(kw) or deepcopy(row['admission_evidence']['manifest']['local_review']))
    monkeypatch.setattr(b,'_check_processes',lambda *a,**kw: pytest.fail('must use exact job evidence'))
    assert b.historical_unknown_allows_claim(client,state,digest,path,tmp_path)==1
    assert path.read_bytes()==before
    assert client.historical_admission_reference.startswith('retained:')
    assert calls[0]['client'] is client


@pytest.mark.parametrize('drift',['files','journal','host','daemon','missing','expiry','batch','operation','schema'])
def test_changed_or_incomplete_review_blocks(tmp_path,monkeypatch,drift):
    p=proof(); fresh=deepcopy(p['manifest']['local_review'])
    if drift in ('files','journal','host','daemon'):
        fresh[drift]='changed'
    elif drift=='missing':
        del p['manifest']['scope']['session_ids']
    elif drift=='expiry':
        p['manifest']['expires_at']='2000-01-01T00:00:00+00:00'
    elif drift=='batch':
        p['manifest']['scope']['batch_id']='f'*32
    elif drift=='operation':
        p['operation_id']='unbound'
    else:
        p['manifest']['schema']='unrecognized'
    p['manifest_sha256']=r._digest(p['manifest'])
    monkeypatch.setattr(r,'inspect',lambda *a,**kw:fresh)
    with pytest.raises(r.AdmissionBlocked):
        r.validate(Client(),tmp_path,A,p,batch_id=OLD_BATCH)


@pytest.mark.parametrize('line',[f'123 1 123 unrelated',f'124 1 123 child',f'125 1 125 runner /exact/job'])
def test_present_or_reused_process_identity_is_not_exit(monkeypatch,line):
    raw=f'{os.getpid()} 1 {os.getpid()} self\n{line}\n'
    monkeypatch.setattr(r.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout=raw))
    with pytest.raises(r.AdmissionBlocked):
        r._processes_absent('/exact/job',[(123,123)])


def test_file_inventory_preserves_failed_results_and_refuses_completion(tmp_path):
    p=tmp_path/'result.json'; p.write_text('{"agent_execution":null,"exception_info":{"type":"error"}}')
    first=r._files(tmp_path); assert first and p.exists()
    p.write_text('{"agent_execution":{"finished_at":"now"},"exception_info":null}')
    with pytest.raises(r.AdmissionBlocked,match='completed'):
        r._files(tmp_path)
