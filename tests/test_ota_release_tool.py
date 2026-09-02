import base64
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).parents[1] / "scripts" / "ota_release.py"
POLICY = Path(__file__).parents[1] / "release" / "ota" / "policy.json"
SPEC = importlib.util.spec_from_file_location("ota_release_tool", SCRIPT)
assert SPEC and SPEC.loader
ota_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ota_release)


def _source(tmp_path: Path, version: str = "0.5.177") -> Path:
    root = tmp_path / "source"
    package = root / "src" / "dradar"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "dradar"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (package / "cli.py").write_text(
        "from . import __version__\n"
        "def main():\n"
        "    print(__version__)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return root


def _key_material(tmp_path: Path, *, status: str = "active"):
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    private_path = tmp_path / "test-signing.key"
    private_path.write_bytes(seed)
    private_path.chmod(0o600)
    registry = tmp_path / "trusted-keys.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": {
                    "test-release-2026": {
                        "public_key_base64": base64.b64encode(public).decode(),
                        "status": status,
                        "not_before": "2026-01-01T00:00:00Z",
                        "not_after": "2027-01-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return private, private_path, registry


def _build(
    tmp_path: Path,
    *,
    sequence: int = 1,
    version: str = "0.5.177",
    destination: str = "bundle",
) -> Path:
    output = tmp_path / destination
    assert (
        ota_release.main(
            [
                "build",
                "--policy",
                str(POLICY),
                "--source-root",
                str(_source(tmp_path / destination, version)),
                "--output-dir",
                str(output),
                "--sequence",
                str(sequence),
                "--source-commit",
                "a" * 40,
                "--source-tree",
                "b" * 40,
                "--base-url",
                "https://updates.example.invalid",
                "--published-at",
                "2026-09-02T00:00:00Z",
                "--expires-at",
                "2026-10-01T00:00:00Z",
                "--rollout-stage",
                "internal",
                "--basis-points",
                "100",
                "--rollout-salt",
                f"test-sequence-{sequence}",
            ]
        )
        == 0
    )
    return output


def _sign(
    bundle: Path,
    private_path: Path,
    registry: Path,
    *,
    previous: Path | None = None,
    bootstrap: bool = True,
) -> int:
    arguments = [
        "sign",
        "--policy",
        str(POLICY),
        "--plan",
        str(bundle / "release-plan.json"),
        "--registry",
        str(registry),
        "--key-id",
        "test-release-2026",
        "--private-key-file",
        str(private_path),
        "--output",
        str(bundle / "manifest.json"),
    ]
    if bootstrap:
        arguments.append("--bootstrap")
    if previous:
        arguments.extend(["--previous-manifest", str(previous)])
    return ota_release.main(arguments)


def _verify(bundle: Path, registry: Path, *, previous=None, bootstrap=True) -> int:
    arguments = [
        "verify",
        "--policy",
        str(POLICY),
        "--plan",
        str(bundle / "release-plan.json"),
        "--manifest",
        str(bundle / "manifest.json"),
        "--registry",
        str(registry),
        "--artifact-dir",
        str(bundle),
        "--audit-output",
        str(bundle / "release-audit.json"),
        "--checksums-output",
        str(bundle / "SHA256SUMS"),
    ]
    if bootstrap:
        arguments.append("--bootstrap")
    if previous:
        arguments.extend(["--previous-manifest", str(previous)])
    return ota_release.main(arguments)


def _signed_previous(private: Ed25519PrivateKey, path: Path, **changes) -> Path:
    body = b"previous"
    document = {
        "schema_version": 1,
        "release_id": "dradar-cli-0.5.176-s0000000001-previous",
        "version": "0.5.176",
        "sequence": 1,
        "channel": "stable",
        "published_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-09-15T00:00:00Z",
        "rollout": {
            "stage": "general",
            "basis_points": 10_000,
            "salt": "previous",
            "paused": False,
        },
        "compatibility": json.loads(POLICY.read_text())["compatibility"],
        "artifacts": [
            {
                "os": "linux",
                "arch": "x86_64",
                "filename": "previous.pyz",
                "url": "https://updates.example.invalid/previous.pyz",
                "size": len(body),
                "sha256": __import__("hashlib").sha256(body).hexdigest(),
            }
        ],
    }
    document.update(changes)
    document["signature"] = {
        "algorithm": "ed25519",
        "key_id": "test-release-2026",
        "value": base64.b64encode(
            private.sign(ota_release._canonical(document))
        ).decode(),
    }
    path.write_bytes(ota_release._canonical(document) + b"\n")
    return path


def test_build_sign_verify_bootstrap_bundle_with_temporary_key(tmp_path, capsys):
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)

    assert _sign(bundle, private_path, registry) == 0
    assert (
        ota_release.main(
            [
                "verify",
                "--policy",
                str(POLICY),
                "--plan",
                str(bundle / "release-plan.json"),
                "--manifest",
                str(bundle / "manifest.json"),
                "--registry",
                str(registry),
                "--artifact-dir",
                str(bundle),
                "--bootstrap",
                "--audit-output",
                str(bundle / "release-audit.json"),
                "--checksums-output",
                str(bundle / "SHA256SUMS"),
            ]
        )
        == 0
    )

    plan = json.loads((bundle / "release-plan.json").read_text())
    assert len(plan["artifacts"]) == 6
    assert len({item["filename"] for item in plan["artifacts"]}) == 6
    audit = json.loads((bundle / "release-audit.json").read_text())
    assert audit["sequence"] == 1
    assert audit["previous_manifest_sha256"] is None
    assert audit["signing_key"]["status"] == "active"
    assert len(audit["trusted_registry_sha256"]) == 64
    checksum_names = {
        line.partition("  ")[2]
        for line in (bundle / "SHA256SUMS").read_text().splitlines()
    }
    assert {"manifest.json", "trusted-keys.json", "release-audit.json"} <= checksum_names
    artifact = bundle / plan["artifacts"][0]["filename"]
    run = subprocess.run(
        [sys.executable, str(artifact), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "0.5.177"
    assert "test-signing.key" not in capsys.readouterr().out


def test_build_is_reproducible_per_target(tmp_path):
    first = _build(tmp_path, destination="first")
    second = _build(tmp_path, destination="second")
    first_plan = json.loads((first / "release-plan.json").read_text())
    second_plan = json.loads((second / "release-plan.json").read_text())
    assert [item["sha256"] for item in first_plan["artifacts"]] == [
        item["sha256"] for item in second_plan["artifacts"]
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("prefix", "outside the locked layout"),
        ("release_id", "not derived"),
        ("rollout", "invalid rollout cohort"),
        ("mixed_origin", "one public base"),
    ],
)
def test_tampered_release_plan_identity_fails_before_signing(
    tmp_path, capsys, mutation, message,
):
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    plan_path = bundle / "release-plan.json"
    plan = json.loads(plan_path.read_text())
    if mutation == "prefix":
        plan["release"]["object_prefix"] = "unlocked/stable/s0000000001/v0.5.177"
    elif mutation == "release_id":
        plan["release"]["release_id"] = "dradar-cli-forged"
    elif mutation == "rollout":
        plan["rollout"]["basis_points"] = 10_001
    else:
        plan["artifacts"][-1]["url"] = plan["artifacts"][-1]["url"].replace(
            "updates.example.invalid", "other.example.invalid"
        )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    assert _sign(bundle, private_path, registry) == 2
    assert not (bundle / "manifest.json").exists()
    assert message in capsys.readouterr().err


def test_non_bootstrap_requires_increasing_signed_chain(tmp_path):
    private, private_path, registry = _key_material(tmp_path)
    previous = _signed_previous(private, tmp_path / "previous.json")
    bundle = _build(tmp_path, sequence=2)

    assert (
        _sign(
            bundle,
            private_path,
            registry,
            previous=previous,
            bootstrap=False,
        )
        == 0
    )


def test_version_rollback_fails_closed_without_manifest_output(tmp_path, capsys):
    private, private_path, registry = _key_material(tmp_path)
    previous = _signed_previous(
        private,
        tmp_path / "previous.json",
        version="0.5.177",
    )
    bundle = _build(tmp_path, sequence=2)

    assert (
        _sign(
            bundle,
            private_path,
            registry,
            previous=previous,
            bootstrap=False,
        )
        == 2
    )
    assert not (bundle / "manifest.json").exists()
    assert "version did not increase" in capsys.readouterr().err


def test_next_key_cannot_sign_before_reviewed_activation(tmp_path, capsys):
    _private, private_path, registry = _key_material(tmp_path, status="next")
    bundle = _build(tmp_path)

    assert _sign(bundle, private_path, registry) == 2
    assert "not active" in capsys.readouterr().err


def test_signing_key_must_match_registry_and_stay_secret(tmp_path, capsys):
    _private, _private_path, registry = _key_material(tmp_path)
    other = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    encoded = base64.b64encode(other).decode()
    bundle = _build(tmp_path)
    env_name = "DRADAR_TEST_OTA_PRIVATE_KEY"
    try:
        __import__("os").environ[env_name] = encoded
        result = ota_release.main(
            [
                "sign",
                "--policy",
                str(POLICY),
                "--plan",
                str(bundle / "release-plan.json"),
                "--registry",
                str(registry),
                "--key-id",
                "test-release-2026",
                "--private-key-env",
                env_name,
                "--bootstrap",
                "--output",
                str(bundle / "manifest.json"),
            ]
        )
    finally:
        __import__("os").environ.pop(env_name, None)
    assert result == 2
    captured = capsys.readouterr()
    assert "does not match" in captured.err
    assert encoded not in captured.out + captured.err


def test_artifact_tampering_fails_before_audit_publication(tmp_path, capsys):
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    plan = json.loads((bundle / "release-plan.json").read_text())
    artifact = bundle / plan["artifacts"][0]["filename"]
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    result = ota_release.main(
        [
            "verify",
            "--policy",
            str(POLICY),
            "--plan",
            str(bundle / "release-plan.json"),
            "--manifest",
            str(bundle / "manifest.json"),
            "--registry",
            str(registry),
            "--artifact-dir",
            str(bundle),
            "--bootstrap",
            "--audit-output",
            str(bundle / "release-audit.json"),
            "--checksums-output",
            str(bundle / "SHA256SUMS"),
        ]
    )
    assert result == 2
    assert not (bundle / "release-audit.json").exists()
    assert "size" in capsys.readouterr().err


def test_private_key_file_permissions_fail_closed(tmp_path, capsys):
    _private, private_path, registry = _key_material(tmp_path)
    if __import__("os").name == "nt":
        pytest.skip("POSIX permission contract")
    private_path.chmod(0o644)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 2
    assert "permissions" in capsys.readouterr().err


class _FakeR2:
    state: ClassVar[dict[str, bytes]] = {}
    etags: ClassVar[dict[str, str]] = {}
    operations: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, **_kwargs):
        pass

    @staticmethod
    def _response(status, content=b"", headers=None):
        return __import__("httpx").Response(
            status,
            content=content,
            headers=headers,
            request=__import__("httpx").Request("GET", "https://r2.invalid"),
        )

    def close(self):
        pass

    def get(self, key):
        self.operations.append(("GET", key))
        if key not in self.state:
            return self._response(404)
        return self._response(
            200,
            self.state[key],
            {"etag": self.etags[key]},
        )

    def put_new(self, key, body, **_kwargs):
        self.operations.append(("PUT_NEW", key))
        if key in self.state:
            return self._response(412)
        self.state[key] = body
        self.etags[key] = f'"etag-{len(self.etags) + 1}"'
        return self._response(200, headers={"etag": self.etags[key]})

    def put_if_match(self, key, body, *, etag):
        self.operations.append(("PUT_CAS", key))
        if self.etags.get(key) != etag:
            return self._response(412)
        self.state[key] = body
        self.etags[key] = f'"etag-{len(self.etags) + 1}"'
        return self._response(200, headers={"etag": self.etags[key]})


def _publish_arguments(bundle, registry, *, previous=None, bootstrap=True):
    arguments = [
        "publish-r2",
        "--policy",
        str(POLICY),
        "--plan",
        str(bundle / "release-plan.json"),
        "--bundle",
        str(bundle),
        "--manifest",
        str(bundle / "manifest.json"),
        "--registry",
        str(registry),
        "--audit",
        str(bundle / "release-audit.json"),
        "--checksums",
        str(bundle / "SHA256SUMS"),
        "--account-id",
        "4d94f3bcb89bc16989d5ea715eaac061",
        "--bucket",
        "dradar-cli-ota-production",
        "--public-base-url",
        "https://updates.example.invalid",
        "--expected-commit",
        "a" * 40,
        "--expected-tree",
        "b" * 40,
    ]
    if bootstrap:
        arguments.append("--bootstrap")
    if previous:
        arguments.extend(["--previous-manifest", str(previous)])
    return arguments


def _mock_public_readback(monkeypatch):
    monkeypatch.setattr(
        ota_release,
        "_utc_now",
        lambda: datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )

    def get(url, **_kwargs):
        key = url.removeprefix("https://updates.example.invalid/")
        body = _FakeR2.state.get(key)
        if body is None:
            return _FakeR2._response(404)
        return _FakeR2._response(200, body)

    monkeypatch.setattr(ota_release.httpx, "get", get)


def test_r2_client_botocore_signs_every_conditional_write_header():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, request=request)

    client = ota_release.R2Client(
        account_id="4d94f3bcb89bc16989d5ea715eaac061",
        bucket="dradar-cli-ota-production",
        access_key_id="test-access",
        secret_access_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        response = client.put_new(
            "releases/stable/s0000000001/v0.5.177/test.pyz",
            b"payload",
            content_type="application/octet-stream",
            cache_control="public,max-age=31536000,immutable",
        )
        cas_response = client.put_if_match(
            "channels/stable/current.json",
            b"manifest",
            etag='"previous-etag"',
        )
    finally:
        client.close()

    assert response.status_code == 200
    assert cas_response.status_code == 200
    assert len(requests) == 2
    new_signed_names = requests[0].headers["authorization"].split(
        "SignedHeaders=", 1
    )[1].split(",", 1)[0]
    assert set(new_signed_names.split(";")) >= {
        "cache-control",
        "content-type",
        "host",
        "if-none-match",
        "x-amz-content-sha256",
        "x-amz-date",
    }
    assert requests[0].headers["if-none-match"] == "*"
    cas_signed_names = requests[1].headers["authorization"].split(
        "SignedHeaders=", 1
    )[1].split(",", 1)[0]
    assert "if-match" in cas_signed_names.split(";")
    assert requests[1].headers["if-match"] == '"previous-etag"'


def test_r2_publication_uploads_immutable_inputs_before_bootstrap_pointer(
    tmp_path, monkeypatch,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 0

    pointer = "channels/stable/current.json"
    assert _FakeR2.state[pointer] == (bundle / "manifest.json").read_bytes()
    puts = [item for item in _FakeR2.operations if item[0].startswith("PUT")]
    assert puts[-1] == ("PUT_NEW", pointer)
    assert puts[-2][1].endswith("/manifest.json")
    assert len([item for item in puts if item[1].endswith(".pyz")]) == 6


def test_r2_nonbootstrap_pointer_uses_etag_cas(tmp_path, monkeypatch):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    private, private_path, registry = _key_material(tmp_path)
    previous = _signed_previous(private, tmp_path / "previous.json")
    pointer = "channels/stable/current.json"
    _FakeR2.state[pointer] = previous.read_bytes()
    _FakeR2.etags[pointer] = '"previous-etag"'
    bundle = _build(tmp_path, sequence=2)
    assert _sign(
        bundle,
        private_path,
        registry,
        previous=previous,
        bootstrap=False,
    ) == 0
    assert _verify(bundle, registry, previous=previous, bootstrap=False) == 0
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(
        _publish_arguments(
            bundle,
            registry,
            previous=previous,
            bootstrap=False,
        )
    ) == 0
    assert ("PUT_CAS", pointer) in _FakeR2.operations


def test_previous_manifest_path_replacement_cannot_bypass_in_memory_chain_snapshot(
    tmp_path, monkeypatch, capsys,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    private, private_path, registry = _key_material(tmp_path)
    previous_a = _signed_previous(private, tmp_path / "previous-a.json")
    current_c = _signed_previous(
        private,
        tmp_path / "current-c.json",
        sequence=100,
        version="9.0.0",
        published_at="2026-08-15T00:00:00Z",
        expires_at="2026-10-15T00:00:00Z",
    )
    pointer = "channels/stable/current.json"
    current_c_bytes = current_c.read_bytes()
    _FakeR2.state[pointer] = current_c_bytes
    _FakeR2.etags[pointer] = '"current-c-etag"'
    bundle_b = _build(tmp_path, sequence=2)
    assert _sign(
        bundle_b,
        private_path,
        registry,
        previous=previous_a,
        bootstrap=False,
    ) == 0
    assert _verify(
        bundle_b,
        registry,
        previous=previous_a,
        bootstrap=False,
    ) == 0
    original_get = _FakeR2.get

    def replace_previous_on_first_current_get(self, key):
        if key == pointer and not any(
            operation == ("GET", pointer) for operation in self.operations
        ):
            previous_a.write_bytes(current_c_bytes)
        return original_get(self, key)

    monkeypatch.setattr(_FakeR2, "get", replace_previous_on_first_current_get)
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(
        _publish_arguments(
            bundle_b,
            registry,
            previous=previous_a,
            bootstrap=False,
        )
    ) == 2
    assert "current stable pointer readback digest differs" in capsys.readouterr().err
    assert _FakeR2.state[pointer] == current_c_bytes
    assert not any(operation[0].startswith("PUT") for operation in _FakeR2.operations)


def test_r2_duplicate_immutable_object_fails_before_pointer_update(
    tmp_path, monkeypatch, capsys,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    plan = json.loads((bundle / "release-plan.json").read_text())
    first = plan["artifacts"][0]["object_key"]
    _FakeR2.state[first] = b"foreign"
    _FakeR2.etags[first] = '"foreign"'
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 2
    assert "immutable upload rejected" in capsys.readouterr().err
    assert "channels/stable/current.json" not in _FakeR2.state


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("retired", "not active"),
        ("next", "not active"),
        ("expired", "current registry validity"),
        ("not_yet_valid", "published_at"),
    ],
)
def test_publish_revalidates_key_status_and_both_validity_times_before_upload(
    tmp_path, monkeypatch, capsys, case, message,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    document = json.loads(registry.read_text())
    key = document["keys"]["test-release-2026"]
    if case in {"retired", "next"}:
        key["status"] = case
    elif case == "expired":
        key["not_after"] = "2026-09-02T12:00:00Z"
    else:
        key["not_before"] = "2026-09-02T00:00:01Z"
    registry.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 2
    assert message in capsys.readouterr().err
    assert _FakeR2.operations == []
    assert "channels/stable/current.json" not in _FakeR2.state


def test_publish_rejects_manifest_already_expired_at_entry_before_network(
    tmp_path, monkeypatch, capsys,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    monkeypatch.setattr(
        ota_release,
        "_utc_now",
        lambda: datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
    )
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 2
    assert "manifest is expired at publication boundary" in capsys.readouterr().err
    assert _FakeR2.operations == []
    assert "channels/stable/current.json" not in _FakeR2.state


def test_manifest_expiring_during_immutable_upload_stops_before_pointer_commit(
    tmp_path, monkeypatch, capsys,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    publication_times = iter(
        [
            datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
            datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(ota_release, "_utc_now", lambda: next(publication_times))
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 2
    assert "manifest is expired at publication boundary" in capsys.readouterr().err
    immutable_puts = [
        operation
        for operation in _FakeR2.operations
        if operation[0] == "PUT_NEW"
    ]
    assert len(immutable_puts) == 10
    assert "channels/stable/current.json" not in _FakeR2.state


def test_registry_path_mutation_after_snapshot_cannot_change_published_bytes(
    tmp_path, monkeypatch,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    _trusted, _metadata, registry_snapshot = ota_release._registry(registry)
    original_readback = ota_release._public_readback

    def mutate_after_manifest_readback(base_url, key, expected):
        response = original_readback(base_url, key, expected)
        if key.endswith("/manifest.json"):
            document = json.loads(registry.read_text())
            document["keys"]["test-release-2026"]["status"] = "retired"
            registry.write_text(json.dumps(document), encoding="utf-8")
        return response

    monkeypatch.setattr(ota_release, "_public_readback", mutate_after_manifest_readback)
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 0
    plan = json.loads((bundle / "release-plan.json").read_text())
    assert _FakeR2.state[f"{plan['release']['object_prefix']}/trusted-keys.json"] == (
        registry_snapshot
    )
    assert _FakeR2.state["channels/stable/current.json"] == (
        bundle / "manifest.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "mutation",
    ["extra_sensitive_field", "missing_field", "sequence", "object_prefix", "key_metadata"],
)
def test_publish_reconstructs_canonical_audit_and_rejects_all_drift_before_upload(
    tmp_path, monkeypatch, capsys, mutation,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    audit_path = bundle / "release-audit.json"
    if mutation == "key_metadata":
        document = json.loads(registry.read_text())
        document["keys"]["test-release-2026"]["not_after"] = "2028-01-01T00:00:00Z"
        registry.write_text(json.dumps(document), encoding="utf-8")
    else:
        audit = json.loads(audit_path.read_text())
        if mutation == "extra_sensitive_field":
            audit["credential_sentinel"] = "DO_NOT_PUBLISH_SECRET_SENTINEL"
        elif mutation == "missing_field":
            audit.pop("source")
        elif mutation == "sequence":
            audit["sequence"] += 1
        else:
            audit["object_prefix"] = "unlocked/forged"
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 2
    assert "unique canonical publication record" in capsys.readouterr().err
    assert _FakeR2.operations == []
    assert all(
        b"DO_NOT_PUBLISH_SECRET_SENTINEL" not in body
        for body in _FakeR2.state.values()
    )
    assert "channels/stable/current.json" not in _FakeR2.state


@pytest.mark.parametrize("failed_readback", ["authenticated", "public"])
def test_successful_pointer_cas_is_reported_as_committed_but_unverified_not_retryable(
    tmp_path, monkeypatch, capsys, failed_readback,
):
    _FakeR2.state, _FakeR2.etags, _FakeR2.operations = {}, {}, []
    monkeypatch.setattr(ota_release, "R2Client", _FakeR2)
    _mock_public_readback(monkeypatch)
    pointer = "channels/stable/current.json"
    if failed_readback == "authenticated":
        original_get = _FakeR2.get

        def failing_get(self, key):
            if key == pointer and key in self.state:
                self.operations.append(("GET", key))
                return self._response(503)
            return original_get(self, key)

        monkeypatch.setattr(_FakeR2, "get", failing_get)
    else:
        def public_get(url, **_kwargs):
            key = url.removeprefix("https://updates.example.invalid/")
            if key == pointer and key in _FakeR2.state:
                return _FakeR2._response(503)
            body = _FakeR2.state.get(key)
            return _FakeR2._response(200, body) if body is not None else _FakeR2._response(404)

        monkeypatch.setattr(ota_release.httpx, "get", public_get)
    _private, private_path, registry = _key_material(tmp_path)
    bundle = _build(tmp_path)
    assert _sign(bundle, private_path, registry) == 0
    assert _verify(bundle, registry) == 0
    monkeypatch.setenv("DRADAR_OTA_R2_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("DRADAR_OTA_R2_SECRET_ACCESS_KEY", "test-secret")

    assert ota_release.main(_publish_arguments(bundle, registry)) == 3
    report = json.loads(capsys.readouterr().err)
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    assert report["status"] == "committed_but_unverified"
    assert report["committed"] is True
    assert report["verified"] is False
    assert report["retryable"] is False
    assert report["next_action"].startswith("do_not_rerun_publish")
    assert report["expected"]["etag"] == _FakeR2.etags[pointer]
    assert report["expected"]["sha256"] == __import__("hashlib").sha256(
        manifest_bytes
    ).hexdigest()
    assert _FakeR2.state[pointer] == manifest_bytes
    assert [item for item in _FakeR2.operations if item == ("PUT_NEW", pointer)] == [
        ("PUT_NEW", pointer)
    ]


def test_cloudflare_preflight_requires_indefinite_releases_lock(
    monkeypatch, capsys,
):
    monkeypatch.setenv("DRADAR_OTA_CLOUDFLARE_API_TOKEN", "read-only-test")

    def get(_url, **_kwargs):
        return __import__("httpx").Response(
            200,
            json={
                "success": True,
                "result": {
                    "rules": [
                        {
                            "id": "ota-releases-indefinite",
                            "enabled": True,
                            "prefix": "releases/",
                            "condition": {"type": "Indefinite"},
                        }
                    ]
                },
            },
            request=__import__("httpx").Request("GET", "https://api.invalid"),
        )

    monkeypatch.setattr(ota_release.httpx, "get", get)
    assert ota_release.main(
        [
            "cloudflare-preflight",
            "--account-id",
            "4d94f3bcb89bc16989d5ea715eaac061",
            "--bucket",
            "dradar-cli-ota-production",
            "--public-base-url",
            "https://updates.example.invalid",
        ]
    ) == 0
    assert "PASS" in capsys.readouterr().out


def test_fetch_current_rejects_redirect_and_does_not_write(tmp_path, monkeypatch, capsys):
    output = tmp_path / "current.json"

    def get(_url, **_kwargs):
        return __import__("httpx").Response(
            302,
            headers={"location": "https://other.invalid/current.json"},
            request=__import__("httpx").Request("GET", "https://updates.invalid"),
        )

    monkeypatch.setattr(ota_release.httpx, "get", get)
    assert ota_release.main(
        [
            "fetch-current",
            "--url",
            "https://updates.example.invalid/channels/stable/current.json",
            "--output",
            str(output),
        ]
    ) == 2
    assert not output.exists()
    assert "HTTP 302" in capsys.readouterr().err


def test_fetch_current_bootstrap_requires_real_404(monkeypatch, capsys):
    def get(_url, **_kwargs):
        return _FakeR2._response(404)

    monkeypatch.setattr(ota_release.httpx, "get", get)
    assert ota_release.main(
        [
            "fetch-current",
            "--url",
            "https://updates.example.invalid/channels/stable/current.json",
            "--expect-absent",
        ]
    ) == 0
    assert "absent" in capsys.readouterr().out
