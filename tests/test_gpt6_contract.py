"""Offline contracts only: no provider credentials, requests, or Docker runs."""
import pytest
from dradar.gpt6 import GPT6_EFFORTS, GPT6_CAPABILITY, GPT6_CODEX_VERSION
from dradar import runner
from dradar.providers import advertised_capabilities

@pytest.mark.parametrize('benchmark', ['deep-swe', 'pompeii-adjacency'])
@pytest.mark.parametrize('model,effort', [(m,e) for m,es in GPT6_EFFORTS.items() for e in es])
def test_dispatch_preserves_model_effort_and_pins_install(tmp_path, monkeypatch, benchmark, model, effort):
    monkeypatch.setattr(runner.shutil, 'which', lambda _: '/fixture/pier')
    task=tmp_path/'task';task.mkdir()
    (task/'task.toml').write_text('[agent]\ntimeout_sec = 7200\n')
    auth=tmp_path/'auth.json';auth.write_text('{}')
    monkeypatch.setenv('CODEX_AUTH_JSON_PATH',str(auth))
    home=tmp_path/'home';home.mkdir()
    a={'assignment_id':'fixture','task_id':'task','agent':'codex','model':model,
       'effort':effort,'agent_version':GPT6_CODEX_VERSION,'benchmark_id':benchmark}
    cmd=runner.build_pier_command(a,tmp_path,tmp_path/'jobs','fixture',home)
    assert cmd[cmd.index('--model')+1]==model
    assert f'reasoning_effort={effort}' in cmd
    assert f'version={GPT6_CODEX_VERSION}' in cmd
    assert '--disable-verification' in cmd
    assert all('gpt-5.6-' not in value for value in cmd)
    # Inspect the real installed Pier adapter, not a hand-written fake argv.
    from pier.agents.installed.codex import Codex
    agent=Codex(logs_dir=tmp_path/'logs',model_name=model,version=GPT6_CODEX_VERSION,reasoning_effort=effort)
    assert any(f'@openai/codex@{GPT6_CODEX_VERSION}' in str(c) for c in agent.install_spec().steps)

@pytest.mark.parametrize('model,effort,version', [
    ('gpt-6-luna','ultra','0.155.1'),('gpt-6-sol','bogus','0.155.1'),
    ('gpt-6-sol','medium','0.154.0'),('gpt-6-sol','medium','latest'),
    ('gpt-6-sol-unknown','medium','0.155.1'),
    ('gpt-6-foo','medium','0.155.1'),
])
def test_invalid_contract_rejected_before_execution(model,effort,version):
    with pytest.raises(runner.RunnerError):
        runner._validate_gpt6_assignment({'model':model,'effort':effort,'agent_version':version})

def test_existing_model_contract_is_not_rewritten():
    runner._validate_gpt6_assignment({'model':'gpt-5.6-sol','effort':'ultra','agent_version':'0.154.0'})

def test_new_client_advertises_gpt6_contract():
    assert GPT6_CAPABILITY in advertised_capabilities()

@pytest.mark.parametrize('stdout,code,accepted', [
    ('codex-cli 0.155.1',0,True),('known warning\ncodex-cli 0.155.1',0,True),
    ('codex-cli 0.154.0',0,False),('codex-cli 0.155.1',1,False),
    ('codex-cli 0.155.1\ncodex-cli 0.154.0',0,False),('',0,False),
])
def test_actual_container_binary_is_checked_before_model_execution(tmp_path, monkeypatch, stdout, code, accepted):
    import asyncio
    from types import SimpleNamespace
    from dradar.pier_codex import CodexRegistered
    agent=CodexRegistered(logs_dir=tmp_path,model_name='gpt-6-sol',version='0.155.1')
    async def execute(environment,command,**kwargs):
        assert command.endswith('codex --version')
        return SimpleNamespace(stdout=stdout,return_code=code)
    monkeypatch.setattr(agent,'exec_as_agent',execute)
    if accepted:
        asyncio.run(agent.verify_gpt6_runtime(object()))
    else:
        with pytest.raises(RuntimeError,match='container version'):
            asyncio.run(agent.verify_gpt6_runtime(object()))
