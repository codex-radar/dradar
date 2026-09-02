import hashlib

import pytest

from dradar.ota.download import download_verified_artifact
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


def test_disconnect_cleans_partial_and_preserves_existing_candidate(tmp_path):
    body = b"candidate-body"
    existing = tmp_path / "candidate.whl"
    existing.write_bytes(body)

    class DisconnectingResponse(FakeResponse):
        def iter_bytes(self, chunk_size=65536):
            del chunk_size
            yield b"partial"
            raise ConnectionError("offline")

    with pytest.raises(ConnectionError, match="offline"):
        download_verified_artifact(
            FakeClient(DisconnectingResponse([])),
            _artifact(body),
            tmp_path,
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
