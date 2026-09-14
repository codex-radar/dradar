import base64
import json
import pytest
from dradar.auth_access import project_access, ConsumptionEvidence, AccessUnavailable

KEY=b'fixture-local-key'*3

def codex(exp=300):
    payload=base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')
    return json.dumps({'tokens':{'access_token':'fake.'+payload+'.fake','refresh_token':'SECRET-RT','id_token':'SECRET-ID'}}).encode()

def test_projection_excludes_refresh_and_id_token_and_uses_expiry_only_as_hint():
    material=project_access('codex',codex(),local_key=KEY)
    assert material.usable(100)
    assert not material.usable(250)
    assert 'SECRET' not in str(material) and 'SECRET' not in material.token

def test_claude_projection_and_api_key_rejected():
    data={'claudeAiOauth':{'accessToken':'fakeAT','refreshToken':'SECRET-RT','expiresAt':300000,'scopes':['user:inference']}}
    assert project_access('claude-code',json.dumps(data).encode(),local_key=KEY).token=='fakeAT'
    data['ANTHROPIC_API_KEY']='SECRET-KEY'
    with pytest.raises(AccessUnavailable): project_access('claude-code',json.dumps(data).encode(),local_key=KEY)

@pytest.mark.parametrize('content',[b'{}', b'not-json', b'{"OPENAI_API_KEY":"SECRET"}', codex(True), codex(-1)])
def test_invalid_projection_never_echoes_contents(content):
    with pytest.raises(AccessUnavailable) as e: project_access('codex',content,local_key=KEY)
    assert 'SECRET' not in str(e.value)

def test_delivery_adoption_and_request_are_separate_and_generation_scoped():
    material=project_access('codex',codex(),local_key=KEY)
    evidence=ConsumptionEvidence(material)
    with pytest.raises(AccessUnavailable): evidence.adopted_generation(material.revision)
    evidence.delivered_generation(material.revision)
    assert evidence.summary()['adopted']=='unknown'
    with pytest.raises(AccessUnavailable): evidence.request_result(material.revision,accepted=True)
    evidence.adopted_generation(material.revision)
    evidence.request_result(material.revision,accepted=True)
    next_material=project_access('codex',codex(500),local_key=KEY)
    next_evidence=ConsumptionEvidence(next_material)
    with pytest.raises(AccessUnavailable): next_evidence.delivered_generation(material.revision)
    assert next_evidence.summary()['request']=='unknown'
    assert material.revision not in repr(evidence) and material.token not in str(evidence.summary())
