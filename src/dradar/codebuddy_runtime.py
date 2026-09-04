"""Pier-free definition of the pinned CodeBuddy container runtime."""

from __future__ import annotations

import shlex

CODEBUDDY_CLI_VERSION = "2.137.1"
CODEBUDDY_CONTAINER_IMAGE = f"dradar-codebuddy:{CODEBUDDY_CLI_VERSION}"
CODEBUDDY_IMAGE_LABEL = "io.codex-radar.codebuddy.version"
CODEBUDDY_BASE_IMAGE = (
    "docker.io/library/debian:bookworm-slim@"
    "sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"
)

_BASE_URL = (
    "https://acc-1258344699.cos.ap-guangzhou.myqcloud.com/"
    "@tencent-ai/codebuddy-code/releases/download"
)
_LINUX_SHA256 = {
    "aarch64": "fe75f4491157837460d33fc201d9062dc1dde67c241ffb96bfac96dda92cbda1",
    "x86_64": "a09e887057cde96383ecab875faac7a3d357094c7147f3bc3a69dd7d68d2887b",
}


def codebuddy_install_command() -> str:
    """Return the reviewed shell command used to build the pinned image."""

    return (
        "set -euo pipefail; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "apt-get update && DEBIAN_FRONTEND=noninteractive "
        "apt-get install -y --no-install-recommends curl ca-certificates tar; "
        "elif command -v apk >/dev/null 2>&1; then "
        "apk add --no-cache curl ca-certificates tar; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "dnf install -y curl ca-certificates tar; "
        "elif command -v yum >/dev/null 2>&1; then "
        "yum install -y curl ca-certificates tar; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        "arch=$(uname -m); "
        f"case \"$arch\" in aarch64|arm64) asset=arm64; "
        f"sha={_LINUX_SHA256['aarch64']} ;; "
        f"x86_64|amd64) asset=x86_64; sha={_LINUX_SHA256['x86_64']} ;; "
        "*) echo \"unsupported CodeBuddy architecture: $arch\" >&2; exit 2 ;; esac; "
        "tmp=$(mktemp -d); trap 'rm -rf \"$tmp\"' EXIT; "
        f"url={shlex.quote(_BASE_URL + '/' + CODEBUDDY_CLI_VERSION)}/"
        "codebuddy-code_Linux_${asset}.tar.gz; "
        "curl --fail --silent --show-error --location "
        "--output \"$tmp/codebuddy.tar.gz\" \"$url\"; "
        "printf '%s  %s\\n' \"$sha\" \"$tmp/codebuddy.tar.gz\" "
        "| sha256sum --check --strict -; "
        "tar -xzf \"$tmp/codebuddy.tar.gz\" -C \"$tmp\"; "
        "mkdir -p /opt/codebuddy/bin; "
        "install -m 0755 \"$tmp/codebuddy\" /opt/codebuddy/bin/codebuddy; "
        "test \"$(/opt/codebuddy/bin/codebuddy --version)\" = "
        f"\"{CODEBUDDY_CLI_VERSION}\""
    )


__all__ = [
    "CODEBUDDY_BASE_IMAGE",
    "CODEBUDDY_CLI_VERSION",
    "CODEBUDDY_CONTAINER_IMAGE",
    "CODEBUDDY_IMAGE_LABEL",
    "codebuddy_install_command",
]
