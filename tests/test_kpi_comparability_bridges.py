"""Comprehensive tests for BHA-100 comparability bridges, admission, and read modes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from compute.kpi_resolver import (
    KpiComparableYoYStatus,
    KpiSeriesReadStatus,
    resolve_cumulative_diff_standalone,
    resolve_kpi_as_reported,
    resolve_kpi_chart_series,
    resolve_kpi_comparable_yoy,
)
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.kpi_comparability_admission import (
    admit_additive_composite_bridge,
    admit_recast_comparative_bridge,
    admit_split_scale_bridge,
    admit_verified_rename_comparability,
)
from pipeline.kpi_comparability_bridges import (
    AdditiveCompositeBridge,
    CumulativeDiffInput,
    KpiPeriodCadenceKind,
    OverlapTolerancePolicy,
    OverlapVerificationError,
    RecastComparativeBridge,
    RenameOverlapBridge,
    SplitScaleBridge,
    build_overlap_witness,
    classify_period_cadence,
    cumulative_diff,
    derive_standalone_from_cumulative,
)
from pipeline.kpi_definition_revisions import (
    ComparativeOverlapCandidate,
    IssuerKpiDefinitionRevision,
    KpiCurrencyDisposition,
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionLifecycle,
    KpiDefinitionPeriodKind,
    KpiDefinitionRelationKind,
    KpiDefinitionStatus,
    KpiDefinitionTextStatus,
    KpiStockFlowBehavior,
    KpiUnitFamily,
    bind_forward_kpi_definition_revision,
    current_kpi_definition_comparability_revision,
    persist_kpi_definition_comparability_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_persistence import persist_kpi_value_at_exact_definition
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
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite, register_sqlite_integrity_functions

ROOT = Path(__file__).resolve().parents[1]
_BASE_TS = datetime(2024, 1, 2, tzinfo=UTC)
_KNOWN_NOW = datetime(2027, 1, 1, tzinfo=UTC)
_KNOWN_AFTER_UPGRADE = datetime(2027, 2, 1, tzinfo=UTC)
_LOCATOR: dict[str, object] = {"pdf_page": 7}


def _alembic_config(path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def _seed_issuer(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    issuer_id: str,
    entity_id: str,
    company_name: str,
) -> None:
    stamp = _BASE_TS.isoformat()
    conn.execute("INSERT OR IGNORE INTO tenants (id, created_at) VALUES ('bhanu', ?)", (stamp,))
    conn.execute(
        "INSERT OR IGNORE INTO tracked_companies (user_id, ticker, name, list_type, instrument_type) "
        "VALUES ('bhanu', ?, ?, 'portfolio', 'equity')",
        (ticker, company_name),
    )
    conn.execute(
        "INSERT OR IGNORE INTO issuer_entities (issuer_id, idempotency_key, entity_kind, created_at) "
        "VALUES (?, ?, 'operating_company', ?)",
        (issuer_id, f"issuer:{ticker.lower()}", stamp),
    )
    conn.execute(
        "INSERT OR IGNORE INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (entity_id, entity_id, issuer_id, "legal_registrant", company_name, stamp),
    )


def _seed_doc(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    ticker: str,
    issuer_id: str,
    period_end: str,
    doc_version_id: str,
    node_id: str,
    sha_hex: str,
    recorded_at: datetime = _BASE_TS,
    locator: dict[str, object] | None = None,
) -> None:
    stamp = recorded_at.isoformat()
    sha = (sha_hex * 64)[:64]
    loc = locator or _LOCATOR
    loc_json = canonical_json_ascii_default_str(loc)
    loc_sha = sha256_text(loc_json)
    conn.execute(
        "INSERT OR REPLACE INTO documents "
        "(id, ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
        "fetch_status, raw_bytes_size, source_quality_tier) VALUES "
        "(?, ?, 'ir_doc', 'ir_press_release', ?, ?, ?, ?, 'ok', 100, 'sec_official')",
        (doc_id, ticker, period_end, f"ir_documents/{ticker}/{doc_id}.pdf", sha, stamp),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_content_blobs "
        "(sha256, byte_size, media_type, storage_uri, recorded_at) VALUES (?, 100, 'application/pdf', ?, ?)",
        (sha, f"https://example.invalid/{ticker.lower()}/{doc_id}.pdf", stamp),
    )
    obs_id = f"obs-{doc_id}"
    run_id = f"run-{doc_id}"
    conn.execute(
        "INSERT OR IGNORE INTO evidence_source_observations "
        "(observation_id, idempotency_key, source_kind, source_url, blob_sha256, observed_at, "
        "retrieved_at, retrieval_config_sha256, collector_code_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            obs_id,
            f"obs:{doc_id}",
            "ir_document",
            f"https://example.invalid/{ticker.lower()}/{doc_id}.pdf",
            sha,
            stamp,
            stamp,
            "d" * 64,
            "bridge-test/v1",
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, blob_sha256, "
        "issuer_id, ticker, document_type, form_type, period_end, language, legacy_document_id, "
        "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            doc_version_id,
            f"doc:{doc_id}",
            1,
            obs_id,
            sha,
            issuer_id,
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
            "bridge-test/v1",
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
        "VALUES (?, ?, 1, ?, NULL, NULL, 'document', 'KPI metric', ?, ?, ?)",
        (node_id, f"node:{doc_id}", run_id, loc_json, loc_sha, stamp),
    )


def _seed_kpi_def(
    conn: sqlite3.Connection,
    *,
    def_id: int,
    ticker: str,
    name: str,
    unit: Unit,
) -> None:
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, primary_source) "
        "VALUES (?, ?, ?, ?, 'ir_doc')",
        (def_id, ticker, name, unit.value),
    )


def _persist_def_rev(
    conn: sqlite3.Connection,
    *,
    rev_id: str,
    def_id: int,
    entity_id: str,
    label: str,
    doc_version_id: str,
    node_id: str,
    effective_at: datetime,
    knowledge_at: datetime,
    unit_key: Unit = Unit.MILLIONS,
    unit_family: KpiUnitFamily = KpiUnitFamily.CURRENCY,
    unit_scale: KpiUnitScale = KpiUnitScale.MILLIONS,
    currency: Currency | None = Currency.USD,
    currency_disposition: KpiCurrencyDisposition = KpiCurrencyDisposition.EXPLICIT,
    accounting_basis: KpiAccountingBasis = KpiAccountingBasis.GAAP,
    consolidation_scope: KpiConsolidationScope = KpiConsolidationScope.CONSOLIDATED,
    dimensions: dict[str, str] | None = None,
    revision: int = 1,
    supersedes_id: str | None = None,
    stock_flow_behavior: KpiStockFlowBehavior = KpiStockFlowBehavior.FLOW,
    period_kind: KpiDefinitionPeriodKind = KpiDefinitionPeriodKind.DURATION,
) -> IssuerKpiDefinitionRevision:
    return persist_kpi_definition_revision(
        conn,
        IssuerKpiDefinitionRevision(
            kpi_definition_revision_id=rev_id,
            idempotency_key=f"key:{rev_id}",
            kpi_definition_id=def_id,
            reporting_entity_id=entity_id,
            revision=revision,
            supersedes_definition_revision_id=supersedes_id,
            status=KpiDefinitionStatus.ADMITTED,
            lifecycle=KpiDefinitionLifecycle.ACTIVE,
            reported_label=label,
            reported_definition_text=f"Reported {label}",
            definition_text_status=KpiDefinitionTextStatus.VERBATIM,
            period_kind=period_kind,
            stock_flow_behavior=stock_flow_behavior,
            unit_family=unit_family,
            unit_key=unit_key,
            unit_scale=unit_scale,
            currency_disposition=currency_disposition,
            currency=currency,
            accounting_basis=accounting_basis,
            consolidation_scope=consolidation_scope,
            dimensions=dimensions or {},
            source_document_version_id=doc_version_id,
            source_evidence_node_id=node_id,
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=effective_at,
            knowledge_at=knowledge_at,
            recorded_at=knowledge_at,
        ),
    )


def _persist_bound_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    def_id: int,
    def_name: str,
    rev: IssuerKpiDefinitionRevision,
    doc_id: int,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    value: Decimal,
    knowledge_at: datetime,
    period_role: KpiPeriodRole = KpiPeriodRole.CURRENT,
    publication_lane: KpiPublicationLane = KpiPublicationLane.CURRENT_ACTUAL,
) -> int:
    ctx = KpiSemanticContext(
        metric_name_as_reported=rev.reported_label,
        reported_period_end=period_end.date(),
        period_role=period_role,
        publication_lane=publication_lane,
        accounting_basis=rev.accounting_basis,
        consolidation_scope=rev.consolidation_scope,
        dimensions=dict(rev.dimensions),
        unit_scale=rev.unit_scale,
        status=KpiSemanticStatus.ADMITTED,
    )
    unit_enum = rev.unit_key
    assert isinstance(unit_enum, Unit)
    res = persist_kpi_value_at_exact_definition(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        source_doc_id=doc_id,
        kpi_definition_id=def_id,
        expected_definition_name=def_name,
        value=value,
        unit=unit_enum,
        currency=rev.currency,
        locator=FactLocator(pdf_page=7),
        source_excerpt=f"{rev.reported_label} was {value}",
        context=ctx,
        reviewed_by="analyst:test",
        knowledge_at=knowledge_at,
        kpi_definition_revision_id=rev.kpi_definition_revision_id,
        extracted_by="ir_press_release_readout",
    )
    return res.fact_id


def _open_db(tmp_path: Path, migrated_db: Callable[..., Path], name: str) -> sqlite3.Connection:
    db_path = tmp_path / name
    migrated_db(db_path)
    return connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)


# ---------------------------------------------------------------------------
# Migration 0074: backward compatibility, CHECK, trigger, downgrade guard
# ---------------------------------------------------------------------------


def test_migration_0074_preserves_legacy_rows_enforces_triggers_and_guards_downgrade(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "mig0074.db", target="0073_drop_redundant_indexes")
    cfg = _alembic_config(db_path)

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(raw)
        _seed_issuer(
            raw, ticker="TEM", issuer_id="iss-tem", entity_id="ent-tem", company_name="Tempus"
        )
        _seed_doc(
            raw,
            doc_id=10,
            ticker="TEM",
            issuer_id="iss-tem",
            period_end="2025-03-31",
            doc_version_id="doc-tem-v1",
            node_id="node-tem-1",
            sha_hex="a",
        )
        _seed_kpi_def(raw, def_id=1001, ticker="TEM", name="Genomics", unit=Unit.MILLIONS)
        _seed_kpi_def(raw, def_id=1002, ticker="TEM", name="Diagnostics", unit=Unit.MILLIONS)
        r1 = _persist_def_rev(
            raw,
            rev_id="tem-genomics-r1",
            def_id=1001,
            entity_id="ent-tem",
            label="Genomics",
            doc_version_id="doc-tem-v1",
            node_id="node-tem-1",
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2024, 2, 1, tzinfo=UTC),
        )
        r2 = _persist_def_rev(
            raw,
            rev_id="tem-diagnostics-r1",
            def_id=1002,
            entity_id="ent-tem",
            label="Diagnostics",
            doc_version_id="doc-tem-v1",
            node_id="node-tem-1",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 2, 1, tzinfo=UTC),
        )
        legacy_comp = KpiDefinitionComparabilityRevision(
            comparability_revision_id="legacy-comp-r1",
            idempotency_key="legacy-comp-key-r1",
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id=r2.kpi_definition_revision_id,
            revision=1,
            relation_kind=KpiDefinitionRelationKind.RENAMED,
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
            reason_code="legacy_unverified_rename",
            reviewed_by="analyst:test",
            source_document_version_id="doc-tem-v1",
            source_evidence_node_id="node-tem-1",
            source_locator=_LOCATOR,
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 2, 1, tzinfo=UTC),
            recorded_at=datetime(2025, 2, 1, tzinfo=UTC),
        )
        assert "bridge_spec" not in legacy_comp.commitment_payload
        persisted_pre = persist_kpi_definition_comparability_revision(raw, legacy_comp)
        assert persisted_pre.bridge_spec is None
        pre_sha = persisted_pre.commitment_sha256
        raw.commit()

    command.upgrade(cfg, "0074_kpi_comparability_bridge_spec")

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(raw)
        loaded = current_kpi_definition_comparability_revision(
            raw,
            predecessor_definition_revision_id="tem-genomics-r1",
            successor_definition_revision_id="tem-diagnostics-r1",
        )
        assert loaded is not None
        assert loaded.bridge_spec is None
        assert loaded.commitment_sha256 == pre_sha

        # Downgrade succeeds while all bridge_spec_json values are NULL.
    command.downgrade(cfg, "0073_drop_redundant_indexes")
    command.upgrade(cfg, "0074_kpi_comparability_bridge_spec")

    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        register_sqlite_integrity_functions(raw)
        witness = build_overlap_witness(
            period_end="2024-12-31",
            predecessor_value=Decimal("100.0"),
            successor_value=Decimal("100.2"),
            source_locator=_LOCATOR,
        )
        upgraded = KpiDefinitionComparabilityRevision(
            comparability_revision_id="comp-with-bridge-r2",
            idempotency_key="comp-with-bridge-key-r2",
            predecessor_definition_revision_id="tem-genomics-r1",
            successor_definition_revision_id="tem-diagnostics-r1",
            revision=2,
            supersedes_comparability_revision_id="legacy-comp-r1",
            relation_kind=KpiDefinitionRelationKind.RENAMED,
            disposition=KpiDefinitionComparabilityDisposition.CONTINUOUS,
            reason_code="verified_rename_overlap",
            reviewed_by="analyst:test",
            source_document_version_id="doc-tem-v1",
            source_evidence_node_id="node-tem-1",
            source_locator=_LOCATOR,
            bridge_spec=RenameOverlapBridge(overlap_witness=witness),
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 3, 1, tzinfo=UTC),
            recorded_at=datetime(2025, 3, 1, tzinfo=UTC),
        )
        # Tampered commitment missing $.bridge_spec must fail DB trigger.
        tampered_payload = dict(upgraded.commitment_payload)
        del tampered_payload["bridge_spec"]
        tampered_json = canonical_json_ascii_default_str(tampered_payload)
        with pytest.raises(sqlite3.IntegrityError, match="bridge_spec commitment mismatch"):
            raw.execute(
                "INSERT INTO kpi_definition_comparability_revisions "
                "(comparability_revision_id,idempotency_key,predecessor_definition_revision_id,"
                "successor_definition_revision_id,revision,supersedes_comparability_revision_id,"
                "relation_kind,disposition,reason_code,reviewed_by,source_document_version_id,"
                "source_evidence_node_id,source_locator_json,source_locator_sha256,"
                "commitment_json,commitment_sha256,effective_at,knowledge_at,recorded_at,"
                "bridge_spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "tampered-r2",
                    "tampered-key-r2",
                    upgraded.predecessor_definition_revision_id,
                    upgraded.successor_definition_revision_id,
                    2,
                    "legacy-comp-r1",
                    upgraded.relation_kind.value,
                    upgraded.disposition.value,
                    upgraded.reason_code,
                    upgraded.reviewed_by,
                    upgraded.source_document_version_id,
                    upgraded.source_evidence_node_id,
                    upgraded.source_locator_json,
                    upgraded.source_locator_sha256,
                    tampered_json,
                    sha256_text(tampered_json),
                    upgraded.effective_at.isoformat().replace("+00:00", "Z"),
                    upgraded.knowledge_at.isoformat().replace("+00:00", "Z"),
                    upgraded.recorded_at.isoformat().replace("+00:00", "Z"),
                    upgraded.bridge_spec_json,
                ),
            )
        # Non-object JSON in bridge_spec_json fails CHECK constraint.
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO kpi_definition_comparability_revisions "
                "(comparability_revision_id,idempotency_key,predecessor_definition_revision_id,"
                "successor_definition_revision_id,revision,supersedes_comparability_revision_id,"
                "relation_kind,disposition,reason_code,reviewed_by,source_document_version_id,"
                "source_evidence_node_id,source_locator_json,source_locator_sha256,"
                "commitment_json,commitment_sha256,effective_at,knowledge_at,recorded_at,"
                "bridge_spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "bad-json-r2",
                    "bad-json-key-r2",
                    upgraded.predecessor_definition_revision_id,
                    upgraded.successor_definition_revision_id,
                    2,
                    "legacy-comp-r1",
                    upgraded.relation_kind.value,
                    upgraded.disposition.value,
                    upgraded.reason_code,
                    upgraded.reviewed_by,
                    upgraded.source_document_version_id,
                    upgraded.source_evidence_node_id,
                    upgraded.source_locator_json,
                    upgraded.source_locator_sha256,
                    upgraded.commitment_json,
                    upgraded.commitment_sha256,
                    upgraded.effective_at.isoformat().replace("+00:00", "Z"),
                    upgraded.knowledge_at.isoformat().replace("+00:00", "Z"),
                    upgraded.recorded_at.isoformat().replace("+00:00", "Z"),
                    "[1,2,3]",
                ),
            )

        saved = persist_kpi_definition_comparability_revision(raw, upgraded)
        assert isinstance(saved.bridge_spec, RenameOverlapBridge)
        raw.commit()

    with pytest.raises(RuntimeError, match="without losing bridge history"):
        command.downgrade(cfg, "0073_drop_redundant_indexes")


# ---------------------------------------------------------------------------
# Pattern 1: Pure Label Rename (TEM: Genomics -> Diagnostics)
# ---------------------------------------------------------------------------


def test_pure_label_rename_verified_continuous_vs_unverified_and_mismatch(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "tem_rename.db")
    try:
        _seed_issuer(
            conn, ticker="TEM", issuer_id="iss-tem", entity_id="ent-tem", company_name="Tempus AI"
        )
        _seed_doc(
            conn,
            doc_id=101,
            ticker="TEM",
            issuer_id="iss-tem",
            period_end="2024-03-31",
            doc_version_id="doc-tem-q1-24",
            node_id="node-tem-q1-24",
            sha_hex="1",
            recorded_at=datetime(2024, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=102,
            ticker="TEM",
            issuer_id="iss-tem",
            period_end="2025-03-31",
            doc_version_id="doc-tem-q1-25",
            node_id="node-tem-q1-25",
            sha_hex="2",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=2001, ticker="TEM", name="Genomics", unit=Unit.MILLIONS)
        _seed_kpi_def(conn, def_id=2002, ticker="TEM", name="Diagnostics", unit=Unit.MILLIONS)
        _seed_kpi_def(conn, def_id=2003, ticker="TEM", name="Data & Services", unit=Unit.MILLIONS)

        r_gen = _persist_def_rev(
            conn,
            rev_id="tem-genomics-r1",
            def_id=2001,
            entity_id="ent-tem",
            label="Genomics",
            doc_version_id="doc-tem-q1-24",
            node_id="node-tem-q1-24",
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
        r_diag = _persist_def_rev(
            conn,
            rev_id="tem-diagnostics-r1",
            def_id=2002,
            entity_id="ent-tem",
            label="Diagnostics",
            doc_version_id="doc-tem-q1-25",
            node_id="node-tem-q1-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="TEM",
            def_id=2001,
            def_name="Genomics",
            rev=r_gen,
            doc_id=101,
            period_end=datetime(2024, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("110.0"),
            knowledge_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="TEM",
            def_id=2002,
            def_name="Diagnostics",
            rev=r_diag,
            doc_id=102,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("165.0"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )

        # Step A: Conservative unverified rename fails closed on YoY and splits chart segments.
        persist_kpi_definition_comparability_revision(
            conn,
            KpiDefinitionComparabilityRevision(
                comparability_revision_id="tem-unverified-r1",
                idempotency_key="tem-unverified-key-r1",
                predecessor_definition_revision_id=r_gen.kpi_definition_revision_id,
                successor_definition_revision_id=r_diag.kpi_definition_revision_id,
                revision=1,
                relation_kind=KpiDefinitionRelationKind.RENAMED,
                disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
                reason_code="forward_label_renamed",
                reviewed_by="analyst:test",
                source_document_version_id="doc-tem-q1-25",
                source_evidence_node_id="node-tem-q1-25",
                source_locator=_LOCATOR,
                effective_at=datetime(2025, 1, 1, tzinfo=UTC),
                knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
                recorded_at=datetime(2025, 5, 2, tzinfo=UTC),
            ),
        )
        t_unverified = _KNOWN_NOW
        eff_q1_25 = datetime(2025, 5, 2, tzinfo=UTC)
        yoy_unverified = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=2002,
            period_end="2025-03-31",
            effective_at=eff_q1_25,
            known_at=t_unverified,
        )
        assert yoy_unverified.status is KpiComparableYoYStatus.NOT_COMPARABLE
        assert yoy_unverified.yoy_delta is None

        chart_unverified = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=2002,
            effective_at=eff_q1_25,
            known_at=t_unverified,
        )
        assert len(chart_unverified.segments) == 2
        assert len(chart_unverified.breaks) == 1
        assert chart_unverified.breaks[0].relation_kind is KpiDefinitionRelationKind.RENAMED
        assert (
            chart_unverified.breaks[0].disposition
            is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK
        )

        # Step B: Mismatching overlap (> 0.5%) raises when fail_on_mismatch=True.
        with pytest.raises(OverlapVerificationError, match="overlap mismatch"):
            admit_verified_rename_comparability(
                conn,
                predecessor_definition_revision_id=r_gen.kpi_definition_revision_id,
                successor_definition_revision_id=r_diag.kpi_definition_revision_id,
                comparative_period_end="2024-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                successor_comparative_value=Decimal("125.0"),
                source_document_version_id="doc-tem-q1-25",
                source_evidence_node_id="node-tem-q1-25",
                source_locator=_LOCATOR,
                reviewed_by="analyst:test",
                effective_at=datetime(2025, 1, 1, tzinfo=UTC),
                knowledge_at=datetime(2027, 1, 10, tzinfo=UTC),
                recorded_at=datetime(2027, 1, 10, tzinfo=UTC),
                fail_on_mismatch=True,
            )

        # Step C: Verified overlap within 0.5% (110.0 vs 110.3 -> 0.27%) upgrades to CONTINUOUS.
        verified = admit_verified_rename_comparability(
            conn,
            predecessor_definition_revision_id=r_gen.kpi_definition_revision_id,
            successor_definition_revision_id=r_diag.kpi_definition_revision_id,
            comparative_period_end="2024-03-31",
            fiscal_period_type=FiscalPeriodType.Q1,
            successor_comparative_value=Decimal("110.3"),
            source_document_version_id="doc-tem-q1-25",
            source_evidence_node_id="node-tem-q1-25",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2027, 1, 15, tzinfo=UTC),
            recorded_at=datetime(2027, 1, 15, tzinfo=UTC),
        )
        assert verified.revision == 2
        assert verified.relation_kind is KpiDefinitionRelationKind.RENAMED
        assert verified.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
        assert isinstance(verified.bridge_spec, RenameOverlapBridge)
        assert verified.bridge_spec.overlap_witness.predecessor_value == Decimal("110.0")
        assert verified.bridge_spec.overlap_witness.successor_value == Decimal("110.3")

        t_verified = _KNOWN_AFTER_UPGRADE
        yoy_verified = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=2002,
            period_end="2025-03-31",
            effective_at=eff_q1_25,
            known_at=t_verified,
        )
        assert yoy_verified.status is KpiComparableYoYStatus.COMPARABLE
        assert yoy_verified.bridge_kind == "rename_overlap"
        assert yoy_verified.prior_value_raw == Decimal("110.0")
        assert yoy_verified.yoy_delta == Decimal("55.0")
        assert yoy_verified.yoy_pct == Decimal("0.5")

        chart_verified = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=2002,
            effective_at=eff_q1_25,
            known_at=t_verified,
        )
        assert chart_verified.status is KpiSeriesReadStatus.ELIGIBLE
        assert chart_verified.breaks == ()
        assert len(chart_verified.segments) == 1
        assert [p.value for p in chart_verified.segments[0].points] == [
            Decimal("110.0"),
            Decimal("165.0"),
        ]

        # Step D: Forward binding on same root (Data & Services -> Data and Applications):
        # overlap disagreement downgrades automatically to REDEFINED + NOT_COMPARABLE.
        b1 = bind_forward_kpi_definition_revision(
            conn,
            kpi_definition_id=2003,
            period_end=datetime(2024, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            unit=Unit.MILLIONS,
            currency=Currency.USD,
            source_doc_id=101,
            context=KpiSemanticContext(
                metric_name_as_reported="Data & Services",
                reported_period_end=date(2024, 3, 31),
                period_role=KpiPeriodRole.CURRENT,
                publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
                accounting_basis=KpiAccountingBasis.GAAP,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                dimensions={},
                unit_scale=KpiUnitScale.MILLIONS,
                status=KpiSemanticStatus.ADMITTED,
            ),
            reviewed_by="pipeline:test",
            knowledge_at=datetime(2024, 5, 2, tzinfo=UTC),
        )
        assert b1 is not None
        b2 = bind_forward_kpi_definition_revision(
            conn,
            kpi_definition_id=2003,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            unit=Unit.MILLIONS,
            currency=Currency.USD,
            source_doc_id=102,
            context=KpiSemanticContext(
                metric_name_as_reported="Data and Applications",
                reported_period_end=date(2025, 3, 31),
                period_role=KpiPeriodRole.CURRENT,
                publication_lane=KpiPublicationLane.CURRENT_ACTUAL,
                accounting_basis=KpiAccountingBasis.GAAP,
                consolidation_scope=KpiConsolidationScope.CONSOLIDATED,
                dimensions={},
                unit_scale=KpiUnitScale.MILLIONS,
                status=KpiSemanticStatus.ADMITTED,
            ),
            reviewed_by="pipeline:test",
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            comparative_overlap=ComparativeOverlapCandidate(
                period_end="2024-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                predecessor_value=Decimal("40.0"),
                successor_comparative_value=Decimal("48.0"),
            ),
        )
        assert b2 is not None
        assert b2.comparability_disposition is KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
        comp_ds = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=b1.kpi_definition_revision_id,
            successor_definition_revision_id=b2.kpi_definition_revision_id,
        )
        assert comp_ds is not None
        assert comp_ds.relation_kind is KpiDefinitionRelationKind.REDEFINED
        assert comp_ds.reason_code == "rename_overlap_mismatch"

        # Rounding-unit tolerance allows 1 reporting unit difference on small numbers when configured.
        with pytest.raises(OverlapVerificationError):
            build_overlap_witness(
                period_end="2024-03-31",
                predecessor_value=Decimal("20"),
                successor_value=Decimal("21"),
                source_locator=_LOCATOR,
            )
        rounded_witness = build_overlap_witness(
            period_end="2024-03-31",
            predecessor_value=Decimal("20"),
            successor_value=Decimal("21"),
            source_locator=_LOCATOR,
            policy=OverlapTolerancePolicy(rounding_unit=Decimal("1")),
        )
        assert rounded_witness.rounding_tolerance == Decimal("1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pattern 2: Stock Split / Share-Count Rescale (BKNG 25:1, BN 3:2)
# ---------------------------------------------------------------------------


def test_stock_split_scale_bridge_preserves_as_reported_and_adjusts_yoy_and_chart(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "bkng_split.db")
    try:
        _seed_issuer(
            conn,
            ticker="BKNG",
            issuer_id="iss-bkng",
            entity_id="ent-bkng",
            company_name="Booking Holdings",
        )
        _seed_doc(
            conn,
            doc_id=201,
            ticker="BKNG",
            issuer_id="iss-bkng",
            period_end="2025-03-31",
            doc_version_id="doc-bkng-q1-25",
            node_id="node-bkng-q1-25",
            sha_hex="3",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=202,
            ticker="BKNG",
            issuer_id="iss-bkng",
            period_end="2026-03-31",
            doc_version_id="doc-bkng-q1-26",
            node_id="node-bkng-q1-26",
            sha_hex="4",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(
            conn, def_id=3001, ticker="BKNG", name="Adjusted EPS (pre-split)", unit=Unit.ACTUAL
        )
        _seed_kpi_def(
            conn, def_id=3002, ticker="BKNG", name="Adjusted EPS (post-split)", unit=Unit.ACTUAL
        )

        r_pre = _persist_def_rev(
            conn,
            rev_id="bkng-eps-pre-r1",
            def_id=3001,
            entity_id="ent-bkng",
            label="Adjusted EPS",
            doc_version_id="doc-bkng-q1-25",
            node_id="node-bkng-q1-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            unit_key=Unit.ACTUAL,
            unit_scale=KpiUnitScale.NONE,
            accounting_basis=KpiAccountingBasis.NON_GAAP,
        )
        r_post = _persist_def_rev(
            conn,
            rev_id="bkng-eps-post-r1",
            def_id=3002,
            entity_id="ent-bkng",
            label="Adjusted EPS",
            doc_version_id="doc-bkng-q1-26",
            node_id="node-bkng-q1-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            unit_key=Unit.ACTUAL,
            unit_scale=KpiUnitScale.NONE,
            accounting_basis=KpiAccountingBasis.NON_GAAP,
        )
        f_pre = _persist_bound_fact(
            conn,
            ticker="BKNG",
            def_id=3001,
            def_name="Adjusted EPS (pre-split)",
            rev=r_pre,
            doc_id=201,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("125.00"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="BKNG",
            def_id=3002,
            def_name="Adjusted EPS (post-split)",
            rev=r_post,
            doc_id=202,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("6.00"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        # Wrong comparative proof fails closed without writing a bridge.
        with pytest.raises(OverlapVerificationError, match="overlap mismatch"):
            admit_split_scale_bridge(
                conn,
                predecessor_definition_revision_id=r_pre.kpi_definition_revision_id,
                successor_definition_revision_id=r_post.kpi_definition_revision_id,
                scale_numerator=25,
                scale_denominator=1,
                applies_to="per_share_divide",
                effective_date="2026-01-15",
                comparative_period_end="2025-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                successor_comparative_value=Decimal("7.50"),
                source_document_version_id="doc-bkng-q1-26",
                source_evidence_node_id="node-bkng-q1-26",
                source_locator=_LOCATOR,
                reviewed_by="analyst:test",
                effective_at=datetime(2026, 1, 15, tzinfo=UTC),
                knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
            )

        bridge_row = admit_split_scale_bridge(
            conn,
            predecessor_definition_revision_id=r_pre.kpi_definition_revision_id,
            successor_definition_revision_id=r_post.kpi_definition_revision_id,
            scale_numerator=25,
            scale_denominator=1,
            applies_to="per_share_divide",
            effective_date="2026-01-15",
            comparative_period_end="2025-03-31",
            fiscal_period_type=FiscalPeriodType.Q1,
            successor_comparative_value=Decimal("5.00"),
            source_document_version_id="doc-bkng-q1-26",
            source_evidence_node_id="node-bkng-q1-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 15, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert isinstance(bridge_row.bridge_spec, SplitScaleBridge)

        # as_reported preserves untouched raw values (125.00 and 6.00).
        eff_bkng = datetime(2026, 5, 2, tzinfo=UTC)
        as_rep = resolve_kpi_as_reported(
            conn,
            kpi_definition_id=3002,
            effective_at=eff_bkng,
            known_at=_KNOWN_NOW,
        )
        assert [o.value for o in as_rep.observations] == [Decimal("125.00"), Decimal("6.00")]

        # comparable_yoy scales pre-split 125.00 -> 5.00 (+20% YoY).
        yoy = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=3002,
            period_end="2026-03-31",
            effective_at=eff_bkng,
            known_at=_KNOWN_NOW,
        )
        assert yoy.status is KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE
        assert yoy.bridge_kind == "scale_ratio"
        assert yoy.scale_factor == Decimal("0.04")
        assert yoy.prior_value_raw == Decimal("125.00")
        assert yoy.prior_value_adjusted == Decimal("5.00")
        assert yoy.yoy_delta == Decimal("1.00")
        assert yoy.yoy_pct == Decimal("0.2")
        assert yoy.component_fact_ids == (f_pre,)

        # chart_series default keeps raw segmented runs + break marker.
        chart_raw = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=3002,
            effective_at=eff_bkng,
            known_at=_KNOWN_NOW,
            apply_split_adjustment=False,
        )
        assert len(chart_raw.segments) == 2
        assert chart_raw.segments[0].points[0].value == Decimal("125.00")
        assert chart_raw.segments[0].points[0].is_adjusted is False
        assert len(chart_raw.breaks) == 1
        assert chart_raw.breaks[0].bridge_kind == "scale_ratio"

        # chart_series with opt-in split adjustment scales pre-split segment AND keeps break marker.
        chart_adj = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=3002,
            effective_at=eff_bkng,
            known_at=_KNOWN_NOW,
            apply_split_adjustment=True,
        )
        assert len(chart_adj.segments) == 2
        assert chart_adj.segments[0].is_transformed is True
        assert chart_adj.segments[0].points[0].value == Decimal("5.00")
        assert chart_adj.segments[0].points[0].raw_value == Decimal("125.00")
        assert chart_adj.segments[0].points[0].scale_factor == Decimal("0.04")
        assert len(chart_adj.breaks) == 1

        # Exact rational 3:2 share-count multiply (BN): 100 -> 150 with zero float drift.
        bn_witness = build_overlap_witness(
            period_end="2025-03-31",
            predecessor_value=Decimal("100"),
            successor_value=Decimal("150"),
            expected_value=Decimal("150"),
            source_locator=_LOCATOR,
        )
        bn_bridge = SplitScaleBridge(
            scale_numerator=3,
            scale_denominator=2,
            applies_to="share_count_multiply",
            effective_date="2025-10-09",
            overlap_witness=bn_witness,
        )
        assert bn_bridge.apply_to_predecessor(Decimal("100")) == Decimal("150")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pattern 3: Additive Segment Combine (TWST) & Split (QCOM)
# ---------------------------------------------------------------------------


def test_additive_composite_combine_and_split_bridges(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "twst_qcom_composite.db")
    try:
        # --- TWST: SynBio + Biopharma -> DSPS ---
        _seed_issuer(
            conn,
            ticker="TWST",
            issuer_id="iss-twst",
            entity_id="ent-twst",
            company_name="Twist Bioscience",
        )
        _seed_doc(
            conn,
            doc_id=301,
            ticker="TWST",
            issuer_id="iss-twst",
            period_end="2025-03-31",
            doc_version_id="doc-twst-q1-25",
            node_id="node-twst-q1-25",
            sha_hex="5",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=302,
            ticker="TWST",
            issuer_id="iss-twst",
            period_end="2026-03-31",
            doc_version_id="doc-twst-q1-26",
            node_id="node-twst-q1-26",
            sha_hex="6",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=4001, ticker="TWST", name="SynBio Revenue", unit=Unit.MILLIONS)
        _seed_kpi_def(
            conn, def_id=4002, ticker="TWST", name="Biopharma Revenue", unit=Unit.MILLIONS
        )
        _seed_kpi_def(conn, def_id=4003, ticker="TWST", name="DSPS Revenue", unit=Unit.MILLIONS)

        r_syn = _persist_def_rev(
            conn,
            rev_id="twst-synbio-r1",
            def_id=4001,
            entity_id="ent-twst",
            label="SynBio Revenue",
            doc_version_id="doc-twst-q1-25",
            node_id="node-twst-q1-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "SynBio"},
        )
        r_bio = _persist_def_rev(
            conn,
            rev_id="twst-biopharma-r1",
            def_id=4002,
            entity_id="ent-twst",
            label="Biopharma Revenue",
            doc_version_id="doc-twst-q1-25",
            node_id="node-twst-q1-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "Biopharma"},
        )
        r_dsps = _persist_def_rev(
            conn,
            rev_id="twst-dsps-r1",
            def_id=4003,
            entity_id="ent-twst",
            label="DSPS Revenue",
            doc_version_id="doc-twst-q1-26",
            node_id="node-twst-q1-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "DSPS"},
        )
        f_syn_q1 = _persist_bound_fact(
            conn,
            ticker="TWST",
            def_id=4001,
            def_name="SynBio Revenue",
            rev=r_syn,
            doc_id=301,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("35.0"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        f_bio_q1 = _persist_bound_fact(
            conn,
            ticker="TWST",
            def_id=4002,
            def_name="Biopharma Revenue",
            rev=r_bio,
            doc_id=301,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("15.0"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        # Q2 2025 only has SynBio (Biopharma missing) to test fail-closed missing member!
        _persist_bound_fact(
            conn,
            ticker="TWST",
            def_id=4001,
            def_name="SynBio Revenue",
            rev=r_syn,
            doc_id=301,
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("38.0"),
            knowledge_at=datetime(2025, 8, 1, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="TWST",
            def_id=4003,
            def_name="DSPS Revenue",
            rev=r_dsps,
            doc_id=302,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("60.0"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="TWST",
            def_id=4003,
            def_name="DSPS Revenue",
            rev=r_dsps,
            doc_id=302,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("64.0"),
            knowledge_at=datetime(2026, 8, 1, tzinfo=UTC),
        )

        # Sum mismatch fails closed.
        with pytest.raises(OverlapVerificationError, match="overlap mismatch"):
            admit_additive_composite_bridge(
                conn,
                direction="combine",
                member_definition_revision_ids=(
                    r_syn.kpi_definition_revision_id,
                    r_bio.kpi_definition_revision_id,
                ),
                composite_definition_revision_id=r_dsps.kpi_definition_revision_id,
                comparative_period_end="2025-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                composite_comparative_value=Decimal("58.0"),
                source_document_version_id="doc-twst-q1-26",
                source_evidence_node_id="node-twst-q1-26",
                source_locator=_LOCATOR,
                reviewed_by="analyst:test",
                effective_at=datetime(2026, 1, 1, tzinfo=UTC),
                knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
            )

        edges = admit_additive_composite_bridge(
            conn,
            direction="combine",
            member_definition_revision_ids=(
                r_syn.kpi_definition_revision_id,
                r_bio.kpi_definition_revision_id,
            ),
            composite_definition_revision_id=r_dsps.kpi_definition_revision_id,
            comparative_period_end="2025-03-31",
            fiscal_period_type=FiscalPeriodType.Q1,
            composite_comparative_value=Decimal("50.0"),
            source_document_version_id="doc-twst-q1-26",
            source_evidence_node_id="node-twst-q1-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert len(edges) == 2
        assert all(isinstance(e.bridge_spec, AdditiveCompositeBridge) for e in edges)

        # Q1 2026 DSPS vs Q1 2025 (SynBio 35 + Biopharma 15 = 50) -> +20% YoY without double counting.
        yoy_q1 = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=4003,
            period_end="2026-03-31",
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
        )
        assert yoy_q1.status is KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE
        assert yoy_q1.bridge_kind == "sum_of_revisions"
        assert yoy_q1.prior_value_adjusted == Decimal("50.0")
        assert yoy_q1.yoy_delta == Decimal("10.0")
        assert yoy_q1.yoy_pct == Decimal("0.2")
        assert yoy_q1.component_fact_ids == (f_syn_q1, f_bio_q1)

        # Q2 2026 DSPS fails closed because Biopharma Q2 2025 is missing.
        yoy_q2 = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=4003,
            period_end="2026-06-30",
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
        )
        assert yoy_q2.status is KpiComparableYoYStatus.COMPOSITE_MEMBER_MISSING
        assert yoy_q2.yoy_delta is None
        assert "twst-biopharma-r1" in (yoy_q2.reason or "")

        # chart_series with opt-in composite bridge builds summed pre-combine segment ONLY for complete periods.
        chart_comp = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=4003,
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
            apply_composite_bridge=True,
        )
        assert len(chart_comp.breaks) == 1
        assert chart_comp.breaks[0].bridge_kind == "sum_of_revisions"
        assert len(chart_comp.segments) == 2
        assert chart_comp.segments[0].is_transformed is True
        assert [p.value for p in chart_comp.segments[0].points] == [Decimal("50.0")]
        assert [p.value for p in chart_comp.segments[1].points] == [
            Decimal("60.0"),
            Decimal("64.0"),
        ]

        # --- QCOM: QCT_old -> QCT_ex_DC + Data Center (split) ---
        _seed_issuer(
            conn, ticker="QCOM", issuer_id="iss-qcom", entity_id="ent-qcom", company_name="Qualcomm"
        )
        _seed_doc(
            conn,
            doc_id=311,
            ticker="QCOM",
            issuer_id="iss-qcom",
            period_end="2025-03-31",
            doc_version_id="doc-qcom-q1-25",
            node_id="node-qcom-q1-25",
            sha_hex="7",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=312,
            ticker="QCOM",
            issuer_id="iss-qcom",
            period_end="2026-03-31",
            doc_version_id="doc-qcom-q1-26",
            node_id="node-qcom-q1-26",
            sha_hex="8",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=4101, ticker="QCOM", name="QCT Revenue", unit=Unit.MILLIONS)
        _seed_kpi_def(
            conn, def_id=4102, ticker="QCOM", name="QCT ex-DC Revenue", unit=Unit.MILLIONS
        )
        _seed_kpi_def(
            conn, def_id=4103, ticker="QCOM", name="Data Center Revenue", unit=Unit.MILLIONS
        )

        r_qct_old = _persist_def_rev(
            conn,
            rev_id="qcom-qct-old-r1",
            def_id=4101,
            entity_id="ent-qcom",
            label="QCT Revenue",
            doc_version_id="doc-qcom-q1-25",
            node_id="node-qcom-q1-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "QCT"},
        )
        r_qct_new = _persist_def_rev(
            conn,
            rev_id="qcom-qct-ex-dc-r1",
            def_id=4102,
            entity_id="ent-qcom",
            label="QCT ex-DC Revenue",
            doc_version_id="doc-qcom-q1-26",
            node_id="node-qcom-q1-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "QCT ex-DC"},
        )
        r_dc = _persist_def_rev(
            conn,
            rev_id="qcom-dc-r1",
            def_id=4103,
            entity_id="ent-qcom",
            label="Data Center Revenue",
            doc_version_id="doc-qcom-q1-26",
            node_id="node-qcom-q1-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.SEGMENT,
            dimensions={"segment": "Data Center"},
        )
        _persist_bound_fact(
            conn,
            ticker="QCOM",
            def_id=4101,
            def_name="QCT Revenue",
            rev=r_qct_old,
            doc_id=311,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("8000"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="QCOM",
            def_id=4102,
            def_name="QCT ex-DC Revenue",
            rev=r_qct_new,
            doc_id=312,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("7500"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="QCOM",
            def_id=4103,
            def_name="Data Center Revenue",
            rev=r_dc,
            doc_id=312,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1300"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        split_edges = admit_additive_composite_bridge(
            conn,
            direction="split",
            member_definition_revision_ids=(
                r_qct_new.kpi_definition_revision_id,
                r_dc.kpi_definition_revision_id,
            ),
            composite_definition_revision_id=r_qct_old.kpi_definition_revision_id,
            comparative_period_end="2025-03-31",
            fiscal_period_type=FiscalPeriodType.Q1,
            composite_comparative_value=Decimal("8000"),
            component_values={
                r_qct_new.kpi_definition_revision_id: Decimal("7100"),
                r_dc.kpi_definition_revision_id: Decimal("900"),
            },
            source_document_version_id="doc-qcom-q1-26",
            source_evidence_node_id="node-qcom-q1-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert len(split_edges) == 2

        # Single split child without child prior comparative fails closed (never compares part vs whole).
        eff_qcom = datetime(2026, 5, 2, tzinfo=UTC)
        yoy_dc = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=4103,
            period_end="2026-03-31",
            effective_at=eff_qcom,
            known_at=_KNOWN_NOW,
        )
        assert yoy_dc.status is KpiComparableYoYStatus.NOT_COMPARABLE
        assert yoy_dc.yoy_delta is None

        # Split parent re-aggregates both split children cleanly (7500 + 1300 = 8800 vs 8000 -> +10%).
        yoy_qct_total = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=4101,
            period_end="2026-03-31",
            effective_at=eff_qcom,
            known_at=_KNOWN_NOW,
        )
        assert yoy_qct_total.status is KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE
        assert yoy_qct_total.bridge_kind == "sum_of_revisions"
        assert yoy_qct_total.current_value == Decimal("8800")
        assert yoy_qct_total.prior_value_adjusted == Decimal("8000")
        assert yoy_qct_total.yoy_pct == Decimal("0.1")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pattern 4: Divestiture / Continuing-Ops Recast (VFC ex-Dickies)
# ---------------------------------------------------------------------------


def test_recast_comparative_bridge_uses_recast_comparative_and_fails_closed_when_absent(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "vfc_recast.db")
    try:
        _seed_issuer(
            conn, ticker="VFC", issuer_id="iss-vfc", entity_id="ent-vfc", company_name="VF Corp"
        )
        _seed_doc(
            conn,
            doc_id=401,
            ticker="VFC",
            issuer_id="iss-vfc",
            period_end="2025-03-31",
            doc_version_id="doc-vfc-25",
            node_id="node-vfc-25",
            sha_hex="9",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=402,
            ticker="VFC",
            issuer_id="iss-vfc",
            period_end="2026-03-31",
            doc_version_id="doc-vfc-26",
            node_id="node-vfc-26",
            sha_hex="b",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=5001, ticker="VFC", name="Total Revenue", unit=Unit.MILLIONS)
        _seed_kpi_def(
            conn,
            def_id=5002,
            ticker="VFC",
            name="Revenue (Continuing Ops ex-Dickies)",
            unit=Unit.MILLIONS,
        )

        r_pre = _persist_def_rev(
            conn,
            rev_id="vfc-rev-pre-r1",
            def_id=5001,
            entity_id="ent-vfc",
            label="Total Revenue",
            doc_version_id="doc-vfc-25",
            node_id="node-vfc-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        r_recast = _persist_def_rev(
            conn,
            rev_id="vfc-rev-ex-dickies-r1",
            def_id=5002,
            entity_id="ent-vfc",
            label="Revenue (Continuing Ops ex-Dickies)",
            doc_version_id="doc-vfc-26",
            node_id="node-vfc-26",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.OTHER,
            dimensions={"continuing_ops": "ex_dickies"},
        )
        # Pre-divestiture reported Q1 2025 ($2200M) and Q2 2025 ($2400M).
        _persist_bound_fact(
            conn,
            ticker="VFC",
            def_id=5001,
            def_name="Total Revenue",
            rev=r_pre,
            doc_id=401,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("2200"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="VFC",
            def_id=5001,
            def_name="Total Revenue",
            rev=r_pre,
            doc_id=401,
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("2400"),
            knowledge_at=datetime(2025, 8, 2, tzinfo=UTC),
        )
        # Recast successor reports Q1 2025 recast comparative ($1800M), Q1 2026 ($1980M), and Q2 2026 ($2100M) WITHOUT Q2 2025 recast comparative.
        _persist_bound_fact(
            conn,
            ticker="VFC",
            def_id=5002,
            def_name="Revenue (Continuing Ops ex-Dickies)",
            rev=r_recast,
            doc_id=402,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1800"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            period_role=KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
            publication_lane=KpiPublicationLane.COMPARATOR,
        )
        _persist_bound_fact(
            conn,
            ticker="VFC",
            def_id=5002,
            def_name="Revenue (Continuing Ops ex-Dickies)",
            rev=r_recast,
            doc_id=402,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1980"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="VFC",
            def_id=5002,
            def_name="Revenue (Continuing Ops ex-Dickies)",
            rev=r_recast,
            doc_id=402,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("2100"),
            knowledge_at=datetime(2026, 8, 2, tzinfo=UTC),
        )

        recast_edge = admit_recast_comparative_bridge(
            conn,
            predecessor_definition_revision_id=r_pre.kpi_definition_revision_id,
            successor_definition_revision_id=r_recast.kpi_definition_revision_id,
            recast_reason="divestiture_ex_dickies",
            scope_change_summary="Dickies classified as discontinued operations.",
            comparative_periods_present=("2025-03-31",),
            source_document_version_id="doc-vfc-26",
            source_evidence_node_id="node-vfc-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert isinstance(recast_edge.bridge_spec, RecastComparativeBridge)

        # Q1 2026 resolves via same-revision recast comparative ($1980 vs $1800 -> +10%).
        yoy_q1 = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=5002,
            period_end="2026-03-31",
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
        )
        assert yoy_q1.status is KpiComparableYoYStatus.COMPARABLE
        assert yoy_q1.bridge_kind == "recast_comparative_only"
        assert yoy_q1.prior_value_raw == Decimal("1800")
        assert yoy_q1.yoy_delta == Decimal("180")
        assert yoy_q1.yoy_pct == Decimal("0.1")

        # Q2 2026 fails closed (never compares ex-Dickies $2100 to pre-divestiture $2400).
        yoy_q2 = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=5002,
            period_end="2026-06-30",
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
        )
        assert yoy_q2.status is KpiComparableYoYStatus.RECAST_COMPARATIVE_MISSING
        assert yoy_q2.yoy_delta is None

        # chart_series splits into two separate segments with a typed RECAST break marker.
        chart = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=5002,
            effective_at=datetime(2026, 6, 30, tzinfo=UTC),
            known_at=_KNOWN_NOW,
        )
        assert len(chart.segments) == 2
        assert len(chart.breaks) == 1
        assert chart.breaks[0].relation_kind is KpiDefinitionRelationKind.RECAST
        assert chart.breaks[0].bridge_kind == "recast_comparative_only"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pattern 5: Cadence Separation & Deterministic cumulative_diff (NTDOY / SNPS)
# ---------------------------------------------------------------------------


def test_cadence_separation_and_deterministic_cumulative_diff(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "ntdoy_cadence.db")
    try:
        _seed_issuer(
            conn,
            ticker="NTDOY",
            issuer_id="iss-ntdoy",
            entity_id="ent-ntdoy",
            company_name="Nintendo",
        )
        _seed_doc(
            conn,
            doc_id=501,
            ticker="NTDOY",
            issuer_id="iss-ntdoy",
            period_end="2024-09-30",
            doc_version_id="doc-ntdoy-25",
            node_id="node-ntdoy-25",
            sha_hex="c",
            recorded_at=datetime(2024, 11, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=502,
            ticker="NTDOY",
            issuer_id="iss-ntdoy",
            period_end="2025-09-30",
            doc_version_id="doc-ntdoy-26",
            node_id="node-ntdoy-26",
            sha_hex="d",
            recorded_at=datetime(2025, 11, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=6001, ticker="NTDOY", name="Digital Sales", unit=Unit.BILLIONS)

        r1 = _persist_def_rev(
            conn,
            rev_id="ntdoy-digital-r1",
            def_id=6001,
            entity_id="ent-ntdoy",
            label="Digital Sales",
            doc_version_id="doc-ntdoy-25",
            node_id="node-ntdoy-25",
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2024, 11, 2, tzinfo=UTC),
            unit_key=Unit.BILLIONS,
            unit_scale=KpiUnitScale.BILLIONS,
            currency=Currency.JPY,
        )
        f_q1_24 = _persist_bound_fact(
            conn,
            ticker="NTDOY",
            def_id=6001,
            def_name="Digital Sales",
            rev=r1,
            doc_id=501,
            period_end=datetime(2024, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("300"),
            knowledge_at=datetime(2024, 11, 2, tzinfo=UTC),
        )
        f_h1_24 = _persist_bound_fact(
            conn,
            ticker="NTDOY",
            def_id=6001,
            def_name="Digital Sales",
            rev=r1,
            doc_id=501,
            period_end=datetime(2024, 9, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.H1,
            value=Decimal("700"),
            knowledge_at=datetime(2024, 11, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="NTDOY",
            def_id=6001,
            def_name="Digital Sales",
            rev=r1,
            doc_id=502,
            period_end=datetime(2025, 9, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.H1,
            value=Decimal("840"),
            knowledge_at=datetime(2025, 11, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="NTDOY",
            def_id=6001,
            def_name="Digital Sales",
            rev=r1,
            doc_id=502,
            period_end=datetime(2025, 9, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("460"),
            knowledge_at=datetime(2025, 11, 2, tzinfo=UTC),
        )

        # Q2 standalone vs prior year (which only has H1 cumulative at 2024-09-30) fails closed!
        eff_ntdoy = datetime(2025, 11, 2, tzinfo=UTC)
        yoy_q2_mismatch = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=6001,
            period_end="2025-09-30",
            fiscal_period_type=FiscalPeriodType.Q2,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
        )
        assert yoy_q2_mismatch.status is KpiComparableYoYStatus.PERIOD_KIND_MISMATCH
        assert yoy_q2_mismatch.yoy_delta is None

        # Explicit cross-cadence request (Q2 vs H1) fails closed immediately.
        yoy_explicit_mismatch = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=6001,
            period_end="2025-09-30",
            fiscal_period_type=FiscalPeriodType.Q2,
            prior_fiscal_period_type=FiscalPeriodType.H1,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
        )
        assert yoy_explicit_mismatch.status is KpiComparableYoYStatus.PERIOD_KIND_MISMATCH

        # H1 vs H1 succeeds cleanly (+20%).
        yoy_h1 = resolve_kpi_comparable_yoy(
            conn,
            kpi_definition_id=6001,
            period_end="2025-09-30",
            fiscal_period_type=FiscalPeriodType.H1,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
        )
        assert yoy_h1.status is KpiComparableYoYStatus.COMPARABLE
        assert yoy_h1.cadence_kind is KpiPeriodCadenceKind.CUMULATIVE_HALF
        assert yoy_h1.yoy_delta == Decimal("140")
        assert yoy_h1.yoy_pct == Decimal("0.2")

        # chart_series never mixes standalone_quarter and cumulative_half into one segment.
        chart_all = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=6001,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
        )
        assert len(chart_all.segments) == 2
        assert {seg.cadence_kind for seg in chart_all.segments} == {
            KpiPeriodCadenceKind.STANDALONE_QUARTER,
            KpiPeriodCadenceKind.CUMULATIVE_HALF,
        }
        chart_q_only = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=6001,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
            cadence_kind=KpiPeriodCadenceKind.STANDALONE_QUARTER,
        )
        assert len(chart_q_only.segments) == 1
        assert chart_q_only.segments[0].cadence_kind is KpiPeriodCadenceKind.STANDALONE_QUARTER
        assert len(chart_q_only.excluded_cadence_fact_ids) == 2

        # Deterministic cumulative_diff: H1 (700) - Q1 (300) -> Q2 (400), without mutating kpi_facts.
        facts_before = conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0]
        derived_q2 = resolve_cumulative_diff_standalone(
            conn,
            kpi_definition_id=6001,
            minuend_period_end="2024-09-30",
            minuend_fiscal_period_type=FiscalPeriodType.H1,
            subtrahend_period_end="2024-06-30",
            subtrahend_fiscal_period_type=FiscalPeriodType.Q1,
            effective_at=eff_ntdoy,
            known_at=_KNOWN_NOW,
        )
        assert derived_q2.derivation_kind == "cumulative_diff"
        assert derived_q2.fiscal_period_type is FiscalPeriodType.Q2
        assert derived_q2.cadence_kind is KpiPeriodCadenceKind.STANDALONE_QUARTER
        assert derived_q2.value == Decimal("400")
        assert derived_q2.input_fact_ids == (f_h1_24, f_q1_24)
        assert derived_q2.kpi_definition_revision_id == "ntdoy-digital-r1"
        assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == facts_before
    finally:
        conn.close()


def test_cumulative_diff_snps_9m_minus_h1_and_negative_guards() -> None:
    assert (
        classify_period_cadence(FiscalPeriodType.NINE_MONTHS)
        is KpiPeriodCadenceKind.CUMULATIVE_NINE_MONTHS
    )
    assert classify_period_cadence(FiscalPeriodType.Q3) is KpiPeriodCadenceKind.STANDALONE_QUARTER

    snps_9m = CumulativeDiffInput(
        fact_id=10,
        ticker="SNPS",
        kpi_definition_id=1,
        kpi_definition_revision_id="snps-rev-1",
        period_end="2025-07-31",
        fiscal_period_type=FiscalPeriodType.NINE_MONTHS,
        value=Decimal("4500.5"),
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        accounting_basis="gaap",
        consolidation_scope="consolidated",
        dimensions={},
        unit_scale="millions",
        stock_flow_behavior="flow",
        source_doc_id=70,
    )
    snps_h1 = CumulativeDiffInput(
        fact_id=9,
        ticker="SNPS",
        kpi_definition_id=1,
        kpi_definition_revision_id="snps-rev-1",
        period_end="2025-04-30",
        fiscal_period_type=FiscalPeriodType.H1,
        value=Decimal("2900.2"),
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        accounting_basis="gaap",
        consolidation_scope="consolidated",
        dimensions={},
        unit_scale="millions",
        stock_flow_behavior="flow",
        source_doc_id=69,
    )
    q3 = cumulative_diff(snps_9m, snps_h1)
    assert q3.fiscal_period_type is FiscalPeriodType.Q3
    assert q3.value == Decimal("1600.3")
    assert q3.formula == "9M - H1 -> Q3"

    # Cross-revision cumulative_diff is rejected.
    with pytest.raises(ValueError, match="identical kpi_definition_revision_id"):
        derive_standalone_from_cumulative(
            snps_9m, snps_h1.model_copy(update={"kpi_definition_revision_id": "snps-rev-2"})
        )
    # Different accounting basis is rejected.
    with pytest.raises(ValueError, match="identical accounting_basis"):
        derive_standalone_from_cumulative(
            snps_9m, snps_h1.model_copy(update={"accounting_basis": "non_gaap"})
        )
    # Different currency is rejected.
    with pytest.raises(ValueError, match="identical currency"):
        derive_standalone_from_cumulative(
            snps_9m, snps_h1.model_copy(update={"currency": Currency.EUR})
        )
    # Non-flow (stock/ratio) metric is rejected.
    with pytest.raises(ValueError, match="additive flow metrics"):
        derive_standalone_from_cumulative(
            snps_9m.model_copy(update={"stock_flow_behavior": "ratio"}),
            snps_h1.model_copy(update={"stock_flow_behavior": "ratio"}),
        )
    # Unsupported period pair is rejected.
    with pytest.raises(ValueError, match="unsupported cumulative_diff period pair"):
        derive_standalone_from_cumulative(
            snps_9m.model_copy(update={"fiscal_period_type": FiscalPeriodType.Q3}),
            snps_h1.model_copy(update={"fiscal_period_type": FiscalPeriodType.Q2}),
        )


def test_bridge_spec_pydantic_discriminated_union_and_contract_validation() -> None:
    witness = build_overlap_witness(
        period_end="2025-03-31",
        predecessor_value=Decimal("100"),
        successor_value=Decimal("100"),
        source_locator=_LOCATOR,
    )
    # Forged witness exceeding tolerance fails Pydantic validation directly.
    with pytest.raises(ValidationError, match="disagrees beyond configured tolerance"):
        build_overlap_witness(
            period_end="2025-03-31",
            predecessor_value=Decimal("100"),
            successor_value=Decimal("100"),
            source_locator=_LOCATOR,
        ).model_validate(
            {
                "period_end": "2025-03-31",
                "predecessor_value": "100",
                "successor_value": "120",
                "relative_delta": "0.2",
                "rounding_tolerance": "0",
                "max_relative_delta": "0.005",
                "source_locator": _LOCATOR,
            }
        )
    # RenameOverlapBridge cannot be attached to RESTATED/COMPARABLE_WITH_BREAK.
    with pytest.raises(ValidationError, match="rename_overlap bridge requires"):
        KpiDefinitionComparabilityRevision(
            comparability_revision_id="bad-bridge-r1",
            idempotency_key="bad-bridge-key-r1",
            predecessor_definition_revision_id="rev-1",
            successor_definition_revision_id="rev-2",
            revision=1,
            relation_kind=KpiDefinitionRelationKind.RESTATED,
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
            reason_code="bad_combo",
            reviewed_by="analyst:test",
            source_document_version_id="doc-v1",
            source_evidence_node_id="node-1",
            source_locator=_LOCATOR,
            bridge_spec=RenameOverlapBridge(overlap_witness=witness),
            effective_at=_BASE_TS,
            knowledge_at=_BASE_TS,
            recorded_at=_BASE_TS,
        )
    # JSON round-trip of discriminated union preserves concrete type.
    raw = json.dumps(
        {
            "kind": "recast_comparative_only",
            "recast_reason": "spinoff",
            "scope_change_summary": "Post-spinoff continuing operations.",
            "comparative_periods_present": ["2025-03-31"],
        }
    )
    from pipeline.kpi_comparability_bridges import parse_bridge_spec_json

    parsed = parse_bridge_spec_json(raw)
    assert isinstance(parsed, RecastComparativeBridge)
    assert parsed.comparative_periods_present == ("2025-03-31",)
