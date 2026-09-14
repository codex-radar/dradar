import multiprocessing as mp
import os
from pathlib import Path
import pytest
import os
pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX host-auth implementation; Windows admission is tested separately')

from dradar.auth_refresh import AccessState, HostRefreshGate, RefreshUnavailable

OLD = 'a' * 32
NEW = 'b' * 32
CHAIN = 'c' * 32


def test_reuse_and_renew_are_separate_from_running_tasks(tmp_path):
    gate = HostRefreshGate(tmp_path, CHAIN)
    result = gate.ensure(lambda: AccessState(OLD, True), lambda: pytest.fail('must not renew'))
    assert result.outcome == 'reused'
    state = AccessState(OLD, False)
    def renew():
        nonlocal state
        state = AccessState(NEW, True)
    assert gate.ensure(lambda: state, renew).outcome == 'refreshed'
    assert not (tmp_path / CHAIN / 'pending.json').exists()


@pytest.mark.parametrize('outcome', ['raise', 'unchanged', 'unusable'])
def test_ambiguous_renewal_blocks_next_refresh_without_retry(tmp_path, outcome):
    gate = HostRefreshGate(tmp_path, CHAIN)
    state = AccessState(OLD, False)
    def renew():
        nonlocal state
        if outcome == 'raise':
            state = AccessState(NEW, True)
            raise RuntimeError('SECRET raw vendor response')
        state = AccessState(OLD if outcome == 'unchanged' else NEW, outcome != 'unusable')
    with pytest.raises(RefreshUnavailable, match='^recovery_required$'):
        gate.ensure(lambda: state, renew)
    with pytest.raises(RefreshUnavailable, match='^recovery_required$'):
        gate.ensure(lambda: pytest.fail('must not even read'), lambda: pytest.fail('must not renew'))
    assert 'SECRET' not in (tmp_path / CHAIN / 'pending.json').read_text()


def _worker(root, start, results):
    path = Path(root) / 'fake-vendor-state'
    gate = HostRefreshGate(Path(root), CHAIN)
    start.wait(10)
    def read():
        return AccessState(NEW if path.exists() else OLD, path.exists())
    def renew():
        with path.open('x') as target:
            target.write('fake-ready')
    results.put(gate.ensure(read, renew).outcome)


def test_processes_coalesce_same_authority(tmp_path):
    ctx = mp.get_context('spawn')
    start, results = ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_worker, args=(str(tmp_path), start, results)) for _ in range(4)]
    for child in children: child.start()
    start.set()
    for child in children:
        child.join(15)
        assert child.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in children) == ['refreshed', 'reused', 'reused', 'reused']


def _crash(root):
    HostRefreshGate(Path(root), CHAIN).ensure(lambda: AccessState(OLD, False), lambda: os._exit(7))


def test_crash_releases_lock_but_preserves_intent(tmp_path):
    ctx = mp.get_context('spawn')
    child = ctx.Process(target=_crash, args=(str(tmp_path),))
    child.start(); child.join(10)
    assert child.exitcode == 7
    with pytest.raises(RefreshUnavailable, match='recovery_required'):
        HostRefreshGate(tmp_path, CHAIN).ensure(lambda: AccessState(NEW, True), lambda: pytest.fail('retry'))


def test_distinct_chain_not_blocked_by_pending_other_chain(tmp_path):
    with pytest.raises(RefreshUnavailable):
        HostRefreshGate(tmp_path, CHAIN).ensure(lambda: AccessState(OLD, False), lambda: None)
    assert HostRefreshGate(tmp_path, 'd' * 32).ensure(lambda: AccessState(NEW, True), lambda: None).outcome == 'reused'


def test_untrusted_source_error_is_not_exposed(tmp_path):
    def read(): raise ValueError('SECRET path and response')
    with pytest.raises(RefreshUnavailable, match='^source_unavailable$') as error:
        HostRefreshGate(tmp_path, CHAIN).ensure(read, lambda: None)
    assert error.value.__cause__ is None
    assert not (tmp_path / CHAIN / 'pending.json').exists()


def test_pending_is_durable_before_vendor_call(tmp_path):
    state = AccessState(OLD, False)
    def renew():
        nonlocal state
        pending = tmp_path / CHAIN / 'pending.json'
        assert pending.is_file()
        assert pending.stat().st_mode & 0o077 == 0
        state = AccessState(NEW, True)
    HostRefreshGate(tmp_path, CHAIN).ensure(lambda: state, renew)


def test_lock_timeout_does_not_start_renewal(tmp_path):
    from dradar.auth_refresh import _lock
    from dradar.credential_files import private_directory
    path = tmp_path / CHAIN
    private_directory(path)
    with _lock(path / 'gate.lock', 1):
        with pytest.raises(RefreshUnavailable, match='lock_timeout'):
            HostRefreshGate(tmp_path, CHAIN).ensure(lambda: pytest.fail('read'), lambda: pytest.fail('renew'), timeout=.01)
    assert not (path / 'pending.json').exists()


def test_directory_sync_failure_prevents_vendor_call(tmp_path, monkeypatch):
    from dradar import auth_refresh
    def fail(path): raise OSError('sync failure')
    monkeypatch.setattr(auth_refresh, '_sync_directory', fail)
    with pytest.raises(OSError):
        HostRefreshGate(tmp_path, CHAIN).ensure(lambda: AccessState(OLD, False), lambda: pytest.fail('renew'))
    assert (tmp_path / CHAIN / 'pending.json').exists()


def test_recovery_requires_changed_state_and_explicit_adapter_audit(tmp_path):
    gate=HostRefreshGate(tmp_path,CHAIN)
    with pytest.raises(RefreshUnavailable): gate.ensure(lambda: AccessState(OLD,False),lambda: None)
    for state, audit in [(AccessState(OLD,True),lambda _:True),(AccessState(NEW,True),lambda _:False)]:
        with pytest.raises(RefreshUnavailable): gate.recover(lambda:state,audit)
    assert gate.recover(lambda:AccessState(NEW,True),lambda _:True).outcome=='recovered'
    assert not (tmp_path/CHAIN/'pending.json').exists()


def test_recovery_audit_cannot_clear_if_generation_changes(tmp_path):
    gate=HostRefreshGate(tmp_path,CHAIN)
    with pytest.raises(RefreshUnavailable): gate.ensure(lambda: AccessState(OLD,False),lambda: None)
    state=AccessState(NEW,True)
    def audit(_):
        nonlocal state
        state=AccessState('d'*32,True)
        return True
    with pytest.raises(RefreshUnavailable): gate.recover(lambda:state,audit)
    assert (tmp_path/CHAIN/'pending.json').exists()
