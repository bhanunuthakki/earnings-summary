"""Tests for the LLM-driven 2-axis cross-tab extractor.

Covers:
  - End-to-end: mock LLM returns the cross-tab contract, extractor writes
    one period row + one dim row per cell, and the cache file is produced
    with timing + sha256.
  - Idempotence on sha256: re-running on the same filing skips the LLM call
    and returns the cached result verbatim.
  - Renderer round-trip: when the dim rows carry ``metric='AWS_revenue'`` on
    the geography axis, ``_build_secondary_expansions`` surfaces an expansion
    with ``parent_label='AWS'`` (the user-facing "By Geography — under AWS"
    caption hangs off that field).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from compute import segment_crosstabs_llm
from compute.segment_crosstabs_llm import extract_for_ticker
from report.sections._common import calendar_quarter_key
from report.sections.segments import _build_secondary_expansions

# ---------------------------------------------------------------------------
# Schema + fixtures
# ---------------------------------------------------------------------------


def _create_schema(conn: sqlite3.Connection) -> None:
    """Minimal slice of the live schema — documents + junction tables.

    Mirrors what migrations 0001 (documents) and 0055 (segment junction)
    produce; kept inline so the tests don't pay the cost of running alembic.
    """
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL,
            period_end TIMESTAMP,
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
            CONSTRAINT uq_segment_periods_provenance UNIQUE
              (ticker, period_end, fiscal_period_type, source_doc_id)
        );
        CREATE INDEX ix_segment_periods_ticker_period
        ON segment_periods (ticker, period_end);

        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL REFERENCES segment_periods(id),
            dim_type VARCHAR(16) NOT NULL,
            dim_name VARCHAR(128) NOT NULL,
            value NUMERIC(20, 4) NOT NULL,
            metric VARCHAR(32) NOT NULL
        );
        CREATE INDEX ix_segment_dimensions_period_type_metric
        ON segment_dimensions (period_id, dim_type, metric);
        CREATE INDEX ix_segment_dimensions_dim
        ON segment_dimensions (dim_type, dim_name);
        """
    )
    conn.commit()


def _seed_amzn_10k_doc(conn: sqlite3.Connection, *, fiscal_year: int) -> int:
    conn.execute(
        """
        INSERT INTO documents (
            ticker, source_type, doc_type, file_path, sha256,
            fetched_at, fetch_status, raw_bytes_size, period_end
        ) VALUES (?, 'fmp', 'fmp_10k_json', ?, ?, ?, 'ok', 1, ?)
        """,
        (
            "AMZN",
            f"data/historical/fmp/AMZN_form_10k_{fiscal_year}.json",
            "0" * 64,
            datetime.now(),
            f"{fiscal_year}-12-31",
        ),
    )
    conn.commit()
    return int(
        conn.execute(
            "SELECT id FROM documents WHERE ticker = 'AMZN' AND doc_type = 'fmp_10k_json'"
        ).fetchone()[0]
    )


