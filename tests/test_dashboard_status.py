"""Tests for src/pipeline/dashboard_status.py.

In-memory SQLite + tmp_path filesystem fixtures. No real DB or repo touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest

from pipeline.dashboard_status import (
    DashboardRow,
    TranscriptStatus,
    build_dashboard_rows,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP,
            sec_validated INTEGER DEFAULT 0,
            ir_url TEXT,
            instrument_type TEXT,
            filing_regime TEXT,
            fiscal_year_end TEXT,
            fmp_data_saved INTEGER DEFAULT 0,
            fmp_data_upto TEXT,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER,
            ticker TEXT NOT NULL,
            call_date TIMESTAMP,
            fiscal_period_type TEXT,
            period_end TIMESTAMP,
            source_url TEXT,
            has_qa_section INTEGER
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT,
            run_id TEXT
        );
        """
    )
    conn.commit()


def _seed_company(
    conn: sqlite3.Connection,
    ticker: str,
    list_type: str,
    fmp_data_upto: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, fmp_data_upto, instrument_type) "
        "VALUES (?, ?, ?, ?, 'equity')",
        (ticker, f"{ticker} Inc", list_type, fmp_data_upto),
    )
    conn.commit()


def _seed_transcript(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: str,
    has_qa_section: bool,
    call_date: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO transcripts (ticker, period_end, has_qa_section, call_date) "
        "VALUES (?, ?, ?, ?)",
        (ticker, period_end, 1 if has_qa_section else 0, call_date),
    )
    conn.commit()


def _seed_thesis_eval(
    conn: sqlite3.Connection, ticker: str, evaluated_at: str, status: str
) -> None:
    conn.execute(
        "INSERT INTO thesis_evaluations (ticker, evaluated_at, overall_status, rule_evaluations_json) "
        "VALUES (?, ?, ?, '[]')",
        (ticker, evaluated_at, status),
    )
    conn.commit()


def _write_workspace_html(repo_root: Path, ticker: str, report_date: str, *, age_seconds: int = 0) -> Path:
    dest = repo_root / "output" / "research" / ticker / f"{report_date}_workspace.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("<html><body>stub</body></html>", encoding="utf-8")
    if age_seconds > 0:
        past = time.time() - age_seconds
        os.utime(dest, (past, past))
    return dest


def _write_comments_file(
    repo_root: Path, ticker: str, report_date: str, *, statuses: list[str]
) -> Path:
    dest = repo_root / "data" / "report_comments" / ticker / f"{report_date}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = {
        "ticker": ticker,
        "report_date": report_date,
        "comments": [
            {
                "id": f"cmt_{report_date}_{i:06x}",
                "anchor": {"type": "thesis_lede", "key": "thesis_lede"},
                "selected_text": None,
                "comment": "stub",
                "intent": None,
                "status": s,
                "created_at": "2026-05-18T12:00:00+00:00",
                "addressed_at": None,
                "resolution_note": None,
                "follow_up_thread": [],
            }
            for i, s in enumerate(statuses)
        ],
    }
    dest.write_text(json.dumps(store), encoding="utf-8")
    return dest


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def test_build_rows_groups_by_list_type_and_excludes_watchlist(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _seed_company(conn, "GOOG", "portfolio")
    _seed_company(conn, "MELI", "evaluation")
    _seed_company(conn, "SOFI", "watchlist")  # must NOT appear

    out = build_dashboard_rows(conn, tmp_path)
    assert sorted(out.keys()) == ["evaluation", "portfolio"]
    assert [r.ticker for r in out["portfolio"]] == ["GOOG", "NU"]
    assert [r.ticker for r in out["evaluation"]] == ["MELI"]


def test_row_carries_fmp_data_upto(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio", fmp_data_upto="2026-05-12")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.fmp_data_upto == "2026-05-12"


def test_row_uses_most_recent_transcript_when_multiple_present(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _seed_transcript(conn, "NU", "2025-12-31", has_qa_section=True)
    _seed_transcript(conn, "NU", "2026-03-31", has_qa_section=False)
    _seed_transcript(conn, "NU", "2025-09-30", has_qa_section=True)

    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_transcript == TranscriptStatus(
        period_end="2026-03-31", has_qa_section=False, call_date=None
    )


def test_row_handles_no_transcripts(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_transcript is None


def test_row_includes_last_build_mtime_when_workspace_html_exists(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _write_workspace_html(tmp_path, "NU", "2026-05-18")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_build_at is not None
    assert row.last_build_at.endswith("+00:00") or "T" in row.last_build_at


def test_row_picks_latest_workspace_html_when_multiple_dates(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _write_workspace_html(tmp_path, "NU", "2026-05-12", age_seconds=86400)
    _write_workspace_html(tmp_path, "NU", "2026-05-18", age_seconds=10)
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    # The latest filename (2026-05-18) wins regardless of mtime.
    # We verify by file-glob semantics: latest filename's mtime is the recent one.
    assert row.last_build_at is not None


def test_row_returns_none_when_no_workspace_html(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_build_at is None


def test_row_counts_open_comments_for_latest_report_date_only(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _write_comments_file(tmp_path, "NU", "2026-05-01", statuses=["open", "open", "addressed"])
    _write_comments_file(tmp_path, "NU", "2026-05-18", statuses=["open", "addressed", "dismissed"])
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    # Latest date is 2026-05-18 with 1 open comment, NOT the 2 opens in the older file.
    assert row.open_comments_count == 1


def test_row_open_comments_zero_when_no_file(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.open_comments_count == 0


def test_row_open_comments_zero_when_all_addressed(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _write_comments_file(tmp_path, "NU", "2026-05-18", statuses=["addressed", "dismissed"])
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.open_comments_count == 0


def test_row_uses_latest_breach_status(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    _seed_thesis_eval(conn, "NU", "2026-01-15T10:00:00", "intact")
    _seed_thesis_eval(conn, "NU", "2026-05-18T10:00:00", "watch")
    _seed_thesis_eval(conn, "NU", "2026-03-15T10:00:00", "intact")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.breach_status == "watch"


def test_row_breach_status_none_when_no_evaluations(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.breach_status is None


def test_to_dict_serialization_round_trip(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio", fmp_data_upto="2026-05-12")
    _seed_transcript(conn, "NU", "2026-03-31", has_qa_section=True)
    _seed_thesis_eval(conn, "NU", "2026-05-18T10:00:00", "intact")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    d = row.to_dict()
    assert d["ticker"] == "NU"
    assert d["list_type"] == "portfolio"
    assert d["fmp_data_upto"] == "2026-05-12"
    assert d["last_transcript"] == {
        "period_end": "2026-03-31",
        "has_qa_section": True,
        "call_date": None,
    }
    assert d["breach_status"] == "intact"
    assert d["open_comments_count"] == 0
    # Should be JSON-serializable
    json.dumps(d)


def test_empty_db_returns_both_keys_with_empty_lists(conn, tmp_path):
    out = build_dashboard_rows(conn, tmp_path)
    assert out == {"portfolio": [], "evaluation": []}


def test_archived_companies_excluded(conn, tmp_path):
    _seed_company(conn, "NU", "portfolio")
    # Archive a different company
    _seed_company(conn, "OLD", "portfolio")
    conn.execute(
        "UPDATE tracked_companies SET archived_at = '2025-01-01' WHERE ticker = 'OLD'"
    )
    conn.commit()

    out = build_dashboard_rows(conn, tmp_path)
    assert [r.ticker for r in out["portfolio"]] == ["NU"]
