import json

from dradar import capacity


class Client:
    def __init__(self, held=5, limit=5):
        self.held = held
        self.limit = limit

    def whoami(self):
        return {"concurrent_limit": self.limit}

    def get_assignment(self):
        return {"active": [object()] * self.held}


class Disk:
    def __init__(self, free_gib):
        self.free = free_gib * 1024 ** 3


def _docker(monkeypatch, *, cpus=8, memory_gib=16):
    monkeypatch.setattr(capacity.shutil, "which", lambda _name: "/usr/bin/docker")
    payload = json.dumps({"NCPU": cpus, "MemTotal": memory_gib * 1024 ** 3})
    monkeypatch.setattr(
        capacity.subprocess, "run",
        lambda *_a, **_k: type("Proc", (), {"returncode": 0, "stdout": payload})(),
    )


def test_recommendation_uses_docker_memory_not_just_cpu(monkeypatch):
    _docker(monkeypatch, cpus=8, memory_gib=16)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(100))

    report = capacity.inspect_capacity(Client())

    assert report.cpu_limit == 4
    assert report.memory_limit == 2
    assert report.recommended_workers == 2


def test_large_machine_auto_recommendation_stays_conservative(monkeypatch):
    _docker(monkeypatch, cpus=64, memory_gib=128)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(1000))

    report = capacity.inspect_capacity(Client(held=20, limit=20))

    assert report.recommended_workers == capacity.AUTO_WORKER_CAP == 4


def test_account_and_requested_task_limits_are_hard_bounds(monkeypatch):
    _docker(monkeypatch, cpus=64, memory_gib=128)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(1000))

    report = capacity.inspect_capacity(Client(held=0, limit=3), requested_tasks=2)

    assert report.recommended_workers == 2


def test_missing_docker_fails_closed_to_one_worker(monkeypatch):
    monkeypatch.setattr(capacity.shutil, "which", lambda _name: None)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(1000))

    report = capacity.inspect_capacity(Client())

    assert report.recommended_workers == 1
    assert "falling back to 1 worker" in report.warnings[0]


def test_worker_resource_warnings_cover_cpu_and_memory_shortfalls():
    warnings = capacity.worker_resource_warnings(3, 4, 12)

    assert any("reserve 6 Docker CPU" in warning for warning in warnings)
    assert any("reserve 20 GiB Docker memory" in warning for warning in warnings)


def test_worker_resource_warnings_accept_exact_published_budget():
    assert capacity.worker_resource_warnings(2, 4, 14) == ()


def test_vfs_driver_reserves_one_worker_and_warns(monkeypatch):
    payload = json.dumps({
        "NCPU": 8, "MemTotal": 32 * 1024 ** 3, "Driver": "vfs",
    })
    monkeypatch.setattr(capacity.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        capacity.subprocess, "run",
        lambda *_a, **_k: type("Proc", (), {"returncode": 0, "stdout": payload})(),
    )
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(100))

    report = capacity.inspect_capacity(Client())

    assert report.docker_driver == "vfs"
    assert report.first_worker_disk_gib == capacity.VFS_FIRST_WORKER_DISK_GIB
    assert report.disk_limit == 1
    assert report.recommended_workers == 1
    assert any("vfs" in warning for warning in report.warnings)


def test_low_disk_space_never_recommends_zero(monkeypatch):
    _docker(monkeypatch, cpus=64, memory_gib=128)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _path: Disk(5))

    report = capacity.inspect_capacity(Client())

    assert report.disk_limit == 1
    assert report.recommended_workers == 1


def test_capacity_command_prints_machine_and_account_summary(monkeypatch, capsys):
    report = capacity.CapacityReport(
        recommended_workers=2, docker_cpus=8, docker_memory_gib=16,
        disk_free_gib=100, account_limit=5, held_tasks=3, task_limit=3,
        cpu_limit=4, memory_limit=2, disk_limit=7,
    )
    monkeypatch.setattr(capacity, "inspect_capacity", lambda *_a, **_k: report)
    monkeypatch.setattr("dradar.identity._client", lambda _cfg: object())
    monkeypatch.setattr("dradar.local_config._load_config", lambda: {})

    assert capacity.cmd_capacity(object()) == 0
    out = capsys.readouterr().out
    assert "8 CPU / 16.0 GiB" in out
    assert "Docker storage driver: unknown" in out
    assert "CPU: 4 worker(s)" in out
    assert "memory: 2 worker(s)" in out
    assert "limiting constraint(s): memory" in out
    assert "recommended workers: 2" in out


def test_standalone_capacity_is_not_limited_by_one_held_task(monkeypatch):
    seen = []
    report = capacity.CapacityReport(
        recommended_workers=3, docker_cpus=12, docker_memory_gib=24,
        disk_free_gib=100, account_limit=5, held_tasks=1, task_limit=4,
        cpu_limit=6, memory_limit=3, disk_limit=7,
    )
    monkeypatch.setattr(
        capacity, "inspect_capacity",
        lambda _client, requested_tasks=None: seen.append(requested_tasks) or report,
    )
    monkeypatch.setattr("dradar.identity._client", lambda _cfg: object())
    monkeypatch.setattr("dradar.local_config._load_config", lambda: {})

    assert capacity.cmd_capacity(object()) == 0
    assert seen == [capacity.AUTO_WORKER_CAP]
