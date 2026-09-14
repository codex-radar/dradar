import os
import pytest
from dradar.auth_authority import select_authority, AuthorityUnavailable

KEY = b'local-test-key-not-a-credential!!' * 2

def private(path, data=b'fake-login'):
    path.write_bytes(data); path.chmod(0o600); return path

def select(paths, explicit=None):
    return select_authority('codex', paths, local_key=KEY, explicit=explicit)

def test_no_login_and_ambiguous_are_distinct(tmp_path):
    a, b = tmp_path/'a', tmp_path/'b'
    with pytest.raises(AuthorityUnavailable, match='login_required'): select([a,b])
    private(a); private(b)
    with pytest.raises(AuthorityUnavailable, match='ambiguous'): select([a,b])
    assert select([a,b], a).path == a

def test_explicit_invalid_never_falls_back(tmp_path):
    good = private(tmp_path/'good')
    with pytest.raises(AuthorityUnavailable, match='unavailable'):
        select([good], tmp_path/'missing')

def test_store_identity_survives_rotation_not_shared_email(tmp_path):
    a, b = private(tmp_path/'a', b'same fake email'), private(tmp_path/'b', b'same fake email')
    old = select([a]); other = select([b])
    replacement = private(tmp_path/'replacement', b'rotated fake state')
    os.replace(replacement, a)
    new = select([a])
    assert old.store_id == new.store_id != other.store_id
    assert str(a) not in repr(old) and old.store_id not in repr(old)

@pytest.mark.skipif(os.name == 'nt', reason='Windows symlink privilege requires native QA')
def test_symlink_and_hardlink_rejected(tmp_path):
    a = private(tmp_path/'a'); link = tmp_path/'link'; link.symlink_to(a)
    with pytest.raises(AuthorityUnavailable): select([link])
    hard = tmp_path/'hard'; os.link(a, hard)
    with pytest.raises(AuthorityUnavailable): select([hard])

def test_secret_never_part_of_error_or_diagnostics(tmp_path):
    a = private(tmp_path/'secret-name', b'SECRET')
    result = select([a]); assert 'SECRET' not in repr(result)
    a.unlink()
    with pytest.raises(AuthorityUnavailable) as e: result.read()
    assert 'secret-name' not in str(e.value)


def test_noncanonical_alias_cannot_create_a_second_store_lock(tmp_path):
    path=private(tmp_path/'auth.json');(tmp_path/'sub').mkdir()
    with pytest.raises(AuthorityUnavailable):select([tmp_path/'sub'/'..'/'auth.json'])
    assert select([path]).path==path


def test_atomic_replacement_read_race_revalidates_same_source_once(tmp_path,monkeypatch):
    from dradar import auth_authority as module
    path=private(tmp_path/'auth.json');calls=[]
    actual=module.read_private_credential
    def read(candidate):
        calls.append(candidate)
        if len(calls)==1:raise ValueError('credential source changed while opening')
        return actual(candidate)
    monkeypatch.setattr(module,'read_private_credential',read)
    assert select([path]).path==path
    assert calls==[path,path]
