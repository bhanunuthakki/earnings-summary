"""Exact source-reviewed native projection, with immutable legacy replay."""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.kpi_definition_revisions import (
    IssuerKpiDefinitionRevision,
    KpiDefinitionPeriodKind,
    KpiStockFlowBehavior,
    persist_kpi_definition_revision,
)
from pipeline.kpi_persistence import persist_kpi_value_at_exact_definition
from pipeline.kpi_semantics import KpiSemanticContext, persist_kpi_semantic_context
from pipeline.queries import open_db
from provenance.evidence_ledger import EvidenceLocator
from provenance.fact_plane_v2 import FactPlaneV2
from provenance.financial_fact_resolution import (
    prepare_reviewed_kpi_native_projection,
    resolve_fact_row,
)
from provenance.population_source_facts import (
    SourceFactDocumentScope,
    SourceFactPopulationRequest,
    SourceFactPopulationResult,
    native_source_observation_id,
    populate_source_fact_plane,
)

seed_resolved_kpi_fact = cast(
    Callable[[sqlite3.Connection], None],
    getattr(
        importlib.import_module("tests.test_kpi_semantic_dispositions"), "_seed_resolved_kpi_fact"
    ),
)
reviewed_definition = cast(
    Callable[..., IssuerKpiDefinitionRevision],
    getattr(importlib.import_module("tests.test_kpi_semantic_refresh_executor"), "_v7_definition"),
)

STAMP = datetime(2026, 10, 4, 12, tzinfo=UTC)
CUTOFF = STAMP + timedelta(hours=1)
RECORDED = STAMP + timedelta(hours=2)


