"""No-generation Grok preflight in the task runtime's Linux lock domain.

The host and a Docker VM may expose identical file bytes without sharing flock.
Every readiness probe therefore uses the same local daemon as task containers.
Only image preparation is serialized; native OAuth locks serialize refreshes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import certifi
import httpx

VERSION = "1.0.40"
LINUX_SHA256 = {
    "x86_64": "92c997dfd109c0672d40d5ae6fbd15835d53ffaf12cf9ea124d22aaef3ff23fc",
    "aarch64": "a16d26cf06892ebb3eca9a702c65e031a053431ed4dde3b23bebc58a92b6117f",
}
BASE_IMAGE = "docker.io/library/debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"
LABEL = "io.codex-radar.grok-probe-spec"
AUTH_ROOT = "/run/dradar-grok-auth"
PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class ProbeUnavailable(ValueError):
    pass


def _run(argv, *, env=None, timeout=15, **kwargs):
    if env is not None and "--host" in argv:
        env = dict(env)
        env.pop("DOCKER_CONTEXT", None)
        env.pop("DOCKER_HOST", None)
    return subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        **kwargs,
    )


def local_daemon(env):
    docker = shutil.which("docker", path=env.get("PATH"))
    if not docker:
        raise ProbeUnavailable("Grok readiness needs the local Docker engine")
    context = env.get("DOCKER_CONTEXT", "").strip()
    endpoint = env.get("DOCKER_HOST", "").strip() if not context else ""
    if not endpoint:
        command = [docker, "context", "inspect"] + ([context] if context else [])
        result = _run(command + ["--format", "{{.Endpoints.docker.Host}}"], env=env)
        if result.returncode:
            raise ProbeUnavailable("Grok readiness cannot resolve the Docker daemon")
        endpoint = result.stdout.strip()
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")) or "\n" in endpoint:
        raise ProbeUnavailable("Grok shared OAuth requires a local Linux Docker daemon")
    command = [docker, "--host", endpoint]
    result = _run(
        command + ["info", "--format", "{{.OSType}} {{.Architecture}}"], env=env
    )
    parts = result.stdout.strip().split()
    if result.returncode or len(parts) != 2 or parts[0] != "linux":
        raise ProbeUnavailable("Grok readiness needs a running Linux Docker engine")
    arch = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(parts[1])
    if arch is None:
        raise ProbeUnavailable("Grok readiness supports Docker amd64 and arm64 only")
    return command, arch


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _download_binary(path, arch, env):
    expected = LINUX_SHA256[arch]
    if path.is_file() and not path.is_symlink() and _digest(path) == expected:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = f"https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-{VERSION}-linux-{arch}"
    # httpx uses the same explicit proxy selection as provider subprocesses.
    proxy = (
        env.get("HTTPS_PROXY")
        or env.get("https_proxy")
        or env.get("ALL_PROXY")
        or env.get("all_proxy")
    )
    temp = None
    try:
        fd, name = tempfile.mkstemp(prefix=".grok-download-", dir=path.parent)
        temp = Path(name)
        with (
            os.fdopen(fd, "wb") as stream,
            httpx.Client(proxy=proxy, timeout=120, follow_redirects=True) as client,
        ):
            with client.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if _digest(temp) != expected:
            raise ProbeUnavailable("Official Grok Linux binary checksum mismatch")
        temp.chmod(0o700)
        temp.replace(path)
    except httpx.HTTPError as exc:
        raise ProbeUnavailable("Official Grok Linux runtime download failed") from exc
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _image_id(command, image, spec, env):
    result = _run(
        command + ["image", "inspect", image, "--format", "{{json .}}"], env=env
    )
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
        identifier = value["Id"]
        if value["Config"].get("Labels", {}).get(LABEL) != spec:
            return None
        if not identifier.startswith("sha256:") or len(identifier) != 71:
            return None
        return identifier
    except (ValueError, KeyError, TypeError):
        return None


def _prepare_image(command, arch, credential, env):
    ca = Path(certifi.where())
    spec = hashlib.sha256(
        (BASE_IMAGE + LINUX_SHA256[arch] + _digest(ca) + "probe-v1").encode()
    ).hexdigest()
    image = f"dradar-grok-probe:{VERSION}-{arch}-{spec[:16]}"
    existing = _image_id(command, image, spec, env)
    if existing:
        return existing
    # This host-local lock only deduplicates image preparation. It is never
    # held across models, and is not an OAuth synchronization mechanism.
    from .auth_refresh import _lock

    cache = credential.parent / "runtime" / "probe" / VERSION / arch
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock(cache / "prepare.lock", timeout=450):
        existing = _image_id(command, image, spec, env)
        if existing:
            return existing
        binary = cache / "grok"
        _download_binary(binary, arch, env)
        with tempfile.TemporaryDirectory(prefix="dradar-grok-image-") as name:
            context = Path(name)
            shutil.copyfile(binary, context / "grok")
            shutil.copyfile(ca, context / "ca.pem")
            (context / "Dockerfile").write_text(
                f"FROM {BASE_IMAGE}\n"
                "COPY --chmod=0755 grok /opt/grok\n"
                "COPY ca.pem /etc/ssl/certs/ca-certificates.crt\n"
                f'LABEL {LABEL}="{spec}"\n'
                "ENV HOME=/tmp/dradar-grok-probe\nWORKDIR /tmp\n"
            )
            result = _run(
                command + ["build", "--tag", image, str(context)], env=env, timeout=300
            )
            if result.returncode:
                raise ProbeUnavailable(
                    "Grok readiness image build failed; retry provider setup"
                )
        identifier = _image_id(command, image, spec, env)
        if identifier is None:
            raise ProbeUnavailable(
                "Grok readiness image identity could not be verified"
            )
        return identifier


def _proxy_env(env):
    result = dict(env)
    # Match the existing Pier/container override; the host download still
    # uses provider_subprocess_env's host-side endpoint.
    override = env.get("DRADAR_CONTAINER_HTTP_PROXY", "").strip()
    if override:
        for key in PROXY_KEYS:
            if key.lower() == "no_proxy":
                continue
            if override.lower() in {"direct", "none", "off"}:
                result.pop(key, None)
            else:
                result[key] = override
    if bypass := env.get("DRADAR_CONTAINER_NO_PROXY", "").strip():
        result["NO_PROXY"] = result["no_proxy"] = bypass
    for key in PROXY_KEYS:
        value = result.get(key)
        if not value or key.lower() == "no_proxy":
            continue
        try:
            parts = urlsplit(value)
            if parts.hostname in {"localhost", "127.0.0.1", "::1"}:
                # Preserve optional proxy authentication without putting it in argv.
                host = parts.netloc.rsplit("@", 1)
                authority = ("".join(host[:-1]) + "@") if len(host) == 2 else ""
                authority += "host.docker.internal" + (
                    ":" + str(parts.port) if parts.port else ""
                )
                result[key] = urlunsplit(parts._replace(netloc=authority))
        except ValueError:
            raise ProbeUnavailable(
                "Grok readiness proxy configuration is invalid"
            ) from None
    return result


def _script(arch):
    version_pattern = VERSION.replace(".", r"\.")
    return (
        "set -eu; "
        f'printf "%s  /opt/grok\\n" {LINUX_SHA256[arch]} | sha256sum --check --status; '
        f'/opt/grok --version | grep -Eq "^grok {version_pattern} "; '
        'mkdir -p "$HOME"; chmod 700 "$HOME"; '
        "timeout --kill-after=5s 40s /opt/grok models"
    )


def run_probe(credential: Path, root: Path, env: dict[str, str]):
    credential = credential.resolve(strict=True)
    command, arch = local_daemon(env)
    from .auth_refresh import RefreshUnavailable

    try:
        image = _prepare_image(command, arch, credential, env)
    except RefreshUnavailable as exc:
        raise ProbeUnavailable(
            "Grok readiness runtime preparation is busy or unavailable"
        ) from exc
    name = "dradar-grok-probe-" + uuid.uuid4().hex
    run_env = _proxy_env(env)
    mount = io.StringIO()
    csv.writer(mount, lineterminator="").writerow(
        [
            "type=bind",
            f"source={credential.parent}",
            f"target={AUTH_ROOT}",
        ]
    )
    argv = command + [
        "run",
        "--name",
        name,
        "--rm",
        "--init",
        "--pull",
        "never",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,mode=1777",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--mount",
        mount.getvalue(),
        "--env",
        f"GROK_AUTH_PATH={AUTH_ROOT}/{credential.name}",
        "--env",
        "GROK_TELEMETRY_ENABLED=0",
        "--env",
        "GROK_TELEMETRY_MIXPANEL_ENABLED=0",
        "--env",
        "GROK_TELEMETRY_TRACE_UPLOAD=0",
    ]
    for key in PROXY_KEYS:
        if run_env.get(key):
            argv += ["--env", key]
    argv += [image, "/bin/sh", "-c", _script(arch)]
    owner_name = name + "-owner"
    try:
        # Resolve ownership inside the daemon: Windows/VM bind ownership need
        # not equal host stat(). Run only metadata inspection as root, read-only
        # and without network. Native refresh runs as the directory's owner so
        # its atomic replacement is host-readable throughout, not just at exit.
        owner_result = _run(
            command
            + [
                "run",
                "--name",
                owner_name,
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--mount",
                mount.getvalue() + ",readonly",
                "--entrypoint",
                "/usr/bin/timeout",
                image,
                "--kill-after=2s",
                "10s",
                "/usr/bin/stat",
                "-c",
                "%u:%g",
                AUTH_ROOT,
            ],
            env=env,
            timeout=15,
        )
        owner = owner_result.stdout.strip()
        parts = owner.split(":")
        if (
            owner_result.returncode
            or len(parts) != 2
            or any(
                not value.isascii()
                or not value.isdecimal()
                or len(value) > 10
                or int(value) >= 2**32 - 1
                for value in parts
            )
        ):
            raise ProbeUnavailable(
                "Grok readiness cannot determine canonical directory ownership"
            )
        argv[argv.index(image) : argv.index(image)] = ["--user", owner]
        return _run(argv, env=run_env, timeout=55)
    finally:
        # Each unique name belongs to this invocation alone. Docker's internal
        # timeout also bounds native auth if this host process crashes outright.
        cleanup_unconfirmed = False
        for owned_name in (name, owner_name):
            try:
                _run(command + ["rm", "--force", owned_name], env=env, timeout=15)
                check = _run(
                    command
                    + [
                        "container",
                        "inspect",
                        owned_name,
                        "--format",
                        "{{.State.Running}}",
                    ],
                    env=env,
                    timeout=10,
                )
                absent = check.returncode != 0 and any(
                    marker in check.stderr.lower()
                    for marker in ("no such container", "no such object")
                )
                cleanup_unconfirmed |= not absent
            except (OSError, subprocess.TimeoutExpired):
                cleanup_unconfirmed = True
        if cleanup_unconfirmed:
            raise ProbeUnavailable("Grok readiness container cleanup is unconfirmed")
