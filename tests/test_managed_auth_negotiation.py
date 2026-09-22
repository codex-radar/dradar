import httpx
import pytest
from dradar import managed_auth_selection as selection
from dradar.api_client import ApiClient, ApiError


def client(monkeypatch, response, *, selected=True, gpt6=False):
    monkeypatch.setattr(selection,'load_selection',lambda: object() if selected else None)
    monkeypatch.setattr(selection,'selection_requested',lambda:selected)
    monkeypatch.setattr(selection,'trial_platform_ready',lambda:True)
    calls=[]
    def request(req):
        calls.append((req.method,req.url.path,req.content))
        if req.method=='GET':return response
        return httpx.Response(200,json={'assignment':{'auth_runtime':selection.PROFILE,'assignment_id':'a'*32,'auth_cohort_id':'b'*32}})
    api=ApiClient('https://fixture.invalid','fake-server-token',transport=httpx.MockTransport(request),capabilities=[selection.CAPABILITY,selection.TRIAL_CAPABILITY]+(["codex-gpt6-sol-luna-v1"] if gpt6 else []))
    return api,calls


@pytest.mark.parametrize('response',[httpx.Response(404),httpx.Response(200,json={}),httpx.Response(200,json={'schema':'dradar.auth-runtime.v1','profiles':[]})])
def test_old_unknown_or_disabled_server_never_receives_claim(monkeypatch,response):
    api,calls=client(monkeypatch,response)
    with pytest.raises(ApiError) as exc:api.claim_assignment('fixture','gpt-5.4','low')
    assert exc.value.code=='auth_runtime_unavailable'
    assert all(method=='GET' for method,_,_ in calls)


