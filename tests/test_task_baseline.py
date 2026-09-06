import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dradar.task_baseline import (
    BASELINE_REQUEST_ENV, REMOTE_BASELINE, SOURCE_COMMIT_PROOF,
    SOURCE_ORIGIN_PROOF, verify_task_baseline,
)


class LocalEnvironment:
    """Exercise real Git commands against a local, network-free source fixture."""

    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        self.baseline = repo.parent / "resolved-base"

    async def exec(self, *, command, timeout_sec, env=None):
        self.calls.append(command)
        assert timeout_sec <= 30
        command = command.replace("/app", shlex.quote(str(self.repo)))
        command = command.replace(REMOTE_BASELINE, shlex.quote(str(self.baseline)))
        command = command.replace(SOURCE_ORIGIN_PROOF, shlex.quote(str(self.repo.parent / "origin-proof")))
        command = command.replace(SOURCE_COMMIT_PROOF, shlex.quote(str(self.repo.parent / "commit-proof")))
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_sec)
        stdout = result.stdout
        if "--show-toplevel" in command and result.returncode == 0:
            stdout = "/app\n"
        return SimpleNamespace(return_code=result.returncode, stdout=stdout)


@pytest.fixture
def source(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    def git(*args, input=None):
        return subprocess.check_output(["git", "-C", str(repo), *args], input=input, text=True).strip()
    git("init", "--quiet")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("remote", "add", "origin", "https://example.invalid/project/source.git")
    git("commit", "--allow-empty", "--quiet", "-m", "base")
    full = git("rev-parse", "HEAD")
    request = tmp_path / "baseline.json"
    monkeypatch.setenv(BASELINE_REQUEST_ENV, str(request))
    def configure(prefix, repository="https://example.invalid/project/source"):
        request.write_text(json.dumps({"base_commit": prefix, "repository_url": repository}))
    return repo, git, full, request, configure


def test_unique_short_commit_is_recorded_before_model(source):
    repo, git, full, request, configure = source
    configure(full[:7])
    # The starting commit must remain the task baseline even after HEAD moves.
    git("commit", "--allow-empty", "--quiet", "-m", "later")
    environment = LocalEnvironment(repo)
    asyncio.run(verify_task_baseline(environment))
    assert environment.baseline.read_text().strip() == full
    evidence = json.loads(request.with_suffix(".resolved.json").read_text())
    assert evidence["resolved_commit"] == full
    assert evidence["verified_before_model"] is True
    assert all("fetch" not in call and "clone" not in call for call in environment.calls)


@pytest.mark.parametrize("prefix", ["HEAD", "main^{commit}", "-abc", "abcd;false", "abcd\nfalse", "abc", "a" * 41])
def test_invalid_input_never_reaches_git_or_model(source, prefix):
    repo, _, _, request, configure = source
    configure(prefix)
    environment = LocalEnvironment(repo)
    with pytest.raises(ValueError):
        asyncio.run(verify_task_baseline(environment))
    assert not environment.calls
    assert not request.with_suffix(".resolved.json").exists()


def test_missing_object_stops_before_model(source):
    repo, git, full, request, configure = source
    prefix = "0" * 39
    assert not git("rev-parse", "--disambiguate=" + prefix)
    configure(prefix)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        asyncio.run(verify_task_baseline(LocalEnvironment(repo)))
    assert not request.with_suffix(".resolved.json").exists()


@pytest.mark.parametrize("kind", ["blob", "tag"])
def test_non_commit_object_is_rejected(source, kind):
    repo, git, full, request, configure = source
    if kind == "blob":
        oid = git("hash-object", "-w", "--stdin", input="fixture blob")
    else:
        git("tag", "-a", "fixture-tag", "-m", "annotation")
        oid = git("rev-parse", "refs/tags/fixture-tag")
    configure(oid[:12])
    with pytest.raises(ValueError, match="commit object"):
        asyncio.run(verify_task_baseline(LocalEnvironment(repo)))
    assert not request.with_suffix(".resolved.json").exists()


def test_ambiguous_prefix_rejects_multiple_real_objects(source):
    repo, git, _, request, configure = source
    seen = {}
    for index in range(10000):
        data = f"collision-fixture-{index}"
        oid = hashlib.sha1(f"blob {len(data)}\0{data}".encode()).hexdigest()
        prefix = oid[:4]
        if prefix in seen:
            git("hash-object", "-w", "--stdin", input=seen[prefix])
            git("hash-object", "-w", "--stdin", input=data)
            break
        seen[prefix] = data
    else:
        pytest.fail("deterministic fixture did not produce a prefix collision")
    configure(prefix)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        asyncio.run(verify_task_baseline(LocalEnvironment(repo)))
    assert not request.with_suffix(".resolved.json").exists()


def test_wrong_repository_stops_before_object_resolution(source):
    repo, _, full, _, configure = source
    configure(full[:7], "https://example.invalid/another/source")
    environment = LocalEnvironment(repo)
    with pytest.raises(ValueError, match="differs"):
        asyncio.run(verify_task_baseline(environment))
    assert not any("disambiguate" in call for call in environment.calls)


def test_legacy_full_sha_path_needs_no_baseline_request(monkeypatch):
    monkeypatch.delenv(BASELINE_REQUEST_ENV, raising=False)
    asyncio.run(verify_task_baseline(None))


def test_codex_model_is_not_called_when_baseline_validation_fails(source, monkeypatch):
    from dradar.pier_codex import Codex, CodexRegistered
    repo, _, _, _, configure = source
    configure("0" * 39)
    model = AsyncMock()
    monkeypatch.setattr(Codex, "run", model)
    adapter = object.__new__(CodexRegistered)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        asyncio.run(adapter.run("ordinary fixture task", LocalEnvironment(repo), None))
    model.assert_not_called()


def test_codex_model_starts_only_after_full_commit_evidence(source, monkeypatch):
    from dradar.pier_codex import Codex, CodexRegistered
    repo, _, full, request, configure = source
    configure(full[:7])
    async def model(*args):
        assert json.loads(request.with_suffix(".resolved.json").read_text())["resolved_commit"] == full
    mocked = AsyncMock(side_effect=model)
    monkeypatch.setattr(Codex, "run", mocked)
    asyncio.run(object.__new__(CodexRegistered).run("ordinary fixture task", LocalEnvironment(repo), None))
    mocked.assert_awaited_once()


def test_collector_uses_resolved_original_commit_after_model_changes_head(source, tmp_path):
    from dradar.runner import _artifact_tasks_overlay
    repo, git, full, request, configure = source
    task = tmp_path / "tasks" / "fixture-task"
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "' + full[:7] + '"\n'
        'repository_url = "https://example.invalid/project/source"\n'
    )
    environment = LocalEnvironment(repo)
    with _artifact_tasks_overlay(
        {"task_id": "fixture-task"}, task.parent, tmp_path / "work", "job",
        baseline_request_path=request,
    ) as selected:
        asyncio.run(verify_task_baseline(environment))
        (repo / "answer.txt").write_text("ordinary implementation\n")
        git("add", "answer.txt")
        git("commit", "--quiet", "-m", "answer")
        assert git("rev-parse", "HEAD") != full
        script = (selected / "fixture-task" / "pre_artifacts.sh").read_text()
        script = script.replace("/app", shlex.quote(str(repo)))
        script = script.replace(REMOTE_BASELINE, shlex.quote(str(environment.baseline)))
        script = script.replace("/logs/artifacts", shlex.quote(str(tmp_path / "artifacts")))
        subprocess.run(["sh", "-c", script], check=True)
        patch = (tmp_path / "artifacts/model.patch").read_text()
        assert "+ordinary implementation" in patch
        assert "answer.txt" in patch


