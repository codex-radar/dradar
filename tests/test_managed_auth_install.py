import hashlib
import io
import pytest
from test_auth_managed import runtime
from dradar import managed_auth_install as installer
from dradar.auth_managed import ManagedAuthStore
from dradar.auth_refresh import RefreshUnavailable


def test_corrupt_download_never_provisions_or_selects(tmp_path,monkeypatch):
    monkeypatch.setattr(installer.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(installer.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(installer.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'not-official'))
    store=ManagedAuthStore(tmp_path/'store')
    with pytest.raises(RefreshUnavailable,match='download_invalid'):installer.acquire(store)
    assert not (store.root/'authorities').exists()
    assert not list(store.root.glob('runtime-*'))


def test_existing_verified_private_runtime_does_not_download(tmp_path,monkeypatch,runtime):
    from dradar import auth_managed
    digest=hashlib.sha256(runtime.read_bytes()).hexdigest()
    monkeypatch.setattr(installer.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(installer.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(auth_managed,'_CODEX_PINS',{('Darwin','arm64'):digest})
    store=ManagedAuthStore(tmp_path/'store');private=store._runtime(runtime)
    monkeypatch.setattr(installer,'DIGEST',hashlib.sha256(runtime.read_bytes()).hexdigest())
    monkeypatch.setattr(installer.urllib.request,'urlopen',lambda *a,**k:pytest.fail('download'))
    assert installer.acquire(store)==private


def test_unsupported_platform_fails_before_network(tmp_path,monkeypatch):
    monkeypatch.setattr(installer.platform,'system',lambda:'Windows')
    monkeypatch.setattr(installer.urllib.request,'urlopen',lambda *a,**k:pytest.fail('network'))
    with pytest.raises(RefreshUnavailable,match='runtime_unverified'):
        installer.acquire(ManagedAuthStore(tmp_path/'store'))
