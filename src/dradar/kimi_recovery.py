"""Bounded, provider-declared recovery for an interrupted Kimi CLI turn.

This module deliberately has no Pier dependency.  DRadar copies it beside the
private Pier adapter, while unit tests exercise the exact same orchestration
without starting Docker or spending subscription quota.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence

KIMI_RETRYABLE_EXIT_CODE = 75
KIMI_PROVIDER_CONNECTION_EXIT_CODE = 1
KIMI_RESUME_DELAYS_SECONDS = (10, 30)
KIMI_RESUME_PROMPT = (
    "Continue the unfinished task from where the previous turn stopped. "
    "Inspect the current working tree first, preserve completed work, finish "
    "the remaining implementation and tests, and commit the final result."
)

_PIER_EXIT_CODE_RE = re.compile(r"^Command failed \(exit ([0-9]+)\):")
_KIMI_PROVIDER_CONNECTION_ERROR = (
    "error: failed to run prompt: provider.connection_error: Connection error."
)


def pier_exit_code(error: BaseException) -> int | None:
    """Read Pier's stable non-zero command prefix without importing Pier."""

    match = _PIER_EXIT_CODE_RE.match(str(error))
    return int(match.group(1)) if match else None


def kimi_provider_connection_stderr_is_retryable(stderr_line: str) -> bool:
    """Accept only Kimi 0.36.1's exact CLI-owned terminal error line."""

    return stderr_line.rstrip("\r\n") == _KIMI_PROVIDER_CONNECTION_ERROR


async def _failure_is_retryable(
    error: BaseException,
    classify_retryable_error: (
        Callable[[BaseException], Awaitable[bool]] | None
    ),
) -> bool:
    if pier_exit_code(error) == KIMI_RETRYABLE_EXIT_CODE:
        return True
    if classify_retryable_error is None:
        return False
    try:
        return bool(await classify_retryable_error(error))
    except Exception:
        return False


def validated_session_id(value: str | None) -> str | None:
    """Accept only a canonical UUID emitted from Kimi's protected session dir."""

    candidate = (value or "").strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    return canonical if candidate.lower() == canonical else None


async def run_with_kimi_resume(
    *,
    run_initial: Callable[[], Awaitable[None]],
    find_session_id: Callable[[], Awaitable[str | None]],
    run_resume: Callable[[str, str], Awaitable[None]],
    delays: Sequence[float] = KIMI_RESUME_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, float, str], None] | None = None,
    classify_retryable_error: (
        Callable[[BaseException], Awaitable[bool]] | None
    ) = None,
) -> tuple[int, str | None]:
    """Run once, then resume only Kimi-declared temporary failures.

    Returns ``(resume_attempts, session_id)``.  Non-retryable failures and an
    exhausted retry budget re-raise the original Pier exception.
    """

    try:
        await run_initial()
        return 0, None
    except Exception as error:
        if not await _failure_is_retryable(error, classify_retryable_error):
            raise
        last_error = error

    try:
        session_id = validated_session_id(await find_session_id())
    except Exception:  # noqa: BLE001 - recovery must not replace the run error
        session_id = None
    if session_id is None:
        raise last_error

    for attempt, delay in enumerate(delays, start=1):
        if on_retry is not None:
            on_retry(attempt, delay, session_id)
        await sleep(delay)
        try:
            await run_resume(session_id, KIMI_RESUME_PROMPT)
            return attempt, session_id
        except Exception as error:
            if not await _failure_is_retryable(error, classify_retryable_error):
                raise
            last_error = error

    raise last_error


__all__ = [
    "KIMI_RESUME_DELAYS_SECONDS",
    "KIMI_RESUME_PROMPT",
    "KIMI_PROVIDER_CONNECTION_EXIT_CODE",
    "KIMI_RETRYABLE_EXIT_CODE",
    "kimi_provider_connection_stderr_is_retryable",
    "pier_exit_code",
    "run_with_kimi_resume",
    "validated_session_id",
]
