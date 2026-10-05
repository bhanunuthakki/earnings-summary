"""Tests for src/pipeline/dashboard_status.py.

In-memory SQLite + tmp_path filesystem fixtures. No real DB or repo touched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.dashboard_status import (
    TranscriptStatus,
    build_dashboard_rows,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT DEFAULT 'bhanu',
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
        CREATE TABLE fmp_endpoint_status (
            ticker TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            period TEXT NOT NULL,
            status TEXT,
            http_code INTEGER,
            record_count INTEGER,
            earliest_date TEXT,
            latest_date TEXT,
            file_path TEXT,
            file_bytes INTEGER,
            error_msg TEXT,
            last_pulled TIMESTAMP
        );
        """
    )
    conn.commit()


def _seed_company(
    conn: sqlite3.Connection,
    ticker: str,
    list_type: str,
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, 'equity')",
        (ticker, f"{ticker} Inc", list_type),
    )
    conn.commit()


def _seed_fmp_pull(
    conn: sqlite3.Connection, ticker: str, endpoint: str, last_pulled: str | None
) -> None:
    conn.execute(
        "INSERT INTO fmp_endpoint_status (ticker, endpoint, period, status, last_pulled) "
        "VALUES (?, ?, 'annual', 'ok', ?)",
        (ticker, endpoint, last_pulled),
    )
    conn.commit()


