"""Save a private immutable artifact snapshot before preparing result upload."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

class ArtifactError(RuntimeError):
    pass

def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ArtifactError("invalid artifact scope")
    return value

def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

class Artifacts:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ArtifactError("artifact root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, assignment_id: str, execution_id: str, sources: dict[str, Path]) -> dict:
        scope = self.root / _id(assignment_id)
        execution_id = _id(execution_id)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in {".", "..", "manifest.json"} for name in sources):
            raise ArtifactError("named output files required")
        if scope.exists() or scope.is_symlink():
            # Never overwrite completed evidence, even after a caller crashes.
            return self.inspect(assignment_id, execution_id)
        stage = Path(tempfile.mkdtemp(prefix=".saving-", dir=self.root))
        records = []
        try:
            for name, source in sorted(sources.items()):
                source = Path(source)
                # Reject lexical symlinks in the source ancestry as well as the leaf.
                if any(p.is_symlink() for p in (source, *source.parents)):
                    raise ArtifactError("source must not contain symlinks")
                fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "rb") as reader:
                    if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                        raise ArtifactError("source is not a regular file")
                    with (stage / name).open("xb") as writer:
                        os.chmod(stage / name, 0o600)
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            writer.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                records.append({"name": name, "sha256": digest.hexdigest(), "size": size})
            manifest = {"assignment_id": assignment_id, "execution_id": execution_id, "files": records}
            with (stage / "manifest.json").open("x", encoding="utf-8") as writer:
                os.chmod(stage / "manifest.json", 0o600)
                json.dump(manifest, writer, sort_keys=True, separators=(",", ":"))
                writer.flush()
                os.fsync(writer.fileno())
            _sync_dir(stage)
            try:
                stage.rename(scope)
            except FileExistsError:
                return self.inspect(assignment_id, execution_id)
            _sync_dir(self.root)
            return manifest
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def inspect(self, assignment_id: str, execution_id: str) -> dict:
        scope = self.root / _id(assignment_id)
        _id(execution_id)
        if scope.is_symlink() or not scope.is_dir() or (scope / "manifest.json").is_symlink():
            raise ArtifactError("preserved artifact scope is unavailable")
        try:
            manifest = json.loads((scope / "manifest.json").read_text())
            if manifest["assignment_id"] != assignment_id or manifest["execution_id"] != execution_id:
                raise ArtifactError("artifact ownership mismatch")
            if not isinstance(manifest["files"], list) :
                raise ArtifactError("artifact manifest missing files")
            names = []
            for record in manifest["files"]:
                name = record["name"]
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or name in {".", "..", "manifest.json"}:
                    raise ArtifactError("invalid preserved filename")
                names.append(name)
                path = scope / name
                if path.is_symlink() or not path.is_file():
                    raise ArtifactError("preserved output is unavailable")
                with path.open("rb") as reader:
                    digest = hashlib.file_digest(reader, "sha256").hexdigest()
                if path.stat().st_size != record["size"] or digest != record["sha256"]:
                    raise ArtifactError("preserved artifact changed")
            if len(names) != len(set(names)):
                raise ArtifactError("duplicate preserved file")
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ArtifactError("cannot verify preserved artifacts") from exc
