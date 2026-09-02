import argparse
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

from dradar import image_cache, local_config, runloop
import pytest


PROJECT = "some-task__abc1234"
MAIN_REF = f"{PROJECT}-main:latest"
PROXY_REF = f"{PROJECT}-pier-egress-proxy:latest"


def _inspect(reference=MAIN_REF, *, project=PROJECT, service="main", image_id="sha256:abc"):
    return {
        "Id": image_id,
        "Created": "2026-07-20T00:00:00Z",
        "Size": 2 * image_cache.GIB,
        "RepoTags": [reference],
        "Config": {"Labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": service,
            "com.docker.compose.version": "2.0",
        }},
    }


def _image(reference=MAIN_REF, *, project=PROJECT, service="main",
           image_id="sha256:abc", size=2 * image_cache.GIB, containers=0):
    return image_cache.DockerImage(
        reference, image_id, project, service, size, containers,
        "2026-07-20T00:00:00Z",
    )


def test_discovery_requires_matching_compose_labels_and_exact_tag(monkeypatch):
    bad_ref = "unrelated-main:latest"
    monkeypatch.setattr(image_cache, "_inventory_rows", lambda: {
        MAIN_REF: {"ID": "sha256:abc", "UniqueSize": "2GB", "Containers": "0"},
        bad_ref: {"ID": "sha256:def", "UniqueSize": "9GB", "Containers": "0"},
    })
    monkeypatch.setattr(image_cache, "_inspect", lambda _refs: {
        MAIN_REF: _inspect(),
        bad_ref: _inspect(bad_ref, project="unrelated", image_id="sha256:def"),
    })

    found = image_cache.discover_pier_images()

    assert set(found) == {MAIN_REF}
    assert found[MAIN_REF].unique_size == 2_000_000_000


def test_record_trial_images_persists_only_valid_current_refs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd, **kwargs):
        reference = cmd[-1]
        if reference == MAIN_REF:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([_inspect()]), "")
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    count = image_cache.record_trial_images(
        tmp_path, assignment_id="a1", task_id="some-task", trial_name=PROJECT,
    )

    assert count == 1
    records = image_cache.load(tmp_path)
    assert records[MAIN_REF]["image_id"] == "sha256:abc"
    assert records[MAIN_REF]["assignment_id"] == "a1"
    assert records[MAIN_REF]["task_id"] == "some-task"


def test_periodic_maintenance_claim_is_shared_and_throttled(tmp_path: Path):
    assert image_cache.claim_periodic_maintenance(
        tmp_path, interval_seconds=900, now=1_000,
    )
    assert not image_cache.claim_periodic_maintenance(
        tmp_path, interval_seconds=900, now=1_899,
    )
    assert image_cache.claim_periodic_maintenance(
        tmp_path, interval_seconds=900, now=1_900,
    )


def _snapshotter_from(command):
    for index, part in enumerate(command):
        if part == "--buildkitd-flags" and index + 1 < len(command):
            value = command[index + 1]
            prefix = "--oci-worker-snapshotter="
            if value.startswith(prefix):
                return value[len(prefix):]
    return None


def test_trial_builder_is_assignment_scoped_and_never_selected_globally(
    tmp_path: Path, monkeypatch,
):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["buildx", "inspect"] and "--bootstrap" not in command:
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(image_cache, "running_in_container", lambda: False)
    monkeypatch.setattr(image_cache, "_run_docker", run)
    lease = image_cache.prepare_trial_builder(
        tmp_path, assignment_id="a1", runtime={},
    )

    name = image_cache.trial_builder_name(tmp_path, "a1")
    creates = [command for command in calls if command[:2] == ["buildx", "create"]]
    assert lease.isolated and lease.name == name
    assert creates == [[
        "buildx", "create", "--name", name,
        "--driver", "docker-container",
        "--buildkitd-flags", "--oci-worker-snapshotter=overlayfs",
    ]]
    assert "--use" not in creates[0]
    assert image_cache.ISOLATED_SNAPSHOTTERS == ("overlayfs", "fuse-overlayfs")


