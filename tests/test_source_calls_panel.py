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

from pipeline.source_calls_panel import render_source_calls_panel  # noqa: E402

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
