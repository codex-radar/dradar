import gzip
import hashlib
import io
import tarfile
from pathlib import PureWindowsPath

import pytest

from dradar import taskpacks
from dradar.taskpacks import MARKER, TaskPackError, ensure_benchmark_task_pack


@pytest.mark.parametrize(
    "member_name",
    [
        r"..\escape.txt",
        r"sub\..\..\escape.txt",
        r"C:\absolute.txt",
        r"C:drive-relative.txt",
        r"\rooted.txt",
        r"\\server\share\escape.txt",
        r"\\?\C:\device-absolute.txt",
        r"\\.\PIPE\device-name",
        "C:/drive-absolute.txt",
    ],
)
def test_safe_target_rejects_windows_path_semantics(member_name):
    # PureWindowsPath makes this test exercise Windows joining semantics on
    # every CI host, including Linux/macOS runners.
    windows_root = PureWindowsPath(r"C:\safe\tasks")

    with pytest.raises(TaskPackError, match="unsafe path"):
        taskpacks._safe_target(windows_root, member_name)


_WINDOWS_RESERVED_NAMES = [
    "CON", "con.ext", "PRN", "prn.log", "AUX", "aux.txt", "NUL", "nul.txt",
    "CONIN$", "conin$.txt", "CONOUT$", "conout$.log", "CLOCK$", "clock$.txt",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"com{index}.log" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"lpt{index}.log" for index in range(1, 10)),
    "COM¹", "COM².txt", "com³.log", "LPT¹", "LPT².txt", "lpt³.log",
]


@pytest.mark.parametrize(
    "member_name",
    [
        *_WINDOWS_RESERVED_NAMES,
        *(f"task/{name}" for name in _WINDOWS_RESERVED_NAMES),
        "task/NUL .txt",
        "task/COM1...log",
        "task/file.txt.",
        "task/trailing-space ",
        "task/file.txt:alternate-stream",
        "task/file::$DATA",
        "task/question?.txt",
        "task/bad<name",
        "task/bad>name",
        'task/bad"name',
        "task/bad|name",
        "task/bad*name",
        "task/control\x00.txt",
        "task/control\x01.txt",
        "task/control\x1f.txt",
        "task/control\x7f.txt",
    ],
)
def test_safe_target_rejects_windows_reserved_components(member_name, tmp_path):
    with pytest.raises(TaskPackError, match="unsafe path"):
        taskpacks._safe_target(tmp_path / "tasks", member_name)


@pytest.mark.parametrize(
    "member_name",
    [
        "task/task.toml",
        "nested/path/file-name_1.2.txt",
        "unicode/任务说明.md",
        "dotfile/.config",
        "devices/COM0",
        "devices/COM0.txt",
        "devices/COM10",
        "devices/LPT0.txt",
        "devices/LPT10.txt",
        "ordinary/CLOCK.txt",
        "ordinary/dollar$.txt",
    ],
)
def test_safe_target_keeps_portable_posix_members(member_name, tmp_path):
    root = tmp_path / "tasks"

    target = taskpacks._safe_target(root, member_name)

    assert target == root.joinpath(*member_name.split("/"))


