"""Tests for src/pipeline/ir_coverage_panel.py — the IR Docs dashboard tab.

Renders a gaps-first coverage table from the live document store + the crawl-health
log. A name with zero ir_doc rows is a gap (manual pull); the reason comes from
ir_fetch_status when present.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.ir_coverage_panel import render_ir_coverage_panel  # noqa: E402


def _build(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
        "archived_at TEXT, fiscal_year_end TEXT)"
    )
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?,?,?)",
        [
            ("NU", "Nu Holdings", "portfolio"),
            ("NOW", "ServiceNow", "portfolio"),
            ("ORCL", "Oracle", "evaluation"),
            ("XYZ", "Watch Co", "watchlist"),  # excluded from the briefed roster
        ],
    )
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, "
        "doc_type TEXT, period_end TEXT, fetched_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO documents (ticker, source_type, doc_type, period_end, fetched_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("NU", "ir_doc", "press_release", "2026-03-31", "2026-06-02T10:00:00"),
            ("NU", "ir_doc", "presentation", "2026-03-31", "2026-06-02T10:05:00"),
        ],
    )
    conn.execute(
        "CREATE TABLE ir_fetch_status (ticker TEXT PRIMARY KEY, last_attempt_at TEXT, "
        "last_status TEXT, discovered INTEGER, downloaded INTEGER, reason TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO ir_fetch_status (ticker, last_attempt_at, last_status, discovered, "
        "downloaded, reason, updated_at) VALUES "
        "('NOW','2026-06-04T01:30:00','failed',0,0,'discover exited 1 (HTTP 403)','2026-06-04T01:30:00')"
    )
    conn.commit()
    conn.close()


def test_panel_surfaces_covered_and_gaps(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _build(db)
    html = render_ir_coverage_panel(db)
    assert "IR document coverage" in html
    # Covered count = 1 (NU) of 3 briefed names (NU, NOW, ORCL); XYZ excluded.
    assert "1/3" in html
    # NU is covered → green pill with doc count + latest period.
    assert "2 docs" in html
    assert "2026-03-31" in html
    # The two gaps render a manual-pull pill; NOW's 403 reason is surfaced.
    assert "manual pull" in html
    assert "HTTP 403" in html
    # ORCL was never crawled → its gap reason names the weekly sweep.
    assert "not yet crawled" in html
    # The how-to names the concrete registration command.
    assert "categorize_ir_uploads.py --ticker" in html


def test_panel_gaps_sort_first(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    _build(db)
    html = render_ir_coverage_panel(db)
    # Gap rows (NOW/ORCL) appear before the covered row (NU) in the table body.
    body = html.split("<tbody>", 1)[1]
    assert body.index("ServiceNow") < body.index("Nu Holdings")
    assert body.index("Oracle") < body.index("Nu Holdings")


def test_panel_empty_roster(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, archived_at TEXT)"
    )
    conn.commit()
    conn.close()
    html = render_ir_coverage_panel(db)
    assert "No portfolio or evaluation names are tracked" in html
