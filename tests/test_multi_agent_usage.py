import json
from pathlib import Path

from dradar.runner import (
    aggregate_codex_session_usage,
    build_codex_trajectory_bundle,
)


def _session(path: Path, session_id: str, role: str, usages: list[dict],
             parent: str | None = None, inherited: dict | None = None,
             history_mode: str | None = None,
             model: str = "gpt-5.6-terra") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = "exec"
    if role == "subagent":
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    session_meta = {
        "id": session_id, "thread_source": role, "source": source,
    }
    if history_mode is not None:
        session_meta["history_mode"] = history_mode
    events = [{"type": "session_meta", "payload": session_meta}]
    if inherited is not None:
        events += [
            {"type": "session_meta", "payload": {
                "id": parent, "thread_source": "user", "source": "exec",
            }},
            {"type": "event_msg", "payload": {
                "type": "task_started",
            }},
            {"type": "event_msg", "payload": {
                "type": "token_count", "info": {
                    "total_token_usage": inherited},
            }, "timestamp": "2026-08-17T00:59:58Z"},
        ]
    events += [
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    events += [{"type": "event_msg", "timestamp": (
        f"2026-08-17T01:00:{index:02d}Z"
    ), "payload": {
        "type": "token_count", "info": {"total_token_usage": usage},
    }} for index, usage in enumerate(usages)]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def _usage(input_tokens, cached, output, reasoning=0):
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output, "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output}


def test_aggregates_final_root_and_subagent_counters(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions" / "2026" / "07" / "19"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(20, 10, 2), _usage(100, 60, 10, 4),
    ])
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(150, 80, 15, 7),
    ], parent="root-1", inherited=_usage(100, 60, 10, 4))

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["agent_session_count"] == 2
    assert usage["root_session_count"] == 1
    assert usage["subagent_session_count"] == 1
    assert usage["n_input_tokens"] == 150
    assert usage["n_cache_tokens"] == 80
    assert usage["n_output_tokens"] == 15
    assert usage["n_reasoning_output_tokens"] == 7
    assert usage["sessions"][1]["parent_session_id"] == "root-1"
    assert usage["timed_usage_complete"] is True
    assert len(usage["token_usage_events"]) == 3


def test_astra_identity_is_retained_in_uploaded_codex_usage(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(
        sessions / "root.jsonl",
        "root-astra",
        "user",
        [_usage(100, 60, 10, 4)],
        model="gpt-6-astra",
    )

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["sessions"][0]["model_name"] == "gpt-6-astra"


def test_duplicate_session_id_uses_largest_cumulative_record(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "old.jsonl", "root-1", "user", [_usage(10, 5, 1)])
    _session(sessions / "new.jsonl", "root-1", "user", [_usage(30, 20, 4)])

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["session_file_count"] == 2
    assert usage["agent_session_count"] == 1
    assert usage["n_input_tokens"] == 30
    assert usage["n_output_tokens"] == 4


def test_paginated_subagent_counters_start_at_zero(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10, 4),
    ], history_mode="paginated")
    # Codex 0.148+ retains parent metadata/task boundaries in paginated child
    # files but omits inherited token counters.  The child's cumulative values
    # are standalone and must be added once, not rejected or subtracted.
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(50, 20, 5, 3),
    ], parent="root-1", inherited=None, history_mode="paginated")
    child_events = [json.loads(line) for line in (
        sessions / "child.jsonl").read_text().splitlines()]
    child_events.insert(1, {"type": "session_meta", "payload": {
        "id": "root-1", "thread_source": "user", "source": "exec",
        "history_mode": "paginated",
    }})
    child_events.insert(2, {"type": "event_msg", "payload": {
        "type": "task_started",
    }})
    (sessions / "child.jsonl").write_text(
        "\n".join(json.dumps(event) for event in child_events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["n_input_tokens"] == 150
    assert usage["n_cache_tokens"] == 80
    assert usage["n_output_tokens"] == 15
    assert usage["n_reasoning_output_tokens"] == 7
    assert usage["timed_usage_complete"] is True
    assert len(usage["token_usage_events"]) == 2


def test_unknown_child_history_without_baseline_remains_incomplete(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10),
    ])
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(50, 20, 5),
    ], parent="root-1")
    child_events = [json.loads(line) for line in (
        sessions / "child.jsonl").read_text().splitlines()]
    child_events.insert(1, {"type": "session_meta", "payload": {
        "id": "root-1", "thread_source": "user", "source": "exec",
    }})
    child_events.insert(2, {"type": "event_msg", "payload": {
        "type": "task_started",
    }})
    (sessions / "child.jsonl").write_text(
        "\n".join(json.dumps(event) for event in child_events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["n_input_tokens"] == 100


def test_paginated_subagent_followup_keeps_all_cumulative_usage(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10),
    ], history_mode="paginated")
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(50, 20, 5),
    ], parent="root-1", history_mode="paginated")
    child = sessions / "child.jsonl"
    events = [json.loads(line) for line in child.read_text().splitlines()]
    events += [
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
        {"type": "event_msg", "timestamp": "2026-08-17T01:01:00Z",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": _usage(90, 40, 9),
         }}},
    ]
    child.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["n_input_tokens"] == 190
    assert usage["n_cache_tokens"] == 100
    assert usage["n_output_tokens"] == 19


