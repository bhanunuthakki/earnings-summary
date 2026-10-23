"""The direct-PDF capture seam retains candidates and requires an existing review."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

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
    *,
    bind_current: bool = True,
) -> None:
    if bind_current:
        for exchange in exchanges:
            if not conn.execute(
                "SELECT 1 FROM llm_artifacts WHERE llm_call_id=?", (exchange.llm_call_id,)
            ).fetchone():
                _bind_producing_artifact(conn, exchange)
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


def test_historical_scaled_wire_rehydrates_only_from_exact_old_producing_artifact(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from compute.ir_supplement_capture import SupplementExchange
    from llm_artifact_store import UpsertRequest, upsert
    from tests import test_ir_supplement_continuation as continuation_fixtures

    synthetic_exchange = cast(
        Callable[..., SupplementExchange], getattr(continuation_fixtures, "_synthetic_exchange")
    )

    original_rows, _ = _capture_inputs()
    raw = {
        **original_rows[0][1][0],
        "label": "Revenue",
        "source_excerpt": "Revenue 60,801",
        "source_value_text": "60,801",
        "value": "60801",
        "unit": "actual",
        "currency": "USD",
        "source_unit_scale": "millions",
    }
    exchange = synthetic_exchange(conn, "exact retained historical prompt", [raw])

    def native_pages(raw: bytes) -> tuple[str, ...]:
        return ("Revenue 60,801",)

    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", native_pages)
    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", native_pages)
    # A caller cannot select legacy behavior; unbound bodies stay current/strict.
    with pytest.raises(ValueError, match="unknown or unbound"):
        _wire(conn, [(1, [raw])], [exchange], bind_current=False)
    conn.execute(
        "INSERT INTO documents (ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) VALUES ('TEST','ir_doc','ir_supplement','/synthetic/historical.pdf',?,'2026-10-23T00:00:00Z','ok',1)",
        (sha(PDF_BYTES),),
    )
    doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    artifact_id, _ = upsert(
        UpsertRequest(
            ticker="TEST",
            purpose="kpi_summary_enumerate",
            scope=f"ir_supplement:{doc_id}:page:1:section:0:14",
            prompt_version="ir-supplement-native-continuation-v6-native-hyphen-wrap",
            model=exchange.model,
            llm_call_id=exchange.llm_call_id,
            source_doc_ids=[doc_id],
            cache_inputs=["retained historical input identity"],
            content_json={
                "status": "processed",
                "rows": [raw],
                "exchange": exchange.model_dump(mode="json"),
                "attempts": [exchange.model_dump(mode="json")],
            },
        ),
        conn=conn,
    )
    conn.commit()
    assert artifact_id is not None
    _wire(conn, [(1, [raw])], [exchange], bind_current=False)
    payload = json.loads(
        conn.execute(
            "SELECT text FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
    )
    assert payload["row"]["raw_row"] == raw
    assert (
        conn.execute(
            "SELECT extractor_code_version FROM evidence_extraction_runs WHERE extractor_name='ir-supplement-native-continuation'"
        ).fetchone()[0]
        == "v6-native-hyphen-wrap"
    )
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    # Whole-body integrity, not merely the fact/token, binds eligibility.
    other = SupplementExchange.model_validate(
        {
            **exchange.model_dump(),
            "raw_response": exchange.raw_response + " ",
            "response_sha256": sha(exchange.raw_response + " "),
        }
    )
    with pytest.raises(ValueError, match="unknown or unbound"):
        _wire(conn, [(1, [raw])], [other], bind_current=False)


def _bind_producing_artifact(
    conn: sqlite3.Connection,
    exchange: SupplementExchange,
    version: str = "v14-page-bounded-sections",
    *,
    source_sha256: str | None = None,
) -> None:
    from llm_artifact_store import UpsertRequest, upsert

    conn.execute(
        "INSERT OR IGNORE INTO llm_calls (id,called_at,model,prompt_sha256,prompt_chars,elapsed_ms,purpose,response_sha256,response_chars,provider,transport,outcome,run_id) "
        "VALUES (?,'2026-10-23',?,?,0,0,'kpi_summary_enumerate',?,0,?,?,'success',?)",
        (
            exchange.llm_call_id,
            exchange.model,
            exchange.prompt_sha256,
            exchange.response_sha256,
            exchange.provider,
            exchange.transport,
            exchange.run_id,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO documents (ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
        "VALUES ('TEST','ir_doc','ir_supplement','/synthetic/producing.pdf',?,'2026-10-23','ok',1)",
        (source_sha256 or sha(PDF_BYTES),),
    )
    doc = conn.execute(
        "SELECT id FROM documents WHERE sha256=?", (source_sha256 or sha(PDF_BYTES),)
    ).fetchone()[0]
    prior_factory = conn.row_factory
    upsert(
        UpsertRequest(
            ticker="TEST",
            purpose="kpi_summary_enumerate",
            scope=f"ir_supplement:{doc}:page:1:section:0:14:call:{exchange.llm_call_id}",
            prompt_version=f"ir-supplement-native-continuation-{version}",
            model=exchange.model,
            llm_call_id=exchange.llm_call_id,
            source_doc_ids=[doc],
            cache_inputs=["synthetic identity"],
            content_json={
                "status": "processed",
                "rows": json.loads(exchange.raw_response),
                "exchange": exchange.model_dump(mode="json"),
                "attempts": [exchange.model_dump(mode="json")],
            },
        ),
        conn=conn,
    )
    conn.commit()
    conn.row_factory = prior_factory


@pytest.mark.parametrize("version", ["v13-complete-numeric-prose-end", "v14-page-bounded-sections"])
def test_exact_producing_version_survives_retention(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setattr("provenance.ir_reported_facts.project_pdf_page_texts", page_texts)
    monkeypatch.setattr("compute.ir_supplement_capture.project_pdf_page_texts", page_texts)
    rows, exchanges = _capture_inputs()
    _bind_producing_artifact(conn, exchanges[0], version)
    _wire(conn, rows, exchanges, bind_current=False)
    assert (
        conn.execute(
            "SELECT extractor_code_version FROM evidence_extraction_runs WHERE extractor_name='ir-supplement-native-continuation'"
        ).fetchone()[0]
        == version
    )
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["unknown", "ambiguous", "source", "call"])
def test_producing_version_fails_closed_on_unbound_or_conflicting_identity(
    conn: sqlite3.Connection, failure: str
) -> None:
    from compute.ir_supplement_capture import producing_capture_version

    _, exchanges = _capture_inputs()
    exchange = exchanges[0]
    _bind_producing_artifact(
        conn,
        exchange,
        "v999-unknown" if failure == "unknown" else "v13-complete-numeric-prose-end",
        source_sha256="0" * 64 if failure == "source" else None,
    )
    if failure == "ambiguous":
        _bind_producing_artifact(conn, exchange)
    if failure == "call":
        conn.execute("UPDATE llm_calls SET provider='forged' WHERE id=?", (exchange.llm_call_id,))
        conn.commit()
    with pytest.raises(ValueError, match=r"unknown or unbound|ambiguous|metadata differs"):
        producing_capture_version(conn, exchange, sha(PDF_BYTES))
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
        == 0
    )


def test_v13_identity_does_not_authorize_relaxed_numeric_wire(
    conn: sqlite3.Connection,
) -> None:
    rows, exchanges = _capture_inputs()
    raw = {
        **rows[0][1][0],
        "value": "60801",
        "currency": "USD",
        "unit": "actual",
        "source_value_text": "60,801",
        "source_excerpt": "Revenue 60,801",
        "source_unit_scale": "millions",
    }
    body = json.dumps([raw])
    exchange = exchanges[0].model_copy(update={"raw_response": body, "response_sha256": sha(body)})
    _bind_producing_artifact(conn, exchange, "v13-complete-numeric-prose-end")
    with pytest.raises(ValueError, match="source token and scale"):
        _wire(conn, [(1, [raw])], [exchange], bind_current=False)


def test_mixed_v13_v14_exchanges_cannot_share_retained_population(conn: sqlite3.Connection) -> None:
    rows, exchanges = _capture_inputs()
    first = exchanges[0]
    second = first.model_copy(update={"llm_call_id": 2})
    _bind_producing_artifact(conn, first, "v13-complete-numeric-prose-end")
    _bind_producing_artifact(conn, second)
    with pytest.raises(ValueError, match="mix producing capture versions"):
        _wire(conn, rows, [first, second], bind_current=False)


@pytest.mark.parametrize("change", ["body", "prompt", "run"])
def test_producing_version_rejects_forged_exchange_metadata(
    conn: sqlite3.Connection, change: str
) -> None:
    from compute.ir_supplement_capture import producing_capture_version

    _, exchanges = _capture_inputs()
    exchange = exchanges[0]
    _bind_producing_artifact(conn, exchange)
    updates = {
        "body": {
            "raw_response": exchange.raw_response + " ",
            "response_sha256": sha(exchange.raw_response + " "),
        },
        "prompt": {"prompt_sha256": "0" * 64},
        "run": {"run_id": "forged"},
    }[change]
    with pytest.raises(ValueError, match="unknown or unbound"):
        producing_capture_version(conn, exchange.model_copy(update=updates), sha(PDF_BYTES))
