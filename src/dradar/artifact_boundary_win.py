"""Windows native handle boundary; no POSIX emulation or optional packages.

Every ancestor is held without write/delete sharing before a child is opened.
Reparse points and non-disk objects are rejected using the opened handle.
Files are held without write/delete sharing for the complete read transaction.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import uuid


class FILE_INFO(C.Structure):
    _fields_ = [('attributes', W.DWORD), ('created', W.FILETIME),
                ('accessed', W.FILETIME), ('written', W.FILETIME),
                ('volume', W.DWORD), ('size_high', W.DWORD), ('size_low', W.DWORD),
                ('links', W.DWORD), ('index_high', W.DWORD), ('index_low', W.DWORD)]


class ACL(C.Structure):
    _fields_ = [('revision', C.c_ubyte), ('sbz1', C.c_ubyte), ('size', W.WORD),
                ('count', W.WORD), ('sbz2', W.WORD)]


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [('length', W.DWORD), ('descriptor', C.c_void_p), ('inherit', W.BOOL)]


def _parts(value):
    path = PureWindowsPath(value)
    if path.is_absolute() or path.drive or path.root:
        raise ValueError('outside_trial')
    for part in path.parts:
        if (part in ('.', '..') or part.endswith((' ', '.'))
                or re.search(r'[<>:"|?*\x00-\x1f]', part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError('unsafe_windows_name')
    return path.parts


class WinAPI:
    def __init__(self, error):
        self.error = error
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.security = C.WinDLL('advapi32', use_last_error=True)
        def bind(dll, name, result, *args):
            function = getattr(dll, name)
            function.restype, function.argtypes = result, list(args)
            return function
        self.open = bind(self.kernel, 'CreateFileW', W.HANDLE, W.LPCWSTR, W.DWORD,
                         W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE)
        self.drive_type = bind(self.kernel, 'GetDriveTypeW', W.UINT, W.LPCWSTR)
        self.volume_info = bind(self.kernel, 'GetVolumeInformationW', W.BOOL,
                                W.LPCWSTR, W.LPWSTR, W.DWORD, C.c_void_p,
                                C.c_void_p, C.c_void_p, W.LPWSTR, W.DWORD)
        self.close = bind(self.kernel, 'CloseHandle', W.BOOL, W.HANDLE)
        self.info = bind(self.kernel, 'GetFileInformationByHandle', W.BOOL,
                         W.HANDLE, C.POINTER(FILE_INFO))
        self.file_type = bind(self.kernel, 'GetFileType', W.DWORD, W.HANDLE)
        self.mkdir = bind(self.kernel, 'CreateDirectoryW', W.BOOL, W.LPCWSTR,
                          C.POINTER(SECURITY_ATTRIBUTES))
        self.free = bind(self.kernel, 'LocalFree', C.c_void_p, C.c_void_p)
        self.get_security = bind(self.security, 'GetSecurityInfo', W.DWORD,
                                 W.HANDLE, C.c_int, W.DWORD, C.POINTER(C.c_void_p),
                                 C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p,
                                 C.POINTER(C.c_void_p))
        self.get_ace = bind(self.security, 'GetAce', W.BOOL, C.c_void_p,
                            W.DWORD, C.POINTER(C.c_void_p))
        self.sid_text = bind(self.security, 'ConvertSidToStringSidW', W.BOOL,
                             C.c_void_p, C.POINTER(W.LPWSTR))
        self.from_sddl = bind(self.security, 'ConvertStringSecurityDescriptorToSecurityDescriptorW',
                              W.BOOL, W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p)
        self.process = bind(self.kernel, 'GetCurrentProcess', W.HANDLE)
        self.open_token = bind(self.security, 'OpenProcessToken', W.BOOL,
                               W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
        self.token_info = bind(self.security, 'GetTokenInformation', W.BOOL,
                               W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))
        self.user = self._user_sid()

    def require_ntfs(self, root):
        root = Path(root).absolute()
        if not re.fullmatch(r'[A-Za-z]:', root.drive) or self.drive_type(root.anchor) != 3:
            raise self.error('windows_local_drive_required')
        filesystem = C.create_unicode_buffer(32)
        if (not self.volume_info(root.anchor, None, 0, None, None, None, filesystem, 32)
                or filesystem.value.upper() != 'NTFS'):
            raise self.error('windows_ntfs_required')

    def fail(self):
        code = C.get_last_error()
        if code in (2, 3):
            raise FileNotFoundError(code, 'artifact_missing')
        raise self.error('windows_handle_rejected')

    def sid(self, pointer):
        text = W.LPWSTR()
        if not self.sid_text(pointer, C.byref(text)):
            self.fail()
        try:
            return text.value
        finally:
            self.free(C.cast(text, C.c_void_p))

    def _user_sid(self):
        token = W.HANDLE()
        if not self.open_token(self.process(), 8, C.byref(token)):
            self.fail()
        try:
            size = W.DWORD()
            self.token_info(token, 1, None, 0, C.byref(size))
            buffer = C.create_string_buffer(size.value)
            if not self.token_info(token, 1, buffer, size, C.byref(size)):
                self.fail()
            return self.sid(C.cast(buffer, C.POINTER(C.c_void_p))[0])
        finally:
            self.close(token)

    def metadata(self, handle):
        info = FILE_INFO()
        if self.file_type(handle) != 1 or not self.info(handle, C.byref(info)):
            raise self.error('unsafe_file_type')
        if info.attributes & 0x400:
            raise self.error('windows_reparse_point')
        return info

    @staticmethod
    def fingerprint(info):
        return (info.attributes, info.volume, info.index_high, info.index_low,
                info.links, info.size_high, info.size_low,
                info.created.dwHighDateTime, info.created.dwLowDateTime,
                info.written.dwHighDateTime, info.written.dwLowDateTime)

    def private(self, handle):
        owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
        if self.get_security(handle, 1, 5, C.byref(owner), None, C.byref(dacl), None,
                             C.byref(descriptor)):
            raise self.error('windows_acl_unavailable')
        try:
            if not owner or self.sid(owner) != self.user or not dacl:
                raise self.error('trial_not_host_private')
            count = C.cast(dacl, C.POINTER(ACL)).contents.count
            # OWNER RIGHTS grants the object's owner, already checked above.
            trusted = {self.user, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4'}
            for index in range(count):
                ace = C.c_void_p()
                if not self.get_ace(dacl, index, C.byref(ace)):
                    self.fail()
                kind, flags = (C.c_ubyte * 2).from_address(ace.value)
                if flags & 8:  # inherit-only ACE does not grant this directory access
                    continue
                if kind == 1:  # deny ACE cannot expand write access
                    continue
                if kind != 0:
                    raise self.error('windows_acl_unsupported')
                mask = W.DWORD.from_address(ace.value + 4).value
                if mask & 0x500D0156 and self.sid(ace.value + 8) not in trusted:
                    raise self.error('trial_not_host_private')
        finally:
            self.free(descriptor)

    def create_private_directory(self, path):
        descriptor = C.c_void_p()
        sddl = f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.user})'
        if not self.from_sddl(sddl, 1, C.byref(descriptor), None):
            self.fail()
        try:
            attributes = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), descriptor, False)
            if not self.mkdir(str(path), C.byref(attributes)) and C.get_last_error() != 183:
                self.fail()
        finally:
            self.free(descriptor)


class WindowsTrialFiles:
    def __init__(self, root, error, *, max_file, max_entries, max_depth):
        self.root = Path(root).absolute()
        self.error = error
        self.api = WinAPI(error)
        self.max_file, self.max_entries, self.max_depth = max_file, max_entries, max_depth
        self.handles, self.links, self.enumerations, self.contents = [], [], [], []
        self.cache = {}

    def _relative(self, relative):
        try:
            return _parts(relative)
        except ValueError as exc:
            raise self.error(str(exc)) from exc

    def __enter__(self):
        try:
            if len(self.root.parts) < 2 or not re.fullmatch(r'[A-Za-z]:', self.root.drive) or self.root.anchor != self.root.drive + '\\':
                raise self.error('windows_local_drive_required')
            self.api.require_ntfs(self.root)
            path = Path(self.root.anchor)
            self._open(path, directory=True)
            for part in self.root.parts[1:]:
                self._relative(part)
                path = path / part
                handle = self._open(path, directory=True)
            self.api.private(handle)
            self.volume = self.api.metadata(handle).volume
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for handle in reversed(self.handles):
            self.api.close(handle)
        self.handles.clear()

    def _open(self, path, *, directory=None, write=False, create=False, lock=False):
        key = (str(path), directory)
        if not write and not create and key in self.cache:
            return self.cache[key]
        access = 0x20080 | (0 if directory is True else 0x80000000)
        if write:
            access |= 0x40000000
        # Missing FILE_SHARE_WRITE/DELETE pins contents, names and reparse state.
        handle = self.api.open(str(path), access, 3 if lock else 1, None,
                               4 if create else 3, 0x02200000, None)
        if handle == C.c_void_p(-1).value:
            self.api.fail()
        self.handles.append(handle)
        info = self.api.metadata(handle)
        is_dir = bool(info.attributes & 0x10)
        if ((directory is not None and directory != is_dir)
                or (not is_dir and info.links != 1)
                or (hasattr(self, 'volume') and info.volume != self.volume)):
            raise self.error('unsafe_file_type')
        self.links.append((handle, self.api.fingerprint(info)))
        if not write and not create:
            self.cache[key] = handle
        return handle

    def parent(self, relative, *, create=False):
        parts = self._relative(relative)
        if not parts:
            raise self.error('missing_leaf')
        path = self.root
        for name in parts[:-1]:
            path = path / name
            if create:
                self.api.create_private_directory(path)
            handle = self._open(path, directory=True)
            if create:
                self.api.private(handle)
        return path, parts[-1]

    def exists(self, relative):
        try:
            parent, leaf = self.parent(relative)
            self._open(parent / leaf)
            return True
        except FileNotFoundError:
            return False

    def _bytes(self, handle, maximum):
        # duplicate is not needed: ReadFile avoids transferring handle ownership.
        read = self.api.kernel.ReadFile
        read.argtypes = [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        read.restype = W.BOOL
        seek = self.api.kernel.SetFilePointerEx
        seek.argtypes = [W.HANDLE, C.c_longlong, C.c_void_p, W.DWORD]
        seek.restype = W.BOOL
        if not seek(handle, 0, None, 0):
            self.api.fail()
        result, total = [], 0
        while total <= maximum:
            buffer = C.create_string_buffer(min(1024 * 1024, maximum + 1 - total))
            count = W.DWORD()
            if not read(handle, buffer, len(buffer), C.byref(count), None):
                self.api.fail()
            if not count.value:
                break
            total += count.value
            result.append(buffer.raw[:count.value])
        if total > maximum:
            raise self.error('file_limit')
        return b''.join(result)

    def read(self, relative, *, max_bytes=None):
        maximum = self.max_file if max_bytes is None else max_bytes
        parent, leaf = self.parent(relative)
        handle = self._open(parent / leaf, directory=False)
        before = self.api.metadata(handle)
        size = (before.size_high << 32) | before.size_low
        if size > maximum:
            raise self.error('file_limit')
        data = self._bytes(handle, maximum)
        if len(data) != size:
            raise self.error('file_changed')
        self.contents.append((handle, size, hashlib.sha256(data).digest()))
        self.verify(contents=False)
        return data

    def _entries(self, path):
        result = {}
        with os.scandir(path) as entries:
            for entry in entries:
                if len(result) >= self.max_entries:
                    raise self.error('entry_limit')
                self._relative(entry.name)
                handle = self._open(path / entry.name)
                info = self.api.metadata(handle)
                result[entry.name] = (info.volume, info.index_high, info.index_low,
                                      info.attributes, info.links)
        return result

    def files(self, relative, *, suffix=None, skip_dirs=frozenset()):
        parent, leaf = self.parent(Path(relative) / '__boundary_leaf__')
        result, count = [], 0
        def walk(path, prefix, depth):
            nonlocal count
            if depth > self.max_depth:
                raise self.error('depth_limit')
            entries = self._entries(path)
            self.enumerations.append((path, entries))
            for name, info in entries.items():
                count += 1
                if count > self.max_entries:
                    raise self.error('entry_limit')
                if info[3] & 0x10:
                    # _entries already validates and pins the directory handle.
                    # Only its contents are pruned, matching the POSIX boundary.
                    if (prefix / name).as_posix() in skip_dirs:
                        continue
                    walk(path / name, prefix / name, depth + 1)
                elif suffix is None or Path(name).suffix == suffix:
                    result.append(prefix / name)
        walk(parent, Path(relative), 0)
        self.verify()
        return sorted(result)

    def verify(self, *, contents=True):
        # Use a copy: enumeration reopens handles which are also checked below.
        for path, expected in list(self.enumerations):
            if self._entries(path) != expected:
                raise self.error('snapshot_changed')
        for handle, expected in self.links:
            info = self.api.fingerprint(self.api.metadata(handle))
            # Directory children may be added during host output construction;
            # identity/type/links are checked here, enumeration above checks sets.
            if info[:5] != expected[:5]:
                raise self.error('directory_changed')
        if contents:
            for handle, size, digest in self.contents:
                if hashlib.sha256(self._bytes(handle, size)).digest() != digest:
                    raise self.error('snapshot_changed')

    def write_host(self, relative, data):
        if self._relative(relative)[0] != '.dradar':
            raise self.error('output_not_host_owned')
        self._write(relative, data, create=True)

    def write_log(self, relative, data):
        if self._relative(relative)[0] != 'agent':
            raise self.error('output_not_host_owned')
        self._write(relative, data, create=False)

    def _write(self, relative, data, *, create):
        parent, leaf = self.parent(relative, create=create)
        temporary = parent / ('.boundary-' + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.verify()
            os.replace(temporary, parent / leaf)
            self.verify()
        finally:
            temporary.unlink(missing_ok=True)

    def open_lock(self, relative):
        import msvcrt
        parent, leaf = self.parent(relative, create=True)
        handle = self._open(parent / leaf, directory=False, write=True, create=True, lock=True)
        self.handles.remove(handle)
        self.links = [(h, info) for h, info in self.links if h != handle]
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
