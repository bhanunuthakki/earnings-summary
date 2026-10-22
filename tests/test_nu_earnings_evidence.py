"""NU qualitative earnings keep exact claims, prior evidence and IFRS contexts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    FinancialStatementContextReview,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
)
from provenance.metric_ontology import MetricOntology
from report.artifacts import (
    RenderedReportBody,
    ReportInteractionManifest,
    ReportSectionRef,
    persist_report_artifact,
)
from research import decision_brief as briefs
from research import nu_earnings_evidence as earnings
from research.memo_report_evidence import DecisionBriefReadiness
from sources.report_financials import FinancialEvidenceReference, read_financial_table
from tests.test_historical_analysis_scope import historical as historical
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database

PUBLIC_PRE = datetime(2026, 8, 12, 23, 59, 59, tzinfo=UTC)


def _pre_context() -> earnings.NuHistoricalEarningsContext:
    return earnings.NuHistoricalEarningsContext(
        recipe="nu-q2-2026-earnings/v1",
        phase="pre",
        fiscal_period="Q2 2026",
        valuation_mode="qualitative_only",
        public_cutoff_at=PUBLIC_PRE,
        inference_anchors=(
            earnings.NuInferenceAnchor(claim_block_id="b" * 64, anchor_block_ids=("a" * 64,)),
        ),
    )


def _claims() -> tuple[briefs.ReviewedMemoClaimV2, ...]:
    return (
        briefs.ReviewedMemoClaimV2(
            block_id="a" * 64,
            passage="A retained statement.",
            kind="management_claim",
            evidence_node_ids=("source",),
            rationale="The exact source wording supports this claim.",
        ),
        briefs.ReviewedMemoClaimV2(
            block_id="b" * 64,
            passage="Analyst inference: risk can remain elevated.",
            kind="analyst_inference",
            rationale="The forward view uses the retained factual anchor.",
        ),
    )


def _review() -> briefs.MemoContextReviewV2:
    return briefs.MemoContextReviewV2(
        artifact_id="pre",
        body_sha256="a" * 64,
        research_snapshot_id="pre-snapshot",
        claims=_claims(),
        reviewed_section_ids=("results",),
        reviewer="synthetic-analyst",
        reviewed_at=STAMP,
        rationale="Synthetic schema and policy proof, not financial acceptance.",
    )


def test_optional_context_preserves_legacy_and_closes_model_escape() -> None:
    legacy = _review()
    assert "earnings" not in legacy.model_dump(mode="json")
    assert briefs.parse_memo_context_review(legacy.model_dump_json()) == legacy
    reviewed = briefs.MemoContextReviewV2.model_validate(
        {**legacy.model_dump(), "earnings": _pre_context().model_dump()}
    )
    assert briefs.parse_memo_context_review(reviewed.model_dump_json()) == reviewed
    with pytest.raises(ValueError, match="nu_qualitative_earnings"):
        briefs.MemoContextReviewV2.model_validate(
            {
                **reviewed.model_dump(),
                "supporting_contexts": [
                    {
                        "context_id": "outside",
                        "issuer_id": "other",
                        "ticker": "OTHER",
                        "research_snapshot_id": "other",
                        "member_set_sha256": "a" * 64,
                    }
                ],
            }
        )
    invalid = _pre_context().model_dump()
    invalid["valuation_mode"] = "dcf"
    with pytest.raises(ValueError):
        earnings.NuHistoricalEarningsContext.model_validate(invalid)


def test_pre_forward_inference_population_requires_verified_source_anchors() -> None:
    context, claims = _pre_context(), _claims()
    earnings.verify_nu_inference_anchors(context, claims, frozenset(("a" * 64, "b" * 64)))
    with pytest.raises(earnings.NuEarningsEvidenceError, match="anchor_not_source_backed"):
        earnings.verify_nu_inference_anchors(context, claims, frozenset(("b" * 64,)))
    with pytest.raises(earnings.NuEarningsEvidenceError, match="population_mismatch"):
        earnings.verify_nu_inference_anchors(context, claims[:1], frozenset(("a" * 64,)))
    with pytest.raises(ValueError, match="source_anchored_expectations"):
        earnings.NuHistoricalEarningsContext.model_validate(
            {
                **context.model_dump(),
                "inference_anchors": [],
            }
        )
    with pytest.raises(ValueError, match="pre_cannot_claim_post"):
        earnings.NuHistoricalEarningsContext.model_validate(
            {
                **context.model_dump(),
                "comparisons": [_comparison().model_dump()],
            }
        )


def _comparison() -> earnings.NuFinancialComparison:
    return earnings.NuFinancialComparison(
        policy="nu-ifrs-quarter-comparison/v1",
        claim_block_id="c" * 64,
        concept="revenue",
        prior_period_end="2026-03-31",
        rationale="Same admitted IFRS consolidated concept and exact calendar quarters.",
    )


def _native_quarters(conn: sqlite3.Connection):
    """Real source publication, ontology, resolution and financial-role admission."""
    facts = seed_table(
        conn,
        [
            ("revenue", "2026-01-01", "2026-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2026-04-01", "2026-06-30", "Q2", "120000000", "USD"),
        ],
        ticker="NU",
        accounting_basis="ifrs",
    )
    heading = "Synthetic consolidated IFRS quarterly statements"
    locator = '{"path":"/statements/heading"}'
    conn.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'context-run','context-run',"
        "document_version_id,input_sha256,'statement-context-section',extractor_config_sha256,"
        "extractor_code_version,output_sha256,started_at,completed_at,outcome "
        "FROM evidence_extraction_runs WHERE extraction_run_id='report-run'"
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES ('context-node','context-node',1,"
        "'context-run',NULL,NULL,'section',?,?,?,?)",
        (heading, locator, hashlib.sha256(locator.encode()).hexdigest(), STAMP),
    )
    doc_sha = conn.execute(
        "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='report-document'"
    ).fetchone()[0]

    def admit(index: int, clock: datetime) -> None:
        observation = facts[index].observation.observation_id
        bundle = FactReadModel(conn).provenance_bundle(observation, cutoff=clock)
        binding = MetricOntology(conn).binding_as_known(observation, clock)
        assert binding is not None
        metric = conn.execute(
            "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
            (binding.canonical_metric_cell_id,),
        ).fetchone()[0]
        definition = MetricOntology(conn).metric_definition_as_known(metric, clock)
        assert definition is not None and bundle.cell.period_start is not None
        role = ReviewedFinancialStatementRole(
            observation_id=observation,
            observation_payload_sha256=bundle.observation_payload_sha256,
            expected_definition_revision_id=definition.metric_definition_revision_id,
            concept="revenue",
            context=FinancialStatementContextReview(
                document_version_id="report-document",
                document_sha256=doc_sha,
                issuer_id="issuer-1",
                reporting_entity_id=bundle.cell.reporting_entity_id,
                evidence_node_id="context-node",
                evidence_locator_sha256=hashlib.sha256(locator.encode()).hexdigest(),
                source_wording=heading,
                accounting_basis="ifrs",
                consolidation_scope="consolidated",
                source_scope_label="consolidated",
                period_start=bundle.cell.period_start,
                period_end=bundle.cell.period_end,
                fiscal_year=2026,
                fiscal_period="Q1" if index == 0 else "Q2",
                reviewer="synthetic-analyst",
                reviewed_at=STAMP,
                rationale="Synthetic heading binds exact IFRS consolidated quarter context.",
            ),
        )
        admit_reviewed_financial_statements(
            conn,
            FinancialStatementAdmissionRequest(
                roles=(role,),
                recorded_at=clock,
                apply=True,
            ),
        )

    def reference(end: str, clock: datetime) -> FinancialEvidenceReference:
        cell = next(
            c
            for c in read_financial_table(conn, "NU", as_of=clock).cells
            if c.provenance is not None and c.provenance.cell.period_end.date().isoformat() == end
        )
        assert cell.available and cell.provenance is not None
        assert cell.metric_definition_revision_id and cell.canonical_resolution_revision_id
        return FinancialEvidenceReference(
            ticker="NU",
            concept="revenue",
            canonical_metric_cell_id=cell.canonical_metric_cell_id,
            observation_id=cell.provenance.observation.observation_id,
            canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
            metric_definition_revision_id=cell.metric_definition_revision_id,
            as_of=clock,
        )

    pre_at, post_at = STAMP + timedelta(seconds=1), STAMP + timedelta(seconds=2)
    admit(0, pre_at)
    previous = reference("2026-03-31", pre_at)
    admit(1, post_at)
    return previous, reference("2026-03-31", post_at), reference("2026-06-30", post_at)


def test_real_ifrs_comparison_allows_reviewed_successor_without_weakening_ordinary_growth(
    database: sqlite3.Connection,
) -> None:
    previous, rebound, latest = _native_quarters(database)
    assert previous.metric_definition_revision_id != rebound.metric_definition_revision_id
    earnings.verify_nu_definition_successor(database, previous, rebound)
    calculation = briefs.MemoCalculation(
        operation="growth",
        operands=(latest, previous),
        displayed_value="20.0%",
        display_format="percentage1",
    )
    with pytest.raises(briefs.MemoEvidenceError, match="growth_comparability_unverified"):
        briefs.verify_memo_calculation(database, calculation)
    briefs.verify_memo_calculation(database, calculation, nu_comparison=_comparison())
    with pytest.raises(briefs.MemoEvidenceError, match="calculation_value_mismatch"):
        briefs.verify_memo_calculation(
            database,
            calculation.model_copy(
                update={
                    "displayed_value": "99.0%",
                }
            ),
            nu_comparison=_comparison(),
        )
    with pytest.raises(earnings.NuEarningsEvidenceError, match="calendar_mismatch"):
        briefs.verify_memo_calculation(
            database,
            calculation,
            nu_comparison=_comparison().model_copy(update={"prior_period_end": "2025-06-30"}),
        )
    with pytest.raises(earnings.NuEarningsEvidenceError, match="financial_identity_changed"):
        earnings.verify_nu_definition_successor(
            database,
            previous,
            rebound.model_copy(
                update={
                    "observation_id": "different-source-version",
                }
            ),
        )
    # Definition history is append-only. Change economics through its native owner.
    metric_id = str(
        database.execute(
            "SELECT metric_id FROM canonical_metric_definition_revisions "
            "WHERE metric_definition_revision_id=?",
            (rebound.metric_definition_revision_id,),
        ).fetchone()[0]
    )
    ontology = MetricOntology(database)
    head = ontology.metric_definition_as_known(metric_id, rebound.as_of)
    assert head is not None
    clock = rebound.as_of + timedelta(seconds=1)
    changed = head.model_copy(
        update={
            "metric_definition_revision_id": "changed-economics",
            "idempotency_key": "changed-economics",
            "revision": head.revision + 1,
            "supersedes_metric_definition_revision_id": head.metric_definition_revision_id,
            "definition_text": "Changed economics",
            "knowledge_at": clock,
            "recorded_at": clock,
        }
    )
    ontology.persist_metric_definition(changed)
    with pytest.raises(earnings.NuEarningsEvidenceError, match="definition_semantics_changed"):
        earnings.verify_nu_definition_successor(
            database,
            previous,
            rebound.model_copy(update={"metric_definition_revision_id": "changed-economics"}),
        )


def _pre_files(root: Path) -> earnings.NuPreBriefCommitment:
    body = RenderedReportBody.from_html(
        ticker="NU",
        report_date=STAMP.date(),
        fiscal_period_label="Q2 2026",
        body_html='<main data-report-body="v1"><section id="results"><p>Historical pre.</p></section></main>',
        sections=(ReportSectionRef(section_id="results", label="Results", group_id="results"),),
        interaction_manifest=ReportInteractionManifest(),
    )
    standalone = root / "standalone.html"
    standalone.write_text(body.body_html)
    artifact = persist_report_artifact(
        repo_root=root,
        body=body,
        standalone_path=standalone,
        generated_at=STAMP,
        coverage_role="evaluation",
        title="Synthetic historical pre",
    )
    review = _review().model_copy(
        update={
            "artifact_id": artifact.artifact_id,
            "body_sha256": artifact.body_sha256,
            "earnings": _pre_context(),
        }
    )
    directory = (root / artifact.manifest_path).parent
    review_path = directory / "context-review.json"
    review_path.write_text(review.model_dump_json())
    # A loader-unit fixture. The product must replay this receipt through every gate.
    readiness = DecisionBriefReadiness(
        artifact_id=artifact.artifact_id,
        ticker="NU",
        body_sha256=artifact.body_sha256,
        evaluated_at=STAMP + timedelta(seconds=1),
        status="verified",
        reason_codes=(),
        research_snapshot_id="pre-snapshot",
        snapshot_member_sha256="a" * 64,
        readiness_scope="historical_bounded",
        joint_mvp_status="not_verified",
        context_review_sha256=hashlib.sha256(review.model_dump_json().encode()).hexdigest(),
    )
    readiness_path = briefs.persist_decision_brief_readiness(root, readiness)
    return earnings.NuPreBriefCommitment(
        artifact_manifest_path=artifact.manifest_path,
        artifact_manifest_sha256=hashlib.sha256(
            (root / artifact.manifest_path).read_bytes()
        ).hexdigest(),
        context_review_path=str(review_path.relative_to(root)),
        context_review_sha256=hashlib.sha256(review_path.read_bytes()).hexdigest(),
        readiness_path=str(readiness_path.relative_to(root)),
        readiness_sha256=hashlib.sha256(readiness_path.read_bytes()).hexdigest(),
        public_cutoff_at=PUBLIC_PRE,
        research_snapshot_id="pre-snapshot",
        snapshot_member_sha256="a" * 64,
    )


@pytest.mark.parametrize("damage", ["review_bytes", "snapshot", "readiness_status", "path_escape"])
def test_pre_loader_rejects_changed_or_spoofed_seal(tmp_path: Path, damage: str) -> None:
    commitment = _pre_files(tmp_path)
    artifact, _review_bytes, readiness = earnings.load_nu_pre_brief(tmp_path, commitment)
    assert readiness.artifact_id == artifact.artifact_id
    if damage == "review_bytes":
        (tmp_path / commitment.context_review_path).write_text("changed")
    elif damage == "snapshot":
        commitment = commitment.model_copy(update={"research_snapshot_id": "spoofed-baseline"})
    elif damage == "readiness_status":
        path = tmp_path / commitment.readiness_path
        raw = json.loads(path.read_text())
        raw["status"] = "degraded"
        path.write_text(json.dumps(raw))
        commitment = commitment.model_copy(
            update={
                "readiness_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    else:
        commitment = commitment.model_copy(update={"context_review_path": "../outside.json"})
    with pytest.raises((earnings.NuEarningsEvidenceError, ValueError)):
        earnings.load_nu_pre_brief(tmp_path, commitment)


def _post_context(commitment: earnings.NuPreBriefCommitment):
    return earnings.NuHistoricalEarningsContext(
        recipe="nu-q2-2026-earnings/v1",
        phase="post",
        fiscal_period="Q2 2026",
        valuation_mode="qualitative_only",
        public_cutoff_at=datetime(2026, 8, 19, 3, 59, 59, tzinfo=UTC),
        comparisons=tuple(
            _comparison().model_copy(update={"claim_block_id": digit * 64, "concept": concept})
            for digit, concept in (("c", "revenue"), ("d", "gross_profit"), ("e", "net_income"))
        ),
        pre_brief=commitment,
    )


@pytest.mark.parametrize("damage", ["replay_changed", "pre_not_before_post", "missing_baseline"])
def test_post_replays_pre_receipt_and_rejects_unverified_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Control-flow unit proof; no doubled receipt is financial acceptance."""
    from provenance.research_snapshot import ResearchSnapshotRequest

    commitment = _pre_files(tmp_path)
    pre, _review_bytes, receipt = earnings.load_nu_pre_brief(tmp_path, commitment)
    post = pre.model_copy(update={"generated_at": STAMP + timedelta(seconds=3)})
    context = _post_context(commitment)
    review = _review().model_copy(update={"earnings": context, "claims": ()})
    snapshot = ResearchSnapshotRequest.model_construct(
        cutoff_at=STAMP + timedelta(seconds=4)
        if damage != "pre_not_before_post"
        else receipt.evaluated_at
    )
    calls: list[str] = []

    def replay(*_args: object, **kwargs: object) -> DecisionBriefReadiness:
        assert kwargs["as_of"] == receipt.evaluated_at
        assert kwargs["artifact"] == pre
        calls.append("pre-replayed")
        if damage == "replay_changed":
            return receipt.model_copy(
                update={"status": "degraded", "reason_codes": ("source_changed",)}
            )
        return receipt

    monkeypatch.setattr(briefs, "_assess_decision_brief", replay)
    expected = {
        "replay_changed": "pre_verification_replay_mismatch",
        "pre_not_before_post": "knowledge_order_or_commitment_mismatch",
        "missing_baseline": "comparison_not_verified",
    }[damage]
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(earnings.NuEarningsEvidenceError, match=expected),
    ):
        getattr(briefs, "_verify_nu_earnings")(
            conn,
            repo_root=tmp_path,
            artifact=post,
            review=review,
            snapshot=snapshot,
            verified_claim_ids=frozenset(),
            source_context=None,
        )
    assert calls == ([] if damage == "pre_not_before_post" else ["pre-replayed"])


