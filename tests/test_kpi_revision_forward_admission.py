"""Tests for forward KPI definition-revision admission, comparability cutover, and primary series resolution."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from compute.kpi_resolver import (
    KpiRevisionSeriesExclusionReason,
    KpiRevisionSeriesStatus,
    resolve_primary_kpi_series,
)
from compute.kpi_revision_shadow_census import (
    SnapshotEvidenceState,
    audit_kpi_revision_shadow_census,
)
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.kpi_batch_admission import run_batch_kpi_admission
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiCurrencyDisposition,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionLifecycle,
    KpiDefinitionPeriodKind,
    KpiDefinitionStatus,
    KpiDefinitionTextStatus,
    KpiStockFlowBehavior,
    KpiUnitFamily,
    bind_forward_kpi_definition_revision,
    current_kpi_definition_comparability_revision,
    current_kpi_definition_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
)
from runtime.deterministic import canonical_json_ascii_default_str, sha256_text
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_EVIDENCE_AT = datetime(2024, 1, 2, tzinfo=UTC)
_KPI_DEF_ID = 900_001
_OTHER_KPI_DEF_ID = 900_002
_KPI_NAME = "Forward Monthly ARPAC"


def _snapshot_evidence(label: str) -> SnapshotEvidenceState:
    return SnapshotEvidenceState.unverified(label)


def _seed_document(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    period_end: str,
    doc_version_id: str,
    sha_char: str,
    seed_evidence: bool = True,
) -> None:
    stamp = _EVIDENCE_AT.isoformat()
    sha = sha_char * 64
    conn.execute(
        "INSERT OR REPLACE INTO documents "
        "(id, ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(?, ?, 'ir_doc', 'ir_press_release', ?, ?, ?, ?, 'ok', 100, 'fmp_normalized')",
        (
            doc_id,
            ticker,
            period_end,
            f"ir_documents/{ticker}/{doc_id}.pdf",
            sha,
            stamp,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_content_blobs "
        "(sha256, byte_size, media_type, storage_uri, recorded_at) VALUES (?, 100, 'text/html', ?, ?)",
        (sha, f"https://example.invalid/{ticker.lower()}/{doc_id}", stamp),
    )
    obs_id = f"obs-{doc_id}"
    run_id = f"ext-run-{doc_id}"
    node_id = f"node-{doc_id}"
    locator_json = canonical_json_ascii_default_str({"pdf_page": 7}) if seed_evidence else None
    locator_sha = sha256_text(locator_json) if locator_json is not None else None
    conn.execute(
        "INSERT OR IGNORE INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
        "retrieved_at, retrieval_config_sha256, collector_code_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            obs_id,
            f"obs:{doc_id}",
            "ir_document",
            f"https://example.invalid/{ticker.lower()}/{doc_id}",
            sha,
            stamp,
            stamp,
            "d" * 64,
            "fwd-test/v1",
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, period_end, language, legacy_document_id, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            doc_version_id,
            f"document:{doc_id}",
            1,
            obs_id,
            sha,
            "issuer-nu",
            ticker,
            "ir_press_release",
            "ir_press_release",
            period_end,
            "en",
            doc_id,
            stamp,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_extraction_runs "
        "(extraction_run_id, idempotency_key, document_version_id, input_sha256, "
        "extractor_name, extractor_config_sha256, extractor_code_version, output_sha256, "
        "started_at, completed_at, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            f"run:{doc_id}",
            doc_version_id,
            sha,
            "ir_press_release_readout",
            "e" * 64,
            "fwd-test/v1",
            "f" * 64,
            stamp,
            stamp,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_nodes "
        "(node_id, evidence_key, revision, extraction_run_id, parent_node_id, supersedes_node_id, "
        "node_kind, text, locator_json, locator_sha256, recorded_at) "
        "VALUES (?, ?, 1, ?, NULL, NULL, 'document', 'Monthly ARPAC', ?, ?, ?)",
        (node_id, f"node:{doc_id}", run_id, locator_json, locator_sha, stamp),
    )


def _open_forward_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> sqlite3.Connection:
    db_path = tmp_path / "forward_kpi.db"
    migrated_db(db_path)
    stamp = _EVIDENCE_AT.isoformat()
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
    conn.execute("INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', ?)", (stamp,))
    conn.execute(
        "INSERT INTO tracked_companies (id, user_id, ticker, name, list_type, instrument_type) "
        "VALUES (1, 'bhanu', 'NU', 'Nu Holdings', 'portfolio', 'equity')"
    )
    conn.execute(
        "INSERT INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
        "VALUES ('issuer-nu', 'issuer:nu', 'operating_company', ?)",
        (stamp,),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "entity-nu",
            "entity-nu",
            "issuer-nu",
            "legal_registrant",
            "Nu Holdings",
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source, threshold_tier) "
        "VALUES (?, 'NU', ?, 'actual', 'ir_doc', 'tier_1_break')",
        (_KPI_DEF_ID, _KPI_NAME),
    )
    _seed_document(
        conn,
        doc_id=10,
        ticker="NU",
        period_end="2024-03-31",
        doc_version_id="document-v1",
        sha_char="a",
        seed_evidence=True,
    )
    conn.commit()
    return conn


def _manifest_for_period(
    *,
    doc_id: int,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    value: Decimal,
    reported_label: str = _KPI_NAME,
    accounting_basis: KpiAccountingBasis = KpiAccountingBasis.MANAGEMENT,
    semantic_context: KpiSemanticContext | None = None,
    auto_admit: bool = True,
) -> KpiExtractionManifest:
    return KpiExtractionManifest(
        ticker="NU",
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        source_doc_id=doc_id,
        auto_admit=auto_admit,
        reviewed_by="pipeline:forward_test",
        extracted_by="ir_press_release_readout",
        values=[
            KpiValue(
                name=_KPI_NAME,
                metric_name_as_reported=reported_label,
                value=value,
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                accounting_basis=accounting_basis,
                source_excerpt=f"{reported_label} was USD {value}.",
                locator=FactLocator(pdf_page=7),
                semantic_context=semantic_context,
            )
        ],
    )


def test_atomic_forward_admission_binds_definition_revision_and_projects_primary_series(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        _seed_document(
            conn,
            doc_id=11,
            ticker="NU",
            period_end="2024-06-30",
            doc_version_id="document-v2",
            sha_char="b",
        )
        q1 = persist_manifest(
            conn,
            run_id="run-q1",
            manifest=_manifest_for_period(
                doc_id=10,
                period_end=datetime(2024, 3, 31, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q1,
                value=Decimal("11.4"),
            ),
        )
        q2 = persist_manifest(
            conn,
            run_id="run-q2",
            manifest=_manifest_for_period(
                doc_id=11,
                period_end=datetime(2024, 6, 30, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q2,
                value=Decimal("12.1"),
            ),
        )
        assert q1.inserted == 1
        assert q2.inserted == 1

        rev_count = conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0]
        assert rev_count == 1
        head = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert head is not None
        assert head.revision == 1
        assert head.reported_label == _KPI_NAME
        assert head.status is KpiDefinitionStatus.ADMITTED

        bindings = conn.execute(
            "SELECT kpi_definition_revision_id FROM kpi_fact_semantic_contexts ORDER BY kpi_fact_id"
        ).fetchall()
        assert [row[0] for row in bindings] == [
            head.kpi_definition_revision_id,
            head.kpi_definition_revision_id,
        ]

        known_now = datetime(2027, 1, 1, tzinfo=UTC)
        projection = resolve_primary_kpi_series(
            conn,
            kpi_definition_id=_KPI_DEF_ID,
            effective_at=datetime(2024, 6, 30, tzinfo=UTC),
            known_at=known_now,
        )
        assert projection.status is KpiRevisionSeriesStatus.ELIGIBLE
        assert projection.anchor_definition_revision_id == head.kpi_definition_revision_id
        assert projection.included_definition_revision_ids == (head.kpi_definition_revision_id,)
        assert projection.breaks == ()
        assert projection.exclusions == ()
        assert [point.value for point in projection.points] == [Decimal("12.1"), Decimal("11.4")]

        census = audit_kpi_revision_shadow_census(
            conn,
            effective_at=datetime(2024, 6, 30, tzinfo=UTC),
            known_at=known_now,
            evaluated_at=known_now,
            snapshot_evidence=_snapshot_evidence("forward-clean"),
        )
        matching_series = [item for item in census.series if item.kpi_definition_id == _KPI_DEF_ID]
        assert len(matching_series) == 1
        assert matching_series[0].resolver_status is KpiRevisionSeriesStatus.ELIGIBLE
        assert len(matching_series[0].revision_aware_fact_ids) == 2
        assert matching_series[0].blocking_reasons == ()
    finally:
        conn.close()


def test_atomic_forward_admission_label_rename_creates_comparable_with_break(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        _seed_document(
            conn,
            doc_id=11,
            ticker="NU",
            period_end="2024-06-30",
            doc_version_id="document-v2",
            sha_char="b",
        )
        persist_manifest(
            conn,
            run_id="run-q1",
            manifest=_manifest_for_period(
                doc_id=10,
                period_end=datetime(2024, 3, 31, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q1,
                value=Decimal("11.4"),
                reported_label=_KPI_NAME,
            ),
        )
        r1 = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert r1 is not None

        persist_manifest(
            conn,
            run_id="run-q2",
            manifest=_manifest_for_period(
                doc_id=11,
                period_end=datetime(2024, 6, 30, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q2,
                value=Decimal("12.1"),
                reported_label="Forward Monthly ARPAC (Active)",
            ),
        )
        r2 = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert r2 is not None
        assert r2.revision == 2
        assert r2.supersedes_definition_revision_id == r1.kpi_definition_revision_id

        comp = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id=r2.kpi_definition_revision_id,
        )
        assert comp is not None
        assert comp.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK

        projection = resolve_primary_kpi_series(
            conn,
            kpi_definition_id=_KPI_DEF_ID,
            effective_at=datetime(2024, 6, 30, tzinfo=UTC),
            known_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert projection.status is KpiRevisionSeriesStatus.ELIGIBLE_WITH_BREAK
        assert projection.included_definition_revision_ids == (
            r1.kpi_definition_revision_id,
            r2.kpi_definition_revision_id,
        )
        assert len(projection.breaks) == 1
        assert projection.breaks[0].relation_kind == "renamed"
        assert [point.value for point in projection.points] == [Decimal("12.1"), Decimal("11.4")]
    finally:
        conn.close()


def test_atomic_forward_admission_semantic_redefinition_blocks_cross_revision_series(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        _seed_document(
            conn,
            doc_id=11,
            ticker="NU",
            period_end="2024-06-30",
            doc_version_id="document-v2",
            sha_char="b",
        )
        persist_manifest(
            conn,
            run_id="run-q1",
            manifest=_manifest_for_period(
                doc_id=10,
                period_end=datetime(2024, 3, 31, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q1,
                value=Decimal("11.4"),
                accounting_basis=KpiAccountingBasis.MANAGEMENT,
            ),
        )
        r1 = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert r1 is not None

        persist_manifest(
            conn,
            run_id="run-q2",
            manifest=_manifest_for_period(
                doc_id=11,
                period_end=datetime(2024, 6, 30, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q2,
                value=Decimal("10.2"),
                accounting_basis=KpiAccountingBasis.GAAP,
            ),
        )
        r2 = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert r2 is not None
        assert r2.revision == 2

        comp = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id=r2.kpi_definition_revision_id,
        )
        assert comp is not None
        assert comp.disposition is KpiDefinitionComparabilityDisposition.NOT_COMPARABLE

        projection = resolve_primary_kpi_series(
            conn,
            kpi_definition_id=_KPI_DEF_ID,
            effective_at=datetime(2024, 6, 30, tzinfo=UTC),
            known_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
        assert projection.included_definition_revision_ids == (r2.kpi_definition_revision_id,)
        assert [point.value for point in projection.points] == [Decimal("10.2")]
        assert any(
            exc.reason is KpiRevisionSeriesExclusionReason.NOT_COMPARABLE
            for exc in projection.exclusions
        )
    finally:
        conn.close()


def test_atomic_forward_admission_replays_idempotently_across_revisions(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        _seed_document(
            conn,
            doc_id=11,
            ticker="NU",
            period_end="2024-06-30",
            doc_version_id="document-v2",
            sha_char="b",
        )
        q1_manifest = _manifest_for_period(
            doc_id=10,
            period_end=datetime(2024, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("11.4"),
        )
        q2_manifest = _manifest_for_period(
            doc_id=11,
            period_end=datetime(2024, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("12.1"),
            reported_label="Forward Monthly ARPAC (Active)",
        )
        persist_manifest(conn, run_id="run-q1-a", manifest=q1_manifest)
        persist_manifest(conn, run_id="run-q2-a", manifest=q2_manifest)

        # Replay both Q1 (older period after r2 exists) and Q2.
        persist_manifest(conn, run_id="run-q1-b", manifest=q1_manifest)
        persist_manifest(conn, run_id="run-q2-b", manifest=q2_manifest)

        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM kpi_definition_comparability_revisions").fetchone()[
                0
            ]
            == 1
        )
        rows = conn.execute(
            "SELECT kpi_fact_id, kpi_definition_revision_id, COUNT(*) AS cnt "
            "FROM kpi_fact_semantic_contexts GROUP BY kpi_fact_id ORDER BY kpi_fact_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["kpi_definition_revision_id"] is not None
        assert rows[1]["kpi_definition_revision_id"] is not None
        assert rows[0]["kpi_definition_revision_id"] != rows[1]["kpi_definition_revision_id"]
        assert rows[0]["cnt"] == 1
        assert rows[1]["cnt"] == 1
    finally:
        conn.close()


def test_batch_forward_admission_promotes_unclassified_fact_idempotently(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        persist_manifest(
            conn,
            run_id="run-unclassified",
            manifest=_manifest_for_period(
                doc_id=10,
                period_end=datetime(2024, 3, 31, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q1,
                value=Decimal("11.4"),
                auto_admit=False,
            ),
        )
        before_row = conn.execute(
            "SELECT status, kpi_definition_revision_id FROM kpi_fact_semantic_contexts"
        ).fetchone()
        assert before_row["status"] == KpiSemanticStatus.LEGACY_UNKNOWN.value
        assert before_row["kpi_definition_revision_id"] is None

        known_at = datetime(2024, 4, 15, tzinfo=UTC)
        report_apply = run_batch_kpi_admission(
            conn,
            tickers=["NU"],
            as_of=date(2024, 5, 1),
            apply=True,
            reviewed_by="batch:forward_test",
            knowledge_at=known_at,
        )
        assert report_apply.total_admitted_applied == 1
        head = current_kpi_definition_revision(conn, kpi_definition_id=_KPI_DEF_ID)
        assert head is not None
        after_row = conn.execute(
            "SELECT status, kpi_definition_revision_id "
            "FROM kpi_fact_semantic_contexts ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        assert after_row["status"] == KpiSemanticStatus.ADMITTED.value
        assert after_row["kpi_definition_revision_id"] == head.kpi_definition_revision_id

        report_replay = run_batch_kpi_admission(
            conn,
            tickers=["NU"],
            as_of=date(2024, 5, 1),
            apply=True,
            reviewed_by="batch:forward_test",
            knowledge_at=known_at,
        )
        assert report_replay.total_admitted_applied == 0
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_adversarial_missing_evidence_and_quarantined_head_stay_isolated(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_forward_db(tmp_path, migrated_db)
    try:
        _seed_document(
            conn,
            doc_id=99,
            ticker="NU",
            period_end="2024-03-31",
            doc_version_id="document-no-evidence",
            sha_char="9",
            seed_evidence=False,
        )
        res = persist_manifest(
            conn,
            run_id="run-no-evidence",
            manifest=_manifest_for_period(
                doc_id=99,
                period_end=datetime(2024, 3, 31, tzinfo=UTC),
                fiscal_period_type=FiscalPeriodType.Q1,
                value=Decimal("9.9"),
                auto_admit=True,
            ),
        )
        assert res.inserted == 1
        assert conn.execute("SELECT COUNT(*) FROM kpi_definition_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0] == 2
        ctx_row = conn.execute(
            "SELECT status, kpi_definition_revision_id FROM kpi_fact_semantic_contexts"
        ).fetchone()
        assert ctx_row["status"] == KpiSemanticStatus.ADMITTED.value
        assert ctx_row["kpi_definition_revision_id"] is None

        projection = resolve_primary_kpi_series(
            conn,
            kpi_definition_id=_KPI_DEF_ID,
            effective_at=datetime(2024, 3, 31, tzinfo=UTC),
            known_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        assert projection.status is KpiRevisionSeriesStatus.LEGACY_UNBOUND
        assert projection.points == ()

        # Quarantined head blocks automatic forward supersession even with full evidence.
        _seed_document(
            conn,
            doc_id=100,
            ticker="NU",
            period_end="2024-06-30",
            doc_version_id="document-v100",
            sha_char="8",
            seed_evidence=True,
        )
        persist_kpi_definition_revision(
            conn,
            IssuerKpiDefinitionRevision(
                kpi_definition_revision_id="quarantined-head-r1",
                idempotency_key="quarantined-head-key-1",
                kpi_definition_id=_KPI_DEF_ID,
                reporting_entity_id="entity-nu",
                revision=1,
                supersedes_definition_revision_id=None,
                status=KpiDefinitionStatus.QUARANTINED,
                lifecycle=KpiDefinitionLifecycle.ACTIVE,
                reported_label=_KPI_NAME,
                reported_definition_text=None,
                definition_text_status=KpiDefinitionTextStatus.NOT_STATED,
                period_kind=KpiDefinitionPeriodKind.UNKNOWN,
                stock_flow_behavior=KpiStockFlowBehavior.UNKNOWN,
                unit_family=KpiUnitFamily.CURRENCY,
                unit_key=Unit.ACTUAL,
                unit_scale=KpiUnitScale.NONE,
                currency_disposition=KpiCurrencyDisposition.EXPLICIT,
                currency=Currency.USD,
                accounting_basis=KpiAccountingBasis.MANAGEMENT,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                dimensions={},
                source_document_version_id="document-v100",
                source_evidence_node_id="node-100",
                source_locator={"pdf_page": 7},
                reason_code="quarantined_ambiguous_definition",
                reviewed_by="owner:audit",
                effective_at=datetime(2024, 3, 31, tzinfo=UTC),
                knowledge_at=datetime(2024, 4, 1, tzinfo=UTC),
                recorded_at=datetime(2024, 4, 1, tzinfo=UTC),
            ),
        )
        blocked_binding = bind_forward_kpi_definition_revision(
            conn,
            kpi_definition_id=_KPI_DEF_ID,
            period_end=datetime(2024, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            unit=Unit.ACTUAL,
            currency=Currency.USD,
            source_doc_id=100,
            context=KpiSemanticContext(
                metric_name_as_reported=_KPI_NAME,
                reported_period_end=date(2024, 6, 30),
                period_role=KpiPeriodRole.CURRENT,
                publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
                accounting_basis=KpiAccountingBasis.MANAGEMENT,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                dimensions={},
                unit_scale=KpiUnitScale.NONE,
                status=KpiSemanticStatus.ADMITTED,
            ),
            reviewed_by="pipeline:forward_test",
            knowledge_at=datetime(2024, 7, 1, tzinfo=UTC),
        )
        assert blocked_binding is None

        # Separate definition root with a similar name is never fuzzy-merged by the primary reader.
        conn.execute(
            "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
            "VALUES (?, 'NU', ?, 'actual', 'ir_doc')",
            (_OTHER_KPI_DEF_ID, f"{_KPI_NAME} "),
        )
        unbound_other = resolve_primary_kpi_series(
            conn,
            kpi_definition_id=_OTHER_KPI_DEF_ID,
            effective_at=datetime(2024, 6, 30, tzinfo=UTC),
            known_at=datetime(2024, 7, 1, tzinfo=UTC),
        )
        assert unbound_other.status is KpiRevisionSeriesStatus.LEGACY_UNBOUND
        assert unbound_other.points == ()
    finally:
        conn.close()
