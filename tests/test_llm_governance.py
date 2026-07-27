"""Regression guard for the governed LLM entrypoint and ledger contract."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from llm.structured import call_llm_structured
from llm_call_ledger import LlmCallRecord, record_call

_SCHEMA_ONLY_EXTRACTORS = frozenset(
    {
        "kpi_summary_extract",
        "kpi_summary_enumerate",
        "segment_6k_breakdown_extract",
        "segment_definition_extract",
        "segment_crosstab_extract",
        "ir_sheet_kpi_map",
    }
)


def test_structured_call_repairs_once_then_validates_typeadapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(['{"count":"wrong"}', '{"count": 2}'])

    def fake_call(*args: object, **kwargs: object) -> str:
        return next(replies)

    monkeypatch.setattr("llm.structured.call_llm", fake_call)
    parsed = call_llm_structured(
        "return a count",
        purpose="kpi_summary_extract",
        schema=TypeAdapter(dict[str, int]),
    )
    assert parsed == {"count": 2}


def test_transport_provenance_is_written_when_schema_has_columns(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY, called_at TEXT, purpose TEXT, ticker TEXT,
          scope TEXT, model TEXT, prompt_sha256 TEXT, response_sha256 TEXT,
          prompt_chars INTEGER, response_chars INTEGER, input_tokens INTEGER,
          cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
          output_tokens INTEGER, elapsed_ms INTEGER, cost_estimate_usd REAL,
          cache_hit INTEGER, fallback_used TEXT, artifact_id INTEGER, error TEXT,
          run_id TEXT, provider TEXT, transport TEXT, attempts INTEGER,
          retries INTEGER, outcome TEXT, failure_class TEXT
        );
        """
    )
    conn.close()
    record_call(
        LlmCallRecord(
            called_at=datetime.now(UTC),
            model="gpt-5.6-terra",
            prompt_sha256="0" * 64,
            prompt_chars=1,
            elapsed_ms=1,
            purpose="kpi_summary_extract",
            provider="openai",
            transport="subscription_cli",
            attempts=1,
            retries=0,
            outcome="success",
        ),
        db_path=db,
    )
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT provider, transport, attempts, retries, outcome FROM llm_calls"
    ).fetchone()
    conn.close()
    assert row == ("openai", "subscription_cli", 1, 0, "success")


def test_private_claude_bypasses_are_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    callers = (
        "src/compute/kpi_extract_summaries.py",
        "src/compute/segment_quarterly_6k.py",
        "src/compute/segment_definitions.py",
        "src/compute/segment_crosstabs_llm.py",
        "src/ir_pipeline/config_builder.py",
    )
    for relative in callers:
        assert "_call_claude(" not in (root / relative).read_text(encoding="utf-8"), relative


def test_schema_validation_is_not_reported_as_golden_quality_coverage() -> None:
    from evals.coverage import GOLDEN_PURPOSES

    assert _SCHEMA_ONLY_EXTRACTORS.isdisjoint(GOLDEN_PURPOSES)
