"""Host-side CodeBuddy subscription runtime and credential preparation.

DRadar imports the minimum CodeBuddy login surface into its own owner-only
provider directory.  Benchmark tasks receive a run-scoped copy; the user's
ordinary CodeBuddy directories are never mounted into Pier and are never
modified by a task.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from .local_config import HOME

CODEBUDDY_AGENT = "codebuddy"
CODEBUDDY_PROVIDER = "codebuddy-subscription"
CODEBUDDY_MODEL = "hy4-preview"
CODEBUDDY_CLI_VERSION = "2.137.1"
CODEBUDDY_NATIVE_EFFORTS = (
    "minimal", "low", "medium", "high", "xhigh", "max",
)
# Keep the public surface narrower than CodeBuddy's native tier list until each
# tier has been explicitly approved for the distributed runner.
CODEBUDDY_SUPPORTED_EFFORTS = frozenset({"medium", "xhigh", "max"})
CODEBUDDY_CAPABILITY = (
    "codebuddy-hy4-preview-subscription-oauth-three-effort-concurrent-v3"
)
CODEBUDDY_RUN_CONFIG_VERSION = (
    "codebuddy-hy4-preview-subscription-oauth-three-effort-concurrent-v3"
)
CODEBUDDY_RUNTIME_PROFILE = (
    "pier-codebuddy-hy4-preview-isolated-copy-concurrent-v2"
)
CODEBUDDY_CONTAINER_IMAGE = f"dradar-codebuddy:{CODEBUDDY_CLI_VERSION}"
CODEBUDDY_SOURCE_IMAGE_ENV = "DRADAR_CODEBUDDY_SOURCE_IMAGE"
CODEBUDDY_HOME_ENV = "CODEBUDDY_CONFIG_DIR"
CODEBUDDY_AUTH_DIR_ENV = "DRADAR_CODEBUDDY_AUTH_DIR"
CODEBUDDY_HOME_RELATIVE_PATH = Path("providers") / "codebuddy" / "current"
CODEBUDDY_IMAGE_LABEL = "io.codex-radar.codebuddy.version"
CODEBUDDY_BASE_IMAGE = (
    "docker.io/library/debian:bookworm-slim@"
    "sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"
)
CODEBUDDY_STORE_MAX_FILES = 256
CODEBUDDY_STORE_MAX_BYTES = 32 * 1024 * 1024
CODEBUDDY_AUTH_MAX_FILES = 16
CODEBUDDY_AUTH_MAX_BYTES = 1024 * 1024
CODEBUDDY_API_KEY_ENVS = frozenset({
    "CODEBUDDY_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
})

_VERSION_RE = re.compile(r"(?:^|\s)(\d+\.\d+\.\d+)(?:\s|$)")
_AUTH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.info")
_VALIDATED_CODEBUDDY_IMAGE_IDS: set[str] = set()


def host_codebuddy_home(
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get(CODEBUDDY_HOME_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".codebuddy"


def host_shared_auth_dir(
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    if configured := env.get(CODEBUDDY_AUTH_DIR_ENV):
        return Path(configured).expanduser()
    if os.name == "nt" and env.get("APPDATA"):
        return (
            Path(env["APPDATA"])
            / "CodeBuddyExtension" / "Data" / "Public" / "auth"
        )
    if sys_platform() == "darwin":
        return (
            Path.home() / "Library" / "Application Support"
            / "CodeBuddyExtension" / "Data" / "Public" / "auth"
        )
    return (
        Path.home() / ".local" / "share" / "CodeBuddyExtension"
        / "Data" / "Public" / "auth"
    )


def sys_platform() -> str:
    """Small seam for platform-specific path tests without importing platform."""

    import sys

    return sys.platform


def managed_codebuddy_home(home: Path | None = None) -> Path:
    root = HOME if home is None else Path(home)
    return root / CODEBUDDY_HOME_RELATIVE_PATH


def managed_local_storage(home: Path | None = None) -> Path:
    return managed_codebuddy_home(home) / "local_storage"


def managed_auth_dir(home: Path | None = None) -> Path:
    return managed_codebuddy_home(home) / "auth"


def codebuddy_executable(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    if explicit := env.get("CODEBUDDY_CLI_PATH"):
        return explicit
    if environ is None:
        return shutil.which("codebuddy") or shutil.which("cbc")
    path = env.get("PATH")
    return shutil.which("codebuddy", path=path) or shutil.which("cbc", path=path)


def codebuddy_version(executable: str | None = None) -> str | None:
    binary = executable or codebuddy_executable()
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(result.stdout + "\n" + result.stderr)
    return match.group(1) if result.returncode == 0 and match else None


def _validated_directory(
    root: Path, label: str, *, require_private: bool,
) -> None:
    try:
        info = root.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {exc}") from exc
    if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a real directory")
    if (
        require_private
        and os.name != "nt"
        and stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(f"{label} must be owner-only (chmod 700)")


def _validated_storage_files(
    root: Path, *, require_private: bool = True,
) -> tuple[Path, ...]:
    _validated_directory(
        root, "CodeBuddy local storage", require_private=require_private,
    )
    files = sorted(root.glob("entry_*.info"))
    if not files:
        raise ValueError(
            "CodeBuddy login storage is empty; log in with the host CodeBuddy CLI first"
        )
    if len(files) > CODEBUDDY_STORE_MAX_FILES:
        raise ValueError("CodeBuddy local storage contains too many records")
    total = 0
    for path in files:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"unsafe CodeBuddy storage record: {path.name}")
        if (
            require_private
            and os.name != "nt"
            and stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError(f"CodeBuddy storage record is not owner-only: {path.name}")
        total += info.st_size
        if total > CODEBUDDY_STORE_MAX_BYTES:
            raise ValueError("CodeBuddy local storage exceeds the safety limit")
    return tuple(files)


def _validated_auth_files(
    root: Path, *, require_private: bool = True,
) -> tuple[Path, ...]:
    _validated_directory(
        root, "CodeBuddy shared auth storage", require_private=require_private,
    )
    files = sorted(root.glob("*.info"))
    if not files:
        raise ValueError(
            "CodeBuddy shared auth storage is empty; log in with the host CLI first"
        )
    if len(files) > CODEBUDDY_AUTH_MAX_FILES:
        raise ValueError("CodeBuddy shared auth storage contains too many records")
    total = 0
    for path in files:
        info = path.lstat()
        if (
            _AUTH_NAME_RE.fullmatch(path.name) is None
            or path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise ValueError(f"unsafe CodeBuddy auth record: {path.name}")
        if (
            require_private
            and os.name != "nt"
            and stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError(f"CodeBuddy auth record is not owner-only: {path.name}")
        total += info.st_size
        if total > CODEBUDDY_AUTH_MAX_BYTES:
            raise ValueError("CodeBuddy shared auth storage exceeds the safety limit")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid CodeBuddy auth record: {path.name}") from exc
        auth = payload.get("auth") if isinstance(payload, dict) else None
        account = payload.get("account") if isinstance(payload, dict) else None
        if not (
            isinstance(auth, dict)
            and isinstance(account, dict)
            and isinstance(auth.get("accessToken"), str)
            and auth["accessToken"]
            and isinstance(auth.get("refreshToken"), str)
            and auth["refreshToken"]
        ):
            raise ValueError(f"incomplete CodeBuddy auth record: {path.name}")
    return tuple(files)


def _copy_private_files(files: tuple[Path, ...], target: Path) -> None:
    target.mkdir(mode=0o700)
    for source in files:
        destination = target / source.name
        shutil.copyfile(source, destination, follow_symlinks=False)
        if os.name != "nt":
            os.chmod(destination, 0o600)


def _replace_login_snapshot(
    storage_files: tuple[Path, ...],
    auth_files: tuple[Path, ...],
    target: Path,
) -> Path:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(parent, 0o700)
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise ValueError("managed CodeBuddy login target must be a real directory")
    staged_parent = Path(tempfile.mkdtemp(prefix=".codebuddy-import-", dir=parent))
    staged = staged_parent / target.name
    staged.mkdir(mode=0o700)
    try:
        _copy_private_files(storage_files, staged / "local_storage")
        _copy_private_files(auth_files, staged / "auth")
        _validated_storage_files(staged / "local_storage")
        _validated_auth_files(staged / "auth")
        previous = target.with_name(f".{target.name}.previous")
        if previous.exists():
            if previous.is_symlink() or not previous.is_dir():
                raise ValueError("unsafe previous CodeBuddy login snapshot")
            if target.exists():
                shutil.rmtree(previous)
            else:
                # Recover the narrow crash window after current -> previous
                # but before staged -> current. Never discard the only valid
                # managed login snapshot merely because setup was interrupted.
                os.replace(previous, target)
        if target.exists():
            os.replace(target, previous)
        try:
            os.replace(staged, target)
        except BaseException:
            if previous.exists():
                os.replace(previous, target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    finally:
        shutil.rmtree(staged_parent, ignore_errors=True)
    return target


def import_host_login(
    *,
    source_home: Path | None = None,
    source_auth: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Atomically import a validated host login into DRadar's private slot."""

    source = (source_home or host_codebuddy_home()).expanduser() / "local_storage"
    storage_files = _validated_storage_files(source, require_private=False)
    auth_files = _validated_auth_files(
        (source_auth or host_shared_auth_dir()).expanduser(),
        require_private=False,
    )
    with _exclusive_provider_lock(home):
        return _replace_login_snapshot(
            storage_files, auth_files, managed_codebuddy_home(home),
        )


