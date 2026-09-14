"""Explicit local selection for the managed Codex consumer.

Readiness never performs login, renewal, quota checks or model requests.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

from . import local_config
from .auth_managed import ManagedAuthStore
from .auth_authority import select_authority
from .auth_access import project_access
from .auth_refresh import RefreshUnavailable
from .credential_files import read_private_credential, atomic_private_credential, private_directory

PROFILE = 'codex-managed-at-v1'
CAPABILITY = 'codex-managed-at-appserver-0154-v1'


def selection_path(environ=None):
    env = os.environ if environ is None else environ
    explicit = env.get('DRADAR_CODEX_MANAGED_CONFIG')
    return Path(explicit) if explicit else local_config.HOME / 'managed-auth' / 'selection.json'


def selection_requested():
    path = selection_path()
    return bool(os.environ.get('DRADAR_CODEX_MANAGED_CONFIG') or path.exists() or path.is_symlink())


def load_selection(environ=None, *, allow_pending=False):
    path = selection_path(environ)
    if not path.exists() and not path.is_symlink():
        env = os.environ if environ is None else environ
        if env.get('DRADAR_CODEX_MANAGED_CONFIG'):
            raise RefreshUnavailable('managed_selection_missing')
        return None
    value = json.loads(read_private_credential(path))
    if (not isinstance(value, dict) or set(value) != {'schema', 'store_root', 'authority_path', 'executable'}
            or value['schema'] != 'dradar.managed_selection.v1'):
        raise RefreshUnavailable('managed_selection_invalid')
    paths = [Path(value[key]) for key in ('store_root', 'authority_path', 'executable')]
    if not all(item.is_absolute() for item in paths):
        raise RefreshUnavailable('managed_selection_invalid')
    store = ManagedAuthStore(paths[0])
    authority = select_authority('codex', [paths[1]], local_key=store._key())
    store.guard(authority, paths[2])()
    payload = json.loads(authority.read())
    tokens = payload.get('tokens') if isinstance(payload, dict) else None
    refresh = tokens.get('refresh_token') if isinstance(tokens, dict) else None
    if not isinstance(refresh, str) or not refresh or len(refresh)>65536 or any(c.isspace() for c in refresh):
        raise RefreshUnavailable('managed_refresh_material_unavailable')
    if not allow_pending and (paths[0] / 'gates' / authority.store_id / 'pending.json').exists():
        raise RefreshUnavailable('recovery_required')
    return path, store, authority, paths[2]


def readiness():
    try:
        selected = load_selection()
        return ('managed', True) if selected is not None else ('native', False)
    except (OSError, ValueError, TypeError, RefreshUnavailable):
        return 'managed', False


def _print_message(message: str) -> None:
    """Keep fixed diagnostics usable on legacy Windows redirected streams."""
    try:
        print(message)
    except UnicodeEncodeError:
        # Do not change global stream configuration or write UTF-8 bytes into
        # a stream whose consumer expects a legacy encoding.
        print(message.encode('ascii', 'backslashreplace').decode('ascii'))


def _cmd_managed_auth(args):
    command = args.managed_auth_command
    path = selection_path()
    if command == 'use-native':
        # Disable future managed selection; retain stores and recovery evidence.
        if path.is_symlink():
            raise RefreshUnavailable('managed_selection_invalid')
        path.unlink(missing_ok=True)
        _print_message('已选择普通官方凭据兼容模式。仅影响后续任务，在途任务不切换。受控源及恢复材料保留；未撤销提供方 OAuth。')
        return
    if command == 'login':
        root = (local_config.HOME / 'managed-auth' / 'store').resolve()
        store = ManagedAuthStore(root)
        executable = getattr(args, 'codex_bin', None)
        if executable is None:
            from .managed_auth_install import acquire
            executable = acquire(store)
        else:
            executable = Path(executable).expanduser().resolve(strict=True)
        authority = store.login(executable)
        pinned = store._runtime(executable)
        private_directory(path.parent)
        atomic_private_credential(path, json.dumps({'schema':'dradar.managed_selection.v1',
            'store_root':str(root), 'authority_path':str(authority.path),
            'executable':str(pinned)}).encode())
        _print_message('受控登录源已建立并选择；仅在服务端支持该模式时领取运行。')
        return
    # Status must preserve the difference between unselected and invalid.
    try:
        selected = load_selection(allow_pending=command in ('recover', 'revoke'))
    except (OSError, ValueError, RefreshUnavailable):
        if command != 'status':
            raise
        _print_message('受控源不可用：请检查登录状态；存在待恢复事务时先执行 recover。')
        return 1
    if selected is None:
        _print_message('当前使用普通官方凭据兼容模式。')
        return
    _, store, authority, executable = selected
    if command == 'recover':
        store.recover(authority, executable)
        _print_message('已完成本地前向恢复；未请求 OAuth 或模型。')
        return
    if command == 'revoke':
        atomic_private_credential(authority.path.parent / 'revoked.json', b'{"state":"locally-revoked"}')
        _print_message('已禁止该受控源的新会话和后续续签，恢复材料保留。在途 AT 未保证立即失效；此操作不等于提供方 OAuth 撤销。')
        return
    material = project_access('codex', authority.read(), local_key=store._key())
    if command == 'status':
        _print_message('受控源已选择；' + ('AT 当前可用。' if material.usable() else '启动前需要宿主续签。'))
        return
    raise ValueError('unsupported managed authentication command')


def cmd_managed_auth(args):
    try:
        return _cmd_managed_auth(args)
    except (OSError, ValueError, TypeError, RefreshUnavailable) as error:
        code = str(error) if isinstance(error, RefreshUnavailable) else 'managed_local_input_invalid'
        _print_message('受控认证操作未完成：' + code + '。请检查固定程序/本地状态；未自动切换账号或模式。')
        return 1