def test_shared_trial_builder_is_bootstrapped_and_reusable(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("DRADAR_SHARED_BUILDER_STATE_DIR", str(tmp_path / "shared-state"))
    calls = []
    builder_exists = False

    def run(command, **_kwargs):
        nonlocal builder_exists
        calls.append(command)
        if command[:2] == ["buildx", "inspect"] and "--bootstrap" not in command:
            if not builder_exists:
                return subprocess.CompletedProcess(command, 1, "", "not found")
            return subprocess.CompletedProcess(command, 0, "Name: shared\nStatus: running\n", "")
        if command[:2] == ["buildx", "create"]:
            builder_exists = True
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(image_cache, "running_in_container", lambda: False)
    monkeypatch.setattr(image_cache, "_run_docker", run)
    lease = image_cache.prepare_trial_builder(
        tmp_path, assignment_id="a1", runtime={}, mode="shared",
    )

    assert lease.isolated and lease.reusable
    assert lease.name == image_cache.shared_builder_name(tmp_path)
    # Fleet gives harnesses separate DRADAR_HOME paths, but the shared cache
    # intentionally crosses those paths within one OS user's Docker context.
    assert image_cache.shared_builder_name(tmp_path / "other-harness") == lease.name
    assert calls[-1] == ["buildx", "inspect", lease.name, "--bootstrap"]
    calls.clear()
    second = image_cache.prepare_trial_builder(
        tmp_path / "other-harness", assignment_id="a2", runtime={}, mode="shared",
    )
    assert second.name == lease.name and second.reusable
    assert [command for command in calls if "--bootstrap" in command] == []
    assert image_cache.remove_trial_builder(
        tmp_path, "a1", mode="shared",
    ) == (True, None)
    # Shared cleanup must not remove the persistent BuildKit cache.
    assert calls[-1] == ["buildx", "inspect", lease.name]


def test_trial_builder_falls_back_to_fuse_overlayfs_when_overlayfs_cannot_boot(
    tmp_path: Path, monkeypatch,
):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["buildx", "inspect"] and "--bootstrap" not in command:
            return subprocess.CompletedProcess(command, 1, "", "not found")
        if _snapshotter_from(command) == "overlayfs":
            return subprocess.CompletedProcess(command, 1, "", "overlayfs not supported")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(image_cache, "running_in_container", lambda: False)
    monkeypatch.setattr(image_cache, "_run_docker", run)
    lease = image_cache.prepare_trial_builder(
        tmp_path, assignment_id="a1", runtime={},
    )

    creates = [command for command in calls if command[:2] == ["buildx", "create"]]
    assert lease.isolated and lease.name == image_cache.trial_builder_name(tmp_path, "a1")
    assert [_snapshotter_from(command) for command in creates] == [
        "overlayfs", "fuse-overlayfs",
    ]


def test_trial_builder_uses_host_default_instead_of_native_copies(
    tmp_path: Path, monkeypatch,
):
    def run(command, **_kwargs):
        if command[:2] == ["buildx", "create"]:
            return subprocess.CompletedProcess(command, 1, "", "no copy-on-write snapshotter")
        if command[:2] == ["buildx", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "not found")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(image_cache, "running_in_container", lambda: False)
    monkeypatch.setattr(image_cache, "_run_docker", run)
    lease = image_cache.prepare_trial_builder(
        tmp_path, assignment_id="a1", runtime={},
    )

    assert not lease.isolated and lease.name is None
    assert not lease.expected
    assert "叠加文件系统" in lease.note
    assert "默认构建空间" in lease.note or "叠加文件系统" in lease.note


def test_trial_builder_skips_isolated_volume_inside_a_container(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(image_cache, "running_in_container", lambda: True)
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not create builder")),
    )
    lease = image_cache.prepare_trial_builder(
        tmp_path, assignment_id="a1", runtime={},
    )

    assert not lease.isolated and lease.name is None
    assert not lease.expected
    assert "容器内" in lease.note


def test_task_cleanup_allows_intentional_default_builder(
    tmp_path: Path, monkeypatch,
):
    assignment_id = "a1"
    job_dir = tmp_path / "work" / "jobs" / "aa1"
    (job_dir / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(
        image_cache, "_remove_project_runtime", lambda *_a: (0, 0, 0),
    )
    monkeypatch.setattr(
        image_cache, "remove_assignment_images",
        lambda *_a, **_k: (0, 0, None),
    )
    monkeypatch.setattr(
        image_cache, "remove_trial_builder", lambda *_a, **_k: (True, None),
    )

    result = image_cache.cleanup_trial_resources(
        tmp_path,
        assignment_id=assignment_id,
        job_dir=job_dir,
        trial_name=PROJECT,
        builder_isolated=False,
        builder_expected=False,
    )

    assert result.success


def test_shared_build_cache_prune_targets_only_dradar_builder(
    tmp_path: Path, monkeypatch,
):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "reclaimed 2GB", "")

    monkeypatch.setattr(image_cache, "_run_docker", run)
    ok, note = image_cache.prune_shared_build_cache(
        tmp_path, max_used_bytes=50 * image_cache.GIB,
    )

    name = image_cache.shared_builder_name(tmp_path)
    assert ok and "2GB" in note
    assert calls == [
        ["buildx", "inspect", name],
        [
            "buildx", "prune", "--builder", name, "--force", "--all",
            "--max-used-space", str(50 * image_cache.GIB),
        ],
    ]


def test_trial_builder_falls_back_without_exposing_loopback_proxy(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not call Docker")),
    )
    lease = image_cache.prepare_trial_builder(
        tmp_path,
        assignment_id="a1",
        runtime={
            "DRADAR_EGRESS_UPSTREAM_HOST": "host.docker.internal",
            "DRADAR_EGRESS_BUILD_PROXY": "http://secret@host.docker.internal:7890",
        },
    )

    assert not lease.isolated and lease.name is None
    assert "代理" in lease.note


def test_wsl_host_disk_space_is_read_from_distribution_storage_drive(
    monkeypatch,
):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/mnt/c/powershell.exe")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, str(80 * image_cache.GIB), "")

    monkeypatch.setattr(image_cache.subprocess, "run", run)

    assert image_cache.wsl_host_disk_free_bytes() == 80 * image_cache.GIB
    assert seen["env"]["WSL_DISTRO_NAME"] == "Ubuntu"
    assert "BasePath" in seen["command"][-1]


def test_wsl_refill_stops_when_windows_host_disk_is_low(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(image_cache, "is_wsl", lambda: True)
    monkeypatch.setattr(
        image_cache, "disk_free_bytes",
        lambda _home: (100 * image_cache.GIB, 10 * image_cache.GIB),
    )

    allowed, reason = image_cache.disk_allows_new_tasks(
        tmp_path, min_free_bytes=25 * image_cache.GIB,
    )

    assert not allowed
    assert "Windows" in reason


def test_wsl_refill_fails_closed_when_host_disk_cannot_be_checked(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(image_cache, "is_wsl", lambda: True)
    monkeypatch.setattr(
        image_cache, "disk_free_bytes",
        lambda _home: (100 * image_cache.GIB, None),
    )

    allowed, reason = image_cache.disk_allows_new_tasks(
        tmp_path, min_free_bytes=25 * image_cache.GIB,
    )

    assert not allowed
    assert "无法确认" in reason


def test_remove_trial_builder_deletes_only_deterministic_assignment_builder(
    tmp_path: Path, monkeypatch,
):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(image_cache, "_run_docker", run)
    assert image_cache.remove_trial_builder(tmp_path, "a1") == (True, None)
    name = image_cache.trial_builder_name(tmp_path, "a1")
    assert calls == [
        ["buildx", "inspect", name],
        ["buildx", "rm", name],
    ]


def test_invalid_trial_name_never_queries_docker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not query Docker")),
    )
    assert image_cache.record_trial_images(
        tmp_path, assignment_id="a1", task_id="t", trial_name="test-fixture",
    ) == 0


def test_cleanup_plan_protects_active_and_container_images_and_legacy_is_opt_in(
    tmp_path: Path, monkeypatch,
):
    active_ref = MAIN_REF
    safe_ref = "other-task__def5678-main:latest"
    container_ref = "third-task__ghi9012-main:latest"
    legacy_ref = "legacy-task__jkl3456-main:latest"
    images = {
        active_ref: _image(active_ref, image_id="sha256:a"),
        safe_ref: _image(safe_ref, project="other-task__def5678", image_id="sha256:b"),
        container_ref: _image(container_ref, project="third-task__ghi9012",
                              image_id="sha256:c", containers=1),
        legacy_ref: _image(legacy_ref, project="legacy-task__jkl3456", image_id="sha256:d"),
    }
    records = {
        active_ref: {"image_id": "sha256:a", "assignment_id": "active", "last_used_at": "1"},
        safe_ref: {"image_id": "sha256:b", "assignment_id": "settled", "last_used_at": "2"},
        container_ref: {"image_id": "sha256:c", "assignment_id": "settled", "last_used_at": "3"},
    }
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, records)
    monkeypatch.setattr(image_cache, "discover_pier_images", lambda: images)

    normal = image_cache.plan_cleanup(
        tmp_path, protected_assignment_ids={"active"}, include_legacy=False,
    )
    legacy = image_cache.plan_cleanup(
        tmp_path, protected_assignment_ids={"active"}, include_legacy=True,
    )

    assert [item.reference for item in normal.candidates] == [safe_ref]
    assert {item.reference for item in legacy.candidates} == {safe_ref, legacy_ref}
    assert normal.protected == 2
    assert normal.legacy_count == 1


def test_legacy_job_without_checkpoint_still_protects_active_assignment_image(
    tmp_path: Path, monkeypatch,
):
    assignment_id = "a" * 32
    trial_dir = tmp_path / "work" / "jobs" / f"a{assignment_id}" / PROJECT
    trial_dir.mkdir(parents=True)
    monkeypatch.setattr(
        image_cache, "discover_pier_images", lambda: {MAIN_REF: _image()},
    )

    plan = image_cache.plan_cleanup(
        tmp_path,
        protected_assignment_ids={assignment_id},
        include_legacy=True,
    )

    assert plan.candidates == []
    assert plan.protected == 1


def test_remove_prunes_only_matching_ledger_entry(tmp_path: Path, monkeypatch):
    image = _image()
    records = {
        MAIN_REF: {"image_id": image.image_id},
        PROXY_REF: {"image_id": "sha256:proxy"},
    }
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, records)
    monkeypatch.setattr(image_cache, "_remove_one", lambda _image: True)

    removed, reclaimed = image_cache.remove_images(tmp_path, [image])

    assert removed == 1 and reclaimed == image.unique_size
    assert set(image_cache.load(tmp_path)) == {PROXY_REF}


