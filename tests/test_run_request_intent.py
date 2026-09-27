import json

import pytest

from dradar import run_intent


@pytest.mark.parametrize("bad", ["bad-batch-id", ["nonempty"], {"batch": "bad"}, "c" * 32])
def test_damaged_request_link_never_prevents_cancellation(tmp_path, bad):
    request = run_intent.request_scope("run_synthetic")
    generation = run_intent.begin(tmp_path, request)
    run_intent.associate_request(tmp_path, request, generation, "b" * 32, automatic=False)
    path, _, _ = run_intent._paths(tmp_path, request)
    state = json.loads(path.read_text())
    state["batch_id"] = bad
    path.write_text(json.dumps(state))
    run_intent.stop_request(tmp_path, request)
    with pytest.raises(run_intent.IntentStopped):
        run_intent.require(tmp_path, request, generation)
    # The unknown link cannot grant authority to stop an unrelated batch.
    assert not run_intent._paths(tmp_path, "c" * 32)[1].exists()
