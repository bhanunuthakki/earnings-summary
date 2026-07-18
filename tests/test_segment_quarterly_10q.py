# pyright: reportPrivateUsage=false
# This test drives compute.segment_quarterly_10q's internal helpers
# (_row_metric) directly — the same convention tests/test_generic_xbrl_
# capture.py uses for generic_xbrl_capture's private helpers.
"""Tests for compute.segment_quarterly_10q — the deterministic 10-Q segment
extractor + LLM Stage-B period-axis disambiguation fallback
(segment_quarterly_framework.md, Phase 1).

Covers: row-label metric classification, an end-to-end extraction against a
synthetic 10-Q-shaped FMP JSON (Q1 discrete + Q2 cumulative), the Q2
one-hop discrete derivation (§2.6), and the LLM Stage-B fallback gate for a
column the deterministic parser can't resolve (mocked — no real LLM call).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.segment_quarterly_10q as sq10q  # noqa: E402

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    fiscal_year_end TEXT
);
CREATE TABLE segment_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    source_doc_id INTEGER NOT NULL REFERENCES documents(id),
    currency VARCHAR(8),
    unit VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    period_basis VARCHAR(16) NOT NULL DEFAULT 'discrete',
    raw_period_label TEXT,
    method_version VARCHAR(32),
    CONSTRAINT uq_segment_periods_provenance UNIQUE
      (ticker, period_end, fiscal_period_type, source_doc_id)
);
CREATE TABLE segment_dimensions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id INTEGER NOT NULL REFERENCES segment_periods(id),
    dim_type VARCHAR(16) NOT NULL,
    dim_name VARCHAR(128) NOT NULL,
    value NUMERIC(20, 4) NOT NULL,
    metric VARCHAR(32) NOT NULL,
    unit VARCHAR(16),
    disclosure_status VARCHAR(16) NOT NULL DEFAULT 'reported',
    method_version VARCHAR(32),
    confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by VARCHAR(64),
    locator TEXT,
    derived_from TEXT,
    supersedes_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC NOT NULL
);
"""


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _insert_document(conn: sqlite3.Connection, *, doc_id: int, ticker: str, file_path: str) -> None:
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (?, ?, 'fmp', 'fmp_10q_json', ?, ?, ?, 'ok', 1)",
        (doc_id, ticker, file_path, "0" * 64, datetime.now()),
    )
    conn.commit()


def _insert_tracked_company(conn: sqlite3.Connection, ticker: str, fye: str) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, fiscal_year_end) VALUES (?, ?)", (ticker, fye)
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _write_10q_json(
    repo_root: Path, ticker: str, year: int, quarter: str, payload: Mapping[str, object]
) -> Path:
    out_dir = repo_root / "data" / "historical" / "fmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ticker}_form_10q_{year}_{quarter}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _segment_section(
    period_label: str, segment_rows: dict[str, dict[str, float]]
) -> list[dict[str, object]]:
    """One FMP-shaped segment section: title header, axis markers per
    segment, then the row values under each."""
    section: list[dict[str, object]] = [
        {
            "Segment Reporting - Schedule of Segment Reporting Information "
            "(Details) - USD ($) $ in Millions": [period_label]
        }
    ]
    for segment_name, rows in segment_rows.items():
        section.append({segment_name: ["\xa0"]})  # axis marker
        for label, value in rows.items():
            section.append({label: [value]})
    return section


# ---------------------------------------------------------------------------
# Row-metric classification
# ---------------------------------------------------------------------------


def test_row_metric_revenue() -> None:
    assert sq10q._row_metric("Total revenues") == "revenue"


def test_row_metric_cost() -> None:
    assert sq10q._row_metric("Total costs and expenses") == "cost_of_revenue"


def test_row_metric_operating_income() -> None:
    assert sq10q._row_metric("Income (loss) from operations") == "operating_income"


def test_row_metric_unrecognized() -> None:
    assert sq10q._row_metric("Depreciation and amortization") is None


# ---------------------------------------------------------------------------
# End-to-end: Q1 discrete extraction
# ---------------------------------------------------------------------------


