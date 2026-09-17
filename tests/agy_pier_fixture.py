"""Credential-free provider substitute; production run/supervisor are unchanged."""
from pathlib import PurePosixPath
from pier.models.agent.install import AgentInstallSpec, InstallStep
from dradar.pier_antigravity import Antigravity


class FixtureAGY(Antigravity):
    _REMOTE_CLI = PurePosixPath('/tmp/fixture-agy')

    def network_allowlist(self):
        from pier.models.agent.network import NetworkAllowlist
        return NetworkAllowlist(domains=[])

    def install_spec(self):
        return AgentInstallSpec(agent_name='agy-fixture', version='1', steps=[InstallStep(user='root', run='true')],
                                verification_command='true', cache_key='agy-fixture-1')

    async def setup(self, environment):
        await environment.exec(command='mkdir -p /tmp/dradar-antigravity-user/.gemini/antigravity-cli /logs/agent; touch /tmp/dradar-antigravity-user/.gemini/antigravity-cli/settings.json')
        source = '''#!/usr/bin/python3
import os,sys,time
from pathlib import Path
if 'models' in sys.argv:
    print('gemini-3.7-flash-low')
    raise SystemExit(0)
mode = sys.argv[sys.argv.index('--print')+1]
Path('/logs/agent/model-calls').write_text('one')
if mode != 'empty':
    Path('/app/file').write_text('changed\\n')
    Path('/app/new').write_bytes(b'\\x00binary')
if mode in ('cancel', 'repeat'):
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            while True:
                Path('/app/file').write_text('changed\\n')
                time.sleep(.01)
        os._exit(0)
    Path('/logs/agent/ready').touch()
    while True: time.sleep(.1)
if mode == 'crash':
    import signal
    os.kill(os.getppid(), signal.SIGKILL)
    time.sleep(1)
if mode == 'filter':
    import subprocess
    Path('/app/.gitattributes').write_text('* filter=evil\\n')
    subprocess.run(['git','config','filter.evil.clean','touch /logs/agent/evil; cat'],check=True)
if mode == 'failure': Path('/app/.git/index.lock').touch()
raise SystemExit(7 if mode == 'nonzero' else 0)
'''
        import shlex
        await environment.exec(command='printf %s ' + shlex.quote(source) + ' > /tmp/fixture-agy; chmod 755 /tmp/fixture-agy')