def test_codex_0149_ultra_eight_agents_matches_max_accounting_rules(
    tmp_path: Path,
):
    ultra = tmp_path / "ultra"
    ultra_sessions = ultra / "agent" / "sessions"
    _session(ultra_sessions / "root.jsonl", "root-ultra", "user", [
        _usage(600_000, 200_000, 50_000, 10_000),
    ], history_mode="paginated")
    for index in range(1, 8):
        usages = [_usage(10_000 * index, 5_000 * index,
                         100 * index, 10 * index)]
        if index == 1:
            # Codex 0.149 Sol ultra: one child is reused for a second task.
            usages = [_usage(675_603, 610_560, 7_966, 5_832)]
        path = ultra_sessions / f"child-{index}.jsonl"
        _session(path, f"child-{index}", "subagent", usages,
                 parent="root-ultra", history_mode="paginated")
        if index == 1:
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events += [
                {"type": "event_msg", "payload": {"type": "task_complete"}},
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "turn_context", "payload": {
                    "model": "gpt-5.6-sol",
                }},
                {"type": "event_msg", "timestamp": "2026-08-21T01:01:00Z",
                 "payload": {"type": "token_count", "info": {
                     "total_token_usage": _usage(
                         1_324_584, 1_239_296, 10_467, 7_423,
                     ),
                 }}},
            ]
            path.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n")

    max_trial = tmp_path / "max"
    _session(max_trial / "agent" / "sessions" / "root.jsonl",
             "root-max", "user", [
                 _usage(600_000, 200_000, 50_000, 10_000),
             ], history_mode="paginated")

    ultra_usage = aggregate_codex_session_usage(ultra)
    max_usage = aggregate_codex_session_usage(max_trial)

    assert ultra_usage is not None and ultra_usage["complete"] is True
    assert ultra_usage["agent_session_count"] == 8
    assert ultra_usage["n_input_tokens"] == 2_194_584
    assert ultra_usage["n_cache_tokens"] == 1_574_296
    assert ultra_usage["n_output_tokens"] == 63_167
    assert ultra_usage["n_reasoning_output_tokens"] == 17_693
    assert max_usage is not None and max_usage["complete"] is True
    assert max_usage["agent_session_count"] == 1
    assert max_usage["n_input_tokens"] == 600_000


def test_paginated_subagent_counter_reset_at_followup_stays_incomplete(
    tmp_path: Path,
):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10),
    ], history_mode="paginated")
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(80, 40, 20, 8),
    ], parent="root-1", history_mode="paginated")
    child = sessions / "child.jsonl"
    events = [json.loads(line) for line in child.read_text().splitlines()]
    events += [
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
        # A new prompt can make input/cache exceed the prior epoch even while
        # output/reasoning reveal that the cumulative counter reset.
        {"type": "event_msg", "timestamp": "2026-08-17T01:01:00Z",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": _usage(120, 90, 5, 2),
         }}},
    ]
    child.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["n_input_tokens"] == 100


def test_paginated_counter_reset_without_task_boundary_stays_incomplete(
    tmp_path: Path,
):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [
        _usage(100, 60, 10),
    ], history_mode="paginated")
    _session(sessions / "child.jsonl", "child-1", "subagent", [
        _usage(80, 40, 20), _usage(120, 90, 5),
    ], parent="root-1", history_mode="paginated")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["n_input_tokens"] == 100


def test_missing_child_token_count_marks_aggregate_incomplete(tmp_path: Path):
    sessions = tmp_path / "agent" / "sessions"
    _session(sessions / "root.jsonl", "root-1", "user", [_usage(30, 20, 4)])
    child = sessions / "child.jsonl"
    child.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "child-1", "thread_source": "subagent",
        "source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "root-1"}}},
    }}) + "\n" + json.dumps({"type": "turn_context", "payload": {
        "model": "gpt-5.6-terra"}}) + "\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is False
    assert usage["agent_session_count"] == 2
    assert usage["subagent_session_count"] == 1
    assert usage["n_input_tokens"] == 30


def test_no_sessions_returns_none(tmp_path: Path):
    assert aggregate_codex_session_usage(tmp_path) is None


def test_blank_jsonl_records_do_not_make_complete_usage_incomplete(
    tmp_path: Path,
):
    session = tmp_path / "agent" / "sessions" / "root.jsonl"
    _session(session, "root-1", "user", [_usage(30, 20, 4)])
    lines = session.read_text().splitlines()
    session.write_text("\n\n".join(lines) + "\n\n")

    usage = aggregate_codex_session_usage(tmp_path)

    assert usage is not None and usage["complete"] is True
    assert usage["n_input_tokens"] == 30
    assert usage["timed_usage_complete"] is True


def test_nonblank_parse_damage_has_narrow_single_root_terminal_recovery_fact(
    tmp_path: Path,
):
    session = tmp_path / "agent" / "sessions" / "root.jsonl"
    _session(session, "root-1", "user", [_usage(30, 20, 4)])
    events = session.read_text().splitlines()
    events.insert(-1, "{not-json")
    events.append(json.dumps({
        "type": "event_msg",
        "timestamp": "2026-08-17T01:00:59Z",
        "payload": {"type": "task_complete"},
    }))
    session.write_text("\n".join(events) + "\n")

    bundle = build_codex_trajectory_bundle(tmp_path)

    assert bundle is not None
    assert bundle["complete"] is False
    assert bundle["timed_usage_complete"] is False
    assert bundle["parse_error_count"] == 1
    assert bundle["parse_degraded_completion_eligible"] is True


def test_parse_damage_without_terminal_event_is_not_recoverable(tmp_path: Path):
    session = tmp_path / "agent" / "sessions" / "root.jsonl"
    _session(session, "root-1", "user", [_usage(30, 20, 4)])
    session.write_text(session.read_text() + "{not-json\n")

    bundle = build_codex_trajectory_bundle(tmp_path)

    assert bundle is not None
    assert bundle["parse_degraded_completion_eligible"] is False
