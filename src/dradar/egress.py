"""Prepare the immutable Pier egress sidecar before a task is claimed or run."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import httpx

from .providers import provider_subprocess_env


EGRESS_PROXY_IMAGE_REPOSITORY = "ghcr.io/codex-radar/dradar-egress-proxy"
# Release assets are ordinary anonymous HTTPS downloads. Their final checksums
# and Docker image IDs are replaced after the CI build publishes both archives.
EGRESS_PROXY_RELEASE_COMMIT = "cd7a4dbde2ff8ad111d1576d55ec48e12d8c0d91"
EGRESS_PROXY_RELEASE_BASE_URL = (
    "https://github.com/codex-radar/dradar/releases/download/"
    f"egress-proxy-{EGRESS_PROXY_RELEASE_COMMIT}"
)
EGRESS_PROXY_ASSETS = {
    "amd64": {
        "archive_sha256": (
            "60d4131dc497222f600180ab8911ef40da3e3c37b84ef8c9b0b05524a2d5172d"
        ),
    },
    "arm64": {
        "archive_sha256": (
            "c7181f2490ffd9e012b270a3981c53cada37d33e86999850c148d42fd097712b"
        ),
    },
}
EGRESS_PROXY_MODE_ENV = "DRADAR_EGRESS_PROXY_MODE"
EGRESS_PROXY_IMAGE_OVERRIDE_ENV = "DRADAR_EGRESS_PROXY_IMAGE_OVERRIDE"
EGRESS_PROXY_LEGACY_MODE = "legacy-build"
NESTED_SIDECAR_IMAGE_ENV = "DRADAR_NESTED_SIDECAR_IMAGE"
NESTED_SIDECAR_IMAGE = "python:3.12-alpine"
NESTED_SIDECAR_LOCAL_TAG = "dradar-nested-sidecar:py3"
DRADAR_HTTP_PROXY_ENV = "DRADAR_HTTP_PROXY"
DRADAR_NO_PROXY_ENV = "DRADAR_NO_PROXY"
DRADAR_CONTAINER_HTTP_PROXY_ENV = "DRADAR_CONTAINER_HTTP_PROXY"
DRADAR_CONTAINER_NO_PROXY_ENV = "DRADAR_CONTAINER_NO_PROXY"
_IMAGE_PULL_TIMEOUT_SEC = 300
_IMAGE_INSPECT_TIMEOUT_SEC = 15
_RUNTIME_PROBE_TIMEOUT_SEC = 20
_MAX_IMAGE_ARCHIVE_BYTES = 512 * 1024 * 1024
_RUNTIME_PROBE_HOST = "registry.npmjs.org"
_RUNTIME_PROBE_URL = f"https://{_RUNTIME_PROBE_HOST}/-/ping"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class EgressProxyError(RuntimeError):
    """The filtered-egress runtime cannot be prepared safely."""


def egress_proxy_mode() -> str:
    mode = os.environ.get(EGRESS_PROXY_MODE_ENV, "image").strip().lower()
    if mode not in {"image", EGRESS_PROXY_LEGACY_MODE}:
        raise EgressProxyError(
            f"{EGRESS_PROXY_MODE_ENV} must be 'image' or "
            f"'{EGRESS_PROXY_LEGACY_MODE}'"
        )
    return mode


def _normalized_arch(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in {"amd64", "x86_64", "x64"}:
        return "amd64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    return None


def _docker_arch(docker: str) -> str:
    try:
        proc = subprocess.run(
            [docker, "version", "--format", "{{.Server.Arch}}"],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EgressProxyError("could not determine the Docker server architecture") from exc
    arch = _normalized_arch(proc.stdout) if proc.returncode == 0 else None
    if arch is None:
        raise EgressProxyError(
            "the pinned Pier egress image supports Docker amd64 and arm64 only"
        )
    return arch


def _validated_image_override() -> str | None:
    override = os.environ.get(EGRESS_PROXY_IMAGE_OVERRIDE_ENV, "").strip()
    if not override:
        return None
    prefix = f"{EGRESS_PROXY_IMAGE_REPOSITORY}@"
    if not override.startswith(prefix) or not _DIGEST_RE.fullmatch(
        override[len(prefix):]
    ):
        raise EgressProxyError(
            f"{EGRESS_PROXY_IMAGE_OVERRIDE_ENV} must pin the official image "
            "repository to a sha256 digest"
        )
    return override


def _portable_image_tag(arch: str) -> str:
    return (
        f"{EGRESS_PROXY_IMAGE_REPOSITORY}:"
        f"release-{EGRESS_PROXY_RELEASE_COMMIT}-{arch}"
    )


def egress_proxy_image(docker: str | None = None) -> str | None:
    """Return the official source reference, or None for emergency rollback."""

    if egress_proxy_mode() == EGRESS_PROXY_LEGACY_MODE:
        return None
    if override := _validated_image_override():
        return override
    arch = _docker_arch(docker) if docker else _normalized_arch(platform.machine())
    if arch is None or arch not in EGRESS_PROXY_ASSETS:
        raise EgressProxyError(
            "the pinned Pier egress image supports Docker amd64 and arm64 only"
        )
    return _portable_image_tag(arch)


def _docker_image_details(docker: str, image: str) -> tuple[str, str] | None:
    try:
        proc = subprocess.run(
            [
                docker,
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}|{{.Config.User}}",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    platform, separator, user = proc.stdout.strip().partition("|")
    if not separator:
        return None
    return platform, user


def _docker_image_identity(docker: str, image: str) -> tuple[str, str, str] | None:
    try:
        proc = subprocess.run(
            [
                docker,
                "image",
                "inspect",
                "--format",
                "{{.Id}}|{{index .Config.Labels "
                "\"org.opencontainers.image.revision\"}}|"
                "{{index .Config.Labels \"org.opencontainers.image.source\"}}",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    image_id, separator, remainder = proc.stdout.strip().partition("|")
    revision, second_separator, source = remainder.partition("|")
    if not separator or not second_separator:
        return None
    return image_id, revision, source


def _validate_image_details(
    details: tuple[str, str] | None, *, expected_arch: str | None = None,
) -> None:
    if details is None:
        raise EgressProxyError("the pinned Pier egress image is not available locally")
    platform, user = details
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise EgressProxyError(
            f"the Pier egress image does not support Docker platform {platform or 'unknown'}"
        )
    if expected_arch is not None and platform != f"linux/{expected_arch}":
        raise EgressProxyError(
            "the loaded Pier egress image does not match the Docker server architecture"
        )
    if user.strip().lower() in {"", "0", "root"}:
        raise EgressProxyError("the pinned Pier egress image unexpectedly runs as root")


def _validated_release_image_id(
    docker: str, tag: str, arch: str,
) -> str | None:
    details = _docker_image_details(docker, tag)
    identity = _docker_image_identity(docker, tag)
    if details is None and identity is None:
        return None
    _validate_image_details(details, expected_arch=arch)
    if identity is None:
        raise EgressProxyError("the loaded Pier egress image identity is unreadable")
    image_id, revision, source = identity
    if not _DIGEST_RE.fullmatch(image_id):
        raise EgressProxyError("the loaded Pier egress image ID is invalid")
    if revision != EGRESS_PROXY_RELEASE_COMMIT:
        raise EgressProxyError("the loaded Pier egress image revision is invalid")
    if source != "https://github.com/codex-radar/dradar":
        raise EgressProxyError("the loaded Pier egress image source is invalid")
    return image_id


def _pull_failure_hint(output: str) -> str:
    lowered = output.lower()
    if "no matching manifest" in lowered:
        return "the machine architecture is not supported by the Pier egress image"
    if "no space left" in lowered:
        return "Docker has insufficient disk space for the Pier egress image"
    if any(marker in lowered for marker in ("unauthorized", "denied", "forbidden")):
        return "GitHub Container Registry rejected the public image request"
    if any(marker in lowered for marker in (
        "timeout", "timed out", "deadline exceeded", "no such host",
        "network is unreachable", "connection refused", "tls handshake",
        "certificate",
    )):
        return "GitHub Container Registry is unreachable through Docker"
    return "Docker could not pull the pinned Pier egress image"


def _image_lock_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "dradar" / "egress-image.lock"


@contextmanager
def _image_lock():
    path = _image_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":  # pragma: no cover - exercised on Windows runners
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _archive_cache_path(arch: str, expected_sha256: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return (
        cache / "dradar" / "egress-images" / EGRESS_PROXY_RELEASE_COMMIT
        / f"{arch}-{expected_sha256}.tar.gz"
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_matches_no_proxy(host: str, value: str) -> bool:
    host = host.rstrip(".").lower()
    for raw_entry in value.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if entry.startswith("[") and "]" in entry:
            entry = entry[1:entry.index("]")]
        elif entry.count(":") == 1:
            possible_host, possible_port = entry.rsplit(":", 1)
            if possible_port.isdigit():
                entry = possible_host
        entry = entry.lstrip(".").rstrip(".")
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


def _download_proxy_settings(
    url: str, source_env: dict[str, str] | None = None,
) -> tuple[str | None, bool]:
    env = source_env if source_env is not None else dict(os.environ)
    explicit = env.get(DRADAR_HTTP_PROXY_ENV, "").strip()
    if not explicit:
        # Let httpx implement the standard HTTP(S)_PROXY/NO_PROXY contract.
        return None, True
    host = urllib.parse.urlsplit(url).hostname or ""
    no_proxy = env.get(DRADAR_NO_PROXY_ENV, "").strip()
    if no_proxy and _host_matches_no_proxy(host, no_proxy):
        return None, False
    return explicit, False


def _download_release_asset(arch: str, expected_sha256: str) -> Path:
    destination = _archive_cache_path(arch, expected_sha256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _file_sha256(destination) == expected_sha256:
        return destination
    if destination.exists():
        try:
            destination.unlink()
        except OSError as exc:
            raise EgressProxyError(
                "the cached Pier egress image archive is invalid and cannot be replaced"
            ) from exc

    filename = f"dradar-egress-proxy-linux-{arch}.tar.gz"
    url = f"{EGRESS_PROXY_RELEASE_BASE_URL}/{filename}"
    proxy, trust_env = _download_proxy_settings(url)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".part", dir=destination.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    size = 0
    digest = hashlib.sha256()
    try:
        try:
            with httpx.Client(
                proxy=proxy,
                trust_env=trust_env,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, connect=20.0),
            ) as client:
                with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise EgressProxyError(
                            "the official Pier egress image download returned HTTP "
                            f"{response.status_code}"
                        )
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > _MAX_IMAGE_ARCHIVE_BYTES:
                                raise EgressProxyError(
                                    "the Pier egress image archive exceeded its safe size limit"
                                )
                            digest.update(chunk)
                            stream.write(chunk)
        except EgressProxyError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise EgressProxyError(
                "could not download the official Pier egress image; set "
                f"{DRADAR_HTTP_PROXY_ENV}=http://host:port and retry"
            ) from exc
        if digest.hexdigest() != expected_sha256:
            raise EgressProxyError(
                "the downloaded Pier egress image failed SHA-256 verification"
            )
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _load_release_asset(docker: str, arch: str, tag: str) -> str:
    asset = EGRESS_PROXY_ASSETS[arch]
    # Validate the same explicit proxy interface before the first download.
    # The translated host.docker.internal form is for containers only; httpx
    # continues to use the user's original host and port from the environment.
    _upstream_proxy_environment()
    archive = _download_release_asset(arch, asset["archive_sha256"])
    try:
        proc = subprocess.run(
            [docker, "load", "--input", str(archive)],
            capture_output=True,
            text=True,
            timeout=_IMAGE_PULL_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EgressProxyError("Docker timed out while loading the Pier egress image") from exc
    except OSError as exc:
        raise EgressProxyError(
            f"could not start Docker image load: {type(exc).__name__}"
        ) from exc
    if proc.returncode != 0:
        hint = _pull_failure_hint(f"{proc.stdout}\n{proc.stderr}")
        raise EgressProxyError(f"{hint} while loading the verified local archive")
    image_id = _validated_release_image_id(docker, tag, arch)
    if image_id is None:
        raise EgressProxyError("Docker did not load the verified Pier egress image")
    return image_id


def ensure_egress_proxy_image(
    docker: str | None = None, *, announce: bool = False,
) -> str | None:
    """Load and validate the exact sidecar image once per Docker host."""

    docker = docker or shutil.which("docker")
    if not docker:
        raise EgressProxyError("docker CLI is required for the Pier egress image")
    source_image = egress_proxy_image(docker)
    if source_image is None:
        return None
    override = _validated_image_override()
    arch = _docker_arch(docker)
    if override is None:
        try:
            image_id = _validated_release_image_id(docker, source_image, arch)
        except EgressProxyError:
            image_id = None
        if image_id is not None:
            return image_id
        with _image_lock():
            try:
                image_id = image_id or _validated_release_image_id(
                    docker, source_image, arch,
                )
            except EgressProxyError:
                image_id = None
            if image_id is None:
                if announce:
                    print("preparing the pinned Pier egress image (one-time download)...")
                image_id = _load_release_asset(docker, arch, source_image)
        return image_id

    details = _docker_image_details(docker, source_image)
    if details is None:
        with _image_lock():
            details = _docker_image_details(docker, source_image)
            if details is None:
                if announce:
                    print("preparing the pinned Pier egress image (one-time download)...")
                try:
                    proc = subprocess.run(
                        [docker, "pull", source_image],
                        capture_output=True,
                        text=True,
                        timeout=_IMAGE_PULL_TIMEOUT_SEC,
                        check=False,
                        env=provider_subprocess_env(),
                    )
                except subprocess.TimeoutExpired as exc:
                    raise EgressProxyError(
                        "GitHub Container Registry timed out through Docker"
                    ) from exc
                except OSError as exc:
                    raise EgressProxyError(
                        f"could not start Docker image pull: {type(exc).__name__}"
                    ) from exc
                if proc.returncode != 0:
                    hint = _pull_failure_hint(f"{proc.stdout}\n{proc.stderr}")
                    raise EgressProxyError(hint)
                details = _docker_image_details(docker, source_image)
    _validate_image_details(details, expected_arch=arch)
    return source_image


def _proxy_value(env: dict[str, str]) -> str | None:
    # The DRadar-specific interface is authoritative: a malformed value should
    # fail loudly instead of silently falling back to an unrelated shell proxy.
    if value := env.get(DRADAR_HTTP_PROXY_ENV, "").strip():
        return value

    first_standard_value = None
    for name in (
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        value = env.get(name)
        if value and value.strip():
            value = value.strip()
            first_standard_value = first_standard_value or value
            # Squid's parent-proxy protocol is HTTP even for HTTPS CONNECT
            # traffic. Prefer a usable standard variable when a shell also
            # carries an unrelated SOCKS/HTTPS proxy variable.
            if urllib.parse.urlsplit(value).scheme.lower() == "http":
                return value
    return first_standard_value


def _container_proxy_value(env: dict[str, str]) -> str | None:
    """Return the explicit Docker/Pier override, or the host-side default."""

    if value := env.get(DRADAR_CONTAINER_HTTP_PROXY_ENV, "").strip():
        return value
    return _proxy_value(env)


def _validate_proxy_credential(value: str, label: str) -> str:
    decoded = urllib.parse.unquote(value)
    if any(character.isspace() for character in decoded) or "#" in decoded:
        raise EgressProxyError(
            f"the upstream proxy {label} contains unsupported characters"
        )
    return decoded


def _upstream_proxy_environment(
    source_env: dict[str, str] | None = None,
) -> dict[str, str]:
    # Container egress uses only explicit, portable configuration. In
    # particular, do not infer a volunteer's Docker topology from the
    # developer's macOS/OrbStack setup. provider_subprocess_env still handles
    # OS proxy discovery for host-side OAuth, independently.
    env = source_env if source_env is not None else dict(os.environ)
    raw = _container_proxy_value(env)
    if not raw:
        return {}
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.lower() != "http" or not parsed.hostname:
        raise EgressProxyError(
            "Pier container egress requires an HTTP upstream proxy; expose a local "
            "HTTP proxy port instead of HTTPS/SOCKS, or clear the proxy variables"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise EgressProxyError("the upstream HTTP proxy URL must not contain a path")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise EgressProxyError("the upstream HTTP proxy port is invalid") from exc
    host = parsed.hostname
    if ":" in host and host not in {"::1"}:
        raise EgressProxyError("IPv6 upstream proxy hosts are not supported yet")
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        host = "host.docker.internal"
    username = _validate_proxy_credential(parsed.username or "", "username")
    password = _validate_proxy_credential(parsed.password or "", "password")
    if ":" in username:
        raise EgressProxyError("the upstream proxy username cannot contain ':'")

    quoted_user = urllib.parse.quote(username, safe="")
    quoted_password = urllib.parse.quote(password, safe="")
    credentials = ""
    if username or password:
        credentials = f"{quoted_user}:{quoted_password}@"
    build_proxy = f"http://{credentials}{host}:{port}"
    result = {
        "DRADAR_EGRESS_UPSTREAM_HOST": host,
        "DRADAR_EGRESS_UPSTREAM_PORT": str(port),
        "DRADAR_EGRESS_BUILD_PROXY": build_proxy,
    }
    if username or password:
        result["DRADAR_EGRESS_UPSTREAM_USERNAME"] = username
        result["DRADAR_EGRESS_UPSTREAM_PASSWORD"] = password
    no_proxy = (
        env.get(DRADAR_CONTAINER_NO_PROXY_ENV)
        or env.get(DRADAR_NO_PROXY_ENV)
        or env.get("NO_PROXY")
        or env.get("no_proxy")
    )
    if no_proxy:
        result["DRADAR_EGRESS_BUILD_NO_PROXY"] = no_proxy
    return result


def pier_egress_environment(image: str | None = None) -> dict[str, str]:
    """Environment consumed only by the fail-closed Pier bootstrap shim."""

    resolved = egress_proxy_image() if image is None else image
    if resolved is None:
        return {}
    return {
        "DRADAR_EGRESS_PROXY_IMAGE": resolved,
        **_upstream_proxy_environment(),
    }


def _active_buildx_driver(docker: str) -> str | None:
    """Return the selected buildx driver without changing Docker state."""

    try:
        proc = subprocess.run(
            [docker, "buildx", "ls", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if item.get("Current") is True:
            driver = item.get("Driver")
            return driver.strip().lower() if isinstance(driver, str) else None
    return None


def _validate_build_proxy_compatibility(
    docker: str, runtime: dict[str, str],
) -> None:
    """Reject a known build-stage proxy mapping that BuildKit cannot resolve."""

    if runtime.get("DRADAR_EGRESS_UPSTREAM_HOST") != "host.docker.internal":
        return
    if _active_buildx_driver(docker) != "docker-container":
        return
    raise EgressProxyError(
        "the active Docker buildx docker-container driver cannot resolve the "
        "host-gateway mapping required by a loopback proxy during the agent "
        "image build; set "
        f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}="
        "http://<docker-reachable-host>:<port> to an HTTP proxy address that "
        "a Docker bridge container can reach, then rerun doctor"
    )


def _running_in_container() -> bool:
    if os.environ.get("container"):
        return True
    if Path("/.dockerenv").is_file():
        return True
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return any(
        token in lowered
        for token in ("docker", "containerd", "kubepods", "lxc", "podman")
    )


def _docker_image_exists(docker: str, image: str) -> bool:
    try:
        proc = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=_IMAGE_INSPECT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _pull_docker_image(docker: str, image: str) -> bool:
    try:
        proc = subprocess.run(
            [docker, "pull", image],
            capture_output=True,
            text=True,
            timeout=_IMAGE_PULL_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _build_python_sidecar_from_proxy(docker: str, proxy_image: str, tag: str) -> bool:
    dockerfile = (
        f"FROM {proxy_image}\n"
        "USER root\n"
        "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "--no-install-recommends python3 && rm -rf /var/lib/apt/lists/*\n"
    )
    try:
        proc = subprocess.run(
            [docker, "build", "--network=default", "-t", tag, "-"],
            input=dockerfile,
            capture_output=True,
            text=True,
            timeout=_IMAGE_PULL_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def ensure_nested_sidecar_image(docker: str, proxy_image: str) -> str:
    """Small python image for DinD unix-socket forwarders.

    vfs copies a full rootfs per container, so the exam image cannot be reused
    for these processes. Prefer a local alpine image, then a Hub pull, then a
    python3 derivative of the already-loaded Pier egress image.
    """
    if _docker_image_exists(docker, NESTED_SIDECAR_IMAGE):
        return NESTED_SIDECAR_IMAGE
    if _pull_docker_image(docker, NESTED_SIDECAR_IMAGE) and _docker_image_exists(
        docker, NESTED_SIDECAR_IMAGE,
    ):
        return NESTED_SIDECAR_IMAGE
    if _docker_image_exists(docker, NESTED_SIDECAR_LOCAL_TAG):
        return NESTED_SIDECAR_LOCAL_TAG
    if _build_python_sidecar_from_proxy(docker, proxy_image, NESTED_SIDECAR_LOCAL_TAG):
        return NESTED_SIDECAR_LOCAL_TAG
    raise EgressProxyError(
        "nested Docker needs a small python image for the unix-socket sidecar; "
        f"could not pull {NESTED_SIDECAR_IMAGE} and could not install python3 "
        "on a copy of the Pier egress image"
    )


def prepare_egress_proxy_runtime(
    docker: str | None = None, *, announce: bool = False,
) -> dict[str, str]:
    resolved_docker = docker or shutil.which("docker")
    image = ensure_egress_proxy_image(resolved_docker, announce=announce)
    runtime = pier_egress_environment(image)
    if resolved_docker:
        _validate_build_proxy_compatibility(resolved_docker, runtime)
        if image and _running_in_container():
            runtime[NESTED_SIDECAR_IMAGE_ENV] = ensure_nested_sidecar_image(
                resolved_docker, image,
            )
    return runtime


def _runtime_proxy_container_env(runtime: dict[str, str], token: str) -> dict[str, str]:
    result = {
        "PROXY_TOKEN": token,
        "ALLOWLIST_DOMAINS": _RUNTIME_PROBE_HOST,
    }
    mappings = {
        "DRADAR_EGRESS_UPSTREAM_HOST": "UPSTREAM_PROXY_HOST",
        "DRADAR_EGRESS_UPSTREAM_PORT": "UPSTREAM_PROXY_PORT",
        "DRADAR_EGRESS_UPSTREAM_USERNAME": "UPSTREAM_PROXY_USERNAME",
        "DRADAR_EGRESS_UPSTREAM_PASSWORD": "UPSTREAM_PROXY_PASSWORD",
    }
    for source, target in mappings.items():
        if value := runtime.get(source):
            result[target] = value
    return result


def _published_host_port(docker: str, container_name: str) -> int | None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            proc = subprocess.run(
                [docker, "port", container_name, "8080/tcp"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                match = re.search(r":([0-9]{1,5})\s*$", line)
                if match:
                    return int(match.group(1))
        time.sleep(0.2)
    return None


def _runtime_proxy_failure_hint(
    runtime: dict[str, str], source_env: dict[str, str] | None = None,
) -> str:
    """Explain the host/container boundary without guessing local topology."""

    env = dict(os.environ) if source_env is None else source_env
    if env.get(DRADAR_CONTAINER_HTTP_PROXY_ENV, "").strip():
        return (
            "the Pier container cannot reach registry.npmjs.org through "
            f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}; verify that URL from a Docker "
            "bridge container and retry"
        )
    if runtime.get("DRADAR_EGRESS_UPSTREAM_HOST"):
        return (
            "the host proxy is configured, but the Pier container cannot reach "
            "it; if Docker needs a different address, set "
            f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}=http://<docker-reachable-host>:<port> "
            "and retry; do not guess Docker hostnames or start relay containers"
        )
    host_env = provider_subprocess_env() if source_env is None else source_env
    if _proxy_value(host_env):
        return (
            "a host-side proxy is available, but the Pier container is trying "
            "direct networking; set "
            f"{DRADAR_HTTP_PROXY_ENV}=http://<host>:<port> when one URL works on "
            "both sides, or set "
            f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}=http://<docker-reachable-host>:<port> "
            "for Docker only"
        )
    return (
        "the Pier container cannot reach registry.npmjs.org directly; set "
        f"{DRADAR_HTTP_PROXY_ENV}=http://<host>:<port> when one URL works on both "
        "sides, or set "
        f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}=http://<docker-reachable-host>:<port> "
        "for Docker only"
    )


def _probe_runtime_egress(
    docker: str, image: str, runtime: dict[str, str],
) -> None:
    token = secrets.token_urlsafe(24)
    name = f"dradar-egress-preflight-{os.getpid()}-{secrets.token_hex(4)}"
    container_env = _runtime_proxy_container_env(runtime, token)
    command = [
        docker, "run", "--detach", "--rm", "--pull", "never",
        "--name", name,
        "--label", "io.codex-radar.dradar.preflight=true",
        "--publish", "127.0.0.1::8080",
    ]
    if runtime.get("DRADAR_EGRESS_UPSTREAM_HOST") == "host.docker.internal":
        command.extend(["--add-host", "host.docker.internal:host-gateway"])
    for key in container_env:
        # Pass only the variable name. Docker reads the value from the child
        # environment, so proxy credentials never appear in process arguments.
        command.extend(["--env", key])
    command.append(image)
    process_env = provider_subprocess_env()
    process_env.update(container_env)
    started = False
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_RUNTIME_PROBE_TIMEOUT_SEC,
            check=False,
            env=process_env,
        )
        if proc.returncode != 0:
            raise EgressProxyError(
                "Docker could not start the disposable Pier egress preflight"
            )
        started = True
        port = _published_host_port(docker, name)
        if port is None:
            raise EgressProxyError(
                "the disposable Pier egress proxy did not publish a healthy port"
            )
        proxy = f"http://agent:{urllib.parse.quote(token, safe='')}@127.0.0.1:{port}"
        response = None
        last_error = None
        deadline = time.monotonic() + 15
        with httpx.Client(proxy=proxy, timeout=5, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get(_RUNTIME_PROBE_URL)
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    time.sleep(0.2)
        if response is None:
            raise EgressProxyError(_runtime_proxy_failure_hint(runtime)) from last_error
        if response.status_code != 200:
            raise EgressProxyError(
                "the Pier container egress probe was rejected; verify the "
                "container network policy and the configured "
                f"{DRADAR_CONTAINER_HTTP_PROXY_ENV}/{DRADAR_HTTP_PROXY_ENV} interface"
            )
    except subprocess.TimeoutExpired as exc:
        raise EgressProxyError("the Pier egress preflight timed out") from exc
    finally:
        if started:
            try:
                subprocess.run(
                    [docker, "rm", "--force", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def ensure_egress_runtime_ready(
    docker: str | None = None, *, announce: bool = False,
) -> dict[str, str]:
    docker = docker or shutil.which("docker")
    if not docker:
        raise EgressProxyError("docker CLI is required for the Pier egress preflight")
    runtime = prepare_egress_proxy_runtime(docker, announce=announce)
    image = runtime.get("DRADAR_EGRESS_PROXY_IMAGE")
    if image:
        _probe_runtime_egress(docker, image, runtime)
    return runtime


def egress_proxy_preflight(docker: str, platform: str) -> tuple[bool, str]:
    """Doctor contract for the exact image and the host/container proxy bridge."""

    del platform  # Hints are intentionally Docker-engine agnostic.
    try:
        ensure_egress_runtime_ready(docker)
    except EgressProxyError as exc:
        return False, str(exc)
    return True, ""


__all__ = [
    "EGRESS_PROXY_ASSETS", "EGRESS_PROXY_IMAGE_REPOSITORY",
    "EGRESS_PROXY_IMAGE_OVERRIDE_ENV", "EGRESS_PROXY_LEGACY_MODE",
    "EGRESS_PROXY_MODE_ENV", "EGRESS_PROXY_RELEASE_COMMIT",
    "DRADAR_CONTAINER_HTTP_PROXY_ENV", "DRADAR_CONTAINER_NO_PROXY_ENV",
    "DRADAR_HTTP_PROXY_ENV", "DRADAR_NO_PROXY_ENV",
    "EgressProxyError", "egress_proxy_image",
    "egress_proxy_mode", "egress_proxy_preflight",
    "ensure_egress_proxy_image", "ensure_egress_runtime_ready",
    "ensure_nested_sidecar_image",
    "NESTED_SIDECAR_IMAGE", "NESTED_SIDECAR_IMAGE_ENV",
    "NESTED_SIDECAR_LOCAL_TAG",
    "pier_egress_environment",
    "prepare_egress_proxy_runtime",
]