def test_per_task_image_cleanup_avoids_global_prune_and_inventory_scan(
    tmp_path: Path, monkeypatch,
):
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, {
            MAIN_REF: {
                "image_id": "sha256:abc",
                "assignment_id": "a1",
                "project": PROJECT,
            },
        })
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, json.dumps([_inspect()]), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(image_cache, "_run_docker", run)

    removed, reclaimed, note = image_cache.remove_assignment_images(
        tmp_path, assignment_id="a1", project=PROJECT,
    )

    assert (removed, reclaimed, note) == (1, 2 * image_cache.GIB, None)
    assert image_cache.load(tmp_path) == {}
    assert ["image", "rm", MAIN_REF] in calls
    assert all(command[:2] != ["system", "df"] for command in calls)
    assert all("prune" not in command for command in calls)


def test_settled_task_cleanup_removes_only_exact_owned_resources(
    tmp_path: Path, monkeypatch,
):
    assignment_id = "a1"
    job_dir = tmp_path / "work" / "jobs" / "aa1"
    (job_dir / PROJECT).mkdir(parents=True)
    image = _image()
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, {
            MAIN_REF: {
                "image_id": image.image_id,
                "assignment_id": assignment_id,
                "project": PROJECT,
            },
        })
    monkeypatch.setattr(
        image_cache, "_remove_project_runtime", lambda *_a: (1, 1, 1),
    )
    removed = []
    monkeypatch.setattr(
        image_cache, "remove_assignment_images",
        lambda *_a, **_k: (removed.append(image) or 1, image.unique_size, None),
    )
    monkeypatch.setattr(
        image_cache, "remove_trial_builder", lambda *_a: (True, None),
    )

    result = image_cache.cleanup_trial_resources(
        tmp_path,
        assignment_id=assignment_id,
        job_dir=job_dir,
        trial_name=PROJECT,
        builder_isolated=True,
    )

    assert result.success
    assert (result.removed_containers, result.removed_networks,
            result.removed_volumes, result.removed_images) == (1, 1, 1, 1)
    assert removed == [image]


