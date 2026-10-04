import asyncio
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

try:
    import pier
except ModuleNotFoundError:
    raise unittest.SkipTest('The actual pinned Pier interpreter is required for adapter tests')

from dradar.v2.image_binding.core import (BackendOperationError, Binding, BindingError, CommandResult, Inspector,
    atomic_private_json, builder_inspect_hash, normalize_platform, read_private_json, state_record, tree_hash,
    verify_paid_image_proof)
from dradar.v2.image_binding.preparation import binding_cli_args, pin_private_task
from dradar.v2.image_binding.pier_adapter import BoundPrebuiltDockerEnvironment
from dradar.v2.image_binding.source_fence import PINNED_HASHES, verify_pinned_pier
from pier.environments.base import ExecResult
from pier.environments.docker.docker import DockerEnvironment
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.task.config import EnvironmentConfig
from pier.models.trial.paths import TrialPaths

BASE = 'sha256:' + 'b' * 64
RUNTIME = 'sha256:' + 'c' * 64
CID = 'd' * 64
PROXY_CID = 'f' * 64
AUX = 'sha256:' + 'e' * 64
DIGEST = 'registry.example/org/task@sha256:' + 'a' * 64
BUILDER_OUTPUT='Name: shared-builder\nDriver: docker-container\nLast Activity: yesterday\nNodes:\nName: shared-builder0\nEndpoint: test-context\nStatus: running\nBuildKit: v0.20.2\nPlatforms: linux/amd64\n'
CANDIDATE = Path(__file__).resolve().parents[1]


class InjectedDocker:
    """Only explicitly constructed Docker outputs; never invokes a Docker process."""
    def __init__(self, binding):
        self.b = binding
        self.calls = []
        self.image_override = {}
        self.auxiliary_platform = 'amd64'
        self.container_override = {}
        self.occupied = False
        self.started = False
        self.proxy = False
        self.extra_cid = None
        self.networks = {}
        self.context = binding.context_name
        self.daemon = binding.daemon_id
        self.compose_paths = []
        self.runtime = RUNTIME
        self.builder_output = BUILDER_OUTPUT

    async def __call__(self, command):
        self.calls.append(command)
        a = command[1:]
        if a == ['buildx','inspect',self.b.builder_name]:
            return CommandResult(0,self.builder_output)
        if a == ['context','show']:
            return CommandResult(0, self.context + '\n')
        if a == ['context','inspect', self.b.context_name]:
            result = [{'Endpoints': {'docker': {'Host': self.b.endpoint, 'SkipTLSVerify':False}}}]
        elif a == ['info','--format','{{json .}}']:
            result = {'ID':self.daemon, 'OSType':'linux'}
        elif a[:2] == ['image','inspect']:
            selector = a[2]
            image_id = BASE if selector in (DIGEST, BASE) else AUX if selector == AUX or selector.endswith("-pier-egress-proxy:latest") else RUNTIME
            result = [{'Id':image_id,'Os':'linux','Architecture':'amd64','RepoDigests':[DIGEST]}]
            if image_id == AUX:
                result[0]['Architecture'] = self.auxiliary_platform
            result[0].update(self.image_override)
        elif a[:2] == ['container','inspect']:
            result = [{'Id':CID, 'Image':self.runtime, 'State':{'Running':True},
                'Config':{'Labels':{'com.docker.compose.project':self.b.project,
                    'com.docker.compose.service':'main','com.docker.compose.oneoff':'False',
                    'com.docker.compose.project.working_dir':self.b.environment_dir,
                    'com.docker.compose.project.config_files':','.join(self.compose_paths)}},
                'Mounts':[{'Type':'bind','Source':str(Path(self.b.trial_dir)/'agent')}]}]
            if a[2] == PROXY_CID:
                result[0]['Id']=PROXY_CID;result[0]['Image']=AUX;result[0]['Mounts']=[]
                result[0]['Config']['Labels']['com.docker.compose.service']='pier-egress-proxy'
            else:
                result[0].update(self.container_override)
        elif a[:2] == ['ps','-aq']:
            ids=[CID] if self.occupied or self.started else []
            if self.started and self.proxy:ids.append(PROXY_CID)
            if self.extra_cid:ids.append(self.extra_cid)
            return CommandResult(0, '\n'.join(ids))
        elif a[:2] == ['network','ls']:
            return CommandResult(0, '\n'.join(self.networks))
        elif a[:2] == ['network','inspect']:
            result=[self.networks[a[2]]]
        elif a[:2] == ['volume','ls']:
            return CommandResult(0,'')
        else:
            raise AssertionError('No injected output for '+repr(command))
        return CommandResult(0,json.dumps(result))


