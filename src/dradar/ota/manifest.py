"""Signed release manifests, target selection and fail-closed rollout policy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PLATFORMS = frozenset({"macos", "linux", "windows"})
_ARCHITECTURES = frozenset({"x86_64", "arm64"})
_ROLLOUT_STAGES = frozenset({"internal", "canary", "progressive", "general"})
_RINGS = {"internal": 0, "canary": 1, "general": 2}


class ManifestError(ValueError):
    """A manifest, signature, artifact, or compatibility claim is unsafe."""


@dataclass(frozen=True)
class PlatformTarget:
    os: str
    arch: str

    @classmethod
    def current(cls) -> "PlatformTarget":
        system = platform.system().lower()
        os_name = {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(
            system,
        )
        machine = platform.machine().lower()
        arch = {
            "amd64": "x86_64",
            "x86_64": "x86_64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(machine)
        if os_name is None or arch is None:
            raise ManifestError(f"unsupported OTA target: {system}/{machine}")
        return cls(os_name, arch)


@dataclass(frozen=True)
class Artifact:
    target: PlatformTarget
    filename: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CompatibilitySnapshot:
    launcher_version: str
    runner_protocol: int
    doctor_contract: int
    provider_contract: int
    ledger_schema: int
    checkpoint_schema: int


@dataclass(frozen=True)
class RolloutContext:
    """Stable identity is injected by #0011; OTA never creates a second ID."""

    subject: str
    ring: str = "general"
    channel: str = "stable"


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    release_id: str
    version: str
    sequence: int
    channel: str
    published_at: str
    rollout_stage: str
    rollout_basis_points: int
    rollout_salt: str
    rollout_paused: bool
    launcher_min_version: str
    runner_protocol_min: int
    runner_protocol_max: int
    doctor_contract: int
    provider_contract: int
    ledger_schema_min: int
    ledger_schema_max: int
    checkpoint_schema_min: int
    checkpoint_schema_max: int
    artifacts: tuple[Artifact, ...]
    key_id: str
    signed_payload: bytes


@dataclass(frozen=True)
class PolicyDecision:
    eligible: bool
    reason: str
    artifact: Artifact | None = None


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ManifestError(f"{name} must be a bounded string")
    return value