def test_task_cleanup_refuses_a_directory_outside_managed_jobs(
    tmp_path: Path, monkeypatch,
):
    external = tmp_path / "external"
    (external / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(
        image_cache, "_remove_project_runtime",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not touch Docker")),
    )

    result = image_cache.cleanup_trial_resources(
        tmp_path,
        assignment_id="a1",
        job_dir=external,
        trial_name=PROJECT,
        builder_isolated=True,
    )

    assert not result.success
    assert "不属于 DRadar" in result.note


def test_remove_revalidates_id_and_never_uses_force(monkeypatch):
    image = _image()
    calls = []
    monkeypatch.setattr(image_cache, "_inventory_rows", lambda: {
        MAIN_REF: {"ID": image.image_id, "UniqueSize": "2GB", "Containers": "0"},
    })
    monkeypatch.setattr(image_cache, "_inspect", lambda _refs: {MAIN_REF: _inspect()})
    monkeypatch.setattr(
        image_cache, "_run_docker",
        lambda cmd, **_kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    assert image_cache._remove_one(image)
    assert calls == [["image", "rm", MAIN_REF]]
    assert "--force" not in calls[0] and "-f" not in calls[0]


def test_balanced_maintenance_removes_old_owned_images_over_limit(tmp_path: Path, monkeypatch):
    first = _image(size=8 * image_cache.GIB)
    second = _image("other-task__def5678-main:latest", project="other-task__def5678",
                    image_id="sha256:def", size=8 * image_cache.GIB)
    plan = image_cache.CleanupPlan(
        [first, second], {first.reference, second.reference}, 0,
        16 * image_cache.GIB, 16 * image_cache.GIB,
    )
    policy = image_cache.CachePolicy(
        "balanced", 10 * image_cache.GIB, 7 * image_cache.GIB,
        25 * image_cache.GIB, True,
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=400 * image_cache.GIB,
                                   free=100 * image_cache.GIB),
    )
    removed = []
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda _home, images: (removed.extend(images) or len(images),
                               sum(item.unique_size for item in images)),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert result.removed == 2
    assert [item.reference for item in removed] == [first.reference, second.reference]
    assert result.allow_new_claims


def test_metered_mode_never_auto_deletes_and_blocks_claims_under_disk_floor(
    tmp_path: Path, monkeypatch,
):
    image = _image()
    plan = image_cache.CleanupPlan(
        [image], {image.reference}, 0, image.unique_size, 60 * image_cache.GIB,
    )
    policy = image_cache.CachePolicy(
        "metered", 50 * image_cache.GIB, 40 * image_cache.GIB,
        25 * image_cache.GIB, False,
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=490 * image_cache.GIB,
                                   free=10 * image_cache.GIB),
    )
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must preserve cache")),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert not result.allow_new_claims
    assert result.removed == 0
    assert "metered" in result.note


