"""The local console must carry the reason a worker died before checkout.

A volunteer on native Windows reported workers exiting 1 with "no error output
at all". The CLI had in fact classified the failure and composed a readable
explanation -- and sent both only to Fleet. When the coordinator is itself the
broken part, that channel is exactly the one that cannot deliver.
"""
import pytest

from dradar import fleet, runloop
from test_workers import _args, _patch_pool_setup, _ScriptedProcess


def _publishing(monkeypatch, result):
    """Stand in for the Fleet channel; result may be a value or an exception."""
    def publish(*_args, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(fleet, "publish_pool_startup_failure", publish)


def test_recorded_failure_is_printed_locally(monkeypatch, capsys):
    _publishing(monkeypatch, True)
    runloop._publish_fleet_startup_failure(
        _args(fleet_pool=True), ModuleNotFoundError("fixture"),
    )
    output = capsys.readouterr().out
    assert "startup-dependency-missing" in output
    assert "这台设备未能完成运行准备" in output


@pytest.mark.parametrize("failure", [
    fleet.FleetError("coordinator is gone"),
    OSError("lock is unreadable"),
    ValueError("unusable state"),
])
def test_unreachable_fleet_channel_still_reports_locally(
        monkeypatch, capsys, failure):
    """The reported case: the only channel carrying the reason was the dead one."""
    _publishing(monkeypatch, failure)
    runloop._publish_fleet_startup_failure(
        _args(fleet_pool=True), PermissionError("fixture"),
    )
    assert "startup-permission-denied" in capsys.readouterr().out


def test_ready_pool_stays_silent(monkeypatch, capsys):
    """Negative control: the success path publishes nothing and must print nothing.

    ``publish_pool_startup_failure`` returns False once a pool reported ready,
    and the tail call on a healthy run lands here. Printing unconditionally
    would put "startup failed" on every successful run.
    """
    _publishing(monkeypatch, False)
    runloop._publish_fleet_startup_failure(
        _args(fleet_pool=True), "pool ended before startup acknowledgement",
    )
    assert capsys.readouterr().out == ""


def test_non_fleet_run_is_unchanged(monkeypatch, capsys):
    """Without a pool the exception propagates to the operator on its own."""
    _publishing(monkeypatch, True)
    runloop._publish_fleet_startup_failure(
        _args(fleet_pool=False), ModuleNotFoundError("fixture"),
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("reasons,expected", [
    (set(), "no child published a reason"),
    ({"worker-entrypoint-failed"}, "reason: worker-entrypoint-failed"),
    ({"startup-unknown", "worker-entrypoint-failed"},
     "reason: startup-unknown, worker-entrypoint-failed"),
])
def test_reason_summary_is_stable(reasons, expected):
    assert runloop._precheckout_reason_summary(reasons) == expected


def test_pool_names_the_reason_on_the_console(monkeypatch, capsys):
    """An entrypoint death never self-reports, so the parent must name it."""
    _patch_pool_setup(monkeypatch, active_count=1)
    monkeypatch.setattr(runloop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runloop, "_pool_backfill_delay", lambda _attempt: 0)
    monkeypatch.setattr(
        runloop, "_pool_ready_work_count", lambda _client, **_kwargs: 1,
    )
    monkeypatch.setattr(
        runloop.subprocess, "Popen",
        lambda command, env, **kwargs: _ScriptedProcess(
            command, env, [1], mark_activity=False, **kwargs,
        ),
    )

    assert runloop._run_worker_pool(_args(workers=1)) == 1
    output = capsys.readouterr().out
    assert "exited 1 before checkout [worker-entrypoint-failed]" in output
