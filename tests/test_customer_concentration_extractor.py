"""Tests for src/table_extractors/customer_concentration.py.

The LLM call is mocked via monkeypatch.setattr; the rest is real
SQLite + real entity-store API.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from table_extractors import customer_concentration as cc


def _schema(conn: sqlite3.Connection) -> None:
    """Inline customer_concentrations + entity spine + extractions schema."""
    conn.executescript(
        """
        CREATE TABLE customer_concentrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            fiscal_period VARCHAR(10) NOT NULL,
            fiscal_period_type VARCHAR(4) NOT NULL,
            customer_label VARCHAR(255) NOT NULL,
            customer_entity_id INTEGER,
            pct_of_revenue FLOAT NOT NULL,
            revenue_amount NUMERIC(24, 6),
            revenue_currency VARCHAR(3),
            source_doc_id INTEGER,
            source_excerpt TEXT,
            extracted_at DATETIME NOT NULL,
            UNIQUE(ticker, fiscal_period, customer_label)
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            external_ids TEXT,
            parent_entity_id INTEGER REFERENCES entities(id),
            meta_json TEXT,
            effective_from DATETIME, effective_to DATETIME,
            created_at DATETIME NOT NULL,
            last_observed_at DATETIME,
            UNIQUE(kind, canonical_name)
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            alias_text VARCHAR(255) NOT NULL,
            alias_kind VARCHAR(32) NOT NULL,
            first_observed_at DATETIME, last_observed_at DATETIME,
            observation_count INTEGER NOT NULL DEFAULT 1,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            exemplar_source_doc_id INTEGER, exemplar_excerpt TEXT,
            UNIQUE(entity_id, alias_text)
        );
        CREATE TABLE concepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(128) NOT NULL,
            unit_kind VARCHAR(32),
            taxonomy_xbrl_tag VARCHAR(128),
            generic_definition_md TEXT,
            computation_kind VARCHAR(32),
            computation_formula_md TEXT,
            created_at DATETIME NOT NULL,
            UNIQUE(canonical_name)
        );
        CREATE TABLE mapping_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(48) NOT NULL,
            ticker VARCHAR(16),
            proposed_by VARCHAR(64) NOT NULL,
            payload_json TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            source_doc_id INTEGER, source_excerpt TEXT,
            status VARCHAR(24) NOT NULL DEFAULT 'pending_review',
            applied_at DATETIME, applied_to_entity_id INTEGER, applied_to_concept_id INTEGER,
            decided_at DATETIME, decided_by VARCHAR(64), decision_notes TEXT,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_doc_id INTEGER NOT NULL,
            char_offset_start INTEGER, char_offset_end INTEGER,
            extraction_kind VARCHAR(48) NOT NULL,
            extractor_id VARCHAR(64) NOT NULL,
            extractor_version VARCHAR(16) NOT NULL,
            payload_json TEXT NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_at DATETIME NOT NULL,
            superseded_by_extraction_id INTEGER REFERENCES extractions(id),
            links_to_json TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    conn = sqlite3.connect(str(p))
    try:
        _schema(conn)
    finally:
        conn.close()
    return p


