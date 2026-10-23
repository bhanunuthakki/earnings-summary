"""Scoped financial closure uses every admitted publication source leaf."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from provenance import research_snapshot as snapshots
from provenance.analysis_scope import (
    AnalysisEvidenceScope,
    AnalysisScopeRequest,
    build_analysis_scope,
)
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivationInputV2,
    DerivationSealV2,
    DerivedFactObservationV2,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.population_canonical_resolution import (
    complete_sealed_assertion_policy,
)
from provenance.population_metric_ontology import (
    MetricOntologyPopulationRequest,
    populate_metric_ontology,
)
from provenance.research_snapshot import (
    CorpusProjectionBundle,
    ResearchSnapshotRequest,
    ResearchUniverse,
)
from provenance.source_fact_repository import (
    DerivedSourceFact,
    ReportedSourceFact,
    SourceFactPublication,
    SourceFactRepository,
)
from runtime.deterministic import content_sha256_model
from tests import test_source_fact_repository as foundation
from tests.test_historical_analysis_scope import ISSUER, K
from tests.test_historical_analysis_scope import historical as historical


def _request(conn: sqlite3.Connection, payload: dict[str, object]) -> ResearchSnapshotRequest:
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    return ResearchSnapshotRequest(
        research_snapshot_id="historical-financial",
        idempotency_key="historical-financial",
        research_universe=ResearchUniverse(
            issuer_id=ISSUER,
            reporting_entity_ids=("reporting-historical",),
            document_version_ids=("original-financial",),
            source_obligation_revision_ids=("not-read-obligation",),
            analysis_scope=scope,
        ),
        processing_snapshot_ids=("not-read-processing",),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id="not-read-corpus", lexical_index_run_id="not-read-lexical"
            ),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="not-read-ontology",
        canonical_fact_resolution_snapshot_id="not-read-resolution",
        canonical_fact_projection_run_id="not-read-projection",
        cutoff_at=scope.request.cutoff_at,
        recorded_at=scope.request.observed_through,
    )


def _seed_subject(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO reporting_entities VALUES(?,?,?,?,?,?)",
        (
            "reporting-historical",
            "reporting-historical",
            ISSUER,
            "legal_registrant",
            "Synthetic",
            K,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-historical",
            "binding-historical",
            ISSUER,
            1,
            ISSUER,
            "reporting-historical",
            None,
            "selected",
            "deterministic",
            "exact_subject",
            "{}",
            0,
            K,
            K,
            K,
            None,
        ),
    )


def _fact(
    conn: sqlite3.Connection, suffix: str, *, outside: bool = False, concept: str = "revenue"
) -> tuple[ReportedSourceFact, ExtractionRunCompletenessSealV2]:
    document = "outside-original" if outside else "original-financial"
    if outside:
        conn.execute(
            "INSERT OR IGNORE INTO evidence_document_versions(document_version_id,document_key,version_sequence,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,recorded_at) SELECT ?,?,1,observation_id,blob_sha256,issuer_id,ticker,document_type,form_type,language,recorded_at FROM evidence_document_versions WHERE document_version_id='original-financial'",
            (document, document),
        )
    run, node = f"run-{suffix}", f"node-{suffix}"
    locator = '{"path":"/facts/value"}'
    blob = str(
        conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id=?",
            (document,),
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            run,
            run,
            document,
            blob,
            "synthetic",
            foundation.sha256(f"config-{suffix}"),
            "synthetic@1",
            foundation.sha256(suffix),
            K,
            K,
            "succeeded",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            node,
            node,
            1,
            run,
            None,
            None,
            "table_cell",
            "100",
            locator,
            foundation.sha256(locator),
            K,
        ),
    )
    cell = FactCellV2.model_validate(
        {
            **foundation.make_cell(suffix).model_dump(),
            "reporting_entity_id": "reporting-historical",
            "dimensions": (),
            "concept_namespace": "urn:earnings-summary:legacy:financial",
            "concept_name": concept,
            "taxonomy_name": "earnings-summary-legacy",
            "semantic_key_sha256": None,
            "period_start": K.replace(month=1, day=1),
            "period_end": K.replace(month=6, day=30),
            "effective_at": K,
            "knowledge_at": K,
            "recorded_at": K,
        }
    )
    observation = foundation.make_report(cell, suffix, at=K).model_copy(
        update={
            "document_version_id": document,
            "evidence_node_id": node,
            "subject_binding_revision_id": "binding-historical",
            "source_locator": CanonicalJSONObject({"path": "/facts/value"}),
            "source_locator_sha256": foundation.sha256(locator),
        }
    )
    seal = ExtractionRunCompletenessSealV2(
        extraction_seal_id=f"seal-{suffix}",
        idempotency_key=f"seal-{suffix}",
        extraction_run_id=run,
        expected_node_count=1,
        completeness_policy_name="all-run-nodes",
        completeness_policy_version="1",
        completeness_policy_sha256=foundation.sha256("complete"),
        knowledge_at=K,
        recorded_at=K,
    )
    return ReportedSourceFact(cell=cell, observation=observation), seal


def _publish(
    conn: sqlite3.Connection,
    identity: str,
    *items: tuple[ReportedSourceFact, ExtractionRunCompletenessSealV2],
) -> None:
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id=identity,
            idempotency_key=identity,
            created_at=K,
            recorded_at=K,
            reported_facts=tuple(item[0] for item in items),
            extraction_seals=tuple(item[1] for item in items),
        )
    )


def _resolved_request(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    *,
    excluded_observations: tuple[str, ...] = (),
    observed_through: datetime | None = None,
) -> ResearchSnapshotRequest:
    observed = K if observed_through is None else observed_through
    ontology = populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(
            knowledge_cutoff=K, operation_recorded_at=observed, apply=True
        ),
    )
    assert ontology.snapshot_id
    cutoff = K
    if excluded_observations:
        cutoff = K + timedelta(seconds=1)
        observed = max(observed, cutoff)
        store = MetricOntology(conn)
        for observation_id in excluded_observations:
            prior = store.binding_as_known(observation_id, K)
            assert prior is not None
            store.persist_binding(
                prior.model_copy(
                    update={
                        "binding_revision_id": f"retired-{observation_id}",
                        "idempotency_key": f"retired-{observation_id}",
                        "revision": prior.revision + 1,
                        "supersedes_binding_revision_id": prior.binding_revision_id,
                        "binding_status": "retired",
                        "effective_at": cutoff,
                        "knowledge_at": cutoff,
                        "recorded_at": cutoff,
                    }
                )
            )
        store.seal_snapshot(
            OntologySnapshot(
                ontology_snapshot_id="later-ontology",
                idempotency_key="later-ontology",
                cutoff_at=cutoff,
                recorded_at=cutoff,
            )
        )
        payload = {**payload, "cutoff_at": cutoff, "observed_through": cutoff}
    payload = {**payload, "cutoff_at": cutoff, "observed_through": observed}
    engine = CanonicalFactResolutionEngine(conn)
    for (cell_id,) in conn.execute(
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells ORDER BY canonical_metric_cell_id"
    ).fetchall():
        engine.resolve(
            str(cell_id), cutoff, complete_sealed_assertion_policy(), recorded_at=observed
        )
    engine.seal_snapshot(
        "source-closure-resolution",
        cutoff,
        observed,
        ResolutionSnapshotScope(issuer_id=ISSUER, reporting_entity_ids=("reporting-historical",)),
    )
    rows = conn.execute(
        "SELECT resolution_snapshot_id FROM canonical_fact_resolution_snapshot_scope_headers WHERE issuer_id=?",
        (ISSUER,),
    ).fetchall()
    assert len(rows) == 1
    snapshot = str(rows[0][0])
    publications = getattr(snapshots, "_required_source_fact_publications")(conn, snapshot)
    assert publications
    return _request(conn, payload).model_copy(
        update={
            "canonical_fact_resolution_snapshot_id": snapshot,
            "source_fact_publication_ids": publications,
        }
    )


def _as_report_profile(request: ResearchSnapshotRequest) -> ResearchSnapshotRequest:
    analysis = request.research_universe.analysis_scope
    assert analysis is not None
    payload = analysis.model_dump(mode="json", exclude={"scope_sha256", "scope_id"})
    payload["schema_version"] = "analysis-evidence-scope/v3"
    payload["request"] = analysis.request.model_copy(
        update={
            "historical_reconstruction": None,
            "readiness_profile": "complete_report_evidence/v1",
        }
    ).model_dump(mode="json")
    digest = content_sha256_model(payload)
    analysis = AnalysisEvidenceScope.model_validate(
        {**payload, "scope_sha256": digest, "scope_id": "analysis-scope:" + digest}
    )
    # This is a gate-unit envelope, not an admitted nonhistorical SEC scope.
    # The retained documents and native facts are real; full scope rebuilding
    # remains a separate mandatory check in _verify_research_universe.
    return request.model_copy(
        update={
            "research_universe": request.research_universe.model_copy(
                update={"analysis_scope": analysis}
            )
        }
    )


def _check(conn: sqlite3.Connection, request: ResearchSnapshotRequest) -> None:
    getattr(snapshots, "_verify_analysis_fact_source_closure")(conn, request)


@pytest.mark.parametrize("report_profile", [False, True])
@pytest.mark.parametrize("verify_fact_subjects", [True, False])
def test_historical_financial_snapshot_requires_native_resolution_even_with_custom_verifier(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    verify_fact_subjects: bool,
    report_profile: bool,
) -> None:
    conn, payload = historical
    request = _request(conn, payload)
    if report_profile:
        request = _as_report_profile(request)
    with pytest.raises(ValueError, match="canonical snapshot scope is missing"):
        getattr(snapshots, "_verify_research_universe")(
            conn, request, verify_fact_subjects=verify_fact_subjects
        )


@pytest.mark.parametrize("report_profile", [False, True])
def test_original_publication_sources_pass_without_writes(
    historical: tuple[sqlite3.Connection, dict[str, object]], report_profile: bool
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "original-publication", _fact(conn, "allowed"))
    request = _resolved_request(conn, payload)
    if report_profile:
        request = _as_report_profile(request)
    before = conn.total_changes
    _check(conn, request)
    assert conn.total_changes == before


@pytest.mark.parametrize("report_profile", [False, True])
@pytest.mark.parametrize("same_publication", [False, True])
def test_outside_dissent_or_unrelated_publication_member_fails(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    same_publication: bool,
    report_profile: bool,
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    allowed = _fact(conn, "allowed")
    outside = _fact(
        conn, "outside", outside=True, concept="unrelated" if same_publication else "Revenue"
    )
    if same_publication:
        _publish(conn, "mixed-publication", allowed, outside)
    else:
        _publish(conn, "original-publication", allowed)
        _publish(conn, "outside-publication", outside)
    request = _resolved_request(conn, payload)
    if report_profile:
        request = _as_report_profile(request)
    with pytest.raises(ValueError, match="analysis_research_fact_source_outside_originals"):
        _check(conn, request)


def test_publication_omission_fails_before_source_closure(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "original-publication", _fact(conn, "allowed"))
    request = _resolved_request(conn, payload).model_copy(
        update={"source_fact_publication_ids": ()}
    )
    with pytest.raises(ValueError, match="Source Fact Publications must exactly match"):
        _check(conn, request)


def test_empty_native_resolution_is_not_financial_coverage(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    CanonicalFactResolutionEngine(conn).seal_snapshot(
        "empty",
        K,
        K,
        ResolutionSnapshotScope(issuer_id=ISSUER, reporting_entity_ids=("reporting-historical",)),
    )
    request = _request(conn, payload).model_copy(
        update={"canonical_fact_resolution_snapshot_id": "empty"}
    )
    with pytest.raises(ValueError, match="analysis_research_fact_source_closure_empty"):
        _check(conn, request)


def test_later_outside_admission_does_not_change_pre_snapshot(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "original-publication", _fact(conn, "allowed"))
    request = _resolved_request(conn, payload)
    _check(conn, request)
    outside, seal = _fact(conn, "post", outside=True, concept="Revenue")
    later = K + timedelta(seconds=1)
    # A later physical admission retains its source version; it is not backdated.
    outside = outside.model_copy(
        update={
            "cell": outside.cell.model_copy(update={"knowledge_at": later, "recorded_at": later}),
            "observation": outside.observation.model_copy(
                update={"knowledge_at": later, "recorded_at": later}
            ),
        }
    )
    seal = seal.model_copy(update={"knowledge_at": later, "recorded_at": later})
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="post-publication",
            idempotency_key="post-publication",
            created_at=later,
            recorded_at=later,
            reported_facts=(outside,),
            extraction_seals=(seal,),
        )
    )
    _check(conn, request)


def _derived(
    source: ReportedSourceFact | DerivedSourceFact, suffix: str
) -> tuple[DerivedSourceFact, DerivationSealV2]:
    cell = source.cell
    observation = DerivedFactObservationV2(
        observation_id=f"observation-{suffix}",
        idempotency_key=f"observation-{suffix}",
        fact_cell_id=cell.fact_cell_id,
        observation_kind="derived",
        value_kind="numeric",
        numeric_value="200",
        method_name="formula-engine",
        method_version="1",
        method_config_sha256=foundation.sha256("formula"),
        revision_kind="initial",
        effective_at=K,
        knowledge_at=K,
        recorded_at=K,
        formula_id="synthetic-double",
        formula_version="1",
    )
    edge = DerivationInputV2(
        edge_id=f"edge-{suffix}",
        idempotency_key=f"edge-{suffix}",
        derived_observation_id=observation.observation_id,
        input_position=0,
        input_observation_id=source.observation.observation_id,
        input_role="base",
        recorded_at=K,
    )
    seal = DerivationSealV2(
        derivation_seal_id=f"derivation-{suffix}",
        idempotency_key=f"derivation-{suffix}",
        derived_observation_id=observation.observation_id,
        ordered_inputs=(edge,),
        input_basis="as_reported",
        formula_definition_sha256=foundation.sha256("formula-definition"),
        formula_config_sha256=foundation.sha256("formula-config"),
        seal_method="canonical-json",
        seal_method_version="1",
        effective_at=K,
        knowledge_at=K,
        recorded_at=K,
    )
    return DerivedSourceFact(cell=cell, observation=observation), seal


@pytest.mark.parametrize("report_profile", [False, True])
@pytest.mark.parametrize(
    "outside_operand, bound", [(False, None), (True, None), (False, "depth"), (False, "nodes")]
)
def test_recursive_operands_must_use_original_sources(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    outside_operand: bool,
    bound: str | None,
    report_profile: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    allowed = _fact(conn, "allowed")
    operand = _fact(conn, "operand", outside=outside_operand, concept="unmapped-operand")
    _publish(conn, "operand-publication", operand)
    first, first_seal = _derived(operand[0], "intermediate")
    second, second_seal = _derived(first, "nested")
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="required-publication",
            idempotency_key="required-publication",
            created_at=K,
            recorded_at=K,
            reported_facts=(allowed[0],),
            extraction_seals=(allowed[1],),
            derived_facts=(first, second),
            derivations=(first_seal, second_seal),
        )
    )
    request = _resolved_request(
        conn, payload, excluded_observations=(operand[0].observation.observation_id,)
    )
    if report_profile:
        request = _as_report_profile(request)
    # This operand is not itself a canonical candidate. Only recursive traversal
    # from a required publication can establish its source closure.
    assert request.source_fact_publication_ids == ("required-publication",)
    if bound is not None:
        monkeypatch.setattr(
            snapshots,
            "MAX_DERIVATION_DEPTH" if bound == "depth" else "MAX_DERIVATION_NODES",
            0 if bound == "depth" else 1,
        )
        with pytest.raises(ValueError, match="analysis_research_fact_derivation_bounds"):
            _check(conn, request)
    elif outside_operand:
        with pytest.raises(ValueError, match="analysis_research_fact_source_outside_originals"):
            _check(conn, request)
    else:
        _check(conn, request)


def test_corrupt_publication_cannot_pass_source_closure(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "original-publication", _fact(conn, "allowed"))
    request = _resolved_request(conn, payload)
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET canonical_member_sha256=? WHERE record_kind='fact_observation'",
        (foundation.sha256("corrupt"),),
    )
    with pytest.raises((ValueError, RuntimeError)):
        _check(conn, request)


@pytest.mark.parametrize("caller_transaction", [False, True])
def test_closure_cancellation_restores_transaction(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    caller_transaction: bool,
) -> None:
    from provenance.fact_read_model import FactReadModel

    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "original-publication", _fact(conn, "allowed"))
    request = _resolved_request(conn, payload)
    conn.commit()
    if caller_transaction:
        conn.execute("BEGIN")

    class Cancelled(BaseException):
        pass

    def cancel(*args: object, **kwargs: object) -> None:
        raise Cancelled()

    monkeypatch.setattr(FactReadModel, "required_provenance_bundles", cancel)
    with pytest.raises(Cancelled):
        _check(conn, request)
    assert conn.in_transaction is caller_transaction


def test_shared_operand_graph_is_closed_once_across_publication_roots(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    from types import FrameType

    conn, payload = historical
    _seed_subject(conn)
    allowed = _fact(conn, "allowed")
    shared, shared_seal = _derived(allowed[0], "shared")
    left, left_seal = _derived(shared, "left")
    right, right_seal = _derived(shared, "right")
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="shared-graph-publication",
            idempotency_key="shared-graph-publication",
            created_at=K,
            recorded_at=K,
            reported_facts=(allowed[0],),
            extraction_seals=(allowed[1],),
            derived_facts=(shared, left, right),
            derivations=(shared_seal, left_seal, right_seal),
        )
    )
    request = _resolved_request(conn, payload)
    calls = 0

    def observe(frame: FrameType, event: str, arg: object) -> None:
        nonlocal calls
        if (
            event == "call"
            and frame.f_code.co_name == "visit"
            and frame.f_code.co_filename == snapshots.__file__
        ):
            calls += 1

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        _check(conn, request)
    finally:
        sys.setprofile(previous)
    # Four unique vertices and three edges: shared operands must not expand
    # again merely because every committed observation is also a root.
    assert calls <= 4 + 3


def test_global_node_bound_holds_before_any_bundle_prefetch(
    historical: tuple[sqlite3.Connection, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from provenance.fact_read_model import FactReadModel

    conn, payload = historical
    _seed_subject(conn)
    _publish(conn, "two-members", _fact(conn, "first"), _fact(conn, "second", concept="Revenue"))
    request = _resolved_request(conn, payload)
    reads = 0
    original = FactReadModel.required_provenance_bundles

    def count(self: FactReadModel, ids: tuple[str, ...], *, cutoff: datetime):
        nonlocal reads
        reads += 1
        return original(self, ids, cutoff=cutoff)

    monkeypatch.setattr(FactReadModel, "required_provenance_bundles", count)
    monkeypatch.setattr(snapshots, "MAX_DERIVATION_NODES", 1)
    with pytest.raises(ValueError, match="analysis_research_fact_derivation_bounds"):
        _check(conn, request)
    assert reads == 0


def test_cached_subgraph_still_enforces_longest_path_depth(
    historical: tuple[sqlite3.Connection, dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, payload = historical
    _seed_subject(conn)
    allowed = _fact(conn, "allowed")
    shared, shared_seal = _derived(allowed[0], "aa-shared")
    parent, parent_seal = _derived(shared, "zz-parent")
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="deep-publication",
            idempotency_key="deep-publication",
            created_at=K,
            recorded_at=K,
            reported_facts=(allowed[0],),
            extraction_seals=(allowed[1],),
            derived_facts=(shared, parent),
            derivations=(shared_seal, parent_seal),
        )
    )
    request = _resolved_request(conn, payload)
    monkeypatch.setattr(snapshots, "MAX_DERIVATION_DEPTH", 1)
    with pytest.raises(ValueError, match="analysis_research_fact_derivation_bounds"):
        _check(conn, request)


def test_required_publication_operand_seal_after_k_is_not_read_at_o(
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    from provenance.fact_read_model import FactAdmissionError, FactReadModel
    from provenance.source_fact_publication import verify_source_fact_publication

    conn, payload = historical
    _seed_subject(conn)
    allowed = _fact(conn, "allowed")
    _publish(conn, "early-allowed", allowed)
    populate_metric_ontology(
        conn,
        MetricOntologyPopulationRequest(knowledge_cutoff=K, operation_recorded_at=K, apply=True),
    )
    operand = _fact(conn, "operand", concept="Revenue")
    derived, derivation = _derived(operand[0], "derived")
    observed = K + timedelta(seconds=1)
    operand_seal = operand[1].model_copy(update={"recorded_at": observed})
    SourceFactRepository(conn).publish(
        SourceFactPublication(
            publication_id="late-operand-seal",
            idempotency_key="late-operand-seal",
            created_at=K,
            recorded_at=observed,
            reported_facts=(allowed[0], operand[0]),
            extraction_seals=(allowed[1], operand_seal),
            derived_facts=(derived,),
            derivations=(derivation,),
        )
    )
    engine = CanonicalFactResolutionEngine(conn)
    for (cell_id,) in conn.execute(
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells"
    ).fetchall():
        engine.resolve(str(cell_id), K, complete_sealed_assertion_policy(), recorded_at=observed)
    engine.seal_snapshot(
        "late-operand-resolution",
        K,
        observed,
        ResolutionSnapshotScope(issuer_id=ISSUER, reporting_entity_ids=("reporting-historical",)),
    )
    request = _request(conn, {**payload, "observed_through": observed}).model_copy(
        update={
            "canonical_fact_resolution_snapshot_id": "late-operand-resolution",
            "source_fact_publication_ids": ("late-operand-seal",),
        }
    )
    publication = verify_source_fact_publication(
        conn, publication_id="late-operand-seal", cutoff=K, observed_through=observed
    )
    assert request.source_fact_publication_ids == (publication.publication_id,)
    assert any(
        member.record_id == operand_seal.extraction_seal_id for member in publication.members
    )
    assert (
        datetime.fromisoformat(
            str(
                conn.execute(
                    "SELECT recorded_at FROM fact_extraction_run_completeness_seals_v2 WHERE extraction_seal_id=?",
                    (operand_seal.extraction_seal_id,),
                ).fetchone()[0]
            )
        )
        == observed
    )
    assert FactReadModel(conn).required_provenance_bundles(
        (derived.observation.observation_id,), cutoff=observed
    )
    with pytest.raises(FactAdmissionError):
        _check(conn, request)


def test_untagged_six_k_receipt_does_not_invent_deferred_xbrl_duty(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import tzinfo

    from provenance import fulltext_backfill
    from provenance.document_processing_evidence import (
        publish_document_processing_evidence,
        verify_document_processing_evidence,
    )
    from provenance.reporting_entity_registry import (
        ReportingEntityRegistry,
        SourceObligationRevision,
    )
    from provenance.research_snapshot import (
        DocumentProcessingDisposition,
        DocumentProcessingPolicy,
        DocumentProcessingScope,
        ProcessingEvidenceReference,
        derive_obligations,
        record_disposition,
        seal_disposition,
        seal_processing_snapshot,
    )
    from provenance.source_coverage_reconcile import (
        ExpectedDocumentLifecycle,
        persist_expected_document_lifecycle,
    )

    class FixtureClockMeta(type):
        def __instancecheck__(cls, instance: object) -> bool:
            return isinstance(instance, datetime)

    class FixtureClock(datetime, metaclass=FixtureClockMeta):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return K if tz is not None else K.replace(tzinfo=None)

    conn, payload = historical
    _seed_subject(conn)
    ReportingEntityRegistry(conn).persist(
        SourceObligationRevision(
            obligation_revision_id="financial-duty",
            idempotency_key="financial-duty",
            obligation_key="financial-duty",
            revision=1,
            issuer_id=ISSUER,
            reporting_entity_id="reporting-historical",
            # The retained source is a SEC Archives filing, not an IR publisher.
            authority_kind="sec_edgar",
            document_family="issuer_financial_statements",
            obligation_state="required",
            completeness_rule="regulator_inventory",
            active_from=K,
            active_to=None,
            decision_kind="manual",
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
            reason_code="synthetic",
            reason_details=(("scope", "test"),),
        )
    )
    persist_expected_document_lifecycle(
        conn,
        ExpectedDocumentLifecycle(
            lifecycle_id="financial-lifecycle",
            idempotency_key="financial-lifecycle",
            inventory_key="synthetic:sec",
            expected_document_key="financial",
            source_inventory_snapshot_id="inventory",
            revision=1,
            status="expected",
            expected_document_id="financial",
            authority_observation_id="metadata",
            reason_code="synthetic_inventory",
            reason_details=(("source", "synthetic_fixture"),),
            effective_at=K,
            knowledge_at=K,
            recorded_at=K,
        ),
    )
    conn.commit()
    monkeypatch.setattr(fulltext_backfill, "datetime", FixtureClock)
    conn.row_factory = sqlite3.Row
    fulltext = fulltext_backfill.backfill_fulltext_evidence(
        conn,
        fulltext_backfill.FullTextBackfillRequest(
            repo_root=tmp_path,
            content_roots=(tmp_path,),
            source_lane="evidence_native",
            document_version_id="original-financial",
            apply=True,
        ),
    )
    assert fulltext.documents_extracted == 1
    analysis = build_analysis_scope(
        conn,
        AnalysisScopeRequest.model_validate(
            {**payload, "readiness_profile": "complete_report_evidence/v1"}
        ),
    )
    evidence = publish_document_processing_evidence(
        conn,
        document_version_id="original-financial",
        processing_lane="html_native_hierarchy",
        cutoff_at=K,
        recorded_at=K,
    )
    verified = verify_document_processing_evidence(
        conn,
        evidence.evidence_seal_id,
        document_version_id="original-financial",
        processing_lane="html_native_hierarchy",
        cutoff_at=K,
        observed_through=K,
    )
    selected = DocumentProcessingScope(
        document_version_ids=("original-financial",), analysis_scope=analysis
    )
    policy = DocumentProcessingPolicy(policy_name="six-k-receipt-proof", policy_version="1")
    for index, obligation in enumerate(
        derive_obligations(conn, selected, K, policy, recorded_at=K)
    ):
        identity = f"six-k-disposition:{index}"
        evidence_refs = (
            ()
            if obligation.applicability == "not_applicable"
            else (
                ProcessingEvidenceReference(
                    evidence_table="document_processing_evidence_seals",
                    evidence_id=verified.evidence_seal_id,
                    evidence_commitment_sha256=verified.member_set_sha256,
                    knowledge_at=verified.knowledge_at,
                    recorded_at=verified.recorded_at,
                ),
            )
        )
        record_disposition(
            conn,
            DocumentProcessingDisposition(
                processing_disposition_id=identity,
                idempotency_key=identity,
                processing_obligation_revision_id=obligation.processing_obligation_revision_id,
                terminal_status="not_applicable"
                if obligation.applicability == "not_applicable"
                else "succeeded",
                reason_code="qualified_native_six_k_processing",
                reason_details={},
                evidence=evidence_refs,
                knowledge_at=K,
                recorded_at=K,
            ),
        )
        seal_disposition(conn, identity, sealed_at=K)
    # This proves processing receipt contents through native public owners.
    # The selection-only base fixture has an immutable issuer-only expectation
    # binding; this does not claim global population or Research Snapshot admission.
    receipt = seal_processing_snapshot(
        conn,
        processing_snapshot_id="six-k-processing",
        idempotency_key="six-k-processing",
        scope=selected,
        cutoff_at=K,
        policy=policy,
        recorded_at=K,
    )
    assert receipt == snapshots.verify_processing_snapshot(conn, "six-k-processing")
    assert receipt.readiness_profile == "complete_report_evidence/v1"
    assert receipt.deferred_processing_lanes == ()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM document_processing_snapshot_members WHERE processing_lane='filing_xbrl'"
        ).fetchone()[0]
        == 0
    )
