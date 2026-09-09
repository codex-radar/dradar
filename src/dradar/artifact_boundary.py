"""Fail-closed access to untrusted trial files (stdlib-only for Pier).

POSIX directory descriptors anchor every component; unsupported platforms
reject access. Never use resolve() to establish an untrusted path boundary.
Host outputs belong under the unmounted trial/.dradar directory, not agent/.
"""
from __future__ import annotations

import os
import json
import hashlib
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 32


class UnsafeArtifact(RuntimeError):
    """A sanitized reason code; never contains file names or file content."""


def _supported():
    if os.name == 'nt':
        return
    if (os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW')
            or not hasattr(os, 'O_DIRECTORY') or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd or os.listdir not in os.supports_fd):
        raise UnsafeArtifact('platform_boundary_unavailable')



def preflight_artifact_platform(root=None):
    """Refuse unsupported execution before an agent consumes paid quota."""
    _supported()
    if os.name == 'nt':
        try:
            from _dradar_artifact_boundary_win import WinAPI
        except ModuleNotFoundError:
            from dradar.artifact_boundary_win import WinAPI
        WinAPI(UnsafeArtifact).require_ntfs(root or tempfile.gettempdir())


PLATFORM_PREFLIGHT_MESSAGE = (
    'Artifact boundary support is unavailable on this platform; no agent was started. '
    'Windows requires a fixed local NTFS volume and native boundary support. Do not start a paid run '
    'or clear existing upload blocks; see docs/ARTIFACT_BOUNDARY_RECOVERY.md.'
)


def _parts(path):
    path = Path(path)
    if path.is_absolute() or '..' in path.parts:
        raise UnsafeArtifact('outside_trial')
    return path.parts


def _identity(s):
    return s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid


