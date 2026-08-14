from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from ask.grounding import EvidenceItem
from ask.grounding_trace import (
    GroundingTraceError,
    narrative_trace_items,
    persist_grounding_trace,
    view_trace_items,
)
from report.models import CellSource
from viewspec.engine import ViewCell, ViewResult, ViewRow
from viewspec.spec import MetricRef, ViewSpec


def test_narrative_trace_persists_only_locators_and_digests(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "grounding-trace.db")
    evidence = [
        EvidenceItem(
            n=1,
            kind="fact",
            label="WIX · Revenue",
            text="WIX revenue: FY2025 $1.8B",
            doc_id=17,
            href="/source/17",
            source_url="https://www.sec.gov/Archives/example?q=ignored#fragment",
            fact_ref="fin:WIX:revenue:FY",
            ticker="WIX",
            doc_type="10-K",
            period="FY2025",
            value="$1.8B",
        )
    ]

    trace = persist_grounding_trace(
        db_path,
        question="What was WIX revenue?",
        scope_tickers=("WIX",),
        route="narrative",
        strategy="sql_facts_and_lexical_documents",
        outcome="ready",
        items=narrative_trace_items(evidence),
        session_id="session-1",
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT question_sha256,scope_json,item_count,item_set_json,item_set_sha256 "
            "FROM ask_grounding_traces WHERE trace_id=?",
            (trace.trace_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row[3]))
        assert json.loads(str(row[1])) == ["WIX"]
        assert row[2] == 1
        assert payload[0]["fact_ref"] == "fin:WIX:revenue:FY"
        assert payload[0]["source_doc_id"] == 17
        assert payload[0]["source_url"] == "https://www.sec.gov/Archives/example"
        assert payload[0]["evidence_sha256"]
        assert "WIX revenue: FY2025 $1.8B" not in str(row[3])
        assert len(str(row[0])) == len(str(row[4])) == 64
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE ask_grounding_traces SET outcome='no_evidence' WHERE trace_id=?",
                (trace.trace_id,),
            )


def test_view_trace_keeps_period_unit_and_source_coordinates() -> None:
    result = ViewResult(
        spec=ViewSpec(
            tickers=("RBRK",),
            metrics=(MetricRef(domain="fin", key="revenue"),),
        ),
        period_labels=["Q2'26"],
        rows=[
            ViewRow(
                ticker="RBRK",
                metric=MetricRef(domain="fin", key="revenue"),
                label="RBRK · revenue",
                unit="USD",
                cells=[
                    ViewCell(
                        value=309_000_000.0,
                        raw=309_000_000.0,
                        source=CellSource(
                            source="sec_companyfacts",
                            source_url="https://www.sec.gov/Archives/rbrk",
                            doc_id=29,
                            fact_id=41,
                            fact_table="financial_facts",
                        ),
                    )
                ],
            )
        ],
        warnings=[],
    )

    items = view_trace_items(result)

    assert len(items) == 1
    assert items[0].metric_ref == "fin:revenue"
    assert items[0].period == "Q2'26"
    assert items[0].unit == "USD"
    assert items[0].source_doc_id == 29
    assert items[0].fact_id == 41
    assert items[0].source_url == "https://www.sec.gov/Archives/rbrk"


def test_view_trace_rejects_a_numeric_cell_without_source_provenance() -> None:
    result = ViewResult(
        spec=ViewSpec(
            tickers=("RBRK",),
            metrics=(MetricRef(domain="fin", key="revenue"),),
        ),
        period_labels=["Q2'26"],
        rows=[
            ViewRow(
                ticker="RBRK",
                metric=MetricRef(domain="fin", key="revenue"),
                label="RBRK · revenue",
                unit="USD",
                cells=[ViewCell(value=309_000_000.0, raw=309_000_000.0, source=None)],
            )
        ],
        warnings=[],
    )

    with pytest.raises(GroundingTraceError, match="missing source provenance"):
        view_trace_items(result)


def test_trace_persistence_fails_closed_when_schema_is_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    sqlite3.connect(db_path).close()

    with pytest.raises(GroundingTraceError, match="could not be recorded"):
        persist_grounding_trace(
            db_path,
            question="question",
            scope_tickers=("WIX",),
            route="narrative",
            strategy="sql_facts_and_lexical_documents",
            outcome="no_evidence",
            items=(),
            session_id=None,
        )
