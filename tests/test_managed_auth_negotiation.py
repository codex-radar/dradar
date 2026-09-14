import httpx
import pytest
from dradar import managed_auth_selection as selection
from dradar.api_client import ApiClient, ApiError


def client(monkeypatch, response, *, selected=True):
    monkeypatch.setattr(selection,'load_selection',lambda: object() if selected else None)
    monkeypatch.setattr(selection,'selection_requested',lambda:selected)
    calls=[]
    def request(req):
        calls.append((req.method,req.url.path,req.content))
        if req.method=='GET':return response
        return httpx.Response(200,json={'assignment':{'auth_runtime':selection.PROFILE}})
    api=ApiClient('https://fixture.invalid','fake-server-token',transport=httpx.MockTransport(request),capabilities=[selection.CAPABILITY])
    return api,calls


@pytest.mark.parametrize('response',[httpx.Response(404),httpx.Response(200,json={}),httpx.Response(200,json={'schema':'dradar.auth-runtime.v1','profiles':[]})])
def test_old_unknown_or_disabled_server_never_receives_claim(monkeypatch,response):
    api,calls=client(monkeypatch,response)
    with pytest.raises(ApiError) as exc:api.claim_assignment('fixture','gpt-5.4','low')
    assert exc.value.code=='auth_runtime_unavailable'
    assert all(method=='GET' for method,_,_ in calls)


def test_matching_actual_contract_precedes_profile_claim(monkeypatch):
    contract={'schema':'dradar.auth-runtime.v1','profiles':[{'id':selection.PROFILE,'capability':selection.CAPABILITY,'agent':'codex','provider':'openai','agent_version':'0.154.0'}]}
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
        api._check_managed_assignments({'active':[{'agent':'codex','auth_runtime':selection.PROFILE},{'agent':'codex'}]})
    assert exc.value.code=='auth_runtime_mismatch'