def _fingerprint(s):
    return (*_identity(s), s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class TrialFiles:
    def __new__(cls, root):
        if os.name == 'nt':
            try:
                from _dradar_artifact_boundary_win import WindowsTrialFiles
            except ModuleNotFoundError:
                from dradar.artifact_boundary_win import WindowsTrialFiles
            return WindowsTrialFiles(root, UnsafeArtifact, max_file=MAX_FILE_BYTES,
                                     max_entries=MAX_ENTRIES, max_depth=MAX_DEPTH)
        return super().__new__(cls)

    def __init__(self, root):
        self.root = Path(root).absolute()
        self._fds = []
        self._links = []
        self._observations = []
        self._enumerations = []
        self._contents = []

    def __enter__(self):
        _supported()
        if '..' in self.root.parts:
            raise UnsafeArtifact('outside_trial')
        try:
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self._fds.append(fd)
            for name in self.root.parts[1:]:
                fd = self._directory(fd, name)
            self.fd = fd
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise UnsafeArtifact('trial_not_host_private')
            self.device = info.st_dev
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for fd in reversed(self._fds):
            os.close(fd)
        self._fds.clear()

    def _directory(self, parent, name, *, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise UnsafeArtifact('unsafe_directory')
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        self._fds.append(fd)
        if _identity(before) != _identity(os.fstat(fd)):
            raise UnsafeArtifact('directory_changed')
        if create and (before.st_uid != os.getuid() or before.st_mode & 0o022):
            raise UnsafeArtifact('output_not_host_private')
        if hasattr(self, 'device') and before.st_dev != self.device:
            raise UnsafeArtifact('cross_device_directory')
        self._links.append((parent, name, fd, _identity(before)))
        return fd

    @staticmethod
    def _entry_set(directory):
        # Reopen "." for an independent directory cursor. dup/scandir(fd)
        # can share a consumed directory offset on Linux.
        fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            result = {}
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(result) >= MAX_ENTRIES:
                        raise UnsafeArtifact('entry_limit')
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    result[entry.name] = (*_identity(info), info.st_nlink)
            return result
        finally:
            os.close(fd)

    @staticmethod
    def _hash_fd(fd, size):
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        length = 0
        while length <= size:
            chunk = os.read(fd, min(1024 * 1024, size + 1 - length))
            if not chunk:
                break
            length += len(chunk)
            digest.update(chunk)
        return length, digest.digest()

    def verify(self, *, contents=True):
        if contents:
            for parent, leaf, expected, size, digest in self._contents:
                fd = None
                try:
                    fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
                    if (_fingerprint(os.fstat(fd)) != expected
                            or self._hash_fd(fd, size) != (size, digest)
                            or _fingerprint(os.fstat(fd)) != expected):
                        raise UnsafeArtifact('snapshot_changed')
                except OSError as exc:
                    raise UnsafeArtifact('snapshot_changed') from exc
                finally:
                    if fd is not None:
                        os.close(fd)
        for directory, expected in self._enumerations:
            if self._entry_set(directory) != expected:
                raise UnsafeArtifact('snapshot_changed')
        for parent, leaf, expected in self._observations:
            current = os.fstat(parent) if leaf is None else os.stat(
                leaf, dir_fd=parent, follow_symlinks=False)
            if _fingerprint(current) != expected:
                raise UnsafeArtifact('snapshot_changed')
        for parent, name, fd, expected in self._links:
            if (_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected
                    or _identity(os.fstat(fd)) != expected):
                raise UnsafeArtifact('directory_changed')

    def parent(self, relative, *, create=False):
        parts = _parts(relative)
        if not parts:
            raise UnsafeArtifact('missing_leaf')
        fd = self.fd
        for name in parts[:-1]:
            fd = self._directory(fd, name, create=create)
        return fd, parts[-1]

    def exists(self, relative):
        try:
            parent, leaf = self.parent(relative)
            os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def open_lock(self, relative):
        parent, leaf = self.parent(relative, create=True)
        return os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=parent)

    def read(self, relative, *, max_bytes=MAX_FILE_BYTES):
        parent, leaf = self.parent(relative)
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_dev != self.device):
            raise UnsafeArtifact('unsafe_file_type')
        if before.st_size > max_bytes:
            raise UnsafeArtifact('file_limit')
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
        try:
            if _fingerprint(before) != _fingerprint(os.fstat(fd)):
                raise UnsafeArtifact('file_changed')
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise UnsafeArtifact('file_limit')
            if (_fingerprint(before) != _fingerprint(os.fstat(fd))
                    or _fingerprint(before) != _fingerprint(
                        os.stat(leaf, dir_fd=parent, follow_symlinks=False))
                    or size != before.st_size):
                raise UnsafeArtifact('file_changed')
            data = b''.join(chunks)
            digest = hashlib.sha256(data).digest()
            if (self._hash_fd(fd, before.st_size) != (before.st_size, digest)
                    or _fingerprint(os.fstat(fd)) != _fingerprint(before)):
                raise UnsafeArtifact('file_changed')
            self._observations.append((parent, leaf, _fingerprint(before)))
            self._contents.append((parent, leaf, _fingerprint(before), len(data), digest))
            # Full content verification is once at snapshot handoff, not for
            # all previous files after every read (which would be quadratic).
            self.verify(contents=False)
            return data
        except OSError as exc:
            raise UnsafeArtifact('file_changed_or_unreadable') from exc
        finally:
            os.close(fd)

    def files(self, relative, *, suffix=None):
        parts = _parts(relative)
        fd = self.fd
        for name in parts:
            fd = self._directory(fd, name)
        result, count = [], 0
        def walk(directory, prefix, depth):
            nonlocal count
            if depth > MAX_DEPTH:
                raise UnsafeArtifact('depth_limit')
            self._observations.append((directory, None, _fingerprint(os.fstat(directory))))
            observed_entries = {}
            # scandir is streaming: hostile entry counts are bounded before sorting.
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_ENTRIES:
                        raise UnsafeArtifact('entry_limit')
                    info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                    observed_entries[entry.name] = (*_identity(info), info.st_nlink)
                    path = prefix / entry.name
                    if stat.S_ISDIR(info.st_mode):
                        walk(self._directory(directory, entry.name), path, depth + 1)
                    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise UnsafeArtifact('unsafe_file_type')
                    elif suffix is None or path.suffix == suffix:
                        result.append(path)
            self._enumerations.append((directory, observed_entries))
        walk(fd, Path(relative), 0)
        self.verify()
        return sorted(result)

    def write_host(self, relative, data):
        parts = _parts(relative)
        if not parts or parts[0] != '.dradar':
            raise UnsafeArtifact('output_not_host_owned')
        parent, leaf = self.parent(relative, create=True)
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise UnsafeArtifact('output_not_host_private')
        name = '.boundary-' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.verify()
            os.replace(name, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            self.verify()
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def read_trial_file(root, relative, *, max_bytes=MAX_FILE_BYTES):
    try:
        with TrialFiles(root) as files:
            return files.read(relative, max_bytes=max_bytes)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafeArtifact('artifact_io_rejected') from exc


@contextmanager
def snapshot_agent(root, *, include_result=False):
    """Copy verified bytes into an unmounted, host-private scratch directory."""
    try:
        with tempfile.TemporaryDirectory(prefix='dradar-logs-') as temporary:
            destination = Path(temporary).resolve()  # host-created, not input
            with TrialFiles(root) as source:
                paths = source.files('agent') if source.exists('agent') else []
                extra = ['.dradar/host-output/trajectory.json', '.dradar/host-output/provider-usage.json', '.dradar/host-output/state.json']
                if include_result:
                    extra.append('result.json')
                if extra:
                    for relative in extra:
                        if source.exists(relative):
                            paths.append(Path(relative))
                total = 0
                for path in paths:
                    data = source.read(path)
                    total += len(data)
                    if total > MAX_TREE_BYTES:
                        raise UnsafeArtifact('tree_limit')
                    target = destination / path
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(data)
                source.verify()
            yield destination
    except OSError as exc:
        raise UnsafeArtifact('artifact_io_rejected') from exc


def private_post_run(method):
    """Run synchronous trajectory/usage generation on a private log snapshot."""
    from functools import wraps
    @wraps(method)
    def guarded(self, context):
        original = self.logs_dir
        trial = original.parent
        with TrialFiles(trial) as files:
            files.write_host('.dradar/host-output/state.json', b'{"complete":false}')
        with snapshot_agent(trial) as snapshot:
            self.logs_dir = snapshot / 'agent'
            self.logs_dir.mkdir(exist_ok=True, mode=0o700)
            try:
                outputs = ('trajectory.json', 'provider-usage.json')
                for name in outputs:
                    (self.logs_dir / name).unlink(missing_ok=True)
                stream_name = getattr(self, '_STREAM_FILE', None)
                inputs = ([self.logs_dir / stream_name] if stream_name else
                          list((self.logs_dir / 'sessions').rglob('*.jsonl')))
                had_input = any(path.is_file() and path.read_bytes().strip() for path in inputs)
                result = method(self, context)
                produced = {}
                published = []
                for name in outputs:
                    try:
                        data = read_trial_file(snapshot, Path('agent') / name)
                    except FileNotFoundError:
                        continue
                    try:
                        value = json.loads(data)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        value = None
                    valid = isinstance(value, dict)
                    if name == 'trajectory.json':
                        valid = valid and isinstance(value.get('steps'), list)
                        if had_input:
                            valid = valid and bool(value['steps'])
                    if not valid:
                        with TrialFiles(trial) as files:
                            files.write_host('.dradar/host-output/state.json', json.dumps({
                                'complete': False, 'outputs': [], 'reason': 'invalid_post_run_output',
                            }).encode('utf-8'))
                        return result
                    produced[name] = data
                if had_input and 'trajectory.json' not in produced:
                    with TrialFiles(trial) as files:
                        files.write_host('.dradar/host-output/state.json', json.dumps({
                            'complete': False, 'outputs': [], 'reason': 'required_trajectory_missing',
                        }).encode('utf-8'))
                    logger = getattr(self, 'logger', None)
                    if logger is not None:
                        logger.warning('DRadar post-run rejected: required_trajectory_missing')
                    return result
                for name, data in produced.items():
                    with TrialFiles(trial) as files:
                        files.write_host(Path('.dradar/host-output') / name, data)
                    published.append(name)
                with TrialFiles(trial) as files:
                    files.write_host('.dradar/host-output/state.json', json.dumps({
                        'complete': True, 'outputs': published,
                        'reason': 'outputs_verified' if published else 'no_conversion_input',
                    }).encode('utf-8'))
                return result
            finally:
                self.logs_dir = original
    return guarded


def preferred_log_path(root, name):
    """Select finalized host output, with legacy input support before migration."""
    if name not in {'trajectory.json', 'provider-usage.json'}:
        raise UnsafeArtifact('unexpected_output')
    root = Path(root)
    try:
        state = json.loads(read_trial_file(root, '.dradar/host-output/state.json', max_bytes=4096))
    except FileNotFoundError:
        state = None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeArtifact('invalid_post_run_state') from exc
    private = root / '.dradar/host-output' / name
    if state is not None:
        if isinstance(state, dict) and state.get('complete') is False:
            reason = state.get('reason')
            if reason in ('required_trajectory_missing', 'invalid_post_run_output'):
                raise UnsafeArtifact(reason)
        if (not isinstance(state, dict) or state.get('complete') is not True
                or not isinstance(state.get('outputs'), list)
                or any(item not in ('trajectory.json', 'provider-usage.json') for item in state['outputs'])):
            raise UnsafeArtifact('post_run_not_finalized')
        if name not in state['outputs']:
            return None
        # A declared output must exist and pass the boundary again at read time.
        return private
    if private.exists() or private.is_symlink():
        return private
    legacy = root / 'agent' / name
    return legacy if legacy.exists() or legacy.is_symlink() else None