def test_docker_failure_still_blocks_new_claims_when_disk_is_low(
    tmp_path: Path, monkeypatch,
):
    policy = image_cache.CachePolicy(
        "balanced", 50 * image_cache.GIB, 37 * image_cache.GIB,
        25 * image_cache.GIB, True,
    )
    unavailable = image_cache.CleanupPlan(
        [], set(), 0, 0, 0, docker_available=False,
        note="Docker socket unavailable",
    )
    monkeypatch.setattr(image_cache, "effective_policy", lambda *_a: policy)
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: unavailable)
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=500 * image_cache.GIB,
                                   used=490 * image_cache.GIB,
                                   free=10 * image_cache.GIB),
    )

    result = image_cache.automatic_maintenance(
        tmp_path, {}, protected_assignment_ids=set(),
    )

    assert not result.allow_new_claims
    assert "no new task" in result.note


def test_server_state_failure_never_deletes_but_keeps_disk_claim_guard(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        runloop, "_active_by_id",
        lambda _client: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(runloop, "_disk_allows_refill", lambda _cfg: False)
    monkeypatch.setattr(
        image_cache, "automatic_maintenance",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not delete without authoritative server state")
        ),
    )

    assert not runloop._maintain_image_cache(object(), {}, phase="before run")
    output = capsys.readouterr().out
    assert "no Docker image was deleted" in output
    assert "no new task will be claimed" in output


