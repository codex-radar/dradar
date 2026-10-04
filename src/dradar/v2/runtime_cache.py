"""Parent-owned public base lease and proof gate; task layers remain private.

Only explicitly approved public references participate. No automatic GC, tag
fallback, anonymous-volume reuse, or retry of an unresolved backend attempt.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
import json
import os
import shutil
import tomllib
import uuid
from .docker_cache import DockerCache
from .image_binding.core import (Binding, BindingError, atomic_private_json,
    builder_inspect_hash, read_private_json, normalize_platform, require,
    verify_paid_image_proof)
from .image_binding.preparation import pin_private_task

IMPORT = 'dradar_v2_image_binding.pier_cache:AllocatedImageEnvironment'

class PublicImagePreparation:
    def __init__(self, journal, *, platform: str, public_references, cache_root: Path, cache_factory=DockerCache):
        normalize_platform(platform)
        if not public_references or any(not isinstance(r,str) or not r or any(c.isspace() for c in r) for r in public_references):
            raise ValueError('explicit approved public image references required')
        self.journal, self.platform = journal, platform
        self.approved = frozenset(public_references)
        self.cache_root, self.cache_factory = Path(cache_root), cache_factory
        self.cache = None
        self.descriptor = None
        self.attempt = None
        self._declined = False

    def prepare(self, *, assignment, tasks_root, work_dir, job_root, builder_lease):
        task = Path(tasks_root).resolve() / assignment['task_id']
        config = tomllib.loads((task/'task.toml').read_text())
        ref = config.get('environment', {}).get('docker_image')
        # A baseline-origin overlay removal is authoritative. Do not resurrect it.
        if ref is None:
            self._declined = True
            return Path(tasks_root), []
        require(ref in self.approved, 'effective base lacks explicit public approval')
        require(builder_lease.reusable and builder_lease.isolated and builder_lease.name, 'ready shared builder required')
        # Unknown prior attempt is never overwritten or automatically restarted.
        work = Path(work_dir).resolve()
        require(not (work/'public-image-bootstrap').exists(), 'prior image preparation is retained')
        require(not any(os.environ.get(k) for k in ('DOCKER_HOST','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')), 'Docker endpoint override unsupported')
        self.cache = self.cache_factory(self.cache_root)
        image = self.cache._image(ref)
        require(image is not None, 'approved base must already be present; prepare exact digest separately')
        digests = image.get('RepoDigests', [])
        if '@sha256:' in ref:
            require(digests.count(ref) == 1, 'base manifest provenance missing')
            pinned = ref
        else:
            # Local tag is metadata only. Require a unique association with its
            # repository; never pass that mutable tag to the builder or runtime.
            repository = ref.rsplit(':',1)[0] if ':' in ref.rsplit('/',1)[-1] else ref
            matches = [d for d in digests if d.split('@',1)[0] == repository]
            require(len(matches) == 1, 'ambiguous public repository manifest provenance')
            pinned = matches[0]
        self.attempt = assignment['_runner_session_id']
        require(self.journal.value('image_attempt:'+self.attempt) is None, 'prior base lease or preparation is retained')
        self.journal.bind('image_attempt:'+self.attempt, 'reserved')
        lease = self.cache.ensure_pinned_image(pinned, platform=self.platform, attempt=self.attempt)
        require(lease.artifact.object_id == image['Id'], 'base changed during immutable ensure')
        context = self.cache._json(['context','inspect',self.cache.context])
        endpoint = context[0]['Endpoints']['docker']
        require(not endpoint.get('SkipTLSVerify',False), 'TLS bypass unsupported')
        builder = self.cache._command(['buildx','inspect',builder_lease.name]).stdout
        # Parent and child use the same policy fingerprint; no builder bootstrap here.
        require([line.split(':',1)[1].strip() for line in builder.splitlines() if line.strip().startswith('Driver:')] == ['docker-container'], 'shared docker-container builder required')
        selected_root = work/'public-image-tasks'
        selected_root.mkdir(mode=0o700)
        selected = selected_root/assignment['task_id']
        hashes = pin_private_task(task, selected, pinned)
        job = Path(job_root).resolve()
        job.mkdir(mode=0o700,parents=True,exist_ok=True)
        records = job/'.binding'
        records.mkdir(mode=0o700)
        identity = dict(schema=1, attempt_id=self.attempt, nonce=uuid.uuid4().hex,
            original_package_hash=assignment['task_content_hash'],
            **{k:hashes[k] for k in ('effective_task_hash','selected_task_hash','original_image_ref','repository_digest')},
            platform=self.platform, base_image_id=lease.artifact.object_id,
            base_lease_token=lease.token, daemon_id=self.cache.daemon_id,
            context_name=self.cache.context, endpoint=endpoint['Host'],
            builder_name=builder_lease.name, builder_inspect_sha256=builder_inspect_hash(builder),
            selected_task_root=str(selected), environment_dir=str(selected/'environment'),
            job_root=str(job), state_path=str(records/'state.json'), proof_path=str(records/'proof.json'))
        self.descriptor = records/'descriptor.json'
        atomic_private_json(self.descriptor, {'schema':'dradar.v2.image-descriptor.v1','binding':identity},exclusive=True)
        self.journal.bind('image_binding:'+self.attempt,json.dumps({'descriptor':str(self.descriptor),'lease_token':lease.token,'repository_digest':pinned,'platform':self.platform,'base_image_id':lease.artifact.object_id,'daemon_id':self.cache.daemon_id},sort_keys=True))
        # Only the small adapter package goes into the actual child interpreter.
        # Credentials, journal, task output and writable user state are excluded.
        bootstrap = work/'public-image-bootstrap'
        bootstrap.mkdir(mode=0o700)
        package = bootstrap/'dradar_v2_image_binding'
        package.mkdir(mode=0o700)
        for source in (Path(__file__).parent/'image_binding').glob('*.py'):
            shutil.copyfile(source, package/source.name)
        shutil.copyfile(Path(__file__).parent/'pier_cache.py',package/'pier_cache.py')
        return selected_root, ['--environment-import-path',IMPORT,'--ek',f'descriptor_path={self.descriptor}','--no-delete']

    def child_environment(self, env):
        if self.descriptor is None:
            return
        bootstrap = self.descriptor.parents[3]/'public-image-bootstrap'
        env['PYTHONPATH'] = os.pathsep.join(filter(None,[str(bootstrap),env.get('PYTHONPATH')]))
        env['DOCKER_CONTEXT'] = self.cache.context
        env['DOCKER_DEFAULT_PLATFORM'] = self.platform

    def _binding(self):
        require(self.descriptor is not None,'image binding missing')
        b = Binding.load(self.descriptor.parent/'allocated.json')
        record = read_private_json(self.descriptor)['binding']
        require(all(b.identity()[k] == value for k,value in record.items()), 'child binding changed parent identity')
        return b

    def verify(self):
        if self._declined:
            return
        b = self._binding()
        async def live_lease(binding):
            return await asyncio.to_thread(self.cache.validate_image_lease,
                token=binding.base_lease_token, attempt=binding.attempt_id,
                repository_digest=binding.repository_digest, platform=binding.platform,
                object_id=binding.base_image_id, daemon_id=binding.daemon_id,
                context=binding.context_name, endpoint=binding.endpoint)
        # Parent verification uses a scoped inspection subprocess, not global env changes.
        from .image_binding.core import CommandResult
        async def command(argv):
            process = await asyncio.create_subprocess_exec(*argv,
                env={**os.environ,'DOCKER_CONTEXT':b.context_name,'BUILDX_BUILDER':b.builder_name,'DOCKER_DEFAULT_PLATFORM':b.platform},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output,_ = await asyncio.wait_for(process.communicate(),20)
            except BaseException:
                if process.returncode is None: process.kill()
                await process.communicate()
                raise
            require(len(output)<=1024*1024,'inspection output too large')
            return CommandResult(process.returncode,output.decode())
        return asyncio.run(verify_paid_image_proof(self.descriptor.parent/'allocated.json',
            expected_attempt_id=self.attempt,expected_nonce=b.nonce,lease_check=live_lease,command=command,environment={**os.environ,'DOCKER_CONTEXT':b.context_name,'BUILDX_BUILDER':b.builder_name,'DOCKER_DEFAULT_PLATFORM':b.platform}))

    def release_after_exit(self, *, confirmed_absent):
        if self._declined or self.descriptor is None:
            return False
        b = self._binding()
        state = read_private_json(Path(b.state_path))
        # Physical absence cannot prove an unresolved BuildKit solve completed.
        if not confirmed_absent or state.get('phase') != 'cleanup_submitted' or not (state.get('build_completed') is True or (state.get('build_completed') is False and state.get('generated_dockerfile_hash') is None and state.get('runtime_image_id') == b.base_image_id)):
            return False
        rows = self.cache._command(['container','ls','--all','--quiet','--no-trunc','--filter',f'label=com.docker.compose.project={b.project}']).stdout
        if rows.strip(): return False
        self.cache.release_attempt(self.attempt, confirm_inactive=lambda value:value==self.attempt and confirmed_absent)
        return True
