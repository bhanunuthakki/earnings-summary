from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest
from pydantic import ValidationError

from execution.apply_kpi_semantic_dispositions import (
    DispositionBlockedError,
    execute_disposition_transaction,
    recover_committed_disposition,
)
from operations.review_bundle import OperationsReviewBundle, ReviewIdentity
from pipeline.kpi_legacy_disposition_capture import (
    captured_legacy_quarantine_matches,
    read_legacy_kpi_disposition_capture,
)
from pipeline.kpi_semantic_dispositions import (
    KpiSemanticDispositionManifest,
    LegacyKpiQuarantineRequest,
    apply_kpi_semantic_disposition_manifest,
    prepare_kpi_semantic_disposition_manifest,
)
from pipeline.kpi_semantic_review import build_quarantined_kpi_correction_review
from pipeline.kpi_semantics import current_kpi_semantic_context, persist_kpi_semantic_context
from schema_compat import expected_head

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class _DispositionArguments(TypedDict):
    db_path: Path
    manifest: KpiSemanticDispositionManifest
    manifest_sha: str
    logical_key_sha: str
    executor_code_sha: str
    review_bundle: OperationsReviewBundle


@pytest.fixture
def legacy_db(migrated_db: Callable[..., Path], tmp_path: Path):
    conn = sqlite3.connect(migrated_db(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO tenants(id,created_at) VALUES ('owner',?)", (NOW.isoformat(),))
    conn.execute(
        "INSERT INTO tracked_companies(ticker,name,list_type,user_id) "
        "VALUES ('BKNG','Booking','portfolio','owner')"
    )
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetched_at,fetch_status,raw_bytes_size,source_quality_tier) "
        "VALUES (10,'BKNG','llm_extracted','llm_summary','2025-12-31',"
        "'.tmp/synthetic-summary.txt',?,?,'ok',1,'fmp_normalized')",
        ("a" * 64, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO kpi_definitions(id,ticker,name,unit,primary_source) "
        "VALUES (982,'BKNG','Marketing / gross profit','percent','ir_doc')"
    )
    # A pre-cutover legacy head has no immutable observation. Keep that condition real.
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trg_kpi_facts_observation_insert'"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
    conn.execute(
        "INSERT INTO kpi_facts(id,ticker,period_end,fiscal_period_type,kpi_definition_id,"
        "value,unit,source_doc_id,confidence,extracted_by,source_excerpt) "
        "VALUES (56892,'BKNG','2025-12-31','Q4',982,28,'percent',10,0.9,'legacy',"
        "'TTM marketing / revenue approximately 28%')"
    )
    conn.execute(str(trigger))
    conn.commit()
    yield conn
    conn.close()


def _prepare(conn: sqlite3.Connection, root: Path) -> KpiSemanticDispositionManifest:
    return prepare_kpi_semantic_disposition_manifest(
        conn,
        repo_root=root,
        user_id="owner",
        reviewer="owner",
        logical_idempotency_key="booking:wrong-denominator:v1",
        expected_schema_revision=expected_head(),
        review_bundle_sha256="b" * 64,
        backup_restore_evidence_id="c" * 64,
        knowledge_at=NOW,
        legacy_fact_requests=(
            LegacyKpiQuarantineRequest(
                fact_id=56892, reason_code="ttm_marketing_revenue_not_quarterly_marketing_gp"
            ),
        ),
    )


def _counts(conn: sqlite3.Connection) -> tuple[int, ...]:
    return tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "kpi_facts",
            "reported_observations",
            "fact_observation_revisions",
            "kpi_fact_semantic_contexts",
            "kpi_legacy_disposition_captures",
        )
    )


