"""Durable raw evidence, separate scrubbed upload copy and v0 result digest."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import json
import os
import uuid
from .artifacts import Artifacts, ArtifactError
from .protocol import result_hash, owner
from ..scrub import redact_patch_secrets, scan_secrets, patch_structure_is_valid, scrub_json_bytes, scrub_text

FILE_TYPES = {"patch": "text/x-diff", "trajectory": "application/json", "trajectory_bundle": "application/json", "runner_result": "application/json"}

@dataclass(frozen=True)
class Completion:
    outcome: str
    exit_confirmed: bool
    files: dict[str, Path] = field(default_factory=dict)
    completed_at: str | None = None
    elapsed_ms: int | None = None
    tokens: dict = field(default_factory=lambda: {"input": None, "output": None, "total": None, "source": None, "missing_reason": "usage_unavailable"})
    failure: dict | None = None

def save_completion(root: Path, assignment: dict, execution_id: str, value: Completion) -> dict:
    aid = assignment["assignment_id"]
    if value.outcome not in {"completed", "failed", "interrupted"} or type(value.exit_confirmed) is not bool:
        raise ArtifactError("invalid completion outcome")
    if set(value.files) - FILE_TYPES.keys():
        raise ArtifactError("unknown upload artifact")
    if value.outcome == "completed" and "patch" not in value.files:
        raise ArtifactError("completed result requires patch")
    if value.elapsed_ms is not None and (type(value.elapsed_ms) is not int or value.elapsed_ms < 0):
        raise ArtifactError("invalid measured elapsed time")
    if set(value.tokens) != {"input", "output", "total", "source", "missing_reason"}:
        raise ArtifactError("token observation fields required")
    for key in ("input", "output", "total"):
        n = value.tokens[key]
        if n is not None and (type(n) is not int or n < 0):
            raise ArtifactError("invalid observed token count")
    if any(value.tokens[k] is None for k in ("input", "output", "total")) and not value.tokens["missing_reason"]:
        raise ArtifactError("missing usage requires a reason")
    raw = Artifacts(root / "raw")
    raw.save(aid, execution_id, value.files)
    if not value.exit_confirmed:
        raise ArtifactError("process exit unknown; evidence retained and slot blocked")
    upload = Artifacts(root / "upload")
    with tempfile.TemporaryDirectory(prefix=".scrub-", dir=root) as directory:
        sanitized = {}
        for name in sorted(value.files):
            data = (raw.root / aid / name).read_bytes()
            if name == "patch":
                data, _, residual = redact_patch_secrets(data)
                if residual or scan_secrets(data) or (data and not patch_structure_is_valid(data)):
                    raise ArtifactError("patch upload quarantined; raw evidence retained")
            else:
                data = scrub_json_bytes(data)
            target = Path(directory) / name
            target.write_bytes(data)
            sanitized[name] = target
        manifest = upload.save(aid, execution_id, sanitized)
    records = [{"name": f["name"], "sha256": f["sha256"], "size_bytes": f["size"], "content_type": FILE_TYPES[f["name"]]} for f in manifest["files"]]
    failure = value.failure
    if failure is not None:
        if set(failure) != {"code", "message"} or not all(isinstance(x, str) for x in failure.values()):
            raise ArtifactError("structured failure required")
        failure = {"code": scrub_text(failure["code"]), "message": scrub_text(failure["message"])}
    payload = {**owner(assignment), "execution_id": execution_id,
               "outcome": value.outcome, "exit_confirmed": True, "completed_at": value.completed_at,
               "elapsed_ms": value.elapsed_ms, "tokens": value.tokens,
               "failure": failure, "artifacts": records}
    payload["result_sha256"] = result_hash(payload)
    metadata = root / "metadata"
    metadata.mkdir(mode=0o700, exist_ok=True)
    destination = metadata / (aid + ".json")
    record = {"assignment_id": aid, "payload": payload}
    raw_meta = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if destination.exists():
        if destination.is_symlink() or destination.read_text() != raw_meta:
            raise ArtifactError("durable completion metadata conflict")
    else:
        temporary = metadata / (".saving-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as output:
                output.write(raw_meta)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            from .artifacts import _sync_dir
            _sync_dir(metadata)
        finally:
            temporary.unlink(missing_ok=True)
    return payload

def upload_files(root: Path, assignment_id: str, payload: dict) -> dict[str, Path]:
    store = Artifacts(root / "upload")
    manifest = store.inspect(assignment_id, payload["execution_id"])
    expected = {f["name"]: (f["sha256"], f["size_bytes"]) for f in payload["artifacts"]}
    actual = {f["name"]: (f["sha256"], f["size"]) for f in manifest["files"]}
    if expected != actual or result_hash(payload) != payload["result_sha256"]:
        raise ArtifactError("saved upload metadata does not match immutable artifacts")
    return {name: store.root / assignment_id / name for name in actual}


def recover_completion(root: Path, a: dict, execution_id: str) -> dict | None:
    """Recover the metadata-rename/journal-commit crash window, without execute."""
    path = root / "metadata" / (a["assignment_id"] + ".json")
    if not path.exists():
        return None
    if path.is_symlink():
        raise ArtifactError("completion metadata is a symlink")
    try:
        record = json.loads(path.read_text())
        payload = record["payload"]
        if (record["assignment_id"] != a["assignment_id"] or payload["execution_id"] != execution_id
                or any(payload[k] != v for k, v in owner(a).items())):
            raise ArtifactError("completion metadata ownership mismatch")
        upload_files(root, a["assignment_id"], payload)
        return payload
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ArtifactError("durable completion metadata unavailable") from exc
