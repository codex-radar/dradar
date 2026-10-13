"""Read-only installed-package evidence. No download, install or activation."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from .. import __version__
from .skill_install import packaged_skill


def identity():
    distribution = importlib.metadata.distribution('dradar')
    package = Path(distribution.locate_file('dradar')).resolve()
    actual = Path(__file__).resolve().parents[1]
    if distribution.version != __version__ or package != actual:
        raise ValueError('v2 requires the selected installed wheel interpreter; source/PYZ shadowing is blocked')
    if not any(e.name == 'dradar' and e.value == 'dradar.launcher:main' for e in distribution.entry_points):
        raise ValueError('installed console entry differs from reviewed package')
    import websockets
    if websockets.__version__ != '15.0.1':
        raise ValueError('install reviewed default dependencies in this interpreter: websockets==15.0.1 required')
    controller = package / 'v2/host_controller.py'
    return {'version': __version__, 'distribution_version': distribution.version,
            'python': sys.executable, 'prefix': sys.prefix,
            'package_root': str(package), 'entry': 'installed-wheel',
            'skill_sha256': hashlib.sha256(packaged_skill()).hexdigest(),
            'websockets_version': websockets.__version__,
            'controller_sha256': hashlib.sha256(controller.read_bytes()).hexdigest()}


def state_observation(root):
    """Journal hints cannot certify server leases or detached process exits."""
    root = Path(root).absolute()
    result = {'state_root': str(root), 'switch_authorized': False}
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        return {**result, 'status': 'blocked', 'reason': 'state_symlink'}
    path = root / 'journal.sqlite3'
    if not root.exists():
        return {**result, 'status': 'unused_state'}
    if path.is_symlink() or not path.is_file():
        return {**result, 'status': 'blocked', 'reason': 'journal_missing_or_unknown'}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as db:
            pending = db.execute('SELECT count(*) FROM requests WHERE response_json IS NULL').fetchone()[0]
            executions = db.execute('SELECT assignment_id,result_json FROM executions').fetchall()
            uploads = unknown = 0
            for aid, raw in executions:
                if raw is None:
                    unknown += 1
                    continue
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('unknown result evidence')
                if payload.get('exit_confirmed') is not True:
                    unknown += 1
                row = db.execute('SELECT response_json FROM requests WHERE operation=?', ('result:'+aid,)).fetchone()
                if row is None or row[0] is None:
                    uploads += 1
            return {**result, 'status': 'blocked' if pending or unknown or uploads else 'requires_exit_and_server_check',
                    'pending_requests': pending, 'unknown_executions': unknown, 'pending_uploads': uploads}
    except (sqlite3.Error, ValueError, TypeError):
        return {**result, 'status': 'blocked', 'reason': 'journal_unreadable_or_unknown'}


def self_check(expected_version, expected_skill_sha256, state_roots):
    current = identity()
    if current['version'] != expected_version or current['skill_sha256'] != expected_skill_sha256:
        raise ValueError('installed version/skill differs from the independently trusted fixed release')
    from .host_runtime import controller_command, controller_environment
    try:
        child = subprocess.run(controller_command('--self-check'), env=controller_environment(),
                               capture_output=True, text=True, timeout=15, check=True)
    except subprocess.SubprocessError as exc:
        raise ValueError('actual controller subprocess failed its bounded no-model check') from exc
    observed = json.loads(child.stdout)
    if observed != current:
        raise ValueError('actual controller subprocess package/interpreter differs from v2')
    return {'status': 'installed_package_verified', 'runtime': current, 'controller': observed,
            'states': [state_observation(p) for p in state_roots],
            'switch_authorized': False, 'network_requests': 0, 'model_started': False,
            'trust': 'expectations supplied by caller; self-report is not release authentication'}
