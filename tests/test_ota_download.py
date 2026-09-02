import hashlib
import os

import pytest

import dradar.ota.download as download_module
from dradar.ota.download import (
    VerifiedArtifact,
    download_verified_artifact,
    stage_verified_artifact,
)
from dradar.ota.manifest import Artifact, ManifestError, PlatformTarget


class FakeResponse:
    def __init__(self, chunks, failure=None):
        self.chunks = chunks
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        if self.failure:
            raise self.failure

    def iter_bytes(self, chunk_size=65536):
        del chunk_size
        yield from self.chunks


class FakeClient:
    def __init__(self, response):
        self.response = response

    def stream(self, method, url, **kwargs):
        assert method == "GET"
        assert url.startswith("https://")
        assert kwargs == {"follow_redirects": False}
        return self.response


def _artifact(body):
    return Artifact(
        target=PlatformTarget("linux", "x86_64"),
        filename="candidate.whl",
        url="https://releases.example.invalid/candidate.whl",
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


def test_download_is_published_only_after_verification(tmp_path):
    body = b"candidate-body"
    path = download_verified_artifact(
        FakeClient(FakeResponse([body[:4], body[4:]])),
        _artifact(body),
        tmp_path,
    )
    assert path.read_bytes() == body
    assert not list(tmp_path.glob("*.partial"))


def test_verified_existing_candidate_is_reused_without_network(tmp_path):
    body = b"candidate-body"
    existing = tmp_path / "candidate.whl"
    existing.write_bytes(body)

    class NoNetworkClient:
        def stream(self, method, url, **kwargs):
            raise AssertionError("verified immutable artifact must be reused")

    assert (
        download_verified_artifact(
            NoNetworkClient(),
            _artifact(body),
            tmp_path,
        )
        == existing
    )

    assert existing.read_bytes() == body
    assert not list(tmp_path.glob("*.partial"))


def test_oversized_or_corrupt_download_never_replaces_final(tmp_path):
    body = b"candidate-body"
    with pytest.raises(ManifestError, match="exceeded"):
        download_verified_artifact(
            FakeClient(FakeResponse([body + b"unexpected"])),
            _artifact(body),
            tmp_path,
        )
    assert not (tmp_path / "candidate.whl").exists()


def test_keyboard_interrupt_cleans_partial_download(tmp_path):
    body = b"candidate-body"

    class InterruptedResponse(FakeResponse):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            yield b"partial"
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        download_verified_artifact(
            FakeClient(InterruptedResponse([])),
            _artifact(body),
            tmp_path,
        )

    assert not list(tmp_path.glob("*.partial"))
    assert not (tmp_path / "candidate.whl").exists()


def test_existing_immutable_artifact_is_never_overwritten(tmp_path):
    body = b"candidate-body"
    final = tmp_path / "candidate.whl"
    final.write_bytes(b"different-body")

    with pytest.raises(ManifestError, match="immutable artifact already exists"):
        download_verified_artifact(
            FakeClient(FakeResponse([body])),
            _artifact(body),
            tmp_path,
        )

    assert final.read_bytes() == b"different-body"


def test_destination_symlink_cannot_redirect_download_outside_ota_root(tmp_path):
    body = b"candidate-body"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "ota" / "downloads"
    destination.parent.mkdir()
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManifestError, match="symlink|safe directory"):
        download_verified_artifact(
            FakeClient(FakeResponse([body])),
            _artifact(body),
            destination,
        )

    assert not (outside / "candidate.whl").exists()
    assert not list(outside.glob("*.partial"))


def test_racing_final_symlink_never_overwrites_its_target(tmp_path):
    body = b"candidate-body"
    destination = tmp_path / "downloads"
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"outside-safe")

    class RacingResponse(FakeResponse):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            (destination / "candidate.whl").symlink_to(outside)
            yield body

    with pytest.raises(ManifestError, match="immutable artifact"):
        download_verified_artifact(
            FakeClient(RacingResponse([])),
            _artifact(body),
            destination,
        )

    assert outside.read_bytes() == b"outside-safe"
    assert not list(destination.glob("*.partial"))


