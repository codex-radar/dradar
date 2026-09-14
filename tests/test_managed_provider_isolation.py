from pathlib import Path
import httpx
import pytest
from dradar import runner,doctor,managed_auth_selection as selection
from dradar.api_client import ApiClient

class OrdinaryPathReached(RuntimeError):pass


def test_other_provider_claim_reaches_ordinary_runner_despite_managed_selection(tmp_path,monkeypatch):
    monkeypatch.setenv('DRADAR_CODEX_MANAGED_CONFIG',str(tmp_path/'missing.json'))
    monkeypatch.setattr(selection,'load_selection',lambda:pytest.fail('unrelated source loaded'))
    assignment={'agent':'codex','provider':'deepseek','model':'deepseek-v4-pro','effort':'high'}
    calls=[]
    def response(request):
        calls.append(request.method)
        if request.method=='GET':return httpx.Response(200,json={'schema':'dradar.auth-runtime.v1','profiles':[],'applicable':False})
        assert b'auth_runtime' not in request.content
        return httpx.Response(200,json={'assignment':assignment})
    api=ApiClient('https://fixture.invalid','fake',transport=httpx.MockTransport(response),capabilities=[])
    claimed=api.claim_assignment('fixture',assignment['model'],assignment['effort'])['assignment']
    monkeypatch.setattr(selection,'selection_requested',lambda:pytest.fail('runner inspected unrelated selection'))
    monkeypatch.setattr(runner,'preflight_artifact_platform',lambda path:None)
    def ordinary(*args,**kwargs):raise OrdinaryPathReached()
    monkeypatch.setattr(runner,'_validate_deepseek_assignment',ordinary)
    with pytest.raises(OrdinaryPathReached):runner.run_trial(claimed,tmp_path,tmp_path,on_worker_registered=lambda event:None)
    assert calls==['GET','POST']


def test_explicit_managed_request_cannot_override_other_provider(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'preflight_artifact_platform',lambda path:None)
    monkeypatch.setattr(runner,'_validate_deepseek_assignment',lambda *a,**k:pytest.fail('ordinary work started'))
    with pytest.raises(runner.RunnerError,match='explicit compatible assignment'):
        runner.run_trial({'agent':'codex','provider':'deepseek','model':'deepseek-v4-pro','effort':'high'},tmp_path,tmp_path,
                         managed_auth_config=tmp_path/'selection.json',on_worker_registered=lambda event:None)


def test_unknown_codex_provider_is_not_promoted_to_openai(tmp_path,monkeypatch):
    monkeypatch.setenv('DRADAR_CODEX_MANAGED_CONFIG',str(tmp_path/'missing.json'))
    monkeypatch.setattr(selection,'selection_requested',lambda:pytest.fail('unknown provider treated as OpenAI'))
    monkeypatch.setattr(runner,'preflight_artifact_platform',lambda path:None)
    def original(*args,**kwargs):raise OrdinaryPathReached()
    monkeypatch.setattr(runner,'resolve_latest_codex_cli_version',original)
    with pytest.raises(OrdinaryPathReached):
        runner.run_trial({'agent':'codex','provider':'future-provider','model':'fixture','effort':'low'},tmp_path,tmp_path)


def test_plain_openai_assignment_still_cannot_be_silently_rebound(tmp_path,monkeypatch):
    monkeypatch.setenv('DRADAR_CODEX_MANAGED_CONFIG',str(tmp_path/'selection.json'))
    monkeypatch.setattr(runner,'preflight_artifact_platform',lambda path:None)
    with pytest.raises(runner.RunnerError,match='explicit compatible assignment'):
        runner.run_trial({'agent':'codex','provider':'openai','model':'fixture','effort':'low'},tmp_path,tmp_path,
                         on_worker_registered=lambda event:None)


def infrastructure(monkeypatch, *, codex=True):
    monkeypatch.setattr(doctor.shutil,'which',lambda name:'/fixture/docker' if name=='docker' else None)
    monkeypatch.setattr(doctor,'_probe',lambda args:True)
    monkeypatch.setattr(runner,'ensure_pier',lambda:None)
    monkeypatch.setattr(runner,'_resolve_user_tool',lambda name:'/fixture/codex' if codex and name=='codex' else None)
    monkeypatch.setattr(doctor,'deepseek_api_key',lambda:'fixture-static-key')
    monkeypatch.setattr(doctor,'deepseek_catalog_error',lambda:None)


def test_deepseek_plan_never_checks_corrupt_or_revoked_managed_source(monkeypatch):
    infrastructure(monkeypatch)
    monkeypatch.setattr(selection,'readiness',lambda:pytest.fail('unrelated managed source checked'))
    monkeypatch.setattr(selection,'selection_requested',lambda:pytest.fail('unrelated managed selection checked'))
    assert doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':'deepseek'}]}) is None


def test_ready_managed_source_cannot_skip_deepseek_native_installation(monkeypatch):
    infrastructure(monkeypatch,codex=False)
    monkeypatch.setattr(selection,'readiness',lambda:('managed',True))
    issue=doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':'deepseek'}]})
    assert issue['error_code']=='codex_not_installed'


def test_persisted_managed_profile_requires_managed_source_not_native_fallback(monkeypatch):
    infrastructure(monkeypatch)
    monkeypatch.setattr(selection,'readiness',lambda:('native',False))
    monkeypatch.setattr(runner,'codex_auth_path',lambda:pytest.fail('native fallback checked'))
    issue=doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':'openai','auth_runtime':selection.PROFILE}]})
    assert issue['error_code']=='managed_auth_unavailable'


def test_mixed_plan_keeps_each_runtime_prerequisite(monkeypatch):
    infrastructure(monkeypatch,codex=False)
    monkeypatch.setattr(selection,'readiness',lambda:('managed',True))
    issue=doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':'openai','auth_runtime':selection.PROFILE},{'provider':'deepseek'}]})
    assert issue['error_code']=='codex_not_installed'


def test_plain_openai_plan_rejects_local_mode_mismatch_without_reading_source(monkeypatch):
    infrastructure(monkeypatch)
    monkeypatch.setattr(selection,'selection_requested',lambda:True)
    monkeypatch.setattr(selection,'readiness',lambda:pytest.fail('ordinary profile loaded managed source'))
    issue=doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':'openai'}]})
    assert issue['error_code']=='auth_runtime_mismatch'


def test_unknown_persisted_runtime_never_falls_back_to_native(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'preflight_artifact_platform',lambda path:None)
    monkeypatch.setattr(selection,'selection_requested',lambda:pytest.fail('unknown runtime looked for source'))
    with pytest.raises(runner.RunnerError,match='unsupported authentication runtime'):
        runner.run_trial({'agent':'codex','provider':'openai','auth_runtime':'future-runtime'},tmp_path,tmp_path)


@pytest.mark.parametrize('provider,profile',[('openai','future-runtime'),('deepseek',selection.PROFILE)])
def test_plan_rejects_unknown_or_contradictory_persisted_profile(monkeypatch,provider,profile):
    infrastructure(monkeypatch)
    monkeypatch.setattr(selection,'readiness',lambda:pytest.fail('bad profile loaded a source'))
    issue=doctor.plan_environment_issue({'harness':'codex','assignments':[{'provider':provider,'auth_runtime':profile}]})
    assert issue['error_code']=='current_tool_unsupported'
