from dradar.flight_recorder import FlightRecorder,SCHEMA_VERSION


class Client:
    def __init__(self,support=False,fail=False):self.support=support;self.fail=fail;self.batches=[]
    def flight_event_capabilities(self):return {'schema_version':SCHEMA_VERSION,'ota_update_v1':self.support}
    def flight_events(self,events):
        if self.fail and events[0]['event_type']=='update_observed':raise OSError('optional unavailable')
        self.batches.append(events)
        return {'acknowledged_event_ids':[e['event_id'] for e in events]}


def test_ota_optional_never_poison_worker_registration(tmp_path):
    client=Client();recorder=FlightRecorder(tmp_path,client)
    recorder.record('update_observed',component='ota',attributes={'update_enabled':True,'launch_method':'launcher'})
    recorder.record('update_failed',component='ota',reason_code='update_download_failed')
    recorder.record('worker_registered',component='provider',attributes={'provider':'codex'})
    assert recorder.flush()==1
    assert recorder.flush_auth()==0
    client.support=True
    assert recorder.flush_auth()==1
    assert [[e['event_type'] for e in b] for b in client.batches]==[['worker_registered'],['update_observed']]
    assert recorder._load(recorder.pending_path)[0]['event_type']=='update_failed'


def test_unscoped_history_is_not_rebound_to_authenticated_session(tmp_path):
    client=Client(True);recorder=FlightRecorder(tmp_path,client)
    recorder.record('update_observed',component='ota',attributes={'update_enabled':True,'launch_method':'launcher'})
    assert recorder.flush_auth(batch_id='a'*32,session_id='b'*32)==0
    assert not client.batches