def test_quarantine_seals_raw_head_without_admission_and_replays(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    original = read_legacy_kpi_disposition_capture(conn, fact_id=56892)
    before = _counts(conn)
    manifest = _prepare(conn, tmp_path)
    assert _counts(conn) == before
    assert manifest.schema_version == "kpi_semantic_dispositions.v2"
    assert manifest.report_reference_dispositions == ()
    result = apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    assert result.inserted_context_rows == 1
    assert _counts(conn) == (*before[:3], before[3] + 1, before[4] + 1)
    assert read_legacy_kpi_disposition_capture(conn, fact_id=56892) == original
    assert (
        conn.execute("SELECT 1 FROM v_kpi_facts_resolved_current WHERE id=56892").fetchone() is None
    )
    assert captured_legacy_quarantine_matches(conn, fact_id=56892, user_id="owner")
    assert not captured_legacy_quarantine_matches(conn, fact_id=56892, user_id="other")
    replay = apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    assert replay.replayed_context_rows == 1
    assert _counts(conn) == (*before[:3], before[3] + 1, before[4] + 1)
    # Source correction remains inspectable even when its independent source is still missing.
    review = build_quarantined_kpi_correction_review(
        conn,
        repo_root=tmp_path,
        user_id="owner",
        fact_id=56892,
        source_value_text="30.1",
        observed_at=NOW,
    )
    assert review.items[0].context_status == "quarantined"
    assert review.items[0].value == "28"
    assert _counts(conn) == (*before[:3], before[3] + 1, before[4] + 1)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE kpi_facts SET source_excerpt='rewritten' WHERE id=56892",
        "UPDATE documents SET sha256='" + "d" * 64 + "' WHERE id=10",
        "UPDATE kpi_definitions SET name='different population' WHERE id=982",
    ],
)
def test_raw_or_source_drift_blocks_before_writing(
    legacy_db: sqlite3.Connection, tmp_path: Path, mutation: str
) -> None:
    manifest = _prepare(legacy_db, tmp_path)
    before = _counts(legacy_db)
    # Simulate drift from an older external writer; production still keeps its source guard.
    trigger = legacy_db.execute(
        "SELECT sql FROM sqlite_master WHERE name='trg_kpi_facts_observation_update'"
    ).fetchone()[0]
    legacy_db.execute("DROP TRIGGER trg_kpi_facts_observation_update")
    legacy_db.execute(mutation)
    legacy_db.execute(str(trigger))
    with pytest.raises(ValueError, match="projection changed"):
        apply_kpi_semantic_disposition_manifest(legacy_db, repo_root=tmp_path, manifest=manifest)
    assert _counts(legacy_db) == before