class InjectedEnvironment(BoundPrebuiltDockerEnvironment):
    def _validate_daemon_mode(self):
        # Inspector.same_daemon has explicit injected authoritative info output.
        pass

    async def _compose(self, command, check, timeout_sec):
        self.compose_calls.append((command[:], [str(p.resolve()) for p in self._docker_compose_paths], self._env_vars.prebuilt_image_name))
        if command[0] == 'build' and self.build_error:
            raise self.build_error
        if command[0] == 'up':
            self.fake.compose_paths = [str(p.resolve()) for p in self._docker_compose_paths]
            self.fake.runtime = self._env_vars.prebuilt_image_name
            self.fake.started=True
            self.fake.proxy=self._egress_proxy_compose_path is not None
        if command[0] == 'ps':
            return ExecResult(stdout=CID+'\n',stderr='',return_code=0)
        return ExecResult(stdout='',stderr='',return_code=0)


class BindingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=CANDIDATE)
        self.root = Path(self.temp.name)
        os.chmod(self.root,0o700)
        self.task = self.root/'task'; (self.task/'environment').mkdir(parents=True)
        (self.task/'task.toml').write_text('[environment]\ndocker_image = "'+DIGEST+'"\n')
        (self.task/'environment/Dockerfile').write_text('ARG BASE\nFROM ${BASE}\n')
        self.job=self.root/'job'; self.trial=self.job/'trial'; self.trial.mkdir(parents=True)
        records=self.job/'.binding'; records.mkdir(mode=0o700)
        for path in ('agent','verifier','artifacts'):
            (self.trial/path).mkdir()
        self.b=Binding(1,'attempt-0001','nonce-000001','0'*64,tree_hash(self.task),tree_hash(self.task),'org/task:mutable',DIGEST,'linux/amd64',BASE,'lease-000001','daemon-0001','test-context','unix:///test.sock','shared-builder','task__attempt0001','task__attempt0001',str(self.task),str(self.task/'environment'),str(self.job),str(self.trial),str(records/'state.json'),str(records/'proof.json'),manifest_digest='sha256:'+'a'*64,builder_inspect_sha256=builder_inspect_hash(BUILDER_OUTPUT))
        self.binding_path=records/'binding.json'
        atomic_private_json(self.binding_path,self.b.identity())
        self.environments=[]
        self.fake=InjectedDocker(self.b)
        self.env_patch=patch.dict(os.environ,{'BUILDX_BUILDER':self.b.builder_name},clear=True);self.env_patch.start()
        self.tempdir_patch=patch.object(tempfile,'tempdir',str(self.root));self.tempdir_patch.start()

    def tearDown(self):
        for env in self.environments:env._cleanup_resources_compose_file()
        self.tempdir_patch.stop(); self.env_patch.stop(); self.temp.cleanup()

    def environment(self, installer=True, config=None, **kwargs):
        spec=AgentInstallSpec(agent_name='codex', steps=[InstallStep(run='echo installer')]) if installer else None
        env=InjectedEnvironment(binding_path=str(self.binding_path),inspection_command=self.fake,
            environment_dir=self.task/'environment',environment_name='task',session_id=self.b.session_id,
            trial_paths=TrialPaths(self.trial),task_env_config=config or EnvironmentConfig(docker_image=DIGEST),
            agent_install_spec=spec, **kwargs)
        env.fake=self.fake;env.compose_calls=[];env.build_error=None
        self.environments.append(env)
        return env

    async def gate(self, lease=True, attempt='attempt-0001', nonce='nonce-000001'):
        async def live(binding): return lease
        return await verify_paid_image_proof(self.binding_path,expected_attempt_id=attempt,expected_nonce=nonce,lease_check=live,command=self.fake)

    async def test_installer_pins_captures_freezes_and_proves(self):
        env=self.environment()
        await env.start(False)
        dockerfile=(self.trial/'agent-build-context/Dockerfile').read_text()
        self.assertEqual(dockerfile.splitlines()[0],'FROM '+DIGEST)
        build=[x for x in env.compose_calls if x[0][0]=='build'];up=[x for x in env.compose_calls if x[0][0]=='up']
        self.assertEqual(len(build),1); self.assertEqual(len(up),1)
        self.assertIn('docker-compose-build.yaml',' '.join(build[0][1]))
        self.assertNotIn('docker-compose-build.yaml',' '.join(up[0][1]))
        self.assertEqual(up[0][2],RUNTIME)
        self.assertEqual(up[0][0][-3:],['--no-build','--pull','never'])
        self.assertIn('docker-compose-prebuilt.yaml',' '.join(up[0][1]))
        proof=await self.gate()
        self.assertEqual(proof['runtime_image_id'],RUNTIME)
        self.assertTrue(proof['build_completed'])
        self.assertTrue(any(c[-1]==self.b.project+'-main:latest' for c in self.fake.calls))
        self.assertFalse(any('hb__' in c[-1] for c in self.fake.calls))
        await env.stop(True)
        self.assertTrue(any(c[0]==['down'] for c in env.compose_calls))
        self.assertFalse(any('--rmi' in c[0] for c in env.compose_calls))
        state=read_private_json(Path(self.b.state_path))
        self.assertEqual(state['phase'],'cleanup_submitted')
        self.assertFalse(state['release_authorized'])
        with self.assertRaises(BindingError):await self.gate()

    async def test_installer_filtered_egress_proxy_has_no_implicit_solve(self):
        env=self.environment(config=EnvironmentConfig(docker_image=DIGEST,allow_internet=False),network_allowlist=NetworkAllowlist(domains=['api.example.com']))
        await env.start(False)
        proxy=json.loads((self.trial/'docker-compose-egress-proxy.json').read_text())
        self.assertIn('build',proxy['services']['pier-egress-proxy'])
        up=[x for x in env.compose_calls if x[0][0]=='up'][0]
        self.assertEqual(up[0][-3:],['--no-build','--pull','never'])
        self.assertEqual((await self.gate())['runtime_image_id'],RUNTIME)

    def pinned_proxy(self, *, path, proxy_dir, allowlist, token):
        path.write_text(json.dumps({'services': {
            'main': {'networks': ['pier-egress-internal']},
            'pier-egress-proxy': {'image': AUX, 'pull_policy': 'never',
                                 'environment': {'PROXY_TOKEN': token}}},
            'networks': {'pier-egress-internal': {'internal': True}}}))
        return path

    async def test_prepared_native_proxy_and_emulated_main_prove_and_cleanup(self):
        self.fake.auxiliary_platform = 'arm64'
        env = self.environment(config=EnvironmentConfig(docker_image=DIGEST, allow_internet=False),
                               network_allowlist=NetworkAllowlist(domains=['api.example.com']))
        with patch.dict(os.environ, {'DRADAR_EGRESS_PROXY_IMAGE': AUX}), \
             patch('pier.environments.docker.docker.write_docker_proxy_compose', self.pinned_proxy):
            await env.start(False)
            proxy = json.loads((self.trial/'docker-compose-egress-proxy.json').read_text())
            service = proxy['services']['pier-egress-proxy']
            self.assertEqual(service['image'], AUX)
            self.assertEqual(service['platform'], 'linux/arm64')
            self.assertNotIn('build', service)
            proof = await self.gate()
            self.assertEqual(proof['platform'], 'linux/amd64')
            self.assertEqual(proof['project_resources']['services']['pier-egress-proxy']['image_id'], AUX)
            self.assertFalse(any(c[-1] == self.b.project+'-pier-egress-proxy:latest' for c in self.fake.calls))
            await env.stop(False)
            self.assertEqual(read_private_json(Path(self.b.state_path))['phase'], 'cleanup_submitted')

    async def test_proxy_from_foreign_runtime_selector_denied_before_build(self):
        env = self.environment(config=EnvironmentConfig(docker_image=DIGEST, allow_internet=False),
                               network_allowlist=NetworkAllowlist(domains=['api.example.com']))
        with patch.dict(os.environ, {'DRADAR_EGRESS_PROXY_IMAGE': BASE}), \
             patch('pier.environments.docker.docker.write_docker_proxy_compose', self.pinned_proxy):
            with self.assertRaisesRegex(BindingError, 'proxy selector differs'):
                await env.start(False)
        self.assertEqual(env.compose_calls, [])

    async def test_native_proxy_does_not_relax_main_platform_validation(self):
        self.fake.auxiliary_platform = 'arm64'
        self.fake.image_override = {'Architecture': 'arm64'}
        with self.assertRaisesRegex(BindingError, 'image platform mismatch'):
            await self.environment().start(False)

    async def test_changed_proxy_platform_denies_paid_proof_and_cleanup(self):
        self.fake.auxiliary_platform = 'arm64'
        env = self.environment(config=EnvironmentConfig(docker_image=DIGEST, allow_internet=False),
                               network_allowlist=NetworkAllowlist(domains=['api.example.com']))
        with patch.dict(os.environ, {'DRADAR_EGRESS_PROXY_IMAGE': AUX}), \
             patch('pier.environments.docker.docker.write_docker_proxy_compose', self.pinned_proxy):
            await env.start(False)
        path = self.trial/'docker-compose-egress-proxy.json'
        config = json.loads(path.read_text())
        config['services']['pier-egress-proxy']['platform'] = 'linux/amd64'
        path.write_text(json.dumps(config))
        with self.assertRaises(BindingError): await self.gate()
        with self.assertRaises(BindingError): await env.stop(False)
        self.assertFalse(any(c[0] == ['down'] for c in env.compose_calls))

    async def test_direct_filtered_proxy_is_preparation_blocker(self):
        env=self.environment(False,config=EnvironmentConfig(docker_image=DIGEST,allow_internet=False),network_allowlist=NetworkAllowlist(domains=['api.example.com']))
        with self.assertRaises(BindingError):await env.start(False)
        self.assertFalse(Path(self.b.state_path).exists())
        self.assertEqual(env.compose_calls,[])

    async def test_compose_mutation_denies_paid_and_cleanup(self):
        env=self.environment();await env.start(False)
        (self.trial/'docker-compose-mounts.json').write_text('{"services":{"main":{"image":"foreign:latest"}}}')
        with self.assertRaises(BindingError):await self.gate()
        with self.assertRaises(BindingError):await env.stop(True)
        self.assertFalse(any(c[0]==['down'] for c in env.compose_calls))

    async def test_extra_project_container_denies_cleanup(self):
        env=self.environment();await env.start(False)
        self.fake.extra_cid='1'*64
        with self.assertRaises(BindingError):await self.gate()
        with self.assertRaises(BindingError):await env.stop(False)

    async def test_preexisting_project_network_denied(self):
        self.fake.networks={'2'*64:{'Id':'2'*64,'Labels':{'com.docker.compose.project':self.b.project}}}
        with self.assertRaises(BindingError):await self.environment().start(False)

    async def test_new_foreign_network_denies_cleanup(self):
        env=self.environment();await env.start(False)
        nid='2'*64
        self.fake.networks={nid:{'Id':nid,'Name':self.b.project+'_default','Labels':{'com.docker.compose.project':self.b.project,'com.docker.compose.network':'default'},'Containers':{'3'*64:{}}}}
        with self.assertRaises(BindingError):await env.stop(False)

    def test_copy_race_added_regular_file_denied(self):
        real=shutil.copytree
        def transient(source,dest,*args,**kwargs):
            source=Path(source)
            if source != self.task:return real(source,dest,*args,**kwargs)
            (source/'raced-file').write_text('unexpected')
            try:return real(source,dest,*args,**kwargs)
            finally:(source/'raced-file').unlink()
        with patch('dradar.v2.image_binding.preparation.shutil.copytree',side_effect=transient):
            with self.assertRaises(BindingError):pin_private_task(self.task,self.root/'selected-task',DIGEST)

    def test_copy_race_symlink_not_dereferenced(self):
        real=shutil.copytree
        private=self.root/'private';private.write_text('host-private')
        def transient(source,dest,*args,**kwargs):
            source=Path(source)
            if source != self.task:return real(source,dest,*args,**kwargs)
            (source/'raced-link').symlink_to(private)
            try:return real(source,dest,*args,**kwargs)
            finally:(source/'raced-link').unlink()
        with patch('dradar.v2.image_binding.preparation.shutil.copytree',side_effect=transient):
            with self.assertRaises(BindingError):pin_private_task(self.task,self.root/'selected-task',DIGEST)
        self.assertTrue((self.root/'selected-task/raced-link').is_symlink())

    async def test_generated_context_mutation_denies_paid_gate(self):
        env=self.environment();await env.start(False)
        (self.trial/'agent-build-context/Dockerfile').write_text('FROM foreign:latest\n')
        with self.assertRaises(BindingError):await self.gate()

    async def test_direct_prebuilt_never_builds(self):
        env=self.environment(False);await env.start(False)
        self.assertFalse(any(c[0][0]=='build' for c in env.compose_calls))
        self.assertEqual((await self.gate())['runtime_image_id'],BASE)
        self.assertEqual(env._env_vars.prebuilt_image_name,BASE)

    async def test_direct_force_build_denied(self):
        env=self.environment(False)
        with self.assertRaises(BindingError):await env.start(True)
        self.assertFalse(Path(self.b.state_path).exists())

    async def test_unknown_build_cancel_is_quarantined(self):
        env=self.environment();env.build_error=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await env.start(False)
        state=read_private_json(Path(self.b.state_path))
        self.assertEqual(state['phase'],'build_quarantined');self.assertFalse(state['release_authorized'])
        self.assertFalse(Path(self.b.proof_path).exists())
        with self.assertRaises(BindingError):await env.stop(True)
        self.assertFalse(any(c[0][0]=='down' for c in env.compose_calls))

    async def test_compose_enospc_is_scrubbed_storage_failure(self):
        env=self.environment()
        async def failure(*args,**kwargs):
            raise RuntimeError('injected private diagnostic: no space left on device')
        with patch.object(DockerEnvironment,'_run_docker_compose_command',side_effect=failure):
            with self.assertRaises(BackendOperationError) as caught:
                await BoundPrebuiltDockerEnvironment._compose(env,['build'],True,None)
        self.assertEqual(caught.exception.code,'storage_enospc')
        self.assertEqual(str(caught.exception),'storage_enospc')

    async def test_failed_build_is_unknown_not_absent(self):
        env=self.environment();env.build_error=RuntimeError('injected build failure')
        with self.assertRaises(RuntimeError):await env.start(False)
        self.assertEqual(read_private_json(Path(self.b.state_path))['phase'],'build_quarantined')

    async def test_foreign_existing_project_denied_without_cleanup(self):
        env=self.environment();self.fake.occupied=True
        with self.assertRaises(BindingError):await env.start(False)
        self.assertEqual(env.compose_calls,[])

    async def test_one_attempt_cannot_restart(self):
        env=self.environment();await env.start(False)
        with self.assertRaises(BindingError):await env.start(False)
        with self.assertRaises(BindingError):
            duplicate=self.environment()
            await duplicate.start(False)

    async def test_stale_nonce_and_live_lease_denied(self):
        env=self.environment();await env.start(False)
        with self.assertRaises(BindingError):await self.gate(nonce='other-nonce')
        with self.assertRaises(BindingError):await self.gate(attempt='other-attempt')
        with self.assertRaises(BindingError):await self.gate(lease=False)

    async def test_moving_runtime_image_is_denied(self):
        env=self.environment();await env.start(False)
        self.fake.runtime=BASE
        with self.assertRaises(BindingError):await self.gate()

    async def test_foreign_container_labels_are_denied(self):
        env=self.environment();await env.start(False)
        self.fake.container_override={'Config':{'Labels':{'com.docker.compose.project':'foreign'}}}
        with self.assertRaises(BindingError):await self.gate()
        with self.assertRaises(BindingError):await env.stop(False)

    async def test_foreign_mount_is_denied(self):
        env=self.environment();await env.start(False)
        self.fake.container_override={'Mounts':[{'Type':'bind','Source':'/tmp'}]}
        with self.assertRaises(BindingError):await self.gate()

    async def test_daemon_switch_denies_paid_and_cleanup(self):
        env=self.environment();await env.start(False);self.fake.daemon='another-daemon'
        with self.assertRaises(BindingError):await self.gate()
        with self.assertRaises(BindingError):await env.stop(False)

    async def test_builder_policy_drift_denies_paid_gate(self):
        env=self.environment();await env.start(False)
        self.fake.builder_output=BUILDER_OUTPUT.replace('docker-container','docker')
        with self.assertRaises(BindingError):await self.gate()

    def test_builder_activity_time_is_not_identity(self):
        self.assertEqual(builder_inspect_hash(BUILDER_OUTPUT),builder_inspect_hash(BUILDER_OUTPUT.replace('yesterday','today')))

    async def test_context_switch_denied(self):
        self.fake.context='foreign-context'
        with self.assertRaises(BindingError):await self.environment().start(False)

    async def test_platform_mismatch_denied(self):
        self.fake.image_override={'Architecture':'arm64','Variant':'v8'}
        with self.assertRaises(BindingError):await self.environment().start(False)

    async def test_missing_ambiguous_digest_or_config_id_confusion_denied(self):
        for refs in ([],[DIGEST,DIGEST],[BASE],['org/task:tag'],None):
            with self.subTest(refs=refs):
                self.fake.image_override={'RepoDigests':refs}
                with self.assertRaises(BindingError):await Inspector(self.b,self.fake).image(DIGEST,base=True)

    async def test_environment_selector_collision_denied(self):
        with self.assertRaises(BindingError):self.environment(persistent_env={'PREBUILT_IMAGE_NAME':'foreign'})
        os.environ['COMPOSE_FILE']='foreign.yaml'
        with self.assertRaises(BindingError):self.environment()

    async def test_unknown_image_override_kwarg_denied(self):
        with self.assertRaises(BindingError):self.environment(docker_image='foreign')

    async def test_implicit_dotenv_denied(self):
        (self.task/'environment/.env').write_text('COMPOSE_BAKE=true\n')
        with self.assertRaises(BindingError):self.environment()

    async def test_prior_installer_context_denied(self):
        (self.trial/'agent-build-context').mkdir()
        with self.assertRaises(BindingError):self.environment()

    async def test_custom_compose_denied(self):
        (self.task/'environment/docker-compose.yaml').write_text('services: {}')
        with self.assertRaises(BindingError):self.environment()

    async def test_world_readable_binding_denied(self):
        os.chmod(self.binding_path,0o644)
        with self.assertRaises(BindingError):Binding.load(self.binding_path)

    async def test_proof_persistence_failure_denies_start(self):
        env=self.environment()
        original=atomic_private_json
        def fail_proof(path,value,**kwargs):
            if path == Path(self.b.proof_path):raise OSError('injected fsync failure')
            return original(path,value,**kwargs)
        with patch('dradar.v2.image_binding.pier_adapter.atomic_private_json',side_effect=fail_proof):
            with self.assertRaises(OSError):await env.start(False)
        self.assertEqual(read_private_json(Path(self.b.state_path))['phase'],'attempt_quarantined')
        with self.assertRaises((BindingError,FileNotFoundError)):await self.gate()

    async def test_missing_or_incomplete_proof_denied(self):
        env=self.environment();await env.start(False)
        p=read_private_json(Path(self.b.proof_path));p['phase']='start_submitted_unknown'
        atomic_private_json(Path(self.b.proof_path),p)
        with self.assertRaises(BindingError):await self.gate()

    async def test_image_removal_flag_denied(self):
        env=self.environment()
        for flag in ('--rmi','--rmi=all'):
            with self.assertRaises(BindingError):await env._run_docker_compose_command(['down',flag,'all'])

    def test_source_fence_actual_pinned_source_and_mutation(self):
        verify_pinned_pier()
        fixture=self.root/'fake-source';fixture.mkdir()
        for name,digest in PINNED_HASHES.items():
            # Missing files and mutation must fail; no version-only bypass.
            p=fixture/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('changed')
        with self.assertRaises(BindingError):verify_pinned_pier(fixture)

    def test_private_overlay_only_changes_one_parsed_field(self):
        before=(self.task/'task.toml').read_bytes()
        (self.task/'task.toml').write_text('[environment]\ndocker_image = "org/task:mutable" # preserve other fields\ncpus = 2\n[metadata]\nname = "task"\n')
        source=(self.task/'task.toml').read_bytes()
        dest=self.root/'selected-task';result=pin_private_task(self.task,dest,DIGEST)
        self.assertEqual((self.task/'task.toml').read_bytes(),source)
        self.assertNotEqual(result['effective_task_hash'],result['selected_task_hash'])
        self.assertEqual(result['original_image_ref'],'org/task:mutable')
        self.assertEqual((dest/'environment/Dockerfile').read_bytes(),(self.task/'environment/Dockerfile').read_bytes())

    def test_baseline_removed_prebuilt_is_not_reinserted(self):
        (self.task/'task.toml').write_text('[environment]\ncpus = 2\n')
        with self.assertRaises(BindingError):pin_private_task(self.task,self.root/'selected-task',DIGEST)
        self.assertFalse((self.root/'selected-task').exists())

    def test_tree_symlink_denied(self):
        (self.task/'escape').symlink_to('/tmp')
        with self.assertRaises(BindingError):tree_hash(self.task)

    def test_cli_flags_and_none_historical_path(self):
        self.assertEqual(binding_cli_args(None),[])
        self.assertEqual(binding_cli_args(self.binding_path),['--environment-import-path','dradar_v2_image_binding.pier_adapter:BoundPrebuiltDockerEnvironment','--ek','binding_path='+str(self.binding_path),'--no-delete'])

    def test_platform_alias_is_explicit(self):
        self.assertEqual(normalize_platform('linux/arm64/v8'),'linux/arm64')
        with self.assertRaises(BindingError):normalize_platform('arm64')


if __name__ == '__main__': unittest.main()