def test_original_dockerfile_remote_removal_preserves_verified_build_proof(source, tmp_path):
    from dradar.runner import _artifact_tasks_overlay
    repo, git, full, request, _ = source
    task = tmp_path / "tasks" / "fixture-task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "' + full[:7] + '"\n'
        'repository_url = "https://example.invalid/project/source"\n'
    )
    original = (
        "FROM fixture\nRUN git clone https://example.invalid/project/source . \\\n"
        " && git checkout -B fixture " + full[:7] + " \\\n"
        " && git remote remove origin \\\n && true\n"
    )
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(original)
    with _artifact_tasks_overlay(
        {"task_id": "fixture-task"}, task.parent, tmp_path / "work", "job",
        baseline_request_path=request,
    ) as selected:
        overlay = (selected / "fixture-task/environment/Dockerfile").read_text()
        assert json.loads(request.read_text())["build_origin_proof"] is True
        assert overlay.index("git remote get-url origin") < overlay.index("git remote remove origin")
        command = overlay.split("RUN ", 1)[1]
        # Replace only transport in the fixture: preserve the original
        # clone -> checkout declared prefix -> remove-origin build ordering.
        command = command.replace(
            "git clone https://example.invalid/project/source .",
            "git clone " + shlex.quote(str(repo)) + " . && "
            "git remote set-url origin https://example.invalid/project/source",
        )
        command = command.replace(SOURCE_ORIGIN_PROOF, shlex.quote(str(tmp_path / "origin-proof")))
        command = command.replace(SOURCE_COMMIT_PROOF, shlex.quote(str(tmp_path / "commit-proof")))
        built = tmp_path / "built"
        built.mkdir()
        subprocess.run(["sh", "-c", command], cwd=built, check=True)
        assert not subprocess.check_output(["git", "-C", str(built), "remote"], text=True).strip()
        assert (tmp_path / "commit-proof").read_text().strip() == full
        asyncio.run(verify_task_baseline(LocalEnvironment(built)))
        assert json.loads(request.with_suffix(".resolved.json").read_text())["resolved_commit"] == full
    assert dockerfile.read_text() == original