def _write_10k_payload(repo_root: Path, ticker: str, year: int) -> Path:
    """Write a minimal 10-K JSON that ``_extract_relevant_text`` will pick up.

    Section keys must match one of the keyword fragments
    (segment / geographic / disaggregation / revenue / sales) and the leaf
    text needs to be >40 chars to clear the flatten threshold.
    """
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    path = fmp_dir / f"{ticker}_form_10k_{year}.json"
    payload = {
        "segment_information": [
            "Amazon Web Services (AWS) revenue is disaggregated by geography below — the table "
            "splits AWS by United States, EMEA, and APAC for each fiscal quarter and the full year."
        ],
        "disaggregation_of_revenue": [
            "The following table presents AWS revenue by geographic region (in millions): United "
            "States contributes the majority, with EMEA and APAC making up the international share."
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


_VALID_LLM_RESPONSE = json.dumps(
    {
        "cross_tabs": [
            {
                "subject_axis": "product",
                "subject_name": "AWS",
                "secondary_axis": "geography",
                "cells": [
                    {
                        "period_end": "2024-12-31",
                        "fiscal_period_type": "FY",
                        "secondary_name": "United States",
                        "value": 80000000000.0,
                        "currency": "USD",
                    },
                    {
                        "period_end": "2024-12-31",
                        "fiscal_period_type": "FY",
                        "secondary_name": "EMEA",
                        "value": 14000000000.0,
                        "currency": "USD",
                    },
                    {
                        "period_end": "2024-12-31",
                        "fiscal_period_type": "FY",
                        "secondary_name": "APAC",
                        "value": 8000000000.0,
                        "currency": "USD",
                    },
                ],
            }
        ]
    }
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


# ---------------------------------------------------------------------------
# End-to-end with mocked LLM
# ---------------------------------------------------------------------------


def test_extract_writes_one_period_and_three_dim_rows(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    call_count = {"n": 0}

    def fake_call_claude(prompt: str, **_: object) -> str:
        call_count["n"] += 1
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(segment_crosstabs_llm, "_call_claude", fake_call_claude)

    result = extract_for_ticker("AMZN", tmp_path, conn)

    assert result.skipped_reason is None
    assert call_count["n"] == 1
    assert result.fiscal_year == 2024
    assert result.source_sha256 is not None
    assert len(result.source_sha256) == 64
    assert result.periods_inserted == 1
    assert result.dimensions_inserted == 3
    assert result.cells_skipped == 0
    assert len(result.cross_tabs) == 1
    assert result.cross_tabs[0]["subject_name"] == "AWS"

    period_rows = conn.execute(
        "SELECT ticker, fiscal_period_type, currency, unit FROM segment_periods"
    ).fetchall()
    assert len(period_rows) == 1
    assert period_rows[0]["ticker"] == "AMZN"
    assert period_rows[0]["fiscal_period_type"] == "FY"
    assert period_rows[0]["currency"] == "USD"

    dim_rows = conn.execute(
        "SELECT dim_type, dim_name, value, metric FROM segment_dimensions ORDER BY id"
    ).fetchall()
    # All three cells share metric='AWS_revenue' so the renderer can map them
    # under parent_label='AWS' (see test_build_secondary_expansions_*).
    assert {(r["dim_type"], r["dim_name"], r["metric"]) for r in dim_rows} == {
        ("geography", "United States", "AWS_revenue"),
        ("geography", "EMEA", "AWS_revenue"),
        ("geography", "APAC", "AWS_revenue"),
    }
    assert {str(r["value"]) for r in dim_rows} == {
        "80000000000",
        "14000000000",
        "8000000000",
    }

    cache_path = tmp_path / "data" / "segment_crosstabs" / "AMZN.json"
    assert cache_path.exists()
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["source_sha256"] == result.source_sha256
    assert cached["extracted_at_start"]
    assert cached["extracted_at_end"]


def test_extract_idempotent_on_sha256(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second invocation with unchanged source hits the cache and skips the LLM."""
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    call_count = {"n": 0}

    def fake_call_claude(prompt: str, **_: object) -> str:
        call_count["n"] += 1
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(segment_crosstabs_llm, "_call_claude", fake_call_claude)

    first = extract_for_ticker("AMZN", tmp_path, conn)
    assert call_count["n"] == 1
    assert first.dimensions_inserted == 3

    second = extract_for_ticker("AMZN", tmp_path, conn)
    # Cache hit: LLM not re-called.
    assert call_count["n"] == 1
    # Cached payload preserved verbatim.
    assert second.source_sha256 == first.source_sha256
    assert second.cross_tabs == first.cross_tabs
    assert second.extracted_at_start == first.extracted_at_start

    # DB rows didn't double; writer's natural-key dedup holds even if a
    # caller wipes the cache by hand and forces a re-extract.
    assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 3


def test_extract_refresh_bypasses_cache(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--refresh forces a fresh LLM call and re-writes the cache."""
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    call_count = {"n": 0}

    def fake_call_claude(prompt: str, **_: object) -> str:
        call_count["n"] += 1
        return _VALID_LLM_RESPONSE

    monkeypatch.setattr(segment_crosstabs_llm, "_call_claude", fake_call_claude)

    extract_for_ticker("AMZN", tmp_path, conn)
    extract_for_ticker("AMZN", tmp_path, conn, refresh=True)
    assert call_count["n"] == 2


def test_extract_skips_when_no_doc_row(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the FMP doc index hasn't run, we skip with an actionable reason
    rather than crash. The 10-K JSON existing on disk is not enough — the
    junction writer requires a real documents.id FK."""
    _write_10k_payload(tmp_path, "AMZN", 2024)

    monkeypatch.setattr(
        segment_crosstabs_llm, "_call_claude", lambda *a, **k: _VALID_LLM_RESPONSE
    )

    result = extract_for_ticker("AMZN", tmp_path, conn)
    assert result.skipped_reason is not None
    assert "documents" in result.skipped_reason.lower() or "doc" in result.skipped_reason.lower()
    assert result.dimensions_inserted == 0
    # No DB writes attempted.
    assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Renderer round-trip
# ---------------------------------------------------------------------------


def test_build_secondary_expansions_surfaces_parent_label_aws(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After running the extractor end-to-end (mocked LLM), the renderer's
    secondary-expansion builder finds an expansion captioned 'under AWS'.

    The renderer derives ``parent_label`` from the dimension row's ``metric``
    via ``metric.rsplit('_revenue', 1)[0].replace('_', ' ')`` — so writing
    ``metric='AWS_revenue'`` is the contract that makes this round-trip work.
    """
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    aws_quarterly = json.dumps(
        {
            "cross_tabs": [
                {
                    "subject_axis": "product",
                    "subject_name": "AWS",
                    "secondary_axis": "geography",
                    "cells": [
                        {
                            "period_end": "2025-09-30",
                            "fiscal_period_type": "Q3",
                            "secondary_name": "United States",
                            "value": 20000000000.0,
                            "currency": "USD",
                        },
                        {
                            "period_end": "2025-09-30",
                            "fiscal_period_type": "Q3",
                            "secondary_name": "EMEA",
                            "value": 5000000000.0,
                            "currency": "USD",
                        },
                        {
                            "period_end": "2025-09-30",
                            "fiscal_period_type": "Q3",
                            "secondary_name": "APAC",
                            "value": 3000000000.0,
                            "currency": "USD",
                        },
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(
        segment_crosstabs_llm, "_call_claude", lambda *a, **k: aws_quarterly
    )

    extract_for_ticker("AMZN", tmp_path, conn)

    # The renderer expects the calendar key of each period to fall inside
    # ``quarters_full`` — pass a window that includes 2025 Q3.
    q3_2025 = calendar_quarter_key("2025-09-30")
    quarters_full = [(2025, 1), (2025, 2), q3_2025, (2025, 4)]
    display_labels = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4"]

    expansions = _build_secondary_expansions(
        conn,
        "AMZN",
        quarters_full,
        display_labels,
        primary_segment_names=set(),
    )

    aws_geo = [e for e in expansions if e.parent_label == "AWS"]
    assert len(aws_geo) == 1, (
        f"expected exactly one AWS expansion, got: "
        f"{[(e.dim_type, e.parent_label) for e in expansions]}"
    )
    exp = aws_geo[0]
    assert exp.dim_type == "geography"
    assert {r.segment_name for r in exp.rows} == {"United States", "EMEA", "APAC"}
    # Rows sort by most-recent value descending — US ($20B) leads, then EMEA, then APAC.
    assert [r.segment_name for r in exp.rows] == ["United States", "EMEA", "APAC"]


def test_extract_one_axis_fallback_writes_revenue_metric(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the LLM emits a 1-axis breakdown (subject_axis/name = null), dim
    rows land with ``metric='revenue'`` so the renderer surfaces them as a
    flat 'By <axis>' expansion with no ``parent_label``. The path matters for
    tickers whose 10-K has revenue-by-country but no explicit product × region
    cross-tab (AMZN, GOOG)."""
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    one_axis_response = json.dumps(
        {
            "cross_tabs": [
                {
                    "subject_axis": None,
                    "subject_name": None,
                    "secondary_axis": "geography",
                    "cells": [
                        {
                            "period_end": "2024-12-31",
                            "fiscal_period_type": "FY",
                            "secondary_name": "United States",
                            "value": 380000000000.0,
                            "currency": "USD",
                        },
                        {
                            "period_end": "2024-12-31",
                            "fiscal_period_type": "FY",
                            "secondary_name": "Germany",
                            "value": 38000000000.0,
                            "currency": "USD",
                        },
                        {
                            "period_end": "2024-12-31",
                            "fiscal_period_type": "FY",
                            "secondary_name": "Rest of world",
                            "value": 219000000000.0,
                            "currency": "USD",
                        },
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(
        segment_crosstabs_llm, "_call_claude", lambda *a, **k: one_axis_response
    )

    result = extract_for_ticker("AMZN", tmp_path, conn)
    assert result.skipped_reason is None
    assert result.dimensions_inserted == 3

    metrics = {
        r["metric"] for r in conn.execute("SELECT DISTINCT metric FROM segment_dimensions").fetchall()
    }
    assert metrics == {"revenue"}, (
        f"1-axis breakdowns must land with metric='revenue' (not subject-prefixed); "
        f"got {metrics}"
    )


def test_build_secondary_expansions_multi_word_subject(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-word subject names round-trip: 'Google Cloud' →
    metric='Google_Cloud_revenue' → parent_label='Google Cloud'."""
    _seed_amzn_10k_doc(conn, fiscal_year=2024)
    _write_10k_payload(tmp_path, "AMZN", 2024)

    payload = json.dumps(
        {
            "cross_tabs": [
                {
                    "subject_axis": "product",
                    "subject_name": "Google Cloud",
                    "secondary_axis": "geography",
                    "cells": [
                        {
                            "period_end": "2025-09-30",
                            "fiscal_period_type": "Q3",
                            "secondary_name": "United States",
                            "value": 7000000000.0,
                            "currency": "USD",
                        }
                    ],
                }
            ]
        }
    )
    monkeypatch.setattr(segment_crosstabs_llm, "_call_claude", lambda *a, **k: payload)

    extract_for_ticker("AMZN", tmp_path, conn)

    expansions = _build_secondary_expansions(
        conn,
        "AMZN",
        [(2025, 3)],
        ["2025 Q3"],
        primary_segment_names=set(),
    )
    labels = {e.parent_label for e in expansions}
    assert "Google Cloud" in labels
