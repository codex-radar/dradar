import argparse
import json

import pytest

from dradar import cells


TABLE = {
    "cells": {
        "alpha-task|gpt-a|low": {
            "st": "open", "mult": 1.2, "total_n": 3, "rate": 0.5,
            "min": 10, "cost": 1.5,
        },
        "beta-task|gpt-a|high": {
            "st": "cooldown", "mult": 3.0, "total_n": 8, "rate": 1.0,
            "min": 20, "cost": 2.5, "suggest_priority": 100,
            "cost_by_band": {"off_peak": 1.25, "peak": 2.5},
        },
        "gamma-task|gpt-b|high": {
            "st": "open", "mult": 2.5, "total_n": 1, "rate": None,
            "min": None, "cost": None,
        },
        "delta-task|gpt-b|medium": {
            "st": "leased", "total_n": 0, "rate": None,
        },
    }
}


class FakeClient:
    def __init__(self, server, token):
        self.server = server
        self.token = token

    def table(self):
        return TABLE


def _args(**overrides):
    values = dict(
        model=None, effort=None, available=False, state=None, task=None,
        min_multiplier=None, min_tests=None, max_tests=None, min_priority=None,
        min_minutes=None, max_minutes=None, min_cost=None, max_cost=None,
        min_pass_rate=None, max_pass_rate=None,
        price_band="off-peak",
        sort="multiplier", reverse=False, limit=20, all=False, json=True,
        format="table",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _run(monkeypatch, capsys, **overrides):
    monkeypatch.setattr(cells, "_load_config", lambda: {
        "server": "https://api.example.com"
    })
    monkeypatch.setattr(cells, "ApiClient", FakeClient)
    assert cells.cmd_cells(_args(**overrides)) == 0
    return json.loads(capsys.readouterr().out)


def test_cells_filters_model_effort_and_availability(monkeypatch, capsys):
    result = _run(
        monkeypatch, capsys, model=["GPT-B"], effort=["high,ultra"],
        available=True,
    )
    assert result["total"] == 4
    assert result["matched"] == 1
    assert result["cells"][0]["task_id"] == "gamma-task"


def test_cells_numeric_filters_and_multiplier_sort(monkeypatch, capsys):
    result = _run(
        monkeypatch, capsys, min_multiplier=1.1, max_tests=3,
        sort="multiplier",
    )
    assert [row["task_id"] for row in result["cells"]] == [
        "gamma-task", "alpha-task",
    ]


def test_cells_resource_and_pass_rate_ranges_exclude_unknown_values(
        monkeypatch, capsys):
    result = _run(
        monkeypatch, capsys,
        min_minutes=5, max_minutes=15,
        min_cost=1, max_cost=2,
        min_pass_rate=0.5, max_pass_rate=0.8,
    )
    assert [row["task_id"] for row in result["cells"]] == ["alpha-task"]


def test_cells_price_band_applies_to_output_filters_and_sorting(monkeypatch, capsys):
    off_peak = _run(monkeypatch, capsys, model=["gpt-a"], sort="cost")
    assert off_peak["price_band"] == "off-peak"
    beta = next(row for row in off_peak["cells"] if row["task_id"] == "beta-task")
    assert beta["cost"] == 1.25

    peak = _run(
        monkeypatch, capsys, model=["gpt-a"], min_cost=2, sort="cost",
        price_band="peak",
    )
    assert peak["price_band"] == "peak"
    assert [row["task_id"] for row in peak["cells"]] == ["beta-task"]
    assert peak["cells"][0]["cost"] == 2.5


def test_cells_rejects_inverted_resource_ranges(monkeypatch, capsys):
    with pytest.raises(SystemExit, match="--min-minutes cannot be greater"):
        _run(monkeypatch, capsys, min_minutes=20, max_minutes=10)

    with pytest.raises(SystemExit, match="--min-pass-rate cannot be greater"):
        _run(monkeypatch, capsys, min_pass_rate=0.9, max_pass_rate=0.5)


def test_cells_missing_sort_values_stay_last_when_reversed(monkeypatch, capsys):
    result = _run(monkeypatch, capsys, sort="minutes", reverse=True)
    assert [row["task_id"] for row in result["cells"]] == [
        "alpha-task", "beta-task", "gamma-task", "delta-task",
    ]


def test_cells_limit_reports_full_match_count(monkeypatch, capsys):
    result = _run(monkeypatch, capsys, sort="task", limit=2)
    assert result["matched"] == 4
    assert result["shown"] == 2
    assert [row["task_id"] for row in result["cells"]] == [
        "alpha-task", "beta-task",
    ]


def test_cells_without_priority_hides_column_and_rejects_priority_operations(
        monkeypatch, capsys):
    table = {
        "cells": {
            key: {k: v for k, v in value.items() if k != "suggest_priority"}
            for key, value in TABLE["cells"].items()
        }
    }

    class NoPriorityClient(FakeClient):
        def table(self):
            return table

    monkeypatch.setattr(cells, "_load_config", lambda: {
        "server": "https://api.example.com"
    })
    monkeypatch.setattr(cells, "ApiClient", NoPriorityClient)

    assert cells.cmd_cells(_args(json=False)) == 0
    output = capsys.readouterr().out
    assert "PRI" not in output.splitlines()[1]

    with pytest.raises(SystemExit, match="does not publish recommendation priority"):
        cells.cmd_cells(_args(sort="priority"))
    with pytest.raises(SystemExit, match="does not publish recommendation priority"):
        cells.cmd_cells(_args(min_priority=1))


def test_cells_pick_format_keeps_full_task_id(monkeypatch, capsys):
    task = "dynamodb-toolbox-conditional-attribute-requirements"

    class LongTaskClient(FakeClient):
        def table(self):
            return {
                "cells": {
                    f"{task}|gpt-5.6-sol|high": {
                        "st": "open", "mult": 2.0, "total_n": 0,
                    }
                }
            }

    monkeypatch.setattr(cells, "_load_config", lambda: {
        "server": "https://api.example.com"
    })
    monkeypatch.setattr(cells, "ApiClient", LongTaskClient)

    assert cells.cmd_cells(_args(json=False, format="pick")) == 0
    assert capsys.readouterr().out == (
        "dradar go --pick "
        "dynamodb-toolbox-conditional-attribute-requirements:gpt-5.6-sol:high\n"
    )
