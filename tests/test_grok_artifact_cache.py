"""The public Grok binary is fetched once before private trial builds."""

from __future__ import annotations

import hashlib
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from dradar import grok_artifact_cache as cache
from dradar import pier_sitecustomize as shim


def _pins(data: bytes) -> dict[str, str]:
    return {"x86_64": hashlib.sha256(data).hexdigest()}


def test_sixteen_waiters_share_one_verified_download(tmp_path, monkeypatch):
    data = b"public-grok-binary" * 100
    calls = []

    def download(url, target, deadline):
        calls.append(url)
        target.write_bytes(data)

    monkeypatch.setattr(cache, "_download", download)
    def ensure():
        return cache.ensure_grok_artifact(
            cache_root=tmp_path, version="1.0.40", arch="x86_64",
            digests=_pins(data),
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: ensure(), range(16)))
    assert len(calls) == 1
    assert len(set(results)) == 1
    assert results[0][0].read_bytes() == data


def test_verified_host_binary_seeds_cache_without_network(tmp_path, monkeypatch):
    data = b"official-linux-grok"
    seed = tmp_path / "managed-grok"
    seed.write_bytes(data)
    monkeypatch.setattr(cache, "_download", lambda *_: pytest.fail("unexpected network"))
    path, digest = cache.ensure_grok_artifact(
        cache_root=tmp_path / "cache", seed_path=seed,
        version="1.0.40", arch="x86_64", digests=_pins(data),
    )
    assert digest == hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


def test_corruption_and_failed_download_do_not_poison_cache(tmp_path, monkeypatch):
    data = b"correct-public-binary"
    digest = _pins(data)
    count = 0

    def download(url, target, deadline):
        nonlocal count
        count += 1
        if count == 1:
            target.write_bytes(b"truncated")
            return
        target.write_bytes(data)

    monkeypatch.setattr(cache, "_download", download)
    options = dict(cache_root=tmp_path, version="1.0.40", arch="x86_64", digests=digest)
    path, _ = cache.ensure_grok_artifact(**options)
    assert count == 2
    assert not list(tmp_path.glob("*.part"))
    path.write_bytes(b"damaged")
    assert cache.ensure_grok_artifact(**options)[0].read_bytes() == data
    assert count == 3

    path.unlink()
    monkeypatch.setattr(cache, "_download", lambda _url, target, _deadline: target.write_bytes(b"bad"))
    with pytest.raises(cache.GrokArtifactError, match="checksum"):
        cache.ensure_grok_artifact(**options)
    assert not path.exists()
    assert not list(tmp_path.glob("*.part"))


def test_version_arch_and_source_digest_are_separate(tmp_path, monkeypatch):
    data = b"binary"
    urls = []

    def download(url, target, deadline):
        urls.append(url)
        target.write_bytes(data)

    monkeypatch.setattr(cache, "_download", download)
    digest = hashlib.sha256(data).hexdigest()
    for version, arch in (("1.0.40", "x86_64"), ("1.0.40", "aarch64"), ("1.0.41", "x86_64")):
        cache.ensure_grok_artifact(
            cache_root=tmp_path, version=version, arch=arch,
            digests={arch: digest},
        )
    assert len(set(urls)) == 3
    original = cache.GROK_ARTIFACT_URL
    monkeypatch.setattr(cache, "GROK_ARTIFACT_URL", "https://example.invalid/other-source")
    other, _ = cache.ensure_grok_artifact(
        cache_root=tmp_path, version="1.0.40", arch="x86_64",
        digests={"x86_64": digest},
    )
    assert len(urls) == 4
    assert other.name != next(
        p.name for p in tmp_path.glob("grok-1.0.40-linux-x86_64-*")
        if hashlib.sha256(original.encode("ascii")).hexdigest()[:12] in p.name
        and p.suffix != ".lock"
    )


def test_pier_rewrite_uses_checked_public_binary_or_keeps_no_cache_path(tmp_path, monkeypatch):
    data = b"public-binary"
    digest = hashlib.sha256(data).hexdigest()
    source_id = hashlib.sha256(cache.GROK_ARTIFACT_URL.encode("ascii")).hexdigest()[:12]
    source = tmp_path / f"grok-1.0.40-linux-x86_64-{digest}-{source_id}"
    source.write_bytes(data)
    run = (
        "set -euo pipefail; apt-get install -y ca-certificates curl coreutils; "
        f'case "$(uname -m)" in x86_64) grok_arch=x86_64; grok_sha={digest} ;; esac; '
        f"curl {cache.GROK_ARTIFACT_URL}/grok-1.0.40-linux-x86_64"
    )
    install = SimpleNamespace(
        agent_name="grok-build", version="1.0.40",
        steps=[SimpleNamespace(user="root", run=run)],
    )
    dockerfile = tmp_path / "Dockerfile"
    original = "FROM ubuntu:24.04\nUSER root\nRUN " + json.dumps(
        ["/bin/bash", "-c", run]
    ) + "\n"
    dockerfile.write_text(original)
    env = SimpleNamespace(agent_install_spec=install, _agent_build_context_dir=tmp_path)
    monkeypatch.delenv(shim._GROK_ARTIFACT_ENV, raising=False)
    monkeypatch.delenv(shim._GROK_ARTIFACT_SHA_ENV, raising=False)
    shim._rewrite_grok_agent_dockerfile(env)
    assert dockerfile.read_text() == original
    monkeypatch.setenv(shim._GROK_ARTIFACT_ENV, str(source))
    monkeypatch.setenv(shim._GROK_ARTIFACT_SHA_ENV, digest)
    shim._rewrite_grok_agent_dockerfile(env)
    changed = dockerfile.read_text()
    assert "COPY dradar-grok-public-artifact" in changed
    assert cache.GROK_ARTIFACT_URL not in changed
    assert "apt-get install" in changed
    assert (tmp_path / "dradar-grok-public-artifact").read_bytes() == data


def test_pier_and_host_pins_match():
    source = Path(shim.__file__).with_name("pier_grok.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    pins = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
        and target.id in {"GROK_LINUX_SHA256", "GROK_CLI_VERSION"}
    }
    assert pins["GROK_LINUX_SHA256"] == cache.GROK_LINUX_SHA256
    assert pins["GROK_CLI_VERSION"] == cache.GROK_CLI_VERSION
    assert shim._GROK_ARTIFACT_SOURCE == cache.GROK_ARTIFACT_URL