def test_extract_q1_discrete(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")
    payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "3 Months Ended March 31, 2025",
            {
                "North America": {"Total revenues": 1000.0},
                "International": {"Total revenues": 500.0},
            },
        ),
    }
    file_path = _write_10q_json(tmp_path, ticker, 2025, "Q1", payload)
    rel = str(file_path.relative_to(tmp_path)).replace("\\", "/")
    _insert_document(conn, doc_id=1, ticker=ticker, file_path=rel)

    result = sq10q.extract_for_ticker(ticker, 2025, "Q1", tmp_path, conn)

    assert result.skipped_reason is None
    assert result.periods_inserted == 1
    assert result.dimensions_inserted == 2

    row = conn.execute(
        "SELECT sp.fiscal_period_type, sp.period_basis, sd.dim_name, sd.value, "
        "sd.disclosure_status, sd.confidence "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
        "WHERE sd.dim_name = 'North America'"
    ).fetchone()
    assert row["fiscal_period_type"] == "Q1"
    assert row["period_basis"] == "discrete"
    assert Decimal(str(row["value"])) == Decimal("1000000000")  # $ in Millions -> ACTUAL
    assert row["disclosure_status"] == "reported"
    assert row["confidence"] == 1.0


def test_extract_is_idempotent_on_cache(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")
    payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "3 Months Ended March 31, 2025", {"North America": {"Total revenues": 1000.0}}
        ),
    }
    file_path = _write_10q_json(tmp_path, ticker, 2025, "Q1", payload)
    rel = str(file_path.relative_to(tmp_path)).replace("\\", "/")
    _insert_document(conn, doc_id=1, ticker=ticker, file_path=rel)

    first = sq10q.extract_for_ticker(ticker, 2025, "Q1", tmp_path, conn)
    second = sq10q.extract_for_ticker(ticker, 2025, "Q1", tmp_path, conn)
    assert first.dimensions_inserted == 1
    assert second.dimensions_inserted == 1  # served from cache, not re-derived as 0
    assert second.source_sha256 == first.source_sha256


# ---------------------------------------------------------------------------
# Q2 one-hop discrete derivation (§2.6)
# ---------------------------------------------------------------------------


def test_q2_cumulative_derives_discrete_from_q1_anchor(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")

    q1_payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "3 Months Ended March 31, 2025", {"North America": {"Total revenues": 1000.0}}
        ),
    }
    q1_path = _write_10q_json(tmp_path, ticker, 2025, "Q1", q1_payload)
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        file_path=str(q1_path.relative_to(tmp_path)).replace("\\", "/"),
    )
    sq10q.extract_for_ticker(ticker, 2025, "Q1", tmp_path, conn)

    q2_payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "6 Months Ended June 30, 2025", {"North America": {"Total revenues": 2200.0}}
        ),
    }
    q2_path = _write_10q_json(tmp_path, ticker, 2025, "Q2", q2_payload)
    _insert_document(
        conn,
        doc_id=2,
        ticker=ticker,
        file_path=str(q2_path.relative_to(tmp_path)).replace("\\", "/"),
    )
    result = sq10q.extract_for_ticker(ticker, 2025, "Q2", tmp_path, conn)

    assert result.derived_inserted == 1
    derived = conn.execute(
        "SELECT sp.fiscal_period_type, sp.period_basis, sd.value, sd.disclosure_status, "
        "sd.method_version, sd.confidence "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
        "WHERE sd.disclosure_status = 'derived'"
    ).fetchone()
    assert derived["fiscal_period_type"] == "Q2"
    assert derived["period_basis"] == "derived"
    # H1 (2200M) - Q1 (1000M) = 1200M, scaled to ACTUAL.
    assert Decimal(str(derived["value"])) == Decimal("1200000000")
    assert derived["method_version"] == "segment_q2q3_derive_v1"
    assert derived["confidence"] < 1.0  # one-hop decay


def test_q2_cumulative_skips_derivation_without_prior_anchor(
    tmp_path: Path, conn: sqlite3.Connection
) -> None:
    """Missing Q1 anchor -> no fabricated value (§2.6 guard 1)."""
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")
    q2_payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "6 Months Ended June 30, 2025", {"North America": {"Total revenues": 2200.0}}
        ),
    }
    q2_path = _write_10q_json(tmp_path, ticker, 2025, "Q2", q2_payload)
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        file_path=str(q2_path.relative_to(tmp_path)).replace("\\", "/"),
    )
    result = sq10q.extract_for_ticker(ticker, 2025, "Q2", tmp_path, conn)
    assert result.derived_inserted == 0
    n_derived = conn.execute(
        "SELECT COUNT(*) FROM segment_dimensions WHERE disclosure_status = 'derived'"
    ).fetchone()[0]
    assert n_derived == 0