def test_default_policy_is_adaptive_and_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        image_cache, "_policy_min_free_bytes",
        lambda: int(image_cache.DEFAULT_MIN_FREE_GIB * image_cache.GIB),
    )
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=2_000 * image_cache.GIB,
                                   used=0, free=2_000 * image_cache.GIB),
    )
    large = image_cache.effective_policy(tmp_path, {})
    assert large.mode == "balanced"
    assert large.limit_bytes == 50 * image_cache.GIB
    assert large.target_bytes == int(37.5 * image_cache.GIB)

    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=128 * image_cache.GIB,
                                   used=0, free=128 * image_cache.GIB),
    )
    small = image_cache.effective_policy(tmp_path, {})
    assert small.limit_bytes == 20 * image_cache.GIB
    assert small.min_free_bytes == 25 * image_cache.GIB


def test_vfs_policy_raises_the_free_disk_floor(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("dradar.capacity.docker_storage_driver", lambda: "vfs")
    monkeypatch.setattr(
        image_cache.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=128 * image_cache.GIB,
                                   used=0, free=112 * image_cache.GIB),
    )

    policy = image_cache.effective_policy(tmp_path, {})

    assert policy.min_free_bytes == 80 * image_cache.GIB


def test_config_set_preserves_identity_token(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    monkeypatch.setattr(local_config, "CONFIG_PATH", tmp_path / "config.json")
    local_config._save_config({"server": "https://deng.example", "token": "secret-token"})

    assert image_cache.cmd_config_set(argparse.Namespace(
        key="image-cache-mode", value="metered",
    )) == 0

    cfg = local_config._load_config()
    assert cfg["token"] == "secret-token"
    assert cfg["image_cache_mode"] == "metered"


def test_cleanup_docker_dry_run_never_removes_images(tmp_path: Path, monkeypatch, capsys):
    image = _image()
    plan = image_cache.CleanupPlan(
        [image], {image.reference}, 0, image.unique_size, image.unique_size,
    )
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: object())
    monkeypatch.setattr(runloop, "_active_by_id", lambda _client: {})
    monkeypatch.setattr(image_cache, "plan_cleanup", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        image_cache, "remove_images",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dry-run must not delete")),
    )

    args = argparse.Namespace(
        dry_run=True, include_kept=False, docker=True,
        all_task_images=False, yes=True,
    )
    assert runloop.cmd_cleanup(args) == 0
    out = capsys.readouterr().out
    assert MAIN_REF in out and "would remove" in out