def test_matching_actual_contract_precedes_profile_claim(monkeypatch):
    contract={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,'capability':selection.TRIAL_CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.154.0'}]}
    api,calls=client(monkeypatch,httpx.Response(200,json=contract))
    api.claim_assignment('fixture','gpt-5.4','low')
    assert [method for method,_,_ in calls]==['GET','POST']
    assert b'auth_runtime=codex-managed-at-v1' in calls[1][2]


def test_default_compatibility_does_not_probe_new_endpoint(monkeypatch):
    api,calls=client(monkeypatch,httpx.Response(404),selected=False)
    api.claim_assignment('fixture','gpt-5.4','low')
    assert [method for method,_,_ in calls]==['POST']
    assert b'auth_runtime' not in calls[0][2]


def test_server_confirmed_other_provider_keeps_its_own_auth(monkeypatch):
    api,calls=client(monkeypatch,httpx.Response(200,json={'schema':'dradar.auth-runtime.v1','profiles':[],'applicable':False}))
    monkeypatch.setattr(selection,'load_selection',lambda:pytest.fail('unrelated Codex source checked'))
    api.claim_assignment('fixture','other-provider-model','low')
    assert [method for method,_,_ in calls]==['GET','POST']
    assert b'auth_runtime' not in calls[-1][2]


def test_mixed_held_codex_contracts_cannot_skip_legacy_row(monkeypatch):
    api,_=client(monkeypatch,httpx.Response(200,json={}))
    with pytest.raises(ApiError) as exc:
        api._check_managed_assignments({'active':[{'agent':'codex','auth_runtime':selection.PROFILE,'assignment_id':'a'*32,'auth_cohort_id':'b'*32},{'agent':'codex'}]})
    assert exc.value.code=='auth_runtime_mismatch'

@pytest.mark.parametrize('continuation',[False,True])
def test_old_globally_enabled_descriptor_cannot_authorize_trial(monkeypatch,continuation):
 old={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,'capability':selection.CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.154.0'}]}
 api,calls=client(monkeypatch,httpx.Response(200,json=old))
 with pytest.raises(ApiError) as error:
  if continuation:api._negotiate_managed_auth_runtime(assignment_id='a'*32)
  else:api.claim_assignment('fixture','gpt-5.4','low')
 assert error.value.code=='auth_runtime_unavailable'
 assert [method for method,_,_ in calls]==['GET']

@pytest.mark.parametrize('binding',[None,{}, {'schema':'unknown','assignment_id':'a'*32,'auth_cohort_id':'b'*32,'auth_runtime':selection.PROFILE}, {'schema':'dradar.managed_trial_binding.v1','assignment_id':'f'*32,'auth_cohort_id':'b'*32,'auth_runtime':selection.PROFILE}])
def test_continuation_requires_exact_server_binding(monkeypatch,binding):
 value={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,'capability':selection.TRIAL_CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.154.0'}],'binding':binding}
 api,calls=client(monkeypatch,httpx.Response(200,json=value))
 with pytest.raises(ApiError):api._negotiate_managed_auth_runtime(assignment_id='a'*32,expected_cohort_id='b'*32)
 assert [m for m,_,_ in calls]==['GET']

def test_confirmed_owned_binding_allows_continuation(monkeypatch):
 value={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,'capability':selection.TRIAL_CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.154.0'}],'binding':{'schema':'dradar.managed_trial_binding.v1','assignment_id':'a'*32,'auth_cohort_id':'b'*32,'auth_runtime':selection.PROFILE}}
 api,calls=client(monkeypatch,httpx.Response(200,json=value))
 assert api._negotiate_managed_auth_runtime(assignment_id='a'*32,expected_cohort_id='b'*32)==selection.PROFILE
 assert [m for m,_,_ in calls]==['GET']

@pytest.mark.parametrize('capability',[None,'codex-managed-trial-observation-v999'])
def test_missing_or_unknown_trial_marker_never_claims(monkeypatch,capability):
 descriptor={'id':selection.PROFILE,'agent':'codex','provider':'openai','agent_version':'0.154.0'}
 if capability is not None:descriptor['capability']=capability
 api,calls=client(monkeypatch,httpx.Response(200,json={'schema':'dradar.auth-runtime.v1','profiles':[descriptor]}))
 with pytest.raises(ApiError):api.claim_assignment('fixture','gpt-5.4','low')
 assert [m for m,_,_ in calls]==['GET']


@pytest.mark.parametrize('model', ['gpt-6-sol', 'gpt-6-luna'])
def test_new_model_needs_exact_managed_container_version_before_claim(monkeypatch, model):
    def descriptor(version):
        return {'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,
            'capability':selection.TRIAL_CAPABILITY,'agent':'codex','provider':'openai','agent_version':version}]}
    api,calls=client(monkeypatch,httpx.Response(200,json=descriptor('0.154.0')),gpt6=True)
    with pytest.raises(ApiError):api.claim_assignment('fixture',model,'medium')
    assert [method for method,_,_ in calls]==['GET']
    api,calls=client(monkeypatch,httpx.Response(200,json=descriptor('0.155.1')),gpt6=True)
    api.claim_assignment('fixture',model,'medium')
    assert [method for method,_,_ in calls]==['GET','POST']

@pytest.mark.parametrize('model', ['gpt-6-sol', 'gpt-6-luna'])
def test_new_model_bound_continuation_checks_new_version(monkeypatch, model):
    descriptor={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,
        'capability':selection.TRIAL_CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.155.1'}],
        'binding':{'schema':'dradar.managed_trial_binding.v1','assignment_id':'a'*32,
                   'auth_cohort_id':'b'*32,'auth_runtime':selection.PROFILE}}
    api,calls=client(monkeypatch,httpx.Response(200,json=descriptor),gpt6=True)
    api._check_managed_assignments({'active':[{'agent':'codex','provider':'openai','model':model,
        'auth_runtime':selection.PROFILE,'assignment_id':'a'*32,'auth_cohort_id':'b'*32}]})
    assert [method for method,_,_ in calls]==['GET']
