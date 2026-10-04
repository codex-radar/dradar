"""Durable v2 requests and a conservative local paid-execution fence.

SQLite commits precede every remote write and every provider invocation.
An unknown request is replayed byte-for-byte. A launch fence is deliberately
never cleared by a restart, lost ACK, missing process, or assignment TTL.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import uuid

class JournalConflict(RuntimeError):
    pass

@dataclass(frozen=True)
class Request:
    operation: str
    request_id: str
    method: str
    path: str
    body_json: str
    response_json: str | None

    @property
    def body(self) -> dict:
        return json.loads(self.body_json)

    @property
    def response(self) -> dict | None:
        return json.loads(self.response_json) if self.response_json is not None else None

class Journal:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise JournalConflict("state directory must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "journal.sqlite3"
        if self.path.is_symlink():
            raise JournalConflict("journal must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    operation TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL, path TEXT NOT NULL, body_json TEXT NOT NULL,
                    response_json TEXT
                );
                CREATE TABLE IF NOT EXISTS executions (
                    assignment_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    seq INTEGER PRIMARY KEY, assignment_id TEXT NOT NULL,
                    v2_execution_id TEXT NOT NULL, audit_execution_id TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def value(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def bind(self, key: str, value: str) -> None:
        """Bind local state to one site/scope; never silently retarget it."""
        with self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row and row[0] != value:
                raise JournalConflict("saved state scope changed")
            db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))

    def identity(self, key: str) -> str:
        """Stable local UUIDs, unrelated to auth or server authorization."""
        with self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if row:
                return row[0]
            value = uuid.uuid4().hex
            db.execute("INSERT INTO metadata VALUES (?,?)", (key, value))
            return value

    def prepare(self, operation: str, path: str, body: dict, *, method: str = "POST") -> Request:
        """Return the saved request; changing its meaning fails closed."""
        if not path.startswith("/api/v2/") or "?" in path or "#" in path:
            raise ValueError("v2 mutation path required")
        if method != "POST" or "request_id" in body:
            raise ValueError("request ID is owned by the journal")
        # JSON roundtrip also rejects NaN and non-JSON values before persisting.
        supplied = json.loads(json.dumps(body, allow_nan=False))
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE operation=?", (operation,)).fetchone()
            if row:
                saved = Request(**dict(row))
                previous = saved.body
                previous.pop("request_id")
                if (saved.path != path or saved.method != method
                        or json.dumps(previous, sort_keys=True, separators=(",", ":"))
                        != json.dumps(supplied, sort_keys=True, separators=(",", ":"))):
                    raise JournalConflict("saved request scope or body changed")
                return saved
            request_id = uuid.uuid4().hex
            exact = {**supplied, "request_id": request_id}
            body_json = json.dumps(exact, sort_keys=True, separators=(",", ":"), allow_nan=False)
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,NULL)",
                       (operation, request_id, method, path, body_json))
            return Request(operation, request_id, method, path, body_json, None)

    def acknowledge(self, request: Request, response: dict) -> None:
        raw = json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE operation=?", (request.operation,)).fetchone()
            if row is None or Request(**dict(row)).body_json != request.body_json:
                raise JournalConflict("request was not durably prepared")
            if row["response_json"] is not None:
                # Replay observations can advance without granting new authorization.
                # Retain the first receipt; compare immutable scope/status strictly.
                def identity(value):
                    value = {k:v for k,v in value.items() if k not in {"server_time","replayed"}}
                    if isinstance(value.get("assignment"),dict):
                        a=value["assignment"]
                        value["assignment"]={k:a[k] for k in ("assignment_id","run_id","device_id","slot_id","lease_id","owner_epoch","execution_id","task") if k in a}
                    if isinstance(value.get("run"),dict):
                        r=value["run"]
                        value["run"]={k:r[k] for k in ("run_id","device_id","benchmark","model","effort","agent","total_count","concurrency") if k in r}
                    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)
                if identity(json.loads(row["response_json"])) != identity(response):
                    raise JournalConflict("receipt identity changed for original request")
                return
            db.execute("UPDATE requests SET response_json=? WHERE operation=?", (raw, request.operation))

    def next_sequence(self, prefix: str) -> int:
        with self._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", ("seq:" + prefix,)).fetchone()
            value = int(row[0]) + 1 if row else 1
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("seq:" + prefix, str(value)))
            return value

    def pending_request(self, prefix: str) -> Request | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM requests WHERE substr(operation,1,?)=? AND response_json IS NULL ORDER BY rowid LIMIT 1", (len(prefix), prefix)).fetchone()
            return Request(**dict(row)) if row else None

    def requests(self) -> list[Request]:
        with self._db() as db:
            return [Request(**dict(row)) for row in db.execute("SELECT * FROM requests ORDER BY rowid")]

    def begin_execution(self, assignment_id: str, execution_id: str) -> bool:
        """Persist the fence BEFORE invoking provider; exactly one caller wins.

        The scheduler must verify the authoritative start ACK first. This
        method is a local safety guard, never an execution authorization.
        """
        with self._db() as db:
            row = db.execute("SELECT * FROM executions WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row:
                if row["execution_id"] != execution_id:
                    raise JournalConflict("assignment already fenced for another execution")
                return False
            db.execute("INSERT INTO executions VALUES (?,?,'launch_fenced',NULL)", (assignment_id, execution_id))
            return True

    def save_result(self, assignment_id: str, execution_id: str, result: dict) -> None:
        """Persist upload metadata only after runtime durably saves artifacts."""
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._db() as db:
            row = db.execute("SELECT * FROM executions WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None or row["execution_id"] != execution_id:
                raise JournalConflict("result has no matching execution fence")
            if row["result_json"] is not None and row["result_json"] != raw:
                raise JournalConflict("preserved result cannot be replaced")
            db.execute("UPDATE executions SET state='result_saved', result_json=? WHERE assignment_id=?", (raw, assignment_id))

    def record_audit(self, assignment_id: str, execution_id: str, event: dict) -> None:
        if (event.get("schema") != "dradar.execution_audit.v1"
                or event.get("scope", {}).get("assignment_id") != assignment_id
                or event.get("scope", {}).get("runner_session_id") != execution_id
                or not isinstance(event.get("execution_id"), str)):
            raise JournalConflict("runtime audit does not match exact v2 execution scope")
        raw = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._db() as db:
            db.execute("INSERT INTO audit_events(assignment_id,v2_execution_id,audit_execution_id,event_json) VALUES (?,?,?,?)", (assignment_id, execution_id, event["execution_id"], raw))

    def audits(self, assignment_id: str) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE assignment_id=? ORDER BY seq", (assignment_id,))]

    def execution(self, assignment_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM executions WHERE assignment_id=?", (assignment_id,)).fetchone()
            return dict(row) if row else None
