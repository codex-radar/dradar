"""Narrow pre-upload checks for schema-only benchmark submissions.

Pompeii tasks declare ``model_answer.json`` as their sole deliverable.  A
multi-megabyte patch therefore cannot be a legitimate answer: it means an
agent committed an input image, cache, log, or another intermediate artifact.
Inspect the patch locally so the volunteer sees the offending files before a
request reaches the server's body-size limit.
"""

from __future__ import annotations

import os
import tempfile
import shlex
import subprocess
from dataclasses import dataclass


POMPEII_DELIVERABLE = "model_answer.json"
# A valid Pompeii answer has at most 15 tiny edge objects.  Leave two orders of
# magnitude of headroom for diff metadata and formatting while still catching
# accidental generated artifacts far below the server's 5 MiB hard limit.
POMPEII_PATCH_MAX_BYTES = 64 * 1024
PATCH_REPORT_FILE_LIMIT = 20


@dataclass(frozen=True)
class PatchFile:
    source_path: str
    target_path: str
    patch_bytes: int
    binary: bool
    added_lines: int | None = None
    deleted_lines: int | None = None

    @property
    def display_path(self) -> str:
        if self.source_path == self.target_path:
            return _printable_path(self.target_path)
        return (
            f"{_printable_path(self.source_path)} -> "
            f"{_printable_path(self.target_path)}"
        )


@dataclass(frozen=True)
class PatchInspection:
    total_bytes: int
    files: tuple[PatchFile, ...]
    parse_error: str | None = None


@dataclass(frozen=True)
class PatchGuardResult:
    inspection: PatchInspection
    violations: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.violations


