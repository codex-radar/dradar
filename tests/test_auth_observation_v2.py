import json
from types import SimpleNamespace
import pytest
from dradar.auth_observation import ObservationSink,ObservationReader
from dradar.flight_recorder import FlightRecorder,SCHEMA_VERSION

def attrs():return {'provider':'codex','auth_stage':'adoption','auth_status':'confirmed','execution_id':'e'*32,'owner_epoch':1,'attempt':1,'auth_seq':1}
class Client:
 def __init__(self,v2=False,fail=False):self.v2=v2;self.fail=fail;self.batches=[]
 def flight_event_capabilities(self):return {'schema_version':SCHEMA_VERSION,'auth_observed_v1':True,'auth_observed_v2':self.v2}
 def flight_events(self,events):
  self.batches.append(events)
  if self.fail and events[0]['event_type']=='auth_observed_v2':raise RuntimeError('optional failure')
  return {'acknowledged_event_ids':[e['event_id'] for e in events]}

def test_negotiated_v2_cannot_poison_core_or_v1(tmp_path):
 c=Client();r=FlightRecorder(tmp_path,c)
 r.record('auth_observed_v2',component='provider',attributes=attrs())
 r.record('worker_registered',component='provider',attributes={'provider':'codex'})
 assert r.flush()==1 and r.flush_auth()==0
 c.v2=True;c.fail=True;assert r.flush_auth()==0
 r.record('worker_registered',component='provider',attributes={'provider':'codex'})
 assert r.flush()==1
 c.fail=False;assert r.flush_auth()==1
 assert all(len({x['event_type'] for x in batch})==1 for batch in c.batches)

def test_v2_fields_rejected_in_old_or_core_events(tmp_path):
 r=FlightRecorder(tmp_path)
 for kind in ['auth_observed','worker_registered']:
  with pytest.raises(ValueError):r.record(kind,component='provider',attributes=attrs())
 for name in ('token','path','email','account_id'):
  with pytest.raises(ValueError):r.record('auth_observed_v2',component='provider',attributes={**attrs(),name:'secret'})

def test_sink_hmac_time_coverage_and_reader_dedup(tmp_path):
 session=SimpleNamespace(local_key=b'k'*32,authority=SimpleNamespace(store_id='a'*32),_material=lambda:SimpleNamespace(revision='b'*32))
 p=tmp_path/'observations';sink=ObservationSink(p,session,'c'*32)
 sink.emit('execution','confirmed',generation='d'*32,observed_at='2026-09-14T00:00:00+00:00',auth_action='start')
 sink.coverage();seen=[]
 reader=ObservationReader(p,lambda data:seen.append(data),{'owner_epoch':3,'_runner_attempt':2})
 reader.drain();reader.drain();assert len(seen)==2
 assert seen[0]['_occurred_at']=='2026-09-14T00:00:00+00:00'
 assert seen[0]['auth_generation_tag']==sink.tag('generation','d'*32)
 assert seen[0]['owner_epoch']==3 and seen[0]['attempt']==2
 assert seen[-1]['auth_events_emitted']==2 and seen[-1]['auth_events_dropped']==0
 raw=p.read_text();assert 'a'*32 not in raw and 'b'*32 not in raw and str(tmp_path) not in raw

@pytest.mark.parametrize('bind_ok',[True,False])
def test_real_runner_pumps_observations_after_owner_bind(tmp_path,monkeypatch,bind_ok):
 from test_runner_tools import _fake_pier,_assignment
 from dradar import runner as module
 _fake_pier(monkeypatch,tmp_path)
 monkeypatch.setattr(module.image_cache,"prepare_trial_builder",lambda *a,**k:module.image_cache.TrialBuilderLease("dradar-task-test",True))
 monkeypatch.setattr(module,'_wait_for_worker_registration',lambda *a,**k:{'profile':'codex_managed_at'})
 base=module.subprocess.Popen
 session=SimpleNamespace(local_key=b'k'*32,authority=SimpleNamespace(store_id='a'*32),_material=lambda:SimpleNamespace(revision='b'*32))
 class Process(base):
  def __init__(self,*a,**k):
   super().__init__(*a,**k);self.env=k['env'];self.sink=ObservationSink(self.env['DRADAR_MANAGED_EVENT_FILE'],session,'c'*32);self.sink.emit('selection','confirmed')
  def wait(self,timeout=None):
   if not __import__('pathlib').Path(self.env['DRADAR_MANAGED_START_PERMIT']).exists():
    assert not bind_ok
    return super().wait(timeout)
   self.sink.emit('execution','confirmed',auth_action='start');self.sink.emit('execution','confirmed',auth_action='end');self.sink.coverage()
   return super().wait(timeout)
 monkeypatch.setattr(module.subprocess,'Popen',Process)
 assignment=_assignment('codex')|{'auth_runtime':'codex-managed-at-v1','auth_cohort_id':'c'*32,'owner_epoch':0}
 observed=[]
 def bind(event):
  if not bind_ok:raise RuntimeError('owner refused')
  assignment['owner_epoch']=3
 call=lambda:module.run_trial(assignment,tmp_path,tmp_path,on_worker_registered=bind,on_auth_observed=lambda x:observed.append(x),managed_auth_config=tmp_path/'selection.json')
 if bind_ok:
  call();assert any(x.get('auth_action')=='start' for x in observed);assert all(x['owner_epoch']==3 for x in observed)
 else:
  with pytest.raises(RuntimeError,match='owner refused'):call()
  assert not any(x.get('auth_action')=='start' for x in observed)

def test_malformed_optional_observation_is_dropped_not_raised(tmp_path):
 session=SimpleNamespace(local_key=b'k'*32,authority=SimpleNamespace(store_id='a'*32),_material=lambda:SimpleNamespace(revision='b'*32))
 sink=ObservationSink(tmp_path/'events',session,'c'*32)
 sink.emit('execution','confirmed',auth_action=['invalid'])
 assert sink.dropped==1 and not sink.path.exists()

def test_terminal_optional_drain_follows_core_close(tmp_path):
 from dradar.telemetry import RunnerTelemetry
 class ClosingClient(Client):
  def runner_close(self,payload):return {'ok':True}
 c=ClosingClient(v2=True);t=RunnerTelemetry(c,home=tmp_path,jitter=False)
 t.bind_batch('b'*32)
 t.record_event('auth_observed_v2',component='provider',assignment_id='a'*32,attributes=attrs())
 t.close('completed')
 assert c.batches[-1][0]['event_type']=='auth_observed_v2'
 assert any(x['event_type']=='session_closed' for batch in c.batches[:-1] for x in batch)
 assert not t.flight_recorder._load(t.flight_recorder.pending_path)

def test_optional_http_does_not_enter_core_rate_limit_retry(monkeypatch):
 import httpx
 from dradar.api_client import ApiClient,ApiError
 calls=[]
 def respond(request):calls.append(request);return httpx.Response(429,headers={'Retry-After':'60'})
 api=ApiClient('http://fixture.invalid','fixture',transport=httpx.MockTransport(respond),capabilities=[])
 with pytest.raises(ApiError):api.auth_flight_events([])
 assert len(calls)==1
