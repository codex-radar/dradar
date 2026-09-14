from types import SimpleNamespace
import pytest
from dradar.managed_auth_selection import trial_platform_ready

@pytest.mark.parametrize('system,machine',[('Windows','AMD64'),('Linux','aarch64'),('Darwin','x86_64')])
def test_unsupported_host_never_launches_docker(monkeypatch,system,machine):
 import platform,subprocess
 monkeypatch.setattr(platform,'system',lambda:system);monkeypatch.setattr(platform,'machine',lambda:machine)
 monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('must reject before process'))
 assert not trial_platform_ready({})

@pytest.mark.parametrize('endpoint,info,expected',[('ssh://remote','linux/aarch64',False),('tcp://127.0.0.1:2375','linux/aarch64',False),('unix:///fixture.sock','linux/x86_64',False),('unix:///fixture.sock','linux/aarch64',True)])
def test_locality_and_daemon_architecture(monkeypatch,endpoint,info,expected):
 import platform,subprocess,shutil
 monkeypatch.setattr(platform,'system',lambda:'Darwin');monkeypatch.setattr(platform,'machine',lambda:'arm64');monkeypatch.setattr(shutil,'which',lambda *a,**k:'/fake/docker')
 monkeypatch.setattr(subprocess,'run',lambda args,**k:SimpleNamespace(returncode=0,stdout=endpoint if 'context' in args else info))
 assert trial_platform_ready({}) is expected
