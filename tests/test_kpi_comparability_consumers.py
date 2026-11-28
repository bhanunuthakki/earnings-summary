"""Integration and unit tests for BHA-100 comparability-bridge consumer wiring.

Covers both seams:
- Seam 1 (Read / Chart & Report Consumers): ``load_kpi_chart_series``,
  ``load_kpi_comparable_yoy``, ``financials._kpi_series_for``,
  ``thesis._build_ledger``, ``segmented_kpi_chart``, segmented ``sparkline``,
  ``yoy_heatmap_table`` overrides/break markers, and workspace renderers.
- Seam 2 (Write / Ingest Consumers): ``persist_manifest`` with
  ``ComparativeOverlapCandidate`` / ``KpiComparativeValue`` /
  ``SplitScaleBridgeIntent`` / ``RecastBridgeIntent``, ``run_batch_kpi_admission``
  with explicit and same-document comparative overlaps, and
  ``build_reviewed_comparability_from_overlap``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from compute.kpi_resolver import (
    KpiComparableYoYStatus,
    KpiSeriesReadStatus,
    resolve_kpi_chart_series,
)
from models.documents import SourceType
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.issuer_fact_manifest import build_reviewed_comparability_from_overlap
from pipeline.kpi_batch_admission import run_batch_kpi_admission
from pipeline.kpi_comparability_admission import (
    admit_recast_comparative_bridge,
    admit_split_scale_bridge,
    admit_verified_rename_comparability,
)
from pipeline.kpi_comparability_bridges import (
    OverlapVerificationError,
    RecastComparativeBridge,
    RenameOverlapBridge,
    SplitScaleBridge,
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
    current_kpi_definition_comparability_revision,
    current_kpi_definition_revision,
    persist_kpi_definition_comparability_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_persistence import (
    KpiComparativeValue,
    KpiExtractionManifest,
    KpiValue,
    RecastBridgeIntent,
    SplitScaleBridgeIntent,
    persist_kpi_value_at_exact_definition,
    persist_manifest,
)
from pipeline.kpi_semantics import (
    KpiAccountingBasis,
    KpiConsolidationScope,
    KpiPeriodRole,
    KpiPublicationLane,
    KpiSemanticContext,
    KpiSemanticStatus,
    KpiUnitScale,
    persist_kpi_semantic_context,
    unclassified_kpi_context,
)
from report.models import (
    AnnualKpiSeries,
    FinancialsSection,
    KpiLedgerRow,
    KpiSeriesBreak,
    KpiSeriesSegment,
    SectionStatus,
)
from report.renderers.charts_v2 import segmented_kpi_chart
from report.renderers.workspace_charts import SparklineSize, sparkline
from report.renderers.workspace_data import kpi_comparable_trend_delta, select_kpi_strip
from report.renderers.workspace_sections.chrome import _kpi_tile
from report.renderers.workspace_sections.financials import (
    _annual_kpi_series_yoy_panel,
    _kpi_series_yoy_panel,
)
from report.renderers.workspace_sections.thesis_risk import _kpi_ledger_row
from report.sections._kpi_comparability import (
    resolve_aligned_kpi_comparability,
    resolve_ledger_kpi_comparability,
)
from report.sections.financials import kpi_series_for
from runtime.deterministic import canonical_json_ascii_default_str, sha256_text
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.loaders import load_kpi_chart_series, load_kpi_comparable_yoy

_BASE_TS = datetime(2024, 1, 2, tzinfo=UTC)
_KNOWN_NOW = datetime(2027, 1, 1, tzinfo=UTC)
_LOCATOR: dict[str, object] = {"pdf_page": 7}
_FACT_LOCATOR = FactLocator(pdf_page=7)


def _open_db(tmp_path: Path, migrated_db: Callable[..., Path], name: str) -> sqlite3.Connection:
    db_path = migrated_db(tmp_path / name)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER)
    conn.row_factory = sqlite3.Row
    return conn


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
            "consumer-test/v1",
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
            "consumer-test/v1",
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
            period_kind=KpiDefinitionPeriodKind.DURATION,
            stock_flow_behavior=KpiStockFlowBehavior.FLOW,
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
        reason_code=None,
    )
    res = persist_kpi_value_at_exact_definition(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type,
        source_doc_id=doc_id,
        kpi_definition_id=def_id,
        expected_definition_name=def_name,
        value=value,
        unit=Unit(str(rev.unit_key)),
        currency=rev.currency,
        locator=_FACT_LOCATOR,
        source_excerpt=f"{rev.reported_label}: {value}",
        context=ctx,
        reviewed_by="analyst:test",
        knowledge_at=knowledge_at,
        kpi_definition_revision_id=rev.kpi_definition_revision_id,
        extracted_by="ir_press_release_readout",
    )
    return res.fact_id


# ---------------------------------------------------------------------------
# Seam 1: Read / Chart & Report Consumer Wiring
# ---------------------------------------------------------------------------


def test_unbroken_revision_bound_series_parity_and_no_break_markers(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "unbroken.db")
    try:
        _seed_issuer(
            conn, ticker="ACME", issuer_id="iss-acme", entity_id="ent-acme", company_name="Acme Co"
        )
        _seed_doc(
            conn,
            doc_id=10,
            ticker="ACME",
            issuer_id="iss-acme",
            period_end="2025-03-31",
            doc_version_id="doc-acme-25",
            node_id="node-acme-25",
            sha_hex="1",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=11,
            ticker="ACME",
            issuer_id="iss-acme",
            period_end="2026-03-31",
            doc_version_id="doc-acme-26",
            node_id="node-acme-26",
            sha_hex="2",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=101, ticker="ACME", name="Cloud ARR", unit=Unit.MILLIONS)
        r1 = _persist_def_rev(
            conn,
            rev_id="acme-arr-r1",
            def_id=101,
            entity_id="ent-acme",
            label="Cloud ARR",
            doc_version_id="doc-acme-25",
            node_id="node-acme-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="ACME",
            def_id=101,
            def_name="Cloud ARR",
            rev=r1,
            doc_id=10,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("200"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="ACME",
            def_id=101,
            def_name="Cloud ARR",
            rev=r1,
            doc_id=11,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("240"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        chart = load_kpi_chart_series(
            "ACME", "Cloud ARR", conn=conn, effective_at=_KNOWN_NOW, known_at=_KNOWN_NOW
        )
        assert chart is not None
        assert chart.status is KpiSeriesReadStatus.ELIGIBLE
        assert len(chart.segments) == 1
        assert chart.breaks == ()

        yoy = load_kpi_comparable_yoy(
            "ACME",
            "Cloud ARR",
            "2026-03-31",
            conn=conn,
            effective_at=_KNOWN_NOW,
            known_at=_KNOWN_NOW,
        )
        assert yoy is not None
        assert yoy.status is KpiComparableYoYStatus.COMPARABLE
        assert yoy.yoy_pct == Decimal("0.2")

        labels_full = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4", "2026 Q1"]
        series = kpi_series_for(conn, "ACME", "Cloud ARR", labels_full, labels_full)
        assert series is not None
        assert series.breaks == []
        assert len(series.segments) == 1
        assert series.yoy_pct_full is not None
        assert series.yoy_pct_full[-1] == pytest.approx(20.0)

        fin = FinancialsSection(
            status=SectionStatus.OK,
            quarter_labels=labels_full,
            quarter_labels_full=labels_full,
            kpi_chart_series=[series],
        )
        buf = StringIO()
        _kpi_series_yoy_panel(buf, fin)
        html_out = buf.getvalue()
        assert "+20.0%" in html_out
        assert "data-kpi-break" not in html_out
        assert "data-segmented-kpi-chart" not in html_out
    finally:
        conn.close()


def test_unbridged_semantic_break_refuses_fake_yoy_and_renders_break_markers(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "unbridged_break.db")
    try:
        _seed_issuer(
            conn, ticker="BRK", issuer_id="iss-brk", entity_id="ent-brk", company_name="Break Corp"
        )
        _seed_doc(
            conn,
            doc_id=20,
            ticker="BRK",
            issuer_id="iss-brk",
            period_end="2025-03-31",
            doc_version_id="doc-brk-25",
            node_id="node-brk-25",
            sha_hex="3",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=21,
            ticker="BRK",
            issuer_id="iss-brk",
            period_end="2026-03-31",
            doc_version_id="doc-brk-26",
            node_id="node-brk-26",
            sha_hex="4",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=201, ticker="BRK", name="Platform GMV", unit=Unit.MILLIONS)
        r1 = _persist_def_rev(
            conn,
            rev_id="brk-gmv-r1",
            def_id=201,
            entity_id="ent-brk",
            label="Gross GMV",
            doc_version_id="doc-brk-25",
            node_id="node-brk-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        r2 = _persist_def_rev(
            conn,
            rev_id="brk-gmv-r2",
            def_id=201,
            entity_id="ent-brk",
            label="Net GMV",
            doc_version_id="doc-brk-26",
            node_id="node-brk-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            accounting_basis=KpiAccountingBasis.NON_GAAP,
            revision=2,
            supersedes_id=r1.kpi_definition_revision_id,
        )
        persist_kpi_definition_comparability_revision(
            conn,
            KpiDefinitionComparabilityRevision(
                comparability_revision_id="kcr-brk-redef-1",
                idempotency_key="kcr-brk-redef-key-1",
                predecessor_definition_revision_id=r1.kpi_definition_revision_id,
                successor_definition_revision_id=r2.kpi_definition_revision_id,
                revision=1,
                relation_kind=KpiDefinitionRelationKind.REDEFINED,
                disposition=KpiDefinitionComparabilityDisposition.NOT_COMPARABLE,
                reason_code="gmv_definition_changed",
                reviewed_by="analyst:test",
                source_document_version_id="doc-brk-26",
                source_evidence_node_id="node-brk-26",
                source_locator=_LOCATOR,
                effective_at=datetime(2026, 1, 1, tzinfo=UTC),
                knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
            ),
        )
        _persist_bound_fact(
            conn,
            ticker="BRK",
            def_id=201,
            def_name="Platform GMV",
            rev=r1,
            doc_id=20,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1000"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="BRK",
            def_id=201,
            def_name="Platform GMV",
            rev=r2,
            doc_id=21,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("600"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        labels_full = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4", "2026 Q1"]
        series = kpi_series_for(conn, "BRK", "Platform GMV", labels_full, labels_full)
        assert series is not None
        assert series.comparability_status == "eligible_with_break"
        assert len(series.breaks) == 1
        assert series.breaks[0].disposition == "not_comparable"
        assert series.breaks[0].relation_kind == "redefined"
        # Refuses fake -40.0% YoY across the unbridged definition break!
        assert series.yoy_pct_full is not None
        assert series.yoy_pct_full[-1] is None
        assert series.yoy_status_full[-1] == "not_comparable"

        fin = FinancialsSection(
            status=SectionStatus.OK,
            quarter_labels=labels_full,
            quarter_labels_full=labels_full,
            kpi_chart_series=[series],
        )
        buf = StringIO()
        _kpi_series_yoy_panel(buf, fin)
        html_out = buf.getvalue()
        assert "-40.0%" not in html_out
        assert 'data-kpi-break="true"' in html_out
        assert 'data-break-disposition="not_comparable"' in html_out
        assert 'data-kpi-break-note="true"' in html_out
        assert ">break<" in html_out

        # Ledger comparability + renderer fail-closed check.
        ledger_comp = resolve_ledger_kpi_comparability(conn, kpi_definition_id=201)
        assert ledger_comp is not None
        assert ledger_comp.comparable_yoy_status == "not_comparable"
        assert ledger_comp.comparable_yoy_pct is None
        row = KpiLedgerRow(
            name="Platform GMV",
            tier="tier_1",
            unit="millions",
            kpi_definition_id=201,
            history=[("2025-03-31", 1000.0), ("2026-03-31", 600.0)],
            current_status="green",
            comparability_status=ledger_comp.status,
            segments=ledger_comp.segments,
            breaks=ledger_comp.breaks,
            comparable_yoy_delta=ledger_comp.comparable_yoy_delta,
            comparable_yoy_pct=ledger_comp.comparable_yoy_pct,
            comparable_yoy_status=ledger_comp.comparable_yoy_status,
            comparable_yoy_bridge_kind=ledger_comp.comparable_yoy_bridge_kind,
            comparable_yoy_reason=ledger_comp.comparable_yoy_reason,
        )
        delta_lbl, delta_sign, _ = kpi_comparable_trend_delta(row)
        assert delta_lbl == "uncomparable"
        assert delta_sign == "warn"

        ledger_buf = StringIO()
        _kpi_ledger_row(ledger_buf, row, date(2026, 5, 10), ticker="BRK")
        ledger_html = ledger_buf.getvalue()
        assert "uncomparable" in ledger_html
        assert "-40%" not in ledger_html
        assert 'data-kpi-break="true"' in ledger_html

        # KPI strip tile does not subtract across a break when the latest segment has 1 point.
        tiles = select_kpi_strip([row], n=4)
        assert len(tiles) == 1
        assert tiles[0].delta_display is None
        tile_buf = StringIO()
        _kpi_tile(tile_buf, tiles[0], "BRK")
        assert 'data-kpi-break="true"' in tile_buf.getvalue()
    finally:
        conn.close()


def test_split_scale_and_recast_bridges_render_segmented_charts_and_honest_yoy(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "bridged_charts.db")
    try:
        _seed_issuer(
            conn, ticker="SPLT", issuer_id="iss-splt", entity_id="ent-splt", company_name="Split Co"
        )
        _seed_doc(
            conn,
            doc_id=30,
            ticker="SPLT",
            issuer_id="iss-splt",
            period_end="2025-03-31",
            doc_version_id="doc-splt-25",
            node_id="node-splt-25",
            sha_hex="5",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=31,
            ticker="SPLT",
            issuer_id="iss-splt",
            period_end="2026-03-31",
            doc_version_id="doc-splt-26",
            node_id="node-splt-26",
            sha_hex="6",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        # --- Pattern A: 2:1 SplitScaleBridge ---
        _seed_kpi_def(conn, def_id=301, ticker="SPLT", name="FCF per Share", unit=Unit.ACTUAL)
        r_pre = _persist_def_rev(
            conn,
            rev_id="splt-fcfps-r1",
            def_id=301,
            entity_id="ent-splt",
            label="FCF per Share (pre-split)",
            doc_version_id="doc-splt-25",
            node_id="node-splt-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            unit_key=Unit.ACTUAL,
            unit_scale=KpiUnitScale.NONE,
        )
        r_post = _persist_def_rev(
            conn,
            rev_id="splt-fcfps-r2",
            def_id=301,
            entity_id="ent-splt",
            label="FCF per Share (post-split)",
            doc_version_id="doc-splt-26",
            node_id="node-splt-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            unit_key=Unit.ACTUAL,
            unit_scale=KpiUnitScale.NONE,
            revision=2,
            supersedes_id=r_pre.kpi_definition_revision_id,
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=301,
            def_name="FCF per Share",
            rev=r_pre,
            doc_id=30,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("100"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=301,
            def_name="FCF per Share",
            rev=r_pre,
            doc_id=30,
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("104"),
            knowledge_at=datetime(2025, 8, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=301,
            def_name="FCF per Share",
            rev=r_post,
            doc_id=31,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("55"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=301,
            def_name="FCF per Share",
            rev=r_post,
            doc_id=31,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("57.2"),
            knowledge_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        admit_split_scale_bridge(
            conn,
            predecessor_definition_revision_id=r_pre.kpi_definition_revision_id,
            successor_definition_revision_id=r_post.kpi_definition_revision_id,
            scale_numerator=2,
            scale_denominator=1,
            applies_to="per_share_divide",
            effective_date="2026-01-01",
            comparative_period_end="2025-03-31",
            fiscal_period_type=FiscalPeriodType.Q1,
            successor_comparative_value=Decimal("50"),
            source_document_version_id="doc-splt-26",
            source_evidence_node_id="node-splt-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        chart = resolve_kpi_chart_series(
            conn,
            kpi_definition_id=301,
            effective_at=_KNOWN_NOW,
            known_at=_KNOWN_NOW,
            apply_split_adjustment=True,
        )
        assert len(chart.segments) == 2
        assert len(chart.breaks) == 1
        svg = segmented_kpi_chart(chart, title="FCF per Share")
        # Two disconnected <path> segments (never one continuous path across split),
        # pre-split segment dashed, plus typed vertical break line.
        assert svg.count('class="cv2-line"') == 2
        assert 'data-segment-index="0" data-segment-transformed="true"' in svg
        assert 'data-segment-index="1" data-segment-transformed="false"' in svg
        assert 'data-kpi-break="true"' in svg
        assert 'data-break-kind="scale_ratio"' in svg
        assert 'stroke-dasharray="6 4"' in svg

        # Sparkline also disconnects the two segments with a break marker.
        sp = sparkline(
            [50.0, 52.0, 55.0, 57.2],
            size=SparklineSize.KPI,
            segments=[[50.0, 52.0], [55.0, 57.2]],
            break_labels=[chart.breaks[0].label],
        )
        assert sp.count('class="ws-spark-line"') == 2
        assert 'data-kpi-break="true"' in sp

        # Aligned KPI projection bridges Q1'26 YoY to +10.0% (55 vs 50), not -45.0%!
        labels_full = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4", "2026 Q1", "2026 Q2"]
        series = kpi_series_for(conn, "SPLT", "FCF per Share", labels_full, labels_full)
        assert series is not None
        assert series.yoy_pct_full is not None
        assert series.yoy_pct_full[4] == pytest.approx(10.0)
        assert series.yoy_pct_full[5] == pytest.approx(10.0)
        assert series.yoy_status_full[4] == "comparable_with_bridge"

        fin = FinancialsSection(
            status=SectionStatus.OK,
            quarter_labels=labels_full,
            quarter_labels_full=labels_full,
            kpi_chart_series=[series],
        )
        buf = StringIO()
        _kpi_series_yoy_panel(buf, fin)
        panel_html = buf.getvalue()
        assert "+10.0%" in panel_html
        assert "-45.0%" not in panel_html
        assert ">bridged<" in panel_html
        assert 'data-segmented-kpi-chart="true"' in panel_html

        # --- Pattern B: RecastComparativeBridge ---
        _seed_kpi_def(conn, def_id=302, ticker="SPLT", name="Total Billings", unit=Unit.MILLIONS)
        _seed_kpi_def(
            conn, def_id=303, ticker="SPLT", name="Continuing Billings", unit=Unit.MILLIONS
        )
        r_old = _persist_def_rev(
            conn,
            rev_id="splt-bill-old-r1",
            def_id=302,
            entity_id="ent-splt",
            label="Total Billings",
            doc_version_id="doc-splt-25",
            node_id="node-splt-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        r_recast = _persist_def_rev(
            conn,
            rev_id="splt-bill-cont-r1",
            def_id=303,
            entity_id="ent-splt",
            label="Continuing Billings",
            doc_version_id="doc-splt-26",
            node_id="node-splt-26",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            consolidation_scope=KpiConsolidationScope.OTHER,
            dimensions={"continuing_ops": "ex_divested"},
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=302,
            def_name="Total Billings",
            rev=r_old,
            doc_id=30,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("2200"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=302,
            def_name="Total Billings",
            rev=r_old,
            doc_id=30,
            period_end=datetime(2025, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("2400"),
            knowledge_at=datetime(2025, 8, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=303,
            def_name="Continuing Billings",
            rev=r_recast,
            doc_id=31,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1800"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            period_role=KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
            publication_lane=KpiPublicationLane.COMPARATOR,
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=303,
            def_name="Continuing Billings",
            rev=r_recast,
            doc_id=31,
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("1980"),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="SPLT",
            def_id=303,
            def_name="Continuing Billings",
            rev=r_recast,
            doc_id=31,
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q2,
            value=Decimal("2100"),
            knowledge_at=datetime(2026, 8, 2, tzinfo=UTC),
        )
        admit_recast_comparative_bridge(
            conn,
            predecessor_definition_revision_id=r_old.kpi_definition_revision_id,
            successor_definition_revision_id=r_recast.kpi_definition_revision_id,
            recast_reason="divestiture",
            scope_change_summary="Divested unit excluded from continuing operations.",
            comparative_periods_present=("2025-03-31",),
            source_document_version_id="doc-splt-26",
            source_evidence_node_id="node-splt-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )

        comp = resolve_aligned_kpi_comparability(
            conn,
            kpi_definition_id=303,
            axis_labels=labels_full,
            axis_kind="quarterly",
            effective_at=_KNOWN_NOW,
            known_at=_KNOWN_NOW,
        )
        assert comp is not None
        assert len(comp.segments) == 2
        assert len(comp.breaks) == 1
        assert comp.breaks[0].bridge_kind == "recast_comparative_only"
        # Q1 2026 uses recast comparative (1980 vs 1800 -> +10%); Q2 2026 fails closed (None).
        assert comp.yoy_pct_full[4] == pytest.approx(10.0)
        assert comp.yoy_status_full[4] == "comparable"
        assert comp.yoy_pct_full[5] is None
        assert comp.yoy_status_full[5] == "recast_comparative_missing"
    finally:
        conn.close()


def test_annual_kpi_panel_honors_comparability_breaks_and_gates_cagr() -> None:
    annual = AnnualKpiSeries(
        name="Capital Ratio",
        unit="millions",
        years=[2023, 2024, 2025],
        values=[100.0, 120.0, 80.0],
        comparability_status="eligible_with_break",
        segments=[
            KpiSeriesSegment(
                segment_index=0,
                reported_label="Old Capital",
                cadence_kind="annual",
                points=[("2023-12-31", 100.0), ("2024-12-31", 120.0)],
            ),
            KpiSeriesSegment(
                segment_index=1,
                reported_label="New Capital",
                cadence_kind="annual",
                points=[("2025-12-31", 80.0)],
            ),
        ],
        breaks=[
            KpiSeriesBreak(
                period_end="2025-01-01",
                period_label="2025",
                relation_kind="redefined",
                disposition="not_comparable",
                bridge_kind=None,
                label="redefined (not_comparable): Old Capital -> New Capital",
                predecessor_revision_id="rev-1",
                successor_revision_id="rev-2",
                comparability_revision_id="kcr-1",
                source_locator={"pdf_page": 12},
            )
        ],
        yoy_pct_full=[None, 20.0, None],
        yoy_status_full=["prior_period_missing", "comparable", "not_comparable"],
        yoy_reasons_full=[None, None, "YoY unavailable (not_comparable): basis changed"],
        comparison_eligible_edges=[False, True, False],
    )
    fin = FinancialsSection(
        status=SectionStatus.OK,
        annual_kpi_chart_series=[annual],
        annual_kpi_years=[2023, 2024, 2025],
    )
    buf = StringIO()
    _annual_kpi_series_yoy_panel(buf, fin)
    html_out = buf.getvalue()
    assert "+20.0%" in html_out
    # Post-break 2025 YoY (-33.3%) and 2y CAGR (-10.6%) must NOT be rendered!
    assert "-33.3%" not in html_out
    assert "-10.6%" not in html_out
    assert 'data-kpi-break="true"' in html_out
    assert 'data-kpi-break-note="true"' in html_out


def test_rename_overlap_bridge_continuous_in_chart_and_yoy(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "rename_chart.db")
    try:
        _seed_issuer(
            conn, ticker="RNM", issuer_id="iss-rnm", entity_id="ent-rnm", company_name="Rename Inc"
        )
        _seed_doc(
            conn,
            doc_id=40,
            ticker="RNM",
            issuer_id="iss-rnm",
            period_end="2025-09-30",
            doc_version_id="doc-rnm-25",
            node_id="node-rnm-25",
            sha_hex="7",
            recorded_at=datetime(2025, 11, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=41,
            ticker="RNM",
            issuer_id="iss-rnm",
            period_end="2026-09-30",
            doc_version_id="doc-rnm-26",
            node_id="node-rnm-26",
            sha_hex="8",
            recorded_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=401, ticker="RNM", name="Active Accounts", unit=Unit.MILLIONS)
        r1 = _persist_def_rev(
            conn,
            rev_id="rnm-r1",
            def_id=401,
            entity_id="ent-rnm",
            label="Active Users",
            doc_version_id="doc-rnm-25",
            node_id="node-rnm-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 11, 2, tzinfo=UTC),
        )
        r2 = _persist_def_rev(
            conn,
            rev_id="rnm-r2",
            def_id=401,
            entity_id="ent-rnm",
            label="Active Accounts",
            doc_version_id="doc-rnm-26",
            node_id="node-rnm-26",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 11, 2, tzinfo=UTC),
            revision=2,
            supersedes_id=r1.kpi_definition_revision_id,
        )
        _persist_bound_fact(
            conn,
            ticker="RNM",
            def_id=401,
            def_name="Active Accounts",
            rev=r1,
            doc_id=40,
            period_end=datetime(2025, 9, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q3,
            value=Decimal("500"),
            knowledge_at=datetime(2025, 11, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="RNM",
            def_id=401,
            def_name="Active Accounts",
            rev=r2,
            doc_id=41,
            period_end=datetime(2026, 9, 30, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q3,
            value=Decimal("600"),
            knowledge_at=datetime(2026, 11, 2, tzinfo=UTC),
        )
        admit_verified_rename_comparability(
            conn,
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id=r2.kpi_definition_revision_id,
            comparative_period_end="2025-09-30",
            fiscal_period_type=FiscalPeriodType.Q3,
            successor_comparative_value=Decimal("500"),
            source_document_version_id="doc-rnm-26",
            source_evidence_node_id="node-rnm-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 11, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 11, 2, tzinfo=UTC),
        )
        chart = load_kpi_chart_series(
            "RNM", "Active Accounts", conn=conn, effective_at=_KNOWN_NOW, known_at=_KNOWN_NOW
        )
        assert chart is not None
        assert chart.status is KpiSeriesReadStatus.ELIGIBLE
        assert len(chart.segments) == 1
        assert len(chart.segments[0].points) == 2
        assert chart.breaks == ()

        yoy = load_kpi_comparable_yoy(
            "RNM",
            "Active Accounts",
            "2026-09-30",
            conn=conn,
            effective_at=_KNOWN_NOW,
            known_at=_KNOWN_NOW,
        )
        assert yoy is not None
        assert yoy.status is KpiComparableYoYStatus.COMPARABLE
        assert yoy.bridge_kind == "rename_overlap"
        assert yoy.yoy_pct == Decimal("0.2")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Seam 2: Write / Ingest Consumer Wiring
# ---------------------------------------------------------------------------


def test_persist_manifest_wires_comparative_overlap_and_fails_closed_on_mismatch(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "ingest_overlap.db")
    try:
        _seed_issuer(
            conn, ticker="ING", issuer_id="iss-ing", entity_id="ent-ing", company_name="Ingest Co"
        )
        _seed_doc(
            conn,
            doc_id=50,
            ticker="ING",
            issuer_id="iss-ing",
            period_end="2025-09-30",
            doc_version_id="doc-ing-q3-25",
            node_id="node-ing-q3-25",
            sha_hex="a",
            recorded_at=datetime(2025, 11, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=51,
            ticker="ING",
            issuer_id="iss-ing",
            period_end="2026-09-30",
            doc_version_id="doc-ing-q3-26",
            node_id="node-ing-q3-26",
            sha_hex="b",
            recorded_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        # Step 1: Ingest Q3 2025 under original reported label.
        res_25 = persist_manifest(
            conn,
            run_id="run-ing-25",
            manifest=KpiExtractionManifest(
                ticker="ING",
                period_end=datetime(2025, 9, 30),
                fiscal_period_type=FiscalPeriodType.Q3,
                source_doc_id=50,
                primary_source=SourceType.IR_DOC,
                extracted_by="ir_press_release_readout",
                auto_admit=True,
                reviewed_by="pipeline:test",
                values=[
                    KpiValue(
                        name="Subscription Revenue",
                        metric_name_as_reported="Cloud Subscription Revenue",
                        value=Decimal("500"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Cloud Subscription Revenue was $500M in Q3 2025.",
                        accounting_basis=KpiAccountingBasis.GAAP,
                    ),
                    KpiValue(
                        name="Net Retention",
                        metric_name_as_reported="Legacy NRR",
                        value=Decimal("115"),
                        unit=Unit.PERCENT,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Legacy NRR was 115% in Q3 2025.",
                        accounting_basis=KpiAccountingBasis.MANAGEMENT,
                    ),
                ],
            ),
        )
        assert res_25.inserted == 2
        sub_def_id = int(
            conn.execute(
                "SELECT id FROM kpi_definitions WHERE ticker='ING' AND name='Subscription Revenue'"
            ).fetchone()[0]
        )
        nrr_def_id = int(
            conn.execute(
                "SELECT id FROM kpi_definitions WHERE ticker='ING' AND name='Net Retention'"
            ).fetchone()[0]
        )
        sub_r1 = current_kpi_definition_revision(conn, kpi_definition_id=sub_def_id)
        nrr_r1 = current_kpi_definition_revision(conn, kpi_definition_id=nrr_def_id)
        assert sub_r1 is not None and nrr_r1 is not None

        # Step 2: Ingest Q3 2026 with:
        #   (a) Subscription Revenue renamed + matching comparative value in `comparative_values`
        #       -> auto-synthesizes ComparativeOverlapCandidate -> admits CONTINUOUS RenameOverlapBridge!
        #   (b) Net Retention renamed + MISMATCHED comparative_overlap (108 vs 115)
        #       -> fails closed to REDEFINED / NOT_COMPARABLE (never fuzzy-merges!).
        res_26 = persist_manifest(
            conn,
            run_id="run-ing-26",
            manifest=KpiExtractionManifest(
                ticker="ING",
                period_end=datetime(2026, 9, 30),
                fiscal_period_type=FiscalPeriodType.Q3,
                source_doc_id=51,
                primary_source=SourceType.IR_DOC,
                extracted_by="ir_press_release_readout",
                auto_admit=True,
                reviewed_by="pipeline:test",
                values=[
                    KpiValue(
                        name="Subscription Revenue",
                        metric_name_as_reported="Platform Subscription Revenue",
                        value=Decimal("600"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Platform Subscription Revenue was $600M in Q3 2026 vs $500M in Q3 2025.",
                        accounting_basis=KpiAccountingBasis.GAAP,
                    ),
                    KpiValue(
                        name="Net Retention",
                        metric_name_as_reported="Expanded NRR",
                        value=Decimal("120"),
                        unit=Unit.PERCENT,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Expanded NRR was 120% in Q3 2026 vs 108% in Q3 2025.",
                        accounting_basis=KpiAccountingBasis.MANAGEMENT,
                        comparative_overlap=ComparativeOverlapCandidate(
                            period_end="2025-09-30",
                            fiscal_period_type=FiscalPeriodType.Q3,
                            successor_comparative_value=Decimal("108"),
                        ),
                    ),
                ],
                comparative_values=[
                    KpiComparativeValue(
                        name="Subscription Revenue",
                        period_end=datetime(2025, 9, 30),
                        fiscal_period_type=FiscalPeriodType.Q3,
                        value=Decimal("500"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Comparative Q3 2025 Platform Subscription Revenue was $500M.",
                    )
                ],
            ),
        )
        assert res_26.inserted == 3  # 2 current + 1 comparative row

        sub_r2 = current_kpi_definition_revision(conn, kpi_definition_id=sub_def_id)
        nrr_r2 = current_kpi_definition_revision(conn, kpi_definition_id=nrr_def_id)
        assert sub_r2 is not None and sub_r2.revision == 2
        assert nrr_r2 is not None and nrr_r2.revision == 2

        sub_edge = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=sub_r1.kpi_definition_revision_id,
            successor_definition_revision_id=sub_r2.kpi_definition_revision_id,
        )
        assert sub_edge is not None
        assert sub_edge.relation_kind is KpiDefinitionRelationKind.RENAMED
        assert sub_edge.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
        assert isinstance(sub_edge.bridge_spec, RenameOverlapBridge)

        nrr_edge = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=nrr_r1.kpi_definition_revision_id,
            successor_definition_revision_id=nrr_r2.kpi_definition_revision_id,
        )
        assert nrr_edge is not None
        assert nrr_edge.relation_kind is KpiDefinitionRelationKind.REDEFINED
        assert nrr_edge.disposition is KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
        assert nrr_edge.reason_code == "rename_overlap_mismatch"
        assert nrr_edge.bridge_spec is None

        # Verify end-to-end read consumer behavior over both ingested KPIs.
        future_now = datetime(2028, 1, 1, tzinfo=UTC)
        sub_yoy = load_kpi_comparable_yoy(
            "ING",
            "Subscription Revenue",
            "2026-09-30",
            conn=conn,
            effective_at=future_now,
            known_at=future_now,
        )
        assert sub_yoy is not None
        assert sub_yoy.status is KpiComparableYoYStatus.COMPARABLE
        assert sub_yoy.yoy_pct == Decimal("0.2")

        nrr_yoy = load_kpi_comparable_yoy(
            "ING",
            "Net Retention",
            "2026-09-30",
            conn=conn,
            effective_at=future_now,
            known_at=future_now,
        )
        assert nrr_yoy is not None
        assert nrr_yoy.status is KpiComparableYoYStatus.NOT_COMPARABLE
        assert nrr_yoy.yoy_pct is None
    finally:
        conn.close()


def test_persist_manifest_admits_recast_and_split_bridges_and_rejects_bad_split(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "ingest_bridges.db")
    try:
        _seed_issuer(
            conn, ticker="BRG", issuer_id="iss-brg", entity_id="ent-brg", company_name="Bridge Co"
        )
        _seed_doc(
            conn,
            doc_id=60,
            ticker="BRG",
            issuer_id="iss-brg",
            period_end="2025-03-31",
            doc_version_id="doc-brg-25",
            node_id="node-brg-25",
            sha_hex="c",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=61,
            ticker="BRG",
            issuer_id="iss-brg",
            period_end="2026-03-31",
            doc_version_id="doc-brg-26",
            node_id="node-brg-26",
            sha_hex="d",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        # Seed initial Q1 2025 for both recast KPI and split KPI.
        persist_manifest(
            conn,
            run_id="run-brg-25",
            manifest=KpiExtractionManifest(
                ticker="BRG",
                period_end=datetime(2025, 3, 31),
                fiscal_period_type=FiscalPeriodType.Q1,
                source_doc_id=60,
                primary_source=SourceType.IR_DOC,
                extracted_by="ir_press_release_readout",
                auto_admit=True,
                reviewed_by="pipeline:test",
                values=[
                    KpiValue(
                        name="Operating Revenue",
                        metric_name_as_reported="Total Operating Revenue",
                        value=Decimal("2200"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Total Operating Revenue was $2,200M.",
                        accounting_basis=KpiAccountingBasis.GAAP,
                    ),
                    KpiValue(
                        name="EPS Adjusted",
                        metric_name_as_reported="EPS Adjusted (pre-split)",
                        value=Decimal("4.00"),
                        unit=Unit.ACTUAL,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="EPS Adjusted was $4.00.",
                        accounting_basis=KpiAccountingBasis.NON_GAAP,
                    ),
                ],
            ),
        )
        rev_def_id = int(
            conn.execute(
                "SELECT id FROM kpi_definitions WHERE ticker='BRG' AND name='Operating Revenue'"
            ).fetchone()[0]
        )
        eps_def_id = int(
            conn.execute(
                "SELECT id FROM kpi_definitions WHERE ticker='BRG' AND name='EPS Adjusted'"
            ).fetchone()[0]
        )
        rev_r1 = current_kpi_definition_revision(conn, kpi_definition_id=rev_def_id)
        eps_r1 = current_kpi_definition_revision(conn, kpi_definition_id=eps_def_id)
        assert rev_r1 is not None and eps_r1 is not None

        # Ingest Q1 2026 with RecastBridgeIntent + comparative_values AND valid 2:1 SplitScaleBridgeIntent.
        persist_manifest(
            conn,
            run_id="run-brg-26",
            manifest=KpiExtractionManifest(
                ticker="BRG",
                period_end=datetime(2026, 3, 31),
                fiscal_period_type=FiscalPeriodType.Q1,
                source_doc_id=61,
                primary_source=SourceType.IR_DOC,
                extracted_by="ir_press_release_readout",
                auto_admit=True,
                reviewed_by="pipeline:test",
                values=[
                    KpiValue(
                        name="Operating Revenue",
                        metric_name_as_reported="Operating Revenue (Continuing Ops)",
                        value=Decimal("1980"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Continuing Operating Revenue was $1,980M vs recast $1,800M.",
                        accounting_basis=KpiAccountingBasis.GAAP,
                        consolidation_scope=KpiConsolidationScope.OTHER,
                        dimensions={"continuing_ops": "ex_spinoff"},
                        recast_bridge=RecastBridgeIntent(
                            recast_reason="spinoff_continuing_ops",
                            scope_change_summary="Spun-off unit reclassified to discontinued ops.",
                        ),
                    ),
                    KpiValue(
                        name="EPS Adjusted",
                        metric_name_as_reported="EPS Adjusted (post-split)",
                        value=Decimal("2.20"),
                        unit=Unit.ACTUAL,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Post-split EPS Adjusted was $2.20 vs $2.00 split-adjusted.",
                        accounting_basis=KpiAccountingBasis.NON_GAAP,
                        split_scale_bridge=SplitScaleBridgeIntent(
                            scale_numerator=2,
                            scale_denominator=1,
                            applies_to="per_share_divide",
                            effective_date="2026-01-01",
                            comparative_overlap=ComparativeOverlapCandidate(
                                period_end="2025-03-31",
                                fiscal_period_type=FiscalPeriodType.Q1,
                                successor_comparative_value=Decimal("2.00"),
                            ),
                        ),
                    ),
                ],
                comparative_values=[
                    KpiComparativeValue(
                        name="Operating Revenue",
                        period_end=datetime(2025, 3, 31),
                        fiscal_period_type=FiscalPeriodType.Q1,
                        value=Decimal("1800"),
                        unit=Unit.MILLIONS,
                        currency=Currency.USD,
                        locator=_FACT_LOCATOR,
                        source_excerpt="Recast Q1 2025 Continuing Operating Revenue was $1,800M.",
                    )
                ],
            ),
        )
        rev_r2 = current_kpi_definition_revision(conn, kpi_definition_id=rev_def_id)
        eps_r2 = current_kpi_definition_revision(conn, kpi_definition_id=eps_def_id)
        assert rev_r2 is not None and eps_r2 is not None

        recast_edge = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=rev_r1.kpi_definition_revision_id,
            successor_definition_revision_id=rev_r2.kpi_definition_revision_id,
        )
        assert recast_edge is not None
        assert isinstance(recast_edge.bridge_spec, RecastComparativeBridge)
        assert recast_edge.bridge_spec.comparative_periods_present == ("2025-03-31",)

        split_edge = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=eps_r1.kpi_definition_revision_id,
            successor_definition_revision_id=eps_r2.kpi_definition_revision_id,
        )
        assert split_edge is not None
        assert isinstance(split_edge.bridge_spec, SplitScaleBridge)

        future_now = datetime(2028, 1, 1, tzinfo=UTC)
        recast_yoy = load_kpi_comparable_yoy(
            "BRG",
            "Operating Revenue",
            "2026-03-31",
            conn=conn,
            effective_at=future_now,
            known_at=future_now,
        )
        assert recast_yoy is not None
        assert recast_yoy.status is KpiComparableYoYStatus.COMPARABLE
        assert recast_yoy.bridge_kind == "recast_comparative_only"
        assert recast_yoy.yoy_pct == Decimal("0.1")

        split_yoy = load_kpi_comparable_yoy(
            "BRG",
            "EPS Adjusted",
            "2026-03-31",
            conn=conn,
            effective_at=future_now,
            known_at=future_now,
        )
        assert split_yoy is not None
        assert split_yoy.status is KpiComparableYoYStatus.COMPARABLE_WITH_BRIDGE
        assert split_yoy.bridge_kind == "scale_ratio"
        assert split_yoy.yoy_pct == Decimal("0.1")

        # Bad split witness (claims 2:1 split but comparative value is 3.50 instead of 2.00) fails closed.
        _seed_doc(
            conn,
            doc_id=62,
            ticker="BRG",
            issuer_id="iss-brg",
            period_end="2027-03-31",
            doc_version_id="doc-brg-27",
            node_id="node-brg-27",
            sha_hex="e",
            recorded_at=datetime(2027, 5, 1, tzinfo=UTC),
        )
        with pytest.raises(OverlapVerificationError):
            persist_manifest(
                conn,
                run_id="run-brg-bad-split",
                manifest=KpiExtractionManifest(
                    ticker="BRG",
                    period_end=datetime(2027, 3, 31),
                    fiscal_period_type=FiscalPeriodType.Q1,
                    source_doc_id=62,
                    primary_source=SourceType.IR_DOC,
                    extracted_by="ir_press_release_readout",
                    auto_admit=True,
                    reviewed_by="pipeline:test",
                    values=[
                        KpiValue(
                            name="EPS Adjusted",
                            metric_name_as_reported="EPS Adjusted (split-2)",
                            value=Decimal("1.10"),
                            unit=Unit.ACTUAL,
                            currency=Currency.USD,
                            locator=_FACT_LOCATOR,
                            source_excerpt="EPS Adjusted was $1.10.",
                            accounting_basis=KpiAccountingBasis.NON_GAAP,
                            split_scale_bridge=SplitScaleBridgeIntent(
                                scale_numerator=2,
                                scale_denominator=1,
                                applies_to="per_share_divide",
                                effective_date="2027-01-01",
                                comparative_overlap=ComparativeOverlapCandidate(
                                    period_end="2026-03-31",
                                    fiscal_period_type=FiscalPeriodType.Q1,
                                    successor_comparative_value=Decimal("1.90"),
                                ),
                            ),
                        )
                    ],
                ),
                commit=False,
            )
    finally:
        conn.close()


def test_batch_admission_forwards_comparative_overlap_and_reviewed_builder_fails_closed(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = _open_db(tmp_path, migrated_db, "batch_overlap.db")
    try:
        _seed_issuer(
            conn, ticker="BCH", issuer_id="iss-bch", entity_id="ent-bch", company_name="Batch Co"
        )
        _seed_doc(
            conn,
            doc_id=70,
            ticker="BCH",
            issuer_id="iss-bch",
            period_end="2025-03-31",
            doc_version_id="doc-bch-25",
            node_id="node-bch-25",
            sha_hex="1",
            recorded_at=datetime(2025, 5, 1, tzinfo=UTC),
        )
        _seed_doc(
            conn,
            doc_id=71,
            ticker="BCH",
            issuer_id="iss-bch",
            period_end="2026-03-31",
            doc_version_id="doc-bch-26",
            node_id="node-bch-26",
            sha_hex="2",
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        _seed_kpi_def(conn, def_id=701, ticker="BCH", name="Active Wallets", unit=Unit.MILLIONS)
        r1 = _persist_def_rev(
            conn,
            rev_id="bch-wallets-r1",
            def_id=701,
            entity_id="ent-bch",
            label="Legacy Wallets",
            doc_version_id="doc-bch-25",
            node_id="node-bch-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )
        _persist_bound_fact(
            conn,
            ticker="BCH",
            def_id=701,
            def_name="Active Wallets",
            rev=r1,
            doc_id=70,
            period_end=datetime(2025, 3, 31, tzinfo=UTC),
            fiscal_period_type=FiscalPeriodType.Q1,
            value=Decimal("80"),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
        )

        # Insert an unreviewed Q1 2026 row under the renamed definition + same-document comparator row.
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "currency, source_doc_id, confidence, extracted_by, locator, source_excerpt) "
            "VALUES (9001, 'BCH', '2026-03-31', 'Q1', 701, 96, 'millions', 'USD', 71, 0.95, "
            "'ir_press_release_readout', ?, 'Active Wallets reached $96M vs $80M.')",
            (json.dumps(_LOCATOR),),
        )
        # Note: existing admitted signature on def 701 is "Legacy Wallets", so
        # batch admission defaults to that unless a new revision label is introduced.
        # Test explicit comparative_overlaps forwarding on a fresh def 702 where rev 1
        # exists under "Old Label" (from a different revision) and unreviewed fact admits under "New Label".
        _seed_kpi_def(conn, def_id=702, ticker="BCH", name="New Label", unit=Unit.MILLIONS)
        r_old = _persist_def_rev(
            conn,
            rev_id="bch-newlbl-r1",
            def_id=702,
            entity_id="ent-bch",
            label="Old Label",
            doc_version_id="doc-bch-25",
            node_id="node-bch-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            accounting_basis=KpiAccountingBasis.MANAGEMENT,
        )
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "currency, source_doc_id, confidence, extracted_by, locator, source_excerpt) "
            "VALUES (9002, 'BCH', '2026-03-31', 'Q1', 702, 120, 'millions', 'USD', 71, 0.95, "
            "'ir_press_release_readout', ?, 'New Label was $120M vs $100M.')",
            (json.dumps(_LOCATOR),),
        )
        # Also test same-document comparator discovery on def 703.
        _seed_kpi_def(conn, def_id=703, ticker="BCH", name="Renamed Volume", unit=Unit.MILLIONS)
        r_vol_old = _persist_def_rev(
            conn,
            rev_id="bch-vol-r1",
            def_id=703,
            entity_id="ent-bch",
            label="Prior Volume",
            doc_version_id="doc-bch-25",
            node_id="node-bch-25",
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2025, 5, 2, tzinfo=UTC),
            accounting_basis=KpiAccountingBasis.MANAGEMENT,
        )
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "currency, source_doc_id, confidence, extracted_by, locator, source_excerpt) "
            "VALUES (9003, 'BCH', '2026-03-31', 'Q1', 703, 330, 'millions', 'USD', 71, 0.95, "
            "'ir_press_release_readout', ?, 'Renamed Volume was $330M.')",
            (json.dumps(_LOCATOR),),
        )
        conn.execute(
            "INSERT INTO kpi_facts "
            "(id, ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
            "currency, source_doc_id, confidence, extracted_by, locator, source_excerpt) "
            "VALUES (9004, 'BCH', '2025-03-31', 'Q1', 703, 300, 'millions', 'USD', 71, 0.95, "
            "'ir_press_release_readout', ?, 'Prior-year comparative was $300M.')",
            (json.dumps(_LOCATOR),),
        )
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=9004,
            context=unclassified_kpi_context(
                metric_name_as_reported="Renamed Volume",
                reported_period_end=date(2025, 3, 31),
            ).model_copy(
                update={
                    "status": KpiSemanticStatus.ADMITTED,
                    "reason_code": None,
                    "period_role": KpiPeriodRole.PRIOR_YEAR_COMPARATOR,
                    "publication_lane": KpiPublicationLane.COMPARATOR,
                    "accounting_basis": KpiAccountingBasis.MANAGEMENT,
                    "consolidation_scope": KpiConsolidationScope.CONSOLIDATED,
                    "unit_scale": KpiUnitScale.MILLIONS,
                }
            ),
            reviewed_by="analyst:test",
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        conn.commit()

        report = run_batch_kpi_admission(
            conn,
            tickers=("BCH",),
            as_of=date(2026, 12, 31),
            apply=True,
            knowledge_at=datetime(2026, 5, 3, tzinfo=UTC),
            comparative_overlaps={
                702: ComparativeOverlapCandidate(
                    period_end="2025-03-31",
                    fiscal_period_type=FiscalPeriodType.Q1,
                    predecessor_value=Decimal("100"),
                    successor_comparative_value=Decimal("100"),
                ),
            },
        )
        assert report.total_admitted_applied >= 2
        r_new = current_kpi_definition_revision(conn, kpi_definition_id=702)
        assert r_new is not None and r_new.revision == 2
        edge_702 = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=r_old.kpi_definition_revision_id,
            successor_definition_revision_id=r_new.kpi_definition_revision_id,
        )
        assert edge_702 is not None
        assert edge_702.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
        assert isinstance(edge_702.bridge_spec, RenameOverlapBridge)

        # For def 703, same-doc comparator was discovered (value 300), but no predecessor fact
        # existed on r_vol_old -> fails closed cleanly as forward_label_renamed_unverified.
        r_vol_new = current_kpi_definition_revision(conn, kpi_definition_id=703)
        assert r_vol_new is not None and r_vol_new.revision == 2
        edge_703 = current_kpi_definition_comparability_revision(
            conn,
            predecessor_definition_revision_id=r_vol_old.kpi_definition_revision_id,
            successor_definition_revision_id=r_vol_new.kpi_definition_revision_id,
        )
        assert edge_703 is not None
        assert edge_703.reason_code == "forward_label_renamed_unverified"
        assert edge_703.bridge_spec is None

        # Verify issuer_fact_manifest.build_reviewed_comparability_from_overlap:
        # 1) Verified overlap against persisted predecessor fact on r1 (value 80) -> RenameOverlapBridge.
        ok_rev = build_reviewed_comparability_from_overlap(
            conn,
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id="bch-wallets-r2-candidate",
            comparative_overlap=ComparativeOverlapCandidate(
                period_end="2025-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                successor_comparative_value=Decimal("80"),
            ),
            source_document_version_id="doc-bch-26",
            source_evidence_node_id="node-bch-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert ok_rev.disposition is KpiDefinitionComparabilityDisposition.CONTINUOUS
        assert isinstance(ok_rev.bridge_spec, RenameOverlapBridge)

        # 2) Mismatched overlap (95 vs 80) -> fails closed as REDEFINED / NOT_COMPARABLE.
        bad_rev = build_reviewed_comparability_from_overlap(
            conn,
            predecessor_definition_revision_id=r1.kpi_definition_revision_id,
            successor_definition_revision_id="bch-wallets-r2-candidate",
            comparative_overlap=ComparativeOverlapCandidate(
                period_end="2025-03-31",
                fiscal_period_type=FiscalPeriodType.Q1,
                successor_comparative_value=Decimal("95"),
            ),
            source_document_version_id="doc-bch-26",
            source_evidence_node_id="node-bch-26",
            source_locator=_LOCATOR,
            reviewed_by="analyst:test",
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
            recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
        assert bad_rev.relation_kind is KpiDefinitionRelationKind.REDEFINED
        assert bad_rev.disposition is KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
        assert bad_rev.bridge_spec is None

        # 3) fail_on_mismatch=True raises OverlapVerificationError.
        with pytest.raises(OverlapVerificationError):
            build_reviewed_comparability_from_overlap(
                conn,
                predecessor_definition_revision_id=r1.kpi_definition_revision_id,
                successor_definition_revision_id="bch-wallets-r2-candidate",
                comparative_overlap=ComparativeOverlapCandidate(
                    period_end="2025-03-31",
                    fiscal_period_type=FiscalPeriodType.Q1,
                    successor_comparative_value=Decimal("95"),
                ),
                source_document_version_id="doc-bch-26",
                source_evidence_node_id="node-bch-26",
                source_locator=_LOCATOR,
                reviewed_by="analyst:test",
                effective_at=datetime(2026, 1, 1, tzinfo=UTC),
                knowledge_at=datetime(2026, 5, 2, tzinfo=UTC),
                recorded_at=datetime(2026, 5, 2, tzinfo=UTC),
                fail_on_mismatch=True,
            )
    finally:
        conn.close()
