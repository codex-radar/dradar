"""Allocate image proof using Pier's real session/trial, without global patches."""
from pathlib import Path
import re
from .core import Binding, atomic_private_json, read_private_json, require
from .pier_adapter import BoundPrebuiltDockerEnvironment

class AllocatedImageEnvironment(BoundPrebuiltDockerEnvironment):
    def __init__(self, *, descriptor_path: str, **kwargs):
        descriptor = Path(descriptor_path)
        record = read_private_json(descriptor)
        require(record.get('schema') == 'dradar.v2.image-descriptor.v1', 'invalid parent descriptor')
        identity = record['binding']
        job = Path(identity['job_root'])
        trial = kwargs['trial_paths'].trial_dir.resolve()
        session = kwargs['session_id']
        require(trial.parent == job and trial.name == session, 'foreign Pier trial allocation')
        prefix = Path(identity['selected_task_root']).name[:32].rstrip('_-')
        require(re.fullmatch(re.escape(prefix) + r'__[A-Za-z0-9]{7}', session) is not None, 'unexpected Pier session')
        require(Path(kwargs['environment_dir']).resolve() == Path(identity['environment_dir']), 'foreign environment allocation')
        binding = Binding(**{**identity, 'session_id':session,
                            'project': re.sub(r'[^a-z0-9_-]', '-', session.lower()),
                            'trial_dir':str(trial)})
        binding.validate()
        path = descriptor.parent / 'allocated.json'
        atomic_private_json(path, binding.identity(), exclusive=True)
        super().__init__(binding_path=str(path), **kwargs)
