"""position_guard_cache — the materialized data/dashboard/position_guard.json
cache: validated round-trip + atomic-write + tolerant-read behavior."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from position_guard import PositionGuardCheck, PositionGuardReport, PositionGuardRow
from position_guard_cache import (
    cache_path,
    read_position_guard_cache,
    to_cache_model,
    write_position_guard_cache,
)

_STAMP = datetime(2026, 7, 11, 8, 0, 0)


def _report_with_one_violation() -> PositionGuardReport:
    passing = PositionGuardCheck(True, "ok")
    failing = PositionGuardCheck(False, "no downside exit rule on file")
    return PositionGuardReport(
        rows=[
            PositionGuardRow(
                ticker="FLKR",
                weight_pct=6.75,
                downside_trigger=failing,
                realistic_bear=passing,
                thesis_fresh=passing,
            ),
            PositionGuardRow(
                ticker="AAA",
                weight_pct=5.0,
                downside_trigger=passing,
                realistic_bear=passing,
                thesis_fresh=passing,
            ),
        ]
    )


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    report = _report_with_one_violation()
    path = write_position_guard_cache(tmp_path, report, computed_at=_STAMP)
    assert path == cache_path(tmp_path)
    assert path.exists()

    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    assert cache.computed_at == _STAMP.isoformat()
    assert [r.ticker for r in cache.rows] == ["FLKR", "AAA"]
    (flkr,) = [r for r in cache.rows if r.ticker == "FLKR"]
    assert not flkr.passed
    assert flkr.failed_checks == ["downside_trigger"]
    assert cache.violations == [flkr]


def test_write_is_atomic_no_partial_file_on_disk(tmp_path: Path) -> None:
    """After a successful write, no leftover .tmp file remains beside the
    cache (temp file + os.replace, mirroring portfolio_weights)."""
    write_position_guard_cache(tmp_path, _report_with_one_violation(), computed_at=_STAMP)
    dashboard_dir = tmp_path / "data" / "dashboard"
    leftovers = [p for p in dashboard_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_write_empty_report_is_a_legitimate_cache_state(tmp_path: Path) -> None:
    """An empty book (no weighted holdings) still writes a valid cache — not
    a skip condition; the render path needs to distinguish "computed, book
    empty" from "never computed"."""
    write_position_guard_cache(tmp_path, PositionGuardReport(rows=[]), computed_at=_STAMP)
    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    assert cache.rows == []
    assert cache.violations == []


def test_read_tolerates_missing_file(tmp_path: Path) -> None:
    assert read_position_guard_cache(tmp_path) is None


def test_read_tolerates_garbage_json(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert read_position_guard_cache(tmp_path) is None


def test_read_tolerates_schema_drift(tmp_path: Path) -> None:
    """A payload missing required fields (a stale/incompatible shape) degrades
    to None rather than raising a pydantic ValidationError up through the
    render path."""
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": "not-a-list"}), encoding="utf-8")
    assert read_position_guard_cache(tmp_path) is None


def test_read_tolerates_non_dict_json(tmp_path: Path) -> None:
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_position_guard_cache(tmp_path) is None


def test_to_cache_model_defaults_computed_at_to_now() -> None:
    model = to_cache_model(_report_with_one_violation())
    # A valid ISO stamp was written, not left blank.
    assert datetime.fromisoformat(model.computed_at)


def test_second_write_overwrites_first(tmp_path: Path) -> None:
    write_position_guard_cache(tmp_path, PositionGuardReport(rows=[]), computed_at=_STAMP)
    write_position_guard_cache(tmp_path, _report_with_one_violation(), computed_at=_STAMP)
    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    assert len(cache.rows) == 2


# ---------------------------------------------------------------------------
# add_trigger backward-compat (PR9, Bull-side symmetry): a cache written
# before this field existed must still validate + render, with add_trigger
# defaulting to None (not applicable) rather than failing schema validation.
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips_add_trigger(tmp_path: Path) -> None:
    passing = PositionGuardCheck(True, "ok")
    failing = PositionGuardCheck(False, "no add-rung on file")
    report = PositionGuardReport(
        rows=[
            PositionGuardRow(
                ticker="NU",
                weight_pct=10.0,
                downside_trigger=passing,
                realistic_bear=passing,
                thesis_fresh=passing,
                add_trigger=failing,
            )
        ]
    )
    write_position_guard_cache(tmp_path, report, computed_at=_STAMP)
    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    (row,) = cache.rows
    assert row.add_trigger is not None
    assert not row.add_trigger.passed
    assert row.passed  # advisory never flips the violation-grade result


def test_read_tolerates_pre_pr9_cache_missing_add_trigger_key(tmp_path: Path) -> None:
    """A cache JSON written by the pre-PR9 code (no add_trigger key at all in
    any row) must still validate — the field defaults to None per-row rather
    than raising, so an old cache renders with no advisory row until the
    nightly stage recomputes it."""
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "computed_at": _STAMP.isoformat(),
        "rows": [
            {
                "ticker": "NU",
                "weight_pct": 10.0,
                "passed": True,
                "failed_checks": [],
                "downside_trigger": {"passed": True, "reason": "ok"},
                "realistic_bear": {"passed": True, "reason": "ok"},
                "thesis_fresh": {"passed": True, "reason": "ok"},
                # no "add_trigger" key at all — the pre-PR9 shape.
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    cache = read_position_guard_cache(tmp_path)
    assert cache is not None
    (row,) = cache.rows
    assert row.add_trigger is None