def _clean_git_path(value: str, prefix: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def _printable_path(value: str) -> str:
    """Keep diagnostics concrete without allowing terminal control bytes."""
    output: list[str] = []
    for character in value:
        if character.isprintable():
            output.append(character)
        else:
            codepoint = ord(character)
            if codepoint <= 0xFF:
                output.append(f"\\x{codepoint:02x}")
            elif codepoint <= 0xFFFF:
                output.append(f"\\u{codepoint:04x}")
            else:
                output.append(f"\\U{codepoint:08x}")
    return "".join(output)


def _diff_header_paths(line: bytes) -> tuple[str, str] | None:
    try:
        parts = shlex.split(
            line.decode("utf-8", errors="surrogateescape"), posix=True,
        )
    except ValueError:
        return None
    if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
        return None
    return _clean_git_path(parts[2], "a/"), _clean_git_path(parts[3], "b/")


def _numstat(data: bytes) -> tuple[list[tuple[int | None, int | None]], str | None]:
    """Return per-section line counts while asking git to validate the diff."""
    try:
        # Statistics must not depend on the caller's repository prefix or
        # user-supplied Git configuration/environment. No patch is applied.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        with tempfile.TemporaryDirectory(prefix="dradar-patch-stat-") as directory:
            env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(directory)
            proc = subprocess.run(
                ["git", "apply", "--numstat", "-"],
                input=data, cwd=directory, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
    except OSError as exc:
        return [], f"git apply could not start: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        return [], f"git could not parse model.patch: {detail or 'unknown error'}"

    stats: list[tuple[int | None, int | None]] = []
    for line in proc.stdout.decode("utf-8", errors="surrogateescape").splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            return [], "git returned malformed patch statistics"
        try:
            added = None if fields[0] == "-" else int(fields[0])
            deleted = None if fields[1] == "-" else int(fields[1])
        except ValueError:
            return [], "git returned malformed patch statistics"
        if (added is None) != (deleted is None) or any(value is not None and value < 0 for value in (added, deleted)):
            return [], "git returned malformed patch statistics"
        stats.append((added, deleted))
    return stats, None


def inspect_patch(data: bytes) -> PatchInspection:
    """Inventory changed paths, binary markers, and patch bytes per file."""
    offsets: list[tuple[int, tuple[str, str] | None]] = []
    cursor = 0
    for line in data.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            offsets.append((cursor, _diff_header_paths(line.rstrip(b"\r\n"))))
        cursor += len(line)

    stats, parse_error = _numstat(data)
    if not offsets:
        if parse_error is None:
            parse_error = "model.patch contains no diff --git sections"
        return PatchInspection(len(data), (), parse_error)
    if parse_error is None and len(stats) != len(offsets):
        parse_error = (
            "patch section count does not match git's file statistics "
            f"({len(offsets)} sections, {len(stats)} files)"
        )

    files: list[PatchFile] = []
    for index, (start, paths) in enumerate(offsets):
        end = offsets[index + 1][0] if index + 1 < len(offsets) else len(data)
        section = data[start:end]
        if paths is None:
            source_path = target_path = "<unparseable diff header>"
            if parse_error is None:
                parse_error = "one or more diff --git headers are malformed"
        else:
            source_path, target_path = paths
        added = deleted = None
        if index < len(stats):
            added, deleted = stats[index]
        binary = (
            b"\nGIT binary patch\n" in b"\n" + section
            or b"\nBinary files " in b"\n" + section
            or (index < len(stats) and added is None and deleted is None)
        )
        files.append(PatchFile(
            source_path=source_path,
            target_path=target_path,
            patch_bytes=len(section),
            binary=binary,
            added_lines=added,
            deleted_lines=deleted,
        ))
    return PatchInspection(len(data), tuple(files), parse_error)


def check_pompeii_patch(data: bytes) -> PatchGuardResult:
    inspection = inspect_patch(data)
    violations: list[str] = []
    if inspection.parse_error:
        violations.append(inspection.parse_error)
    if inspection.total_bytes > POMPEII_PATCH_MAX_BYTES:
        violations.append(
            f"model.patch is {inspection.total_bytes} bytes; "
            f"the local Pompeii limit is {POMPEII_PATCH_MAX_BYTES} bytes"
        )
    if len(inspection.files) != 1:
        violations.append(
            f"patch changes {len(inspection.files)} files; exactly one is allowed"
        )
    for item in inspection.files:
        if (
            item.source_path != POMPEII_DELIVERABLE
            or item.target_path != POMPEII_DELIVERABLE
        ):
            violations.append(
                f"unexpected changed path: {item.display_path}"
            )
        if item.binary:
            violations.append(f"binary patch content: {item.display_path}")
    return PatchGuardResult(inspection, tuple(violations))


def _format_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MiB ({value} bytes)"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB ({value} bytes)"
    return f"{value} bytes"


def format_patch_guard_report(result: PatchGuardResult) -> list[str]:
    """Build bounded, content-free diagnostics safe to print locally."""
    inspection = result.inspection
    lines = [
        f"model.patch: {_format_bytes(inspection.total_bytes)}",
        f"allowed deliverable: {POMPEII_DELIVERABLE}",
    ]
    if result.violations:
        lines.append("violations:")
        lines.extend(f"  - {item}" for item in result.violations)
    lines.append("changed files (largest patch contribution first):")
    ordered = sorted(
        inspection.files, key=lambda item: item.patch_bytes, reverse=True,
    )
    for item in ordered[:PATCH_REPORT_FILE_LIMIT]:
        traits = [f"patch {_format_bytes(item.patch_bytes)}"]
        if item.binary:
            traits.append("binary")
        elif item.added_lines is not None and item.deleted_lines is not None:
            traits.append(f"+{item.added_lines}/-{item.deleted_lines} lines")
        lines.append(f"  - {item.display_path}: {', '.join(traits)}")
    if len(ordered) > PATCH_REPORT_FILE_LIMIT:
        lines.append(
            f"  - ... {len(ordered) - PATCH_REPORT_FILE_LIMIT} more files omitted"
        )
    if not ordered:
        lines.append("  - <none parsed>")
    return lines