def _mock_llm(response_rows: list[dict[str, object]]):
    """Build a fake call_llm that returns the given rows as JSON."""

    def _fake(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        return json.dumps(response_rows)

    return _fake


def _payload_with_relevant_sections() -> dict[str, object]:
    """A minimal FMP-shaped payload with the keyword-matched sections."""
    return {
        "symbol": "NVDA",
        "year": "2024",
        "Summary of Significant Accounting Policies": [
            {"Concentrations of Credit Risk":
                ["One customer accounted for approximately 19% of total revenue in fiscal 2024."]},
        ],
        "Revenues": [
            {"Revenue from Customers":
                ["Customer A accounted for approximately 13% of revenue."]},
        ],
        "Unrelated Section": [{"noise": ["should not be included"]}],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_extract_persists_named_and_anonymized(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        _mock_llm([
            {
                "customer_label": "Microsoft Corporation",
                "pct_of_revenue": 0.19,
                "revenue_amount": 11500,
                "anonymized": False,
                "source_excerpt": "Microsoft Corporation accounted for approximately 19% of revenue",
            },
            {
                "customer_label": "Customer A",
                "pct_of_revenue": 0.13,
                "revenue_amount": None,
                "anonymized": True,
                "source_excerpt": "Customer A accounted for approximately 13% of revenue.",
            },
        ]),
    )
    outcome = cc.extract(
        ticker="NVDA", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=7,
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_proposed == 2
    assert outcome.n_rows_inserted == 2

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT customer_label, pct_of_revenue, customer_entity_id FROM customer_concentrations "
            "WHERE ticker = 'NVDA' ORDER BY pct_of_revenue DESC"
        ).fetchall()
        assert len(rows) == 2
        msft_row = rows[0]
        anon_row = rows[1]
        assert msft_row[0] == "Microsoft Corporation"
        assert pytest.approx(float(msft_row[1])) == 0.19
        # Anonymized customer was upserted into entities — has an FK
        assert anon_row[2] is not None
        # The named customer has no FK (no global resolve, proposal queued)
        assert msft_row[2] is None

        # Anonymized entity was created with ticker-scoped canonical name
        anon_entity = conn.execute(
            "SELECT canonical_name, meta_json FROM entities WHERE id = ?", (anon_row[2],)
        ).fetchone()
        assert anon_entity[0] == "NVDA Customer A"
        assert "anonymized" in (anon_entity[1] or "")

        # Mapping proposal was emitted for the named customer
        proposal = conn.execute(
            "SELECT kind, payload_json, ticker, status FROM mapping_proposals "
            "WHERE ticker = 'NVDA'"
        ).fetchone()
        assert proposal is not None
        assert proposal[0] == "new_entity"
        payload = json.loads(proposal[1])
        assert payload["canonical_name"] == "Microsoft Corporation"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extract_handles_empty_disclosure(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        _mock_llm([]),
    )
    outcome = cc.extract(
        ticker="GOOG", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=1,
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_inserted == 0


def test_extract_normalizes_percentage_above_one(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model sometimes emits 12 (meaning 12%) instead of 0.12; normalize.
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        _mock_llm([{
            "customer_label": "BigCo",
            "pct_of_revenue": 18,  # int 18, should normalize to 0.18
            "anonymized": False,
            "source_excerpt": "BigCo accounted for ~18% of revenue.",
        }]),
    )
    outcome = cc.extract(
        ticker="AAPL", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=2,
    )
    assert outcome.n_rows_inserted == 1
    conn = sqlite3.connect(str(db))
    try:
        pct = conn.execute(
            "SELECT pct_of_revenue FROM customer_concentrations WHERE ticker='AAPL'"
        ).fetchone()[0]
        assert pytest.approx(float(pct)) == 0.18
    finally:
        conn.close()


def test_extract_skips_out_of_range_percentage(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 500 is clearly malformed — should be dropped, not stored.
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        _mock_llm([
            {"customer_label": "Sane", "pct_of_revenue": 0.15, "anonymized": False, "source_excerpt": "x"},
            {"customer_label": "Bogus", "pct_of_revenue": 500, "anonymized": False, "source_excerpt": "x"},
        ]),
    )
    outcome = cc.extract(
        ticker="X", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=3,
    )
    assert outcome.n_rows_inserted == 1


def test_extract_is_idempotent(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm",
        _mock_llm([{
            "customer_label": "Customer A",
            "pct_of_revenue": 0.12,
            "anonymized": True,
            "source_excerpt": "Customer A: 12% of revenue.",
        }]),
    )
    out1 = cc.extract(
        ticker="X", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=1,
    )
    out2 = cc.extract(
        ticker="X", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=1,
    )
    assert out1.n_rows_inserted == 1
    assert out2.n_rows_inserted == 0  # unique violation, silently skipped


def test_extract_returns_llm_failed_when_llm_raises(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        raise RuntimeError("simulated upstream LLM failure")

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm", _boom)
    outcome = cc.extract(
        ticker="X", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "llm_failed"
    assert "simulated upstream" in outcome.notes


def test_extract_returns_parse_failed_on_non_json_response(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _bad(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        return "Here is the answer: not really json"

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm", _bad)
    outcome = cc.extract(
        ticker="X", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path,
    )
    assert outcome.status == "parse_failed"


def test_extract_strips_json_code_fence(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fenced(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        return '```json\n[{"customer_label":"Y","pct_of_revenue":0.11,"anonymized":false,"source_excerpt":"y"}]\n```'

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm", _fenced)
    outcome = cc.extract(
        ticker="Z", fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None, db_path=db, repo_root=tmp_path, filing_doc_id=8,
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_inserted == 1