def _archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _special_archive(entry_type: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            info = tarfile.TarInfo("task/escape")
            info.type = entry_type
            if entry_type in {tarfile.LNKTYPE, tarfile.SYMTYPE}:
                info.linkname = "../outside"
            tar.addfile(info)
    return output.getvalue()


class FakeClient:
    def __init__(self, payload: bytes, digest: str | None = None):
        self.payload = payload
        self.digest = digest or hashlib.sha256(payload).hexdigest()
        self.downloads = 0

    def benchmarks(self):
        return {"benchmarks": [{
            "id": "pompeii-adjacency",
            "title": "Pompeii",
            "task_bundle": {
                "url": "/api/v1/benchmark-bundles/pompeii-adjacency",
                "sha256": self.digest,
                "bytes": len(self.payload),
            },
        }]}

    def download(self, _url, destination):
        self.downloads += 1
        destination.write_bytes(self.payload)
        return self.digest


def test_task_pack_install_is_atomic_verified_and_idempotent(tmp_path):
    payload = _archive({
        "pompeii-adjacency-rp-002/task.toml": b"[task]\n",
        "pompeii-adjacency-rp-002/instruction.md": b"recover adjacency\n",
    })
    client = FakeClient(payload)
    root = tmp_path / "benchmarks" / "pompeii-adjacency" / "tasks"

    assert ensure_benchmark_task_pack(client, "pompeii-adjacency", root) is True
    assert (root / "pompeii-adjacency-rp-002" / "task.toml").is_file()
    assert (root / MARKER).is_file()
    assert ensure_benchmark_task_pack(client, "pompeii-adjacency", root) is False
    assert client.downloads == 1


def test_task_pack_checksum_upgrade_atomically_replaces_managed_pack(tmp_path):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    new = FakeClient(_archive({"task/new.txt": b"new"}))
    assert ensure_benchmark_task_pack(new, "pompeii-adjacency", root) is True

    assert not (root / "task" / "old.txt").exists()
    assert (root / "task" / "new.txt").read_bytes() == b"new"
    assert new.downloads == 1


def test_task_pack_upgrade_checksum_failure_preserves_installed_pack(tmp_path):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    bad = FakeClient(_archive({"task/new.txt": b"new"}), digest="0" * 64)
    with pytest.raises(TaskPackError, match="checksum mismatch"):
        ensure_benchmark_task_pack(bad, "pompeii-adjacency", root)

    assert (root / "task" / "old.txt").read_bytes() == b"old"
    assert not (root / "task" / "new.txt").exists()


def test_task_pack_never_replaces_unmanaged_existing_directory(tmp_path):
    root = tmp_path / "tasks"
    root.mkdir()
    (root / "user-file").write_text("keep")
    client = FakeClient(_archive({"task/new.txt": b"new"}))

    with pytest.raises(TaskPackError, match="already exists"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)

    assert (root / "user-file").read_text() == "keep"
    assert client.downloads == 0


def test_task_pack_checksum_mismatch_leaves_no_partial_install(tmp_path):
    payload = _archive({"task/task.toml": b"x"})
    client = FakeClient(payload, digest="0" * 64)
    root = tmp_path / "tasks"
    with pytest.raises(TaskPackError, match="checksum mismatch"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)
    assert not root.exists()


def test_task_pack_rejects_path_traversal(tmp_path):
    payload = _archive({"../escape": b"no"})
    client = FakeClient(payload)
    root = tmp_path / "tasks"
    with pytest.raises(TaskPackError, match="unsafe path"):
        ensure_benchmark_task_pack(client, "pompeii-adjacency", root)
    assert not (tmp_path / "escape").exists()
    assert not root.exists()


@pytest.mark.parametrize(
    "member_name",
    [
        r"..\escape.txt",
        "C:/escape.txt",
        "task/NUL.txt",
        "task/file.txt:stream",
        "task/trailing. ",
        "task/control\x1f.txt",
    ],
)
def test_task_pack_rejects_cross_platform_member_and_preserves_installed_pack(
    member_name, tmp_path,
):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    unsafe = FakeClient(_archive({member_name: b"unsafe"}))
    with pytest.raises(TaskPackError, match="unsafe path"):
        ensure_benchmark_task_pack(unsafe, "pompeii-adjacency", root)

    assert (root / "task" / "old.txt").read_bytes() == b"old"


@pytest.mark.parametrize(
    "entry_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ],
)
def test_task_pack_rejects_non_file_entries_and_preserves_installed_pack(
    entry_type, tmp_path,
):
    old = FakeClient(_archive({"task/old.txt": b"old"}))
    root = tmp_path / "tasks"
    assert ensure_benchmark_task_pack(old, "pompeii-adjacency", root) is True

    unsafe = FakeClient(_special_archive(entry_type))
    with pytest.raises(TaskPackError, match="non-file entry"):
        ensure_benchmark_task_pack(unsafe, "pompeii-adjacency", root)

    assert (root / "task" / "old.txt").read_bytes() == b"old"
    assert not (tmp_path / "escape").exists()