def credential_status(home: Path | None = None) -> tuple[bool, str]:
    try:
        storage_files = _validated_storage_files(managed_local_storage(home))
        auth_files = _validated_auth_files(managed_auth_dir(home))
    except ValueError as exc:
        return False, str(exc)
    return True, (
        f"{len(auth_files)} auth record(s) and {len(storage_files)} opaque "
        "storage record(s), permissions isolated"
    )


def credential_files(home: Path | None = None) -> tuple[Path, ...]:
    return (*auth_files(home), *local_storage_files(home))


def auth_files(home: Path | None = None) -> tuple[Path, ...]:
    return _validated_auth_files(managed_auth_dir(home))


def local_storage_files(home: Path | None = None) -> tuple[Path, ...]:
    return _validated_storage_files(managed_local_storage(home))


@contextmanager
def _exclusive_provider_lock(
    home: Path | None = None, *, blocking: bool = False,
) -> Iterator[None]:
    root = managed_codebuddy_home(home).parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(root, 0o700)
    lock_path = root / "run.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, 1)
            else:
                import fcntl

                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                fcntl.flock(fd, operation)
        except (BlockingIOError, OSError) as exc:
            raise ValueError(
                "CodeBuddy credential maintenance is active; retry shortly"
            ) from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _credential_freshness(files: tuple[Path, ...]) -> tuple[int, int, int]:
    """Return a non-secret ordering key for monotonic OAuth refresh merges."""

    freshest = (0, 0, 0)
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        auth = payload.get("auth") if isinstance(payload, dict) else None
        values = tuple(
            value if isinstance(value, int) and not isinstance(value, bool) else 0
            for value in (
                auth.get("lastRefreshTime") if isinstance(auth, dict) else None,
                auth.get("expiresAt") if isinstance(auth, dict) else None,
                auth.get("refreshExpiresAt") if isinstance(auth, dict) else None,
            )
        )
        freshest = max(freshest, values)
    return freshest


