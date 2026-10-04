import hashlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import publish_fixed_wheel as module


@pytest.mark.parametrize('upload,public,valid,ok', [
    (200, 200, True, True), (412, 200, True, True),
    (403, 200, True, False), (200, 302, True, False),
    (200, 200, False, False), (200, 404, True, False),
])
def test_publication_receipt_only_after_verified_http(tmp_path, monkeypatch, upload, public, valid, ok):
    body = b'synthetic wheel bytes'
    monkeypatch.setattr(module, 'SIZE', len(body))
    monkeypatch.setattr(module, 'SHA256', hashlib.sha256(body).hexdigest())
    wheel = tmp_path / 'dradar-0.5.295-py3-none-any.whl'
    wheel.write_bytes(body)
    receipt = tmp_path / 'receipt.json'
    store = Mock()
    store.put_new.return_value = httpx.Response(upload)
    store.get.return_value = httpx.Response(200, content=body)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(public, content=body if valid else b'wrong')), follow_redirects=False) as client:
        if ok:
            result = module.publish(wheel, receipt, store, client, source_commit="a" * 40)
            assert result['channel_pointer_changed'] is False
            assert receipt.exists()
        else:
            with pytest.raises(RuntimeError):
                module.publish(wheel, receipt, store, client, source_commit="a" * 40)
            assert not receipt.exists()


def test_wrong_local_bytes_never_upload(tmp_path):
    wheel = tmp_path / 'dradar-0.5.295-py3-none-any.whl'
    wheel.write_bytes(b'wrong')
    store = Mock()
    with pytest.raises(ValueError):
        module.publish(wheel, tmp_path / 'receipt', store, Mock(), source_commit="a" * 40)
    store.put_new.assert_not_called()


def test_conflicting_immutable_object_never_receipts(tmp_path, monkeypatch):
    body = b'synthetic'
    monkeypatch.setattr(module, 'SIZE', len(body))
    monkeypatch.setattr(module, 'SHA256', hashlib.sha256(body).hexdigest())
    wheel = tmp_path / 'dradar-0.5.295-py3-none-any.whl'
    wheel.write_bytes(body)
    store = Mock()
    store.put_new.return_value = httpx.Response(412)
    store.get.return_value = httpx.Response(200, content=b'other')
    with pytest.raises(RuntimeError):
        module.publish(wheel, tmp_path / 'receipt', store, Mock(), source_commit="a" * 40)


def test_invalid_actual_commit_rejected_before_upload(tmp_path):
    store = Mock()
    with pytest.raises(ValueError, match="actual full reviewed main commit"):
        module.publish(tmp_path / "wheel", tmp_path / "receipt", store, Mock(), source_commit="unfixed")
    store.put_new.assert_not_called()
