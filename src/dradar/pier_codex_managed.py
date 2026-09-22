"""Explicit Codex app-server consumer for dedicated host-managed credentials.

The ordinary Codex adapter remains separate. This adapter never uploads a
native auth store and records native acceptance separately from request use.
"""
from __future__ import annotations
import asyncio
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import time
import uuid

from pier.agents.installed.codex import Codex
from pier.agents.installed.base import with_prompt_template
from pier.models.trial.paths import EnvironmentPaths

try:
    from _dradar_worker_events import emit_worker_registered, verify_task_baseline
    from _dradar_artifact_boundary import private_post_run
except ModuleNotFoundError:
    from dradar.worker_events import emit_worker_registered, verify_task_baseline
    from dradar.artifact_boundary import private_post_run


def validate_codex_version_output(output, expected_version="0.154.0"):
    """Pier may merge stderr before or after stdout; require one exact version."""
    lines = (output or '').strip().splitlines()
    if expected_version not in ('0.154.0', '0.155.1'):
        raise RuntimeError('managed container version unsupported')
    version = 'codex-cli ' + expected_version
    home = r'/tmp/dradar-managed-[a-f0-9]{32}/codex-home'
    warning = (r'WARNING: proceeding, even though we could not create PATH aliases: '
               r'(?:CODEX_HOME points to "' + home + r'", but that path does not exist'
               r'|Refusing to create helper binaries under temporary dir "/tmp" '
               r'\(codex_home: AbsolutePathBuf\("' + home + r'"\)\))')
    if lines.count(version) != 1 or any(line != version and re.fullmatch(warning, line) is None for line in lines):
        raise RuntimeError('managed container version mismatch')


