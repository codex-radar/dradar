import pytest
from dradar.flight_recorder import FlightRecorder, SCHEMA_VERSION

class Client:
    def __init__(self, support=False, fail_auth=False):
        self.support=support; self.fail_auth=fail_auth; self.batches=[]
    def flight_event_capabilities(self):
        return {'schema_version':SCHEMA_VERSION,'auth_observed_v1':self.support}
    def flight_events(self,events):
        self.batches.append(events)
        if self.fail_auth and events[0]['event_type']=='auth_observed':
            error=RuntimeError('old backend'); error.status_code=404; raise error
        return {'acknowledged_event_ids':[e['event_id'] for e in events]}

def record_auth(recorder):
    return recorder.record('auth_observed',component='provider',attributes={'provider':'codex','auth_stage':'adoption','auth_status':'unknown','auth_delivery':'host-at'})

def test_old_server_never_receives_auth_in_core_batch(tmp_path):
    client=Client(); recorder=FlightRecorder(tmp_path,client)
    record_auth(recorder)
    core=recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    assert recorder.flush()==1
    assert client.batches[0][0]['event_id']==core['event_id']
    assert recorder.flush_auth()==0
    assert recorder.flush()==0

def test_new_server_gets_separate_optional_batch(tmp_path):
    client=Client(True);recorder=FlightRecorder(tmp_path,client)
    record_auth(recorder)
    recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    assert recorder.flush()==1
    assert recorder.flush_auth()==1
    assert [[e['event_type'] for e in batch] for batch in client.batches]==[['worker_registered'],['auth_observed']]

def test_rolling_downgrade_optional_failure_does_not_disable_registration(tmp_path):
    client=Client(True,True);recorder=FlightRecorder(tmp_path,client)
    record_auth(recorder)
    assert recorder.flush_auth()==0
    recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    assert recorder.flush()==1

def test_auth_metadata_cannot_sneak_into_core_event(tmp_path):
    recorder=FlightRecorder(tmp_path)
    with pytest.raises(ValueError): recorder.record('worker_registered',component='provider',attributes={'auth_status':'unknown'})
    for key in ('token','email','path','raw_response'):
        with pytest.raises(ValueError): recorder.record('auth_observed',component='provider',attributes={'provider':'codex','auth_stage':'adoption','auth_status':'unknown',key:'SECRET'})


def test_optional_auth_is_evicted_before_core_local_retention(tmp_path,monkeypatch):
    from dradar import flight_recorder as module
    monkeypatch.setattr(module,'MAX_LOG_EVENTS',3)
    recorder=FlightRecorder(tmp_path)
    core=recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    for _ in range(5): record_auth(recorder)
    assert core['event_id'] in {event['event_id'] for event in recorder._load(recorder.pending_path)}


def test_optional_success_cannot_clear_core_delivery_failure(tmp_path):
    class CoreFails(Client):
        def flight_events(self,events):
            if events[0]['event_type']!='auth_observed':
                error=RuntimeError('core down');error.status_code=503;raise error
            return super().flight_events(events)
    recorder=FlightRecorder(tmp_path,CoreFails(True))
    recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    record_auth(recorder)
    assert recorder.flush()==0
    before=recorder.flush_status_path.read_bytes()
    assert recorder.flush_auth()==1
    assert recorder.flush_status_path.read_bytes()==before
