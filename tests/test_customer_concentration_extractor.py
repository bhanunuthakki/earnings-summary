"""Tests for src/table_extractors/customer_concentration.py.

The LLM call is mocked via monkeypatch.setattr; the rest is real
SQLite + real entity-store API.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest

from table_extractors import customer_concentration as cc


@pytest.fixture()
def db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "test.db")


class _Validator(Protocol):
    def validate_python(self, value: object) -> object: ...


def _mock_llm(response_rows: list[dict[str, object]]):
    """Build a fake structured call that validates the given rows."""

    def _fake(prompt: str, **kwargs: object) -> object:
        del prompt
        schema = cast(_Validator, kwargs["schema"])
        return schema.validate_python(response_rows)

    return _fake


def _payload_with_relevant_sections() -> dict[str, object]:
    """A minimal FMP-shaped payload with the keyword-matched sections."""
    return {
        "symbol": "NVDA",
        "year": "2024",
        "Summary of Significant Accounting Policies": [
            {
                "Concentrations of Credit Risk": [
                    "One customer accounted for approximately 19% of total revenue in fiscal 2024."
                ]
            },
        ],
        "Revenues": [
            {"Revenue from Customers": ["Customer A accounted for approximately 13% of revenue."]},
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
        "table_extractors.customer_concentration.call_llm_structured",
        _mock_llm(
            [
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
            ]
        ),
    )
    outcome = cc.extract(
        ticker="NVDA",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=7,
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
            "SELECT kind, payload_json, ticker, status FROM mapping_proposals WHERE ticker = 'NVDA'"
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
        "table_extractors.customer_concentration.call_llm_structured",
        _mock_llm([]),
    )
    outcome = cc.extract(
        ticker="GOOG",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=1,
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_inserted == 0


def test_extract_normalizes_percentage_above_one(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Model sometimes emits 12 (meaning 12%) instead of 0.12; normalize.
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm_structured",
        _mock_llm(
            [
                {
                    "customer_label": "BigCo",
                    "pct_of_revenue": 18,  # int 18, should normalize to 0.18
                    "anonymized": False,
                    "source_excerpt": "BigCo accounted for ~18% of revenue.",
                }
            ]
        ),
    )
    outcome = cc.extract(
        ticker="AAPL",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=2,
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
        "table_extractors.customer_concentration.call_llm_structured",
        _mock_llm(
            [
                {
                    "customer_label": "Sane",
                    "pct_of_revenue": 0.15,
                    "anonymized": False,
                    "source_excerpt": "x",
                },
                {
                    "customer_label": "Bogus",
                    "pct_of_revenue": 500,
                    "anonymized": False,
                    "source_excerpt": "x",
                },
            ]
        ),
    )
    outcome = cc.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=3,
    )
    assert outcome.n_rows_inserted == 1


def test_extract_is_idempotent(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "table_extractors.customer_concentration.call_llm_structured",
        _mock_llm(
            [
                {
                    "customer_label": "Customer A",
                    "pct_of_revenue": 0.12,
                    "anonymized": True,
                    "source_excerpt": "Customer A: 12% of revenue.",
                }
            ]
        ),
    )
    out1 = cc.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=1,
    )
    out2 = cc.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=1,
    )
    assert out1.n_rows_inserted == 1
    assert out2.n_rows_inserted == 0  # unique violation, silently skipped


def test_extract_returns_llm_failed_when_llm_raises(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        raise RuntimeError("simulated upstream LLM failure")

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm_structured", _boom)
    outcome = cc.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
    )
    assert outcome.status == "llm_failed"
    assert "simulated upstream" in outcome.notes


def test_extract_returns_parse_failed_on_non_json_response(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _bad(prompt: str, **kwargs: object) -> object:
        del prompt, kwargs
        from llm.structured import StructuredParseError

        raise StructuredParseError("invalid structured response")

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm_structured", _bad)
    outcome = cc.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
    )
    assert outcome.status == "structured_failed"


def test_extract_strips_json_code_fence(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fenced(prompt: str, **kwargs: object) -> object:
        del prompt
        schema = cast(_Validator, kwargs["schema"])
        return schema.validate_python(
            [
                {
                    "customer_label": "Y",
                    "pct_of_revenue": 0.11,
                    "anonymized": False,
                    "source_excerpt": "y",
                }
            ]
        )

    monkeypatch.setattr("table_extractors.customer_concentration.call_llm_structured", _fenced)
    outcome = cc.extract(
        ticker="Z",
        fiscal_year=2024,
        fmp_payload=_payload_with_relevant_sections(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=8,
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_inserted == 1
