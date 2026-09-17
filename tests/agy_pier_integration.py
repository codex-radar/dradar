import asyncio,json,os,sys,time,uuid
from pathlib import Path
from pier.models.trial.config import TrialConfig, TaskConfig, AgentConfig, EnvironmentConfig, VerifierConfig
from pier.trial.trial import Trial
from dradar.runner import _verify_antigravity_export

ROOT=Path(os.environ['DRADAR_AGY_TEST_OUTPUT']).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
async def one(mode):
    root=ROOT / (mode+'-'+uuid.uuid4().hex[:8]); root.mkdir()
    task=root/'task'; (task/'environment').mkdir(parents=True)
    (task/'instruction.md').write_text(mode)
    (task/'task.toml').write_text('schema_version="1.3"\n[agent]\ntimeout_sec=60\n[environment]\ncpus=1\nmemory_mb=512\nallow_internet=false\n')
    (task/'pre_artifacts.sh').write_text('#!/bin/sh\ntest -f /logs/artifacts/model.patch\n')
    (task/'environment/Dockerfile').write_text('FROM dradar-0126-fixture:latest\nENTRYPOINT []\nWORKDIR /app\nRUN git init -q && git config user.email fixture@example.invalid && git config user.name fixture && printf "base\\n" > file && git add . && git commit -qm base && git tag pompeii-base\n')
    auth=root/'fake-auth';auth.mkdir()
    run_id=uuid.uuid4().hex
    config=TrialConfig(task=TaskConfig(path=task), trials_dir=root/'trials',
        agent=AgentConfig(import_path='agy_pier_fixture:FixtureAGY',model_name='gemini-3.7-flash',kwargs=dict(auth_home_dir=str(auth),reasoning_effort='low',artifact_base_commit='pompeii-base',artifact_run_id=run_id)),
        environment=EnvironmentConfig(type='docker'),verifier=VerifierConfig(disable=True),artifacts=['/logs/artifacts/model.patch'])
    trial=await Trial.create(config)
    events=[]
    collect=trial._collect_artifacts
    async def counted_collect():
        events.append('collect'); await collect()
    trial._collect_artifacts=counted_collect
    cleanup=trial._cleanup_and_finalize
    async def counted_cleanup():
        events.append('cleanup'); await cleanup()
    trial._cleanup_and_finalize=counted_cleanup
    execution=asyncio.create_task(trial.run())
    elapsed=None
    if mode in ('cancel','repeat'):
        deadline=time.monotonic()+70
        while not execution.done() and time.monotonic()<deadline:
            await asyncio.sleep(.2)
            try:
                result=await trial._environment.exec('test -f /logs/agent/ready', timeout_sec=2)
                if result.return_code == 0:break
            except Exception:pass
        assert not execution.done(), 'fixture never became ready'
        start=time.monotonic();execution.cancel()
        if mode=='repeat':
            await asyncio.sleep(.02); execution.cancel()
        try:await execution
        except asyncio.CancelledError:pass
        elapsed=time.monotonic()-start
    else:await execution
    directory=trial.trial_dir
    patch=directory/'artifacts/model.patch'
    # Pier's artifact basename layout is confirmed below, not presumed success.
    receipt=directory/'.dradar/agy-export.json'
    accepted=False
    try:
        _verify_antigravity_export(directory,patch,{'_artifact_run_id':run_id});accepted=True
    except Exception:pass
    expected=mode not in ('crash','failure')
    assert accepted==expected,(mode, list(directory.rglob('*')), receipt.read_text() if receipt.exists() else 'missing')
    assert events.index('collect') < events.index('cleanup')
    if elapsed is not None: assert elapsed<15,elapsed
    if accepted:
        assert (directory/'agent/model-calls').read_text()=='one'
        assert json.loads(receipt.read_text())['writer_stopped']
        assert bool(patch.read_bytes()) == (mode!='empty')
    assert not (directory/'agent/evil').exists()
    return dict(mode=mode,accepted=accepted,cancel_seconds=elapsed,events=events,trial_dir=str(directory))
async def main():
    results=[]
    for mode in sys.argv[1:] or ['normal','nonzero','empty','cancel','repeat','failure','crash','filter']:
        results.append(await one(mode)); print(json.dumps(results[-1]),flush=True)
    (ROOT/'results.json').write_text(json.dumps(results,indent=2))
asyncio.run(main())
