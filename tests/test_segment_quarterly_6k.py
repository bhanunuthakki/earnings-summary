"""Tests for compute.segment_quarterly_6k -- the Phase-3 FPI 6-K extractor
(docs/design/segment_quarterly_framework.md §1.1, spike-gated ``fpi_6k`` route).

Covers the three outcomes the Phase-3 spike verdict requires be distinguishable:
  1. A ticker the spike validated as extractable (NU/NVO/WIX-shaped): end-to-
     end mocked fetch + LLM writes segment_periods/segment_dimensions rows,
     with an explicit non-"revenue" metric (non_current_assets) round-tripping.
  2. A ticker the spike confirmed has NO quarterly disclosure (ASML): no
     network call attempted, a ``not_disclosed``/``fpi_annual_only``
     coverage row is written instead.
  3. A ticker the spike never tested (e.g. BHP): no network call, no
     coverage row written by this module (left to
     audit_segment_quarterly_coverage.py's existing fpi_route_unproven
     default) -- this module must not claim evidence it doesn't have.

Also covers the image-only-exhibit and no-exhibit-located paths degrading to
an honest coverage row rather than silently doing nothing.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute import segment_quarterly_6k  # noqa: E402
from compute.segment_quarterly_6k import extract_for_ticker  # noqa: E402
from pipeline.sec_6k_fetch import FetchedExhibit, LocatedExhibit  # noqa: E402

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    period_start DATETIME,
    period_end DATETIME,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    http_code INTEGER,
    raw_bytes_size INTEGER NOT NULL,
    source_url TEXT,
    parent_document_id INTEGER,
    accession_number TEXT,
    filing_date TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
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
CREATE TABLE segment_quarterly_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    dim_type VARCHAR(16),
    dim_name VARCHAR(128),
    status VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    source_doc_id INTEGER,
    method_version VARCHAR(32),
    checked_at DATETIME NOT NULL
);
"""


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


_NU_LOCATED = LocatedExhibit(
    ticker="NU",
    cik="0001691493",
    accession="0001292814-26-003053",
    filing_date="2026-05-14",
    exhibit_filename="nufs1q26_6k.htm",
    exhibit_url="https://example.invalid/nufs1q26_6k.htm",
)

_NU_LLM_RESPONSE = json.dumps(
    {
        "breakdowns": [
            {
                "axis": "geography",
                "metric": "revenue",
                "cells": [
                    {
                        "name": "Brazil",
                        "value": 3586566000,
                        "period_end": "2026-03-31",
                        "fiscal_period_type": "Q1",
                        "currency": "BRL",
                    },
                    {
                        "name": "Mexico",
                        "value": 289026000,
                        "period_end": "2026-03-31",
                        "fiscal_period_type": "Q1",
                        "currency": "BRL",
                    },
                ],
            },
            {
                "axis": "geography",
                "metric": "non_current_assets",
                "cells": [
                    {
                        "name": "Brazil",
                        "value": 943015000,
                        "period_end": "2026-03-31",
                        "fiscal_period_type": "Q1",
                        "currency": "BRL",
                    }
                ],
            },
        ]
    }
)


