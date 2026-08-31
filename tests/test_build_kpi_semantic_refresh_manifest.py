from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

import pipeline.kpi_semantic_review as review_module
from execution.apply_kpi_semantic_refresh import (
    RefreshManifest,
    RepairBlockedError,
    validate_refresh_entry,
)
from execution.build_kpi_semantic_refresh_manifest import (
    KpiSemanticRefreshDecisionBatch,
    ReviewedKpiSemanticDecision,
    build_kpi_semantic_refresh_manifest,
    write_refresh_manifest,
)
from models.facts import FactLocator, LocatorKind
from operations.kpi_semantic_review_export import (
    KpiSemanticReviewExport,
    payload_sha256,
    seal_kpi_semantic_review_export,
)
from pipeline.kpi_semantic_review import (
    KpiEvidenceLocatorCoordinates,
    KpiSemanticReviewBatch,
    build_kpi_semantic_review_batch,
)
from provenance.evidence_ledger import EvidenceLocator
from provenance.fulltext_extractor_identity import resolve_fulltext_extractor_identity

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
SOURCE_SHA = "a" * 64
REVIEW_SHA = "b" * 64
BACKUP_SHA = "c" * 64
SOURCE_TEXT = (
    "Q4 2024 consolidated management KPI; figures in millions; "
    "Total customers reached 114.2 million."
)
EVIDENCE_LOCATOR = EvidenceLocator(source_ref="C:/sources/nu-q4.pdf", page_number=7)
LOCATOR_JSON = EVIDENCE_LOCATOR.canonical_json
LOCATOR_SHA = EVIDENCE_LOCATOR.canonical_sha256


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0033_add_report_kpi_reference_resolutions');
        CREATE TABLE tracked_companies(
            ticker TEXT, list_type TEXT, user_id TEXT, archived_at TEXT
        );
        INSERT INTO tracked_companies VALUES ('NU','portfolio','owner',NULL);
        CREATE TABLE kpi_definitions(
            id INTEGER PRIMARY KEY,ticker TEXT,name TEXT,unit TEXT
        );
        INSERT INTO kpi_definitions VALUES (1,'NU','Total customers','count');
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
            kpi_definition_id INTEGER,value TEXT,unit TEXT,currency TEXT,
            source_doc_id INTEGER,supersedes_id INTEGER,source_excerpt TEXT,locator TEXT
        );
        INSERT INTO kpi_facts VALUES (
            10,'NU','2024-12-31','Q4',1,'114200000','count',NULL,2,NULL,
            'Total customers reached 114.2 million.',NULL
        );
        CREATE VIEW v_kpi_facts_resolved_current AS
        SELECT fact.* FROM kpi_facts fact WHERE NOT EXISTS (
            SELECT 1 FROM kpi_facts successor WHERE successor.supersedes_id=fact.id
        );
        CREATE TABLE documents(
            id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,doc_type TEXT,
            period_end TEXT,sha256 TEXT,fetched_at TEXT,parent_document_id INTEGER,
            file_path TEXT
        );
        INSERT INTO documents VALUES (
            2,'NU','ir_doc','ir_presentation','2024-12-31',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2025-01-30T12:00:00+00:00',NULL,'C:/sources/nu-q4.pdf'
        );
        CREATE TABLE kpi_fact_semantic_contexts(
            id INTEGER PRIMARY KEY,kpi_fact_id INTEGER,revision INTEGER,
            supersedes_context_id INTEGER,metric_name_as_reported TEXT,
            reported_period_end TEXT,period_role TEXT,publication_lane TEXT,
            accounting_basis TEXT,consolidation_scope TEXT,dimensions_json TEXT,
            unit_scale TEXT,source_row_label TEXT,source_column_header TEXT,
            source_value_text TEXT,status TEXT,reason_code TEXT,reviewed_by TEXT,
            knowledge_at TEXT
        );
        INSERT INTO kpi_fact_semantic_contexts VALUES (
            5,10,1,NULL,'Total customers','2024-12-31','unknown','unclassified',
            'unknown','unknown','{}','unknown',NULL,NULL,NULL,'quarantined',
            'exact_numeric_evidence_candidates','source_review:owner',
            '2026-08-30T10:00:00Z'
        );
        CREATE TABLE evidence_document_versions(
            document_version_id TEXT,legacy_document_id INTEGER,version_sequence INTEGER,
            blob_sha256 TEXT,ticker TEXT
        );
        INSERT INTO evidence_document_versions VALUES (
            'doc-v1',2,1,
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','NU'
        );
        CREATE TABLE evidence_extraction_runs(
            extraction_run_id TEXT,document_version_id TEXT,input_sha256 TEXT,
            extractor_name TEXT,extractor_config_sha256 TEXT,extractor_code_version TEXT,
            outcome TEXT
        );
        CREATE TABLE evidence_nodes(
            node_id TEXT,extraction_run_id TEXT,node_kind TEXT,text TEXT,
            locator_json TEXT,locator_sha256 TEXT,supersedes_node_id TEXT
        );
        CREATE TABLE v_legacy_document_evidence_bindings_current(
            legacy_document_id INTEGER,document_version_id TEXT,evidence_node_id TEXT,
            scope_content_sha256 TEXT
        );
        """
    )
    extractor = resolve_fulltext_extractor_identity("C:/sources/nu-q4.pdf", None)
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?)",
        (
            "run-1",
            "doc-v1",
            SOURCE_SHA,
            extractor.name,
            extractor.config_sha256,
            extractor.code_version,
            "succeeded",
        ),
    )
    conn.executemany(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,NULL)",
        (
            ("document-node", "run-1", "document", "", LOCATOR_JSON, LOCATOR_SHA),
            ("page-7", "run-1", "pdf_page", SOURCE_TEXT, LOCATOR_JSON, LOCATOR_SHA),
        ),
    )
    conn.execute(
        "INSERT INTO v_legacy_document_evidence_bindings_current VALUES (2,'doc-v1',"
        "'document-node',?)",
        (SOURCE_SHA,),
    )
    return conn


def _use_spreadsheet_evidence(
    conn: sqlite3.Connection,
    *,
    coordinate_field: Literal["cell_address", "cell_range"],
    coordinate_value: str,
) -> EvidenceLocator:
    locator = (
        EvidenceLocator(
            source_ref="C:/sources/nu-q4.xlsx",
            sheet_name="Customer KPIs",
            cell_address=coordinate_value,
        )
        if coordinate_field == "cell_address"
        else EvidenceLocator(
            source_ref="C:/sources/nu-q4.xlsx",
            sheet_name="Customer KPIs",
            cell_range=coordinate_value,
        )
    )
    extractor = resolve_fulltext_extractor_identity("C:/sources/nu-q4.xlsx", None)
    conn.execute(
        "UPDATE documents SET doc_type='ir_historical_spreadsheet',period_end=NULL,file_path=? "
        "WHERE id=2",
        ("C:/sources/nu-q4.xlsx",),
    )
    conn.execute(
        "UPDATE evidence_extraction_runs SET extractor_name=?,extractor_config_sha256=?,"
        "extractor_code_version=? WHERE extraction_run_id='run-1'",
        (extractor.name, extractor.config_sha256, extractor.code_version),
    )
    conn.execute(
        "UPDATE evidence_nodes SET node_kind='table_cell',locator_json=?,locator_sha256=? "
        "WHERE node_id='page-7'",
        (locator.canonical_json, locator.canonical_sha256),
    )
    return locator


def _review(
    conn: sqlite3.Connection, tmp_path: Path, *, limit: int = 5_000
) -> KpiSemanticReviewBatch:
    return build_kpi_semantic_review_batch(
        conn,
        repo_root=tmp_path,
        user_id="owner",
        ticker="NU",
        limit=limit,
        observed_at=NOW,
        after_fact_id=0,
    )


def _export(review: KpiSemanticReviewBatch) -> KpiSemanticReviewExport:
    return seal_kpi_semantic_review_export(
        review=review,
        code_instance_sha256="d" * 64,
        database_instance_sha256="e" * 64,
        schema_revision="0033_add_report_kpi_reference_resolutions",
        next_after_fact_id=review.items[-1].fact_id if review.truncated else None,
    )


def _decision(
    review_batch: KpiSemanticReviewBatch, **changes: object
) -> ReviewedKpiSemanticDecision:
    values: dict[str, object] = {
        "fact_id": 10,
        "action": "bind_existing",
        "expected_context_head_id": 5,
        "expected_context_revision": 1,
        "expected_old_source_sha256": SOURCE_SHA,
        "evidence_candidate_index": 0,
        "context": {
            "metric_name_as_reported": "Total customers",
            "reported_period_end": "2024-12-31",
            "period_role": "current",
            "publication_lane": "current_actual",
            "accounting_basis": "management",
            "consolidation_scope": "consolidated",
            "dimensions": {},
            "unit_scale": "millions",
            "source_row_label": "Total customers",
            "source_column_header": "Q4 2024",
            "status": "admitted",
        },
        "semantic_evidence": {
            "metric_name_value": "Total customers",
            "metric_name_quote": "Total customers",
            "reported_period_end_value": "2024-12-31",
            "reported_period_quote": "Q4 2024",
            "accounting_basis_value": "management",
            "accounting_basis_quote": "management KPI",
            "consolidation_scope_value": "consolidated",
            "consolidation_scope_quote": "consolidated",
            "unit_scale_value": "millions",
            "unit_scale_quote": "figures in millions",
            "dimension_values": {},
            "dimension_quotes": {},
        },
    }
    values.update(changes)
    return ReviewedKpiSemanticDecision.model_validate(values)


def _decisions(
    review_export: KpiSemanticReviewExport, **decision_changes: object
) -> KpiSemanticRefreshDecisionBatch:
    review_batch = review_export.review
    return KpiSemanticRefreshDecisionBatch(
        schema_version="kpi_semantic_refresh_decisions.v2",
        review_export_sha256=review_export.content_sha256,
        review_batch_sha256=review_batch.content_sha256,
        reviewer="owner",
        logical_idempotency_key="nu:doc-v1:semantic-review:v1",
        knowledge_at=NOW,
        review_bundle_sha256=REVIEW_SHA,
        expected_schema_revision="0033_add_report_kpi_reference_resolutions",
        backup_restore_evidence_id=BACKUP_SHA,
        decisions=(_decision(review_batch, **decision_changes),),
    )


def test_builds_deterministic_source_bound_refresh_manifest(tmp_path: Path) -> None:
    conn = _database()
    review_batch = _review(conn, tmp_path)
    review_export = _export(review_batch)
    decisions = _decisions(review_export)
    changes_before = conn.total_changes

    first = build_kpi_semantic_refresh_manifest(
        conn, repo_root=tmp_path, review_export=review_export, decisions=decisions, now=NOW
    )
    second = build_kpi_semantic_refresh_manifest(
        conn, repo_root=tmp_path, review_export=review_export, decisions=decisions, now=NOW
    )

    assert first == second
    assert conn.total_changes == changes_before
    assert RefreshManifest.model_validate_json(first.model_dump_json()) == first
    assert len(first.entries) == 1
    entry = first.entries[0]
    assert entry.expected_context_head_id == 5
    assert entry.expected_context_revision == 1
    assert entry.source_value_text == "114.2"
    assert str(entry.value) == "114200000.0"
    assert entry.source_excerpt == SOURCE_TEXT
    assert entry.locator.pdf_page == 7
    assert entry.locator.verbatim_snippet == SOURCE_TEXT

    output = tmp_path / "refresh-manifest.json"
    write_refresh_manifest(output, first)
    before = output.read_bytes()
    write_refresh_manifest(output, second)
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    ("coordinate_field", "coordinate_value"),
    (("cell_address", "B7"), ("cell_range", "B7:C7")),
)
def test_spreadsheet_coordinates_survive_export_v5_and_executor_validation(
    tmp_path: Path,
    coordinate_field: Literal["cell_address", "cell_range"],
    coordinate_value: str,
) -> None:
    conn = _database()
    source_locator = _use_spreadsheet_evidence(
        conn,
        coordinate_field=coordinate_field,
        coordinate_value=coordinate_value,
    )
    review_export = _export(_review(conn, tmp_path))

    manifest = build_kpi_semantic_refresh_manifest(
        conn,
        repo_root=tmp_path,
        review_export=review_export,
        decisions=_decisions(review_export),
        now=NOW,
    )

    entry = RefreshManifest.model_validate_json(manifest.model_dump_json()).entries[0]
    assert entry.locator.kind is LocatorKind.SPREADSHEET_CELL
    assert entry.locator.spreadsheet_cell is not None
    assert entry.locator.spreadsheet_cell.sheet_name == source_locator.sheet_name
    assert entry.locator.spreadsheet_cell.cell_address == source_locator.cell_address
    assert entry.locator.spreadsheet_cell.cell_range == source_locator.cell_range


def test_rejects_decision_before_review_and_tampered_export_coordinates(tmp_path: Path) -> None:
    conn = _database()
    review_export = _export(_review(conn, tmp_path))
    stale_decisions = _decisions(review_export).model_copy(
        update={"knowledge_at": NOW.replace(hour=11)}
    )
    with pytest.raises(ValueError, match="predates"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=stale_decisions,
            now=NOW,
        )

    boundary_decisions = _decisions(review_export).model_copy(
        update={"knowledge_at": NOW + timedelta(minutes=5)}
    )
    assert (
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=boundary_decisions,
            now=NOW,
        ).knowledge_at
        == boundary_decisions.knowledge_at
    )
    future_decisions = _decisions(review_export).model_copy(
        update={"knowledge_at": NOW + timedelta(minutes=5, microseconds=1)}
    )
    with pytest.raises(ValueError, match="future clock skew"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=future_decisions,
            now=NOW,
        )

    review_payload = review_export.review.model_dump(mode="json", exclude={"content_sha256"})
    candidate_payload = review_payload["items"][0]["evidence_candidates"][0]
    candidate_payload["locator_coordinates"] = KpiEvidenceLocatorCoordinates(
        page_number=999
    ).model_dump(mode="json")
    tampered_review = KpiSemanticReviewBatch.model_validate(
        {**review_payload, "content_sha256": payload_sha256(review_payload)}
    )
    tampered_export = _export(tampered_review)
    with pytest.raises(ValueError, match="coordinates changed"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=tampered_export,
            decisions=_decisions(tampered_export),
            now=NOW,
        )


def test_rejects_caller_supplied_locator_that_disagrees_with_evidence(tmp_path: Path) -> None:
    conn = _database()
    review_batch = _review(conn, tmp_path)
    raw = _decision(review_batch).model_dump(mode="json")
    raw["locator"] = {"pdf_page": 999, "verbatim_snippet": SOURCE_TEXT}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReviewedKpiSemanticDecision.model_validate(raw)


def test_executor_rejects_manifest_locator_that_disagrees_with_evidence(tmp_path: Path) -> None:
    conn = _database()
    review_export = _export(_review(conn, tmp_path))
    entry = build_kpi_semantic_refresh_manifest(
        conn,
        repo_root=tmp_path,
        review_export=review_export,
        decisions=_decisions(review_export),
        now=NOW,
    ).entries[0]
    bad_locator = FactLocator(
        kind=LocatorKind.PDF_SLIDE,
        pdf_page=999,
        verbatim_snippet=SOURCE_TEXT,
    )
    bad_locator_json = bad_locator.to_json()
    assert bad_locator_json is not None
    bad_entry = entry.model_copy(
        update={
            "locator": bad_locator,
            "fact_locator_sha256": hashlib.sha256(bad_locator_json.encode("utf-8")).hexdigest(),
        }
    )

    with pytest.raises(RepairBlockedError, match="fact_locator_evidence_mismatch"):
        validate_refresh_entry(conn, bad_entry, {1})


def test_rejects_review_hash_mismatch(tmp_path: Path) -> None:
    conn = _database()
    review_batch = _review(conn, tmp_path)
    review_export = _export(review_batch)
    nested_mismatch = _decisions(review_export).model_copy(update={"review_batch_sha256": "f" * 64})

    with pytest.raises(ValueError, match="nested review hash"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=nested_mismatch,
            now=NOW,
        )

    export_mismatch = _decisions(review_export).model_copy(
        update={"review_export_sha256": "f" * 64}
    )
    with pytest.raises(ValueError, match="review export hash"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=export_mismatch,
            now=NOW,
        )


def test_nonterminal_review_partition_can_authorize_its_complete_item(tmp_path: Path) -> None:
    conn = _database()
    conn.execute(
        "INSERT INTO kpi_facts VALUES (11,'NU','2024-09-30','Q3',1,'100000000','count',"
        "NULL,2,NULL,'Total customers reached 100 million.',NULL)"
    )
    review_batch = _review(conn, tmp_path, limit=1)
    assert review_batch.truncated is True
    review_export = _export(review_batch)

    manifest = build_kpi_semantic_refresh_manifest(
        conn,
        repo_root=tmp_path,
        review_export=review_export,
        decisions=_decisions(review_export),
        now=NOW,
    )

    assert review_export.next_after_fact_id == review_batch.items[-1].fact_id
    assert [entry.old_fact_id for entry in manifest.entries] == [10]


def test_rejects_missing_verbatim_semantic_evidence(tmp_path: Path) -> None:
    conn = _database()
    review_batch = _review(conn, tmp_path)
    review_export = _export(review_batch)
    semantic_evidence = _decision(review_batch).semantic_evidence.model_copy(
        update={"accounting_basis_quote": "not present in source"}
    )

    with pytest.raises(ValueError, match="semantic evidence quote"):
        build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=tmp_path,
            review_export=review_export,
            decisions=_decisions(review_export, semantic_evidence=semantic_evidence),
            now=NOW,
        )


def test_rejects_incomplete_or_truncated_candidate_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incomplete_conn = _database()
    incomplete_conn.execute(
        "UPDATE evidence_nodes SET text=? WHERE node_id='page-7'",
        (SOURCE_TEXT + " 114.2",),
    )
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT", 1)
    incomplete_review = _review(incomplete_conn, tmp_path)
    assert incomplete_review.items[0].evidence_search_incomplete is True
    incomplete_export = _export(incomplete_review)
    with pytest.raises(ValueError, match=r"incomplete|review state"):
        build_kpi_semantic_refresh_manifest(
            incomplete_conn,
            repo_root=tmp_path,
            review_export=incomplete_export,
            decisions=_decisions(incomplete_export),
            now=NOW,
        )

    monkeypatch.setattr(review_module, "MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT", 4_096)
    truncated_conn = _database()
    truncated_conn.execute(
        "UPDATE evidence_nodes SET text=? WHERE node_id='page-7'",
        (SOURCE_TEXT + " 114.2" * 9,),
    )
    truncated_review = _review(truncated_conn, tmp_path)
    assert truncated_review.items[0].evidence_candidates_truncated is True
    truncated_export = _export(truncated_review)
    with pytest.raises(ValueError, match="truncated"):
        build_kpi_semantic_refresh_manifest(
            truncated_conn,
            repo_root=tmp_path,
            review_export=truncated_export,
            decisions=_decisions(truncated_export),
            now=NOW,
        )


def test_rejects_fact_head_and_issuer_drift(tmp_path: Path) -> None:
    head_conn = _database()
    head_review = _review(head_conn, tmp_path)
    head_export = _export(head_review)
    head_conn.execute(
        "INSERT INTO kpi_facts VALUES (12,'NU','2024-12-31','Q4',1,'114200000','count',"
        "NULL,2,10,'Total customers reached 114.2 million.',NULL)"
    )
    with pytest.raises(ValueError, match="fact head"):
        build_kpi_semantic_refresh_manifest(
            head_conn,
            repo_root=tmp_path,
            review_export=head_export,
            decisions=_decisions(head_export),
            now=NOW,
        )

    context_conn = _database()
    context_review = _review(context_conn, tmp_path)
    context_export = _export(context_review)
    context_conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts SELECT 6,kpi_fact_id,2,5,"
        "metric_name_as_reported,reported_period_end,period_role,publication_lane,"
        "accounting_basis,consolidation_scope,dimensions_json,unit_scale,source_row_label,"
        "source_column_header,source_value_text,status,'new_review_state',reviewed_by,"
        "'2026-08-30T11:00:00Z' FROM kpi_fact_semantic_contexts WHERE id=5"
    )
    with pytest.raises(ValueError, match="semantic context head"):
        build_kpi_semantic_refresh_manifest(
            context_conn,
            repo_root=tmp_path,
            review_export=context_export,
            decisions=_decisions(context_export),
            now=NOW,
        )

    issuer_conn = _database()
    issuer_review = _review(issuer_conn, tmp_path)
    issuer_export = _export(issuer_review)
    issuer_conn.execute("UPDATE documents SET ticker='NOW' WHERE id=2")
    with pytest.raises(ValueError, match="issuer"):
        build_kpi_semantic_refresh_manifest(
            issuer_conn,
            repo_root=tmp_path,
            review_export=issuer_export,
            decisions=_decisions(issuer_export),
            now=NOW,
        )


def test_rejects_unsupported_decision_action_and_review_state(tmp_path: Path) -> None:
    conn = _database()
    review_batch = _review(conn, tmp_path)
    raw = _decision(review_batch).model_dump(mode="json")
    raw["action"] = "guess_fix"
    with pytest.raises(ValidationError):
        ReviewedKpiSemanticDecision.model_validate(raw)

    unsupported_conn = _database()
    unsupported_conn.execute("DELETE FROM evidence_nodes")
    unsupported_review = _review(unsupported_conn, tmp_path)
    assert unsupported_review.items[0].state.value != "source_review_required"
    unsupported_export = _export(unsupported_review)
    unsupported_decision = _decision(review_batch)
    unsupported_decisions = _decisions(unsupported_export).model_copy(
        update={
            "decisions": (unsupported_decision,),
        }
    )
    with pytest.raises(ValueError, match="source_review_required"):
        build_kpi_semantic_refresh_manifest(
            unsupported_conn,
            repo_root=tmp_path,
            review_export=unsupported_export,
            decisions=unsupported_decisions,
            now=NOW,
        )
