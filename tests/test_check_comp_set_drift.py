"""Unit tests for execution.check_comp_set_drift
(docs/design/comparable_sets_bottoms_up.md §7, Phase 2).

Exercises the module's pure functions directly (in-process import of the
execution/ script, same pattern other execution/*.py test files in this repo
use) rather than shelling out -- faster and gives direct access to the
computed rows for assertion.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_SPEC = importlib.util.spec_from_file_location(
    "check_comp_set_drift", PROJECT_ROOT / "execution" / "check_comp_set_drift.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check_comp_set_drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_comp_set_drift)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comp_set_metrics_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_type TEXT, scope_key TEXT, as_of_date TEXT, metric TEXT, stat_type TEXT, "
        "value REAL, n_members INTEGER, n_valid INTEGER, coverage_pct REAL, "
        "method_version INTEGER, method_flags TEXT, computed_at TEXT)"
    )
    return conn


def _insert_row(
    conn: sqlite3.Connection,
    scope_type: str,
    scope_key: str,
    as_of: str,
    value: float | None,
    *,
    metric: str = "pe_ttm",
    stat_type: str = "median",
    method_version: int = 1,
    n_members: int = 10,
    n_valid: int = 10,
) -> None:
    conn.execute(
        "INSERT INTO comp_set_metrics_daily (scope_type, scope_key, as_of_date, metric, "
        "stat_type, value, n_members, n_valid, coverage_pct, method_version, method_flags, "
        "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '2026-07-17T00:00:00')",
        (
            scope_type,
            scope_key,
            as_of,
            metric,
            stat_type,
            value,
            n_members,
            n_valid,
            n_valid / n_members if n_members else 0.0,
            method_version,
        ),
    )
    conn.commit()


def _write_snapshot(tmp_path: Path, filename: str, rows: list[dict[str, object]]) -> None:
    d = tmp_path / "data" / "historical" / "sector_industry"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(json.dumps(rows), encoding="utf-8")


# ---------------------------------------------------------------------------
# _load_fmp_snapshot
# ---------------------------------------------------------------------------


def test_load_fmp_snapshot_missing_file_degrades_to_empty(tmp_path: Path) -> None:
    out = check_comp_set_drift._load_fmp_snapshot(tmp_path, "industry_pe_snapshot.json", "industry")
    assert out == {}


def test_load_fmp_snapshot_parses_real_shape(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path,
        "industry_pe_snapshot.json",
        [
            {
                "date": "2026-05-19",
                "industry": "Software - Application",
                "exchange": "NASDAQ",
                "pe": 70.4,
            },
            {
                "date": "2026-05-19",
                "industry": "Banks - Regional",
                "exchange": "NASDAQ",
                "pe": 12.1,
            },
        ],
    )
    out = check_comp_set_drift._load_fmp_snapshot(tmp_path, "industry_pe_snapshot.json", "industry")
    assert out["Software - Application"] == (70.4, "2026-05-19")
    assert out["Banks - Regional"] == (12.1, "2026-05-19")


# ---------------------------------------------------------------------------
# _load_bottoms_up
# ---------------------------------------------------------------------------


def test_load_bottoms_up_picks_nearest_prior_date() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "Software - Application", "2026-07-10", 60.0)
    _insert_row(conn, "industry", "Software - Application", "2026-07-15", 65.0)
    out = check_comp_set_drift._load_bottoms_up(conn, "industry", date(2026, 7, 17), 1)
    assert out["Software - Application"][0] == 65.0
    assert out["Software - Application"][1] == "2026-07-15"


def test_load_bottoms_up_ignores_future_dates() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "Software - Application", "2026-07-20", 999.0)
    out = check_comp_set_drift._load_bottoms_up(conn, "industry", date(2026, 7, 17), 1)
    assert "Software - Application" not in out


def test_load_bottoms_up_only_pe_ttm_median() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "X", "2026-07-17", 10.0, metric="ev_ebitda_ttm")
    _insert_row(conn, "industry", "X", "2026-07-17", 20.0, stat_type="aggregate")
    out = check_comp_set_drift._load_bottoms_up(conn, "industry", date(2026, 7, 17), 1)
    assert "X" not in out


# ---------------------------------------------------------------------------
# _compare_scope / drift flagging
# ---------------------------------------------------------------------------


def test_drift_flagged_beyond_threshold() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "Software - Application", "2026-07-17", 100.0)
    fmp = {"Software - Application": (70.0, "2026-05-19")}  # (100-70)/70 = 42.8% > 25%
    results = check_comp_set_drift._compare_scope(conn, "industry", fmp, date(2026, 7, 17), 1)
    assert len(results) == 1
    assert results[0]["flagged"] is True
    assert results[0]["drift_pct"] > check_comp_set_drift.DRIFT_ALERT_THRESHOLD


def test_drift_not_flagged_within_threshold() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "Software - Application", "2026-07-17", 72.0)
    fmp = {"Software - Application": (70.0, "2026-05-19")}  # ~2.9% drift
    results = check_comp_set_drift._compare_scope(conn, "industry", fmp, date(2026, 7, 17), 1)
    assert results[0]["flagged"] is False


def test_drift_skips_keys_missing_on_either_side() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "OnlyOurs", "2026-07-17", 50.0)
    fmp = {"OnlyFmp": (50.0, "2026-05-19")}
    results = check_comp_set_drift._compare_scope(conn, "industry", fmp, date(2026, 7, 17), 1)
    assert results == []


def test_drift_skips_null_bottoms_up_value() -> None:
    conn = _make_conn()
    _insert_row(conn, "industry", "Undefined", "2026-07-17", None)
    fmp = {"Undefined": (50.0, "2026-05-19")}
    results = check_comp_set_drift._compare_scope(conn, "industry", fmp, date(2026, 7, 17), 1)
    assert results == []