# ---------------------------------------------------------------------------
# LLM Stage-B fallback (mocked — degraded/ambiguous case)
# ---------------------------------------------------------------------------


def test_ambiguous_column_resolved_via_mocked_llm_fallback(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column whose header carries no 'N Months Ended' duration prefix
    (e.g. a filer that only spells the end-date) hits AMBIGUOUS_SAME_DATE
    deterministically; Stage B (mocked here — no real LLM call) resolves it
    and the value lands as a current_cumulative H1 cell."""
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")
    payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "June 30, 2025",  # no duration prefix -> deterministically ambiguous
            {"North America": {"Total revenues": 2200.0}},
        ),
    }
    file_path = _write_10q_json(tmp_path, ticker, 2025, "Q2", payload)
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        file_path=str(file_path.relative_to(tmp_path)).replace("\\", "/"),
    )

    def _fake_call_llm_structured(prompt: str, **kwargs: object) -> object:
        return {
            "columns": [
                {"index": 0, "duration_months": 6, "end_date": "2025-06-30", "is_cumulative": True}
            ]
        }

    monkeypatch.setattr(sq10q, "call_llm_structured", _fake_call_llm_structured)

    result = sq10q.extract_for_ticker(ticker, 2025, "Q2", tmp_path, conn)

    assert result.llm_fallback_calls == 1
    row = conn.execute(
        "SELECT sp.fiscal_period_type, sp.period_basis, sd.value "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sp.id = sd.period_id "
        "WHERE sd.dim_name = 'North America'"
    ).fetchone()
    assert row["fiscal_period_type"] == "H1"
    assert row["period_basis"] == "cumulative"
    assert Decimal(str(row["value"])) == Decimal("2200000000")


def test_ambiguous_column_llm_parse_failure_leaves_column_unresolved(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stage-B parse failure (StructuredParseError) must not fabricate a
    value — the column is skipped, tallied, never silently guessed."""
    ticker = "TESTCO"
    _insert_tracked_company(conn, ticker, "12-31")
    payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "June 30, 2025", {"North America": {"Total revenues": 2200.0}}
        ),
    }
    file_path = _write_10q_json(tmp_path, ticker, 2025, "Q2", payload)
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        file_path=str(file_path.relative_to(tmp_path)).replace("\\", "/"),
    )

    def _raise(prompt: str, **kwargs: object) -> object:
        from llm.structured import StructuredParseError

        raise StructuredParseError("mocked: both attempts unusable")

    monkeypatch.setattr(sq10q, "call_llm_structured", _raise)

    result = sq10q.extract_for_ticker(ticker, 2025, "Q2", tmp_path, conn)

    n_dims = conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0]
    assert n_dims == 0
    assert result.skip_counts.get("period_axis_ambiguous_no_duration_prefix", 0) >= 1


# ---------------------------------------------------------------------------
# Skip / degrade paths
# ---------------------------------------------------------------------------


def test_no_10q_file_skips_with_reason(tmp_path: Path, conn: sqlite3.Connection) -> None:
    result = sq10q.extract_for_ticker("NOPE", 2025, "Q1", tmp_path, conn)
    assert result.skipped_reason == "no_10q_json_fetched"


def test_fye_unresolvable_skips_with_reason(tmp_path: Path, conn: sqlite3.Connection) -> None:
    ticker = "TESTCO"
    payload = {
        "symbol": ticker,
        "Segment Note": _segment_section(
            "3 Months Ended March 31, 2025", {"North America": {"Total revenues": 1000.0}}
        ),
    }
    file_path = _write_10q_json(tmp_path, ticker, 2025, "Q1", payload)
    _insert_document(
        conn,
        doc_id=1,
        ticker=ticker,
        file_path=str(file_path.relative_to(tmp_path)).replace("\\", "/"),
    )
    # No tracked_companies row at all, and no 10-K on disk to fall back to.
    result = sq10q.extract_for_ticker(ticker, 2025, "Q1", tmp_path, conn)
    assert result.skipped_reason == "fiscal_year_end_unresolvable"