def _integer(
    value: Any, name: str, *, minimum: int = 0, maximum: int = 2**31 - 1
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ManifestError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _identifier(value: Any, name: str) -> str:
    result = _string(value, name, maximum=128)
    if not _IDENTIFIER.fullmatch(result):
        raise ManifestError(f"{name} contains unsafe characters")
    return result


def _version(value: Any, name: str) -> str:
    result = _string(value, name, maximum=32)
    if not _SEMVER.fullmatch(result):
        raise ManifestError(f"{name} must be a stable semantic version")
    return result


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if not match:
        raise ManifestError(f"unsafe semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _canonical_payload(document: Mapping[str, Any]) -> bytes:
    unsigned = dict(document)
    unsigned.pop("signature", None)
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError("manifest cannot be canonically encoded") from exc


def _decode_key(value: bytes | str, name: str) -> bytes:
    if isinstance(value, str):
        try:
            value = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise ManifestError(f"{name} is not valid base64") from exc
    if not isinstance(value, bytes) or len(value) != 32:
        raise ManifestError(f"{name} must contain one raw Ed25519 public key")
    return value


def verify_signed_manifest(
    raw: bytes | str | Mapping[str, Any],
    trusted_keys: Mapping[str, bytes | str],
) -> ReleaseManifest:
    """Verify the Ed25519 signature before trusting any release policy field."""

    if isinstance(raw, (bytes, str)):
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError("manifest is not valid JSON") from exc
    else:
        document = raw
    document = _mapping(document, "manifest")
    signature = _mapping(document.get("signature"), "signature")
    if signature.get("algorithm") != "ed25519":
        raise ManifestError("only Ed25519 release signatures are accepted")
    key_id = _identifier(signature.get("key_id"), "signature.key_id")
    trusted = trusted_keys.get(key_id)
    if trusted is None:
        raise ManifestError("manifest signing key is not trusted")
    encoded_signature = _string(signature.get("value"), "signature.value", maximum=128)
    try:
        signature_bytes = base64.b64decode(encoded_signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise ManifestError("manifest signature is not valid base64") from exc
    if len(signature_bytes) != 64:
        raise ManifestError("manifest signature has the wrong length")
    payload = _canonical_payload(document)
    try:
        Ed25519PublicKey.from_public_bytes(
            _decode_key(trusted, f"trusted key {key_id}"),
        ).verify(signature_bytes, payload)
    except (InvalidSignature, ValueError) as exc:
        raise ManifestError("manifest signature verification failed") from exc

    schema_version = _integer(
        document.get("schema_version"), "schema_version", minimum=1, maximum=1
    )
    release_id = _identifier(document.get("release_id"), "release_id")
    version = _version(document.get("version"), "version")
    sequence = _integer(document.get("sequence"), "sequence", minimum=1)
    channel = _identifier(document.get("channel"), "channel")
    published_at = _string(document.get("published_at"), "published_at", maximum=64)

    rollout = _mapping(document.get("rollout"), "rollout")
    rollout_stage = _string(rollout.get("stage"), "rollout.stage", maximum=32)
    if rollout_stage not in _ROLLOUT_STAGES:
        raise ManifestError("unsupported rollout stage")
    rollout_basis_points = _integer(
        rollout.get("basis_points"),
        "rollout.basis_points",
        maximum=10_000,
    )
    rollout_salt = _string(rollout.get("salt"), "rollout.salt", maximum=128)
    rollout_paused = rollout.get("paused")
    if not isinstance(rollout_paused, bool):
        raise ManifestError("rollout.paused must be boolean")

    compatibility = _mapping(document.get("compatibility"), "compatibility")
    launcher_min_version = _version(
        compatibility.get("launcher_min_version"),
        "compatibility.launcher_min_version",
    )
    runner_protocol = _mapping(
        compatibility.get("runner_protocol"),
        "compatibility.runner_protocol",
    )
    ledger_schema = _mapping(
        compatibility.get("ledger_schema"),
        "compatibility.ledger_schema",
    )
    checkpoint_schema = _mapping(
        compatibility.get("checkpoint_schema"),
        "compatibility.checkpoint_schema",
    )

    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= 12:
        raise ManifestError("artifacts must contain 1..12 platform artifacts")
    artifacts: list[Artifact] = []
    seen_targets: set[PlatformTarget] = set()
    for index, item in enumerate(raw_artifacts):
        item = _mapping(item, f"artifacts[{index}]")
        os_name = _string(item.get("os"), f"artifacts[{index}].os", maximum=16)
        arch = _string(item.get("arch"), f"artifacts[{index}].arch", maximum=16)
        if os_name not in _PLATFORMS or arch not in _ARCHITECTURES:
            raise ManifestError(f"artifacts[{index}] has an unsupported target")
        target = PlatformTarget(os_name, arch)
        if target in seen_targets:
            raise ManifestError("manifest contains duplicate platform artifacts")
        seen_targets.add(target)
        filename = _identifier(item.get("filename"), f"artifacts[{index}].filename")
        url = _string(item.get("url"), f"artifacts[{index}].url", maximum=2048)
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username:
            raise ManifestError("artifact URLs must use credential-free HTTPS")
        sha256 = _string(item.get("sha256"), f"artifacts[{index}].sha256", maximum=64)
        if not _HEX_SHA256.fullmatch(sha256):
            raise ManifestError("artifact SHA-256 must be lowercase hexadecimal")
        artifacts.append(
            Artifact(
                target=target,
                filename=filename,
                url=url,
                size=_integer(item.get("size"), f"artifacts[{index}].size", minimum=1),
                sha256=sha256,
            )
        )

    runner_min = _integer(runner_protocol.get("min"), "runner_protocol.min")
    runner_max = _integer(runner_protocol.get("max"), "runner_protocol.max")
    ledger_min = _integer(ledger_schema.get("min"), "ledger_schema.min")
    ledger_max = _integer(ledger_schema.get("max"), "ledger_schema.max")
    checkpoint_min = _integer(
        checkpoint_schema.get("min"),
        "checkpoint_schema.min",
    )
    checkpoint_max = _integer(
        checkpoint_schema.get("max"),
        "checkpoint_schema.max",
    )
    if (
        runner_min > runner_max
        or ledger_min > ledger_max
        or checkpoint_min > checkpoint_max
    ):
        raise ManifestError("compatibility range minimum exceeds maximum")

    return ReleaseManifest(
        schema_version=schema_version,
        release_id=release_id,
        version=version,
        sequence=sequence,
        channel=channel,
        published_at=published_at,
        rollout_stage=rollout_stage,
        rollout_basis_points=rollout_basis_points,
        rollout_salt=rollout_salt,
        rollout_paused=rollout_paused,
        launcher_min_version=launcher_min_version,
        runner_protocol_min=runner_min,
        runner_protocol_max=runner_max,
        doctor_contract=_integer(
            compatibility.get("doctor_contract"), "doctor_contract"
        ),
        provider_contract=_integer(
            compatibility.get("provider_contract"), "provider_contract"
        ),
        ledger_schema_min=ledger_min,
        ledger_schema_max=ledger_max,
        checkpoint_schema_min=checkpoint_min,
        checkpoint_schema_max=checkpoint_max,
        artifacts=tuple(artifacts),
        key_id=key_id,
        signed_payload=payload,
    )


def _cohort_basis_points(subject: str, salt: str) -> int:
    digest = hmac.new(
        salt.encode("utf-8"),
        subject.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def evaluate_manifest(
    manifest: ReleaseManifest,
    *,
    current_version: str,
    committed_sequence: int,
    compatibility: CompatibilitySnapshot,
    rollout: RolloutContext,
    target: PlatformTarget | None = None,
) -> PolicyDecision:
    """Return a stable, explainable decision without downloading anything."""

    if manifest.rollout_paused:
        return PolicyDecision(False, "rollout_paused")
    if manifest.channel != rollout.channel:
        return PolicyDecision(False, "channel_mismatch")
    if rollout.ring not in _RINGS:
        return PolicyDecision(False, "unknown_rollout_ring")
    required_ring = {
        "internal": "internal",
        "canary": "canary",
        "progressive": "general",
        "general": "general",
    }[manifest.rollout_stage]
    if _RINGS[rollout.ring] > _RINGS[required_ring]:
        return PolicyDecision(False, "rollout_ring_not_eligible")
    if not rollout.subject or len(rollout.subject) > 256:
        return PolicyDecision(False, "rollout_subject_unavailable")
    if manifest.rollout_basis_points < 10_000 and (
        _cohort_basis_points(rollout.subject, manifest.rollout_salt)
        >= manifest.rollout_basis_points
    ):
        return PolicyDecision(False, "outside_rollout_cohort")
    if manifest.sequence <= committed_sequence:
        return PolicyDecision(False, "anti_rollback_sequence")
    if _version_tuple(manifest.version) <= _version_tuple(current_version):
        return PolicyDecision(False, "version_not_newer")
    if _version_tuple(compatibility.launcher_version) < _version_tuple(
        manifest.launcher_min_version,
    ):
        return PolicyDecision(False, "launcher_too_old")
    if (
        not manifest.runner_protocol_min
        <= compatibility.runner_protocol
        <= manifest.runner_protocol_max
    ):
        return PolicyDecision(False, "runner_protocol_incompatible")
    if compatibility.doctor_contract != manifest.doctor_contract:
        return PolicyDecision(False, "doctor_contract_incompatible")
    if compatibility.provider_contract != manifest.provider_contract:
        return PolicyDecision(False, "provider_contract_incompatible")
    if (
        not manifest.ledger_schema_min
        <= compatibility.ledger_schema
        <= manifest.ledger_schema_max
    ):
        return PolicyDecision(False, "ledger_schema_incompatible")
    if (
        not manifest.checkpoint_schema_min
        <= compatibility.checkpoint_schema
        <= manifest.checkpoint_schema_max
    ):
        return PolicyDecision(False, "checkpoint_schema_incompatible")
    target = target or PlatformTarget.current()
    artifact = next(
        (item for item in manifest.artifacts if item.target == target), None
    )
    if artifact is None:
        return PolicyDecision(False, "platform_artifact_unavailable")
    return PolicyDecision(True, "eligible", artifact)


def verify_artifact(path: Path, artifact: Artifact) -> None:
    """Verify exact size and SHA-256 using constant-time digest comparison."""

    try:
        stat = path.stat()
    except OSError as exc:
        raise ManifestError("downloaded artifact is unavailable") from exc
    if not path.is_file() or path.is_symlink() or stat.st_size != artifact.size:
        raise ManifestError("downloaded artifact size or file type is invalid")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError("downloaded artifact cannot be read") from exc
    if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
        raise ManifestError("downloaded artifact SHA-256 mismatch")
