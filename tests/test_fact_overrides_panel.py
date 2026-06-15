"""Tests for the read-only fact-overrides panel (src/pipeline/fact_overrides_panel.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.fact_overrides_panel import collect_rows, render_fact_overrides_panel

_DDL = """
CREATE TABLE fact_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    action TEXT NOT NULL,
    value NUMERIC,
    unit TEXT,
    value_json TEXT,
    source_doc_type TEXT NOT NULL,
    source_accession TEXT,
    source_exhibit TEXT,
    source_url TEXT,
    source_excerpt TEXT,
    source_doc_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);
"""


def _db(tmp_path: Path, *, with_table: bool = True, rows: bool = True) -> Path:
    path = tmp_path / "p.db"
    conn = sqlite3.connect(str(path))
    if with_table:
        conn.executescript(_DDL)
        if rows:
            conn.executemany(
                "INSERT INTO fact_overrides (ticker, period_end, fiscal_period_type, fact_kind, "
                "fact_key, action, value, unit, value_json, source_doc_type, source_accession, "
                "source_exhibit, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "GOOG",
                        "2025-12-31",
                        "Q4",
                        "segment",
                        "product",
                        "replace",
                        None,
                        None,
                        '{"Google Cloud": 17664000000, "Other Bets": 370000000}',
                        "sec_8k",
                        "0001652044-26-000012",
                        "googexhibit991q42025.htm",
                        "active",
                        "cli",
                        "2026-06-14 00:00",
                    ),
                    (
                        "GOOG",
                        "2025-12-31",
                        "Q4",
                        "kpi",
                        "Google Cloud revenue growth",
                        "replace",
                        48,
                        "percent",
                        None,
                        "ir_press_release",
                        None,
                        None,
                        "active",
                        "cli",
                        "2026-06-14 00:00",
                    ),
                    (
                        "META",
                        "2025-09-30",
                        "Q3",
                        "financial_fact",
                        "revenue",
                        "replace",
                        40000000000,
                        "actual",
                        None,
                        "sec_8k",
                        None,
                        None,
                        "retired",
                        "cli",
                        "2026-05-01 00:00",
                    ),
                ],
            )
    conn.commit()
    conn.close()
    return path


def test_collect_rows_active_only(tmp_path: Path) -> None:
    rows = collect_rows(_db(tmp_path))
    assert len(rows) == 2  # the retired META row is excluded
    keys = {(r.ticker, r.fact_kind) for r in rows}
    assert ("GOOG", "segment") in keys
    assert ("GOOG", "kpi") in keys
    assert ("META", "financial_fact") not in keys


def test_render_shows_overrides(tmp_path: Path) -> None:
    html = render_fact_overrides_panel(_db(tmp_path))
    assert "Overrides" in html
    assert "GOOG" in html
    assert "0001652044-26-000012" in html
    assert "2 segments" in html  # the segment value_json summary
    assert "48 percent" in html  # the kpi scalar summary
    assert "META" not in html  # retired row not shown


def test_render_empty_states(tmp_path: Path) -> None:
    # No rows.
    assert "No active company-doc overrides" in render_fact_overrides_panel(
        _db(tmp_path, rows=False)
    )
    # No table (pre-0111 DB).
    assert "No active company-doc overrides" in render_fact_overrides_panel(
        _db(tmp_path, with_table=False)
    )
    # No DB file.
    assert collect_rows(tmp_path / "absent.db") == []