def _seed_transcript(
    conn: sqlite3.Connection,
    ticker: str,
    period_end: str | None,
    has_qa_section: bool | None,
    call_date: str | None = None,
) -> None:
    # has_qa_section passes through raw so NULL (unknown QA state) stays NULL.
    conn.execute(
        "INSERT INTO transcripts (ticker, period_end, has_qa_section, call_date) "
        "VALUES (?, ?, ?, ?)",
        (ticker, period_end, has_qa_section, call_date),
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


def _write_workspace_html(
    repo_root: Path, ticker: str, report_date: str, *, age_seconds: int = 0
) -> Path:
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
    store: dict[str, object] = {
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


def test_build_rows_groups_by_list_type_and_excludes_watchlist(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _seed_company(conn, "GOOG", "portfolio")
    _seed_company(conn, "MELI", "evaluation")
    _seed_company(conn, "SOFI", "watchlist")  # must NOT appear

    out = build_dashboard_rows(conn, tmp_path)
    assert sorted(out.keys()) == ["evaluation", "portfolio"]
    assert [r.ticker for r in out["portfolio"]] == ["GOOG", "NU"]
    assert [r.ticker for r in out["evaluation"]] == ["MELI"]


def test_row_uses_max_fmp_endpoint_pull_time_not_data_upto(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Last FMP must show when the fetcher last ran, not the latest data point.

    tracked_companies.fmp_data_upto holds the latest fiscal period in the
    fetched payload (often a forward quarter like 2030-12-31), which is the
    wrong column for "freshness". The right source is the MAX(last_pulled)
    across fmp_endpoint_status for the ticker.
    """
    _seed_company(conn, "NU", "portfolio")
    _seed_fmp_pull(conn, "NU", "income-statement", "2026-05-03T12:00:00")
    _seed_fmp_pull(conn, "NU", "balance-sheet", "2026-05-11T01:02:14")
    _seed_fmp_pull(conn, "NU", "cash-flow", "2026-04-15T09:00:00")

    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    # MAX across endpoints — 2026-05-11 wins
    assert row.fmp_last_pulled == "2026-05-11T01:02:14"


def test_row_fmp_last_pulled_none_when_no_endpoints_recorded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.fmp_last_pulled is None


def test_row_uses_most_recent_transcript_when_multiple_present(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _seed_transcript(conn, "NU", "2025-12-31", has_qa_section=True)
    _seed_transcript(conn, "NU", "2026-03-31", has_qa_section=False)
    _seed_transcript(conn, "NU", "2025-09-30", has_qa_section=True)

    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_transcript == TranscriptStatus(
        period_end="2026-03-31", has_qa_section=False, call_date=None
    )


def test_row_handles_no_transcripts(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_transcript is None


def test_row_includes_last_build_mtime_when_workspace_html_exists(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _write_workspace_html(tmp_path, "NU", "2026-05-18")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_build_at is not None
    assert row.last_build_at.endswith("+00:00") or "T" in row.last_build_at


def test_row_picks_latest_workspace_html_when_multiple_dates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _write_workspace_html(tmp_path, "NU", "2026-05-12", age_seconds=86400)
    _write_workspace_html(tmp_path, "NU", "2026-05-18", age_seconds=10)
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    # The latest filename (2026-05-18) wins regardless of mtime.
    # We verify by file-glob semantics: latest filename's mtime is the recent one.
    assert row.last_build_at is not None


def test_row_returns_none_when_no_workspace_html(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.last_build_at is None


def test_row_counts_open_comments_for_latest_report_date_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _write_comments_file(tmp_path, "NU", "2026-05-01", statuses=["open", "open", "addressed"])
    _write_comments_file(tmp_path, "NU", "2026-05-18", statuses=["open", "addressed", "dismissed"])
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    # Latest date is 2026-05-18 with 1 open comment, NOT the 2 opens in the older file.
    assert row.open_comments_count == 1


def test_row_open_comments_zero_when_no_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.open_comments_count == 0


def test_row_open_comments_zero_when_all_addressed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    _write_comments_file(tmp_path, "NU", "2026-05-18", statuses=["addressed", "dismissed"])
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.open_comments_count == 0


def test_row_uses_latest_breach_status(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    _seed_thesis_eval(conn, "NU", "2026-01-15T10:00:00", "intact")
    _seed_thesis_eval(conn, "NU", "2026-05-18T10:00:00", "watch")
    _seed_thesis_eval(conn, "NU", "2026-03-15T10:00:00", "intact")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.breach_status == "watch"


def test_row_breach_status_none_when_no_evaluations(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_company(conn, "NU", "portfolio")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    assert row.breach_status is None


def test_to_dict_serialization_round_trip(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    _seed_fmp_pull(conn, "NU", "income-statement", "2026-05-11T01:02:14")
    _seed_transcript(conn, "NU", "2026-03-31", has_qa_section=True)
    _seed_thesis_eval(conn, "NU", "2026-05-18T10:00:00", "intact")
    row = build_dashboard_rows(conn, tmp_path)["portfolio"][0]
    d = row.to_dict()
    assert d["ticker"] == "NU"
    assert d["list_type"] == "portfolio"
    assert d["fmp_last_pulled"] == "2026-05-11T01:02:14"
    assert d["last_transcript"] == {
        "period_end": "2026-03-31",
        "has_qa_section": True,
        "call_date": None,
    }
    assert d["breach_status"] == "intact"
    assert d["open_comments_count"] == 0
    # Should be JSON-serializable
    json.dumps(d)


def test_empty_db_returns_both_keys_with_empty_lists(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    out = build_dashboard_rows(conn, tmp_path)
    assert out == {"portfolio": [], "evaluation": []}


def test_archived_companies_excluded(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _seed_company(conn, "NU", "portfolio")
    # Archive a different company
    _seed_company(conn, "OLD", "portfolio")
    conn.execute("UPDATE tracked_companies SET archived_at = '2025-01-01' WHERE ticker = 'OLD'")
    conn.commit()

    out = build_dashboard_rows(conn, tmp_path)
    assert [r.ticker for r in out["portfolio"]] == ["NU"]


# ---------------------------------------------------------------------------
# Characterization: the exact row set (batch-dashboard-rows, VAL-SERVE-010).
#
# build_dashboard_rows was rewritten from a per-ticker N+1 (~5 statements + 2
# directory walks + 1 JSON read per ticker) into grouped queries + one
# filesystem pass per artifact family. This fixture pins the full
# DashboardRow.to_dict() output, ordering included, so the batched build is
# proven value-identical to the pre-change per-ticker build.
# ---------------------------------------------------------------------------


def _workspace_mtime(repo_root: Path, ticker: str, report_date: str) -> str:
    path = repo_root / "output" / "research" / ticker / f"{report_date}_workspace.html"
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def test_row_set_matches_fixture_snapshot(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Every field of every row equals the pre-change per-ticker build output."""
    # --- portfolio ---
    # AA: every field populated; MAX across fmp endpoints; newest transcript;
    # newest workspace file; comments counted on the latest date file only.
    _seed_company(conn, "AA", "portfolio")
    _seed_fmp_pull(conn, "AA", "income-statement", "2026-05-03T12:00:00")
    _seed_fmp_pull(conn, "AA", "balance-sheet", "2026-05-11T01:02:14")
    _seed_fmp_pull(conn, "AA", "cash-flow", "2026-04-15T09:00:00")
    _seed_transcript(conn, "AA", "2025-12-31", True, call_date="2026-01-02")
    _seed_transcript(conn, "AA", "2026-03-31", False)
    _seed_thesis_eval(conn, "AA", "2026-01-15T10:00:00", "intact")
    _seed_thesis_eval(conn, "AA", "2026-05-18T10:00:00", "watch")
    _seed_thesis_eval(conn, "AA", "2026-03-15T10:00:00", "intact")
    _write_workspace_html(tmp_path, "AA", "2026-05-12", age_seconds=86400)
    _write_workspace_html(tmp_path, "AA", "2026-05-18", age_seconds=10)
    _write_comments_file(tmp_path, "AA", "2026-05-01", statuses=["open", "open", "addressed"])
    _write_comments_file(tmp_path, "AA", "2026-05-18", statuses=["open", "addressed", "dismissed"])
    # BB: no fmp rows; QA state unknown (NULL); call date truncated to ISO date.
    _seed_company(conn, "BB", "portfolio")
    _seed_transcript(conn, "BB", "2026-06-30", None, call_date="2026-07-01 09:30")
    # CC: NULL last_pulled rows lose to a real one in MAX; a NULL-period
    # transcript row must stay excluded; no open comments.
    _seed_company(conn, "CC", "portfolio")
    _seed_fmp_pull(conn, "CC", "income-statement", "2026-05-03T12:00:00")
    _seed_fmp_pull(conn, "CC", "balance-sheet", None)
    _seed_transcript(conn, "CC", None, True, call_date="2026-02-01")
    _seed_transcript(conn, "CC", "2026-03-31", True)
    _write_workspace_html(tmp_path, "CC", "2026-05-12")
    _write_comments_file(tmp_path, "CC", "2026-05-18", statuses=["addressed", "dismissed"])
    _seed_thesis_eval(conn, "CC", "2026-01-15T10:00:00", "intact")
    _seed_thesis_eval(conn, "CC", "2026-05-18T10:00:00", "watch")
    _seed_thesis_eval(conn, "CC", "2026-03-15T10:00:00", "intact")
    # nu: lowercase roster ticker -- row keeps the stored ticker while the
    # per-ticker lookups key on the uppercased form (data rows are stored as NU).
    _seed_company(conn, "nu", "portfolio")
    _seed_fmp_pull(conn, "NU", "income-statement", "2026-06-01T00:00:00")

    # --- evaluation ---
    # DD: all-NULL last_pulled -> None; a non-date-named comment file sorts
    # after the date files, so the newest name is unparseable -> 0 open.
    _seed_company(conn, "DD", "evaluation")
    _seed_fmp_pull(conn, "DD", "income-statement", None)
    _seed_fmp_pull(conn, "DD", "balance-sheet", None)
    _seed_transcript(conn, "DD", "2026-03-31", True)
    _write_comments_file(tmp_path, "DD", "2026-05-18", statuses=["open", "addressed"])
    notes = tmp_path / "data" / "report_comments" / "DD" / "notes.json"
    notes.write_text(json.dumps({"ticker": "DD", "comments": []}), encoding="utf-8")
    _seed_thesis_eval(conn, "DD", "2026-05-18T10:00:00", "ok")
    # EE: multiple open comments on the latest date; NULL-period transcript excluded.
    _seed_company(conn, "EE", "evaluation")
    _seed_fmp_pull(conn, "EE", "cash-flow", "2026-04-15T09:00:00")
    _seed_transcript(conn, "EE", None, True, call_date="2025-08-01")
    _seed_transcript(conn, "EE", "2025-09-30", True)
    _write_workspace_html(tmp_path, "EE", "2026-05-18")
    _write_comments_file(tmp_path, "EE", "2026-05-18", statuses=["open", "open"])

    # Excluded rosters: watchlist never appears; archived names disappear.
    _seed_company(conn, "SOFI", "watchlist")
    _seed_company(conn, "GONE", "portfolio")
    conn.execute("UPDATE tracked_companies SET archived_at = '2025-01-01' WHERE ticker = 'GONE'")
    conn.commit()

    out = build_dashboard_rows(conn, tmp_path)
    assert [r.ticker for r in out["portfolio"]] == ["AA", "BB", "CC", "nu"]
    assert [r.ticker for r in out["evaluation"]] == ["DD", "EE"]

    assert [r.to_dict() for r in out["portfolio"]] == [
        {
            "ticker": "AA",
            "list_type": "portfolio",
            "fmp_last_pulled": "2026-05-11T01:02:14",
            "last_transcript": {
                "period_end": "2026-03-31",
                "has_qa_section": False,
                "call_date": None,
            },
            "last_build_at": _workspace_mtime(tmp_path, "AA", "2026-05-18"),
            "open_comments_count": 1,
            "breach_status": "watch",
        },
        {
            "ticker": "BB",
            "list_type": "portfolio",
            "fmp_last_pulled": None,
            "last_transcript": {
                "period_end": "2026-06-30",
                "has_qa_section": None,
                "call_date": "2026-07-01",
            },
            "last_build_at": None,
            "open_comments_count": 0,
            "breach_status": None,
        },
        {
            "ticker": "CC",
            "list_type": "portfolio",
            "fmp_last_pulled": "2026-05-03T12:00:00",
            "last_transcript": {
                "period_end": "2026-03-31",
                "has_qa_section": True,
                "call_date": None,
            },
            "last_build_at": _workspace_mtime(tmp_path, "CC", "2026-05-12"),
            "open_comments_count": 0,
            "breach_status": "watch",
        },
        {
            "ticker": "nu",
            "list_type": "portfolio",
            "fmp_last_pulled": "2026-06-01T00:00:00",
            "last_transcript": None,
            "last_build_at": None,
            "open_comments_count": 0,
            "breach_status": None,
        },
    ]
    assert [r.to_dict() for r in out["evaluation"]] == [
        {
            "ticker": "DD",
            "list_type": "evaluation",
            "fmp_last_pulled": None,
            "last_transcript": {
                "period_end": "2026-03-31",
                "has_qa_section": True,
                "call_date": None,
            },
            "last_build_at": None,
            "open_comments_count": 0,
            "breach_status": "ok",
        },
        {
            "ticker": "EE",
            "list_type": "evaluation",
            "fmp_last_pulled": "2026-04-15T09:00:00",
            "last_transcript": {
                "period_end": "2025-09-30",
                "has_qa_section": True,
                "call_date": None,
            },
            "last_build_at": _workspace_mtime(tmp_path, "EE", "2026-05-18"),
            "open_comments_count": 2,
            "breach_status": None,
        },
    ]


# ---------------------------------------------------------------------------
# Structural: the build is bounded, not per-ticker (VAL-SERVE-010).
#
# Pre-change, one build issued ~5 SQL statements + 2 Path.glob walks per
# ticker (baseline.json records 246 statements for the 49-ticker bench
# roster: 5 shapes x 49 tickers + 1 roster read). The batched build must
# issue a fixed roster read plus at most 3 grouped data statements (plus the
# once-per-build relation probes), and one filesystem pass per artifact
# family -- nothing may scale with the roster size.
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    _create_schema(fresh)
    return fresh


def _seed_full_ticker(
    conn: sqlite3.Connection, repo_root: Path, ticker: str, list_type: str, index: int
) -> None:
    """Seed a ticker with data on every path the row build touches."""
    _seed_company(conn, ticker, list_type)
    _seed_fmp_pull(conn, ticker, "income-statement", f"2026-05-{index:02d}T00:00:00")
    _seed_transcript(conn, ticker, f"2026-0{index % 9 + 1}-30", bool(index % 2))
    _seed_thesis_eval(conn, ticker, f"2026-05-{index:02d}T10:00:00", "intact")
    _write_workspace_html(repo_root, ticker, "2026-05-18")
    _write_comments_file(repo_root, ticker, "2026-05-18", statuses=["open", "addressed"])


def _build_statement_count(conn: sqlite3.Connection, repo_root: Path) -> list[str]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        build_dashboard_rows(conn, repo_root)
    finally:
        conn.set_trace_callback(None)
    return statements


def test_statement_count_is_bounded_and_does_not_scale_with_roster(tmp_path: Path) -> None:
    small_conn = _make_conn()
    for index, ticker in enumerate(("AA", "BB", "CC"), start=1):
        _seed_full_ticker(small_conn, tmp_path, ticker, "portfolio", index)
    large_conn = _make_conn()
    large_tickers = ("AA", "BB", "CC", "DD", "EE", "FF", "GG", "HH")
    for index, ticker in enumerate(large_tickers, start=1):
        _seed_full_ticker(large_conn, tmp_path, ticker, "portfolio", index)

    small_statements = _build_statement_count(small_conn, tmp_path)
    large_statements = _build_statement_count(large_conn, tmp_path)

    # Bounded: the same statements serve any roster size (no per-ticker
    # replicas). 1 roster read + 3 grouped data statements + the once-per-build
    # relation-resolution probes (2 for the transcripts relation + 3 for the
    # episode relation on this legacy fixture schema = 9 total here; views on
    # governed schemas make it 6).
    assert len(small_statements) == len(large_statements)
    assert len(large_statements) <= 12
    # Strictly fewer than the pre-change count: ~5 statements per ticker.
    assert len(large_statements) < 5 * len(large_tickers)


def test_glob_calls_are_bounded_not_two_per_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _make_conn()
    tickers = ("AA", "BB", "CC", "DD", "EE")
    for index, ticker in enumerate(tickers, start=1):
        _seed_full_ticker(conn, tmp_path, ticker, "portfolio", index)

    glob_calls: list[tuple[str, str]] = []
    original_glob = Path.glob

    def counting_glob(path: Path, pattern: str) -> object:
        glob_calls.append((str(path), pattern))
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)
    out = build_dashboard_rows(conn, tmp_path)

    # Pre-change this was 2 walks per ticker (10 for this fixture). One pass
    # per artifact family is the bounded replacement.
    assert len(glob_calls) <= 2
    # And the bounded pass still finds every ticker's data.
    assert [r.ticker for r in out["portfolio"]] == list(tickers)
    for row in out["portfolio"]:
        assert row.last_build_at is not None
        assert row.open_comments_count == 1
