"""Small, checkpoint-free safety helpers shared by private Pier adapters."""

from __future__ import annotations

import os
import shlex
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


_MAX_AGENT_LOG_BYTES = 64 * 1024 * 1024
_ROOT_EXEC_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C",
    "LC_ALL": "C",
    "BASH_ENV": "/dev/null",
    "ENV": "/dev/null",
    "CDPATH": "",
    "PYTHONPATH": "",
    "PYTHONHOME": "",
    "LD_PRELOAD": "",
    "LD_LIBRARY_PATH": "",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


class UnsafeAgentLog(ValueError):
    """A model-writable host log could not be handled without following it."""


class RuntimeSafetyError(RuntimeError):
    """A fixed adapter-maintenance operation violated its safety boundary."""


class AgentLogStore:
    """Bounded, no-follow reads and atomic host-owned writes below agent logs."""

    REJECTED = "[DRADAR rejected an unsafe agent log]\n"

    # Native Windows exposes no os.getuid() and cannot os.open() a directory;
    # those runs fall back to plain path-based reads while keeping the same
    # redaction guarantees.
    _DIRFD_CAPABLE = hasattr(os, "getuid")

    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = Path(logs_dir)
        _getuid = getattr(os, "getuid", None)
        self.uid = _getuid() if callable(_getuid) else 0

    @staticmethod
    def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_uid, value.st_gid, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @staticmethod
    def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
        return value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid

    @staticmethod
    def _stable_windows_fingerprint(
        metadata: "os.stat_result | tuple[int, ...]",
    ) -> tuple[int, ...]:
        """Mask ctime_ns for the native-Windows fallback comparisons.

        Live-protection software (Defender 等) re-stamps NTFS change time
        seconds after a freshly materialized file is scanned, so ctime is
        not a stability signal there. Every other fingerprint field keeps
        being enforced.
        """

        values = tuple(metadata)
        if len(values) >= 9:
            values = values[:8] + (0,)
        return values

    def _leaf(self, path: Path) -> str:
        candidate = Path(path)
        if (
            candidate.parent != self.logs_dir
            or candidate.name in {"", ".", ".."}
            or candidate != self.logs_dir / candidate.name
        ):
            raise UnsafeAgentLog("agent log is outside the logs directory")
        return candidate.name

    def _open_dir(self) -> tuple[int, tuple[int, ...]]:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= (
            getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            parent_before = self.logs_dir.parent.lstat()
            parent_fd = os.open(self.logs_dir.parent, parent_flags)
        except OSError as exc:
            raise UnsafeAgentLog("agent logs parent directory is unsafe") from exc
        try:
            parent_opened = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_before.st_mode)
                or not stat.S_ISDIR(parent_opened.st_mode)
                or (parent_opened.st_dev, parent_opened.st_ino)
                != (parent_before.st_dev, parent_before.st_ino)
                or parent_opened.st_uid != self.uid
                or stat.S_IMODE(parent_opened.st_mode) & 0o022
            ):
                raise UnsafeAgentLog("agent logs parent is not host-private")
            observed = os.stat(
                self.logs_dir.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(self.logs_dir.name, flags, dir_fd=parent_fd)
        except (OSError, UnsafeAgentLog) as exc:
            os.close(parent_fd)
            if isinstance(exc, UnsafeAgentLog):
                raise
            raise UnsafeAgentLog("agent logs directory is unsafe") from exc
        os.close(parent_fd)
        try:
            opened = os.fstat(descriptor)
            mode = stat.S_IMODE(opened.st_mode)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (observed.st_dev, observed.st_ino)
                or opened.st_uid != self.uid
                or mode & 0o700 != 0o700
            ):
                raise UnsafeAgentLog("agent logs directory is not host-owned")
            identity = self._directory_identity(opened)
            self._verify_dir(descriptor, identity)
            return descriptor, identity
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_dir(self, descriptor: int, expected: tuple[int, ...]) -> None:
        try:
            current = self.logs_dir.lstat()
            opened = os.fstat(descriptor)
        except OSError as exc:
            raise UnsafeAgentLog("agent logs directory changed") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or self._directory_identity(current) != expected
            or self._directory_identity(opened) != expected
        ):
            raise UnsafeAgentLog("agent logs directory changed")

    def _read_text_windows(
        self, leaf: str, *, max_bytes: int,
    ) -> tuple[str, tuple[int, ...]] | None:
        target = self.logs_dir / leaf
        try:
            observed = target.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnsafeAgentLog("agent log is unreadable") from exc
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > max_bytes:
            raise UnsafeAgentLog("agent log is not a bounded regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            file_fd = os.open(target, flags)
        except OSError as exc:
            raise UnsafeAgentLog("agent log could not be opened safely") from exc
        try:
            opened = os.fstat(file_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > max_bytes
                or (opened.st_dev, opened.st_ino, opened.st_mtime_ns)
                != (observed.st_dev, observed.st_ino, observed.st_mtime_ns)
            ):
                raise UnsafeAgentLog("agent log changed before it was opened")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(file_fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(file_fd)
            if (
                len(payload) > max_bytes
                or len(payload) != after.st_size
                or (after.st_dev, after.st_ino, after.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_mtime_ns)
            ):
                raise UnsafeAgentLog("agent log changed while it was read")
        except OSError as exc:
            raise UnsafeAgentLog("agent log could not be read safely") from exc
        finally:
            os.close(file_fd)
        return payload.decode("utf-8", errors="replace"), (
            self._stable_windows_fingerprint(after)
        )

    def read_text(
        self, path: Path, *, max_bytes: int = _MAX_AGENT_LOG_BYTES,
    ) -> tuple[str, tuple[int, ...]] | None:
        leaf = self._leaf(path)
        if not self._DIRFD_CAPABLE:
            return self._read_text_windows(leaf, max_bytes=max_bytes)
        directory_fd, identity = self._open_dir()
        try:
            try:
                observed = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                self._verify_dir(directory_fd, identity)
                return None
            except OSError as exc:
                raise UnsafeAgentLog("agent log is unreadable") from exc
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or observed.st_size > max_bytes
                or stat.S_IMODE(observed.st_mode) & 0o7000
            ):
                raise UnsafeAgentLog("agent log is not a bounded regular file")
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                file_fd = os.open(leaf, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise UnsafeAgentLog("agent log could not be opened safely") from exc
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (observed.st_dev, observed.st_ino)
                    or opened.st_size > max_bytes
                    or stat.S_IMODE(opened.st_mode) & 0o7000
                ):
                    raise UnsafeAgentLog("agent log changed before it was opened")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(file_fd, min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                after = os.fstat(file_fd)
                if (
                    len(payload) > max_bytes
                    or len(payload) != after.st_size
                    or self._fingerprint(after) != self._fingerprint(opened)
                ):
                    raise UnsafeAgentLog("agent log changed while it was read")
            except OSError as exc:
                raise UnsafeAgentLog("agent log could not be read safely") from exc
            finally:
                os.close(file_fd)
            self._verify_dir(directory_fd, identity)
            return payload.decode("utf-8", errors="replace"), self._fingerprint(after)
        finally:
            os.close(directory_fd)

    def replace_text(
        self, path: Path, text: str, *, expected: tuple[int, ...] | None = None,
    ) -> bool:
        leaf = self._leaf(path)
        payload = text.encode("utf-8")
        if len(payload) > _MAX_AGENT_LOG_BYTES:
            raise UnsafeAgentLog("replacement agent log is too large")
        if not self._DIRFD_CAPABLE:
            matched = True
            hit_notes: list[str] = []
            if expected is not None:
                try:
                    current = (self.logs_dir / leaf).lstat()
                except OSError:
                    matched = False
                    hit_notes.append(f"replace-precheck-gone {leaf}")
                else:
                    masked_expected = self._stable_windows_fingerprint(expected)
                    masked_current = self._stable_windows_fingerprint(current)
                    matched = masked_current == masked_expected
                    if not matched:
                        names = (
                            "dev", "ino", "mode", "nlink", "uid",
                            "gid", "size", "mtime_ns", "ctime_ns",
                        )
                        diffed = ",".join(
                            name
                            for name, old, new in zip(
                                names, masked_expected, masked_current,
                            )
                            if old != new
                        )
                        hit_notes.append(
                            f"replace-precheck-nomatch[{diffed}] {leaf}",
                        )
                self._record_hit_notes(hit_notes)
            temporary = self.logs_dir / f".dradar-log-{uuid.uuid4().hex}.tmp"
            temporary_exists = True
            try:
                with open(temporary, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.logs_dir / leaf)
                temporary_exists = False
            except OSError as exc:
                raise UnsafeAgentLog(
                    "agent log could not be atomically replaced",
                ) from exc
            finally:
                if temporary_exists and temporary.exists():
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
            published = (self.logs_dir / leaf).stat()
            if not stat.S_ISREG(published.st_mode) or published.st_size != len(
                payload,
            ):
                raise UnsafeAgentLog("published agent log is invalid")
            return matched
        directory_fd, identity = self._open_dir()
        temporary = f".dradar-log-{uuid.uuid4().hex}.tmp"
        created_temp = False
        try:
            matched = True
            if expected is not None:
                try:
                    current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    matched = False
                else:
                    matched = self._fingerprint(current) == expected
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            created_temp = True
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                created = os.fstat(fd)
            finally:
                os.close(fd)
            if expected is not None:
                try:
                    current = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    matched = False
                else:
                    matched = matched and self._fingerprint(current) == expected
            os.replace(temporary, leaf, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            created_temp = False
            os.fsync(directory_fd)
            published = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(published.st_mode)
                or published.st_nlink != 1
                or published.st_uid != self.uid
                or stat.S_IMODE(published.st_mode) != 0o600
                or (published.st_dev, published.st_ino)
                != (created.st_dev, created.st_ino)
            ):
                raise UnsafeAgentLog("published agent log is invalid")
            self._verify_dir(directory_fd, identity)
            return matched
        except OSError as exc:
            raise UnsafeAgentLog("agent log could not be atomically replaced") from exc
        finally:
            if created_temp:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)

    def redact_texts(
        self,
        paths: list[Path],
        sensitive_values: tuple[str, ...],
        marker: str,
        *,
        retain_paths: Iterable[Path] | None = None,
    ) -> dict[Path, str]:
        retained = set(paths if retain_paths is None else retain_paths)
        safe: dict[Path, str] = {}
        rejected = False
        hit_notes: list[str] = []
        for path in paths:
            try:
                snapshot = self.read_text(path)
            except UnsafeAgentLog:
                self.replace_text(path, self.REJECTED)
                rejected = True
                hit_notes.append(f"unsafe-read {path.name}")
                continue
            if snapshot is None:
                continue
            text, fingerprint = snapshot
            redacted = text
            for value in sensitive_values:
                if value and value in redacted:
                    hit_notes.append(
                        f"credential-x{text.count(value)} {path.name}",
                    )
                    redacted = redacted.replace(value, marker)
            matched = self.replace_text(path, redacted, expected=fingerprint)
            if redacted != text or not matched:
                rejected = True
            elif path in retained:
                safe[path] = text
        if hit_notes:
            self._record_hit_notes(hit_notes)
        if rejected:
            raise ValueError(
                "credential material or an unsafe entry reached agent output; "
                "logs were sanitized and the run was rejected"
            )
        return safe

    def _record_hit_notes(self, notes: list[str]) -> None:
        """Append filename-only diagnostic notes outside the scanned tree."""

        from time import strftime

        try:
            report = (
                self.logs_dir.parent.parent / "redaction-hit-report.log"
            )
            stamp = strftime("%Y-%m-%dT%H:%M:%S")
            with open(report, "a", encoding="utf-8") as handle:
                for note in notes:
                    handle.write(f"{stamp} {note}\n")
        except OSError:
            pass


class RuntimeSafety:
    """Host-layout and root-maintenance boundary, without task persistence."""

    # Native Windows exposes no os.getuid() and cannot os.open() a directory.
    _DIRFD_CAPABLE = hasattr(os, "getuid")

    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = Path(logs_dir)
        self.trial_dir = self.logs_dir.parent
        _getuid = getattr(os, "getuid", None)
        _getgid = getattr(os, "getgid", None)
        self.host_uid = _getuid() if callable(_getuid) else 0
        self.host_gid = _getgid() if callable(_getgid) else 0

    def prepare_host_layout(self) -> None:
        if not self._DIRFD_CAPABLE:
            self._prepare_host_layout_windows()
            return
        descriptors: list[int] = []
        try:
            for path in (self.trial_dir, self.logs_dir):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(path, flags)
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self.host_uid
                    or metadata.st_gid != self.host_gid
                ):
                    raise RuntimeSafetyError("runtime host layout is not owned by DRadar")
                os.fchmod(descriptor, 0o700)
            if os.fstat(descriptors[0]).st_dev != os.fstat(descriptors[1]).st_dev:
                raise RuntimeSafetyError("runtime host layout crosses a filesystem")
        except OSError as exc:
            raise RuntimeSafetyError("runtime host layout is unreadable") from exc
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def _prepare_host_layout_windows(self) -> None:
        # Native Windows cannot os.open() a directory; assert the same
        # directory/ownership boundary through plain path stats instead.
        # st_uid/st_gid are always 0 there, matching the guarded host ids.
        metadatas = []
        try:
            for path in (self.trial_dir, self.logs_dir):
                metadata = path.lstat()
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != self.host_uid
                    or metadata.st_gid != self.host_gid
                ):
                    raise RuntimeSafetyError(
                        "runtime host layout is not owned by DRadar",
                    )
                os.chmod(path, 0o700)
                metadatas.append(metadata)
            if metadatas[0].st_dev != metadatas[1].st_dev:
                raise RuntimeSafetyError("runtime host layout crosses a filesystem")
        except OSError as exc:
            raise RuntimeSafetyError("runtime host layout is unreadable") from exc

    async def exec_root_maintenance(
        self, environment: Any, command: str, *, timeout_sec: int = 120,
    ) -> Any:
        clean = (
            "/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root "
            "LANG=C LC_ALL=C BASH_ENV=/dev/null ENV=/dev/null CDPATH= "
            "GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null "
            "/bin/bash --noprofile --norc -c " + shlex.quote(command)
        )
        result = await environment.exec(
            command=clean, user="root", env=dict(_ROOT_EXEC_ENV), cwd="/",
            timeout_sec=timeout_sec,
        )
        if getattr(result, "return_code", None) != 0:
            raise RuntimeSafetyError("runtime root maintenance failed")
        return result

    async def return_runtime_tree_to_host_owner(
        self, environment: Any, remote_path: str,
    ) -> None:
        candidate = PurePosixPath(remote_path)
        logs_root = PurePosixPath("/logs/agent")
        if (
            not candidate.is_absolute()
            or ".." in candidate.parts
            or candidate == logs_root
            or not candidate.is_relative_to(logs_root)
        ):
            raise RuntimeSafetyError("runtime ownership handoff path is unsafe")
        path = shlex.quote(candidate.as_posix())
        owner = f"{self.host_uid}:{self.host_gid}"
        await self.exec_root_maintenance(
            environment,
            "set -eu; "
            f"test -d {path}; test ! -L {path}; "
            f"/usr/bin/find -P {path} -xdev -exec /usr/bin/chown -h -- {owner} {{}} +; "
            f"/usr/bin/chown -h -- {owner} {path}",
        )


__all__ = ["AgentLogStore", "RuntimeSafety", "RuntimeSafetyError", "UnsafeAgentLog"]
