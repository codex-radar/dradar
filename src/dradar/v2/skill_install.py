"""Checksum-pinned install of the packaged candidate skill, with reversible backup."""
from importlib.resources import files
import hashlib
import os
from pathlib import Path
import re
import tempfile
import uuid

def packaged_skill() -> bytes:
    return files('dradar.v2').joinpath('skill/SKILL.md').read_bytes()

def install_skill(destination: Path, expected_sha256: str) -> dict:
    data=packaged_skill()
    digest=hashlib.sha256(data).hexdigest()
    if not isinstance(expected_sha256,str) or not re.fullmatch('[a-f0-9]{64}',expected_sha256) or digest!=expected_sha256:
        raise ValueError('技能哈希与可信交付清单不匹配，未安装')
    destination=Path(destination).absolute()
    if destination.name!='dradar-v2' or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('独立dradar-v2真实技能目录 required')
    destination.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=destination/'SKILL.md'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('技能文件边界无效，未更新')
    backup=None
    if path.exists():
        old=path.read_bytes()
        if old==data: return {'status':'already_installed','path':str(path),'sha256':digest,'candidate':True}
        backup=destination/('SKILL.md.backup-'+uuid.uuid4().hex)
        fd=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(old);stream.flush();os.fsync(stream.fileno())
    fd,temp=tempfile.mkstemp(prefix='.skill-',dir=destination)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        if os.name!='nt':
            directory=os.open(destination,os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return {'status':'installed','path':str(path),'backup':str(backup) if backup else None,'sha256':digest,'candidate':True}
