"""Local execution evidence. Callers persist events; missing events prove nothing."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
import os
import uuid


class ExecutionObserverError(RuntimeError):
    """The durable consumer did not acknowledge an execution event."""


class ExecutionAudit:
    def __init__(self, assignment: dict, work_dir: Path, observer: Callable[[dict], None] | None):
        self.observer = observer
        self.execution_id = uuid.uuid4().hex
        self.scope = {key: assignment.get(key) for key in (
            "assignment_id", "task_id", "batch_id", "owner_epoch", "resume_generation")}
        self.scope["runner_session_id"] = (
            assignment.get("_runner_session_id") or assignment.get("runner_session_id"))
        self.work_dir = str(work_dir.resolve())
        self.job_name = None
        self.job_dir = None
        self.launch_pending = False
        self.spawn_failed = False
        self.spawned = False
        self.confirmed = False
        self.observer_failed = False
        self.pid = None
        self.pgid = None
        self.windows_job_id = None

    def emit(self, event: str, **facts) -> None:
        payload = dict(schema="dradar.execution_audit.v1", event=event,
                       execution_id=self.execution_id, scope=deepcopy(self.scope),
                       work_dir=self.work_dir, job_name=self.job_name,
                       job_dir=self.job_dir, platform=os.name, **facts)
        if self.observer is not None:
            try:
                self.observer(payload)
            except Exception as exc:
                self.observer_failed = True
                raise ExecutionObserverError("execution evidence could not be persisted") from exc

    def pending(self, job_name: str, job_dir: Path) -> None:
        self.job_name, self.job_dir = job_name, str(job_dir.resolve())
        self.launch_pending = True
        self.emit("launch_pending", execution_started=False)

    def record_spawn(self, pid, *, windows_job_id=None) -> None:
        self.spawned = True
        self.pid = pid if type(pid) is int and pid > 0 else None
        if windows_job_id is not None and self.pid is None:
            raise ExecutionObserverError("Windows process identity is invalid")
        if windows_job_id is not None and (
                not isinstance(windows_job_id, str) or len(windows_job_id) != 32
                or any(c not in "0123456789abcdef" for c in windows_job_id)):
            raise ExecutionObserverError("Windows Job identity is invalid")
        self.windows_job_id = windows_job_id
        self.pgid = self.pid if os.name == "posix" and windows_job_id is None else None
        self.emit("spawned", pid=self.pid, pgid=self.pgid, execution_started=True,
                  windows_job_id=windows_job_id,
                  process_identity_kind=("exact_windows_job" if windows_job_id else
                                         "live_child_handle_and_private_pgid"))

    def absent(self) -> None:
        if self.observer_failed:
            raise ExecutionObserverError("earlier execution evidence was not persisted")
        if self.observer is not None and not self.scope_bound():
            raise ExecutionObserverError("execution exit evidence has no exact session scope")
        if os.name == "nt" and self.windows_job_id is None:
            raise ExecutionObserverError("Windows execution exit has no exact Job identity")
        if self.windows_job_id is not None and (type(self.pid) is not int or self.pid <= 0):
            raise ExecutionObserverError("Windows execution exit has no valid process identity")
        self.emit("confirmed_absent", pid=self.pid, pgid=self.pgid,
                  execution_started=True, process_group="absent",
                  exact_job_containers="absent",
                  windows_job_id=self.windows_job_id,
                  evidence_kind=("windows_job_and_exact_job_docker_recheck_v1" if self.windows_job_id
                                 else "private_pgid_and_exact_job_docker_recheck_v1"))
        self.confirmed = True

    def never_started(self, reason: str) -> None:
        # This is an explicit control-flow fact about this invocation, never
        # an inference from absent files or a missing observer event.
        if not self.scope_bound():
            self.emit("unknown", reason="unbound_never_started_scope", execution_started=False)
            return
        self.emit("never_started", reason=reason, execution_started=False,
                  evidence_kind="provider_launch_boundary_not_crossed")

    def scope_bound(self) -> bool:
        return all(isinstance(self.scope.get(key), str) and self.scope[key]
                   for key in ("assignment_id", "runner_session_id"))

    def unknown(self, reason: str) -> None:
        self.emit("unknown", reason=reason, pid=self.pid, pgid=self.pgid,
                  execution_started=(True if self.spawned else
                                     None if self.launch_pending else False))