class CodexManaged(Codex):
    def __init__(self, *args, managed_config_file: str, managed_bridge_file: str,
                 managed_package: str = 'dradar', **kwargs):
        if managed_package != 'dradar' and not re.fullmatch(r'_dradar_managed_auth_[a-f0-9]{12}', managed_package):
            raise ValueError('managed package invalid')
        self._observation_sink = None
        self._observed_states = set()
        self._managed_package = managed_package
        self._managed_config_file = Path(managed_config_file)
        self._managed_bridge_file = Path(managed_bridge_file)
        super().__init__(*args, **kwargs)
        if any(key in self._extra_env for key in ('OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_AUTH_JSON_PATH', 'CODEX_FORCE_AUTH_JSON')):
            raise ValueError('managed consumer rejects alternate credential environment')
        if self._version not in ('0.154.0', '0.155.1'):
            raise ValueError('managed consumer runtime unsupported')
        model = (self.model_name or '').split('/')[-1]
        if model in ('gpt-6-sol', 'gpt-6-luna') and self._version != '0.155.1':
            raise ValueError('GPT-6 managed consumer requires Codex 0.155.1')
        if model not in ('gpt-6-sol', 'gpt-6-luna') and self._version != '0.154.0':
            raise ValueError('legacy managed consumer requires Codex 0.154.0')

    def _module(self, name):
        return importlib.import_module(self._managed_package + '.' + name)

    def _session(self):
        read = self._module('credential_files').read_private_credential
        config = json.loads(read(self._managed_config_file))
        if set(config) != {'schema', 'store_root', 'authority_path', 'executable'} or config['schema'] != 'dradar.managed_selection.v1':
            raise ValueError('managed selection invalid')
        paths = [Path(config[key]) for key in ('store_root', 'authority_path', 'executable')]
        if not all(path.is_absolute() for path in paths):
            raise ValueError('managed paths must be absolute')
        store = self._module('auth_managed').ManagedAuthStore(paths[0])
        authority = self._module('auth_authority').select_authority('codex', [paths[1]], local_key=store._key())
        return store.session(authority, paths[2])

    async def _checked(self, environment, command, **kwargs):
        result = await self.exec_as_agent(environment, command=command, **kwargs)
        if result.return_code != 0:
            raise RuntimeError('managed container operation failed')
        return result

    async def _publish(self, environment, root, name, value, *, replace=False):
        destination = root + '/' + ('.publish-' + uuid.uuid4().hex if replace else name)
        with tempfile.TemporaryDirectory(prefix='dradar-managed-') as raw:
            source = Path(raw).resolve() / 'payload'
            source.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
            source.chmod(0o600)
            await self._module('pier_credential_delivery').inject_private_files(self, environment, [(source, destination)])
        if replace:
            await self._checked(environment, 'mv -f -- ' + shlex.quote(destination) + ' ' + shlex.quote(root + '/' + name), timeout_sec=5)

    async def _metadata(self, environment, root, name):
        # Only fixed metadata files are read. Never download the control tree.
        if name not in ('status.json', 'refresh-request.json'):
            raise ValueError('metadata name invalid')
        result = await self.exec_as_agent(environment, command='if [ ! -L ' + shlex.quote(root+'/'+name) + ' ] && [ -f ' + shlex.quote(root+'/'+name) + ' ]; then head -c 4096 ' + shlex.quote(root+'/'+name) + '; fi', timeout_sec=5)
        if result.return_code != 0 or not (result.stdout or "").strip():
            return None
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise RuntimeError('managed metadata invalid') from None
        return value

    def _retain_status(self, value):
        if not value:
            return
        generation = value.get('generation')
        if (value.get('schema') != 'dradar.managed_consumer.v1'
                or value.get('state') not in ('ready', 'running', 'completed', 'failed')
                or (generation is not None and not re.fullmatch(r'[a-f0-9]{32}', generation))
                or value.get('native_acceptance') not in ('confirmed', 'unknown')
                or value.get('request_used') != 'unknown'):
            raise RuntimeError('managed status invalid')
        sink=self._observation_sink
        if sink is not None:
            generation=value.get('generation')
            if generation and value.get('native_acceptance')=='confirmed' and ('adoption',generation) not in self._observed_states:
                self._observed_states.add(('adoption',generation))
                sink.emit('adoption','confirmed',generation=generation)
                sink.emit('request','unknown',generation=generation)
            lifecycle=value.get('lifecycle',[])
            if isinstance(lifecycle,list) and len(lifecycle)<=2:
                for item in lifecycle:
                    if not isinstance(item,dict) or set(item)!={'state','at','generation','native_closed'}:continue
                    if not isinstance(item['at'],str) or not isinstance(item['state'],str) or type(item['native_closed']) is not bool:continue
                    if not isinstance(item['generation'],str) or not re.fullmatch(r'[a-f0-9]{32}',item['generation']):continue
                    identity=(item['state'],item['at'],item['generation'],item['native_closed'])
                    if identity in self._observed_states:continue
                    if item['state']=='running' or item['native_closed'] is True:
                        self._observed_states.add(identity)
                        sink.emit('execution','confirmed',generation=item['generation'],observed_at=item['at'],auth_action='end' if item['native_closed'] is True else 'start')
        destination = os.environ.get('DRADAR_MANAGED_STATUS_FILE')
        if destination:
            # Retain only finite control metadata, never native response bodies.
            safe = {key: value[key] for key in ('schema', 'state', 'generation', 'native_acceptance', 'request_used')}
            try:
                self._module('credential_files').atomic_private_credential(Path(destination), json.dumps(safe).encode())
            except (OSError, ValueError):
                pass  # Optional observation cannot terminate provider work.

    async def _generation(self, environment, root, session, material):
        # Recheck custody and exact generation before exposing even an AT.
        session.check_session_contract()
        raw = session.authority.read()
        current = self._module('auth_access').project_access('codex', raw, local_key=session.local_key)
        if current != material or not current.usable():
            raise RuntimeError('managed authority changed')
        account_id = json.loads(raw)['tokens']['account_id']
        await self._publish(environment, root, 'at-' + material.revision + '.json', {
            'generation': material.revision, 'access_token': material.token,
            'account_id': account_id, 'expires_at': material.expires_at})
        await self._publish(environment, root, 'current.json', {'generation': material.revision}, replace=True)
        if self._observation_sink is not None:self._observation_sink.emit("delivery","confirmed",generation=material.revision)

    @with_prompt_template
    async def run(self, instruction, environment, context):
        if not self.model_name:
            raise ValueError('model required')
        await verify_task_baseline(environment)
        session = self._session()
        observation_path=os.environ.get('DRADAR_MANAGED_EVENT_FILE')
        cohort=os.environ.get('DRADAR_MANAGED_COHORT_ID','')
        if observation_path and re.fullmatch(r'[a-f0-9]{32}',cohort):
            self._observation_sink=self._module('auth_observation').ObservationSink(observation_path,session,cohort)
            session.observe=self._observation_sink.session_observation
            session.observe_v2=True
            self._observation_sink.emit('selection','confirmed')
        root = '/tmp/dradar-managed-' + uuid.uuid4().hex
        env = self.build_process_env({'CODEX_HOME': root + '/codex-home'})
        for key in ('OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_AUTH_JSON_PATH', 'CODEX_FORCE_AUTH_JSON'):
            env.pop(key, None)
        worker = None
        renewal = None
        rejected_once = False
        try:
            await self._publish(environment, root, 'bridge.cjs', self._managed_bridge_file.read_bytes())
            await self._publish(environment, root, 'codex-home/config.toml', (self._config_toml or '').encode())
            result = await self._checked(environment, 'if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; codex --version', env=env, timeout_sec=10)
            validate_codex_version_output(result.stdout, self._version)
            material = await asyncio.to_thread(session.prepare)
            await self._publish(environment, root, 'request.json', {'schema': 'dradar.managed_run.v1',
                'instruction': instruction, 'model': self._command_model_name or self.model_name.split('/')[-1],
                'effort': self._resolved_flags.get('reasoning_effort') or 'high',
                'summary': self._resolved_flags.get('reasoning_summary') or 'auto'})
            await self._generation(environment, root, session, material)
            await self._publish(environment, root, 'heartbeat.json', {'sequence': uuid.uuid4().hex}, replace=True)
            setup = '\n'.join(filter(None, [self._build_register_skills_command(), self._build_register_mcp_servers_command()]))
            if setup:
                await self._checked(environment, setup, env=env)
            worker = asyncio.create_task(self.exec_as_agent(environment,
                command='if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; node ' + shlex.quote(root+'/bridge.cjs') + ' ' + shlex.quote(root+'/request.json'), env=env))
            registered = False
            permitted = False
            deadline = time.monotonic() + 60
            while not worker.done():
                await self._publish(environment, root, 'heartbeat.json', {'sequence': uuid.uuid4().hex}, replace=True)
                state = await self._metadata(environment, root, 'status.json')
                self._retain_status(state)
                if state and state.get('state') == 'ready' and not registered:
                    emit_worker_registered(runtime='pier', context='agent', profile='codex_managed_at')
                    registered = True
                if registered and not permitted:
                    permit = os.environ.get('DRADAR_MANAGED_START_PERMIT')
                    if not permit:
                        raise RuntimeError('managed host start permit missing')
                    if Path(permit).is_file():
                        read = self._module('credential_files').read_private_credential
                        permission = json.loads(read(Path(permit)))
                        if permission != {'schema': 'dradar.managed_start.v1'}:
                            raise RuntimeError('managed host start permit invalid')
                        await self._publish(environment, root, 'start.json', {'schema': permission['schema'], 'generation': material.revision})
                        permitted = True
                if not permitted and time.monotonic() > deadline:
                    raise RuntimeError('managed start timeout')
                refresh = await self._metadata(environment, root, 'refresh-request.json')
                if renewal is not None and renewal.done():
                    renewed = await renewal
                    renewal = None
                    if renewed.revision != material.revision:
                        await self._generation(environment, root, session, renewed)
                        material = renewed
                if renewal is None and refresh and refresh.get('generation') == material.revision:
                    if rejected_once:
                        raise RuntimeError('managed repeated provider rejection')
                    rejected_once = True
                    renewal = asyncio.create_task(asyncio.to_thread(session.prepare, rejected_revision=material.revision))
                if renewal is None and not material.usable(margin=60):
                    renewal = asyncio.create_task(asyncio.to_thread(session.prepare))
                await asyncio.sleep(.5)
            result = await worker
            self._retain_status(await self._metadata(environment, root, 'status.json'))
            if result.return_code != 0:
                raise RuntimeError('managed runtime failed')
        finally:
            had_failure = sys.exc_info()[0] is not None
            # Do not abandon a native refresh thread mid-rotation. The RPC and
            # gate have their own bounds, and preserve uncertain candidates.
            if renewal is not None:
                try:
                    await asyncio.shield(renewal)
                except Exception:
                    pass
            if worker is not None and not worker.done():
                try:
                    await self._publish(environment, root, 'stop.json', {})
                    await asyncio.wait_for(asyncio.shield(worker), 8)
                except Exception:
                    # The bridge's independent heartbeat watchdog also stops it.
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
                try:
                    self._retain_status(await self._metadata(environment, root, 'status.json'))
                except Exception:
                    pass
            try:
                logs = EnvironmentPaths.agent_dir.as_posix()
                await self._checked(environment, 'mkdir -p ' + shlex.quote(logs) + ' && if [ -d "$CODEX_HOME/sessions" ]; then cp -R "$CODEX_HOME/sessions" ' + shlex.quote(logs+'/sessions') + '; fi', env=env, timeout_sec=10)
            finally:
                try:
                    await self._checked(environment, 'rm -rf -- ' + shlex.quote(root), env=env, timeout_sec=10)
                except Exception:
                    if not had_failure:
                        raise

                finally:
                    if self._observation_sink is not None:self._observation_sink.coverage()

    @private_post_run
    def populate_context_post_run(self, context):
        return super().populate_context_post_run(context)
