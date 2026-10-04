"""Role-admission controls; these checks do not certify a financial population."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import provenance.fulltext_backfill as fulltext
from dcf.input_evidence import FactBinding, canonical_digest
from dcf.meli_input_preview import preview_meli_inputs
from dcf.meli_inputs import requirements_for
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.evidence_ledger import (
    ContentBlob,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.filing_xbrl_extraction_ledger import FilingXbrlExtractionLedger
from provenance.filing_xbrl_fact_adapter import FilingXbrlNormalizedOutput, NormalizedFilingXbrlFact
from provenance.meli_role_admission import (
    ReviewedRoleAdmission,
    RoleAdmissionRequest,
    RoleAssignment,
    RoleDefinitionPlan,
    RoleEvidence,
    apply_reviewed_meli_role_admission,
    plan_meli_role_admission,
)
from provenance.metric_ontology import (
    CanonicalMetricDefinitionRevision,
    MetricOntology,
    OntologySnapshot,
)
from provenance.population_canonical_resolution import (
    CanonicalResolutionPopulationRequest,
    populate_canonical_resolution,
)
from provenance.population_document_processing import (
    DocumentProcessingPopulationRequest,
    populate_document_processing,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.population_research_snapshots import assemble_research_snapshot_request
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntityRegistry,
    SourceObligationRevision,
)
from provenance.research_snapshot import build_research_snapshot, verify_research_snapshot
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_expectation_lifecycle import (
    ExpectedDocumentLifecycle,
    persist_expected_document_lifecycle,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from search.corpus_builder import (
    CorpusBuildRequest,
    build_grounded_search_corpus,
    load_analysis_expected_document_inventory,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from tests.test_filing_xbrl_extraction_ledger import (
    filing_xbrl_entry,
    filing_xbrl_ledger_database,
    filing_xbrl_output,
    insert_filing_xbrl_extraction_run,
)

AS_OF = datetime(2026, 10, 3, tzinfo=UTC)
PERIOD = date(2026, 6, 30)


def request(period: date = PERIOD) -> RoleAdmissionRequest:
    return RoleAdmissionRequest(
        issuer_id="issuer-1",
        research_snapshot_id="missing-snapshot",
        financial_period_end=period,
        as_of=AS_OF,
        assignments=tuple(
            RoleAssignment(
                key=item.key,
                fact=FactBinding(
                    canonical_metric_cell_id=item.key,
                    metric_id=item.role,
                    metric_definition_revision_id=item.role + ":1",
                    canonical_resolution_revision_id=item.key + ":resolution",
                    observation_id=item.key + ":observation",
                    observation_payload_sha256="a" * 64,
                ),
                rationale="Explicit synthetic role review; no issuer financial claim.",
                evidence=(
                    RoleEvidence(
                        node_id=item.key + ":node",
                        text_sha256="b" * 64,
                        locator_sha256="c" * 64,
                    ),
                ),
            )
            for item in requirements_for(period)
        ),
    )


@pytest.mark.parametrize("period,count", [(PERIOD, 28), (date(2025, 12, 31), 14)])
def test_request_preserves_exact_recipe_population(period: date, count: int) -> None:
    assert len(request(period).assignments) == count


def test_request_rejects_shortened_or_duplicate_population() -> None:
    original = request()
    with pytest.raises(ValidationError, match="required_role_population_mismatch"):
        RoleAdmissionRequest.model_validate(
            original.model_copy(update={"assignments": original.assignments[:-1]}).model_dump()
        )
    with pytest.raises(ValidationError, match="required_role_population_mismatch"):
        RoleAdmissionRequest.model_validate(
            original.model_copy(
                update={"assignments": (*original.assignments[:-1], original.assignments[0])}
            ).model_dump()
        )


def test_request_rejects_naive_cutoff_and_missing_role_evidence() -> None:
    original = request()
    with pytest.raises(ValidationError):
        RoleAdmissionRequest.model_validate(
            original.model_copy(update={"as_of": AS_OF.replace(tzinfo=None)}).model_dump()
        )
    with pytest.raises(ValidationError):
        RoleAssignment.model_validate(
            original.assignments[0].model_copy(update={"evidence": ()}).model_dump()
        )


def test_actual_missing_snapshot_blocks_plan_without_writes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "roles.db"))
    try:
        conn.execute("BEGIN")
        before = conn.total_changes
        result = plan_meli_role_admission(conn, request())
        assert result.state == "blocked"
        assert result.model_ready is False
        assert result.definitions == ()
        assert result.blockers
        assert conn.total_changes == before
        assert conn.in_transaction
    finally:
        conn.close()


def test_request_has_no_forecasts_or_model_outputs() -> None:
    fields = request().model_dump()
    assert "assumptions" not in fields
    assert "model_output_sha256" not in fields
    assert request().as_of < AS_OF + timedelta(seconds=1)


def test_candidate_preserves_source_definition_and_dates_role_knowledge() -> None:
    parent = CanonicalMetricDefinitionRevision(
        metric_definition_revision_id="definition:1",
        idempotency_key="definition:1",
        metric_id="metric",
        revision=1,
        lifecycle="active",
        definition_text="Issuer wording remains exact.",
        aliases=("Exact source name",),
        value_kind="numeric",
        period_kind="duration",
        unit_family="currency",
        accounting_basis="us_gaap",
        scope_constraints={"reporting_entity_id": "issuer-reporting-entity"},
        effective_at=AS_OF - timedelta(days=300),
        knowledge_at=AS_OF - timedelta(days=10),
        recorded_at=AS_OF - timedelta(days=10),
    )
    item = RoleDefinitionPlan(
        parent=parent,
        scope_constraints={
            **parent.scope_constraints,
            "valuation_role_selectors": {
                "meli.operating_income": {
                    "canonical_dimensions": [],
                    "semantic_constraints": {"reported_population": "actual"},
                }
            },
        },
    )
    successor = item.reviewed_successor(
        review_sha256="a" * 64, plan_sha256="b" * 64, applied_at=AS_OF
    )
    assert successor.definition_text == parent.definition_text
    assert successor.aliases == parent.aliases
    assert successor.metric_id == parent.metric_id
    assert successor.accounting_basis == parent.accounting_basis
    assert successor.effective_at == parent.effective_at
    assert successor.knowledge_at == AS_OF
    assert successor.recorded_at == AS_OF
    assert successor.revision == 2
    assert successor.supersedes_metric_definition_revision_id == "definition:1"
    assert successor.scope_constraints["reporting_entity_id"] == "issuer-reporting-entity"
    assert parent.scope_constraints == {"reporting_entity_id": "issuer-reporting-entity"}


def synthetic_current_population(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    contextual_recorded_at: datetime | None = None,
    snapshot_cutoff: datetime | None = None,
) -> tuple[sqlite3.Connection, RoleAdmissionRequest]:
    """Synthetic 28-input closure through actual public owners; no verifier doubles."""
    requirements = requirements_for(PERIOD)
    entries = tuple(
        NormalizedFilingXbrlFact.model_validate(
            {
                **filing_xbrl_entry(
                    i, concept_name=item.role, numeric_value=Decimal(i + 100)
                ).model_dump(),
                "period_kind": item.period_kind,
                "period_start": None
                if item.period_kind == "instant"
                else datetime.combine(
                    item.period_start or date(2026, 1, 1), datetime.min.time(), UTC
                ),
                "period_end": datetime.combine(item.period_end or PERIOD, datetime.min.time(), UTC),
                "effective_at": datetime.combine(
                    item.period_end or PERIOD, datetime.min.time(), UTC
                ),
                "accounting_basis": item.accounting_basis or "us_gaap",
                "unit_key": item.unit_key,
                "currency": item.currency,
            }
        )
        for i, item in enumerate(requirements)
    )
    output = filing_xbrl_output(entries)
    if contextual_recorded_at is not None:
        assert snapshot_cutoff is not None
        # Admit the actual additional node population through the public XBRL seal.
        # The 28 source-fact entries and their original anchors remain unchanged.
        output = FilingXbrlNormalizedOutput.with_computed_digest(
            extraction=output.extraction.model_copy(
                update={"expected_evidence_node_count": len(entries) + 1}
            ),
            subject=output.subject,
            entries=entries,
        )
    blob = tmp_path / "data/evidence/blobs/filing.xhtml"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"filing-bytes")
    conn = filing_xbrl_ledger_database(
        tmp_path,
        output,
        migrated_db,
        document_ticker="MELI",
        document_period_end=datetime(2026, 6, 30, tzinfo=UTC),
        document_period_start=datetime(2026, 1, 1, tzinfo=UTC),
        document_type="filing",
        document_form_type="10-Q",
        blob_path=blob,
    )
    conn.close()
    conn = connect_sqlite(tmp_path / "filing-xbrl-ledger.db", role=SQLiteConnectionRole.WRITER)
    try:
        contextual_node = None
        if contextual_recorded_at is not None:
            contextual_node = EvidenceNode(
                node_id="role-context-node",
                evidence_key="role-context-evidence",
                revision=1,
                extraction_run_id=output.extraction.extraction_run_id,
                parent_node_id=entries[0].evidence_node_id,
                supersedes_node_id=None,
                node_kind="passage",
                text="Synthetic role rationale from the selected filing; no issuer claim.",
                locator=EvidenceLocator(source_ref="filing.xhtml", char_start=0, char_end=12),
                recorded_at=contextual_recorded_at,
            )
            # Persist before any extraction/processing seal. No frozen clock update.
            EvidenceLedger(conn).persist(contextual_node)
        FilingXbrlExtractionLedger(conn).publish(output)
        clock = datetime.now(UTC)
        ledger = EvidenceLedger(conn)
        raw = b'{"filings": []}'
        inventory_blob = blob.parent / "inventory.json"
        inventory_blob.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        ledger.persist(
            ContentBlob(
                sha256=sha,
                byte_size=len(raw),
                media_type="application/json",
                storage_uri=inventory_blob.as_uri(),
                recorded_at=clock,
            )
        )
        url = "https://data.sec.gov/submissions/CIK0000000001.json"
        ledger.persist(
            SourceObservation(
                observation_id="inventory-observation",
                idempotency_key="inventory-observation",
                source_kind="sec_submissions",
                source_url=url,
                blob_sha256=sha,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=clock,
                retrieved_at=clock,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
            )
        )
        coverage = SourceCoverageLedger(conn)
        coverage.persist(
            SourceInventorySnapshot(
                snapshot_id="inventory",
                idempotency_key="inventory",
                inventory_key="issuer-1:sec",
                revision=1,
                issuer_id="issuer-1",
                ticker="MELI",
                source_kind="sec_submissions",
                source_url=url,
                source_observation_id="inventory-observation",
                outcome="succeeded",
                authoritative=True,
                retrieval_config_sha256="a" * 64,
                collector_code_version="synthetic@1",
                started_at=clock,
                completed_at=clock,
                recorded_at=clock,
            )
        )
        component = InventoryComponent(
            component_id="component",
            idempotency_key="component",
            snapshot_id="inventory",
            component_key="root",
            component_kind="primary",
            source_url=url,
            source_observation_id="inventory-observation",
            outcome="succeeded",
            required=True,
            ordinal=0,
            recorded_at=clock,
        )
        seals = SourceInventorySealStore(conn)
        seals.persist(component)
        seals.persist(
            InventorySeal(
                snapshot_id="inventory",
                expected_component_count=1,
                component_digest_sha256=component_digest((component,)),
                completion_status="complete",
                sealed_at=clock,
            )
        )
        ReportingEntityRegistry(conn).persist(
            SourceObligationRevision(
                obligation_revision_id="periodic:v1",
                idempotency_key="periodic:v1",
                obligation_key="periodic",
                revision=1,
                issuer_id="issuer-1",
                reporting_entity_id="reporting-1",
                authority_kind="sec_edgar",
                document_family="operating_company_periodic",
                obligation_state="required",
                completeness_rule="regulator_inventory",
                active_from=clock,
                active_to=None,
                decision_kind="deterministic",
                reason_code="synthetic",
                reason_details=(("source", "fixture"),),
                effective_at=clock,
                knowledge_at=clock,
                recorded_at=clock,
            )
        )
        expected_document = ExpectedDocument(
            expected_document_id="expected",
            idempotency_key="expected",
            snapshot_id="inventory",
            expected_document_key="expected",
            issuer_id="issuer-1",
            ticker="MELI",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000000001-26-000001",
            source_url="https://www.sec.gov/Archives/example/filing.xhtml",
            primary_document="filing.xhtml",
            filing_at=clock,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            expectation_basis="authoritative",
            recorded_at=clock,
        )
        coverage.persist(expected_document)
        coverage.persist(
            CoverageAssessment(
                assessment_id="captured",
                idempotency_key="captured",
                expected_document_id="expected",
                revision=1,
                coverage_status="captured",
                document_version_id="document-1",
                reason_code="synthetic_capture",
                reason_details=(("source", "fixture"),),
                decision_kind="deterministic",
                policy_name="fixture",
                policy_version="1",
                policy_config_sha256="a" * 64,
                material_dissent=False,
                effective_at=clock,
                knowledge_at=clock,
                recorded_at=clock,
            )
        )
        persist_expected_document_lifecycle(
            conn,
            ExpectedDocumentLifecycle(
                lifecycle_id="lifecycle",
                idempotency_key="lifecycle",
                inventory_key="issuer-1:sec",
                expected_document_key="expected",
                source_inventory_snapshot_id="inventory",
                revision=1,
                status="expected",
                expected_document_id="expected",
                authority_observation_id="inventory-observation",
                reason_code="synthetic",
                reason_details=(("source", "fixture"),),
                effective_at=clock,
                knowledge_at=clock,
                recorded_at=clock,
            ),
        )
        conn.commit()
        result = fulltext.backfill_fulltext_evidence(
            conn,
            fulltext.FullTextBackfillRequest(
                repo_root=tmp_path,
                content_roots=(blob.parent,),
                source_lane="evidence_native",
                document_version_id="document-1",
                apply=True,
            ),
        )
        assert result.documents_extracted == 1
        cutoff = snapshot_cutoff or datetime.now(UTC)
        population = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=cutoff, operation_recorded_at=cutoff, apply=True
            ),
        )
        assert population.safe_to_seal
        resolutions = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=cutoff, operation_recorded_at=cutoff, apply=True
            ),
        )
        assert resolutions.state == "complete" and resolutions.resolved_cell_count == 28
        scope = build_analysis_scope(
            conn,
            AnalysisScopeRequest(
                purpose="post_earnings_readout",
                issuer_id="issuer-1",
                inventory_key="issuer-1:sec",
                required_period_ends=(PERIOD,),
                cutoff_at=cutoff,
                observed_through=cutoff,
            ),
        )
        processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=cutoff,
                operation_recorded_at=cutoff,
                apply=True,
                analysis_scope=scope,
                phase="all",
            ),
        )
        assert processing.processing_snapshot_count == 1
        inventory, snapshots = load_analysis_expected_document_inventory(
            conn, scope, cutoff_at=cutoff, observed_through=cutoff
        )
        build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key=scope.scope_id,
                revision=1,
                selector_code_version="synthetic@1",
                recorded_at=cutoff,
                knowledge_cutoff=cutoff,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=snapshots,
                analysis_scope=scope,
                apply=True,
            ),
        )
        snapshot = assemble_research_snapshot_request(
            conn, "issuer-1", cutoff, analysis_scope=scope, projection_mode="lexical_only"
        )
        admission = build_research_snapshot(conn, snapshot)
        assert (
            admission.admitted
            and verify_research_snapshot(conn, snapshot.research_snapshot_id) == admission
        )
        assignments: list[RoleAssignment] = []
        reader, ontology, resolver = (
            FactReadModel(conn),
            MetricOntology(conn),
            CanonicalFactResolutionEngine(conn),
        )
        for requirement, entry in zip(requirements, entries, strict=True):
            row = conn.execute(
                "SELECT disposition.observation_id, binding.canonical_metric_cell_id, cell.metric_id FROM filing_xbrl_extraction_dispositions disposition JOIN fact_cell_canonical_binding_revisions binding ON binding.source_observation_id=disposition.observation_id JOIN canonical_metric_cells cell USING(canonical_metric_cell_id) WHERE disposition.input_ordinal=?",
                (entry.ordinal,),
            ).fetchone()
            assert row is not None
            observation_id, cell_id, metric_id = map(str, row)
            bundle = reader.provenance_bundle(observation_id, cutoff=cutoff)
            definition = ontology.metric_definition_as_known(metric_id, cutoff)
            resolution = resolver.as_known(cell_id, cutoff)
            assert definition is not None and resolution is not None
            role_node_id = (
                contextual_node.node_id
                if contextual_node is not None and entry.ordinal == entries[0].ordinal
                else entry.evidence_node_id
            )
            node = conn.execute(
                "SELECT text,locator_sha256 FROM evidence_nodes WHERE node_id=?",
                (role_node_id,),
            ).fetchone()
            assignments.append(
                RoleAssignment(
                    key=requirement.key,
                    fact=FactBinding(
                        canonical_metric_cell_id=cell_id,
                        metric_id=metric_id,
                        metric_definition_revision_id=definition.metric_definition_revision_id,
                        canonical_resolution_revision_id=resolution.canonical_resolution_revision_id,
                        observation_id=observation_id,
                        observation_payload_sha256=bundle.observation_payload_sha256,
                    ),
                    rationale="Synthetic explicit economic review; no real issuer claim.",
                    evidence=(
                        RoleEvidence(
                            node_id=role_node_id,
                            text_sha256=hashlib.sha256(str(node[0]).encode()).hexdigest(),
                            locator_sha256=str(node[1]),
                        ),
                    ),
                )
            )
        conn.commit()
        return conn, RoleAdmissionRequest(
            issuer_id="issuer-1",
            research_snapshot_id=snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            as_of=cutoff,
            assignments=tuple(assignments),
        )
    except Exception:
        conn.close()
        raise


def test_full_unmocked_28_role_plan_apply_and_exact_replay(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        changes = conn.total_changes
        plan = plan_meli_role_admission(conn, original)
        assert plan.state == "planned", plan.blockers
        assert conn.total_changes == changes
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic-reviewer",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        receipt = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert receipt.inserted_revisions == 14
        assert receipt.model_ready is False
        assert receipt.next_step == "new_ontology_and_research_snapshot_required"
        conn.commit()
        conn.execute("BEGIN")
        replay = apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert replay.inserted_revisions == 0
        assert replay.definition_commitments == receipt.definition_commitments
        assert replay.applied_at == receipt.applied_at
        old_preview = preview_meli_inputs(
            conn,
            research_snapshot_id=original.research_snapshot_id,
            financial_period_end=PERIOD,
            as_of=original.as_of,
        )
        assert old_preview.state == "incomplete" and old_preview.facts == {}
        later = datetime.now(UTC)
        MetricOntology(conn).seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="ontology:reviewed-role",
                idempotency_key="ontology:reviewed-role",
                cutoff_at=later,
                recorded_at=later,
            )
        )
        new_resolutions = populate_canonical_resolution(
            conn,
            CanonicalResolutionPopulationRequest(
                cutoff_at=later, operation_recorded_at=later, apply=True
            ),
        )
        assert new_resolutions.state == "complete"
        scope = build_analysis_scope(
            conn,
            AnalysisScopeRequest(
                purpose="post_earnings_readout",
                issuer_id="issuer-1",
                inventory_key="issuer-1:sec",
                required_period_ends=(PERIOD,),
                cutoff_at=later,
                observed_through=later,
            ),
        )
        processing = populate_document_processing(
            conn,
            DocumentProcessingPopulationRequest(
                cutoff_at=later, operation_recorded_at=later, apply=True, analysis_scope=scope
            ),
        )
        assert processing.processing_snapshot_count == 1
        inventory, inventories = load_analysis_expected_document_inventory(
            conn, scope, cutoff_at=later, observed_through=later
        )
        conn.commit()
        build_grounded_search_corpus(
            conn,
            CorpusBuildRequest(
                corpus_key=scope.scope_id,
                revision=1,
                selector_code_version="synthetic-reviewed-roles@1",
                recorded_at=later,
                knowledge_cutoff=later,
                expected_documents=inventory.expected_documents,
                source_inventory_snapshot_ids=inventories,
                analysis_scope=scope,
                apply=True,
            ),
        )
        new_snapshot = assemble_research_snapshot_request(
            conn, "issuer-1", later, analysis_scope=scope, projection_mode="lexical_only"
        )
        assert build_research_snapshot(conn, new_snapshot).admitted
        new_preview = preview_meli_inputs(
            conn,
            research_snapshot_id=new_snapshot.research_snapshot_id,
            financial_period_end=PERIOD,
            as_of=later,
        )
        assert new_preview.state == "reported_inputs_verified_not_model_ready"
        assert len(new_preview.slots) == len(new_preview.facts) == 28
        assert all(slot.state == "matched" for slot in new_preview.slots)
        assert new_preview.model_ready is False
    finally:
        conn.close()


def test_transaction_and_revalidation_controls(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = sqlite3.connect(migrated_db(tmp_path / "controls.db"))
    try:
        with pytest.raises(ValueError, match="caller_read_transaction"):
            plan_meli_role_admission(conn, request())
        conn.execute("BEGIN")
        bypassed = request().model_copy(update={"assignments": request().assignments[:-1]})
        with pytest.raises(ValidationError, match="required_role_population_mismatch"):
            plan_meli_role_admission(conn, bypassed)
        blocked = plan_meli_role_admission(conn, request())
        with pytest.raises(ValidationError, match="review_requires_exact_planned_population"):
            ReviewedRoleAdmission(
                plan=blocked,
                plan_sha256=blocked.commitment_sha256,
                reviewer="synthetic",
                reviewed_at=AS_OF,
                decision="approved",
            )
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_role_plan_refuses_changed_current_subject(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        later = datetime.now(UTC)
        ReportingEntityRegistry(conn).persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id="subject:retired",
                idempotency_key="subject:retired",
                recorded_issuer_id="issuer-1",
                revision=2,
                outcome="retired",
                decision_kind="manual",
                material_dissent=False,
                reason_code="synthetic_subject_retirement",
                reason_details=(("source", "fixture"),),
                effective_at=later,
                knowledge_at=later,
                recorded_at=later,
                supersedes_binding_revision_id="binding-1",
            )
        )
        result = plan_meli_role_admission(conn, original.model_copy(update={"as_of": later}))
        assert result.state == "blocked", (
            "Old source subject must not authorize a current role candidate."
        )
        assert result.definitions == ()
        assert result.blockers == (
            "every research document must have the exact issuer and reporting entity",
        )
    finally:
        conn.close()


def test_reviewed_batch_rolls_back_after_one_definition(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        plan = plan_meli_role_admission(conn, original)
        assert plan.state == "planned", plan.blockers
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        metric = plan.definitions[1].parent.metric_id.replace("'", "''")
        conn.execute(
            "CREATE TEMP TRIGGER role_fixture_failure BEFORE INSERT ON canonical_metric_definition_revisions WHEN NEW.metric_id='"
            + metric
            + "' BEGIN SELECT RAISE(ABORT, 'synthetic_second_definition_stop'); END"
        )
        before = conn.execute(
            "SELECT COUNT(*) FROM canonical_metric_definition_revisions"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="synthetic_second_definition_stop"):
            apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
            == before
        )
        assert conn.in_transaction
        ontology = MetricOntology(conn)
        assert all(
            ontology.metric_definition_as_known(item.parent.metric_id, datetime.now(UTC))
            == item.parent
            for item in plan.definitions
        )
    finally:
        conn.close()


def test_role_plan_refuses_new_unresolved_candidate_graph(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        later = datetime.now(UTC)
        entry = filing_xbrl_entry(
            0, concept_name=requirements_for(PERIOD)[0].role, numeric_value=Decimal("9999")
        )
        requirement = requirements_for(PERIOD)[0]
        entry = NormalizedFilingXbrlFact.model_validate(
            {
                **entry.model_dump(),
                "evidence_node_id": "source-revision-node",
                "period_start": datetime.combine(
                    requirement.period_start or PERIOD, datetime.min.time(), UTC
                ),
                "period_end": datetime.combine(
                    requirement.period_end or PERIOD, datetime.min.time(), UTC
                ),
                "effective_at": datetime.combine(
                    requirement.period_end or PERIOD, datetime.min.time(), UTC
                ),
                "unit_key": requirement.unit_key,
                "source_entry_sha256": hashlib.sha256(b"synthetic-source-revision").hexdigest(),
                "knowledge_at": later,
                "recorded_at": later,
            }
        )
        base = filing_xbrl_output((entry,), extraction_run_id="source-revision-run")
        output = FilingXbrlNormalizedOutput.with_computed_digest(
            extraction=base.extraction.model_copy(
                update={
                    "knowledge_at": later,
                    "recorded_at": later,
                    "extractor_code_version": "synthetic-corrected-v2",
                }
            ),
            subject=base.subject,
            entries=(entry,),
        )
        insert_filing_xbrl_extraction_run(conn, output)
        FilingXbrlExtractionLedger(conn).publish(output)
        population = populate_metric_ontology(
            conn,
            MetricOntologyPopulationRequest(
                knowledge_cutoff=later, operation_recorded_at=later, apply=True
            ),
        )
        assert population.safe_to_seal
        new_candidates = CanonicalFactResolutionEngine(conn).candidate_manifest(
            original.assignments[0].fact.canonical_metric_cell_id, later, observed_through=later
        )
        assert len(new_candidates) == 2
        before = conn.total_changes
        plan = plan_meli_role_admission(conn, original.model_copy(update={"as_of": later}))
        assert plan.state == "blocked", (
            "A new unreviewed source candidate requires fresh canonical resolution."
        )
        assert "role_candidate_graph_changed" in str(plan.blockers)
        assert conn.total_changes == before
    finally:
        conn.close()


def test_review_rechecks_heads_evidence_inventory_and_partial_replay(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, original = synthetic_current_population(tmp_path, migrated_db)
    try:
        conn.execute("BEGIN")
        plan = plan_meli_role_admission(conn, original)
        assert plan.state == "planned", plan.blockers
        review = ReviewedRoleAdmission(
            plan=plan,
            plan_sha256=plan.commitment_sha256,
            reviewer="synthetic",
            reviewed_at=datetime.now(UTC),
            decision="approved",
        )
        ontology = MetricOntology(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM canonical_metric_definition_revisions"
        ).fetchone()[0]
        with pytest.raises(ValidationError, match="review_requires_exact_planned_population"):
            apply_reviewed_meli_role_admission(
                conn, review.model_copy(update={"plan_sha256": "0" * 64}), as_of=datetime.now(UTC)
            )
        with pytest.raises(ValueError, match="source_inventory_stale_or_future"):
            apply_reviewed_meli_role_admission(
                conn, review, as_of=original.as_of + timedelta(hours=25)
            )
        assignment = original.assignments[0]
        changed = original.model_copy(
            update={
                "assignments": (
                    assignment.model_copy(
                        update={
                            "evidence": (
                                assignment.evidence[0].model_copy(update={"text_sha256": "0" * 64}),
                            )
                        }
                    ),
                    *original.assignments[1:],
                )
            }
        )
        changed_plan = plan.model_copy(update={"request": changed})
        altered = review.model_copy(
            update={"plan": changed_plan, "plan_sha256": changed_plan.commitment_sha256}
        )
        with pytest.raises(ValueError, match="role_evidence_changed"):
            apply_reviewed_meli_role_admission(conn, altered, as_of=datetime.now(UTC))
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
            == count
        )
        item = plan.definitions[0]
        later = datetime.now(UTC)
        conn.execute("SAVEPOINT changed_head")
        ontology.persist_metric_definition(
            CanonicalMetricDefinitionRevision.model_validate(
                item.parent.model_copy(
                    update={
                        "metric_definition_revision_id": "synthetic:changed-head",
                        "idempotency_key": "synthetic:changed-head",
                        "revision": 2,
                        "supersedes_metric_definition_revision_id": item.parent.metric_definition_revision_id,
                        "definition_text": "An unrelated reviewed definition correction.",
                        "knowledge_at": later,
                        "recorded_at": later,
                    }
                ).model_dump()
            )
        )
        with pytest.raises(ValueError, match="role_definition_head_changed"):
            apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        conn.execute("ROLLBACK TO changed_head")
        conn.execute("RELEASE changed_head")
        ontology.persist_metric_definition(
            item.reviewed_successor(
                review_sha256=canonical_digest(review.model_dump(mode="json")),
                plan_sha256=review.plan_sha256,
                applied_at=later,
            )
        )
        partial_count = conn.execute(
            "SELECT COUNT(*) FROM canonical_metric_definition_revisions"
        ).fetchone()[0]
        with pytest.raises(ValueError, match="role_partial_replay_refused"):
            apply_reviewed_meli_role_admission(conn, review, as_of=datetime.now(UTC))
        assert (
            conn.execute("SELECT COUNT(*) FROM canonical_metric_definition_revisions").fetchone()[0]
            == partial_count
            == count + 1
        )
        assert conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize(
    "recorded_microsecond, offset_hours, expected_state",
    [
        (50_000, 0, "planned"),
        (100_000, 0, "planned"),
        (900_000, 0, "blocked"),
        (900_000, 5, "blocked"),
    ],
)
def test_role_context_evidence_uses_exact_aware_recorded_cutoff(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    recorded_microsecond: int,
    offset_hours: int,
    expected_state: str,
) -> None:
    # Explicit synthetic snapshot clocks leave the public backfill's actual clock
    # and every original source-fact clock intact. No wait or test deadline is used.
    second = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    cutoff = second.replace(microsecond=100_000)
    recorded = second.replace(microsecond=recorded_microsecond).astimezone(
        timezone(timedelta(hours=offset_hours))
    )
    conn, original = synthetic_current_population(
        tmp_path,
        migrated_db,
        contextual_recorded_at=recorded,
        snapshot_cutoff=cutoff,
    )
    try:
        assert len(original.assignments) == 28
        contextual = original.assignments[0].evidence[0]
        assert contextual.node_id == "role-context-node"
        stored = conn.execute(
            "SELECT recorded_at,parent_node_id,extraction_run_id FROM evidence_nodes "
            "WHERE node_id=?",
            (contextual.node_id,),
        ).fetchone()
        assert stored is not None
        assert datetime.fromisoformat(str(stored[0])).astimezone(UTC) == recorded.astimezone(UTC)
        assert stored[1] is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fact_observations_v2 observation "
                "JOIN fact_reported_observation_anchors_v2 anchor USING(observation_id) "
                "WHERE observation.evidence_node_id=?",
                (contextual.node_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fact_extraction_run_completeness_seals_v2 "
                "WHERE extraction_run_id=?",
                (stored[2],),
            ).fetchone()[0]
            == 1
        )
        snapshot = verify_research_snapshot(conn, original.research_snapshot_id)
        assert snapshot.admitted
        conn.execute("BEGIN")
        changes = conn.total_changes
        plan = plan_meli_role_admission(conn, original)
        assert plan.state == expected_state, plan.blockers
        if expected_state == "blocked":
            assert plan.blockers == (f"role_evidence_changed:{original.assignments[0].key}",)
            assert plan.definitions == ()
            assert plan.source_commitment_sha256 is None
        else:
            assert plan.blockers == ()
            assert len(plan.definitions) == 14
        assert plan.model_ready is False
        assert conn.total_changes == changes
        conn.rollback()
    finally:
        conn.close()