def test_shared_build_cache_cleanup_dry_run_is_non_destructive(
    tmp_path: Path, monkeypatch, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    monkeypatch.setattr(runloop, "_load_config", lambda: {})
    monkeypatch.setattr(runloop, "_client", lambda _cfg: object())
    monkeypatch.setattr(runloop, "_active_by_id", lambda _client: {})
    monkeypatch.setattr(
        image_cache, "plan_cleanup",
        lambda *_a, **_k: image_cache.CleanupPlan([], set(), 0, 0, 0),
    )
    monkeypatch.setattr(
        image_cache, "prune_shared_build_cache",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("dry-run must not prune shared cache")
        ),
    )

    args = argparse.Namespace(
        dry_run=True, include_kept=False, docker=True,
        all_task_images=False, shared_build_cache=True, yes=True,
    )
    assert runloop.cmd_cleanup(args) == 0
    out = capsys.readouterr().out
    assert "Shared BuildKit cache" in out and "would prune builder" in out


def test_cleanup_requires_explicit_docker_flag_for_legacy_sweep(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runloop, "HOME", tmp_path)
    args = argparse.Namespace(
        dry_run=True, include_kept=False, docker=False,
        all_task_images=True, yes=True,
    )
    assert runloop.cmd_cleanup(args) == 1


def test_inspect_tolerates_a_missing_tag_inside_a_batch(monkeypatch):
    """A single stale tag must not abort the whole batch inspect.

    ``docker image inspect a b c`` exits non-zero when one reference is gone,
    but still prints valid JSON for the survivors on stdout. The batch loop
    must parse those survivors and never propagate the failure.
    """
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")
    present = MAIN_REF
    missing = "some-task__gone-main:latest"
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        refs = cmd[cmd.index("inspect") + 1:]
        # Batch call: docker exits 1 because ``missing`` is gone, but stdout
        # still carries the JSON for every reference that does exist.
        if len(refs) > 1:
            payload = [_inspect(present)]
            return subprocess.CompletedProcess(cmd, 1, json.dumps(payload), "No such image")
        # Per-reference fallback: the present image resolves, the missing one
        # fails outright and is skipped.
        if refs == [present]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([_inspect(present)]), "")
        return subprocess.CompletedProcess(cmd, 1, "", "No such image")

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    found = image_cache._inspect([present, missing])

    # The present tag survives; the missing one is simply absent, and the
    # batch loop never raised and never aborted on the stale reference.
    assert set(found) == {present}
    # Survivors were parsed straight from the batch stdout, so the missing
    # tag did not force a one-by-one fallback in this path.
    assert calls["n"] == 1


def test_inspect_falls_back_to_per_reference_when_batch_stdout_empty(monkeypatch):
    """When the batch yields no parseable output, inspect each ref alone.

    Some Docker daemon responses surface the per-image metadata only when
    references are queried individually. The fallback must recover every
    surviving image and skip the truly-missing ones without raising.
    """
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")
    present_a = "some-task__aaa-main:latest"
    present_b = "some-task__bbb-main:latest"
    missing = "some-task__gone-main:latest"
    calls = {"refs": []}

    def fake_run(cmd, **kwargs):
        refs = cmd[cmd.index("inspect") + 1:]
        calls["refs"].append(refs)
        if len(refs) > 1:
            # Batch returns nothing usable (empty stdout, non-zero exit).
            return subprocess.CompletedProcess(cmd, 1, "", "No such image")
        if refs == [present_a]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([_inspect(present_a)]), "")
        if refs == [present_b]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([_inspect(present_b)]), "")
        return subprocess.CompletedProcess(cmd, 1, "", "No such image")

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    found = image_cache._inspect([present_a, missing, present_b])

    assert set(found) == {present_a, present_b}
    # The batch was attempted once, then every reference was retried alone.
    assert calls["refs"][0] == [present_a, missing, present_b]
    assert calls["refs"][1:] == [[present_a], [missing], [present_b]]


def test_inspect_raises_on_non_missing_single_reference_error(monkeypatch):
    """A single-reference inspect must propagate real Docker faults.

    ``permission denied`` is not a missing image; swallowing it would let a
    sick daemon masquerade as an empty cache. ``_inspect`` must raise
    ``DockerUnavailable`` instead of returning ``{}``.
    """
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "", "permission denied while accessing docker socket",
        )

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    with pytest.raises(image_cache.DockerUnavailable):
        image_cache._inspect([MAIN_REF])


