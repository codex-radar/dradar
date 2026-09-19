"""The environment build must not be a silent wait.

A volunteer whose resolver could not answer for ``ghcr.io`` reported the
CLI as permanently hung (2026-09-08).  It was not: ``run_trial`` waits at
most ``WORKER_REGISTRATION_GRACE_SEC``.  But the run heartbeat starts only
*after* worker registration, so the entire build phase printed nothing at
all -- and 30 minutes of silence is indistinguishable from a hang.  These
tests pin the output, not the bound.
"""

import pytest

import dradar.runner as runner


class LiveProcess:
    """A Pier child that neither registers nor exits."""

    def poll(self):
        return None


class DeadProcess:
    def poll(self):
        return 1


def _wait(monkeypatch, tmp_path, log_text, ticks, proc=None):
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    log_path = tmp_path / "build.log"
    log_path.write_text(log_text, encoding="utf-8")
    with pytest.raises(runner.RunnerError) as exc:
        runner._wait_for_worker_registration(
            proc or LiveProcess(), tmp_path / "events.jsonl",
            environment_build_timeout_multiplier=3.0,
            worker_event_source=lambda: None,
            log_path=log_path,
        )
    return str(exc.value)


def test_the_wait_prints_progress_instead_of_going_silent(
    monkeypatch, tmp_path, capsys,
):
    ticks = iter((0.0, 30.0, 90.0, 1800.1))
    _wait(monkeypatch, tmp_path, "#2 extracting sha256:abc\n", ticks)
    lines = [
        line for line in capsys.readouterr().out.splitlines()
        if "preparing environment" in line
    ]
    assert len(lines) == 2, lines
    assert "30s elapsed" in lines[0]
    assert "1 min elapsed" in lines[1]
    # The volunteer must be able to see that the wait is bounded, and by how
    # much, without reading the source.
    assert "gives up after 30 min" in lines[0]
    assert "#2 extracting sha256:abc" in lines[0]


def test_a_stalled_build_is_named_and_explained_once(monkeypatch, tmp_path, capsys):
    """Silence past BUILD_STALL_WARN_SEC is reported with the cause the log
    already contains, and the advice is not repeated every 30 seconds."""

    ticks = iter((0.0, 30.0, 400.0, 500.0, 1800.1))
    _wait(
        monkeypatch, tmp_path,
        'failed to do request: Head "https://ghcr.io/v2/x/manifests/y": '
        "dial tcp: lookup ghcr.io on 172.29.240.1:53: no such host\n",
        ticks,
    )
    out = capsys.readouterr().out
    assert "no new build output for 6 min" in out
    assert "DNS failure reaching ghcr.io" in out
    assert out.count("fix the resolver/proxy for that host") == 1


def test_a_stall_without_registry_evidence_does_not_invent_a_cause(
    monkeypatch, tmp_path, capsys,
):
    """Negative control: a quiet but healthy-looking build gets the generic
    pointer, never a fabricated DNS/TLS diagnosis."""

    ticks = iter((0.0, 30.0, 400.0, 1800.1))
    _wait(monkeypatch, tmp_path, "#5 [3/9] RUN pip install -r reqs.txt\n", ticks)
    out = capsys.readouterr().out
    assert "no new build output for" in out
    assert "DNS failure" not in out
    assert "TLS failure" not in out
    assert "run `dradar doctor` in another terminal" in out


def test_the_timeout_names_the_phase_and_the_registry(monkeypatch, tmp_path):
    ticks = iter((0.0, 1799.0, 1800.1))
    message = _wait(
        monkeypatch, tmp_path,
        'Get "https://auth.docker.io/token": net/http: TLS handshake timeout\n',
        ticks,
    )
    assert "the environment never finished building" in message
    assert "30 min grace window" in message
    assert "TLS failure reaching auth.docker.io" in message
    # No model ran, so the volunteer must be told the attempt was free.
    assert "no quota was consumed" in message


def test_pier_exiting_early_also_carries_the_registry_diagnosis(
    monkeypatch, tmp_path,
):
    ticks = iter((0.0, 5.0))
    message = _wait(
        monkeypatch, tmp_path,
        "dial tcp: lookup ghcr.io: no such host\n",
        ticks, proc=DeadProcess(),
    )
    assert "Pier exited before" in message
    assert "DNS failure reaching ghcr.io" in message


def test_grace_window_and_cadence_are_unchanged(monkeypatch, tmp_path, capsys):
    """This change adds output; it must not quietly move the bound.

    The 30-minute grace is the inner bound against the server's 45-minute
    preparation grace, so silently widening it here would let the server
    expire a lease the CLI still considers alive.
    """

    assert runner.WORKER_REGISTRATION_GRACE_SEC == 1800
    assert runner.BUILD_PROGRESS_SEC == runner.HEARTBEAT_SEC == 30
    assert runner.BUILD_STALL_WARN_SEC == 300


def test_duration_reads_in_seconds_below_a_minute():
    assert runner._duration(0) == "0s"
    assert runner._duration(45) == "45s"
    assert runner._duration(60) == "1 min"
    assert runner._duration(1800) == "30 min"


def test_progress_is_skipped_when_no_log_is_available(monkeypatch, tmp_path, capsys):
    """Legacy callers pass no log path; they must keep working, silently."""

    ticks = iter((0.0, 30.0, 1800.1))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    with pytest.raises(runner.RunnerError):
        runner._wait_for_worker_registration(
            LiveProcess(), tmp_path / "events.jsonl",
            environment_build_timeout_multiplier=3.0,
            worker_event_source=lambda: None,
        )
    assert "preparing environment" not in capsys.readouterr().out
