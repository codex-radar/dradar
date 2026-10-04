import hashlib
from pathlib import Path
import pytest
from dradar.v2.skill_install import packaged_skill,install_skill
from dradar.v2 import commands

def test_install_checks_independent_hash_and_preserves_previous_skill(tmp_path):
    target=tmp_path/'dradar-v2';target.mkdir()
    (target/'SKILL.md').write_text('original skill')
    with pytest.raises(ValueError):install_skill(target,'0'*64)
    assert (target/'SKILL.md').read_text()=='original skill'
    result=install_skill(target,hashlib.sha256(packaged_skill()).hexdigest())
    assert Path(result['backup']).read_text()=='original skill'
    assert (target/'SKILL.md').read_bytes()==packaged_skill()
    assert install_skill(target,result['sha256'])['status']=='already_installed'

def test_install_rejects_symlink_target(tmp_path):
    target=tmp_path/'real';target.mkdir()
    link=tmp_path/'dradar-v2';link.symlink_to(target,target_is_directory=True)
    with pytest.raises(ValueError):install_skill(link,hashlib.sha256(packaged_skill()).hexdigest())
    assert not (target/'SKILL.md').exists()

def test_install_command_does_not_need_login_or_claim(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(commands,'runtime_config',lambda:pytest.fail('skill installation must not read credentials'))
    rc=commands.main(['install-skill','--destination',str(tmp_path/'dradar-v2'),'--expected-sha256',hashlib.sha256(packaged_skill()).hexdigest()])
    assert rc==0 and 'installed' in capsys.readouterr().out
