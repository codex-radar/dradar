"""Local observations. Execution, delivery and grading are separate facts."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math

EXECUTION = {"not_started", "preparing", "running", "completed", "failed", "unknown"}
DELIVERY = {"not_saved", "saved", "uploading", "accepted", "failed", "unknown"}
GRADING = {"not_requested", "queued", "running", "passed", "not_passed", "failed", "unknown"}

@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self):
        for value in asdict(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token usage must be a nonnegative integer or unknown")

    def view(self) -> dict:
        return {key: "unknown" if value is None else value for key, value in asdict(self).items()}

@dataclass(frozen=True)
class TaskProgress:
    task_id: str
    execution: str = "not_started"
    delivery: str = "not_saved"
    grading: str = "not_requested"
    elapsed_seconds: float | None = None
    usage: Usage = Usage()

    def __post_init__(self):
        if self.execution not in EXECUTION or self.delivery not in DELIVERY or self.grading not in GRADING:
            raise ValueError("invalid local observation")
        value = self.elapsed_seconds
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("elapsed time must be finite, nonnegative or unknown")

    def view(self) -> dict:
        return {"task_id": self.task_id, "execution": self.execution,
                "delivery": self.delivery, "grading": self.grading,
                "elapsed_seconds": "unknown" if self.elapsed_seconds is None else self.elapsed_seconds,
                "usage": self.usage.view()}

    def text(self) -> str:
        elapsed = "unknown" if self.elapsed_seconds is None else f"{self.elapsed_seconds:.1f}s"
        usage = self.usage.view()
        return (f"{self.task_id}: 执行={self.execution} 保存/上传={self.delivery} 判分={self.grading} "
                f"耗时={elapsed} tokens(input/output/total)="
                f"{usage['input_tokens']}/{usage['output_tokens']}/{usage['total_tokens']}")


def assignment_view(a: dict) -> dict:
    """Public CLI view excludes lease/credentials/raw server text."""
    progress = a.get("progress") or {}
    tokens = progress.get("tokens") or {}
    measured = progress.get("elapsed_ms")
    usage = Usage(tokens.get("input"), tokens.get("output"), tokens.get("total"))
    execution = a.get("outcome")
    if execution not in {"completed", "failed", "interrupted"}:
        execution = {"leased": "preparing", "running": "running"}.get(a.get("state"), "unknown")
    return {"task_id": a["task"]["task_id"], "assignment_id": a["assignment_id"],
            "device_id": a["device_id"], "slot_id": a["slot_id"], "phase": a.get("phase", "unknown"),
            "execution": execution, "upload": "accepted" if a.get("state") == "submitted" else "pending",
            "grading": a.get("grading_state", "unknown"),
            "elapsed_ms": "unknown" if measured is None else measured, "tokens": usage.view()}

def run_view(snapshot: dict) -> dict:
    run = snapshot["run"]
    counts = run["counts"]
    return {"run_id": run["run_id"], "state": run["state"], "requested": run["total_count"],
            "concurrency": run["concurrency"], "started": counts["started"], "submitted": counts["submitted"],
            "uncertain": counts["uncertain"], "shortfall": max(0, run["total_count"] - counts["started"]),
            "tasks": [assignment_view(a) for a in snapshot["assignments"]]}
