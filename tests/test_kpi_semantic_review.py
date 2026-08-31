from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import operations.kpi_semantic_review_export as export_module
import pipeline.kpi_semantic_review as review_module
from execution.prepare_kpi_semantic_review import (
    KpiSemanticReviewSummary,
    build_parser,
)
from execution.prepare_kpi_semantic_review import (
    main as prepare_review_main,
)
from operations.kpi_semantic_review_export import (
    KpiSemanticReviewExport,
    KpiSemanticReviewExportError,
    load_current_kpi_semantic_review_export,
    publish_kpi_semantic_review_exports,
    seal_kpi_semantic_review_export,
)
from pipeline.kpi_semantic_review import (
    MAX_EVIDENCE_CANDIDATES_PER_FACT,
    MAX_KPI_SEMANTIC_REVIEW_ITEMS,
    OPERATIONS_GOVERNANCE_DISPOSITION,
    OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT,
    KpiSemanticReviewBatch,
    KpiSemanticReviewState,
    build_kpi_semantic_review_batch,
)
from provenance.evidence_ledger import EvidenceLocator
from provenance.fulltext_extractor_identity import resolve_fulltext_extractor_identity

NOW = datetime(2026, 8, 30, tzinfo=UTC)
SHA = "a" * 64
EVIDENCE_LOCATOR = EvidenceLocator(source_ref="C:/sources/document.pdf", page_number=9)
LOCATOR_JSON = EVIDENCE_LOCATOR.canonical_json
LOCATOR_SHA = EVIDENCE_LOCATOR.canonical_sha256


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracked_companies(
            ticker TEXT, list_type TEXT, user_id TEXT, archived_at TEXT
        );
        CREATE TABLE kpi_definitions(id INTEGER PRIMARY KEY,ticker TEXT,name TEXT);
        CREATE TABLE kpi_facts(
            id INTEGER PRIMARY KEY,ticker TEXT,period_end TEXT,fiscal_period_type TEXT,
            kpi_definition_id INTEGER,value TEXT,unit TEXT,source_doc_id INTEGER,
            supersedes_id INTEGER
        );
        CREATE TABLE documents(
            id INTEGER PRIMARY KEY,ticker TEXT,source_type TEXT,doc_type TEXT,
            period_end TEXT,sha256 TEXT,fetched_at TEXT,parent_document_id INTEGER,
            file_path TEXT
        );
        CREATE TABLE kpi_fact_semantic_contexts(
            id INTEGER PRIMARY KEY,kpi_fact_id INTEGER,status TEXT,revision INTEGER,
            supersedes_context_id INTEGER
        );
        CREATE TABLE evidence_document_versions(
            document_version_id TEXT,legacy_document_id INTEGER,version_sequence INTEGER,
            blob_sha256 TEXT,ticker TEXT
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
        CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts;
        """
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','portfolio','owner',NULL)")
    conn.executemany(
        "INSERT INTO kpi_definitions VALUES (?,?,?)",
        [(1, "NU", "Total customers"), (2, "NU", "Deposits")],
    )
    return conn


def _document(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    source_type: str,
    doc_type: str,
    parent_document_id: int | None = None,
    period_end: str | None = "2024-12-31",
) -> None:
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)",
        (
            document_id,
            "NU",
            source_type,
            doc_type,
            period_end,
            SHA,
            "2026-05-03T20:55:53Z",
            parent_document_id,
            "C:/sources/document.pdf",
        ),
    )


@pytest.mark.parametrize(
    ("doc_type", "expected_state"),
    [
        ("ir_historical_spreadsheet", KpiSemanticReviewState.NEEDS_LEDGER_CAPTURE),
        ("ir_presentation", KpiSemanticReviewState.SOURCE_IDENTITY_MISSING),
    ],
)
def test_source_period_is_optional_only_for_multi_period_documents(
    tmp_path: Path,
    doc_type: str,
    expected_state: KpiSemanticReviewState,
) -> None:
    conn = _database()
    _document(
        conn,
        document_id=10,
        source_type="ir_doc",
        doc_type=doc_type,
        period_end=None,
    )
    _fact(conn, fact_id=1, definition_id=1, document_id=10)

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.items[0].state is expected_state


def _fact(conn: sqlite3.Connection, *, fact_id: int, definition_id: int, document_id: int) -> None:
    conn.execute(
        "INSERT INTO kpi_facts VALUES (?,?,?,?,?,?,?,?,NULL)",
        (fact_id, "NU", "2024-12-31", "Q4", definition_id, "114.2", "count", document_id),
    )


def _evidence(conn: sqlite3.Connection, *, document_id: int, text: str) -> None:
    identity = resolve_fulltext_extractor_identity("C:/sources/document.pdf", None)
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES ('doc-v1',?,1,?,'NU')",
        (document_id, SHA),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?)",
        (
            "run-1",
            "doc-v1",
            SHA,
            identity.name,
            identity.config_sha256,
            identity.code_version,
            "succeeded",
        ),
    )
    conn.executemany(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("node-document", "run-1", "document", "", LOCATOR_JSON, LOCATOR_SHA, None),
            ("node-1", "run-1", "pdf_page", text, LOCATOR_JSON, LOCATOR_SHA, None),
        ],
    )
    conn.execute(
        "INSERT INTO v_legacy_document_evidence_bindings_current VALUES (?,?,?,?)",
        (document_id, "doc-v1", "node-document", SHA),
    )


def test_synthetic_fact_resolves_to_parent_and_requests_ledger_capture(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _document(
        conn,
        document_id=20,
        source_type="llm_extracted",
        doc_type="llm_summary",
        parent_document_id=10,
    )
    _fact(conn, fact_id=1, definition_id=1, document_id=20)

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.total_items == 1
    item = batch.items[0]
    assert item.legacy_source_doc_id == 20
    assert item.source_doc_id == 10
    assert item.state is KpiSemanticReviewState.NEEDS_LEDGER_CAPTURE


def test_exact_evidence_value_is_a_review_candidate_not_auto_admission(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    source_text = "Customers (MM) 114.2. Active customers (MM) 94.9."
    _evidence(conn, document_id=10, text=source_text)

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    item = batch.items[0]
    assert item.state is KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED
    assert item.evidence_candidates[0].source_value_text == "114.2"
    assert item.evidence_candidates[0].document_version_id == "doc-v1"
    assert item.evidence_candidates[0].extraction_run_id == "run-1"
    assert "Active customers" in item.evidence_candidates[0].excerpt
    candidate = item.evidence_candidates[0]
    assert candidate.excerpt == source_text[candidate.excerpt_start : candidate.excerpt_end]
    assert candidate.evidence_locator == EVIDENCE_LOCATOR
    assert candidate.locator_sha256 == candidate.evidence_locator.canonical_sha256
    assert item.context_status is None


def test_evidence_locator_payload_and_hash_must_match(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    _evidence(conn, document_id=10, text="Customers (MM) 114.2.")
    mismatched = EvidenceLocator(source_ref="C:/sources/document.pdf", page_number=10)
    conn.execute(
        "UPDATE evidence_nodes SET locator_json=? WHERE node_id='node-1'",
        (mismatched.canonical_json,),
    )

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    item = batch.items[0]
    assert item.state is KpiSemanticReviewState.EVIDENCE_SEARCH_INCOMPLETE
    assert item.evidence_search_reason_codes == ("evidence_node_locator_invalid",)
    assert item.evidence_candidates == ()


def test_integer_ending_in_zero_is_not_shortened_for_evidence_matching(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    conn.execute("INSERT INTO kpi_facts VALUES (1,'NU','2024-12-31','Q4',1,'110','count',10,NULL)")
    _evidence(conn, document_id=10, text="Customers (MM) 11.")

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.items[0].state is KpiSemanticReviewState.EVIDENCE_NO_NUMERIC_MATCH


def test_nonreviewable_source_is_explicit_and_admitted_fact_is_omitted(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=30, source_type="fmp", doc_type="fmp_cashflow")
    _fact(conn, fact_id=1, definition_id=1, document_id=30)
    _fact(conn, fact_id=2, definition_id=2, document_id=30)
    conn.execute("INSERT INTO kpi_fact_semantic_contexts VALUES (1,2,'admitted',1,NULL)")

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert [item.fact_id for item in batch.items] == [1]
    assert batch.items[0].state is KpiSemanticReviewState.SOURCE_NOT_REVIEWABLE


def test_source_issuer_mismatch_never_becomes_review_ready(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    conn.execute("UPDATE documents SET ticker='NOW' WHERE id=10")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    _evidence(conn, document_id=10, text="Customers (MM) 114.2.")

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.items[0].state is KpiSemanticReviewState.SOURCE_ISSUER_MISMATCH
    assert batch.items[0].evidence_candidates == ()


def test_definition_fact_and_synthetic_child_issuer_mismatches_fail_closed(
    tmp_path: Path,
) -> None:
    definition_mismatch = _database()
    _document(
        definition_mismatch,
        document_id=10,
        source_type="ir_doc",
        doc_type="ir_presentation",
    )
    definition_mismatch.execute("UPDATE documents SET ticker='NOW' WHERE id=10")
    definition_mismatch.execute(
        "INSERT INTO kpi_facts VALUES (1,'NOW','2024-12-31','Q4',1,'114.2','count',10,NULL)"
    )

    first = build_kpi_semantic_review_batch(
        definition_mismatch, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert first.items[0].state is KpiSemanticReviewState.SOURCE_ISSUER_MISMATCH
    assert first.items[0].evidence_candidates == ()

    synthetic_mismatch = _database()
    _document(
        synthetic_mismatch,
        document_id=10,
        source_type="ir_doc",
        doc_type="ir_presentation",
    )
    _document(
        synthetic_mismatch,
        document_id=20,
        source_type="llm_extracted",
        doc_type="llm_summary",
        parent_document_id=10,
    )
    synthetic_mismatch.execute("UPDATE documents SET ticker='NOW' WHERE id=20")
    _fact(synthetic_mismatch, fact_id=1, definition_id=1, document_id=20)

    second = build_kpi_semantic_review_batch(
        synthetic_mismatch, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert second.items[0].state is KpiSemanticReviewState.SOURCE_ISSUER_MISMATCH
    assert second.items[0].evidence_candidates == ()


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            f"UPDATE evidence_document_versions SET blob_sha256='{'b' * 64}'",
            "evidence_document_version_identity_mismatch",
        ),
        (
            "UPDATE evidence_document_versions SET ticker='NOW'",
            "evidence_document_version_identity_mismatch",
        ),
        (
            "UPDATE evidence_extraction_runs SET extractor_name='arbitrary-extractor'",
            "promoted_fulltext_extraction_missing",
        ),
        (
            "UPDATE evidence_extraction_runs SET extractor_code_version='stale-version'",
            "promoted_fulltext_extraction_missing",
        ),
        (
            "UPDATE evidence_nodes SET node_kind='section' WHERE node_id='node-document'",
            "current_document_binding_invalid",
        ),
        (
            "UPDATE v_legacy_document_evidence_bindings_current "
            "SET document_version_id='other-version'",
            "current_document_binding_invalid",
        ),
    ],
)
def test_untrusted_evidence_binding_never_becomes_review_ready(
    tmp_path: Path, mutation: str, expected_reason: str
) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    _evidence(conn, document_id=10, text="Customers (MM) 114.2.")
    conn.execute(mutation)

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.items[0].state is KpiSemanticReviewState.EVIDENCE_BINDING_INVALID
    assert batch.items[0].state_reason_code == expected_reason
    assert batch.items[0].evidence_candidates == ()


@pytest.mark.parametrize("source_text", ["1,110", "-110", "1100", "110.5"])
def test_numeric_matching_rejects_embedded_or_signed_values(
    tmp_path: Path, source_text: str
) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    conn.execute("INSERT INTO kpi_facts VALUES (1,'NU','2024-12-31','Q4',1,'110','count',10,NULL)")
    _evidence(conn, document_id=10, text=f"Customers (MM) {source_text}.")

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert batch.items[0].state is KpiSemanticReviewState.EVIDENCE_NO_NUMERIC_MATCH


def test_repeated_numeric_matches_report_overflow_and_preserve_verbatim_offsets(
    tmp_path: Path,
) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    conn.execute("INSERT INTO kpi_facts VALUES (1,'NU','2024-12-31','Q4',1,'110','count',10,NULL)")
    source_text = "Customers (MM)\n  " + "110\t" * 10
    _evidence(conn, document_id=10, text=source_text)

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    item = batch.items[0]
    assert item.evidence_candidate_total == 10
    assert len(item.evidence_candidates) == MAX_EVIDENCE_CANDIDATES_PER_FACT
    assert item.evidence_candidates_truncated is True
    for candidate in item.evidence_candidates:
        assert (
            candidate.source_value_text == source_text[candidate.match_start : candidate.match_end]
        )
        assert candidate.excerpt == source_text[candidate.excerpt_start : candidate.excerpt_end]
        assert candidate.excerpt in source_text


def test_evidence_node_and_text_search_budgets_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_limited = _database()
    _document(node_limited, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(node_limited, fact_id=1, definition_id=1, document_id=10)
    _evidence(node_limited, document_id=10, text="No metric on the first node.")
    node_limited.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,NULL)",
        (
            "node-2",
            "run-1",
            "pdf_page",
            "Customers (MM) 114.2.",
            LOCATOR_JSON,
            LOCATOR_SHA,
        ),
    )
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_NODES_PER_DOCUMENT", 1)

    node_batch = build_kpi_semantic_review_batch(
        node_limited, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert node_batch.items[0].state is KpiSemanticReviewState.EVIDENCE_SEARCH_INCOMPLETE
    assert node_batch.items[0].evidence_search_reason_codes == (
        "evidence_node_search_budget_exceeded",
    )

    text_limited = _database()
    _document(text_limited, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(text_limited, fact_id=1, definition_id=1, document_id=10)
    _evidence(text_limited, document_id=10, text="Long prefix before customers 114.2.")
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_NODES_PER_DOCUMENT", 2_048)
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_TEXT_CHARS_PER_NODE", 10)
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_TEXT_CHARS_PER_DOCUMENT", 10)

    text_batch = build_kpi_semantic_review_batch(
        text_limited, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert text_batch.items[0].state is KpiSemanticReviewState.EVIDENCE_SEARCH_INCOMPLETE
    assert text_batch.items[0].evidence_search_reason_codes == (
        "evidence_text_search_budget_exceeded",
    )


def test_match_budget_and_document_snapshot_reuse_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    _fact(conn, fact_id=2, definition_id=2, document_id=10)
    _evidence(conn, document_id=10, text="114.2 then 114.2 again")
    monkeypatch.setattr(review_module, "MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT", 1)
    node_searches: list[str] = []
    conn.set_trace_callback(
        lambda sql: node_searches.append(sql) if "substr(node.text" in sql else None
    )

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )
    conn.set_trace_callback(None)

    assert len(node_searches) == 1
    assert [item.state for item in batch.items] == [
        KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED,
        KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED,
    ]
    assert all(item.evidence_search_incomplete for item in batch.items)
    assert all(
        item.evidence_search_reason_codes == ("evidence_match_search_budget_exceeded",)
        for item in batch.items
    )


def test_exact_match_budget_is_complete_and_overflow_consumes_only_one_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_finditer = re.finditer
    consumed = 0

    def counted_finditer(pattern: str, string: str, flags: int = 0) -> Iterator[re.Match[str]]:
        nonlocal consumed
        for match in original_finditer(pattern, string, flags):
            consumed += 1
            yield match

    monkeypatch.setattr(review_module, "MAX_EVIDENCE_MATCHES_SCANNED_PER_FACT", 3)
    monkeypatch.setattr(re, "finditer", counted_finditer)

    exact = _database()
    _document(exact, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(exact, fact_id=1, definition_id=1, document_id=10)
    _evidence(exact, document_id=10, text="114.2 114.2 114.2")

    exact_batch = build_kpi_semantic_review_batch(
        exact, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert consumed == 3
    assert exact_batch.items[0].evidence_candidate_total == 3
    assert exact_batch.items[0].evidence_search_incomplete is False
    assert exact_batch.items[0].evidence_search_reason_codes == ()

    consumed = 0
    overflow = _database()
    _document(overflow, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(overflow, fact_id=1, definition_id=1, document_id=10)
    _evidence(overflow, document_id=10, text=" ".join(["114.2"] * 10_000))

    overflow_batch = build_kpi_semantic_review_batch(
        overflow, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert consumed == 4
    assert overflow_batch.items[0].evidence_candidate_total == 4
    assert overflow_batch.items[0].evidence_search_incomplete is True
    assert overflow_batch.items[0].evidence_search_reason_codes == (
        "evidence_match_search_budget_exceeded",
    )


def test_unknown_owner_and_foreign_ticker_fail_closed(tmp_path: Path) -> None:
    conn = _database()

    with pytest.raises(ValueError, match="owner portfolio scope is absent"):
        build_kpi_semantic_review_batch(conn, repo_root=tmp_path, user_id="unknown")
    with pytest.raises(ValueError, match="outside the owner portfolio"):
        build_kpi_semantic_review_batch(conn, repo_root=tmp_path, user_id="owner", ticker="NOW")


def test_pre_cutover_fact_schema_fails_closed(tmp_path: Path) -> None:
    conn = _database()
    conn.execute("DROP VIEW v_kpi_facts_resolved_current")

    with pytest.raises(ValueError, match="resolved current-fact view"):
        build_kpi_semantic_review_batch(conn, repo_root=tmp_path, user_id="owner")


def test_review_timestamp_requires_awareness_and_normalizes_to_utc(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)

    with pytest.raises(ValueError, match="timezone-aware"):
        build_kpi_semantic_review_batch(
            conn,
            repo_root=tmp_path,
            user_id="owner",
            observed_at=datetime(2026, 8, 30),
        )

    offset_now = NOW.astimezone(timezone(-timedelta(hours=7)))
    utc_batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )
    offset_batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=offset_now
    )
    assert offset_batch.observed_at == NOW
    assert offset_batch.content_sha256 == utc_batch.content_sha256


def test_internal_review_cli_has_explicit_no_surface_change_disposition() -> None:
    assert (
        OPERATIONS_GOVERNANCE_DISPOSITION
        == "no_surface_change_internal_read_only_kpi_review_preparation"
    )
    assert OPERATIONS_GOVERNANCE_PRESERVED_CONTRACT == (
        "src/operations/registry.py:OperationsRegistry",
        "src/pipeline/operations_panel.py:visible_surface_dispositions",
    )


def test_superseded_fact_is_not_queued(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    conn.execute(
        "INSERT INTO kpi_facts VALUES (2,'NU','2024-12-31','Q4',1,'114200000','count',10,1)"
    )
    conn.execute("DROP VIEW v_kpi_facts_resolved_current")
    conn.execute("CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts WHERE id=2")

    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )

    assert [item.fact_id for item in batch.items] == [2]


def test_review_batch_rejects_tampered_content(tmp_path: Path) -> None:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    batch = build_kpi_semantic_review_batch(
        conn, repo_root=tmp_path, user_id="owner", observed_at=NOW
    )
    tampered = batch.model_dump(mode="json")
    tampered["items"][0]["value"] = "999"

    with pytest.raises(ValidationError, match="content_sha256"):
        KpiSemanticReviewBatch.model_validate(tampered)


def test_cli_reads_migrated_database_without_mutating_it(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "portfolio.db")
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO tracked_companies (user_id,ticker,name,list_type) "
        "VALUES ('owner','NU','Nu Holdings','portfolio')"
    )
    conn.commit()
    conn.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    output = tmp_path / ".tmp" / "review.json"

    assert (
        prepare_review_main(
            [
                "--db",
                str(database),
                "--repo-root",
                str(tmp_path),
                "--user-id",
                "owner",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    parsed = KpiSemanticReviewBatch.model_validate_json(output.read_text(encoding="utf-8"))
    streams = capsys.readouterr()
    summary = KpiSemanticReviewSummary.model_validate_json(streams.out)
    assert summary.content_sha256 == parsed.content_sha256
    assert "kpi_semantic_review_prepared" in streams.err
    assert parsed.total_items == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_cli_refuses_to_overwrite_source_database(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "portfolio.db")

    with pytest.raises(ValueError, match="must not overwrite"):
        prepare_review_main(
            [
                "--db",
                str(database),
                "--repo-root",
                str(tmp_path),
                "--user-id",
                "owner",
                "--output",
                str(database),
            ]
        )


def test_cli_publishes_one_atomic_current_index_without_mutating_database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = migrated_db(tmp_path / "portfolio.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (user_id,ticker,name,list_type) "
            "VALUES ('owner','NU','Nu Holdings','portfolio')"
        )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    artifact_root = tmp_path / "published"

    assert (
        prepare_review_main(
            [
                "--db",
                str(database),
                "--repo-root",
                str(tmp_path),
                "--user-id",
                "owner",
                "--artifact-root",
                str(artifact_root),
            ]
        )
        == 0
    )

    streams = capsys.readouterr()
    summary = KpiSemanticReviewSummary.model_validate_json(streams.out)
    assert summary.truncated is False
    assert summary.output == str(artifact_root / "latest.json")
    loaded, _ = load_current_kpi_semantic_review_export(
        root=artifact_root,
        ticker="NU",
        now=datetime.now(UTC),
        max_age=timedelta(minutes=20),
    )
    assert loaded.review.schema_version == "kpi_semantic_review.v2"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_cli_limit_is_bounded() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "--db",
                "input.db",
                "--user-id",
                "owner",
                "--limit",
                str(MAX_KPI_SEMANTIC_REVIEW_ITEMS + 1),
                "--output",
                "output.json",
            ]
        )


def _review_export(tmp_path: Path) -> KpiSemanticReviewExport:
    conn = _database()
    _document(conn, document_id=10, source_type="ir_doc", doc_type="ir_presentation")
    _fact(conn, fact_id=1, definition_id=1, document_id=10)
    batch = build_kpi_semantic_review_batch(
        conn,
        repo_root=tmp_path,
        user_id="owner",
        ticker="NU",
        limit=1_000,
        observed_at=NOW,
    )
    return seal_kpi_semantic_review_export(
        review=batch,
        code_instance_sha256="b" * 64,
        database_instance_sha256="c" * 64,
        schema_revision="0035",
    )


def test_semantic_review_export_is_content_addressed_and_current_portfolio_scoped(
    tmp_path: Path,
) -> None:
    export = _review_export(tmp_path)
    root = tmp_path / "exports"

    index = publish_kpi_semantic_review_exports(root=root, exports=(export,))
    loaded, payload = load_current_kpi_semantic_review_export(
        root=root,
        ticker="nu",
        now=NOW + timedelta(minutes=1),
        max_age=timedelta(minutes=20),
    )

    assert loaded == export
    assert index.artifacts[0].content_sha256 == export.content_sha256
    assert payload == (root / "artifacts" / f"{export.content_sha256}.json").read_bytes()
    with pytest.raises(KpiSemanticReviewExportError, match="outside the current portfolio"):
        load_current_kpi_semantic_review_export(
            root=root,
            ticker="NOW",
            now=NOW + timedelta(minutes=1),
            max_age=timedelta(minutes=20),
        )


def test_semantic_review_export_fails_closed_for_v1_truncation_and_oversize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = _review_export(tmp_path)
    legacy = export.model_dump(mode="json")
    legacy["review"]["schema_version"] = "kpi_semantic_review.v1"
    with pytest.raises(ValidationError, match=r"kpi_semantic_review\.v2"):
        KpiSemanticReviewExport.model_validate(legacy)

    truncated_payload = export.review.model_dump(mode="json", exclude={"content_sha256"})
    truncated_payload["truncated"] = True
    truncated_review = KpiSemanticReviewBatch.model_validate(
        {**truncated_payload, "content_sha256": export_module.payload_sha256(truncated_payload)}
    )
    with pytest.raises(ValidationError, match="truncated"):
        seal_kpi_semantic_review_export(
            review=truncated_review,
            code_instance_sha256="b" * 64,
            database_instance_sha256="c" * 64,
            schema_revision="0035",
        )

    monkeypatch.setattr(export_module, "MAX_KPI_SEMANTIC_EXPORT_BYTES", 100)
    with pytest.raises(KpiSemanticReviewExportError, match="byte bound"):
        export_module.encoded_kpi_semantic_review_export(export)


def test_semantic_review_artifact_publication_rejects_stale_index(tmp_path: Path) -> None:
    export = _review_export(tmp_path)
    root = tmp_path / "exports"
    publish_kpi_semantic_review_exports(root=root, exports=(export,))

    with pytest.raises(KpiSemanticReviewExportError, match="stale"):
        load_current_kpi_semantic_review_export(
            root=root,
            ticker="NU",
            now=NOW + timedelta(hours=1),
            max_age=timedelta(minutes=20),
        )
