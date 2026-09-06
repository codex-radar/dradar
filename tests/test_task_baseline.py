import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dradar.task_baseline import BASELINE_REQUEST_ENV, REMOTE_BASELINE, verify_task_baseline


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