@pytest.fixture
def reviewed(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> Generator[tuple[sqlite3.Connection, str, int, KpiSemanticContext], None, None]:
    conn = open_db(migrated_db(tmp_path / "native.db"))
    seed_resolved_kpi_fact(conn)
    conn.execute("UPDATE documents SET doc_type='ir_press_release' WHERE id=10")
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-nu",
            "reporting:nu",
            "issuer-nu",
            "legal_registrant",
            "Nu Holdings",
            STAMP.isoformat(sep=" "),
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-nu",
            "binding:nu",
            "issuer-nu",
            1,
            "issuer-nu",
            "reporting-nu",
            None,
            "selected",
            "deterministic",
            "test",
            "{}",
            0,
            STAMP.isoformat(sep=" "),
            STAMP.isoformat(sep=" "),
            STAMP.isoformat(sep=" "),
            None,
        ),
    )
    locator = EvidenceLocator(source_ref="ir_documents/NU/q4.pdf", page_number=1)
    wording = (
        "Q4 2024 | Figures in USD millions | Adjusted EBITDA | 1088 | approximately | Year Ended"
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-money",
            "node:money",
            1,
            "run-nu-q4",
            "root-nu-q4",
            None,
            "pdf_page",
            wording,
            locator.canonical_json,
            locator.canonical_sha256,
            STAMP.isoformat(sep=" "),
        ),
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id,ticker,name,unit,primary_source) VALUES (123,'NU','Adjusted EBITDA','millions','ir_doc')"
    )
    definition = reviewed_definition(
        kpi_definition_revision_id="definition-money",
        idempotency_key="definition:money",
        kpi_definition_id=123,
        reporting_entity_id="reporting-nu",
        reported_label="Adjusted EBITDA",
        reported_definition_text="Adjusted EBITDA",
        period_kind=KpiDefinitionPeriodKind.DURATION,
        stock_flow_behavior=KpiStockFlowBehavior.FLOW,
        accounting_basis="non_gaap",
        source_document_version_id="document-nu-q4",
        source_evidence_node_id="node-money",
        source_locator=json.loads(locator.canonical_json),
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    persist_kpi_definition_revision(conn, definition)
    context = KpiSemanticContext.model_validate(
        dict(
            metric_name_as_reported="Adjusted EBITDA",
            reported_period_start=date(2024, 10, 1),
            reported_period_end=date(2024, 12, 31),
            period_role="current",
            publication_lane="current_actual",
            accounting_basis="non_gaap",
            consolidation_scope="consolidated",
            unit_scale="millions",
            source_row_label="Adjusted EBITDA",
            source_column_header="Q4 2024",
            source_value_text="1088",
            source_precision={"kind": "exact"},
            status="admitted",
        )
    )
    result = persist_kpi_value_at_exact_definition(
        conn,
        ticker="NU",
        period_end=datetime(2024, 12, 31, tzinfo=UTC),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=10,
        kpi_definition_id=123,
        expected_definition_name="Adjusted EBITDA",
        value=Decimal("1088"),
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        locator=FactLocator(pdf_page=1, verbatim_snippet=wording),
        source_excerpt=wording,
        context=context,
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
        extracted_by="source_review:fixture",
    )
    resolve_fact_row(
        conn, fact_table="kpi_facts", fact_row_id=result.fact_id, knowledge_cutoff=STAMP
    )
    legacy = str(
        conn.execute(
            "SELECT observation_id FROM fact_observation_revisions WHERE fact_table='kpi_facts' AND fact_row_id=?",
            (result.fact_id,),
        ).fetchone()[0]
    )
    conn.commit()
    try:
        yield conn, legacy, result.fact_id, context
    finally:
        conn.close()


def request(conn: sqlite3.Connection, legacy: str) -> SourceFactPopulationRequest:
    proof = prepare_reviewed_kpi_native_projection(
        conn, observation_id=legacy, source_evidence_node_id="node-money", knowledge_cutoff=CUTOFF
    )
    return SourceFactPopulationRequest(
        schema_version="source_fact_population.v2",
        observation_ids=(legacy,),
        reviewed_kpi_projections=(proof,),
        document_scopes=(SourceFactDocumentScope(ticker="NU", document_sha256="a" * 64),),
        data_cutoff_at=CUTOFF,
        operation_recorded_at=RECORDED,
    )


def apply(
    conn: sqlite3.Connection, proposed: SourceFactPopulationRequest
) -> SourceFactPopulationResult:
    dry = populate_source_fact_plane(conn, proposed)
    return populate_source_fact_plane(
        conn,
        proposed.model_copy(
            update={
                "apply": True,
                "input_commitment_sha256": dry.input_commitment_sha256,
                "planned_output_commitment_sha256": dry.planned_output_commitment_sha256,
            }
        ),
    )


def test_reviewed_projection_preserves_v1_and_publishes_exactly_one_native_observation(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, _, _ = reviewed
    old = tuple(
        conn.execute(
            "SELECT * FROM reported_observations WHERE observation_id=?", (legacy,)
        ).fetchone()
    )
    assert (
        conn.execute(
            "SELECT currency FROM reported_observations WHERE observation_id=?", (legacy,)
        ).fetchone()[0]
        == "USD"
    )
    proposed = request(conn, legacy)
    dry = populate_source_fact_plane(conn, proposed)
    assert dry.policy_version == "6" and dry.expected_count == dry.eligible_count == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 0
    apply(conn, proposed)
    native = native_source_observation_id(legacy)
    cell_id = conn.execute(
        "SELECT fact_cell_id FROM fact_observations_v2 WHERE observation_id=?", (native,)
    ).fetchone()[0]
    graph = FactPlaneV2(conn).as_reported(cell_id)
    assert graph.cell.currency == "USD"
    assert graph.cell.fiscal_period == "Q4" and graph.cell.fiscal_year == 2024
    assert graph.cell.period_start == datetime(2024, 10, 1, tzinfo=UTC)
    assert graph.cell.accounting_basis == "non_gaap" and graph.cell.dimensions == ()
    assert graph.observations[0].numeric_value == "1088"
    assert graph.observations[0].evidence_node_id == "node-money"
    assert (
        tuple(
            conn.execute(
                "SELECT * FROM reported_observations WHERE observation_id=?", (legacy,)
            ).fetchone()
        )
        == old
    )
    replay = apply(conn, proposed)
    assert replay.exact_replay_run_count == 1
    assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 1


@pytest.mark.parametrize("change", [{"source_precision": None}, {"reported_period_start": None}])
def test_missing_typed_coordinates_fail_closed(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext], change: dict[str, object]
) -> None:
    conn, legacy, fact_id, context = reviewed
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=KpiSemanticContext.model_validate({**context.model_dump(mode="json"), **change}),
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
    )
    with pytest.raises(ValueError, match="requires"):
        request(conn, legacy)


def test_immutable_capture_currency_conflict_blocks_projection(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, _, _ = reviewed
    # Corrupt only this disposable fixture to exercise the immutable boundary.
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='reported_observations'"
    ).fetchall()
    for trigger in triggers:
        conn.execute('DROP TRIGGER "' + str(trigger[0]).replace('"', '""') + '"')
    conn.execute(
        "UPDATE reported_observations SET currency='EUR' WHERE observation_id=?", (legacy,)
    )
    with pytest.raises(ValueError, match="immutable capture"):
        request(conn, legacy)


