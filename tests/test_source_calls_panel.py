"""Tests for src/pipeline/source_calls_panel.py — the Data Cache dashboard tab.

Renders the cache-effectiveness KPI strip + per-(source, kind) table from the
source_calls provenance log, so cache-hit behaviour is visible in the app and
not only from the show_source_calls CLI (v6 re-grade, Smart caching).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.source_calls_panel import (  # noqa: E402
    render_action_usage_section,
    render_source_calls_panel,
)

_CREATE = """
CREATE TABLE source_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL, kind TEXT NOT NULL, ticker TEXT,
    called_at TEXT NOT NULL, latency_ms INTEGER, status TEXT NOT NULL,
    http_code INTEGER, record_count INTEGER, notes TEXT
)
"""


def _seed(db: Path, rows: list[tuple[str, str, str, int | None, str, int | None]]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_CREATE)
        conn.executemany(
            "INSERT INTO source_calls (source_name, kind, called_at, latency_ms, status, "
            "record_count) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_panel_renders_kpi_strip_and_table(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            ("fmp", "income-statement", "2026-05-10", 120, "ok", 5),
            ("fmp", "income-statement", "2026-05-11", None, "skipped", None),
            ("fmp", "income-statement", "2026-05-12", None, "skipped", None),
            ("fmp", "income-statement", "2026-05-13", None, "skipped", None),
        ],
    )
    html = render_source_calls_panel(db)
    assert "Data fetch cache" in html
    assert "Cache skip rate" in html
    assert "75.0%" in html  # 3 of 4 skipped
    assert "Calls avoided" in html
    assert "income-statement" in html  # per-source row present


def test_panel_shows_dollars_when_cost_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_COST_PER_CALL_USD", "0.50")
    db = tmp_path / "portfolio.db"
    _seed(
        db,
        [
            ("fmp", "ratios", "2026-05-10", 120, "ok", 5),
            ("fmp", "ratios", "2026-05-11", None, "skipped", None),
            ("fmp", "ratios", "2026-05-12", None, "skipped", None),
        ],
    )
    html = render_source_calls_panel(db)
    assert "Cost avoided" in html
    assert "$1.00" in html  # 2 saved * $0.50


def test_panel_empty_state(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed(db, [])
    html = render_source_calls_panel(db)
    assert "No source-call rows yet" in html


# --- Ledger action usage (30d) section --------------------------------------

_CREATE_ACT = """
CREATE TABLE panel_activation_counts (
    panel_id TEXT NOT NULL, day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (panel_id, day)
)
"""


def _seed_acts(db: Path, rows: list[tuple[str, str, int]]) -> None:
    """rows: (panel_id, day 'YYYY-MM-DD', count)."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_CREATE_ACT)
        conn.executemany(
            "INSERT INTO panel_activation_counts (panel_id, day, count) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _day(offset_days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def test_action_usage_families_and_breakdown(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed_acts(
        db,
        [
            # reply family, two intents, across two in-window days → total 9
            ("act:reply:question", _day(1), 5),
            ("act:reply:question", _day(3), 2),
            ("act:reply:save", _day(2), 2),
            # research family: research_run + research_reject collapse together → 6
            ("act:research_run", _day(1), 4),
            ("act:research_reject", _day(2), 2),
            # capture single-segment family → 3
            ("act:capture", _day(1), 3),
            # a shell tab-nav activation — must NOT appear
            ("home", _day(1), 99),
        ],
    )
    html = render_action_usage_section(db)
    assert "Ledger action usage (30d)" in html
    # family totals surface (reply=9 is the largest, ordered first)
    assert ">reply <strong>9<" in html
    assert ">research <strong>6<" in html
    assert ">capture <strong>3<" in html
    # per-action breakdown present
    assert ">question<" in html
    assert ">save<" in html
    # research_run/research_reject collapsed to run/reject actions under research
    assert ">run<" in html
    assert ">reject<" in html
    # non-act: panel_ids are excluded entirely (family label + the raw count)
    assert "home" not in html
    assert "99" not in html
    # reply (9) ordered before research (6) before capture (3)
    assert html.index("reply <strong>9") < html.index("research <strong>6")
    assert html.index("research <strong>6") < html.index("capture <strong>3")


def test_action_usage_excludes_rows_older_than_30d(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed_acts(
        db,
        [
            ("act:reply:question", _day(2), 4),  # in window
            ("act:reply:question", _day(45), 100),  # older than 30d → excluded
            ("act:proposal:accept", _day(60), 50),  # older than 30d → family absent
        ],
    )
    html = render_action_usage_section(db)
    # only the in-window 4 counts; the 100 + 50 are outside the window
    assert ">reply <strong>4<" in html
    assert "100" not in html
    assert "proposal" not in html  # its only rows are older than 30d


def test_action_usage_empty_state(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    # table exists but holds only non-act: rows → no act:* usage to show
    _seed_acts(db, [("home", _day(1), 5), ("holdings", _day(2), 3)])
    html = render_action_usage_section(db)
    assert "No Ledger actions recorded in the last 30 days" in html


def test_action_usage_empty_state_no_table(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    # source_calls exists but panel_activation_counts never created → clean degrade
    _seed(db, [("fmp", "ratios", "2026-05-10", 120, "ok", 5)])
    html = render_action_usage_section(db)
    assert "No Ledger actions recorded in the last 30 days" in html


def test_action_usage_mounted_in_panel(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _seed(db, [("fmp", "ratios", "2026-05-10", 120, "ok", 5)])
    _seed_acts(db, [("act:capture", _day(1), 2)])
    html = render_source_calls_panel(db)
    # both the cache section AND the action-usage section render together
    assert "Data fetch cache" in html
    assert "Ledger action usage (30d)" in html
    assert ">capture <strong>2<" in html
