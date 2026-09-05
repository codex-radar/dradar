"""Fixed-source bootstrap tests; network and CLI execution are inert seams."""

import json
from types import SimpleNamespace

import pytest

from dradar import cli, failure_reports, local_config, session_entry


REVISION = "a" * 40
CODE = "inert_plan_code_123456789"


def metadata(monkeypatch, *, url="https://github.com/codex-radar/dradar", commit=REVISION, vcs="git"):
    data = {"url": url, "vcs_info": {"vcs": vcs, "commit_id": commit}}
    monkeypatch.setattr(session_entry.importlib.metadata, "distribution", lambda _: SimpleNamespace(
        read_text=lambda _: json.dumps(data)))


def argv(**kw):
    values = {"revision": REVISION, "server": "https://api.example.invalid", "plan": CODE, **kw}
    return ["--" + key + "=" + value for key, value in values.items()]


@pytest.mark.parametrize("source", ["https://github.com/codex-radar/dradar", "https://github.com/SecurityMind/dradar"])
def test_exact_git_provenance_and_direct_entry_without_ota_switch(monkeypatch, source):
    from dradar import launcher
    metadata(monkeypatch, url=source)
    monkeypatch.setattr(launcher, "main", lambda: pytest.fail("no dynamic OTA launcher"))
    calls = []
    monkeypatch.setattr(cli, "main", lambda args: calls.append(args) or 0)
    assert session_entry.main(argv()) == 0
    assert calls == [["run", "--follow", "--plan=" + CODE,
                      "--server=https://api.example.invalid", "--locale", "zh-CN"]]


@pytest.mark.parametrize("changes", [
    {"url": "https://example.invalid/dradar"}, {"url": "http://github.com/codex-radar/dradar"},
    {"url": "https://github.com/other/repository"}, {"commit": "b" * 40}, {"vcs": "other"},
])
def test_changed_source_never_exchanges_plan_or_reports_capability(monkeypatch, changes, capsys):
    metadata(monkeypatch, **changes)
    monkeypatch.setattr(cli, "main", lambda _: pytest.fail("no CLI operation"))
    monkeypatch.setattr(session_entry, "_startup_report", lambda *_: pytest.fail("no report on untrusted source"))
    assert session_entry.main(argv()) == 2
    assert CODE not in capsys.readouterr().out


@pytest.mark.parametrize("site", ["http://remote.example.invalid", "https://example.invalid/other", "https://[invalid"])
def test_invalid_site_fails_without_execution(monkeypatch, site):
    monkeypatch.setattr(cli, "main", lambda _: pytest.fail("no operation"))
    assert session_entry.main(argv(server=site)) == 2


def test_bootstrap_report_keeps_capability_only_in_header_and_no_output_fields(monkeypatch, tmp_path):
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 3
            return Response()
    monkeypatch.setattr(session_entry, "build_opener", lambda handler: Opener())
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    assert session_entry._startup_report("https://api.example.invalid", CODE) == "received"
    request = seen[0]
    payload = json.loads(request.data)
    assert set(payload) == {"schema", "report_key", "source", "phase", "failure_kind", "failure_code",
                            "client_version", "platform", "occurred_at", "detail"}
    assert CODE not in request.data.decode() and payload["detail"] == {}
    assert request.get_header("X-dradar-run-code") == CODE
    assert session_entry._NoRedirect().redirect_request(None) is None


def test_failed_bootstrap_report_queue_contains_no_capability(monkeypatch, tmp_path):
    class Opener:
        def open(self, *_args, **_kw): raise OSError("inert transport failure")
    monkeypatch.setattr(session_entry, "build_opener", lambda _: Opener())
    monkeypatch.setattr(local_config, "HOME", tmp_path)
    assert session_entry._startup_report("https://api.example.invalid", CODE) == "queued"
    queued = list((tmp_path / "failure-reports").glob("*.json"))
    assert len(queued) == 1 and CODE not in queued[0].read_text()
