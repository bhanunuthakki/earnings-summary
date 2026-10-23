"""The direct-PDF capture seam retains candidates and requires an existing review."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from compute.ir_supplement_capture import SupplementExchange
from compute.kpi_extract_summaries import retain_and_publish_reviewed_ir_source
from provenance.ir_reported_facts import (
    IrCandidatePopulation,
    IrReviewedCandidate,
    IrSourceContextReview,
    persist_ir_source_context_review,
)
from tests.test_ir_reported_facts import (
    PDF_BYTES,
    STAMP,
    add_ir_document,
    candidate,
    page_texts,
    payload_sha,
    seed_foundation,
    sha,
    witnesses,
)


@pytest.fixture
def conn(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "ir-source-wire.db"
    migrated_db(path)
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys=ON")
    seed_foundation(database)
    database.commit()
    add_ir_document(database)
    try:
        yield database
    finally:
        database.close()


def _capture_inputs() -> tuple[list[tuple[int, list[dict[str, object]]]], list[SupplementExchange]]:
    raw: dict[str, object] = {
        "label": "Margin",
        "value": "10",
        "unit": "percent",
        "currency": None,
        "source_excerpt": "Margin North 10%",
        "source_value_text": "10%",
        "reported_period_start": None,
        "reported_period_end": "2026-06-30",
        "period_role": "current",
        "range_bound": None,
        "accounting_basis": "management",
        "consolidation_scope": "segment",
        "dimensions": {"Geography": "North"},
        "source_unit_scale": "none",
        "source_row_label": "Margin",
        "source_column_header": "Three months ended June 30, 2026",
    }
    response = json.dumps([raw])
    exchange = SupplementExchange(
        raw_response=response,
        submitted_prompt="fixture prompt",
        model="fixture-model",
        provider="fixture-provider",
        transport="fixture-transport",
        prompt_sha256=sha("fixture prompt"),
        response_sha256=sha(response),
        llm_call_id=1,
        run_id="fixture-run",
    )
    return [(1, [raw])], [exchange]


def _wire(
    conn: sqlite3.Connection,
    page_rows: list[tuple[int, list[dict[str, object]]]],
    exchanges: list[SupplementExchange],
) -> None:
    retain_and_publish_reviewed_ir_source(
        conn,
        document_version_id="ir-document-v1",
        source_sha256=sha(PDF_BYTES),
        raw_pdf_bytes=PDF_BYTES,
        page_rows=page_rows,
        exchanges=exchanges,
        max_facts_per_doc=200,
        extractor_config_sha256=sha("fixture wire config"),
        extraction_started_at=STAMP,
    )


def test_wire_retains_raw_response_without_auto_review_or_financial_write(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", page_texts)
    page_rows, exchanges = _capture_inputs()
    _wire(conn, page_rows, exchanges)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-response:%'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    raw_node = str(
        conn.execute(
            "SELECT text FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-response:%'"
        ).fetchone()[0]
    )
    assert json.loads(raw_node)["response"]["raw_response"] == exchanges[0].raw_response


def test_wire_publishes_only_after_exact_review_node_exists(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", page_texts)
    page_rows, exchanges = _capture_inputs()
    _wire(conn, page_rows, exchanges)
    node_id, run_id = conn.execute(
        "SELECT node_id,extraction_run_id FROM evidence_nodes "
        "WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone()
    run = conn.execute(
        "SELECT output_sha256,extractor_config_sha256 FROM evidence_extraction_runs "
        "WHERE extraction_run_id=?",
        (run_id,),
    ).fetchone()
    population = IrCandidatePopulation(
        document_version_id="ir-document-v1",
        source_sha256=sha(PDF_BYTES),
        extraction_run_id=str(run_id),
        extractor_config_sha256=str(run[1]),
        candidate_population_sha256=str(run[0]),
        candidate_node_ids=(str(node_id),),
        retained_node_count=2,
        exact_replay=True,
    )
    fact = candidate("Margin North 10%", "10%", "10").model_copy(
        update={"source_row_key": str(node_id)}
    )
    entry = IrReviewedCandidate(
        candidate_node_id=str(node_id),
        disposition="accepted",
        fact=fact,
        witnesses=witnesses(fact),
    )
    payload = {
        "schema_version": "ir-pdf-source-context-review.v1",
        "document_version_id": "ir-document-v1",
        "source_sha256": sha(PDF_BYTES),
        "extraction_run_id": str(run_id),
        "extractor_config_sha256": str(run[1]),
        "candidate_population_sha256": str(run[0]),
        "reviewer_identity": "fixture-human-reviewer",
        "review_method": "manual_source_context_review",
        "reviewed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "review_evidence": "fixture source header and row inspected",
        "entries": [entry.model_dump(mode="json")],
    }
    review = IrSourceContextReview.model_validate(
        {
            **payload,
            "review_sha256": payload_sha(payload),
        }
    )
    persist_ir_source_context_review(conn, review, population, raw_pdf_bytes=PDF_BYTES)
    _wire(conn, page_rows, exchanges)
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (1,)


@pytest.mark.parametrize(
    ("empty", "same_prompt", "page_numbers"),
    [(True, False, (1, 2)), (False, False, (1, 2)), (False, True, (1, 1))],
)
def test_identical_response_bodies_preserve_each_governed_exchange_and_replay(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    empty: bool,
    same_prompt: bool,
    page_numbers: tuple[int, int],
) -> None:
    def repeated_pages(raw: bytes) -> tuple[str, ...]:
        page = page_texts(raw)[0]
        return (page, page)

    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", repeated_pages)
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", repeated_pages)
    first_rows, first_exchanges = _capture_inputs()
    raw_rows = [] if empty else first_rows[0][1]
    body = json.dumps(raw_rows)
    exchanges = [
        first_exchanges[0].model_copy(
            update={
                "raw_response": body,
                "response_sha256": sha(body),
                "submitted_prompt": "same prompt" if same_prompt else f"page prompt {index}",
                "prompt_sha256": sha("same prompt" if same_prompt else f"page prompt {index}"),
                "llm_call_id": index,
            }
        )
        for index in (1, 2)
    ]
    _wire(conn, [(page, raw_rows) for page in page_numbers], exchanges)
    responses = conn.execute(
        "SELECT node_id,text FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-response:%'"
    ).fetchall()
    assert len(responses) == 2
    original_calls = {
        node_id: json.loads(text)["response"]["llm_call_id"] for node_id, text in responses
    }
    assert set(original_calls.values()) == {1, 2}
    candidates = conn.execute(
        "SELECT text,parent_node_id FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchall()
    assert len(candidates) == (0 if empty else 2)
    if not empty:
        assert {original_calls[parent] for _, parent in candidates} == {1, 2}
        assert sorted(json.loads(text)["row"]["page_number"] for text, _ in candidates) == sorted(
            page_numbers
        )
    before = conn.execute("SELECT * FROM evidence_nodes ORDER BY node_id").fetchall()
    clocks = conn.execute("SELECT * FROM evidence_extraction_runs").fetchall()
    _wire(conn, [(page, raw_rows) for page in page_numbers], exchanges)
    assert conn.execute("SELECT * FROM evidence_nodes ORDER BY node_id").fetchall() == before
    assert conn.execute("SELECT * FROM evidence_extraction_runs").fetchall() == clocks
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone() == (0,)