def test_supported_ticker_end_to_end_writes_both_metrics(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NU: mocked locate + fetch + LLM call writes segment_periods/dimensions
    rows, with the explicit non_current_assets metric surviving (the shape
    gap segment_crosstabs_llm.py's own contract doesn't cover)."""
    fetched = FetchedExhibit(
        located=_NU_LOCATED,
        raw_html="<html>34. SEGMENT INFORMATION geographical area Brazil Mexico</html>",
        plain_text=(
            "34. SEGMENT INFORMATION Information about geographical area. "
            "The table below shows the revenue and non-current assets per "
            "geographical area: Brazil 3,586,566 Mexico 289,026"
        ),
        is_image_only=False,
    )

    def fake_locate(*_a: object, **_k: object) -> LocatedExhibit:
        return _NU_LOCATED

    def fake_fetch(*_a: object, **_k: object) -> FetchedExhibit:
        return fetched

    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", fake_locate)
    monkeypatch.setattr(segment_quarterly_6k, "fetch_6k_exhibit_text", fake_fetch)
    call_count = {"n": 0}

    def fake_call_claude(prompt: str, **_: object) -> object:
        call_count["n"] += 1
        return json.loads(_NU_LLM_RESPONSE)

    monkeypatch.setattr(segment_quarterly_6k, "call_llm_structured", fake_call_claude)

    result = extract_for_ticker("NU", 2026, "Q1", tmp_path, conn)

    assert result.skipped_reason is None
    assert call_count["n"] == 1
    assert result.periods_inserted == 1
    assert result.dimensions_inserted == 3
    assert result.cells_skipped == 0

    dims = conn.execute(
        "SELECT sd.dim_type, sd.dim_name, sd.metric, sd.value, sp.currency FROM segment_dimensions sd "
        "JOIN segment_periods sp ON sd.period_id = sp.id"
    ).fetchall()
    metrics = {(r["dim_name"], r["metric"]) for r in dims}
    assert ("Brazil", "revenue") in metrics
    assert ("Mexico", "revenue") in metrics
    assert ("Brazil", "non_current_assets") in metrics

    # Provenance columns land per the design doc §4.1 contract.
    row = conn.execute(
        "SELECT extracted_by, method_version, confidence, disclosure_status "
        "FROM segment_dimensions LIMIT 1"
    ).fetchone()
    assert row["extracted_by"].startswith("llm:")
    assert row["method_version"] == "segment_quarterly_6k_v1"
    assert row["disclosure_status"] == "reported"
    assert row["confidence"] == 1.0


def test_confirmed_annual_only_ticker_skips_network_and_records_coverage(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASML: the spike's confirmed-negative finding -- never fetches, writes
    an honest not_disclosed/fpi_annual_only coverage row."""
    located_mock = MagicMock(side_effect=AssertionError("must not attempt to locate a 6-K"))
    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", located_mock)

    result = extract_for_ticker("ASML", 2026, "Q1", tmp_path, conn)

    assert result.skipped_reason is not None
    assert "fpi_annual_only" in result.skipped_reason
    located_mock.assert_not_called()

    row = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage WHERE ticker = 'ASML'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "not_disclosed"
    assert row["reason_code"] == "fpi_annual_only"


def test_untested_ticker_skips_network_and_writes_no_coverage_row(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BHP (a 20-F name on the roster the spike never sampled): no network
    call, and NO coverage row from this module -- audit_segment_quarterly_
    coverage.py's existing fpi_route_unproven default is the honest answer
    here, not a claim this module has evidence for."""
    located_mock = MagicMock(side_effect=AssertionError("must not attempt to locate a 6-K"))
    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", located_mock)

    result = extract_for_ticker("BHP", 2026, "Q1", tmp_path, conn)

    assert result.skipped_reason is not None
    assert "not in the Phase-3 spike-validated ticker set" in result.skipped_reason
    located_mock.assert_not_called()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM segment_quarterly_coverage WHERE ticker = 'BHP'"
        ).fetchone()[0]
        == 0
    )


def test_wix_is_supported_and_attempts_a_locate_call() -> None:
    """WIX moved from untested to spike-validated 2026-07-25 (D1.2): it must
    take the "supported" branch, distinct from both ASML's confirmed-negative
    short-circuit and an untested ticker's silent skip -- proven by checking
    the classification table directly, the single source of truth every other
    code path in this module reads from."""
    assert segment_quarterly_6k._TICKER_6K_STATUS["WIX"] == "supported"


def test_wix_exhibit_not_located_records_not_computable_coverage(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WIX must actually reach the network-locate step (unlike ASML/untested
    tickers, which short-circuit before it) -- proven by mocking
    ``locate_6k_exhibit`` to return None and checking it was CALLED, then that
    the honest not_computable coverage row is written."""
    locate_mock = MagicMock(return_value=None)
    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", locate_mock)

    result = extract_for_ticker("WIX", 2026, "Q1", tmp_path, conn)

    locate_mock.assert_called_once()
    assert result.skipped_reason is not None
    row = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage WHERE ticker = 'WIX'"
    ).fetchone()
    assert row["status"] == "not_computable"
    assert row["reason_code"] == "fpi_6k_exhibit_not_located"


def test_exhibit_not_located_records_not_computable_coverage(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_locate_none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", fake_locate_none)

    result = extract_for_ticker("NU", 2026, "Q1", tmp_path, conn)

    assert result.skipped_reason is not None
    row = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage WHERE ticker = 'NU'"
    ).fetchone()
    assert row["status"] == "not_computable"
    assert row["reason_code"] == "fpi_6k_exhibit_not_located"


def test_image_only_exhibit_records_not_computable_coverage(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive path: even a "supported" ticker degrades honestly if a
    future quarter's exhibit turns out image-scanned (the ASML shape) rather
    than getting silently fed to the LLM as if it were narrative text."""
    fetched = FetchedExhibit(
        located=_NU_LOCATED, raw_html="<img>", plain_text="", is_image_only=True
    )

    def fake_locate(*_a: object, **_k: object) -> LocatedExhibit:
        return _NU_LOCATED

    def fake_fetch(*_a: object, **_k: object) -> FetchedExhibit:
        return fetched

    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", fake_locate)
    monkeypatch.setattr(segment_quarterly_6k, "fetch_6k_exhibit_text", fake_fetch)

    result = extract_for_ticker("NU", 2026, "Q1", tmp_path, conn)

    assert result.skipped_reason is not None
    row = conn.execute(
        "SELECT status, reason_code FROM segment_quarterly_coverage WHERE ticker = 'NU'"
    ).fetchone()
    assert row["status"] == "not_computable"
    assert row["reason_code"] == "fpi_6k_image_only_exhibit"


def test_subtotal_rows_are_skipped_deterministically(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "Total" cell emitted despite the prompt's no-subtotal instruction is
    dropped by the persist guard, never written -- a subtotal alongside its
    components double-counts every consumer that sums a dimension (the prod
    NU 3Q24 shape that motivated the guard)."""
    fetched = FetchedExhibit(
        located=_NU_LOCATED,
        raw_html="<html>34. SEGMENT INFORMATION geographical area</html>",
        plain_text=(
            "34. SEGMENT INFORMATION Information about geographical area. "
            "Brazil 2,124,441 Mexico 141,540 Total 2,296,212"
        ),
        is_image_only=False,
    )
    response_with_total = json.dumps(
        {
            "breakdowns": [
                {
                    "axis": "geography",
                    "metric": "revenue",
                    "cells": [
                        {
                            "name": "Brazil",
                            "value": 2124441000,
                            "period_end": "2024-09-30",
                            "fiscal_period_type": "Q3",
                            "currency": "USD",
                        },
                        {
                            "name": "Total",
                            "value": 2296212000,
                            "period_end": "2024-09-30",
                            "fiscal_period_type": "Q3",
                            "currency": "USD",
                        },
                        {
                            "name": "Total fee and commission income",
                            "value": 469381000,
                            "period_end": "2024-09-30",
                            "fiscal_period_type": "Q3",
                            "currency": "USD",
                        },
                    ],
                }
            ]
        }
    )

    monkeypatch.setattr(segment_quarterly_6k, "locate_6k_exhibit", lambda *a, **k: _NU_LOCATED)
    monkeypatch.setattr(segment_quarterly_6k, "fetch_6k_exhibit_text", lambda *a, **k: fetched)
    monkeypatch.setattr(
        segment_quarterly_6k,
        "call_llm_structured",
        lambda *a, **k: json.loads(response_with_total),
    )

    result = extract_for_ticker("NU", 2024, "Q3", tmp_path, conn)

    assert result.skipped_reason is None
    assert result.dimensions_inserted == 1
    assert result.cells_skipped == 2
    names = {
        r["dim_name"] for r in conn.execute("SELECT dim_name FROM segment_dimensions").fetchall()
    }
    assert names == {"Brazil"}