def test_racing_directory_replacement_cannot_publish_outside_anchor(tmp_path):
    body = b"candidate-body"
    destination = tmp_path / "downloads"
    outside = tmp_path / "outside"
    outside.mkdir()

    class RacingResponse(FakeResponse):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            destination.rename(tmp_path / "detached-downloads")
            destination.symlink_to(outside, target_is_directory=True)
            yield body

    with pytest.raises(ManifestError, match="changed during publication"):
        download_verified_artifact(
            FakeClient(RacingResponse([])),
            _artifact(body),
            destination,
        )

    assert not (outside / "candidate.whl").exists()
    assert not (tmp_path / "detached-downloads" / "candidate.whl").exists()
    assert not list((tmp_path / "detached-downloads").glob("*.partial"))


def test_verified_external_hardlink_is_not_reused_as_ota_artifact(tmp_path):
    body = b"candidate-body"
    outside = tmp_path / "outside.whl"
    outside.write_bytes(body)
    destination = tmp_path / "downloads"
    destination.mkdir()
    (destination / "candidate.whl").hardlink_to(outside)

    with pytest.raises(ManifestError, match="external hardlink|immutable artifact"):
        download_verified_artifact(
            FakeClient(FakeResponse([body])),
            _artifact(body),
            destination,
        )

    assert outside.read_bytes() == body


def test_final_name_replacement_after_last_check_returns_open_inode_capability(
    tmp_path,
    monkeypatch,
):
    body = b"candidate-body"
    destination = tmp_path / "downloads"
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"outside-safe")
    original = download_module._name_matches_open_file
    raced = False

    def replace_after_check(dir_fd, filename, file_fd):
        nonlocal raced
        result = original(dir_fd, filename, file_fd)
        if not raced:
            raced = True
            final = destination / "candidate.whl"
            final.unlink()
            final.symlink_to(outside)
        return result

    monkeypatch.setattr(
        download_module,
        "_name_matches_open_file",
        replace_after_check,
    )
    verified = download_verified_artifact(
        FakeClient(FakeResponse([body])),
        _artifact(body),
        destination,
    )

    assert isinstance(verified, VerifiedArtifact)
    assert verified.read_bytes() == body
    assert verified.binding_is_current() is False
    with pytest.raises(ManifestError):
        stage_verified_artifact(verified, _artifact(body), destination)
    verified.close()
    assert outside.read_bytes() == b"outside-safe"


def test_existing_artifact_name_race_returns_open_inode_capability(
    tmp_path, monkeypatch
):
    body = b"candidate-body"
    destination = tmp_path / "downloads"
    destination.mkdir()
    final = destination / "candidate.whl"
    final.write_bytes(body)
    outside = tmp_path / "outside.whl"
    outside.write_bytes(b"outside-safe")
    original = download_module._directory_matches_path
    raced = False

    def replace_after_check(dir_fd, path):
        nonlocal raced
        result = original(dir_fd, path)
        if not raced:
            raced = True
            final.unlink()
            final.symlink_to(outside)
        return result

    monkeypatch.setattr(
        download_module,
        "_directory_matches_path",
        replace_after_check,
    )
    verified = download_verified_artifact(
        FakeClient(FakeResponse([body])),
        _artifact(body),
        destination,
    )

    assert isinstance(verified, VerifiedArtifact)
    assert verified.read_bytes() == body
    assert verified.binding_is_current() is False
    verified.close()
    assert outside.read_bytes() == b"outside-safe"


@pytest.mark.skipif(
    not os.path.isdir("/dev/fd"), reason="requires observable POSIX fds"
)
def test_repeated_final_symlink_rejection_does_not_leak_directory_fds(tmp_path):
    body = b"candidate-body"
    destination = tmp_path / "downloads"
    destination.mkdir()
    outside = tmp_path / "outside.whl"
    outside.write_bytes(body)
    (destination / "candidate.whl").symlink_to(outside)
    before = len(os.listdir("/dev/fd"))

    for _ in range(100):
        with pytest.raises(ManifestError, match="immutable artifact"):
            download_verified_artifact(
                FakeClient(FakeResponse([body])),
                _artifact(body),
                destination,
            )

    assert len(os.listdir("/dev/fd")) <= before + 1


def test_verified_artifact_close_is_idempotent_and_does_not_close_reused_fd(tmp_path):
    body = b"candidate-body"
    verified = download_verified_artifact(
        FakeClient(FakeResponse([body])),
        _artifact(body),
        tmp_path / "downloads",
    )
    verified.close()
    sentinel = os.open(tmp_path / "sentinel", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        verified.close()
        assert os.write(sentinel, b"still-open") == len(b"still-open")
    finally:
        os.close(sentinel)
