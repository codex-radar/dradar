import json
import httpx
from dradar.v2 import commands
from dradar.v2.client import Client
from test_v2_scheduler import Server, Runtime, controller, CONFIG

def setup(monkeypatch, server):
    monkeypatch.setattr(commands, "runtime_config", lambda: {"server": "http://localhost:1234", "token": "synthetic"})
    return lambda server_url, token, journal: Client(server_url, token, journal, transport=httpx.MockTransport(server))

def test_schema_is_candidate_and_does_not_read_credentials(monkeypatch, capsys):
    monkeypatch.setattr(commands, "runtime_config", lambda: __import__("pytest").fail("schema must not load auth"))
    assert commands.main(["schema"]) == 0
    assert json.loads(capsys.readouterr().out)["candidate"]

def test_missing_selection_asks_only_missing_and_creates_no_run(tmp_path, monkeypatch, capsys):
    server = Server()
    factory = setup(monkeypatch, server)
    rc = commands.main(["select", "--state-root", str(tmp_path / "state"), "--benchmark", "synthetic", "--model", "codex-test"], client_factory=factory)
    assert rc == 2
    view = json.loads(capsys.readouterr().out)
    assert [q["field"] for q in view["questions"]] == ["total", "concurrency"]
    assert server.run is None and not server.assignments

def test_all_choices_use_catalog_effort_without_reasking(tmp_path, monkeypatch, capsys):
    server = Server()
    factory = setup(monkeypatch, server)
    rc = commands.main(["select", "--state-root", str(tmp_path / "state"), "--benchmark", "synthetic", "--model", "codex-test", "--total-count", "20", "--concurrency", "7"], client_factory=factory)
    assert rc == 0
    view = json.loads(capsys.readouterr().out)
    assert view["configuration"]["effort"] == "high"
    assert view["configuration"]["total_count"] == 20
    assert view["configuration"]["concurrency"] == 7
    assert server.run is None

def test_cli_run_with_injected_synthetic_executor(tmp_path, monkeypatch, capsys):
    server = Server()
    factory = setup(monkeypatch, server)
    runtime = Runtime(tmp_path)
    runtime.server = server
    rc = commands.main(["run", "--state-root", str(tmp_path / "state"), "--tasks-root", str(tmp_path), "--benchmark", "synthetic", "--model", "codex-test", "--total-count", "4", "--concurrency", "2"], client_factory=factory, runtime_factory=lambda *args: runtime, poll_seconds=.01)
    assert rc == 0 and len(runtime.calls) == 4
    out = capsys.readouterr().out
    assert "lease_id" not in out and "synthetic-token" not in out
    assert '"submitted": 4' in out

def test_progress_can_read_while_original_controller_locked(tmp_path, monkeypatch, capsys):
    server, runtime = Server(), Runtime(tmp_path)
    factory = setup(monkeypatch, server)
    with controller(tmp_path, server, runtime) as c:
        c.initialize()
        assert commands.main(["progress", "--state-root", str(tmp_path / "state")], client_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["started"] == 0

def test_actual_cli_replays_last_start_ack_loss_before_success_exit(tmp_path,monkeypatch,capsys):
    import itertools
    clock=itertools.count(1000)
    monkeypatch.setattr(commands.time,'monotonic',lambda:next(clock))
    server=Server();server.start_drop=True
    factory=setup(monkeypatch,server);runtime=Runtime(tmp_path);runtime.server=server
    rc=commands.main(['run','--state-root',str(tmp_path/'state'),'--tasks-root',str(tmp_path),'--benchmark','synthetic','--model','codex-test','--total-count','1','--concurrency','1'],client_factory=factory,runtime_factory=lambda *args:runtime,poll_seconds=.001)
    assert rc==0 and len(runtime.calls)==1 and server.run['counts']['submitted']==1
    assert sum(path.endswith('/start') for _,path in server.calls)==2


def test_library_only_claim_never_asks_user_to_pick_cells_or_guess_plan(tmp_path,monkeypatch,capsys):
    server=Server();factory=setup(monkeypatch,server)
    rc=commands.main(['run','--state-root',str(tmp_path/'state'),'--tasks-root',str(tmp_path),'--benchmark','synthetic'],client_factory=factory)
    assert rc==3 and server.run is None and not server.assignments
    view=json.loads(capsys.readouterr().out)
    assert view['status']=='blocked' and 'questions' not in view
    assert 'Existing approved Harness-model/effort' in view['user_message']