@pytest.mark.parametrize("proof", ["absent", "wrong-origin", "wrong-commit", "not-enabled"])
def test_removed_origin_requires_matching_runner_enabled_build_proof(source, proof):
    repo, git, full, request, configure = source
    configure(full[:7])
    value = json.loads(request.read_text())
    value["build_origin_proof"] = proof != "not-enabled"
    request.write_text(json.dumps(value))
    git("remote", "remove", "origin")
    if proof != "absent":
        (repo.parent / "origin-proof").write_text(
            "https://example.invalid/another/source" if proof == "wrong-origin"
            else "https://example.invalid/project/source"
        )
        (repo.parent / "commit-proof").write_text("0" * 40 if proof == "wrong-commit" else full)
    with pytest.raises(ValueError):
        asyncio.run(verify_task_baseline(LocalEnvironment(repo)))
    assert not request.with_suffix(".resolved.json").exists()


@pytest.mark.parametrize("removal", [" && git remote rm origin", " && git remote remove origin && true"])
def test_unknown_removal_forms_do_not_enable_build_proof(source, tmp_path, removal):
    from dradar.runner import _artifact_tasks_overlay
    _, _, full, request, _ = source
    task = tmp_path / "tasks" / "fixture-task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "' + full[:7] + '"\n'
        'repository_url = "https://example.invalid/project/source"\n'
    )
    original = "FROM fixture\nRUN true \\\n" + removal + "\n"
    (task / "environment/Dockerfile").write_text(original)
    with _artifact_tasks_overlay(
        {"task_id": "fixture-task"}, task.parent, tmp_path / "work", "job",
        baseline_request_path=request,
    ) as selected:
        assert json.loads(request.read_text())["build_origin_proof"] is False
        assert (selected / "fixture-task/environment/Dockerfile").read_text() == original


def test_dockerfile_symlink_cannot_modify_source_outside_overlay(source, tmp_path):
    from dradar.runner import _artifact_tasks_overlay, RunnerError
    _, _, full, request, _ = source
    task = tmp_path / "tasks" / "fixture-task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[metadata]\nbase_commit_hash = "' + full[:7] + '"\n'
        'repository_url = "https://example.invalid/project/source"\n'
    )
    external = tmp_path / "original-Dockerfile"
    original = "FROM fixture\nRUN true \\\n && git remote remove origin\n"
    external.write_text(original)
    (task / "environment/Dockerfile").symlink_to(external)
    with pytest.raises(RunnerError, match="escapes"):
        with _artifact_tasks_overlay(
            {"task_id": "fixture-task"}, task.parent, tmp_path / "work", "job",
            baseline_request_path=request,
        ):
            pass
    assert external.read_text() == original