def test_annual_header_cannot_supply_a_standalone_q4_duration(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, fact_id, context = reviewed
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=context.model_copy(update={"source_column_header": "Year Ended"}),
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
    )
    with pytest.raises(ValueError, match="duration"):
        request(conn, legacy)


def test_nonexact_precision_is_visible_and_excluded(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, fact_id, context = reviewed
    changed = KpiSemanticContext.model_validate(
        {
            **context.model_dump(mode="json"),
            "source_precision": {"kind": "approximate", "qualifiers": ["approximately"]},
        }
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=changed,
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
    )
    dry = populate_source_fact_plane(conn, request(conn, legacy))
    assert (
        dry.eligible_count == 0
        and dry.exclusion_counts["reviewed_kpi_source_precision_nonexact"] == 1
    )


def test_context_head_change_and_future_review_block_replay(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, fact_id, context = reviewed
    proposed = request(conn, legacy)
    changed = context.model_copy(update={"source_column_header": "Q4"})
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=changed,
        reviewed_by="fixture",
        knowledge_at=STAMP + timedelta(minutes=1),
        kpi_definition_revision_id="definition-money",
    )
    with pytest.raises(ValueError, match="changed"):
        populate_source_fact_plane(conn, proposed)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=context.model_copy(update={"source_column_header": "2024"}),
        reviewed_by="fixture",
        knowledge_at=CUTOFF + timedelta(minutes=1),
        kpi_definition_revision_id="definition-money",
    )
    with pytest.raises(ValueError, match="after the cutoff"):
        request(conn, legacy)


