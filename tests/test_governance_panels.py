"""Tests for the P3.4 Governance additions: the validation panel (whole-book
data-quality over validation_issues) and the ticker-settings drawer fragment.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pipeline.ticker_settings_panel import render_ticker_settings_panel
from pipeline.validation_issues_panel import (
    load_validation_overview,
    render_validation_panel,
)

_VALIDATION_DDL = """
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id VARCHAR NOT NULL,
    source_doc_id INTEGER,
    ticker VARCHAR,
    severity VARCHAR NOT NULL,
    rule VARCHAR NOT NULL,
    raw_value TEXT,
    expected TEXT,
    raised_at DATETIME NOT NULL,
    resolved_at DATETIME
);
"""


def _seed_validation_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_VALIDATION_DDL)
    rows = [
        ("r1", "NU", "halt", "plausible_range", "NPL=-5 <b>x</b>", "NPL >= 0", "2026-06-08", None),
        ("r1", "NU", "warn", "magnitude_jump", "revenue 5x jump", "within 5x", "2026-06-07", None),
        ("r1", "MELI", "warn", "source_disagreement", "1.01 vs 1.02", "<0.5%", "2026-06-09", None),
        ("r0", "NU", "warn", "unit_mismatch", "old", None, "2026-05-01", "2026-05-02"),
    ]
    conn.executemany(
        "INSERT INTO validation_issues "
        "(run_id, ticker, severity, rule, raw_value, expected, raised_at, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_validation_overview_aggregates(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    _seed_validation_db(db)
    ov = load_validation_overview(db)
    assert ov is not None
    assert (ov.open_total, ov.open_halt, ov.open_warn) == (3, 1, 2)
    assert ov.tickers_affected == 2
    assert ov.resolved_total == 1
    assert ov.last_raised_at is not None and ov.last_raised_at.startswith("2026-06-09")
    assert ("plausible_range", "halt", 1) in ov.by_rule
    # Detail orders halt first, then newest.
    assert ov.detail[0][1] == "halt"


def test_validation_panel_renders_and_escapes(tmp_path: Path) -> None:
    db = tmp_path / "v.db"
    _seed_validation_db(db)
    html = render_validation_panel(db)
    assert "Open · halt" in html
    assert "plausible_range" in html
    # raw_value HTML is escaped, never injected.
    assert "<b>x</b>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_validation_panel_empty_and_missing_states(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(_VALIDATION_DDL)
    conn.commit()
    conn.close()
    html = render_validation_panel(db)
    assert "No open issues." in html
    assert "run_validation_engine" in html

    no_table = tmp_path / "no_table.db"
    sqlite3.connect(no_table).close()
    assert "alembic upgrade head" in render_validation_panel(no_table)
    assert "alembic upgrade head" in render_validation_panel(tmp_path / "missing.db")


def test_validation_panel_renders_resolve_controls(tmp_path: Path) -> None:
    """Each open issue carries a Resolve control keyed by its row id, wired to
    the synchronous /actions/resolve-issue route — NOT the streaming
    data-prov-post path that peeks.py's global listener hijacks."""
    db = tmp_path / "v.db"
    _seed_validation_db(db)
    html = render_validation_panel(db)
    # Open issues are ids 1, 2, 3 (id 4 is resolved → never listed).
    assert 'data-resolve-issue="1"' in html
    assert 'data-resolve-issue="3"' in html
    assert 'data-resolve-issue="4"' not in html
    # The resolve fetch targets the synchronous route, never the streaming one.
    assert "/actions/resolve-issue" in html
    assert "data-prov-post" not in html
    # KPI counts are addressable so the listener can decrement on success.
    assert 'data-vi-count="halt"' in html
    assert 'data-vi-count="resolved"' in html
    # The detail rows render through the shared prov kit, not the old table.
    assert "k-prov-row" in html


def test_validation_panel_no_resolve_control_when_clean(tmp_path: Path) -> None:
    """Empty / missing tables degrade with no resolve control and no crash."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(_VALIDATION_DDL)
    conn.commit()
    conn.close()
    html = render_validation_panel(db)
    assert "No open issues." in html
    assert "data-resolve-issue" not in html
    assert "data-resolve-issue" not in render_validation_panel(tmp_path / "missing.db")


def test_ticker_settings_panel(tmp_path: Path) -> None:
    db = tmp_path / "ts.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE ticker_settings (ticker TEXT PRIMARY KEY, bypass_budget INTEGER, "
        "created_at TEXT, updated_at TEXT);"
    )
    conn.execute(
        "INSERT INTO ticker_settings VALUES ('NU', 1, '2026-06-01T00:00:00', '2026-06-02T00:00:00')"
    )
    conn.commit()
    conn.close()

    html = render_ticker_settings_panel(db)
    assert "NU" in html
    assert 'data-ticker="NU" checked' in html
    assert "/api/ticker-settings/" in html  # the save wiring targets the existing API
    assert 'id="ts-add-btn"' in html

    no_table = tmp_path / "no_table.db"
    sqlite3.connect(no_table).close()
    assert "alembic upgrade head" in render_ticker_settings_panel(no_table)