def test_inspect_raises_on_non_missing_error_during_batch_fallback(monkeypatch):
    """A real Docker fault mid-fallback must not be silently dropped.

    When the batch stdout is empty and we retry references one by one, a
    non-``No such image`` failure on an individual reference is still a real
    fault. It must propagate rather than be skipped as ``continue`` would do
    for a merely-missing tag.
    """
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")
    present = MAIN_REF
    other = "some-task__xyz-main:latest"
    seen = {"n": 0}

    def fake_run(cmd, **kwargs):
        refs = cmd[cmd.index("inspect") + 1:]
        seen["n"] += 1
        if len(refs) > 1:
            # Batch yields nothing usable (empty stdout, non-zero exit).
            return subprocess.CompletedProcess(cmd, 1, "", "No such image")
        # Per-reference: a real daemon fault, not a missing image.
        return subprocess.CompletedProcess(
            cmd, 1, "", "Got permission denied while trying to connect",
        )

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    with pytest.raises(image_cache.DockerUnavailable):
        image_cache._inspect([present, other])
    # The batch was attempted, then the first single reference raised.
    assert seen["n"] == 2


@pytest.mark.parametrize("payload", ("", "[]", "{}", "{not-json"))
def test_inspect_rejects_empty_or_malformed_success_payload(monkeypatch, payload):
    """A successful Docker command must still provide valid image metadata.

    Treating malformed output as an empty inventory would make every ledger
    entry look stale and silently disable future automatic cache maintenance.
    """
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        image_cache.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, payload, ""),
    )

    with pytest.raises(image_cache.DockerUnavailable):
        image_cache._inspect([MAIN_REF])


def test_missing_ok_rejects_mixed_missing_and_real_errors(monkeypatch):
    """A missing tag must not hide a simultaneous real Docker failure."""
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        image_cache.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "No such image: stale\npermission denied while accessing Docker",
        ),
    )

    with pytest.raises(image_cache.DockerUnavailable):
        image_cache._missing_ok_command(["image", "inspect", MAIN_REF])


def test_plan_cleanup_keeps_ledger_when_inspect_fails(monkeypatch, tmp_path: Path):
    """A Docker fault during cleanup planning must not wipe the ledger.

    If ``discover_pier_images`` cannot reach Docker (permission denied, daemon
    down), every record would otherwise look stale and be pruned, silently
    losing the cache ledger on every transient Docker hiccup. The fault must
    propagate so the records survive untouched.
    """
    records = {
        MAIN_REF: {"image_id": "sha256:abc", "assignment_id": "a1", "last_used_at": "1"},
        "some-task__def5678-main:latest": {
            "image_id": "sha256:def", "assignment_id": "a2", "last_used_at": "2",
        },
    }
    with image_cache._ledger_lock(tmp_path):
        image_cache._save_unlocked(tmp_path, records)
    monkeypatch.setattr(image_cache.shutil, "which", lambda _name: "/usr/bin/docker")

    def fake_run(cmd, **kwargs):
        args = [a for a in cmd[1:] if a != "--format"]
        # Inventory (``system df``) still works so we reach the inspect stage.
        # Repository carries the ``-main`` suffix so the reference matches the
        # Pier tag pattern and ``_inspect`` is actually exercised.
        if args and args[0] == "system":
            line = json.dumps({"Images": [{
                "Repository": f"{PROJECT}-main", "Tag": "latest",
                "ID": "sha256:abc", "UniqueSize": "2GB", "Containers": "0",
            }]})
            return subprocess.CompletedProcess(cmd, 0, line, "")
        # Inspect stage fails with a non-missing Docker fault.
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    monkeypatch.setattr(image_cache.subprocess, "run", fake_run)

    plan = image_cache.plan_cleanup(
        tmp_path, protected_assignment_ids=set(), include_legacy=False,
    )

    # The fault surfaces as an unavailable plan, never an empty silent wipe.
    assert plan.docker_available is False
    # The ledger is intact: no record was pruned.
    assert set(image_cache.load(tmp_path)) == set(records)
