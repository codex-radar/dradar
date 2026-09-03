#!/usr/bin/env python3
"""Build, sign, and verify fail-closed DRadar OTA release bundles.

The production workflow passes an Ed25519 private key through one protected
environment secret. This tool never generates production keys and never writes
private key material to an output file or release artifact.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
import zipfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dradar.ota.manifest import ManifestError, verify_signed_manifest

TARGETS = (
    ("linux", "x86_64"),
    ("linux", "arm64"),
    ("macos", "x86_64"),
    ("macos", "arm64"),
    ("windows", "x86_64"),
    ("windows", "arm64"),
)
ROLLOUT_STAGES = {"internal", "canary", "progressive", "general"}
KEY_STATUSES = {"active", "next", "retired"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
ETAG = re.compile(r'^(?:W/)?("[\x21\x23-\x7e\x80-\xff]*")$')
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ReleaseError(ValueError):
    """A release input or output is unsafe or internally inconsistent."""


class CommittedButUnverified(RuntimeError):
    """The public pointer was committed but its final state is not yet verified."""

    def __init__(
        self,
        *,
        release_id: str,
        sequence: int,
        bucket: str,
        pointer: str,
        public_url: str,
        expected_etag: str | None,
        expected_sha256: str,
        expected_size: int,
        reason: str,
    ) -> None:
        self.report = {
            "status": "committed_but_unverified",
            "committed": True,
            "verified": False,
            "retryable": False,
            "release_id": release_id,
            "sequence": sequence,
            "pointer": pointer,
            "public_url": public_url,
            "expected": {
                "etag": expected_etag,
                "sha256": expected_sha256,
                "size": expected_size,
            },
            "reason": reason,
            "next_action": "do_not_rerun_publish; perform_read_only_verification",
            "verification_steps": [
                (
                    f"GET authenticated R2 object s3://{bucket}/{pointer}; require "
                    "HTTP 200, the expected ETag when available, exact size and SHA-256"
                ),
                (
                    f"GET {public_url} without redirects or cache reuse; require HTTP 200, "
                    "exact size and SHA-256"
                ),
                "If either read differs, stop rollout and escalate; never overwrite or roll back current",
            ],
        }
        super().__init__(reason)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular_snapshot(path: Path, name: str) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ReleaseError(f"expected a regular {name} file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read(), file_stat
    except OSError as exc:
        raise ReleaseError(f"could not read {name}: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_bytes(path: Path, name: str) -> bytes:
    return _read_regular_snapshot(path, name)[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_regular_bytes(path, "JSON"),
            object_pairs_hook=_reject_duplicates,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"could not read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"expected a JSON object: {path}")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReleaseError("release data is not canonical JSON") from exc


def _write_new(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if path.is_symlink():
        raise ReleaseError(f"refusing to replace symlink: {path}")
    if path.exists():
        if path.is_file() and path.read_bytes() == data:
            return
        raise ReleaseError(f"refusing to overwrite non-identical output: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temporary, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReleaseError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ReleaseError(f"not a stable semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_artifact_snapshot(data: bytes, artifact: Any) -> None:
    if len(data) != artifact.size:
        raise ManifestError("downloaded artifact size or file type is invalid")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ManifestError("downloaded artifact SHA-256 mismatch")


def _policy(path: Path) -> dict[str, Any]:
    policy = _read_json(path)
    if set(policy) != {
        "schema_version",
        "channel",
        "minimum_version",
        "max_manifest_validity_days",
        "targets",
        "compatibility",
    }:
        raise ReleaseError("OTA policy has unsupported or missing fields")
    if policy["schema_version"] != 1 or policy["channel"] != "stable":
        raise ReleaseError("unsupported OTA policy")
    _version_tuple(policy["minimum_version"])
    targets = policy["targets"]
    parsed_targets = tuple((item.get("os"), item.get("arch")) for item in targets)
    if parsed_targets != TARGETS:
        raise ReleaseError("policy must contain the exact six supported targets")
    validity = policy["max_manifest_validity_days"]
    if not isinstance(validity, int) or isinstance(validity, bool) or not 1 <= validity <= 90:
        raise ReleaseError("unsafe manifest validity limit")
    return policy


def _registry(
    path: Path,
) -> tuple[dict[str, bytes], dict[str, dict[str, Any]], bytes]:
    document = _read_json(path)
    if set(document) != {"schema_version", "keys"} or document["schema_version"] != 1:
        raise ReleaseError("unsupported trusted-key registry")
    raw_keys = document["keys"]
    if not isinstance(raw_keys, dict):
        raise ReleaseError("trusted-key registry keys must be an object")
    public: dict[str, bytes] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for key_id, item in raw_keys.items():
        if not isinstance(key_id, str) or not IDENTIFIER.fullmatch(key_id):
            raise ReleaseError("unsafe key_id in trusted-key registry")
        if not isinstance(item, dict) or set(item) != {
            "public_key_base64",
            "status",
            "not_before",
            "not_after",
        }:
            raise ReleaseError(f"invalid registry entry for {key_id}")
        if item["status"] not in KEY_STATUSES:
            raise ReleaseError(f"invalid key status for {key_id}")
        not_before = _timestamp(item["not_before"], f"{key_id}.not_before")
        not_after = _timestamp(item["not_after"], f"{key_id}.not_after")
        if not_after <= not_before:
            raise ReleaseError(f"invalid key validity window for {key_id}")
        try:
            decoded = base64.b64decode(item["public_key_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ReleaseError(f"invalid public key for {key_id}") from exc
        if len(decoded) != 32:
            raise ReleaseError(f"invalid public key length for {key_id}")
        public[key_id] = decoded
        metadata[key_id] = item
    return public, metadata, _canonical(document) + b"\n"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_publication_key(
    *,
    key_id: str,
    published_at: str,
    metadata: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    key = metadata.get(key_id)
    if key is None or key["status"] != "active":
        raise ReleaseError("manifest signing key is not active at publication boundary")
    not_before = _timestamp(key["not_before"], "key.not_before")
    not_after = _timestamp(key["not_after"], "key.not_after")
    published = _timestamp(published_at, "manifest.published_at")
    current = now.astimezone(UTC)
    if not not_before <= published < not_after:
        raise ReleaseError(
            "manifest published_at is outside the current registry key validity window"
        )
    if not not_before <= current < not_after:
        raise ReleaseError(
            "manifest signing key is outside its current registry validity window"
        )


def _validate_manifest_publication_window(manifest: Any, now: datetime) -> None:
    published = _timestamp(manifest.published_at, "manifest.published_at")
    expires = _timestamp(manifest.expires_at, "manifest.expires_at")
    current = now.astimezone(UTC)
    if current < published:
        raise ReleaseError("manifest is not yet valid at publication boundary")
    if current >= expires:
        raise ReleaseError("manifest is expired at publication boundary")


def _project_version(source_root: Path) -> str:
    pyproject = source_root / "pyproject.toml"
    init = source_root / "src" / "dradar" / "__init__.py"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ReleaseError("pyproject version is not stable SemVer")
    expected = f'__version__ = "{version}"\n'
    if init.read_text(encoding="utf-8") != expected:
        raise ReleaseError("pyproject and runtime versions differ")
    return version


def _zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info, data


def _build_zipapp(
    source_root: Path,
    destination: Path,
    *,
    version: str,
    sequence: int,
    commit: str,
    tree: str,
    target: tuple[str, str],
) -> None:
    package_root = source_root / "src" / "dradar"
    entries: list[tuple[str, bytes]] = []
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            raise ReleaseError(f"source package contains a symlink: {path}")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(source_root / "src").as_posix()
        if relative == "dradar/_ota_build.json":
            raise ReleaseError("source package contains reserved OTA build metadata")
        entries.append((relative, path.read_bytes()))
    if not entries:
        raise ReleaseError("source package is empty")
    metadata = {
        "schema_version": 1,
        "version": version,
        "sequence": sequence,
        "source_commit": commit,
        "source_tree": tree,
        "target": {"os": target[0], "arch": target[1]},
    }
    entries.extend(
        [
            ("dradar/_ota_build.json", _canonical(metadata) + b"\n"),
            (
                "__main__.py",
                b"from dradar.cli import main\nraise SystemExit(main())\n",
            ),
        ]
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if destination.is_symlink():
        raise ReleaseError(f"refusing to replace symlink: {destination}")
    if destination.exists():
        raise ReleaseError(f"refusing to overwrite artifact: {destination}")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, data in sorted(entries):
                info, body = _zip_entry(name, data)
                archive.writestr(info, body)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build(args: argparse.Namespace) -> int:
    source_root = args.source_root.resolve()
    output_dir = args.output_dir.resolve()
    policy = _policy(args.policy.resolve())
    version = _project_version(source_root)
    if _version_tuple(version) < _version_tuple(policy["minimum_version"]):
        raise ReleaseError("project version is below the first safe OTA version")
    if not COMMIT.fullmatch(args.source_commit) or not COMMIT.fullmatch(args.source_tree):
        raise ReleaseError("source commit and tree must be full lowercase Git hashes")
    if not isinstance(args.sequence, int) or args.sequence < 1:
        raise ReleaseError("sequence must be positive")
    if args.rollout_stage not in ROLLOUT_STAGES:
        raise ReleaseError("unsupported rollout stage")
    if not 0 <= args.basis_points <= 10_000:
        raise ReleaseError("basis points must be in 0..10000")
    if not IDENTIFIER.fullmatch(args.rollout_salt):
        raise ReleaseError("rollout salt must be a bounded identifier")
    published = _timestamp(args.published_at, "published_at")
    expires = _timestamp(args.expires_at, "expires_at")
    if expires <= published:
        raise ReleaseError("expires_at must be later than published_at")
    if expires - published > timedelta(days=policy["max_manifest_validity_days"]):
        raise ReleaseError("manifest validity exceeds policy")
    parsed_base = urlparse(args.base_url)
    if (
        parsed_base.scheme != "https"
        or not parsed_base.netloc
        or parsed_base.username
        or parsed_base.password
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise ReleaseError("base URL must be credential-free HTTPS without query/fragment")
    base_url = args.base_url.rstrip("/")
    short_commit = args.source_commit[:12]
    release_id = f"dradar-cli-{version}-s{args.sequence:010d}-{short_commit}"
    prefix = f"releases/stable/s{args.sequence:010d}/v{version}"
    artifacts: list[dict[str, Any]] = []
    for os_name, arch in TARGETS:
        filename = (
            f"dradar-{version}-s{args.sequence:010d}-{short_commit}-"
            f"{os_name}-{arch}.pyz"
        )
        path = output_dir / filename
        _build_zipapp(
            source_root,
            path,
            version=version,
            sequence=args.sequence,
            commit=args.source_commit,
            tree=args.source_tree,
            target=(os_name, arch),
        )
        artifacts.append(
            {
                "os": os_name,
                "arch": arch,
                "filename": filename,
                "object_key": f"{prefix}/{filename}",
                "url": f"{base_url}/{prefix}/{filename}",
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    plan = {
        "schema_version": 1,
        "source": {
            "repository": "codex-radar/dradar",
            "commit": args.source_commit,
            "tree": args.source_tree,
        },
        "release": {
            "release_id": release_id,
            "version": version,
            "sequence": args.sequence,
            "channel": policy["channel"],
            "published_at": args.published_at,
            "expires_at": args.expires_at,
            "object_prefix": prefix,
        },
        "rollout": {
            "stage": args.rollout_stage,
            "basis_points": args.basis_points,
            "salt": args.rollout_salt,
            "paused": args.paused,
        },
        "compatibility": policy["compatibility"],
        "artifacts": artifacts,
    }
    _write_new(output_dir / "release-plan.json", _canonical(plan) + b"\n")
    print(output_dir / "release-plan.json")
    return 0


def _private_key(args: argparse.Namespace) -> Ed25519PrivateKey:
    if bool(args.private_key_env) == bool(args.private_key_file):
        raise ReleaseError("choose exactly one private-key source")
    if args.private_key_env:
        encoded = os.environ.pop(args.private_key_env, None)
        if encoded is None:
            raise ReleaseError("private-key environment secret is unavailable")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise ReleaseError("private-key environment secret is not valid base64") from exc
    else:
        path = args.private_key_file.resolve()
        raw, file_stat = _read_regular_snapshot(path, "private key")
        if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ReleaseError("private-key file permissions must be 0600 or stricter")
        if len(raw) != 32:
            try:
                raw = base64.b64decode(raw.strip(), validate=True)
            except (TypeError, ValueError) as exc:
                raise ReleaseError("private-key file is neither raw nor base64") from exc
    if len(raw) != 32:
        raise ReleaseError("Ed25519 private key must contain exactly 32 seed bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _manifest_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    release = plan["release"]
    return {
        "schema_version": 1,
        "release_id": release["release_id"],
        "version": release["version"],
        "sequence": release["sequence"],
        "channel": release["channel"],
        "published_at": release["published_at"],
        "expires_at": release["expires_at"],
        "rollout": plan["rollout"],
        "compatibility": plan["compatibility"],
        "artifacts": [
            {
                name: artifact[name]
                for name in ("os", "arch", "filename", "url", "size", "sha256")
            }
            for artifact in plan["artifacts"]
        ],
    }


def _validate_plan(plan: dict[str, Any], policy: dict[str, Any]) -> None:
    if set(plan) != {
        "schema_version",
        "source",
        "release",
        "rollout",
        "compatibility",
        "artifacts",
    } or plan.get("schema_version") != 1:
        raise ReleaseError("unsupported release plan")
    source = plan["source"]
    release = plan["release"]
    rollout = plan["rollout"]
    if not isinstance(source, dict) or set(source) != {"repository", "commit", "tree"}:
        raise ReleaseError("invalid release source")
    if source["repository"] != "codex-radar/dradar":
        raise ReleaseError("unexpected source repository")
    if not COMMIT.fullmatch(source["commit"]) or not COMMIT.fullmatch(source["tree"]):
        raise ReleaseError("invalid source commit/tree")
    if not isinstance(release, dict) or set(release) != {
        "release_id",
        "version",
        "sequence",
        "channel",
        "published_at",
        "expires_at",
        "object_prefix",
    }:
        raise ReleaseError("invalid release identity")
    if release["channel"] != policy["channel"]:
        raise ReleaseError("release channel does not match policy")
    _version_tuple(release["version"])
    if _version_tuple(release["version"]) < _version_tuple(policy["minimum_version"]):
        raise ReleaseError("release version is below policy minimum")
    if (
        not isinstance(release["sequence"], int)
        or isinstance(release["sequence"], bool)
        or release["sequence"] < 1
    ):
        raise ReleaseError("invalid release sequence")
    expected_release_id = (
        f"dradar-cli-{release['version']}-s{release['sequence']:010d}-"
        f"{source['commit'][:12]}"
    )
    expected_prefix = (
        f"releases/stable/s{release['sequence']:010d}/v{release['version']}"
    )
    if release["release_id"] != expected_release_id:
        raise ReleaseError("release ID is not derived from source/version/sequence")
    if release["object_prefix"] != expected_prefix:
        raise ReleaseError("release object prefix is outside the locked layout")
    published = _timestamp(release["published_at"], "published_at")
    expires = _timestamp(release["expires_at"], "expires_at")
    if expires <= published or expires - published > timedelta(
        days=policy["max_manifest_validity_days"]
    ):
        raise ReleaseError("release validity violates policy")
    if not isinstance(rollout, dict) or set(rollout) != {
        "stage",
        "basis_points",
        "salt",
        "paused",
    }:
        raise ReleaseError("invalid rollout policy")
    if rollout["stage"] not in ROLLOUT_STAGES or not isinstance(rollout["paused"], bool):
        raise ReleaseError("invalid rollout state")
    if (
        not isinstance(rollout["basis_points"], int)
        or isinstance(rollout["basis_points"], bool)
        or not 0 <= rollout["basis_points"] <= 10_000
        or not isinstance(rollout["salt"], str)
        or not IDENTIFIER.fullmatch(rollout["salt"])
    ):
        raise ReleaseError("invalid rollout cohort")
    if plan["compatibility"] != policy["compatibility"]:
        raise ReleaseError("compatibility differs from reviewed policy")
    artifacts = plan["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != len(TARGETS):
        raise ReleaseError("release plan must contain exactly six artifacts")
    if not all(isinstance(item, dict) for item in artifacts):
        raise ReleaseError("invalid artifact plan")
    targets = tuple((item.get("os"), item.get("arch")) for item in artifacts)
    if targets != TARGETS:
        raise ReleaseError("release plan must contain the exact six targets")
    prefix = expected_prefix + "/"
    public_bases: set[str] = set()
    for item, (os_name, arch) in zip(artifacts, TARGETS, strict=True):
        if set(item) != {
            "os",
            "arch",
            "filename",
            "object_key",
            "url",
            "size",
            "sha256",
        }:
            raise ReleaseError("invalid artifact plan")
        expected_filename = (
            f"dradar-{release['version']}-s{release['sequence']:010d}-"
            f"{source['commit'][:12]}-{os_name}-{arch}.pyz"
        )
        if item["filename"] != expected_filename:
            raise ReleaseError("artifact filename is not derived from release identity")
        if item["object_key"] != prefix + item["filename"]:
            raise ReleaseError("artifact object key escapes the release prefix")
        suffix = "/" + item["object_key"]
        if not isinstance(item["url"], str) or not item["url"].endswith(suffix):
            raise ReleaseError("artifact URL and object key differ")
        public_base = item["url"][: -len(suffix)]
        parsed_base = urlparse(public_base)
        if (
            parsed_base.scheme != "https"
            or not parsed_base.netloc
            or parsed_base.username
            or parsed_base.password
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ReleaseError("artifact URL has an unsafe public base")
        public_bases.add(public_base)
        if (
            not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 1
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            raise ReleaseError("artifact size or digest is invalid")
    if len(public_bases) != 1:
        raise ReleaseError("artifact URLs do not share one public base")


def _verify_chain(
    manifest: Any,
    *,
    previous_bytes: bytes | None,
    bootstrap: bool,
    trusted_keys: dict[str, bytes],
) -> str | None:
    if bootstrap:
        if previous_bytes is not None:
            raise ReleaseError("bootstrap cannot include a previous manifest")
        if manifest.sequence != 1:
            raise ReleaseError("bootstrap release must use sequence 1")
        return None
    if previous_bytes is None:
        raise ReleaseError("non-bootstrap release requires the previous signed manifest")
    previous = verify_signed_manifest(previous_bytes, trusted_keys)
    if manifest.channel != previous.channel:
        raise ReleaseError("release chain changed channel")
    if manifest.sequence <= previous.sequence:
        raise ReleaseError("release sequence did not increase")
    if _version_tuple(manifest.version) <= _version_tuple(previous.version):
        raise ReleaseError("release version did not increase")
    if _timestamp(manifest.published_at, "published_at") <= _timestamp(
        previous.published_at, "previous.published_at"
    ):
        raise ReleaseError("release publication time did not increase")
    return hashlib.sha256(previous_bytes).hexdigest()


def sign(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan.resolve())
    policy = _policy(args.policy.resolve())
    _validate_plan(plan, policy)
    trusted, metadata, _registry_bytes = _registry(args.registry.resolve())
    key_metadata = metadata.get(args.key_id)
    if key_metadata is None or key_metadata["status"] != "active":
        raise ReleaseError("signing key_id is not active in the reviewed registry")
    published = _timestamp(plan["release"]["published_at"], "published_at")
    if not (
        _timestamp(key_metadata["not_before"], "key.not_before")
        <= published
        < _timestamp(key_metadata["not_after"], "key.not_after")
    ):
        raise ReleaseError("signing key is outside its reviewed validity window")
    private = _private_key(args)
    derived = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    if derived != trusted[args.key_id]:
        raise ReleaseError("private key does not match the reviewed public key")
    document = _manifest_from_plan(plan)
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": args.key_id,
        "value": base64.b64encode(private.sign(_canonical(document))).decode("ascii"),
    }
    encoded = _canonical(document) + b"\n"
    verified = verify_signed_manifest(encoded, trusted)
    previous_bytes = (
        _read_regular_bytes(args.previous_manifest.resolve(), "previous manifest")
        if args.previous_manifest
        else None
    )
    _verify_chain(
        verified,
        previous_bytes=previous_bytes,
        bootstrap=args.bootstrap,
        trusted_keys=trusted,
    )
    _write_new(args.output.resolve(), encoded)
    print(args.output.resolve())
    return 0


def _expected_audit(
    *,
    plan: dict[str, Any],
    manifest: Any,
    manifest_bytes: bytes,
    previous_digest: str | None,
    trusted_keys: dict[str, bytes],
    metadata: dict[str, dict[str, Any]],
    registry_bytes: bytes,
) -> dict[str, Any]:
    key_metadata = metadata.get(manifest.key_id)
    public_key = trusted_keys.get(manifest.key_id)
    if key_metadata is None or public_key is None:
        raise ReleaseError("manifest signing key is absent from the current registry")
    return {
        "schema_version": 1,
        "release_id": manifest.release_id,
        "version": manifest.version,
        "sequence": manifest.sequence,
        "channel": manifest.channel,
        "source": plan["source"],
        "key_id": manifest.key_id,
        "signing_key": {
            "status": key_metadata["status"],
            "not_before": key_metadata["not_before"],
            "not_after": key_metadata["not_after"],
            "public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        },
        "trusted_registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "previous_manifest_sha256": previous_digest,
        "object_prefix": plan["release"]["object_prefix"],
        "artifacts": [
            {
                "filename": item.filename,
                "size": item.size,
                "sha256": item.sha256,
                "url": item.url,
            }
            for item in manifest.artifacts
        ],
    }


def _expected_checksums(
    *,
    manifest: Any,
    manifest_bytes: bytes,
    registry_bytes: bytes,
    audit_bytes: bytes,
) -> bytes:
    entries = {
        artifact.filename: artifact.sha256 for artifact in manifest.artifacts
    }
    entries.update(
        {
            "manifest.json": hashlib.sha256(manifest_bytes).hexdigest(),
            "trusted-keys.json": hashlib.sha256(registry_bytes).hexdigest(),
            "release-audit.json": hashlib.sha256(audit_bytes).hexdigest(),
        }
    )
    return (
        "\n".join(f"{entries[name]}  {name}" for name in sorted(entries)) + "\n"
    ).encode("ascii")


def verify(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan.resolve())
    policy = _policy(args.policy.resolve())
    _validate_plan(plan, policy)
    trusted, metadata, registry_bytes = _registry(args.registry.resolve())
    manifest_bytes = _read_regular_bytes(
        args.manifest.resolve(), "signed manifest"
    )
    manifest = verify_signed_manifest(manifest_bytes, trusted)
    expected = _manifest_from_plan(plan)
    actual = json.loads(manifest.signed_document)
    actual.pop("signature")
    if actual != expected:
        raise ReleaseError("signed manifest differs from the reviewed release plan")
    previous_bytes = (
        _read_regular_bytes(args.previous_manifest.resolve(), "previous manifest")
        if args.previous_manifest
        else None
    )
    previous_digest = _verify_chain(
        manifest,
        previous_bytes=previous_bytes,
        bootstrap=args.bootstrap,
        trusted_keys=trusted,
    )
    artifact_dir = args.artifact_dir.resolve()
    for artifact in manifest.artifacts:
        artifact_bytes = _read_regular_bytes(
            artifact_dir / artifact.filename, "release artifact"
        )
        _verify_artifact_snapshot(artifact_bytes, artifact)
    audit = _expected_audit(
        plan=plan,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        previous_digest=previous_digest,
        trusted_keys=trusted,
        metadata=metadata,
        registry_bytes=registry_bytes,
    )
    audit_bytes = _canonical(audit) + b"\n"
    checksums_bytes = _expected_checksums(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        registry_bytes=registry_bytes,
        audit_bytes=audit_bytes,
    )
    _write_new(args.audit_output.resolve(), audit_bytes)
    _write_new(args.checksums_output.resolve(), checksums_bytes)
    print(json.dumps(audit, sort_keys=True, separators=(",", ":")))
    return 0


def cloudflare_preflight(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[0-9a-f]{32}", args.account_id):
        raise ReleaseError("unsafe Cloudflare account ID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", args.bucket):
        raise ReleaseError("unsafe R2 bucket name")
    parsed_public = urlparse(args.public_base_url)
    if (
        parsed_public.scheme != "https"
        or not parsed_public.netloc
        or parsed_public.username
        or parsed_public.password
        or parsed_public.query
        or parsed_public.fragment
    ):
        raise ReleaseError("unsafe R2 public base URL")
    token = os.environ.pop(args.api_token_env, None)
    if not token:
        raise ReleaseError("Cloudflare bucket-config read token is unavailable")
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{args.account_id}/r2/buckets/{args.bucket}/lock"
    )
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
        timeout=30,
    )
    if response.status_code != 200:
        raise ReleaseError(
            f"could not verify R2 Bucket Lock (HTTP {response.status_code})"
        )
    try:
        document = response.json()
    except json.JSONDecodeError as exc:
        raise ReleaseError("Cloudflare Bucket Lock response is not JSON") from exc
    if not isinstance(document, dict) or document.get("success") is not True:
        raise ReleaseError("Cloudflare did not confirm Bucket Lock configuration")
    result = document.get("result")
    rules = result.get("rules") if isinstance(result, dict) else None
    if not isinstance(rules, list) or not any(
        isinstance(rule, dict)
        and rule.get("enabled") is True
        and rule.get("prefix") == "releases/"
        and isinstance(rule.get("condition"), dict)
        and rule["condition"].get("type") == "Indefinite"
        for rule in rules
    ):
        raise ReleaseError("R2 releases/ prefix lacks an enabled indefinite Bucket Lock")
    host = parsed_public.hostname or ""
    if host.endswith(".r2.dev"):
        raise ReleaseError("r2.dev is not an approved production origin")
    print("R2 preflight: PASS")
    return 0


def fetch_current(args: argparse.Namespace) -> int:
    if args.expect_absent and args.output is not None:
        raise ReleaseError("absent-pointer check must not write an output")
    if not args.expect_absent and args.output is None:
        raise ReleaseError("existing-pointer fetch requires --output")
    parsed = urlparse(args.url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseError("current-manifest URL must be credential-free HTTPS")
    if not args.url.endswith("/channels/stable/current.json"):
        raise ReleaseError("current-manifest URL is not the stable channel pointer")
    response = httpx.get(
        args.url,
        follow_redirects=False,
        timeout=30,
        headers={"cache-control": "no-cache"},
    )
    if args.expect_absent:
        if response.status_code != 404:
            raise ReleaseError("bootstrap requires an absent public stable pointer")
        print("stable pointer: absent")
        return 0
    if response.status_code != 200 or response.headers.get("location"):
        raise ReleaseError(
            f"public stable pointer is unavailable (HTTP {response.status_code})"
        )
    if not 1 <= len(response.content) <= 48 * 1024:
        raise ReleaseError("public stable pointer has an unsafe size")
    _write_new(args.output.resolve(), response.content)
    print(args.output.resolve())
    return 0


class R2Client:
    """Conditional path-style R2 writes signed by the pinned botocore library."""

    def __init__(
        self,
        *,
        account_id: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        session_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", account_id):
            raise ReleaseError("unsafe R2 account ID")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
            raise ReleaseError("unsafe R2 bucket name")
        if not access_key_id or not secret_access_key:
            raise ReleaseError("R2 credentials are unavailable")
        self.account_id = account_id
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.client = httpx.Client(
            follow_redirects=False,
            timeout=60,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if (
            not key
            or key.startswith("/")
            or ".." in key.split("/")
            or not all(part for part in key.split("/"))
        ):
            raise ReleaseError("unsafe R2 object key")
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        path = "/" + quote(self.bucket, safe="-_.~") + "/" + quote(
            key,
            safe="/-_.~",
        )
        payload_digest = hashlib.sha256(body).hexdigest()
        request_headers = {
            "host": host,
            "x-amz-content-sha256": payload_digest,
        }
        for name, value in (headers or {}).items():
            lowered = name.lower()
            if lowered in request_headers:
                raise ReleaseError(f"duplicate signed header: {lowered}")
            request_headers[lowered] = " ".join(value.strip().split())
        try:
            from botocore.auth import S3SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials
        except ImportError as error:  # pragma: no cover - locked release environment
            raise ReleaseError("the locked botocore release dependency is unavailable") from error
        url = f"https://{host}{path}"
        aws_request = AWSRequest(
            method=method,
            url=url,
            data=body,
            headers=request_headers,
        )
        S3SigV4Auth(
            Credentials(
                self.access_key_id,
                self.secret_access_key,
                self.session_token,
            ),
            "s3",
            "auto",
        ).add_auth(aws_request)
        signed_headers = dict(aws_request.headers.items())
        if not signed_headers.get("Authorization"):
            raise ReleaseError("botocore did not sign the R2 request")
        return self.client.request(
            method,
            url,
            content=body,
            headers=signed_headers,
        )

    def get(self, key: str) -> httpx.Response:
        return self._request("GET", key)

    def put_new(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        cache_control: str,
    ) -> httpx.Response:
        return self._request(
            "PUT",
            key,
            body=body,
            headers={
                "content-type": content_type,
                "cache-control": cache_control,
                "if-none-match": "*",
            },
        )

    def put_if_match(
        self,
        key: str,
        body: bytes,
        *,
        etag: str,
    ) -> httpx.Response:
        return self._request(
            "PUT",
            key,
            body=body,
            headers={
                "content-type": "application/json",
                "cache-control": "no-store,max-age=0",
                "if-match": etag,
            },
        )


def _expected_public_base(plan: dict[str, Any]) -> str:
    first = plan["artifacts"][0]
    suffix = "/" + first["object_key"]
    if not first["url"].endswith(suffix):
        raise ReleaseError("release plan has no stable public base URL")
    return first["url"][: -len(suffix)]


def _verify_response(response: httpx.Response, expected: bytes, name: str) -> None:
    if response.status_code != 200:
        raise ReleaseError(f"{name} readback failed (HTTP {response.status_code})")
    if response.headers.get("location"):
        raise ReleaseError(f"{name} unexpectedly redirects")
    if response.content != expected:
        raise ReleaseError(f"{name} readback digest differs")


def _strong_etag(value: str) -> str | None:
    match = ETAG.fullmatch(value)
    return match.group(1) if match else None


def _etag_matches(left: str, right: str) -> bool:
    """Compare the same opaque ETag across strong/weak R2 readback forms.

    Exact response bytes are verified separately before this comparison, so a
    weak marker can safely be ignored while every opaque tag byte remains
    significant.
    """

    left_strong = _strong_etag(left)
    right_strong = _strong_etag(right)
    return left_strong is not None and left_strong == right_strong


def _public_readback(base_url: str, key: str, expected: bytes) -> httpx.Response:
    response = httpx.get(
        f"{base_url.rstrip('/')}/{key}",
        follow_redirects=False,
        timeout=60,
        headers={"cache-control": "no-cache"},
    )
    _verify_response(response, expected, f"public object {key}")
    return response


def publish_r2(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan.resolve())
    policy = _policy(args.policy.resolve())
    _validate_plan(plan, policy)
    if plan["source"]["commit"] != args.expected_commit:
        raise ReleaseError("release plan source commit differs from the workflow commit")
    if plan["source"]["tree"] != args.expected_tree:
        raise ReleaseError("release plan source tree differs from the workflow tree")
    public_base = _expected_public_base(plan)
    if public_base != args.public_base_url.rstrip("/"):
        raise ReleaseError("release plan public base URL differs from protected environment")
    host = urlparse(public_base).hostname or ""
    if host.endswith(".r2.dev"):
        raise ReleaseError("r2.dev is not an approved production origin")
    bundle = args.bundle.resolve()
    manifest_path = args.manifest.resolve()
    registry_path = args.registry.resolve()
    audit_path = args.audit.resolve()
    checksums_path = args.checksums.resolve()
    manifest_bytes = _read_regular_bytes(manifest_path, "signed manifest")
    previous_bytes = (
        _read_regular_bytes(args.previous_manifest.resolve(), "previous manifest")
        if args.previous_manifest
        else None
    )
    supplied_audit_bytes = _read_regular_bytes(audit_path, "release audit")
    supplied_checksums_bytes = _read_regular_bytes(checksums_path, "SHA256SUMS")
    trusted_keys, metadata, registry_bytes = _registry(registry_path)
    manifest = verify_signed_manifest(manifest_bytes, trusted_keys)
    unsigned = json.loads(manifest.signed_document)
    unsigned.pop("signature")
    if unsigned != _manifest_from_plan(plan):
        raise ReleaseError("publication manifest differs from its reviewed plan")
    previous_digest = _verify_chain(
        manifest,
        previous_bytes=previous_bytes,
        bootstrap=args.bootstrap,
        trusted_keys=trusted_keys,
    )
    artifact_snapshots: dict[str, bytes] = {}
    for artifact in manifest.artifacts:
        artifact_bytes = _read_regular_bytes(
            bundle / artifact.filename, "release artifact"
        )
        _verify_artifact_snapshot(artifact_bytes, artifact)
        artifact_snapshots[artifact.filename] = artifact_bytes
    publication_now = _utc_now()
    _validate_publication_key(
        key_id=manifest.key_id,
        published_at=manifest.published_at,
        metadata=metadata,
        now=publication_now,
    )
    _validate_manifest_publication_window(manifest, publication_now)
    audit = _expected_audit(
        plan=plan,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        previous_digest=previous_digest,
        trusted_keys=trusted_keys,
        metadata=metadata,
        registry_bytes=registry_bytes,
    )
    audit_bytes = _canonical(audit) + b"\n"
    checksums_bytes = _expected_checksums(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        registry_bytes=registry_bytes,
        audit_bytes=audit_bytes,
    )
    for supplied, expected, name in (
        (supplied_audit_bytes, audit_bytes, "release audit"),
        (supplied_checksums_bytes, checksums_bytes, "SHA256SUMS"),
    ):
        if supplied != expected:
            raise ReleaseError(f"{name} is not the unique canonical publication record")
    prefix = plan["release"]["object_prefix"]
    immutable: list[tuple[str, bytes, str]] = []
    for item in plan["artifacts"]:
        artifact_bytes = artifact_snapshots[item["filename"]]
        immutable.append(
            (item["object_key"], artifact_bytes, "application/octet-stream")
        )
    immutable.extend(
        [
            (f"{prefix}/trusted-keys.json", registry_bytes, "application/json"),
            (f"{prefix}/release-audit.json", audit_bytes, "application/json"),
            (f"{prefix}/SHA256SUMS", checksums_bytes, "text/plain"),
            (f"{prefix}/manifest.json", manifest_bytes, "application/json"),
        ]
    )
    access_key = os.environ.pop(args.access_key_env, None)
    secret_key = os.environ.pop(args.secret_key_env, None)
    session_token = (
        os.environ.pop(args.session_token_env, None) if args.session_token_env else None
    )
    client = R2Client(
        account_id=args.account_id,
        bucket=args.bucket,
        access_key_id=access_key or "",
        secret_access_key=secret_key or "",
        session_token=session_token,
    )
    pointer_etag: str | None = None
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    current_key = "channels/stable/current.json"
    public_pointer_url = f"{public_base}/{current_key}"
    try:
        current = client.get(current_key)
        previous_etag: str | None = None
        if args.bootstrap:
            if current.status_code != 404:
                raise ReleaseError("bootstrap requires an absent stable pointer")
        else:
            if previous_bytes is None:
                raise ReleaseError("non-bootstrap publication requires previous manifest")
            _verify_response(current, previous_bytes, "current stable pointer")
            raw_previous_etag = current.headers.get("etag")
            previous_etag = (
                _strong_etag(raw_previous_etag) if raw_previous_etag else None
            )
            if not previous_etag:
                raise ReleaseError("current stable pointer has no ETag for CAS")
        for key, body, content_type in immutable:
            response = client.put_new(
                key,
                body,
                content_type=content_type,
                cache_control="public,max-age=31536000,immutable",
            )
            if response.status_code not in {200, 201}:
                raise ReleaseError(
                    f"immutable upload rejected for {key} (HTTP {response.status_code})"
                )
            _verify_response(client.get(key), body, f"R2 object {key}")
            _public_readback(public_base, key, body)
        pointer_commit_now = _utc_now()
        _validate_publication_key(
            key_id=manifest.key_id,
            published_at=manifest.published_at,
            metadata=metadata,
            now=pointer_commit_now,
        )
        _validate_manifest_publication_window(manifest, pointer_commit_now)
        if args.bootstrap:
            pointer_response = client.put_new(
                current_key,
                manifest_bytes,
                content_type="application/json",
                cache_control="no-store,max-age=0",
            )
        else:
            assert previous_etag is not None
            pointer_response = client.put_if_match(
                current_key,
                manifest_bytes,
                etag=previous_etag,
            )
        if pointer_response.status_code not in {200, 201}:
            raise ReleaseError(
                "stable pointer CAS failed; immutable release remains undiscoverable "
                f"(HTTP {pointer_response.status_code})"
            )
        pointer_etag = pointer_response.headers.get("etag")
        try:
            if not pointer_etag:
                raise ReleaseError("successful pointer CAS response has no ETag")
            authenticated = client.get(current_key)
            _verify_response(authenticated, manifest_bytes, "stable pointer")
            authenticated_etag = authenticated.headers.get("etag")
            if not authenticated_etag or not _etag_matches(
                authenticated_etag, pointer_etag
            ):
                raise ReleaseError("authenticated stable pointer ETag differs from CAS")
            public = _public_readback(public_base, current_key, manifest_bytes)
            public_etag = public.headers.get("etag")
            if public_etag is not None and not _etag_matches(public_etag, pointer_etag):
                raise ReleaseError("public stable pointer ETag differs from CAS")
        except Exception as error:
            raise CommittedButUnverified(
                release_id=manifest.release_id,
                sequence=manifest.sequence,
                bucket=args.bucket,
                pointer=current_key,
                public_url=public_pointer_url,
                expected_etag=pointer_etag,
                expected_sha256=manifest_digest,
                expected_size=len(manifest_bytes),
                reason=str(error),
            ) from error
    finally:
        with suppress(Exception):
            client.close()
    print(
        json.dumps(
            {
                "published": True,
                "status": "committed_and_verified",
                "release_id": plan["release"]["release_id"],
                "sequence": plan["release"]["sequence"],
                "object_prefix": prefix,
                "pointer": current_key,
                "pointer_etag": pointer_etag,
                "pointer_sha256": manifest_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--policy",
        type=Path,
        default=Path("release/ota/policy.json"),
    )

    build_parser = sub.add_parser("build", parents=[common])
    build_parser.add_argument("--source-root", type=Path, default=Path("."))
    build_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser.add_argument("--sequence", type=int, required=True)
    build_parser.add_argument("--source-commit", required=True)
    build_parser.add_argument("--source-tree", required=True)
    build_parser.add_argument("--base-url", required=True)
    build_parser.add_argument("--published-at", required=True)
    build_parser.add_argument("--expires-at", required=True)
    build_parser.add_argument(
        "--rollout-stage",
        choices=sorted(ROLLOUT_STAGES),
        default="internal",
    )
    build_parser.add_argument("--basis-points", type=int, default=0)
    build_parser.add_argument("--rollout-salt", required=True)
    build_parser.add_argument("--paused", action="store_true")
    build_parser.set_defaults(func=build)

    sign_parser = sub.add_parser("sign", parents=[common])
    sign_parser.add_argument("--plan", type=Path, required=True)
    sign_parser.add_argument("--registry", type=Path, required=True)
    sign_parser.add_argument("--key-id", required=True)
    sign_parser.add_argument("--private-key-env")
    sign_parser.add_argument("--private-key-file", type=Path)
    sign_parser.add_argument("--previous-manifest", type=Path)
    sign_parser.add_argument("--bootstrap", action="store_true")
    sign_parser.add_argument("--output", type=Path, required=True)
    sign_parser.set_defaults(func=sign)

    verify_parser = sub.add_parser("verify", parents=[common])
    verify_parser.add_argument("--plan", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--registry", type=Path, required=True)
    verify_parser.add_argument("--artifact-dir", type=Path, required=True)
    verify_parser.add_argument("--previous-manifest", type=Path)
    verify_parser.add_argument("--bootstrap", action="store_true")
    verify_parser.add_argument("--audit-output", type=Path, required=True)
    verify_parser.add_argument("--checksums-output", type=Path, required=True)
    verify_parser.set_defaults(func=verify)

    preflight_parser = sub.add_parser("cloudflare-preflight")
    preflight_parser.add_argument("--account-id", required=True)
    preflight_parser.add_argument("--bucket", required=True)
    preflight_parser.add_argument("--public-base-url", required=True)
    preflight_parser.add_argument(
        "--api-token-env",
        default="DRADAR_OTA_CLOUDFLARE_API_TOKEN",
    )
    preflight_parser.set_defaults(func=cloudflare_preflight)

    fetch_parser = sub.add_parser("fetch-current")
    fetch_parser.add_argument("--url", required=True)
    fetch_parser.add_argument("--output", type=Path)
    fetch_parser.add_argument("--expect-absent", action="store_true")
    fetch_parser.set_defaults(func=fetch_current)

    publish_parser = sub.add_parser("publish-r2", parents=[common])
    publish_parser.add_argument("--plan", type=Path, required=True)
    publish_parser.add_argument("--bundle", type=Path, required=True)
    publish_parser.add_argument("--manifest", type=Path, required=True)
    publish_parser.add_argument("--registry", type=Path, required=True)
    publish_parser.add_argument("--audit", type=Path, required=True)
    publish_parser.add_argument("--checksums", type=Path, required=True)
    publish_parser.add_argument("--previous-manifest", type=Path)
    publish_parser.add_argument("--bootstrap", action="store_true")
    publish_parser.add_argument("--account-id", required=True)
    publish_parser.add_argument("--bucket", required=True)
    publish_parser.add_argument("--public-base-url", required=True)
    publish_parser.add_argument("--expected-commit", required=True)
    publish_parser.add_argument("--expected-tree", required=True)
    publish_parser.add_argument(
        "--access-key-env",
        default="DRADAR_OTA_R2_ACCESS_KEY_ID",
    )
    publish_parser.add_argument(
        "--secret-key-env",
        default="DRADAR_OTA_R2_SECRET_ACCESS_KEY",
    )
    publish_parser.add_argument("--session-token-env")
    publish_parser.set_defaults(func=publish_r2)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.func(args)
    except CommittedButUnverified as exc:
        print(
            json.dumps(exc.report, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
        return 3
    except (
        ManifestError,
        OSError,
        ReleaseError,
        KeyError,
        TypeError,
        httpx.HTTPError,
    ) as exc:
        print(f"ota-release: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