def test_post_requires_three_distinct_concepts_and_exact_sealed_source_identity(tmp_path: Path):
    """Baseline binding unit proof, independent of admitted numerical replay above."""
    context = _post_context(_pre_files(tmp_path))
    pre_claims: list[briefs.ReviewedMemoClaimV2] = []
    post_claims: list[briefs.ReviewedMemoClaimV2] = []
    for item in context.comparisons:
        reference = FinancialEvidenceReference(
            ticker="NU",
            concept=item.concept,
            canonical_metric_cell_id="cell:" + item.concept,
            observation_id="observation:" + item.concept,
            canonical_resolution_revision_id="resolution:" + item.concept,
            metric_definition_revision_id="definition:" + item.concept,
            as_of=STAMP,
        )
        pre_claims.append(
            _claims()[0].model_copy(
                update={"block_id": item.claim_block_id, "financial_references": (reference,)}
            )
        )
        post_claims.append(
            _claims()[0].model_copy(
                update={
                    "block_id": item.claim_block_id,
                    "kind": "calculation",
                    "calculation": briefs.MemoCalculation(
                        operation="growth",
                        operands=(reference, reference),
                        displayed_value="0.0%",
                        display_format="percentage1",
                    ),
                }
            )
        )
    verified = frozenset(claim.block_id for claim in post_claims)
    with sqlite3.connect(":memory:") as conn:
        earnings.verify_nu_post_baselines(
            conn, context, tuple(post_claims), verified, tuple(pre_claims)
        )
        with pytest.raises(earnings.NuEarningsEvidenceError, match="not_in_sealed_pre"):
            earnings.verify_nu_post_baselines(
                conn, context, tuple(post_claims), verified, tuple(pre_claims[:2])
            )
        changed = context.model_copy(update={"comparisons": context.comparisons[:2]})
        with pytest.raises(earnings.NuEarningsEvidenceError, match="population_incomplete"):
            earnings.verify_nu_post_baselines(
                conn, changed, tuple(post_claims), verified, tuple(pre_claims)
            )


