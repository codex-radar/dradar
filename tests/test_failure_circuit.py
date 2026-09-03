import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from dradar import failure_circuit, runloop


def test_isolated_failure_does_not_open(tmp_path):
    assert failure_circuit.observe(
        scope="batch/runtime", signature="exit=1",
        state_path=tmp_path / "state.json",
    ) == (1, False)


def test_same_failure_opens_on_second_observation(tmp_path):
    path = tmp_path / "state.json"
    failure_circuit.observe(scope="batch/runtime", signature="exit=1", state_path=path)
    assert failure_circuit.observe(
        scope="batch/runtime", signature="exit=1", state_path=path,
    ) == (2, True)


def test_success_resets_consecutive_failure_count(tmp_path):
    path = tmp_path / "state.json"
    failure_circuit.observe(scope="batch/runtime", signature="exit=1", state_path=path)
    assert failure_circuit.observe(
        scope="batch/runtime", signature=None, state_path=path,
    ) == (0, False)
    assert failure_circuit.observe(
        scope="batch/runtime", signature="exit=1", state_path=path,
    ) == (1, False)


def test_open_provider_circuit_survives_success_and_explicit_clear(tmp_path):
    path = tmp_path / "persistent.json"
    scope = "batch/provider/model/version/root"
    failure_circuit.observe(scope=scope, signature="false-success", state_path=path)
    assert failure_circuit.observe(
        scope=scope, signature="false-success", state_path=path,
    ) == (2, True)
    assert failure_circuit.observe(
        scope=scope, signature=None, state_path=path, clear_open=False,
    ) == (2, True)
    assert failure_circuit.status(scope=scope, state_path=path) == (2, True)

    failure_circuit.clear(scope=None, state_path=path)

    assert failure_circuit.status(scope=scope, state_path=path) == (0, False)


def test_open_provider_circuit_is_visible_to_a_new_process(tmp_path):
    path = tmp_path / "persistent.json"
    scope = "batch/provider/model/version/root"
    failure_circuit.observe(scope=scope, signature="false-success", state_path=path)
    failure_circuit.observe(scope=scope, signature="false-success", state_path=path)
    env = dict(os.environ)
    env["CIRCUIT_PATH"] = str(path)
    env["CIRCUIT_SCOPE"] = scope
    source_root = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        source_root, env.get("PYTHONPATH"),
    )))

    proc = subprocess.run(
        [sys.executable, "-c", (
            "import os; from pathlib import Path; "
            "from dradar import failure_circuit; "
            "print(failure_circuit.status(scope=os.environ['CIRCUIT_SCOPE'], "
            "state_path=Path(os.environ['CIRCUIT_PATH'])))"
        )],
        env=env, check=True, capture_output=True, text=True,
    )

    assert proc.stdout.strip() == "(2, True)"


def test_different_signatures_are_not_merged(tmp_path):
    path = tmp_path / "state.json"
    failure_circuit.observe(scope="batch/runtime", signature="exit=1", state_path=path)
    assert failure_circuit.observe(
        scope="batch/runtime", signature="exit=2", state_path=path,
    ) == (1, False)


def test_scopes_keep_independent_streaks_across_other_scope_activity(tmp_path):
    path = tmp_path / "state.json"
    assert failure_circuit.observe(
        scope="batch-a/runtime", signature="exit=1", state_path=path,
    ) == (1, False)
    assert failure_circuit.observe(
        scope="batch-b/runtime", signature="exit=1", state_path=path,
    ) == (1, False)
    assert failure_circuit.observe(
        scope="batch-b/runtime", signature=None, state_path=path,
    ) == (0, False)
    assert failure_circuit.observe(
        scope="batch-a/runtime", signature="exit=1", state_path=path,
    ) == (2, True)


def test_zero_progress_signature_requires_command_exit_and_no_usage(tmp_path):
    assignment = {
        "batch_id": "batch-1", "agent": "codex", "provider": None,
        "model": "gpt-5.6-sol", "agent_version": "0.149.0",
    }
    art = SimpleNamespace(codex_cli_version="0.149.0", trial_dir=tmp_path)
    diag = {
        "type": "NonZeroAgentExitCodeError", "kind": None,
        "tail": ["Command failed (exit 1): codex"],
    }
    signature = runloop._repeat_failure_signature(
        assignment, {"n_agent_steps": 0, "n_input_tokens": None}, diag, art,
    )
    assert signature is not None
    assert '"exit_code":1' in signature[1]
    assert runloop._repeat_failure_signature(
        assignment, {"n_agent_steps": 1, "n_input_tokens": None}, diag, art,
    ) is None
    assert runloop._repeat_failure_signature(
        assignment, {"n_agent_steps": 0},
        {**diag, "tail": ["unclassified failure"]}, art,
    ) is None


def test_observed_trajectory_tokens_are_not_zero_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle",
        lambda _trial_dir: {
            "complete": False,
            "aggregate_usage": {"n_input_tokens": 42, "n_output_tokens": 0},
        },
    )
    assert runloop._repeat_failure_signature(
        {"batch_id": "b", "agent": "codex", "model": "gpt-5.6-sol"},
        {"n_agent_steps": 0, "n_input_tokens": None},
        {
            "type": "NonZeroAgentExitCodeError", "kind": None,
            "tail": ["Command failed (exit 1): codex"],
        },
        SimpleNamespace(codex_cli_version="0.149.0", trial_dir=tmp_path),
    ) is None


def test_non_codex_harness_is_outside_this_narrow_circuit(tmp_path):
    assert runloop._repeat_failure_signature(
        {"batch_id": "b", "agent": "dsh", "model": "deepseek"},
        {"n_agent_steps": 0},
        {
            "type": "NonZeroAgentExitCodeError", "kind": None,
            "tail": ["Command failed (exit 1): dsh"],
        },
        SimpleNamespace(codex_cli_version=None, trial_dir=tmp_path),
    ) is None
