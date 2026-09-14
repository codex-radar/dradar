"""A late exact child may drain; only the parent can prove campaign completion."""
from types import SimpleNamespace

import pytest

from dradar import assignment_boundary, runloop
from dradar.api_client import ApiError

BATCH = 'a' * 32
OTHER = 'b' * 32


class EmptyClient:
    batch_id = BATCH
    error = ApiError('active batch not found', status_code=404, code='claim_batch_not_found')

    def get_assignment(self):
        raise self.error

    def set_batch_id(self, value):
        self.batch_id = value


def setup(monkeypatch, tmp_path):
    row = {'assignment_id': 'one', 'batch_id': BATCH}
    path = assignment_boundary.prepare(tmp_path, 'deep-swe', [row])
    monkeypatch.setattr(runloop, 'HOME', tmp_path)
    monkeypatch.setenv(runloop._ASSIGNMENT_BOUNDARY_ENV, str(path))
    monkeypatch.setattr(runloop, '_pending_assignment_ids_for_client', lambda *_a, **_k: set())
    monkeypatch.setattr(runloop, '_setup_refill', lambda args, client, active, free: active)
    args = SimpleNamespace(worker_child=True, resume=True, parallel=True, refill=False,
                           yes=True, batch_id=BATCH, pick=None, auto=None)
    return args, EmptyClient(), path, row


def test_admitted_late_child_empty_does_not_hide_missing_parent_outcome(monkeypatch, tmp_path):
    args, client, path, row = setup(monkeypatch, tmp_path)
    assert runloop._prepare_batch(args, client) == ([], True)
    assert not runloop._finish_assignment_boundary(client, path)
    assert path.exists()  # expiration/disappearance cannot masquerade as submission
    assignment_boundary.record_outcome(path, row, 'submitted')
    assert runloop._finish_assignment_boundary(client, path)
    assert not path.exists()


@pytest.mark.parametrize('case', ['ordinary-resume', 'unadmitted', 'missing-ledger', 'corrupt-ledger',
                                  'different-boundary', 'unknown-404', 'unauthorized', 'server-error'])
def test_empty_child_exception_does_not_weaken_fail_closed_paths(monkeypatch, tmp_path, case):
    args, client, path, row = setup(monkeypatch, tmp_path)
    if case == 'ordinary-resume':
        args.worker_child = False
    elif case == 'unadmitted':
        args.batch_id = client.batch_id = OTHER
    elif case == 'missing-ledger':
        path.unlink()
    elif case == 'corrupt-ledger':
        path.write_text('{')
    elif case == 'different-boundary':
        args._assignment_boundary_path = str(tmp_path / 'other.json')
    elif case == 'unknown-404':
        client.error = ApiError('endpoint missing', status_code=404, code='unknown')
    elif case == 'unauthorized':
        client.error = ApiError('unauthorized', status_code=401)
    else:
        client.error = ApiError('server error', status_code=503)
    with pytest.raises(SystemExit):
        runloop._prepare_batch(args, client)
