"""Real ONON fact/model replay; source-coverage admission alone is isolated.

This follows test_meli_input_evidence's declared model boundary. Publication,
ontology, canonical resolution, byte reads, scenario review, persistence and
readiness execute their production owners. It is not annual-package admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from dcf import input_evidence as evidence
from dcf import onon_inputs
from dcf.artifact_promotion import StagedFilePromotion, hold_dcf_artifacts
from dcf.readiness import load_valuation_readiness
from dcf.scenario_acceptance import ScenarioAcceptance, ScenarioReview
from execution import build_onon_dcf as builder
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
)
from provenance.fact_read_model import FactReadModel
from provenance.metric_ontology import (
    BindingRevision,
    CanonicalMetric,
    CanonicalMetricCell,
    CanonicalMetricDefinitionRevision,
    MappingRevision,
    MetricOntology,
    SourceObservationTaxonomyAssertion,
    SourceTaxonomyComponent,
)
from provenance.research_snapshot import ResearchSnapshotRequest
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from research.memo_model_evidence import (
    MemoModelCommitment,
    MemoModelEvidenceError,
    VerifiedMemoModel,
    load_verified_memo_model,
)
from tests.test_onon_inputs import END, NOW, proof
from tests.test_onon_model import memo_inputs
from tests.test_source_fact_repository import STAMP, make_cell, make_report, seed_foundation, sha256

ModelFixture = tuple[sqlite3.Connection, MemoModelCommitment, evidence.SourceReadContext]


def seed_model_facts(
    conn: sqlite3.Connection, source: Path, *, presentation: bool = False
) -> dict[str, evidence.FactBinding]:
    if presentation:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"filing bytes")
        _presentation_foundation(conn, storage_uri=source.as_uri())
    else:
        seed_foundation(conn, source_path=source)
    requirements = onon_inputs.requirements_for(END)
    values = {item.key: item.value for item in proof().inputs}
    facts: list[ReportedSourceFact] = []
    for index, req in enumerate(requirements):
        amount = Decimal(str(values[req.key])) / req.scale
        cell = FactCellV2.model_validate(
            make_cell(req.key)
            .model_copy(
                update={
                    "semantic_key_sha256": None,
                    "dimensions": (),
                    "concept_name": req.role.removeprefix("onon."),
                    "period_start": datetime.combine(
                        req.period_start, datetime.min.time(), tzinfo=UTC
                    )
                    if req.period_start
                    else None,
                    "period_end": datetime.combine(
                        req.period_end or END, datetime.min.time(), tzinfo=UTC
                    ),
                    "period_kind": req.period_kind,
                    "unit_key": req.unit_key,
                    "currency": req.currency,
                    "fiscal_period": "FY" if req.key.endswith("_fy") else None,
                    "fiscal_year": (req.period_end or END).year,
                    "accounting_basis": req.accounting_basis or "ifrs",
                }
            )
            .model_dump(mode="json")
        )
        node = f"onon-node-{index}"
        locator = json.dumps({"path": req.key}, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,1,'run-1',NULL,NULL,'table_cell',?,?,?,?)",
            (node, node, str(amount), locator, sha256(locator), STAMP),
        )
        report = make_report(cell, req.key, numeric_value=str(amount)).model_copy(
            update={
                "evidence_node_id": node,
                "source_locator": CanonicalJSONObject.model_validate({"path": req.key}),
                "source_locator_sha256": None,
            }
        )
        facts.append(ReportedSourceFact(cell=cell, observation=report))
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="publication-onon",
            created_at=NOW,
            recorded_at=NOW,
            idempotency_key="publication-onon",
            reported_facts=tuple(facts),
            extraction_seals=(
                ExtractionRunCompletenessSealV2(
                    extraction_seal_id="seal-onon",
                    idempotency_key="seal-onon",
                    extraction_run_id="run-1",
                    expected_node_count=len(facts) + 1,
                    completeness_policy_name="synthetic-all-nodes",
                    completeness_policy_version="1",
                    completeness_policy_sha256="a" * 64,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                ),
            ),
        )
    )
    ontology, resolver = MetricOntology(conn), CanonicalFactResolutionEngine(conn)
    registered: set[str] = set()
    refs: dict[str, evidence.FactBinding] = {}
    for req, fact in zip(requirements, facts, strict=True):
        cell, observation = fact.cell, fact.observation
        metric = req.role
        family = "currency" if req.currency else req.unit_key
        if metric not in registered:
            ontology.persist_metric(
                CanonicalMetric(
                    metric_id=metric,
                    idempotency_key=metric,
                    canonical_name="revenue"
                    if presentation and metric == "onon.revenue"
                    else metric,
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_metric_definition(
                CanonicalMetricDefinitionRevision(
                    metric_definition_revision_id=metric + ":v1",
                    idempotency_key=metric + ":v1",
                    metric_id=metric,
                    revision=1,
                    lifecycle="active",
                    definition_text="Synthetic ONON reported definition",
                    value_kind="numeric",
                    period_kind=req.period_kind,
                    unit_family=family,
                    accounting_basis=cell.accounting_basis,
                    scope_constraints={
                        "valuation_role": metric,
                        **req.definition_constraints,
                        "reporting_entity_id": "reporting-1",
                        "consolidation_scope": "consolidated",
                    },
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            qualifier = {
                "accounting_basis": cell.accounting_basis,
                "concept_name": cell.concept_name,
                "concept_namespace": cell.concept_namespace,
                "consolidation_scope": "consolidated",
                "period_kind": req.period_kind,
                "reporting_entity_id": "reporting-1",
                "schema_version": "source-definition-identity/v1",
                "taxonomy_name": "US GAAP",
                "taxonomy_version": "2026",
                "unit_family": family,
                "value_kind": "numeric",
            }
            ontology.persist_source_component(
                SourceTaxonomyComponent(
                    component_id=metric + ":component",
                    idempotency_key=metric + ":component",
                    component_kind="concept",
                    taxonomy_namespace=cell.concept_namespace,
                    local_name=cell.concept_name,
                    taxonomy_name="US GAAP",
                    taxonomy_version="2026",
                    definition_qualifier_sha256=evidence.canonical_digest(qualifier),
                    reporting_entity_id="reporting-1",
                    is_extension=False,
                    evidence_locator={"fixture": True},
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            ontology.persist_mapping(
                MappingRevision(
                    mapping_revision_id=metric + ":mapping",
                    idempotency_key=metric + ":mapping",
                    source_component_id=metric + ":component",
                    metric_id=metric,
                    revision=1,
                    disposition="exact",
                    policy_name="synthetic-review",
                    policy_version="1",
                    policy_config_sha256="a" * 64,
                    method_name="synthetic-review",
                    method_version="1",
                    constraints={},
                    evidence={"fixture": True},
                    reviewer_identity="fixture",
                    effective_at=STAMP,
                    knowledge_at=STAMP,
                    recorded_at=STAMP,
                )
            )
            registered.add(metric)
        source_proof = conn.execute(
            """SELECT anchor.extraction_run_id,cell.taxonomy_name,anchor.source_taxonomy_version,
            cell_seal.semantic_key_sha256,anchor.anchor_payload_sha256,payload.observation_payload_sha256,
            run.output_sha256,anchor.raw_entry_sha256,completeness.observation_set_sha256
            FROM fact_reported_observation_anchors_v2 anchor JOIN fact_cells_v2 cell ON cell.fact_cell_id=?
            JOIN fact_cell_identity_seals_v2 cell_seal ON cell_seal.fact_cell_id=cell.fact_cell_id
            JOIN fact_observation_payload_commitments_v2 payload ON payload.observation_id=anchor.observation_id
            JOIN evidence_extraction_runs run ON run.extraction_run_id=anchor.extraction_run_id
            JOIN fact_extraction_run_completeness_seals_v2 completeness ON completeness.extraction_run_id=anchor.extraction_run_id
            WHERE anchor.observation_id=?""",
            (cell.fact_cell_id, observation.observation_id),
        ).fetchone()
        assert source_proof is not None
        ontology.persist_observation_taxonomy_assertion(
            SourceObservationTaxonomyAssertion(
                observation_id=observation.observation_id,
                idempotency_key="taxonomy:" + req.key,
                extraction_run_id=str(source_proof[0]),
                taxonomy_name=str(source_proof[1]),
                taxonomy_version=str(source_proof[2]),
                fact_cell_semantic_key_sha256=str(source_proof[3]),
                anchor_payload_sha256=str(source_proof[4]),
                observation_payload_sha256=str(source_proof[5]),
                extraction_output_sha256=str(source_proof[6]),
                raw_entry_sha256=str(source_proof[7]),
                observation_set_sha256=str(source_proof[8]),
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        canonical_id = "canonical:" + req.key
        ontology.persist_canonical_metric_cell(
            CanonicalMetricCell(
                canonical_metric_cell_id=canonical_id,
                idempotency_key=canonical_id,
                metric_id=metric,
                reporting_entity_id="reporting-1",
                period_kind=req.period_kind,
                period_start=cell.period_start,
                period_end=cell.period_end,
                dimensions=(),
                scope_security_id=cell.scope_security_id,
                unit_family=family,
                accounting_basis=cell.accounting_basis,
                consolidation_scope="consolidated",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        ontology.persist_binding(
            BindingRevision(
                binding_revision_id="binding:" + req.key,
                idempotency_key="binding:" + req.key,
                fact_cell_id=cell.fact_cell_id,
                source_observation_id=observation.observation_id,
                revision=1,
                canonical_metric_cell_id=canonical_id,
                mapping_revision_id=metric + ":mapping",
                source_component_id=metric + ":component",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
        resolved = resolver.resolve(
            canonical_id,
            NOW,
            ResolutionPolicy(name="synthetic", version="1", config={}),
            recorded_at=NOW,
        )
        assert resolved.status == "resolved"
        refs[req.key] = evidence.FactBinding(
            canonical_metric_cell_id=canonical_id,
            metric_id=metric,
            metric_definition_revision_id=metric + ":v1",
            canonical_resolution_revision_id=resolved.canonical_resolution_revision_id,
            observation_id=observation.observation_id,
            observation_payload_sha256=FactReadModel(conn)
            .provenance_bundle(observation.observation_id, cutoff=NOW)
            .observation_payload_sha256,
        )
    resolver.seal_snapshot(
        "resolution-snapshot",
        NOW,
        NOW,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    conn.commit()
    return refs


@pytest.fixture
def admitted_model(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[sqlite3.Connection, MemoModelCommitment, evidence.SourceReadContext]]:
    db = migrated_db(tmp_path / "model.db")
    source_context = evidence.SourceReadContext(content_roots=(tmp_path / "sources",))
    conn = sqlite3.connect(db)
    refs = seed_model_facts(conn, source_context.content_roots[0] / "source.json")
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "snapshot",
            "idempotency_key": "snapshot",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["document-1"],
                "source_obligation_revision_ids": ["obligation"],
            },
            "processing_snapshot_ids": ["processing"],
            "corpus_bundles": [
                {"corpus_manifest_id": "manifest", "lexical_index_run_id": "lexical"}
            ],
            "source_fact_publication_ids": ["publication-onon"],
            "ontology_snapshot_id": "ontology",
            "canonical_fact_resolution_snapshot_id": "resolution-snapshot",
            "canonical_fact_projection_run_id": "projection",
            "cutoff_at": NOW,
            "recorded_at": NOW,
        }
    )

    # Only the inventory/research-coverage boundary is isolated. Every fact is
    # checked against this explicit primary universe by the real shared reader.
    def coverage(
        _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return snapshot, "b" * 64, ("inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    request = proof().request.model_copy(update={"facts": refs})
    numeric = memo_inputs()
    raw = evidence.verify_model_inputs(
        conn,
        request,
        recipe=onon_inputs.RECIPE,
        requirements=onon_inputs.requirements_for(END),
        effective_inputs=numeric,
        assumption_keys=onon_inputs.ASSUMPTION_KEYS,
        as_of=NOW,
        source_context=source_context,
    )
    actuals, _ = onon_inputs.calculate_actuals(raw)
    numeric.update({key: actuals[key] for key in onon_inputs.REPORTED_DRIVER_KEYS})
    review = evidence.AssumptionReview(
        reviewed_at=NOW,
        reviewer="synthetic analyst",
        effective_inputs_sha256=evidence.canonical_digest(numeric),
        actuals_sha256=evidence.canonical_digest(actuals),
        drivers={
            key: evidence.DriverReview(
                observed=a,
                forecast=f,
                variance=f - a,
                rationale="Synthetic analyst explicitly reviews the forecast variance",
            )
            for key, (a, f) in onon_inputs.review_drivers(actuals, numeric).items()
        },
    )
    request = request.model_copy(update={"assumption_review": review})
    numeric, receipt = onon_inputs.prepare_onon_inputs(
        conn, request, effective_inputs=numeric, as_of=NOW, source_context=source_context
    )
    assumptions = tmp_path / "assumptions.json"
    assumptions.write_text(json.dumps({"input_evidence": request.model_dump(mode="json")}))
    receipt = receipt.model_copy(
        update={
            "assumptions_source_path": str(assumptions.resolve()),
            "assumptions_source_sha256": hashlib.sha256(assumptions.read_bytes()).hexdigest(),
        }
    )
    output = onon_inputs.model_output(numeric)
    scenarios = cast(dict[str, object], output["scenarios"])
    acceptance = ScenarioAcceptance(
        ticker="ONON",
        recipe=receipt.recipe,
        model=builder.MODEL,
        engine_version=builder.ENGINE_VERSION,
        reviewer="synthetic analyst",
        attribution="analyst",
        reviewed_at=NOW,
        model_input_receipt_sha256=evidence.canonical_digest(receipt.model_dump(mode="json")),
        effective_inputs_sha256=receipt.effective_inputs_sha256,
        actuals_sha256=receipt.actuals_sha256 or "",
        snapshot_member_sha256=receipt.snapshot_member_sha256,
        inventory_snapshot_ids=receipt.inventory_snapshot_ids,
        model_output_sha256=evidence.canonical_digest(output),
        scenarios={
            name: ScenarioReview(
                output_sha256=evidence.canonical_digest(value),
                accepted=True,
                rationale="Explicit synthetic scenario assessment for the exact model",
            )
            for name, value in scenarios.items()
        },
    )
    acceptance_path = tmp_path / "acceptance.json"
    acceptance_path.write_text(acceptance.model_dump_json())
    stage = tmp_path / "stage.xlsx"
    builder.build_workbook(numeric, output, receipt, stage)
    os.utime(assumptions, (NOW.timestamp(), NOW.timestamp()))
    os.utime(acceptance_path, (NOW.timestamp(), NOW.timestamp()))
    conn.commit()
    with hold_dcf_artifacts(tmp_path, "ONON", owner="synthetic-test"):
        assert builder.persist_dcf_run(
            numeric,
            output,
            receipt,
            db_path=db,
            assumptions_path=assumptions,
            destination=stage,
            repo_root=tmp_path,
            market_observed_at=NOW,
            calculated_at=NOW,
            artifact_promotion=StagedFilePromotion(stage, tmp_path / "ONON.xlsx"),
            scenario_acceptance=acceptance,
            scenario_acceptance_path=acceptance_path,
            source_context=source_context,
        )
    row = conn.execute(
        "SELECT id,input_sha256 FROM dcf_runs WHERE ticker='ONON' AND is_latest=1"
    ).fetchone()
    assert row is not None
    commitment = MemoModelCommitment(
        run_id=int(row[0]),
        input_sha256=str(row[1]),
        receipt_sha256=evidence.canonical_digest(receipt.model_dump(mode="json")),
        research_snapshot_id="snapshot",
        snapshot_member_sha256="b" * 64,
        effective_inputs_sha256=evidence.canonical_digest(numeric),
        model_output_sha256=evidence.canonical_digest(output),
        scenario_acceptance_sha256=evidence.canonical_digest(acceptance.model_dump(mode="json")),
    )
    try:
        yield conn, commitment, source_context
    finally:
        conn.close()


def _load(
    conn: sqlite3.Connection, commitment: MemoModelCommitment, context: evidence.SourceReadContext
) -> VerifiedMemoModel:
    return load_verified_memo_model(
        conn,
        commitment,
        primary_snapshot_id="snapshot",
        primary_member_sha256="b" * 64,
        ticker="ONON",
        as_of=NOW,
        source_context=context,
    )


def test_real_model_commitment_roundtrip(admitted_model: ModelFixture) -> None:
    conn, commitment, context = admitted_model
    ready = load_valuation_readiness(
        conn, "ONON", as_of=NOW, purpose="analyst_memo", source_context=context
    )
    assert ready.ready, ready.reason_codes
    verified = _load(conn, commitment, context)
    assert _positive_vps(verified) > 0
    assert verified.calculations["shares"] == pytest.approx(336.1087012)
    scenarios = verified.output["scenarios"]
    assert isinstance(scenarios, dict)
    assert set(cast(dict[str, object], scenarios)) == {"bear", "base", "bull"}


@pytest.mark.parametrize(
    "field",
    [
        "input_sha256",
        "receipt_sha256",
        "effective_inputs_sha256",
        "model_output_sha256",
        "scenario_acceptance_sha256",
        "run_id",
        "snapshot_member_sha256",
        "research_snapshot_id",
    ],
)
def test_changed_memo_commitment_refuses_after_real_positive(
    admitted_model: ModelFixture, field: str
) -> None:
    conn, commitment, context = admitted_model
    assert _positive_vps(_load(conn, commitment, context)) > 0
    changed = commitment.model_copy(
        update={
            field: 99
            if field == "run_id"
            else "foreign"
            if field == "research_snapshot_id"
            else "f" * 64
        }
    )
    with pytest.raises(MemoModelEvidenceError):
        _load(conn, changed, context)


@pytest.mark.parametrize(
    "section,key",
    [
        ("snapshot", "effective_model_inputs"),
        ("snapshot", "model_output"),
        ("provenance", "model_input_receipt"),
        ("provenance", "scenario_acceptance"),
    ],
)
def test_changed_persisted_evidence_refuses(
    admitted_model: ModelFixture, section: str, key: str
) -> None:
    conn, commitment, context = admitted_model
    assert _positive_vps(_load(conn, commitment, context)) > 0
    column = "assumption_snapshot_json" if section == "snapshot" else "provenance_json"
    payload = json.loads(
        conn.execute(f"SELECT {column} FROM dcf_runs WHERE id=?", (commitment.run_id,)).fetchone()[
            0
        ]
    )
    if key == "effective_model_inputs":
        payload[key]["base_rent"] += 1
    elif key == "model_output":
        payload[key]["vps"] += 1
    elif key == "model_input_receipt":
        payload[key]["inputs"][0]["value"] += 1
    else:
        payload[key]["scenarios"]["bear"]["accepted"] = False
    conn.execute(
        f"UPDATE dcf_runs SET {column}=? WHERE id=?", (json.dumps(payload), commitment.run_id)
    )
    with pytest.raises(MemoModelEvidenceError):
        _load(conn, commitment, context)


def test_current_bytes_change_refuses_real_model(admitted_model: ModelFixture) -> None:
    conn, commitment, context = admitted_model
    assert _positive_vps(_load(conn, commitment, context)) > 0
    (context.content_roots[0] / "source.json").write_bytes(b"changed same")
    with pytest.raises(MemoModelEvidenceError):
        _load(conn, commitment, context)


def _presentation_foundation(
    conn: sqlite3.Connection, *, storage_uri: str = "file:///filing.json"
) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-key-1", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-1",
            "reporting-key-1",
            "issuer-1",
            "legal_registrant",
            "Issuer One",
            STAMP,
        ),
    )
    blob_sha = sha256("filing bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (blob_sha, 12, "application/json", storage_uri, STAMP),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-1",
            "source-key-1",
            "sec_companyfacts",
            "https://data.sec.gov/example.json",
            blob_sha,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            sha256("retrieval"),
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,accession_number,"
        "exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-1",
            "document-key-1",
            1,
            "source-1",
            blob_sha,
            "issuer-1",
            "ONON",
            "investor_presentation",
            "presentation",
            "0000000001-26-000001",
            None,
            STAMP - timedelta(days=365),
            STAMP,
            STAMP,
            "en",
            None,
            None,
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-1",
            "binding-key-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "exact_subject",
            "{}",
            0,
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-1",
            "run-key-1",
            "document-1",
            blob_sha,
            "test-extractor",
            sha256("extractor-config"),
            "test-v1",
            sha256("output"),
            STAMP,
            STAMP,
            "succeeded",
        ),
    )
    locator = '{"path":"facts.us-gaap.Revenues.units.USD[0]"}'
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-1",
            "node-key-1",
            1,
            "run-1",
            None,
            None,
            "table_cell",
            "100",
            locator,
            sha256(locator),
            STAMP,
        ),
    )


def _positive_vps(model: VerifiedMemoModel) -> float:
    value = model.output["vps"]
    assert isinstance(value, (float, int)) and not isinstance(value, bool)
    return float(value)