def test_exact_selection_rejects_missing_ids_and_unchecked_proofs(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, _, _ = reviewed
    proposed = request(conn, legacy)
    with pytest.raises(ValueError, match="incomplete"):
        populate_source_fact_plane(
            conn, proposed.model_copy(update={"observation_ids": (legacy, "missing")})
        )
    wrong = proposed.reviewed_kpi_projections[0].model_copy(update={"fiscal_year": 2025})
    with pytest.raises(ValueError, match="changed"):
        populate_source_fact_plane(
            conn, proposed.model_copy(update={"reviewed_kpi_projections": (wrong,)})
        )


def test_existing_incompatible_native_observation_blocks_without_duplicate(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, legacy, _, _ = reviewed
    proposed = request(conn, legacy)
    module = importlib.import_module("provenance.population_source_facts")
    adapter = getattr(module, "_source_fact_from_row")

    def legacy_projection(*args: object, **kwargs: object) -> object:
        kwargs["projection"] = None
        return adapter(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_source_fact_from_row", legacy_projection)
        apply(conn, proposed)
    with pytest.raises(ValueError, match="immutable native observation"):
        populate_source_fact_plane(conn, proposed)
    assert conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone()[0] == 1


def test_v1_request_bytes_remain_unchanged() -> None:
    proposed = SourceFactPopulationRequest(data_cutoff_at=CUTOFF, operation_recorded_at=RECORDED)
    assert "schema_version" not in proposed.model_dump(mode="json")
    assert "observation_ids" not in proposed.model_dump(mode="json")
    with pytest.raises(ValueError, match="request v2"):
        SourceFactPopulationRequest(
            data_cutoff_at=CUTOFF, operation_recorded_at=RECORDED, observation_ids=("unreviewed",)
        )


def test_explicit_calendar_ytd_remains_a_cumulative_duration(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, fact_id, context = reviewed
    cumulative = context.model_copy(
        update={"reported_period_start": date(2024, 1, 1), "source_column_header": "Year Ended"}
    )
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=cumulative,
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
    )
    proposed = request(conn, legacy)
    apply(conn, proposed)
    native = native_source_observation_id(legacy)
    cell_id = conn.execute(
        "SELECT fact_cell_id FROM fact_observations_v2 WHERE observation_id=?", (native,)
    ).fetchone()[0]
    graph = FactPlaneV2(conn).as_reported(cell_id)
    assert graph.cell.period_start == datetime(2024, 1, 1, tzinfo=UTC)
    assert graph.cell.fiscal_period == "Q4"
    assert graph.cell.period_start is not None
    assert (graph.cell.period_end - graph.cell.period_start).days + 1 == 366


def test_ytd_without_exact_duration_header_fails_closed(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, legacy, fact_id, context = reviewed
    cumulative = context.model_copy(update={"reported_period_start": date(2024, 1, 1)})
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=cumulative,
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
    )
    with pytest.raises(ValueError, match="duration"):
        request(conn, legacy)


def test_bridge_is_ready_for_exact_ontology_and_financial_scale(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    from provenance.fact_read_model import FactReadModel
    from provenance.financial_derivations import MonetaryScaleRequest, publish_monetary_scale
    from provenance.population_metric_ontology import (
        ExactSourceAdmissionRequest,
        ExactSourceObservationReview,
        admit_exact_source_observations,
    )

    conn, legacy, _, _ = reviewed
    apply(conn, request(conn, legacy))
    native = native_source_observation_id(legacy)
    bundle = FactReadModel(conn).provenance_bundle(native, cutoff=RECORDED)
    assert bundle.evidence is not None
    semantic = conn.execute(
        "SELECT semantic_key_sha256 FROM fact_cell_identity_seals_v2 WHERE fact_cell_id=?",
        (bundle.cell.fact_cell_id,),
    ).fetchone()[0]
    review = ExactSourceObservationReview(
        observation_id=native,
        document_version_id=bundle.evidence.document_version_id,
        subject_binding_revision_id=bundle.evidence.subject_binding_revision_id,
        observation_payload_sha256=bundle.observation_payload_sha256,
        source_locator_sha256=bundle.evidence.source_locator_sha256,
        fact_cell_semantic_key_sha256=semantic,
    )
    admission = ExactSourceAdmissionRequest(
        observations=(review,),
        reviewer_identity="fixture",
        review_evidence={
            "source_projection": request(conn, legacy)
            .reviewed_kpi_projections[0]
            .source_entry_sha256
        },
        knowledge_cutoff=RECORDED,
        operation_recorded_at=RECORDED,
    )
    receipt = admit_exact_source_observations(conn, admission)
    assert receipt.observation_ids == (native,)
    scaled, _ = publish_monetary_scale(
        conn,
        MonetaryScaleRequest(
            source_observation_id=native,
            source_scale="millions",
            knowledge_cutoff=RECORDED,
            recorded_at=RECORDED,
        ),
    )
    assert scaled.observation.numeric_value == "1088000000"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM fact_observations_v2 WHERE observation_kind='reported'"
        ).fetchone()[0]
        == 1
    )
    assert conn.execute("SELECT COUNT(*) FROM ontology_snapshot_headers").fetchone()[0] == 0


def test_exact_raw_fy_label_remains_fy(
    reviewed: tuple[sqlite3.Connection, str, int, KpiSemanticContext],
) -> None:
    conn, _, _, context = reviewed
    wording = str(
        conn.execute("SELECT text FROM evidence_nodes WHERE node_id='node-money'").fetchone()[0]
    )
    annual = context.model_copy(
        update={"reported_period_start": date(2024, 1, 1), "source_column_header": "Year Ended"}
    )
    result = persist_kpi_value_at_exact_definition(
        conn,
        ticker="NU",
        period_end=datetime(2024, 12, 31, tzinfo=UTC),
        fiscal_period_type=FiscalPeriodType.FY,
        source_doc_id=10,
        kpi_definition_id=123,
        expected_definition_name="Adjusted EBITDA",
        value=Decimal("1088"),
        unit=Unit.MILLIONS,
        currency=Currency.USD,
        locator=FactLocator(pdf_page=1, verbatim_snippet=wording),
        source_excerpt=wording,
        context=annual,
        reviewed_by="fixture",
        knowledge_at=STAMP,
        kpi_definition_revision_id="definition-money",
        extracted_by="source_review:fixture",
    )
    resolve_fact_row(
        conn, fact_table="kpi_facts", fact_row_id=result.fact_id, knowledge_cutoff=STAMP
    )
    legacy = str(
        conn.execute(
            "SELECT observation_id FROM fact_observation_revisions WHERE fact_table='kpi_facts' AND fact_row_id=?",
            (result.fact_id,),
        ).fetchone()[0]
    )
    proof = prepare_reviewed_kpi_native_projection(
        conn, observation_id=legacy, source_evidence_node_id="node-money", knowledge_cutoff=CUTOFF
    )
    assert proof.fiscal_period == "FY"
    proposed = SourceFactPopulationRequest(
        schema_version="source_fact_population.v2",
        observation_ids=(legacy,),
        reviewed_kpi_projections=(proof,),
        document_scopes=(SourceFactDocumentScope(ticker="NU", document_sha256="a" * 64),),
        data_cutoff_at=CUTOFF,
        operation_recorded_at=RECORDED,
    )
    apply(conn, proposed)
    cell_id = conn.execute(
        "SELECT fact_cell_id FROM fact_observations_v2 WHERE observation_id=?",
        (native_source_observation_id(legacy),),
    ).fetchone()[0]
    assert FactPlaneV2(conn).as_reported(cell_id).cell.fiscal_period == "FY"
