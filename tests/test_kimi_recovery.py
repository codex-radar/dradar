from __future__ import annotations

import asyncio

import pytest

from dradar.kimi_recovery import (
    KIMI_RESUME_PROMPT,
    kimi_provider_connection_stderr_is_retryable,
    pier_exit_code,
    run_with_kimi_resume,
    validated_session_id,
)

SESSION = "832d7f94-ab9a-4f83-b630-37a3dab65025"


class CommandFailed(RuntimeError):
    def __init__(self, code: int, *, stdout: str = "", stderr: str = ""):
        super().__init__(
            f"Command failed (exit {code}): kimi --print\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )


def test_pier_exit_code_is_fail_closed() -> None:
    assert pier_exit_code(CommandFailed(75)) == 75
    assert pier_exit_code(RuntimeError("model said exit 75")) is None


def test_exact_kimi_connection_error_on_stderr_is_retryable() -> None:
    assert kimi_provider_connection_stderr_is_retryable(
        (
            "error: failed to run prompt: provider.connection_error: "
            "Connection error.\n"
        )
    ) is True


@pytest.mark.parametrize(
    "stderr_line",
    [
        "",
        (
            "warning: provider request failed after 10 retries\n"
            "error: failed to run prompt: provider.connection_error: "
            "Connection error."
        ),
        (
            "error: failed to run prompt: provider.connection_error: "
            "Connection error.\nunrelated terminal failure"
        ),
        (
            "error: failed to run prompt: provider.connection_error: "
            "Connection error. "
        ),
    ],
)
def test_kimi_connection_fallback_is_fail_closed(stderr_line: str) -> None:
    assert kimi_provider_connection_stderr_is_retryable(stderr_line) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (SESSION, SESSION),
        (f" {SESSION}\n", SESSION),
        ("not-a-session", None),
        (f"{SESSION}; touch /tmp/owned", None),
    ],
)
def test_session_id_requires_one_canonical_uuid(value: str, expected: str | None) -> None:
    assert validated_session_id(value) == expected


def test_success_does_not_probe_or_resume() -> None:
    calls: list[object] = []

    async def scenario() -> tuple[int, str | None]:
        async def initial() -> None:
            calls.append("initial")

        async def find() -> str | None:
            calls.append("find")
            return SESSION

        async def resume(session_id: str, prompt: str) -> None:
            calls.append((session_id, prompt))

        return await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(0,),
        )

    assert asyncio.run(scenario()) == (0, None)
    assert calls == ["initial"]


def test_exit_75_resumes_same_session_and_workspace() -> None:
    calls: list[object] = []

    async def scenario() -> tuple[int, str | None]:
        async def initial() -> None:
            calls.append("initial")
            raise CommandFailed(75)

        async def find() -> str | None:
            calls.append("find")
            return SESSION

        async def resume(session_id: str, prompt: str) -> None:
            calls.append(("resume", session_id, prompt))

        async def no_wait(delay: float) -> None:
            calls.append(("sleep", delay))

        return await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(10, 30),
            sleep=no_wait,
        )

    assert asyncio.run(scenario()) == (1, SESSION)
    assert calls == [
        "initial",
        "find",
        ("sleep", 10),
        ("resume", SESSION, KIMI_RESUME_PROMPT),
    ]


def test_exit_1_with_exact_provider_signal_resumes_same_session() -> None:
    calls: list[object] = []

    async def scenario() -> tuple[int, str | None]:
        async def initial() -> None:
            calls.append("initial")
            raise CommandFailed(
                1,
                stderr=(
                    "error: failed to run prompt: provider.connection_error: "
                    "Connection error."
                ),
            )

        async def find() -> str | None:
            calls.append("find")
            return SESSION

        async def resume(session_id: str, prompt: str) -> None:
            calls.append(("resume", session_id, prompt))

        async def no_wait(delay: float) -> None:
            calls.append(("sleep", delay))

        async def classify(error: BaseException) -> bool:
            calls.append(("classify", pier_exit_code(error)))
            return (
                pier_exit_code(error) == 1
                and kimi_provider_connection_stderr_is_retryable(
                    "error: failed to run prompt: provider.connection_error: "
                    "Connection error.\n"
                )
            )

        return await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(10, 30),
            sleep=no_wait,
            classify_retryable_error=classify,
        )

    assert asyncio.run(scenario()) == (1, SESSION)
    assert calls == [
        "initial",
        ("classify", 1),
        "find",
        ("sleep", 10),
        ("resume", SESSION, KIMI_RESUME_PROMPT),
    ]


def test_retry_budget_is_bounded_and_reraises_last_failure() -> None:
    attempts: list[str] = []

    async def scenario() -> None:
        async def initial() -> None:
            raise CommandFailed(75)

        async def find() -> str | None:
            return SESSION

        async def resume(session_id: str, prompt: str) -> None:
            del prompt
            attempts.append(session_id)
            raise CommandFailed(75)

        async def no_wait(_delay: float) -> None:
            return None

        await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(10, 30),
            sleep=no_wait,
        )

    with pytest.raises(CommandFailed, match=r"exit 75"):
        asyncio.run(scenario())
    assert attempts == [SESSION, SESSION]


def test_retry_never_drifts_to_a_newly_discovered_session() -> None:
    other_session = "d9428888-122b-4543-9bda-fcb60bf132d1"
    resumed_sessions: list[str] = []
    find_calls = 0

    async def scenario() -> tuple[int, str | None]:
        async def initial() -> None:
            raise CommandFailed(75)

        async def find() -> str | None:
            nonlocal find_calls
            find_calls += 1
            return SESSION if find_calls == 1 else other_session

        async def resume(session_id: str, prompt: str) -> None:
            del prompt
            resumed_sessions.append(session_id)
            if len(resumed_sessions) == 1:
                raise CommandFailed(75)

        async def no_wait(_delay: float) -> None:
            return None

        return await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(10, 30),
            sleep=no_wait,
        )

    assert asyncio.run(scenario()) == (2, SESSION)
    assert resumed_sessions == [SESSION, SESSION]
    assert find_calls == 1


def test_nonretryable_exit_and_missing_session_never_restart() -> None:
    resume_calls = 0

    async def run(code: int, session: str | None) -> None:
        nonlocal resume_calls

        async def initial() -> None:
            raise CommandFailed(code)

        async def find() -> str | None:
            return session

        async def resume(_session_id: str, _prompt: str) -> None:
            nonlocal resume_calls
            resume_calls += 1

        await run_with_kimi_resume(
            run_initial=initial,
            find_session_id=find,
            run_resume=resume,
            delays=(0,),
        )

    with pytest.raises(CommandFailed, match=r"exit 1"):
        asyncio.run(run(1, SESSION))
    with pytest.raises(CommandFailed, match=r"exit 75"):
        asyncio.run(run(75, "invalid"))
    assert resume_calls == 0
