"""_go_menu: quota is informational only now — the disclosure line fires
exactly when it should (real run + nonzero estimate) and stays quiet
otherwise (dev-agent runs, zero/missing estimate). No case here should ever
refuse to proceed on quota grounds -- that gate was deliberately removed."""

import argparse
import json
from pathlib import Path

import pytest

from dradar import runloop
from dradar.api_client import ApiError
from dradar.providers import (
    DEEPSEEK_CATALOG_SHA256,
    DEEPSEEK_RUN_CONFIG_VERSION,
    DEEPSEEK_RUNTIME_PROFILE,
    DSH_AGENT,
    DSH_FLASH_MODEL,
    DSH_RUN_CONFIG_VERSION,
    DSH_RUNTIME_PROFILE,
    DSH_VERSION,
    GROK_AGENT,
    GROK_CLI_VERSION,
    GROK_MODEL,
    GROK_PROVIDER,
)
from dradar.runner import RunnerError, TrialArtifacts

ASSIGNMENT = {
    "assignment_id": "a1", "task_id": "t1", "model": "m", "effort": "e",
    "agent": "claude", "expires_at": "2099-01-01T00:00:00Z",
    "est_minutes": 42, "est_quota_pct": 17, "nonce": "n1",
    "deep_swe_commit": None,
}

MENU = [
    {"task_id": "t1", "model": "m", "effort": "e", "est_minutes": 5, "est_quota_pct": 1},
    {"task_id": "t2", "model": "m", "effort": "e", "est_minutes": 9, "est_quota_pct": 2},
]


class FakeClient:
    def __init__(self, assignment_data, claims=None, suggested=None):
        # assignment_data: one payload (repeated), or a list served in order
        # (the last one repeats). claims: scripted claim_assignment results,
        # in order — a dict is returned, an exception instance is raised.
        # suggested: the cells `suggest()` hands back for --auto.
        self._payloads = assignment_data if isinstance(assignment_data, list) else [assignment_data]
        self._claims = list(claims or [])
        self._suggested = suggested or []
        self.claim_calls = []
        self.suggest_calls = []

    def get_assignment(self):
        return self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]

    def claim_assignment(self, task_id, model, effort):
        self.claim_calls.append((task_id, model, effort))
        result = self._claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def suggest(self, n):
        self.suggest_calls.append(n)
        return {"cells": self._suggested}

    def checkout(self, exclude_assignment_ids=None):
        # The default fake predates the per-cell dispenser, so callers take
        # the legacy whole-batch path these tests were written against.
        raise ApiError("not found", status_code=404)


def _args(yes=True, dev_agent=None, auto=None, pick=None):
    return argparse.Namespace(yes=yes, dev_agent=dev_agent, resume=False,
                              allow_task_drift=False, keep=False, auto=auto, pick=pick)


# runloop._run_and_submit and runloop._check_version_pin are the sanctioned
# monkeypatch seams for driving _go_menu; stub them by name, not by signature.
def _patch_run(monkeypatch, outcome="submitted", ran=None):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    if ran is None:
        monkeypatch.setattr(runloop, "_run_and_submit", lambda *a, **kw: outcome)
    else:
        monkeypatch.setattr(runloop, "_run_and_submit",
                            lambda *a, **kw: ran.append(a[1]["assignment_id"]) or outcome)


def test_real_run_with_estimate_prints_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    rc = runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "it's your call" in out
    assert "nothing is counted" in out
    assert rc == 0