@pytest.mark.parametrize("historical", ["q1_pre"], indirect=True)
def test_v6_q1_call_data_profile_accepts_only_closed_nu_earnings_context(
    historical: tuple[sqlite3.Connection, dict[str, object]],
    tmp_path: Path,
) -> None:
    """Native May issuer-day proof joins generic readiness without MELI coverage."""
    from provenance.analysis_scope import (
        build_analysis_scope,
        require_analysis_documents,
        verify_analysis_scope,
    )
    from provenance.research_snapshot import ResearchSnapshotRequest, ResearchUniverse
    from tests import test_analysis_issuer_pdf_scope as issuer_pdf

    conn, payload = historical
    scope = build_analysis_scope(
        conn, getattr(issuer_pdf, "_historical_call")(conn, tmp_path, payload, pre=True)
    )
    verify_analysis_scope(conn, scope)
    assert scope.schema_version == "analysis-evidence-scope/v6"
    assert scope.request.readiness_profile == "complete_report_evidence/v1"
    documents = require_analysis_documents(
        conn, scope, scope.request.cutoff_at, scope.request.observed_through
    )
    call = next(w for w in scope.public_witnesses if w.document_version_id == "nu-call-version")
    assert call.raw_value == "2026-05-15T00:00:00.000Z"
    assert call.upper_at < PUBLIC_PRE
    assert scope.request.issuer_documents[0].issuer_day is not None
    # Only the closed context helper is under test here. This deliberately does
    # not construct a financial/research admission from an unsealed fixture.
    snapshot = ResearchSnapshotRequest.model_construct(
        research_universe=ResearchUniverse(
            issuer_id=scope.request.issuer_id,
            reporting_entity_ids=("synthetic-entity",),
            document_version_ids=documents,
            source_obligation_revision_ids=("current-duty", "nu-call-duty"),
            analysis_scope=scope,
        )
    )
    commitment = _pre_files(tmp_path)
    artifact, _raw, _receipt = earnings.load_nu_pre_brief(tmp_path, commitment)
    earnings.verify_nu_historical_context(_pre_context(), artifact, snapshot)
    wrong_scope = scope.model_copy(update={"schema_version": "analysis-evidence-scope/v4"})
    wrong = snapshot.model_copy(
        update={
            "research_universe": snapshot.research_universe.model_copy(
                update={"analysis_scope": wrong_scope}
            )
        }
    )
    with pytest.raises(
        earnings.NuEarningsEvidenceError, match="historical_source_context_required"
    ):
        earnings.verify_nu_historical_context(_pre_context(), artifact, wrong)
    # Ordinary NU has no closed earnings recipe and cannot claim MELI coverage.
    with pytest.raises(briefs.MemoClaimSupportError, match="recipe_unsupported"):
        briefs.verify_memo_report_evidence(
            _review(), scope_request=scope.request, ticker="NU", verified_claim_ids=frozenset()
        )
