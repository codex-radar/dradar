import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dradar.ota.manifest import (
    CompatibilitySnapshot,
    ManifestError,
    PlatformTarget,
    RolloutContext,
    evaluate_manifest,
    verify_artifact,
    verify_signed_manifest,
)


def _signed_document(**changes):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    artifact_body = b"signed DRadar candidate"
    document = {
        "schema_version": 1,
        "release_id": "dradar-cli-0.6.0-a1b2c3d4",
        "version": "0.6.0",
        "sequence": 600,
        "channel": "stable",
        "published_at": "2026-09-02T04:30:00Z",
        "rollout": {
            "stage": "progressive",
            "basis_points": 10_000,
            "salt": "release-600",
            "paused": False,
        },
        "compatibility": {
            "launcher_min_version": "1.0.0",
            "runner_protocol": {"min": 3, "max": 4},
            "doctor_contract": 1,
            "provider_contract": 2,
            "ledger_schema": {"min": 1, "max": 2},
            "checkpoint_schema": {"min": 0, "max": 0},
        },
        "artifacts": [
            {
                "os": "linux",
                "arch": "x86_64",
                "filename": "dradar-0.6.0-linux-x86_64.whl",
                "url": "https://releases.example.invalid/dradar-0.6.0-linux-x86_64.whl",
                "size": len(artifact_body),
                "sha256": hashlib.sha256(artifact_body).hexdigest(),
            }
        ],
    }
    for key, value in changes.items():
        if key.startswith("rollout_"):
            document["rollout"][key.removeprefix("rollout_")] = value
        else:
            document[key] = value
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": "release-root-2026",
        "value": base64.b64encode(private.sign(payload)).decode(),
    }
    return document, {"release-root-2026": public}, artifact_body


def _compatibility(**changes):
    values = {
        "launcher_version": "1.0.0",
        "runner_protocol": 3,
        "doctor_contract": 1,
        "provider_contract": 2,
        "ledger_schema": 1,
        "checkpoint_schema": 0,
    }
    return CompatibilitySnapshot(**{**values, **changes})


def test_signed_manifest_selects_exact_platform_and_compatibility():
    document, keys, _body = _signed_document()
    manifest = verify_signed_manifest(document, keys)

    decision = evaluate_manifest(
        manifest,
        current_version="0.5.175",
        committed_sequence=599,
        compatibility=_compatibility(),
        rollout=RolloutContext(subject="client-instance-from-0011"),
        target=PlatformTarget("linux", "x86_64"),
    )

    assert decision.eligible is True
    assert decision.artifact.filename == "dradar-0.6.0-linux-x86_64.whl"


def test_manifest_tampering_fails_before_policy_is_trusted():
    document, keys, _body = _signed_document()
    document["rollout"]["basis_points"] = 10_000
    document["sequence"] = 601

    with pytest.raises(ManifestError, match="signature verification failed"):
        verify_signed_manifest(document, keys)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"committed_sequence": 600}, "anti_rollback_sequence"),
        ({"current_version": "0.6.0"}, "version_not_newer"),
        (
            {"compatibility": _compatibility(runner_protocol=2)},
            "runner_protocol_incompatible",
        ),
        (
            {"compatibility": _compatibility(provider_contract=1)},
            "provider_contract_incompatible",
        ),
        (
            {"target": PlatformTarget("windows", "arm64")},
            "platform_artifact_unavailable",
        ),
    ],
)
def test_policy_fails_closed_with_explainable_reason(changes, reason):
    document, keys, _body = _signed_document()
    manifest = verify_signed_manifest(document, keys)
    arguments = {
        "current_version": "0.5.175",
        "committed_sequence": 599,
        "compatibility": _compatibility(),
        "rollout": RolloutContext(subject="stable-subject"),
        "target": PlatformTarget("linux", "x86_64"),
    }
    arguments.update(changes)

    assert evaluate_manifest(manifest, **arguments).reason == reason


def test_rollout_pause_and_ring_gate_do_not_download():
    paused_document, keys, _body = _signed_document(rollout_paused=True)
    paused = verify_signed_manifest(paused_document, keys)
    assert (
        evaluate_manifest(
            paused,
            current_version="0.5.175",
            committed_sequence=599,
            compatibility=_compatibility(),
            rollout=RolloutContext(subject="one"),
            target=PlatformTarget("linux", "x86_64"),
        ).reason
        == "rollout_paused"
    )

    canary_document, canary_keys, _body = _signed_document(rollout_stage="canary")
    canary = verify_signed_manifest(canary_document, canary_keys)
    assert (
        evaluate_manifest(
            canary,
            current_version="0.5.175",
            committed_sequence=599,
            compatibility=_compatibility(),
            rollout=RolloutContext(subject="two", ring="general"),
            target=PlatformTarget("linux", "x86_64"),
        ).reason
        == "rollout_ring_not_eligible"
    )


def test_artifact_digest_and_size_are_both_enforced(tmp_path):
    document, keys, body = _signed_document()
    artifact = verify_signed_manifest(document, keys).artifacts[0]
    path = tmp_path / artifact.filename
    path.write_bytes(body)
    verify_artifact(path, artifact)

    path.write_bytes(b"x" * len(body))
    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_artifact(path, artifact)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "x86_64", PlatformTarget("macos", "x86_64")),
        ("Darwin", "arm64", PlatformTarget("macos", "arm64")),
        ("Linux", "AMD64", PlatformTarget("linux", "x86_64")),
        ("Linux", "aarch64", PlatformTarget("linux", "arm64")),
        ("Windows", "x86_64", PlatformTarget("windows", "x86_64")),
        ("Windows", "ARM64", PlatformTarget("windows", "arm64")),
    ],
)
def test_platform_target_normalizes_supported_os_arch_pairs(
    monkeypatch, system, machine, expected
):
    monkeypatch.setattr("dradar.ota.manifest.platform.system", lambda: system)
    monkeypatch.setattr("dradar.ota.manifest.platform.machine", lambda: machine)
    assert PlatformTarget.current() == expected


def test_zero_percent_progressive_rollout_is_deterministically_ineligible():
    document, keys, _body = _signed_document(rollout_basis_points=0)
    manifest = verify_signed_manifest(document, keys)
    decisions = [
        evaluate_manifest(
            manifest,
            current_version="0.5.175",
            committed_sequence=599,
            compatibility=_compatibility(),
            rollout=RolloutContext(subject="same-client-instance"),
            target=PlatformTarget("linux", "x86_64"),
        ).reason
        for _ in range(3)
    ]
    assert decisions == ["outside_rollout_cohort"] * 3