@contextmanager
def codebuddy_subscription_session(
    directory: Path, *, home: Path | None = None,
) -> Iterator[Path]:
    """Expose a per-run login copy and monotonically merge OAuth refreshes.

    Independent tasks never share a writable credential tree.  The short host
    lock covers only snapshot and merge operations, so provider calls can run
    concurrently while a late-finishing stale copy cannot overwrite a newer
    refresh produced by another worker.
    """

    with _exclusive_provider_lock(home, blocking=True):
        canonical = managed_codebuddy_home(home)
        storage = _validated_storage_files(canonical / "local_storage")
        auth = _validated_auth_files(canonical / "auth")
        run_home = directory / "codebuddy-login"
        if run_home.exists():
            raise ValueError(
                f"temporary CodeBuddy login path already exists: {run_home}"
            )
        _replace_login_snapshot(storage, auth, run_home)
    body_failed = False
    try:
        yield run_home
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            refreshed_storage = _validated_storage_files(
                run_home / "local_storage"
            )
            refreshed_auth = _validated_auth_files(run_home / "auth")
            with _exclusive_provider_lock(home, blocking=True):
                current_auth = _validated_auth_files(canonical / "auth")
                if (
                    _credential_freshness(refreshed_auth)
                    > _credential_freshness(current_auth)
                ):
                    _replace_login_snapshot(
                        refreshed_storage, refreshed_auth, canonical,
                    )
        except (OSError, ValueError):
            if not body_failed:
                raise
        finally:
            shutil.rmtree(run_home, ignore_errors=True)