def test_dev_agent_run_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent="nop"), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_zero_estimate_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    assignment = {**ASSIGNMENT, "est_quota_pct": 0}
    client = FakeClient({"assignment": assignment, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_missing_estimate_suppresses_quota_disclosure(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    assignment = dict(ASSIGNMENT)
    del assignment["est_quota_pct"]
    client = FakeClient({"assignment": assignment, "menu": None, "resumed": False})
    runloop._go_menu(_args(dev_agent=None), {}, client, tmp_path)
    assert "it's your call" not in capsys.readouterr().out


def test_declining_the_prompt_never_blocks_or_errors(monkeypatch, capsys, tmp_path: Path):
    """Declining just leaves the lease active for a later `dradar resume` --
    there is no quota-based refusal path left to hit."""
    _patch_run(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    client = FakeClient({"assignment": ASSIGNMENT, "menu": None, "resumed": False})
    rc = runloop._go_menu(_args(yes=False, dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "it's your call" in out  # still shown before the prompt
    assert "aborted" in out and "stay active" in out
    assert rc == 1


def test_runs_the_whole_held_batch_serially(monkeypatch, capsys, tmp_path: Path):
    """Free-pick: `active` carries several claimed cells -> the loop runs each
    one, in order, with a single get_assignment call (no re-claim per cell)."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": f"a{i}", "task_id": f"t{i}"} for i in range(1, 4)]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a1", "a2", "a3"]       # all three, claim order
    assert "holding 3 cells" in capsys.readouterr().out
    assert rc == 0


def test_free_pick_with_no_held_cells_points_to_the_web(monkeypatch, capsys, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"active": [], "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    out = capsys.readouterr().out
    assert "pick some on the radar page" in out
    assert rc == 0


def test_disk_safety_floor_runs_existing_work_but_does_not_claim_new_menu_cell():
    client = FakeClient({"active": [], "free_pick": False, "menu": MENU})

    active, free_pick = runloop._acquire_batch(
        client, yes=True, allow_new_claims=False,
    )

    assert active == [] and not free_pick
    assert client.claim_calls == []


def test_explicit_batch_404_exits_without_claiming_or_running():
    class MissingBatchClient:
        def get_assignment(self):
            raise ApiError(
                "server returned 404: active claim batch not found",
                status_code=404, code="claim_batch_not_found",
            )

        def claim_assignment(self, *_args, **_kwargs):
            pytest.fail("a missing explicit batch must never claim replacement work")

    with pytest.raises(SystemExit, match="active claim batch not found"):
        runloop._acquire_batch(MissingBatchClient(), yes=True)


# --- --auto / --pick: CLI-side claiming for free-pick instances (volunteer -
# issue #1, 2026-07-15) so an Agent never has to touch the web UI -----------

def test_go_rejects_auto_and_pick_together():
    with pytest.raises(SystemExit):
        runloop.cmd_go(argparse.Namespace(pick=["t1:m:e"], auto=5))


def test_go_rejects_nonpositive_auto():
    with pytest.raises(SystemExit, match="N >= 1"):
        runloop.cmd_go(argparse.Namespace(pick=None, auto=0))


def test_auto_claims_suggested_cells_and_runs_them(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [{"assignment": {**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}},
              {"assignment": {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert client.suggest_calls == [2]
    assert client.claim_calls == [("t1", "m", "e"), ("t2", "m", "e")]
    assert ran == ["a1", "a2"]
    out = capsys.readouterr().out
    assert "t1/m@e: claimed" in out and "t2/m@e: claimed" in out
    assert rc == 0


def test_auto_skips_a_stale_suggestion_and_keeps_going(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [ApiError("cell no longer available, fetch a fresh menu", status_code=409),
              {"assignment": {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert ran == ["a2"]                    # t1 skipped, t2 claimed and ran
    out = capsys.readouterr().out
    assert "t1/m@e: not claimed" in out
    assert rc == 0


def test_auto_stops_clean_at_the_concurrent_cap(monkeypatch, capsys, tmp_path: Path):
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [ApiError(
        "server returned 409: 已达到持有上限",
        status_code=409,
        code="claim_limit_reached",
    )]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]   # never tried t2 — cap already hit
    out = capsys.readouterr().out
    assert "stopping —" in out and "已达到持有上限" in out
    assert rc == 0


def test_auto_legacy_server_still_detects_concurrent_cap(monkeypatch, capsys, tmp_path: Path):
    suggested = [{"task_id": "t1", "model": "m", "effort": "e"},
                 {"task_id": "t2", "model": "m", "effort": "e"}]
    claims = [ApiError(
        "you're already holding 10 cells (max 10) — run or finish some",
        status_code=409,
    )]
    client = FakeClient({"active": [], "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)

    rc = runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path)

    assert client.claim_calls == [("t1", "m", "e")]
    assert "stopping —" in capsys.readouterr().out
    assert rc == 0


def test_pick_claims_exact_cells_by_id(monkeypatch, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    claims = [{"assignment": {**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}}]
    client = FakeClient({"active": [], "free_pick": True, "menu": None}, claims=claims)
    rc = runloop._go_menu(_args(yes=True, pick=["t1:m:e"]), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]
    assert ran == ["a1"]
    assert rc == 0


def test_pick_tops_up_an_existing_batch(monkeypatch, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}]
    claims = [{"assignment": {
        **ASSIGNMENT, "assignment_id": "a2", "task_id": "t2",
    }}]
    client = FakeClient(
        {"active": batch, "free_pick": True, "menu": None}, claims=claims,
    )

    rc = runloop._go_menu(
        _args(yes=True, pick=["t2:m:e"]), {}, client, tmp_path,
    )

    assert client.claim_calls == [("t2", "m", "e")]
    assert ran == ["a1", "a2"]
    assert rc == 0


def test_pick_skips_held_and_duplicate_cells(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}]
    claims = [{"assignment": {
        **ASSIGNMENT, "assignment_id": "a2", "task_id": "t2",
    }}]
    client = FakeClient(
        {"active": batch, "free_pick": True, "menu": None}, claims=claims,
    )

    rc = runloop._go_menu(
        _args(yes=True, pick=["t1:m:e", "t2:m:e", "t2:m:e"]),
        {}, client, tmp_path,
    )

    assert client.claim_calls == [("t2", "m", "e")]
    assert ran == ["a1", "a2"]
    assert capsys.readouterr().out.count("already held; skipping") == 2
    assert rc == 0


def test_pick_malformed_spec_exits_clearly():
    with pytest.raises(SystemExit):
        runloop._parse_pick("not-enough-colons")


def test_auto_tops_up_an_existing_batch_to_the_requested_size(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"}]
    suggested = [{"task_id": f"t{i}", "model": "m", "effort": "e"}
                 for i in range(2, 6)]
    claims = [{"assignment": {**ASSIGNMENT, "assignment_id": f"a{i}",
                              "task_id": f"t{i}"}} for i in range(2, 6)]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None},
                        claims=claims, suggested=suggested)
    rc = runloop._go_menu(_args(yes=True, auto=5), {}, client, tmp_path)
    assert client.suggest_calls == [4]
    assert ran == ["a1", "a2", "a3", "a4", "a5"]
    assert rc == 0


def test_auto_does_not_claim_when_existing_batch_meets_target(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    batch = [{**ASSIGNMENT, "assignment_id": f"a{i}", "task_id": f"t{i}"}
             for i in range(1, 4)]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    assert runloop._go_menu(_args(yes=True, auto=2), {}, client, tmp_path) == 0
    assert client.suggest_calls == []
    assert ran == ["a1", "a2", "a3"]
    assert "--auto target 2 already met" in capsys.readouterr().out


def test_skip_in_a_batch_moves_to_the_next_cell(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    # 's' skips cell 1, 'y' runs cell 2
    # Decline continuous refill, then skip cell 1 and run cell 2.
    answers = iter(["n", "s", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    batch = [{**ASSIGNMENT, "assignment_id": "a1"}, {**ASSIGNMENT, "assignment_id": "a2"}]
    client = FakeClient({"active": batch, "free_pick": True, "menu": None})
    runloop._go_menu(_args(yes=False, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a2"]                    # a1 skipped, a2 ran


# --- menu mode: nothing held on a non-free-pick instance -> claim here ------

def test_menu_mode_claims_the_chosen_entry_and_runs_it(monkeypatch, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient({"active": [], "free_pick": False, "menu": MENU},
                        claims=[{"assignment": ASSIGNMENT}])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e")]  # -y takes the top pick
    assert ran == ["a1"]
    assert rc == 0


def test_menu_claim_409_refetches_menu_and_claims_again(monkeypatch, capsys, tmp_path: Path):
    """The chosen cell filled up between menu fetch and claim: one fresh menu
    is fetched and the claim retried before giving up."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient(
        [{"active": [], "free_pick": False, "menu": [MENU[0]]},
         {"active": [], "free_pick": False, "menu": [MENU[1]]}],
        claims=[ApiError("server returned 409: cell filled", status_code=409),
                {"assignment": ASSIGNMENT}])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert client.claim_calls == [("t1", "m", "e"), ("t2", "m", "e")]
    assert "went stale" in capsys.readouterr().out
    assert ran == ["a1"]
    assert rc == 0


def test_menu_double_409_means_no_work_and_rc_0(monkeypatch, capsys, tmp_path: Path):
    ran = []
    _patch_run(monkeypatch, ran=ran)
    menu_payload = {"active": [], "free_pick": False, "menu": [MENU[0]]}
    client = FakeClient(
        [menu_payload, menu_payload],
        claims=[ApiError("server returned 409: cell filled", status_code=409),
                ApiError("server returned 409: cell filled", status_code=409)])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "no work available" in capsys.readouterr().out
    assert ran == []
    assert rc == 0


def test_menu_claim_409_self_heals_to_an_already_active_lease(monkeypatch, tmp_path: Path):
    """A 409 that actually means "you already hold a lease": the fresh
    get_assignment carries no menu but an active assignment -> run that."""
    ran = []
    _patch_run(monkeypatch, ran=ran)
    client = FakeClient(
        [{"active": [], "free_pick": False, "menu": [MENU[0]]},
         {"assignment": ASSIGNMENT, "menu": None, "resumed": True}],
        claims=[ApiError("server returned 409: already at cap", status_code=409)])
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert ran == ["a1"]
    assert rc == 0


def test_menu_claim_non_409_error_exits(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = FakeClient({"active": [], "free_pick": False, "menu": [MENU[0]]},
                        claims=[ApiError("server returned 500: boom", status_code=500)])
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "500" in str(excinfo.value)


# --- _exit_for: dead ends in the run flow come with a next step --------------

class ErrorClient:
    def __init__(self, exc):
        self._exc = exc

    def get_assignment(self):
        raise self._exc


def test_get_assignment_401_exits_with_token_recovery_hint(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("server returned 401: invalid token", status_code=401))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "invalid token" in msg                # the server detail survives
    assert "dradar login --github" in msg        # ...plus how to recover
    assert "radar page" in msg


def test_get_assignment_network_error_exits_mentioning_resume(monkeypatch, tmp_path: Path):
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("cannot reach https://radar.example: boom",
                                  status_code=None))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "cannot reach" in msg
    assert "check your connection" in msg
    assert "leases stay active" in msg and "dradar resume" in msg


def test_get_assignment_403_passes_server_detail_through(monkeypatch, tmp_path: Path):
    # Suspension carries the server's own explanation; no bogus recovery hint.
    _patch_run(monkeypatch)
    client = ErrorClient(ApiError("server returned 403: account suspended", status_code=403))
    with pytest.raises(SystemExit) as excinfo:
        runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    msg = str(excinfo.value)
    assert "account suspended" in msg
    assert "login --github" not in msg


def test_terminal_local_upload_rejection_stops_multi_cell_checkout(
        monkeypatch, tmp_path: Path):
    first = dict(ASSIGNMENT)
    second = {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2", "nonce": "n2"}
    checkout_calls = []
    ran = []

    class AtomicClient:
        second_sent = False

        def checkout(self, exclude_assignment_ids=None):
            excluded = set(exclude_assignment_ids or ())
            checkout_calls.append(excluded)
            if first["assignment_id"] not in excluded:
                return {"assignment": first, "held": 2, "unstarted": 1}
            if not self.second_sent:
                self.second_sent = True
                return {"assignment": second, "held": 2, "unstarted": 0}
            return {"assignment": None, "held": 1, "unstarted": 0}

    monkeypatch.setattr(runloop, "_check_version_pin", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_a, **_k: (
            ran.append(assignment["assignment_id"])
            or ("not-uploaded" if assignment["assignment_id"] == "a1" else "submitted")
        ),
    )
    args = _args(yes=True)
    assert runloop._run_checkout_loop(
        args, AtomicClient(), tmp_path, [first, second],
    ) == 1
    assert ran == ["a1"]
    assert checkout_calls == [set()]


def test_checkout_acknowledges_direct_fleet_before_provider_starts(
    monkeypatch, tmp_path: Path,
):
    events = []

    class AtomicClient:
        sent = False

        def checkout(self, exclude_assignment_ids=None):
            if not self.sent:
                self.sent = True
                return {"assignment": ASSIGNMENT, "held": 1, "unstarted": 0}
            return {"assignment": None, "held": 1, "unstarted": 0}

    monkeypatch.setattr(runloop, "_check_version_pin", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runloop, "_record_supervised_worker_checkout",
        lambda args, assignment_id: events.append(
            ("ready", args.fleet_pool, assignment_id)
        ) or True,
    )

    def run_and_submit(_client, assignment, *_args, **_kwargs):
        assert events == [("ready", True, assignment["assignment_id"])]
        events.append(("provider", assignment["assignment_id"]))
        return "submitted"

    monkeypatch.setattr(runloop, "_run_and_submit", run_and_submit)
    monkeypatch.setattr(
        runloop, "_record_assignment_boundary", lambda *_args: True,
    )
    args = _args(yes=True)
    args.fleet_pool = True
    args.worker_child = False
    args.batch_id = "550e8400e29b41d4a716446655440000"

    assert runloop._run_checkout_loop(
        args, AtomicClient(), tmp_path, [ASSIGNMENT],
    ) == 0
    assert events == [
        ("ready", True, ASSIGNMENT["assignment_id"]),
        ("provider", ASSIGNMENT["assignment_id"]),
    ]


def test_choose_menu_entry_numeric_pick(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "2")
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[1]


def test_choose_menu_entry_empty_input_takes_top_pick_silently(monkeypatch, capsys):
    # Enter-for-default is a deliberate choice, not a typo: no announcement.
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[0]
    assert "invalid choice" not in capsys.readouterr().out


def test_choose_menu_entry_invalid_input_reprompts_once(monkeypatch, capsys):
    # A typo must not silently lease the wrong cell: announce and re-prompt.
    answers = iter(["abc", "2"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert runloop._choose_menu_entry(MENU, yes=False) is MENU[1]
    out = capsys.readouterr().out
    assert "invalid choice 'abc'" in out
    assert "taking the top pick" not in out


def test_choose_menu_entry_double_invalid_falls_back_announced(monkeypatch, capsys):
    # Garbage-piping automation still terminates, but the fallback is loud.
    for pair in (("abc", "xyz"), ("99", "0")):
        answers = iter(pair)
        monkeypatch.setattr("builtins.input", lambda *_: next(answers))
        assert runloop._choose_menu_entry(MENU, yes=False) is MENU[0]
        out = capsys.readouterr().out
        assert "invalid choice" in out
        assert "taking the top pick (t1)" in out


# --- _run_and_submit: the outcome tag the server grades by ------------------
# Stubbed one level lower (runloop.run_trial) so the real outcome derivation
# and meta assembly run; the server marks `interrupted` invalid instead of
# grading it 0, so mislabeling here corrupts grading fleet-wide.

class SubmitClient(FakeClient):
    def __init__(self, assignment_data, claims=None):
        super().__init__(assignment_data, claims)
        self.submissions = []

    def submit(self, assignment_id, nonce, patch, trajectory, result, meta,
               outcome="completed", resume_generation=None, **_kwargs):
        self.submissions.append(
            {"assignment_id": assignment_id, "outcome": outcome, "meta": meta})
        return {"submission_id": f"s-{assignment_id}", "grade_status": "pending"}


def _fake_art(base: Path, rc: int = 0, result_data: dict | None = None,
              codex_cli_version: str | None = "0.145.0",
              zcode_cli_sha256: str | None = None) -> TrialArtifacts:
    trial_dir = base / "trial"
    (trial_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    patch = trial_dir / "artifacts" / "model.patch"
    patch.write_text("diff --git a b\n")
    result = None
    if result_data is not None:
        result = trial_dir / "result.json"
        result.write_text(json.dumps(result_data))
    job_dir = base / "job"
    job_dir.mkdir(exist_ok=True)
    return TrialArtifacts(job_dir=job_dir, trial_dir=trial_dir, patch=patch,
                          trajectory=None, result=result, returncode=rc,
                          duration_sec=61.0, log_path=base / "pier.log",
                          codex_cli_version=codex_cli_version,
                          zcode_cli_sha256=zcode_cli_sha256)


def test_clean_run_submits_outcome_completed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "submitted"
    assert client.submissions[0]["outcome"] == "completed"
    assert client.submissions[0]["meta"]["codex_cli_version"] == "0.145.0"


def test_submission_attests_observed_zcode_cli_sha256(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    digest = "3" * 64
    art = _fake_art(tmp_path, rc=0, zcode_cli_sha256=digest)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})

    tag = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    assert client.submissions[0]["meta"]["zcode_cli_sha256"] == digest


@pytest.mark.parametrize(
    ("agent", "provider"),
    [
        ("codex", None),
        ("dsh-minimal", "deepseek"),
        ("grok-build", "xai-subscription"),
        ("kimi-code", "kimi-subscription"),
        ("zcode", "bigmodel-coding-plan"),
    ],
)
def test_task_content_mismatch_stops_before_model_run_for_every_harness(
    monkeypatch, tmp_path: Path, capsys, agent, provider,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *_args: False)
    monkeypatch.setattr(
        runloop, "run_trial",
        lambda *_args, **_kwargs: pytest.fail("model runner must not start"),
    )
    stopped = []
    monkeypatch.setattr(
        runloop, "_mark_stopped_quietly",
        lambda _client, assignment, **kwargs: stopped.append(
            (assignment["assignment_id"], kwargs)),
    )
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": agent,
        "provider": provider,
        "task_content_hash": "a" * 64,
        "deep_swe_commit": "b" * 40,
    }

    outcome = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "c" * 40,
    )

    assert outcome == "task-content-mismatch"
    assert len(stopped) == 1
    assignment_id, stop_kwargs = stopped[0]
    assert assignment_id == "a1"
    assert stop_kwargs["failure_kind"] == "task_content_mismatch"
    diagnostic = stop_kwargs["failure_diagnostic"]
    assert diagnostic == {
        "schema": "dradar-task-content-mismatch-v1",
        "failure_code": "task_content_mismatch",
        "expected_hash_prefix": "a" * 12,
        "actual_hash_prefix": "e3b0c44298fc",
        "server_task_commit_prefix": "b" * 12,
        "local_task_commit_prefix": "c" * 12,
        "model_started": False,
        "quota_consumed": False,
    }
    assert not ({"task_id", "model", "agent", "provider", "path"} & diagnostic.keys())
    assert client.submissions == []
    output = capsys.readouterr().out
    assert "No model process was started" in output
    assert "no model quota was consumed" in output
    assert "Do not use `--allow-task-drift` for an ordinary retry" in output


def test_cleanup_unconfirmed_quarantines_slot_without_marking_lease_stopped(
    monkeypatch, tmp_path: Path, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "POOL_ABORT"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))

    def fail(*_args, **_kwargs):
        raise runloop.RunnerCleanupUnconfirmedError("exact runtime is unknown")

    monkeypatch.setattr(runloop, "run_trial", fail)
    stopped = []
    monkeypatch.setattr(
        runloop,
        "_mark_stopped_quietly",
        lambda *_args, **_kwargs: stopped.append(True),
    )
    client = SubmitClient({})

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert outcome == "cleanup-unconfirmed"
    assert stopped == []
    assert client.submissions == []
    assert not abort_file.exists()
    output = capsys.readouterr().out
    assert "lease remains running" in output
    assert "worker slot is quarantined" in output


def test_task_retryable_failure_isolates_assignment_without_pool_abort(
    monkeypatch, tmp_path: Path, capsys,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    abort_file = tmp_path / "POOL_ABORT"
    monkeypatch.setenv("DRADAR_POOL_ABORT_FILE", str(abort_file))
    diagnostic = {"schema": "dradar-runner-failure-v1", "reason": "terminal_status"}

    def fail(*_args, **_kwargs):
        raise runloop.RunnerTaskRetryableError(
            "ZCode terminal state is not gradeable",
            failure_diagnostic=diagnostic,
        )

    monkeypatch.setattr(runloop, "run_trial", fail)
    stopped = []
    monkeypatch.setattr(
        runloop,
        "_mark_stopped_quietly",
        lambda _client, assignment, **kwargs: stopped.append(
            (assignment["assignment_id"], kwargs)
        ) or True,
    )
    client = SubmitClient({})

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert outcome == "assignment-isolated"
    assert stopped == [("a1", {
        "failure_kind": "runner_failed",
        "failure_diagnostic": diagnostic,
    })]
    assert not abort_file.exists()
    assert "other worker slots may continue" in capsys.readouterr().out


def test_legacy_batch_quarantines_cleanup_unconfirmed_worker_slot(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    monkeypatch.setattr(
        runloop, "_run_and_submit", lambda *_args, **_kwargs: "cleanup-unconfirmed",
    )
    args = _args()
    args.worker_child = True
    telemetry = type("Telemetry", (), {
        "bind_batch": lambda self, _batch_id: None,
        "flush": lambda self: None,
        "set_phase": lambda self, *values: setattr(self, "phase", values),
    })()

    rc = runloop._run_batch(
        args, SubmitClient({}), tmp_path, [ASSIGNMENT], telemetry=telemetry,
    )

    assert rc == runloop._WORKER_SLOT_QUARANTINED_EXIT_CODE
    assert telemetry.phase == ("paused", "a1", None)


def test_explicit_task_drift_override_keeps_existing_audited_behavior(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "check_task_content_hash", lambda *_args: False)
    art = _fake_art(tmp_path, rc=0)
    monkeypatch.setattr(runloop, "run_trial", lambda *_args, **_kwargs: art)
    client = SubmitClient({})
    args = _args()
    args.allow_task_drift = True

    outcome = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, args, "abc123",
    )

    assert outcome == "submitted"
    assert client.submissions[0]["meta"]["task_content_hash_match"] is False


def test_deepseek_submission_attests_catalog_and_runtime_profile(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, codex_cli_version="0.146.0")
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": "codex",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "effort": "high",
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    meta = client.submissions[0]["meta"]
    assert meta["model_config_version"] == DEEPSEEK_RUN_CONFIG_VERSION
    assert meta["model_catalog_sha256"] == DEEPSEEK_CATALOG_SHA256
    assert meta["model_runtime_profile"] == DEEPSEEK_RUNTIME_PROFILE
    assert meta["honey_execution_security_profile"] == (
        "full-container-tools-outer-boundary-v1"
    )
    assert meta["honey_inner_permission_mode"] == "full-auto-approve"
    assert meta["honey_child_agent_access"] == "native-enabled"
    assert meta["honey_outer_isolation"] == (
        "pier-docker-exact-egress-minimal-credentials-v1"
    )


def test_dsh_submission_attests_minimal_native_runtime(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=0, codex_cli_version=None)
    art.dsh_version = DSH_VERSION
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": DSH_AGENT,
        "provider": "deepseek",
        "model": DSH_FLASH_MODEL,
        "effort": "off",
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    meta = client.submissions[0]["meta"]
    assert meta["dsh_version"] == DSH_VERSION
    assert meta["model_config_version"] == DSH_RUN_CONFIG_VERSION
    assert meta["model_runtime_profile"] == DSH_RUNTIME_PROFILE
    assert meta["dsh_minimal_tools"] == ["bash", "str_replace_editor"]
    assert meta["dsh_native_efforts"] == ["off", "high", "max"]
    assert meta["honey_execution_security_profile"] == (
        "full-container-tools-outer-boundary-v1"
    )
    assert meta["honey_child_agent_access"] == "native-enabled"


def test_nonzero_pier_rc_submits_outcome_interrupted_with_meta(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {"agent_result": {"n_input_tokens": 10, "n_output_tokens": 3,
                                    "n_cache_tokens": 0, "n_agent_steps": 7}}
    art = _fake_art(tmp_path, rc=1, result_data=result_data)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "interrupted"
    sub = client.submissions[0]
    assert sub["outcome"] == "interrupted"
    assert sub["meta"]["pier_returncode"] == 1
    assert sub["meta"]["duration_sec"] == 61.0
    assert sub["meta"]["n_input_tokens"] == 10  # token stats still reported


def test_complete_bundle_survives_nonzero_pier_postrun_rc(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    phases = {
        name: {
            "started_at": "2026-08-14T00:00:01Z",
            "finished_at": "2026-08-14T00:00:02Z",
        }
        for name in ("environment_setup", "agent_setup", "agent_execution")
    }
    art = _fake_art(tmp_path, rc=1, result_data={
        "started_at": "2026-08-14T00:00:00Z",
        "finished_at": "2026-08-14T00:10:00Z",
        "exception_info": None,
        "agent_result": {"n_agent_steps": 4},
        **phases,
    })
    art.patch.write_text(
        "diff --git a/result.txt b/result.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/result.txt\n"
        "@@ -0,0 +1 @@\n+done\n",
        encoding="utf-8",
    )
    bundle = {"schema_version": "test", "sessions": []}
    usage = {
        "schema": "dradar-codex-trajectory-bundle-v1",
        "complete": True,
        "agent_session_count": 2,
        "root_session_count": 1,
        "subagent_session_count": 1,
        "sessions": [],
        "n_input_tokens": 100,
        "n_cache_tokens": 20,
        "n_output_tokens": 5,
    }
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle", lambda _trial_dir: bundle,
    )
    monkeypatch.setattr(
        runloop, "codex_trajectory_bundle_usage", lambda _bundle: usage,
    )
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})

    tag = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    sub = client.submissions[0]
    assert sub["outcome"] == "completed"
    assert sub["meta"]["pier_returncode"] == 1
    assert sub["meta"]["pier_postrun_warning"] is True
    assert sub["meta"]["pier_failure_phase"] == "post_agent"
    assert sub["meta"]["bundled_completion_evidence"] == {
        "schema": "dradar-bundled-completion-v1",
        "usage_schema": "dradar-codex-trajectory-bundle-v1",
        "agent_session_count": 2,
        "root_session_count": 1,
        "subagent_session_count": 1,
    }


def test_parse_degraded_terminal_bundle_survives_nonzero_postrun_rc(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    phases = {
        name: {
            "started_at": "2026-08-14T00:00:01Z",
            "finished_at": "2026-08-14T00:00:02Z",
        }
        for name in ("environment_setup", "agent_setup", "agent_execution")
    }
    art = _fake_art(tmp_path, rc=1, result_data={
        "started_at": "2026-08-14T00:00:00Z",
        "finished_at": "2026-08-14T00:10:00Z",
        "exception_info": None,
        "agent_result": {"n_agent_steps": 4},
        **phases,
    })
    art.patch.write_text(
        "diff --git a/result.txt b/result.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/result.txt\n"
        "@@ -0,0 +1 @@\n+done\n",
        encoding="utf-8",
    )
    bundle = {
        "schema_version": "test",
        "sessions": [],
        "parse_error_count": 1,
        "parse_degraded_completion_eligible": True,
    }
    usage = {
        "schema": "dradar-codex-trajectory-bundle-v1",
        "complete": False,
        "agent_session_count": 1,
        "root_session_count": 1,
        "subagent_session_count": 0,
        "sessions": [],
        "n_input_tokens": 100,
        "n_cache_tokens": 20,
        "n_output_tokens": 5,
    }
    monkeypatch.setattr(
        runloop, "build_codex_trajectory_bundle", lambda _trial_dir: bundle,
    )
    monkeypatch.setattr(
        runloop, "codex_trajectory_bundle_usage", lambda _bundle: usage,
    )
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})

    tag = runloop._run_and_submit(
        client, ASSIGNMENT, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    sub = client.submissions[0]
    assert sub["outcome"] == "completed"
    assert sub["meta"]["bundled_completion_evidence"] == {
        "schema": "dradar-bundled-completion-v2",
        "evidence_mode": "single-root-terminal-parse-degraded",
        "usage_schema": "dradar-codex-trajectory-bundle-v1",
        "agent_session_count": 1,
        "root_session_count": 1,
        "subagent_session_count": 0,
        "parse_error_count": 1,
    }


def _write_complete_grok_artifacts(art: TrialArtifacts) -> dict:
    usage = {
        "schema": "dradar-subscription-provider-usage-v1",
        "provider": "grok",
        "model": GROK_MODEL,
        "complete": True,
        "request_count": 2,
        "n_input_tokens": 300,
        "n_cache_tokens": 120,
        "n_output_tokens": 30,
        "request_usage_complete": True,
        "request_usage_observed": True,
        "timed_usage_complete": False,
        "usage_evidence_tier": "complete_reconciled",
        "token_usage_events": [
            {"n_input_tokens": 100, "n_cache_tokens": 40,
             "n_output_tokens": 10},
            {"n_input_tokens": 200, "n_cache_tokens": 80,
             "n_output_tokens": 20},
        ],
    }
    phases = {
        "environment_setup": {
            "started_at": "2026-08-19T15:00:01Z",
            "finished_at": "2026-08-19T15:01:00Z",
        },
        "agent_setup": {
            "started_at": "2026-08-19T15:01:00Z",
            "finished_at": "2026-08-19T15:01:01Z",
        },
        "agent_execution": {
            "started_at": "2026-08-19T15:01:01Z",
            "finished_at": "2026-08-19T15:09:59Z",
        },
    }
    result = {
        "started_at": "2026-08-19T15:00:00Z",
        "finished_at": "2026-08-19T15:10:00Z",
        "exception_info": None,
        "agent_result": {
            "n_input_tokens": 300,
            "n_cache_tokens": 120,
            "n_output_tokens": 30,
            "n_agent_steps": 1,
            "metadata": {"provider_usage": usage},
        },
        **phases,
    }
    assert art.result is not None
    art.result.write_text(json.dumps(result), encoding="utf-8")
    agent = art.trial_dir / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "provider-usage.json").write_text(
        json.dumps(usage), encoding="utf-8",
    )
    (agent / "trajectory.json").write_text(json.dumps({
        "schema_version": "ATIF-v1.7",
        "session_id": "grok-session-1",
        "agent": {
            "name": GROK_AGENT,
            "version": GROK_CLI_VERSION,
            "model_name": GROK_MODEL,
            "extra": {"provider": GROK_PROVIDER, "oauth": True},
        },
        "steps": [{
            "step_id": 1,
            "source": "agent",
            "message": "Implementation complete.",
            "model_name": GROK_MODEL,
            "reasoning_effort": "xhigh",
            "llm_call_count": 1,
        }],
        "final_metrics": {
            "total_prompt_tokens": 300,
            "total_cached_tokens": 120,
            "total_completion_tokens": 30,
            "total_steps": 1,
        },
    }), encoding="utf-8")
    art.patch.write_text(
        "diff --git a/result.txt b/result.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/result.txt\n"
        "@@ -0,0 +1 @@\n+done\n",
        encoding="utf-8",
    )
    return usage


def test_grok_complete_evidence_survives_nonzero_pier_postrun_rc(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=1, result_data={})
    art.codex_cli_version = None
    art.grok_cli_version = GROK_CLI_VERSION
    _write_complete_grok_artifacts(art)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": GROK_AGENT,
        "provider": GROK_PROVIDER,
        "model": GROK_MODEL,
        "effort": "xhigh",
        "agent_version": GROK_CLI_VERSION,
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    sub = client.submissions[0]
    assert sub["outcome"] == "completed"
    assert sub["meta"]["pier_returncode"] == 1
    assert sub["meta"]["pier_postrun_warning"] is True
    assert sub["meta"]["pier_failure_phase"] == "post_agent"
    assert sub["meta"]["grok_completion_evidence"] == {
        "schema": "dradar-grok-completion-v1",
        "provider": "grok",
        "model": GROK_MODEL,
        "agent_version": GROK_CLI_VERSION,
        "trajectory_schema": "ATIF-v1.7",
        "session_id": "grok-session-1",
        "request_count": 2,
        "n_agent_steps": 1,
    }


@pytest.mark.parametrize(
    "tamper",
    ["secret-patch", "incomplete-usage", "wrong-model", "bad-metrics",
     "missing-phase-finish"],
)
def test_grok_nonzero_rc_stays_interrupted_without_every_completion_gate(
    monkeypatch, tmp_path: Path, tamper: str,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=1, result_data={})
    art.codex_cli_version = None
    art.grok_cli_version = GROK_CLI_VERSION
    _write_complete_grok_artifacts(art)
    agent = art.trial_dir / "agent"
    if tamper == "secret-patch":
        art.patch.write_text(
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n"
            "+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n",
            encoding="utf-8",
        )
    elif tamper == "incomplete-usage":
        usage = json.loads((agent / "provider-usage.json").read_text())
        usage["complete"] = False
        usage["usage_incomplete_reason"] = (
            "terminal_aggregate_missing_or_inconsistent"
        )
        (agent / "provider-usage.json").write_text(json.dumps(usage))
    elif tamper == "wrong-model":
        trajectory = json.loads((agent / "trajectory.json").read_text())
        trajectory["agent"]["model_name"] = "grok-other"
        (agent / "trajectory.json").write_text(json.dumps(trajectory))
    elif tamper == "bad-metrics":
        trajectory = json.loads((agent / "trajectory.json").read_text())
        trajectory["final_metrics"]["total_prompt_tokens"] += 1
        (agent / "trajectory.json").write_text(json.dumps(trajectory))
    else:
        assert art.result is not None
        result = json.loads(art.result.read_text())
        result["agent_execution"]["finished_at"] = None
        art.result.write_text(json.dumps(result))
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": GROK_AGENT,
        "provider": GROK_PROVIDER,
        "model": GROK_MODEL,
        "effort": "xhigh",
        "agent_version": GROK_CLI_VERSION,
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "interrupted"
    assert client.submissions[0]["outcome"] == "interrupted"
    assert "grok_completion_evidence" not in client.submissions[0]["meta"]


def test_dsh_completed_agent_survives_nonzero_pier_postrun_rc(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {
        "started_at": "2026-08-14T00:00:00Z",
        "finished_at": "2026-08-14T00:10:00Z",
        "agent_execution": {
            "started_at": "2026-08-14T00:00:10Z",
            "finished_at": "2026-08-14T00:09:50Z",
        },
        "exception_info": None,
        "agent_result": {
            "n_input_tokens": 10,
            "n_cache_tokens": 2,
            "n_output_tokens": 3,
        },
    }
    art = _fake_art(tmp_path, rc=1, result_data=result_data,
                    codex_cli_version=None)
    art.dsh_version = DSH_VERSION
    art.patch.write_text(
        "diff --git a/result.txt b/result.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/result.txt\n"
        "@@ -0,0 +1 @@\n"
        "+done\n",
        encoding="utf-8",
    )
    outcome = art.trial_dir / "agent" / "dsh-home" / "dsh-outcome.json"
    outcome.parent.mkdir(parents=True)
    outcome.write_text(json.dumps({
        "schema": "dradar-dsh-outcome-v1",
        "terminalKind": "completed",
        "requestCount": 7,
        "agentCompleted": True,
        "errorCode": None,
    }), encoding="utf-8")
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": DSH_AGENT,
        "provider": "deepseek",
        "model": DSH_FLASH_MODEL,
        "effort": "high",
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "submitted"
    sub = client.submissions[0]
    assert sub["outcome"] == "completed"
    assert sub["meta"]["pier_returncode"] == 1
    assert sub["meta"]["pier_postrun_warning"] is True
    assert sub["meta"]["pier_failure_phase"] == "post_agent"
    assert sub["meta"]["dsh_completion_evidence"] == {
        "schema": "dradar-dsh-outcome-v1",
        "terminal_kind": "completed",
        "request_count": 7,
    }


def test_dsh_nonzero_pier_rc_without_terminal_sidecar_stays_interrupted(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    art = _fake_art(tmp_path, rc=1, result_data={
        "started_at": "2026-08-14T00:00:00Z",
        "finished_at": "2026-08-14T00:10:00Z",
        "agent_execution": {
            "started_at": "2026-08-14T00:00:10Z",
            "finished_at": "2026-08-14T00:09:50Z",
        },
        "exception_info": None,
        "agent_result": {},
    }, codex_cli_version=None)
    art.dsh_version = DSH_VERSION
    art.patch.write_text(
        "diff --git a/result.txt b/result.txt\n"
        "new file mode 100644\n--- /dev/null\n+++ b/result.txt\n"
        "@@ -0,0 +1 @@\n+done\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    assignment = {
        **ASSIGNMENT,
        "agent": DSH_AGENT,
        "provider": "deepseek",
        "model": DSH_FLASH_MODEL,
        "effort": "high",
    }

    tag = runloop._run_and_submit(
        client, assignment, tmp_path, _args(), "abc123",
    )

    assert tag == "interrupted"
    assert client.submissions[0]["outcome"] == "interrupted"
    assert "pier_postrun_warning" not in client.submissions[0]["meta"]


def test_recorded_exception_info_submits_outcome_interrupted(monkeypatch, tmp_path: Path):
    # pier rc 0 but result.json recorded an exception (e.g. rate-limit death
    # inside the harness): still interrupted, never a graded 0.
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    result_data = {"exception_info": {"type": "RateLimitDeath"}, "agent_result": {}}
    art = _fake_art(tmp_path, rc=0, result_data=result_data)
    monkeypatch.setattr(runloop, "run_trial", lambda *a, **kw: art)
    client = SubmitClient({})
    tag = runloop._run_and_submit(client, ASSIGNMENT, tmp_path, _args(), "abc123")
    assert tag == "interrupted"
    assert client.submissions[0]["outcome"] == "interrupted"


def test_run_trial_error_is_failed_and_go_menu_rc_1(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RunnerError("pier exploded")
    monkeypatch.setattr(runloop, "run_trial", boom)
    client = SubmitClient({"assignment": ASSIGNMENT, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert "trial failed: pier exploded" in capsys.readouterr().out
    assert client.submissions == []  # nothing uploaded
    assert rc == 1


def test_mixed_batch_one_failure_yields_rc_1(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runloop, "HOME", tmp_path / "home")
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)

    def fake_run(assignment, *a, **kw):
        if assignment["assignment_id"] == "a1":
            raise RunnerError("boom")
        return _fake_art(tmp_path / assignment["assignment_id"], rc=0)
    monkeypatch.setattr(runloop, "run_trial", fake_run)
    batch = [{**ASSIGNMENT, "assignment_id": "a1"},
             {**ASSIGNMENT, "assignment_id": "a2", "nonce": "n2"}]
    client = SubmitClient({"active": batch, "free_pick": True, "menu": None})
    rc = runloop._go_menu(_args(yes=True, dev_agent=None), {}, client, tmp_path)
    assert [s["assignment_id"] for s in client.submissions] == ["a2"]  # a2 still landed
    assert rc == 1  # but the batch as a whole reports failure


def test_serial_batch_stops_before_second_cell_on_insufficient_balance(
        monkeypatch, capsys, tmp_path: Path):
    attempts = []

    def run(client, assignment, *a, **kw):
        attempts.append(assignment["assignment_id"])
        return "insufficient-balance"

    monkeypatch.setattr(runloop, "_run_and_submit", run)
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *a, **kw: None)
    batch = [{**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"},
             {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"}]

    rc = runloop._run_batch(_args(), SubmitClient({}), tmp_path, batch)

    assert rc == 1
    assert attempts == ["a1"]
    out = capsys.readouterr().out
    assert "stopping this worker before the next task" in out
    assert "siblings with model runs already in flight are allowed to finish" in out


def test_serial_batch_stops_before_second_cell_on_task_content_mismatch(
    monkeypatch, capsys, tmp_path: Path,
):
    attempts = []
    monkeypatch.setattr(runloop, "_check_version_pin", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runloop, "_run_and_submit",
        lambda _client, assignment, *_args, **_kwargs: (
            attempts.append(assignment["assignment_id"])
            or "task-content-mismatch"
        ),
    )
    batch = [
        {**ASSIGNMENT, "assignment_id": "a1", "task_id": "t1"},
        {**ASSIGNMENT, "assignment_id": "a2", "task_id": "t2"},
    ]

    rc = runloop._run_batch(_args(), SubmitClient({}), tmp_path, batch)

    assert rc == 1
    assert attempts == ["a1"]
    assert "same mismatched task checkout" in capsys.readouterr().out