def test_capture_is_append_only_and_context_drift_blocks_replay_and_correction(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    manifest = _prepare(conn, tmp_path)
    apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    for statement in (
        "DELETE FROM kpi_legacy_disposition_captures",
        "UPDATE kpi_legacy_disposition_captures SET user_id='other'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    current = current_kpi_semantic_context(conn, kpi_fact_id=56892)
    assert current is not None
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=56892,
        context=current.context.model_copy(update={"reason_code": "a_later_review"}),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=None,
    )
    assert not captured_legacy_quarantine_matches(conn, fact_id=56892, user_id="owner")
    with pytest.raises(ValueError, match="semantic head changed"):
        apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    with pytest.raises(ValueError, match="already has a semantic context"):
        build_quarantined_kpi_correction_review(
            conn,
            repo_root=tmp_path,
            user_id="owner",
            fact_id=56892,
            source_value_text="30.1",
            observed_at=NOW,
        )


def test_tampered_projection_and_widened_manifest_fail_validation(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    payload = _prepare(legacy_db, tmp_path).model_dump(mode="json")
    payload["fact_dispositions"][0]["legacy_capture"]["payload"]["fact"]["value"] = 30
    with pytest.raises(ValidationError, match="commitment mismatch"):
        KpiSemanticDispositionManifest.model_validate(payload)
    payload = _prepare(legacy_db, tmp_path).model_dump(mode="json")
    payload["schema_version"] = "kpi_semantic_dispositions.v1"
    with pytest.raises(ValidationError, match="require manifest v2"):
        KpiSemanticDispositionManifest.model_validate(payload)
    with pytest.raises(ValueError, match="distinct explicit"):
        prepare_kpi_semantic_disposition_manifest(
            legacy_db,
            repo_root=tmp_path,
            user_id="owner",
            reviewer="owner",
            logical_idempotency_key="empty",
            expected_schema_revision=expected_head(),
            review_bundle_sha256="b" * 64,
            backup_restore_evidence_id="c" * 64,
            legacy_fact_requests=(),
        )


def test_failed_disposition_transaction_rolls_back_capture(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    manifest = _prepare(conn, tmp_path)
    before = _counts(conn)
    conn.execute("BEGIN IMMEDIATE")
    apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    conn.rollback()
    assert _counts(conn) == before
    assert not captured_legacy_quarantine_matches(conn, fact_id=56892, user_id="owner")


def test_successor_and_owner_drift_block_exact_legacy_manifest(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    manifest = _prepare(conn, tmp_path)
    conn.execute(
        "UPDATE tracked_companies SET archived_at=? WHERE ticker='BKNG'", (NOW.isoformat(),)
    )
    with pytest.raises(ValueError, match="owner portfolio scope"):
        apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    conn.rollback()
    # The new head can be another unresolved legacy row. It still invalidates the CAS.
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='trg_kpi_facts_observation_insert'"
    ).fetchone()[0]
    conn.execute("DROP TRIGGER trg_kpi_facts_observation_insert")
    conn.execute(
        "INSERT INTO kpi_facts(ticker,period_end,fiscal_period_type,kpi_definition_id,"
        "value,unit,source_doc_id,confidence,extracted_by,supersedes_id) "
        "VALUES ('BKNG','2025-12-31','Q4',982,30,'percent',10,0.9,'legacy',56892)"
    )
    conn.execute(str(trigger))
    with pytest.raises(ValueError, match="fact-chain head"):
        apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    assert conn.execute("SELECT COUNT(*) FROM kpi_legacy_disposition_captures").fetchone()[0] == 0
    assert current_kpi_semantic_context(conn, kpi_fact_id=56892) is None


def test_uncaptured_quarantine_cannot_become_a_correction_predecessor(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    manifest = _prepare(conn, tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    apply_kpi_semantic_disposition_manifest(conn, repo_root=tmp_path, manifest=manifest)
    current = current_kpi_semantic_context(conn, kpi_fact_id=56892)
    assert current is not None
    conn.rollback()
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=56892,
        context=current.context,
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=None,
    )
    assert not captured_legacy_quarantine_matches(conn, fact_id=56892, user_id="owner")
    with pytest.raises(ValueError, match="already has a semantic context"):
        _prepare(conn, tmp_path)
    with pytest.raises(ValueError, match="already has a semantic context"):
        build_quarantined_kpi_correction_review(
            conn,
            repo_root=tmp_path,
            user_id="owner",
            fact_id=56892,
            source_value_text="30.1",
            observed_at=NOW,
        )


def test_real_commit_recovery_checks_current_capture_and_source_identity(
    legacy_db: sqlite3.Connection, tmp_path: Path
) -> None:
    conn = legacy_db
    manifest = _prepare(conn, tmp_path)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    # This internal transaction test needs only the real identity. Native authority
    # envelopes are verified by the separate CLI authority tests.
    review = OperationsReviewBundle.model_construct(
        identity=ReviewIdentity.model_construct(
            database_instance_sha256=manifest.expected_database_instance_sha256
        )
    )
    arguments: _DispositionArguments = {
        "db_path": db_path,
        "manifest": manifest,
        "manifest_sha": manifest.content_sha256(),
        "logical_key_sha": hashlib.sha256(manifest.logical_idempotency_key.encode()).hexdigest(),
        "executor_code_sha": "d" * 64,
        "review_bundle": review,
    }
    execute_disposition_transaction(**arguments, repo_root=tmp_path, apply=True)
    before = _counts(conn)
    replay = recover_committed_disposition(**arguments)
    assert replay is not None and replay.replayed_context_rows == 1
    assert _counts(conn) == before
    conn.execute("UPDATE documents SET sha256=? WHERE id=10", ("e" * 64,))
    conn.commit()
    with pytest.raises(DispositionBlockedError, match="committed_legacy_disposition_head_changed"):
        recover_committed_disposition(**arguments)
    assert _counts(conn) == before


def test_v1_absent_capture_has_identical_serialization() -> None:
    from pipeline.kpi_semantic_dispositions import KpiFactQuarantineDisposition

    payload = {
        "fact_id": 1,
        "expected_fact_head_id": 1,
        "expected_context_head_id": None,
        "expected_context_revision": 0,
        "ticker": "BKNG",
        "kpi_definition_id": 1,
        "stored_definition_name": "Room nights",
        "stored_period_end": "2025-12-31",
        "reason_code": "missing_source",
    }
    assert (
        json.loads(KpiFactQuarantineDisposition.model_validate(payload).model_dump_json())
        == payload
    )