def codebuddy_runtime_image_error(docker: str | None = None) -> str | None:
    executable = docker or shutil.which("docker")
    if not executable:
        return "Docker CLI is unavailable"
    try:
        result = subprocess.run(
            [executable, "image", "inspect", CODEBUDDY_CONTAINER_IMAGE],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not inspect the CodeBuddy runtime image: {type(exc).__name__}"
    if result.returncode != 0:
        return f"pinned CodeBuddy runtime image {CODEBUDDY_CONTAINER_IMAGE} is missing"
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "Docker returned invalid CodeBuddy image metadata"
    if not isinstance(values, list) or len(values) != 1:
        return "Docker returned ambiguous CodeBuddy image metadata"
    image = values[0] if isinstance(values[0], dict) else None
    config = image.get("Config") if isinstance(image, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(labels, dict)
        or labels.get(CODEBUDDY_IMAGE_LABEL) != CODEBUDDY_CLI_VERSION
    ):
        return "CodeBuddy runtime image provenance label is missing or mismatched"
    image_id = image.get("Id") if isinstance(image, dict) else None
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or image.get("Os") != "linux"
    ):
        return "CodeBuddy runtime image identity or operating system is invalid"
    if image_id not in _VALIDATED_CODEBUDDY_IMAGE_IDS:
        try:
            checked = subprocess.run(
                [
                    executable, "run", "--rm", "--pull", "never",
                    CODEBUDDY_CONTAINER_IMAGE,
                    "/opt/codebuddy/bin/codebuddy", "--version",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (
                "could not execute the pinned CodeBuddy runtime: "
                f"{type(exc).__name__}"
            )
        if (
            checked.returncode != 0
            or checked.stdout.strip() != CODEBUDDY_CLI_VERSION
        ):
            return "CodeBuddy runtime binary version is missing or mismatched"
        _VALIDATED_CODEBUDDY_IMAGE_IDS.add(image_id)
    return None


def ensure_codebuddy_runtime_image(docker: str | None = None) -> str:
    executable = docker or shutil.which("docker")
    if not executable:
        raise ValueError("Docker CLI is unavailable")
    if codebuddy_runtime_image_error(executable) is None:
        return CODEBUDDY_CONTAINER_IMAGE
    from .pier_codebuddy import _install_command

    dockerfile = (
        f"FROM {CODEBUDDY_BASE_IMAGE}\n"
        'SHELL ["/bin/bash", "-lc"]\n'
        f"LABEL {CODEBUDDY_IMAGE_LABEL}={CODEBUDDY_CLI_VERSION}\n"
        f"RUN {_install_command()}\n"
    )
    try:
        built = subprocess.run(
            [
                executable, "build", "--progress=plain", "--pull",
                "--tag", CODEBUDDY_CONTAINER_IMAGE, "-",
            ],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"could not build the pinned CodeBuddy runtime: {type(exc).__name__}"
        ) from exc
    if built.returncode != 0:
        tail = "\n".join((built.stdout + "\n" + built.stderr).splitlines()[-20:])
        raise ValueError(f"could not build the pinned CodeBuddy runtime:\n{tail}")
    issue = codebuddy_runtime_image_error(executable)
    if issue is not None:
        raise ValueError(issue)
    return CODEBUDDY_CONTAINER_IMAGE


__all__ = [name for name in globals() if name.startswith("CODEBUDDY_")] + [
    "auth_files",
    "codebuddy_executable",
    "codebuddy_runtime_image_error",
    "codebuddy_subscription_session",
    "codebuddy_version",
    "credential_files",
    "credential_status",
    "ensure_codebuddy_runtime_image",
    "host_codebuddy_home",
    "host_shared_auth_dir",
    "import_host_login",
    "local_storage_files",
    "managed_auth_dir",
    "managed_codebuddy_home",
    "managed_local_storage",
]
