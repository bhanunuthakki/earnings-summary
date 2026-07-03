"""Tests for execution/fmp_backpop.py — diff-aware EDGAR-vs-FMP job selection."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

# fmp_backpop imports save_fmp_data at module scope, which hard-exits if
# FMP_API_KEY is unset (see tests/test_fmp_tier_ladder.py for precedent).
os.environ.setdefault("FMP_API_KEY", "test-key-unused")

from fmp_backpop import SEC_OVERLAP_KEYS, build_manifest, sec_covers_well  # noqa: E402


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            extracted_by TEXT
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ticker TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            raised_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        );
        """
    )
    conn.commit()


def _insert_fact(conn: sqlite3.Connection, ticker: str, period_end: str, extracted_by: str) -> None:
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, line_item, value, extracted_by) "
        "VALUES (?, ?, 'revenue', 100, ?)",
        (ticker, period_end, extracted_by),
    )
    conn.commit()


def _insert_issue(conn: sqlite3.Connection, ticker: str, *, resolved: bool) -> None:
    conn.execute(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raised_at, resolved_at) "
        "VALUES ('run1', ?, 'warn', 'source_disagreement', '2026-01-01', ?)",
        (ticker, "2026-01-02" if resolved else None),
    )
    conn.commit()


def test_sec_covers_well_true_when_recent_fact_no_disagreement() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    covered, count, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is True
    assert count == 1
    assert has_disagreement is False


def test_sec_covers_well_false_when_no_sec_facts() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "FLKR", "2026-06-30", "fmp")
    covered, count, _has_disagreement = sec_covers_well(conn, "FLKR", today=date(2026, 7, 2))
    assert covered is False
    assert count == 0


def test_sec_covers_well_false_when_facts_stale() -> None:
    """fact_count is itself freshness-filtered, so a stale-only fact yields 0 —
    covered=False for the right reason (no recent fact), not an off-by-one."""
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2024-01-01", "sec_xbrl")
    covered, count, _ = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is False
    assert count == 0


def test_sec_covers_well_false_when_unresolved_disagreement() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    _insert_issue(conn, "AAPL", resolved=False)
    covered, _, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is False
    assert has_disagreement is True


def test_sec_covers_well_true_when_disagreement_resolved() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    _insert_issue(conn, "AAPL", resolved=True)
    covered, _, has_disagreement = sec_covers_well(conn, "AAPL", today=date(2026, 7, 2))
    assert covered is True
    assert has_disagreement is False


def test_build_manifest_skips_overlap_jobs_when_covered() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    manifest, decisions = build_manifest(conn, [("AAPL", "portfolio")], today=date(2026, 7, 2))
    assert decisions[0].sec_covered is True
    assert decisions[0].jobs_skipped_via_edgar == len(SEC_OVERLAP_KEYS)
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert manifest_keys.isdisjoint(SEC_OVERLAP_KEYS)


def test_build_manifest_includes_overlap_jobs_when_not_covered() -> None:
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    manifest, decisions = build_manifest(conn, [("FLKR", "etf")], today=date(2026, 7, 2))
    assert decisions[0].sec_covered is False
    assert decisions[0].jobs_skipped_via_edgar == 0
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert SEC_OVERLAP_KEYS.issubset(manifest_keys)


def test_build_manifest_always_includes_non_overlap_jobs() -> None:
    """Segments/growth/TTM/etc. are never skipped, regardless of EDGAR coverage —
    EDGAR structurally cannot provide them."""
    conn = sqlite3.connect(":memory:")
    _create_schema(conn)
    _insert_fact(conn, "AAPL", "2026-06-30", "sec_xbrl")
    manifest, decisions = build_manifest(conn, [("AAPL", "portfolio")], today=date(2026, 7, 2))
    manifest_keys = {(m["endpoint"], m["period"]) for m in manifest}
    assert ("stock-peers", "") in manifest_keys
    assert ("profile", "") in manifest_keys
    assert decisions[0].jobs_included < decisions[0].jobs_total
    assert decisions[0].jobs_included == decisions[0].jobs_total - len(SEC_OVERLAP_KEYS)
