from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from dradar.v2.journal import Journal
from dradar.v2.runtime_cache import PublicImagePreparation
from dradar.v2.image_binding.core import BindingError, read_private_json
from dradar.shared_cache import ArtifactKey, Artifact, Lease
from dradar import runner
from test_runner_exit_evidence import runtime as old_runtime

PIN='python@sha256:'+'a'*64
BASE='sha256:'+'b'*64

class Cache:
    context='test'; daemon_id='daemon'
    def __init__(self,root): self.acquired=[]
    def _image(self,ref): return {'Id':BASE,'RepoDigests':[PIN]}
    def ensure_pinned_image(self,ref,*,platform,attempt):
        self.acquired.append((ref,platform,attempt))
        return Lease(ArtifactKey('image',ref.rsplit('@',1)[1],platform),'lease',Artifact(BASE,1),True)
    def _json(self,args): return [{'Endpoints':{'docker':{'Host':'unix:///test.sock','SkipTLSVerify':False}}}]
    def _command(self,args):
        return SimpleNamespace(stdout='Name: shared\nDriver: docker-container\nNodes:\nEndpoint: test\nStatus: running\n')

@pytest.fixture
def scope(tmp_path):
    root=tmp_path.resolve()
    task=root/'tasks/task'; (task/'environment').mkdir(parents=True)
    (task/'instruction.md').write_text('synthetic')
    (task/'task.toml').write_text('[environment]\ndocker_image="python:synthetic"\nallow_internet=true\n')
    (task/'environment/Dockerfile').write_text('ARG BASE\nFROM ${BASE}\n')
    work=root/'work'; work.mkdir()
    j=Journal(root/'state')
    obj=PublicImagePreparation(j,platform='linux/arm64',public_references=['python:synthetic'],cache_root=root/'cache',cache_factory=Cache)
    arguments=dict(assignment={'task_id':'task','_runner_session_id':'e'*32,'task_content_hash':'c'*64},tasks_root=task.parent,work_dir=work,job_root=work/'jobs/job',builder_lease=SimpleNamespace(name='shared',isolated=True,reusable=True))
    return obj,arguments,task

def test_public_preparation_pins_private_copy_and_real_attempt(scope):
    obj,args,task=scope
    original=(task/'task.toml').read_bytes()
    tasks,argv=obj.prepare(**args)
    assert (task/'task.toml').read_bytes()==original
    assert PIN in (tasks/'task/task.toml').read_text()
    assert '--no-delete' in argv and 'descriptor_path=' in argv[-2]
    assert obj.cache.acquired==[(PIN,'linux/arm64','e'*32)]
    identity=read_private_json(obj.descriptor)['binding']
    assert identity['original_package_hash']=='c'*64 and identity['base_lease_token']=='lease'
    assert 'session_id' not in identity # Actual Pier allocator must provide it.
    env={};obj.child_environment(env)
    assert Path(env['PYTHONPATH'])==args['work_dir']/'public-image-bootstrap'
    assert env['DOCKER_DEFAULT_PLATFORM']=='linux/arm64'

def test_baseline_removed_base_is_never_restored(scope):
    obj,args,task=scope
    (task/'task.toml').write_text('[environment]\nallow_internet=false\n')
    tasks,argv=obj.prepare(**args)
    assert tasks==task.parent and argv==[] and obj.cache is None
    assert obj.verify() is None

def test_unapproved_or_nonshared_base_fails_before_acquire(scope):
    obj,args,task=scope
    obj.approved=frozenset(['foreign'])
    with pytest.raises(BindingError): obj.prepare(**args)
    assert obj.cache is None

def test_unknown_preparation_does_not_create_second_lease(scope):
    obj,args,task=scope
    obj.prepare(**args)
    with pytest.raises(BindingError): obj.prepare(**args)
    assert len(obj.cache.acquired)==1

def test_missing_image_proof_denies_parent_paid_gate(scope):
    obj,args,_=scope
    obj.prepare(**args)
    with pytest.raises((BindingError,FileNotFoundError)): obj.verify()

def test_runner_passes_cache_hook_after_overlay_before_real_spawn(old_runtime):
    state=old_runtime
    calls=[]
    class Preparation:
        def prepare(self,**kwargs):
            calls.append('prepare')
            assert kwargs['assignment']['assignment_id']==state['assignment']['assignment_id']
            return kwargs['tasks_root'],[]
        def child_environment(self,env): calls.append('child_env')
    runner.run_trial(state['assignment'],state['tasks'],state['work'],
                     public_image_preparer=Preparation(),on_worker_registered=lambda _:calls.append('registered'),execution_observer=state['observer'])
    assert calls==['prepare','child_env','registered']
    assert any(e['event']=='confirmed_absent' for e in state['events'])
