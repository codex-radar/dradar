import pytest
from dradar.v2.selection import Catalog, resolve, questions
from dradar.v2.presentation import TaskProgress, Usage

@pytest.fixture
def catalog():
    return Catalog({"synthetic": ("codex-test",), "other": ("codex-other",)}, 4, 20)

def test_explicit_selection_never_prompts_again(catalog):
    values = dict(benchmark="synthetic", model="codex-test", total=8, concurrency=2)
    assert questions(values, catalog) == []
    assert resolve(values, catalog).total == 8

def test_only_missing_choices_are_requested(catalog):
    qs = questions(dict(benchmark="synthetic", model="codex-test"), catalog)
    assert [q["field"] for q in qs] == ["total", "concurrency"]

def test_model_options_follow_selected_benchmark(catalog):
    assert questions(dict(benchmark="synthetic", total=3, concurrency=1), catalog)[0]["options"] == ["codex-test"]

@pytest.mark.parametrize("changes", [dict(concurrency=5), dict(total=True), dict(total=0), dict(model="codex-other"), dict(benchmark="missing"), dict(unknown=1)])
def test_invalid_explicit_choice_is_not_silently_changed(catalog, changes):
    values = dict(benchmark="synthetic", model="codex-test", total=8, concurrency=2)
    values.update(changes)
    with pytest.raises(ValueError):
        resolve(values, catalog)

def test_missing_usage_and_elapsed_are_unknown():
    progress = TaskProgress("task1", execution="unknown", delivery="accepted", grading="passed")
    assert progress.view()["execution"] == "unknown"
    assert progress.view()["elapsed_seconds"] == "unknown"
    assert progress.view()["usage"]["total_tokens"] == "unknown"
    assert "执行=unknown" in progress.text()

def test_partial_usage_does_not_invent_total():
    assert Usage(input_tokens=0, output_tokens=7).view() == {"input_tokens": 0, "output_tokens": 7, "total_tokens": "unknown"}

def test_completed_execution_does_not_imply_upload_or_grade():
    progress = TaskProgress("task1", execution="completed", delivery="failed", elapsed_seconds=2.5)
    assert progress.view()["grading"] == "not_requested"
    assert "保存/上传=failed" in progress.text()

@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_elapsed_rejected(value):
    with pytest.raises(ValueError):
        TaskProgress("t", elapsed_seconds=value)

@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_token_evidence_rejected(value):
    with pytest.raises(ValueError):
        Usage(total_tokens=value)

@pytest.mark.parametrize("changes", [dict(benchmark=[]), dict(model={})])
def test_nontext_selection_is_explicitly_invalid(catalog, changes):
    values = dict(benchmark="synthetic", model="codex-test", total=8, concurrency=2)
    values.update(changes)
    with pytest.raises(ValueError):
        questions(values, catalog)


def test_null_policy_caps_do_not_invent_a_batch_ceiling():
    catalog = Catalog({"synthetic": ("codex-test",)}, None, None)
    assert resolve(dict(benchmark="synthetic", model="codex-test", total=100, concurrency=20), catalog).total == 100
