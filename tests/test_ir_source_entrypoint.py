"""The automatic IR PDF entrypoint cannot publish unreviewed financial rows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter

import compute.ir_supplement_capture as continuation
import compute.kpi_extract_summaries as summaries
import provenance.ir_reported_facts as ir_facts
from llm.ledger import record_llm_call
from llm.prompt_ab import apply_prompt_override
from llm.structured import StructuredCallResult
from provenance.evidence_backfill import ensure_legacy_document_evidence
from provenance.evidence_ledger import EvidenceLedger, EvidenceNode, LedgerRecord, PersistResult
from provenance.ir_reported_facts import (
    IrCandidatePopulation,
    IrReviewedCandidate,
    IrSourceContextReview,
    persist_ir_source_context_review,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityRegistry,
)
from provenance.source_fact_repository import (
    PublicationReceipt,
    SourceFactPublication,
    SourceFactRepository,
)
from tests.test_ir_reported_facts import PAGE, candidate, payload_sha, sha, witnesses

RICH_PAGE = (
    PAGE
    + " Six months ended June 30, 2026 Margin 20%."
    + " We expect third quarter 2026 total revenue to be in the range of $61-64 billion."
)


def _recorded_governed_call(
    prompt: str, *, response: Callable[..., object], **kwargs: object
) -> object:
    payload = response(prompt, **kwargs)
    raw = json.dumps(payload)
    effective = apply_prompt_override("kpi_summary_enumerate", None, prompt)
    record_llm_call(
        started_at=datetime.now(UTC),
        elapsed_ms=0,
        model="fixture-actual",
        prompt_sha=sha(effective),
        prompt_chars=len(effective),
        purpose="kpi_summary_enumerate",
        ticker=None,
        scope=None,
        run_id=str(kwargs["run_id"]),
        response_text=raw,
        provider="fixture",
        transport="fixture",
        outcome="success",
    )
    schema = cast("TypeAdapter[object]", kwargs["schema"])
    return StructuredCallResult(
        value=schema.validate_python(payload), raw_response=raw, prompt=prompt
    )


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    db = migrated_db(tmp_path / "ir-source-entrypoint.db")
    database = sqlite3.connect(db)
    database.row_factory = sqlite3.Row
    try:
        yield database
    finally:
        database.close()


def _retained_source(conn: sqlite3.Connection, tmp_path: Path) -> Path:
    source = tmp_path / "original.pdf"
    source.write_bytes(b"original source")
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetched_at,fetch_status,raw_bytes_size) VALUES "
        "(1,'TEST','ir_doc','ir_supplement','2026-06-30 00:00:00',?,?,'2026-07-01','ok',15)",
        (str(source), sha(source.read_bytes())),
    )
    ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=1)
    conn.commit()
    return source


def _entrypoint_fixture(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    two_rows: bool = False,
    rich_population: bool = False,
    mock_enumerator: bool = True,
) -> Path:
    source = _retained_source(conn, tmp_path)
    issuer_id = str(
        conn.execute(
            "SELECT issuer_id FROM evidence_document_versions WHERE legacy_document_id=1"
        ).fetchone()[0]
    )
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        ReportingEntity(
            reporting_entity_id="fixture-reporting-entity",
            idempotency_key="fixture-reporting-entity",
            issuer_id=issuer_id,
            reporting_entity_kind="legal_registrant",
            display_name="Fixture issuer",
            created_at=datetime.now(UTC),
        )
    )
    now = datetime.now(UTC)
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="fixture-source-binding",
            idempotency_key="fixture-source-binding",
            recorded_issuer_id=issuer_id,
            revision=1,
            issuer_id=issuer_id,
            reporting_entity_id="fixture-reporting-entity",
            outcome="selected",
            decision_kind="manual",
            material_dissent=False,
            reason_code="synthetic_exact_subject",
            reason_details=(("fixture", "exact issuer"),),
            effective_at=now,
            knowledge_at=now,
            recorded_at=now,
        )
    )
    conn.commit()

    def list_sources(*args: object) -> list[tuple[int, Path, datetime]]:
        return [(1, source, datetime(2026, 6, 30))]

    def route(*args: object, **kwargs: object) -> tuple[str, str]:
        return ("fixture", "fixture")

    def native_pages(raw: bytes) -> tuple[str, ...]:
        return (RICH_PAGE,)

    monkeypatch.setattr(summaries, "_list_ir_capture_pdf_docs", list_sources)
    monkeypatch.setattr(summaries, "resolve_model_and_backend", route)
    monkeypatch.setattr(continuation, "project_pdf_page_texts", native_pages)
    monkeypatch.setattr(ir_facts, "project_pdf_page_texts", native_pages)

    def extract(ticker: str, period: str, text: str, **kwargs: object) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = [
            {
                "label": "Margin",
                "value": 10,
                "unit": "percent",
                "source_excerpt": "Margin North 10%",
                "source_value_text": "10%",
                "reported_period_end": "2026-06-30",
                "period_role": "current",
                "accounting_basis": "management",
                "consolidation_scope": "segment",
                "dimensions": {"Geography": "North"},
                "source_unit_scale": "none",
            }
        ]
        if two_rows:
            rows.append(
                {
                    **rows[0],
                    "value": 8,
                    "source_excerpt": "Margin North 8%",
                    "source_value_text": "8%",
                }
            )
        if rich_population:
            rows.extend(
                [
                    {
                        **rows[0],
                        "value": 8,
                        "source_excerpt": "Margin North 8%",
                        "source_value_text": "8%",
                        "reported_period_end": "2025-06-30",
                        "period_role": "prior_year_comparator",
                    },
                    {
                        **rows[0],
                        "value": 20,
                        "source_excerpt": "Six months ended June 30, 2026 Margin 20%",
                        "source_value_text": "20%",
                        "reported_period_start": "2026-01-01",
                    },
                ]
            )
            guidance: dict[str, object] = {
                "label": "Revenue guidance",
                "value": 61_000_000_000,
                "unit": "actual",
                "currency": "USD",
                "source_excerpt": "We expect third quarter 2026 total revenue to be in the range of $61-64 billion.",
                "source_value_text": "61",
                "reported_period_end": "2026-09-30",
                "period_role": "guidance",
                "range_bound": "lower",
                "accounting_basis": "management",
                "consolidation_scope": "consolidated",
                "dimensions": {},
                "source_unit_scale": "billions",
            }
            rows.extend(
                [
                    guidance,
                    {
                        **guidance,
                        "value": 64_000_000_000,
                        "source_value_text": "64",
                        "range_bound": "upper",
                    },
                ]
            )
        prompt = getattr(summaries, "_enumerate_prompt")(
            ticker, period, text, 200, source_scope="direct_pdf_rich"
        )
        raw = json.dumps(rows)
        record_llm_call(
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            model="fixture",
            prompt_sha=hashlib.sha256(prompt.encode()).hexdigest(),
            prompt_chars=len(prompt),
            purpose="kpi_summary_enumerate",
            ticker=ticker,
            scope=None,
            run_id=str(kwargs["run_id"]),
            response_text=raw,
            provider="fixture",
            transport="fixture",
            outcome="success",
        )
        observer = cast(
            "Callable[[StructuredCallResult[list[object]]], None]", kwargs["exchange_observer"]
        )
        observer(StructuredCallResult(value=[], raw_response=raw, prompt=prompt))
        return rows

    if mock_enumerator:
        monkeypatch.setattr(summaries, "_llm_extract_enumerate", extract)
    return source


def _review_retained_population(
    conn: sqlite3.Connection, source: Path, *, accept_first: bool
) -> None:
    run = conn.execute(
        "SELECT extraction_run_id,output_sha256,extractor_config_sha256,document_version_id "
        "FROM evidence_extraction_runs "
        "WHERE extractor_name='ir-supplement-native-continuation'"
    ).fetchone()
    assert run is not None
    retained = conn.execute(
        "SELECT node_id,text FROM evidence_nodes WHERE extraction_run_id=? "
        "AND node_id LIKE 'ir-pdf-candidate:%' ORDER BY node_id",
        (run[0],),
    ).fetchall()
    assert retained
    population = IrCandidatePopulation(
        document_version_id=str(run[3]),
        source_sha256=sha(source.read_bytes()),
        extraction_run_id=str(run[0]),
        extractor_config_sha256=str(run[2]),
        candidate_population_sha256=str(run[1]),
        candidate_node_ids=tuple(str(row[0]) for row in retained),
        retained_node_count=2 * len(retained),
        exact_replay=True,
    )
    entries: list[IrReviewedCandidate] = []
    for node_id, text_value in retained:
        raw = json.loads(str(text_value))["row"]["raw_row"]
        accepted = accept_first and raw["source_value_text"] == "10%"
        if accepted:
            fact = candidate("Margin North 10%", "10%", "10").model_copy(
                update={"source_row_key": str(node_id)}
            )
            entries.append(
                IrReviewedCandidate(
                    candidate_node_id=str(node_id),
                    disposition="accepted",
                    fact=fact,
                    witnesses=witnesses(fact),
                )
            )
        else:
            entries.append(
                IrReviewedCandidate(
                    candidate_node_id=str(node_id),
                    disposition="rejected",
                    reason_code="source_context_unapproved",
                )
            )
    payload = {
        "schema_version": "ir-pdf-source-context-review.v1",
        "document_version_id": str(run[3]),
        "source_sha256": sha(source.read_bytes()),
        "extraction_run_id": str(run[0]),
        "extractor_config_sha256": str(run[2]),
        "candidate_population_sha256": str(run[1]),
        "reviewer_identity": "fixture-human-reviewer",
        "review_method": "manual_source_context_review",
        "reviewed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "review_evidence": "synthetic native page and exact rows inspected",
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    review = IrSourceContextReview.model_validate(
        {**payload, "review_sha256": payload_sha(payload)}
    )
    persist_ir_source_context_review(conn, review, population, raw_pdf_bytes=source.read_bytes())


def test_full_entrypoint_retains_unreviewed_rows_without_financial_writes(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entrypoint_fixture(conn, tmp_path, monkeypatch)
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert result.error is None
    assert result.kpis_inserted_total == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
        == 1
    )
    artifact = conn.execute("SELECT content_json FROM llm_artifacts").fetchone()
    assert artifact is not None
    exchange = json.loads(str(artifact[0]))["exchange"]
    submitted = str(exchange["submitted_prompt"])
    assert "prior-year comparatives" in submitted
    assert "TWO rows with the SAME verbatim source_excerpt" in submitted
    assert exchange["prompt_sha256"] == sha(submitted)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE prompt_sha256=? AND response_sha256=?",
            (exchange["prompt_sha256"], exchange["response_sha256"]),
        ).fetchone()[0]
        == 1
    )


def test_full_entrypoint_retains_comparator_ytd_and_guidance_range_as_raw_candidates(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entrypoint_fixture(conn, tmp_path, monkeypatch, rich_population=True)
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert result.error is None
    rows = [
        json.loads(str(row[0]))["row"]["raw_row"]
        for row in conn.execute(
            "SELECT text FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        )
    ]
    assert len(rows) == 5
    assert any(
        row["period_role"] == "prior_year_comparator" and row["reported_period_end"] == "2025-06-30"
        for row in rows
    )
    assert any(
        row.get("reported_period_start") == "2026-01-01" and row["source_value_text"] == "20%"
        for row in rows
    )
    guidance = [row for row in rows if row["period_role"] == "guidance"]
    assert {row["range_bound"] for row in guidance} == {"lower", "upper"}
    assert {row["source_value_text"] for row in guidance} == {"61", "64"}
    assert {row["value"] for row in guidance} == {61_000_000_000, 64_000_000_000}
    assert len({row["source_excerpt"] for row in guidance}) == 1
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0


def test_capped_lower_range_bound_recurses_and_retains_complete_raw_population(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entrypoint_fixture(conn, tmp_path, monkeypatch, mock_enumerator=False)
    actuals: list[dict[str, object]] = [
        {
            "label": "Margin",
            "value": value,
            "unit": "percent",
            "source_excerpt": f"Margin North {token}",
            "source_value_text": token,
            "reported_period_end": "2026-06-30",
            "period_role": "current",
            "accounting_basis": "management",
            "consolidation_scope": "segment",
            "dimensions": {"Geography": "North"},
            "source_unit_scale": "none",
        }
        for value, token in [(10, "10%"), (8, "8%")]
    ]
    guidance: dict[str, object] = {
        "label": "Revenue guidance",
        "value": 61_000_000_000,
        "unit": "actual",
        "currency": "USD",
        "source_excerpt": "We expect third quarter 2026 total revenue to be in the range of $61-64 billion.",
        "source_value_text": "61",
        "reported_period_end": "2026-09-30",
        "period_role": "guidance",
        "range_bound": "lower",
        "accounting_basis": "management",
        "consolidation_scope": "consolidated",
        "dimensions": {},
        "source_unit_scale": "billions",
    }
    upper = {**guidance, "value": 64_000_000_000, "source_value_text": "64", "range_bound": "upper"}
    native_page = (
        "Margin North 10%. Margin North 8%.\n"
        "We expect third quarter 2026 total revenue to be in the range of $61-64 billion.\n"
    )

    def native_pages(raw: bytes) -> tuple[str, ...]:
        return (native_page,)

    monkeypatch.setattr(continuation, "project_pdf_page_texts", native_pages)
    monkeypatch.setattr(ir_facts, "project_pdf_page_texts", native_pages)
    responses: list[list[dict[str, object]]] = []

    def capped_then_complete(prompt: str, **_kwargs: object) -> object:
        if "CONTEXT ONLY (page header" not in prompt:
            rows = [*actuals, guidance]
        elif "EXTRACT ONLY FROM THIS SOURCE SECTION:\nMargin North" in prompt:
            rows = actuals
        else:
            rows = [guidance, upper]
        responses.append(rows)
        return rows

    monkeypatch.setattr(
        summaries,
        "call_llm_structured_with_raw",
        partial(_recorded_governed_call, response=capped_then_complete),
    )
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn, max_facts_per_doc=3)
    assert result.error is None
    assert len(responses) == 3
    artifacts = [
        json.loads(str(row[0])) for row in conn.execute("SELECT content_json FROM llm_artifacts")
    ]
    assert sorted(artifact["status"] for artifact in artifacts) == [
        "processed",
        "processed",
        "saturated",
    ]
    saturated = next(artifact for artifact in artifacts if artifact["status"] == "saturated")
    assert saturated["rows"] == []
    assert json.loads(saturated["exchange"]["raw_response"])[-1] == guidance
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 3
    retained = [
        json.loads(str(row[0]))["row"]["raw_row"]
        for row in conn.execute(
            "SELECT text FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        )
    ]
    assert len(retained) == 4
    assert {row["range_bound"] for row in retained if row["period_role"] == "guidance"} == {
        "lower",
        "upper",
    }
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0

    replay = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn, max_facts_per_doc=3)
    assert replay.error is None
    assert len(responses) == 3
    assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 3


def test_unsaturated_lone_guidance_bound_fails_closed(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entrypoint_fixture(conn, tmp_path, monkeypatch, mock_enumerator=False)
    quote = "We expect third quarter 2026 total revenue to be in the range of $61-64 billion."
    guidance: dict[str, object] = {
        "label": "Revenue guidance",
        "value": 61_000_000_000,
        "unit": "actual",
        "currency": "USD",
        "source_excerpt": quote,
        "source_value_text": "61",
        "reported_period_end": "2026-09-30",
        "period_role": "guidance",
        "range_bound": "lower",
        "accounting_basis": "management",
        "consolidation_scope": "consolidated",
        "dimensions": {},
        "source_unit_scale": "billions",
    }

    def single_guidance(prompt: str, **kwargs: object) -> object:
        return [guidance]

    monkeypatch.setattr(
        summaries,
        "call_llm_structured_with_raw",
        partial(_recorded_governed_call, response=single_guidance),
    )
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn, max_facts_per_doc=3)
    assert result.error and "both exact endpoints" in result.error
    assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0


def test_capped_rows_still_reject_numeric_token_outside_source_quote(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entrypoint_fixture(conn, tmp_path, monkeypatch, mock_enumerator=False)
    rows: list[dict[str, object]] = [
        {
            "label": "Margin",
            "value": value,
            "unit": "percent",
            "source_excerpt": quote,
            "source_value_text": token,
            "reported_period_end": "2026-06-30",
            "period_role": "current",
            "accounting_basis": "management",
            "consolidation_scope": "segment",
            "dimensions": {"Geography": "North"},
            "source_unit_scale": "none",
        }
        for value, quote, token in [
            (10, "Margin North 10%", "10%"),
            (8, "Margin North 8%", "8%"),
            (20, "Margin 20%", "22%"),
        ]
    ]

    def capped_bad_rows(prompt: str, **kwargs: object) -> object:
        return rows

    monkeypatch.setattr(
        summaries,
        "call_llm_structured_with_raw",
        partial(_recorded_governed_call, response=capped_bad_rows),
    )
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn, max_facts_per_doc=3)
    assert result.error and "numeric token must occur" in result.error
    assert conn.execute("SELECT COUNT(*) FROM llm_artifacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0


def test_source_changes_during_candidate_write_roll_back_evidence(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _entrypoint_fixture(conn, tmp_path, monkeypatch)
    original_persist = EvidenceLedger.persist

    def persist_then_replace(self: EvidenceLedger, record: LedgerRecord) -> PersistResult:
        result = original_persist(self, record)
        if isinstance(record, EvidenceNode) and record.node_id.startswith("ir-pdf-candidate:"):
            source.write_bytes(b"replacement before candidate commit")
        return result

    monkeypatch.setattr(EvidenceLedger, "persist", persist_then_replace)
    result = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert result.error and "changed" in result.error
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
        ).fetchone()[0]
        == 0
    )
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("two_rows", "accept_first", "expected_source_facts"),
    [(False, False, 0), (True, True, 1)],
)
def test_full_entrypoint_uses_only_exact_reviewed_source_population(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    two_rows: bool,
    accept_first: bool,
    expected_source_facts: int,
) -> None:
    source = _entrypoint_fixture(conn, tmp_path, monkeypatch, two_rows=two_rows)
    first = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert first.error is None
    _review_retained_population(conn, source, accept_first=accept_first)
    second = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert second.error is None
    assert second.kpis_inserted_total == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == expected_source_facts
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone()[0] == (
        expected_source_facts
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM evidence_nodes WHERE node_id LIKE 'ir-pdf-candidate:%'"
    ).fetchone()[0] == (2 if two_rows else 1)


def test_source_changes_during_reviewed_publication_roll_back_financial_graph(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _entrypoint_fixture(conn, tmp_path, monkeypatch)
    first = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert first.error is None
    _review_retained_population(conn, source, accept_first=True)
    original_publish = SourceFactRepository.publish

    def publish_then_replace(
        self: SourceFactRepository, publication: SourceFactPublication
    ) -> PublicationReceipt:
        receipt = original_publish(self, publication)
        source.write_bytes(b"replacement before source publication commit")
        return receipt

    monkeypatch.setattr(SourceFactRepository, "publish", publish_then_replace)
    second = summaries.capture_for_ir_pdf_docs("TEST", tmp_path, conn)
    assert second.error and "changed" in second.error
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
