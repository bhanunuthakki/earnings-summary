"""Bounded report proof declares more than the model's numerical population."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from provenance.analysis_scope import AnalysisEvidenceScope
from research import decision_brief as briefs
from research import memo_report_evidence as report_evidence
from research.memo_claim_support import MemoClaimSupportError
from tests.test_historical_analysis_scope import historical as historical

STAMP = datetime(2026, 10, 18, tzinfo=UTC)
REQUIREMENTS = (
    "assumptions_capital_needs",
    "business",
    "commerce",
    "credit",
    "downside_scenarios",
    "financial_baseline",
    "fintech",
    "geography",
    "operating",
    "valuation_sensitivity",
)


def _population() -> dict[str, object]:
    material: dict[str, object] = {
        "recipe": "meli_integrated_report.v1",
        "requirements": [
            {
                "requirement": name,
                "claim_block_ids": ["b" * 64],
                "rationale": "The reviewed block supports this exact analytical requirement.",
            }
            for name in REQUIREMENTS
        ],
    }
    material["population_sha256"] = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return material


def _review() -> dict[str, object]:
    return {
        "artifact_id": "meli-brief",
        "body_sha256": "a" * 64,
        "research_snapshot_id": "snapshot",
        "claims": [
            {
                "block_id": "b" * 64,
                "passage": "A supported reported fact.",
                "kind": "reported_fact",
                "evidence_node_ids": ["node"],
                "rationale": "Exact source wording is retained and reviewed.",
            }
        ],
        "reviewed_section_ids": ["company"],
        "reviewer": "analyst",
        "reviewed_at": STAMP,
        "rationale": "Full body and source context reviewed.",
    }


def test_report_population_is_additive_and_hash_bound() -> None:
    original = briefs.MemoContextReview.model_validate(_review())
    assert "report_evidence" not in original.model_dump(mode="json")
    review = briefs.MemoContextReview.model_validate(
        {**_review(), "report_evidence": _population()}
    )
    assert review.model_dump(mode="json")["report_evidence"] == _population()
    assert briefs.parse_memo_context_review(review.model_dump_json()) == review


def _rehash(population: dict[str, object]) -> dict[str, object]:
    material = {key: value for key, value in population.items() if key != "population_sha256"}
    return {
        **material,
        "population_sha256": hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest(),
    }


def _scope_request(profile: bool = True):
    from provenance.analysis_scope import AnalysisScopeRequest

    return AnalysisScopeRequest(
        purpose="integrated investment brief and DCF",
        issuer_id="issuer-meli",
        inventory_key="inventory",
        required_period_ends=(STAMP.date(),),
        require_latest_period=False,
        cutoff_at=STAMP,
        observed_through=STAMP,
        readiness_profile="complete_report_evidence/v1" if profile else None,
    )


def _witness(requirement: str) -> str:
    return (
        "b" * 64
        if requirement == REQUIREMENTS[0]
        else hashlib.sha256(requirement.encode()).hexdigest()
    )


def _grounded_review() -> briefs.MemoContextReview:
    payload = _review()
    payload["claims"] = [
        {
            "block_id": _witness(name),
            "passage": f"A supported reported fact for {name}.",
            "kind": "reported_fact",
            "evidence_node_ids": ["node:" + name],
            "rationale": "Source support was checked by the existing whole-output gate.",
        }
        for name in REQUIREMENTS
    ] + [
        {
            "block_id": "c" * 64,
            "passage": "Analyst inference: reviewed assumptions and downside.",
            "kind": "analyst_inference",
            "rationale": "Separate analyst reasoning with source-backed factual anchors.",
        }
    ]
    population = _population()
    raw_requirements = population["requirements"]
    assert isinstance(raw_requirements, list)
    requirements = cast(list[dict[str, object]], raw_requirements)
    for name, item in zip(REQUIREMENTS, requirements, strict=True):
        item["claim_block_ids"] = sorted([_witness(name), "c" * 64])
    return briefs.MemoContextReview.model_validate(
        {**payload, "report_evidence": _rehash(population)}
    )


def test_distinct_grounding_keeps_labelled_assumptions_but_requires_model_proof() -> None:
    review = _grounded_review()
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_model_support_missing"):
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=frozenset(claim.block_id for claim in review.claims),
        )


def test_report_declaration_without_native_profile_is_rejected() -> None:
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_profile_required"):
        report_evidence.verify_memo_report_evidence(
            _grounded_review(),
            scope_request=_scope_request(False),
            ticker="MELI",
            verified_claim_ids=frozenset(),
        )


def _remap(
    review: briefs.MemoContextReview, mapping: dict[str, list[str]]
) -> briefs.MemoContextReview:
    assert review.report_evidence is not None
    material = review.report_evidence.model_dump(mode="json")
    raw_rows = material["requirements"]
    assert isinstance(raw_rows, list)
    for row in cast(list[dict[str, object]], raw_rows):
        name = str(row["requirement"])
        if name in mapping:
            row["claim_block_ids"] = sorted(mapping[name])
    return review.model_copy(
        update={
            "report_evidence": report_evidence.MemoReportEvidencePopulation.model_validate(
                _rehash(material)
            )
        }
    )


def test_one_grounded_block_cannot_cover_all_ten_topics() -> None:
    review = _remap(_grounded_review(), {name: ["b" * 64, "c" * 64] for name in REQUIREMENTS})
    with pytest.raises(
        MemoClaimSupportError, match="memo_report_evidence_distinct_grounding_missing"
    ):
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=frozenset(claim.block_id for claim in review.claims),
        )


def test_overlapping_topics_use_a_complete_matching_not_a_greedy_choice() -> None:
    first, second = sorted((_witness(REQUIREMENTS[0]), _witness(REQUIREMENTS[1])))
    review = _remap(
        _grounded_review(),
        {
            REQUIREMENTS[0]: [first, second, "c" * 64],
            REQUIREMENTS[1]: [first, "c" * 64],
        },
    )
    # A complete matching exists. The next missing gate is actual model proof.
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_model_support_missing"):
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=frozenset(claim.block_id for claim in review.claims),
        )


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "unknown", "empty", "hash", "rationale"]
)
def test_report_requirement_population_is_closed(damage: str) -> None:
    from pydantic import ValidationError

    population = _population()
    raw_requirements = population["requirements"]
    assert isinstance(raw_requirements, list)
    requirements = cast(list[dict[str, object]], raw_requirements)
    if damage == "missing":
        requirements.pop(3)  # All28 model slots cannot replace a missing credit requirement.
    elif damage == "duplicate":
        requirements[3] = requirements[2]
    else:
        assert isinstance(requirements[0], dict)
        if damage == "unknown":
            requirements[0]["requirement"] = "model_slots_only"
        elif damage == "empty":
            requirements[0]["claim_block_ids"] = []
        elif damage == "rationale":
            requirements[0]["rationale"] = " " * 30
    if damage != "hash":
        population = _rehash(population)
    else:
        population["population_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        briefs.MemoContextReview.model_validate({**_review(), "report_evidence": population})


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("absent", "population_missing"),
        ("wrong_ticker", "recipe_unsupported"),
        ("unverified", "claim_unverified"),
        ("unknown_block", "claim_unverified"),
        ("inference_only", "grounded_support_missing"),
        ("fact_only", "analyst_or_model_support_missing"),
        ("tampered", "population_invalid"),
        ("removed_requirement", "population_invalid"),
    ],
)
def test_native_profile_refuses_unqualified_report_mapping(damage: str, reason: str) -> None:
    review = _grounded_review()
    verified: frozenset[str] = frozenset(claim.block_id for claim in review.claims)
    ticker = "MELI"
    if damage == "absent":
        review = review.model_copy(update={"report_evidence": None})
    elif damage == "wrong_ticker":
        ticker = "NU"
    elif damage == "unverified":
        verified = frozenset()
    elif damage in {"tampered", "removed_requirement"}:
        proof = review.report_evidence
        assert proof is not None
        updates = (
            {"population_sha256": "0" * 64}
            if damage == "tampered"
            else {"requirements": proof.requirements[:-1]}
        )
        review = review.model_copy(update={"report_evidence": proof.model_copy(update=updates)})
    else:
        proof = review.report_evidence
        assert proof is not None
        ids = (
            ("d" * 64,)
            if damage == "unknown_block"
            else ("c" * 64,)
            if damage == "inference_only"
            else ("b" * 64,)
        )
        material = proof.model_dump(mode="json")
        raw_rows = material["requirements"]
        assert isinstance(raw_rows, list)
        rows = cast(list[dict[str, object]], raw_rows)
        for row in rows:
            assert isinstance(row, dict)
            row["claim_block_ids"] = list(ids)
        review = review.model_copy(
            update={
                "report_evidence": report_evidence.MemoReportEvidencePopulation.model_validate(
                    _rehash(material)
                )
            }
        )
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_" + reason):
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker=ticker,
            verified_claim_ids=verified,
        )


def _bounded_readiness() -> briefs.DecisionBriefReadiness:
    proof = _grounded_review().report_evidence
    assert proof is not None
    return briefs.DecisionBriefReadiness(
        artifact_id="meli-brief",
        ticker="MELI",
        body_sha256="a" * 64,
        evaluated_at=STAMP,
        status="verified",
        decision_grade=False,
        reason_codes=(),
        research_snapshot_id="snapshot",
        snapshot_member_sha256="b" * 64,
        context_review_sha256="c" * 64,
        readiness_profile="complete_report_evidence/v1",
        readiness_scope="bounded_report",
        joint_mvp_status="not_verified",
        report_evidence_recipe="meli_integrated_report.v1",
        report_evidence_population=proof,
        report_evidence_sha256=proof.population_sha256,
        report_evidence_requirement_count=10,
        scope_limitation="Selected report evidence only; whole filing/issuer coverage deferred.",
    )


def test_bounded_acceptance_is_exact_and_not_status_alone() -> None:
    ready = _bounded_readiness()
    assert ready.report_evidence_qualified and not ready.decision_grade
    assert ready.joint_mvp_status == "not_verified"
    for field, value in (
        ("readiness_profile", None),
        ("readiness_scope", None),
        ("report_evidence_recipe", None),
        ("report_evidence_requirement_count", 28),
        ("report_evidence_sha256", None),
        ("report_evidence_sha256", "bad"),
        ("context_review_sha256", None),
        ("snapshot_member_sha256", None),
        ("research_snapshot_id", None),
        ("scope_limitation", None),
        ("reason_codes", ("missing_critical_evidence",)),
        ("status", "degraded"),
        ("ticker", "NU"),
    ):
        assert not ready.model_copy(update={field: value}).report_evidence_qualified, field
    legacy = briefs.DecisionBriefReadiness(
        artifact_id="memo",
        ticker="MELI",
        body_sha256="a" * 64,
        evaluated_at=STAMP,
        status="verified",
        decision_grade=True,
        reason_codes=(),
    )
    assert not legacy.report_evidence_qualified and legacy.decision_grade
    assert "readiness_profile" not in legacy.model_dump(mode="json")


def test_nested_population_and_context_commitment_are_retained(tmp_path: Path) -> None:
    ready = _bounded_readiness()
    path = briefs.persist_decision_brief_readiness(tmp_path, ready)
    replay = briefs.DecisionBriefReadiness.model_validate_json(path.read_bytes())
    assert replay == ready and replay.report_evidence_qualified
    assert replay.report_evidence_population == _grounded_review().report_evidence
    assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem
    assert not ready.model_copy(
        update={"report_evidence_sha256": "0" * 64}
    ).report_evidence_qualified
    assert not ready.model_copy(
        update={"report_evidence_population": None}
    ).report_evidence_qualified


def test_v2_review_legacy_serialization_stays_exact() -> None:
    review = briefs.MemoContextReviewV2.model_validate(
        {**_review(), "schema_version": "memo_context_review.v2"}
    )
    payload = review.model_dump(mode="json")
    assert "report_evidence" not in payload
    assert briefs.parse_memo_context_review(review.model_dump_json()) == review


def _artifact(tmp_path: Path, *, financial_anchor: str = "", ticker: str = "MELI"):

    from report.artifacts import (
        RenderedReportBody,
        ReportInteractionManifest,
        ReportSectionRef,
        persist_report_artifact,
    )

    body = RenderedReportBody.from_html(
        ticker=ticker,
        report_date=STAMP.date(),
        body_html=f'<main data-report-body="v1"><section id="company"><p>Test body.{financial_anchor}</p></section></main>',
        sections=(ReportSectionRef(section_id="company", label="Company", group_id="company"),),
        interaction_manifest=ReportInteractionManifest(),
    )
    standalone = tmp_path / "standalone.html"
    standalone.write_text(body.body_html)
    return persist_report_artifact(
        repo_root=tmp_path,
        body=body,
        standalone_path=standalone,
        generated_at=STAMP,
        coverage_role="evaluation",
        title=f"{ticker} test",
    )


@pytest.mark.parametrize("mode,expected", [("bounded", 3), ("status_only", 3), ("full", 0)])
def test_read_only_verifier_cli_uses_exact_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    expected: int,
) -> None:
    import sqlite3

    from execution import verify_decision_brief as cli
    from sqlite_runtime import SQLiteConnectionRole

    artifact = _artifact(tmp_path)
    ready = _bounded_readiness()
    if mode == "status_only":
        ready = ready.model_copy(update={"report_evidence_population": None})
    elif mode == "full":
        ready = briefs.DecisionBriefReadiness(
            artifact_id="memo",
            ticker="MELI",
            body_sha256="a" * 64,
            evaluated_at=STAMP,
            status="verified",
            decision_grade=True,
            reason_codes=(),
        )
    conn = sqlite3.connect(":memory:")
    calls: list[str] = []

    def connect(_database: object, **kwargs: object) -> sqlite3.Connection:
        assert kwargs["role"] == SQLiteConnectionRole.READ_ONLY
        calls.append("read_only")
        return conn

    def assess(_conn: sqlite3.Connection, **_kwargs: object) -> briefs.DecisionBriefReadiness:
        return ready

    def environment(_root: object) -> None:
        return None

    monkeypatch.setattr(cli, "connect_sqlite", connect)
    monkeypatch.setattr(cli, "assess_decision_brief", assess)
    monkeypatch.setattr(cli, "load_project_env", environment)
    database = tmp_path / "explicit.db"
    database.touch()
    assert (
        cli.main(
            [
                "--db",
                str(database),
                "--repo-root",
                str(tmp_path),
                "--artifact-manifest",
                str(tmp_path / artifact.manifest_path),
                "--source-state-root",
                str(tmp_path),
            ]
        )
        == expected
    )
    assert calls == ["read_only"]
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def _assess_native_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    scope: AnalysisEvidenceScope,
    population: report_evidence.MemoReportEvidencePopulation | None = None,
    financial_anchor: str = "",
    ticker: str = "MELI",
) -> briefs.DecisionBriefReadiness:
    """Exercise native assessment wiring, without claiming financial admission.

    Only the prior snapshot-admission result is doubled. The native saved
    request, scope hash, report artifact, and downstream degraded gates remain.
    """
    from provenance.research_snapshot import (
        CorpusProjectionBundle,
        ResearchSnapshotAdmission,
        ResearchSnapshotRequest,
        ResearchUniverse,
    )

    snapshot_at = scope.request.observed_through
    snapshot = ResearchSnapshotRequest(
        research_snapshot_id="profile-wiring-only",
        idempotency_key="profile-wiring-only",
        research_universe=ResearchUniverse(
            issuer_id=scope.request.issuer_id,
            reporting_entity_ids=("not-admitted-entity",),
            document_version_ids=("document:expected-10k",),
            source_obligation_revision_ids=("sec-periodic:v1",),
            analysis_scope=scope,
        ),
        processing_snapshot_ids=("not-admitted-processing",),
        corpus_bundles=(
            CorpusProjectionBundle(
                corpus_manifest_id="not-admitted-corpus",
                lexical_index_run_id="not-admitted-index",
            ),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="not-admitted-ontology",
        canonical_fact_resolution_snapshot_id="not-admitted-resolution",
        canonical_fact_projection_run_id="not-admitted-projection",
        cutoff_at=snapshot_at,
        recorded_at=snapshot_at,
    )
    raw = snapshot.model_dump_json()
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES(?,?,?,?,?,?)",
        (
            snapshot.research_snapshot_id,
            snapshot.idempotency_key,
            raw,
            hashlib.sha256(raw.encode()).hexdigest(),
            snapshot_at,
            snapshot_at,
        ),
    )

    def admission(_conn: object, snapshot_id: str) -> ResearchSnapshotAdmission:
        assert snapshot_id == snapshot.research_snapshot_id
        return ResearchSnapshotAdmission(
            research_snapshot_id=snapshot_id,
            cutoff_at=snapshot_at,
            member_count=1,
            member_set_sha256="d" * 64,
            requested_lanes=("test_wiring_only",),
        )

    monkeypatch.setattr(briefs, "verify_research_snapshot", admission)
    artifact = _artifact(tmp_path, financial_anchor=financial_anchor, ticker=ticker)
    review = briefs.MemoContextReview.model_validate(
        {
            **_review(),
            "artifact_id": artifact.artifact_id,
            "body_sha256": artifact.body_sha256,
            "research_snapshot_id": snapshot.research_snapshot_id,
            "reviewed_section_ids": artifact.section_ids,
            "report_evidence": population,
        }
    )
    return briefs.assess_decision_brief(
        conn,
        repo_root=tmp_path,
        artifact=artifact,
        as_of=max(STAMP, snapshot_at),
        context_review=review,
    )


@pytest.mark.parametrize("bounded", [True, False])
def test_native_assessment_reads_profile_from_saved_snapshot(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    bounded: bool,
) -> None:
    from provenance.analysis_scope import build_analysis_scope
    from tests.test_analysis_scope import scope_db, scope_request

    conn, _ = scope_db(tmp_path, migrated_db)
    try:
        scope = build_analysis_scope(
            conn,
            scope_request(readiness_profile="complete_report_evidence/v1" if bounded else None),
        )
        result = _assess_native_scope(tmp_path, monkeypatch, conn, scope)
        assert ("memo_report_evidence_population_missing" in result.reason_codes) is bounded
        assert result.readiness_profile == ("complete_report_evidence/v1" if bounded else None)
        assert result.joint_mvp_status == ("not_verified" if bounded else None)
        assert not result.report_evidence_qualified and not result.decision_grade
    finally:
        conn.close()


def test_native_historical_reconstruction_never_uses_full_grade_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope

    conn, payload = historical
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    assert scope.schema_version == "analysis-evidence-scope/v2"
    result = _assess_native_scope(tmp_path, monkeypatch, conn, scope)
    assert result.readiness_scope == "historical_bounded"
    assert result.joint_mvp_status == "not_verified"
    assert result.scope_limitation is not None and "historical" in result.scope_limitation.lower()
    assert not result.decision_grade


def test_native_full_scope_does_not_ignore_a_bounded_report_declaration(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_analysis_scope import scope_db

    conn, scope = scope_db(tmp_path, migrated_db)
    try:
        result = _assess_native_scope(
            tmp_path, monkeypatch, conn, scope, _grounded_review().report_evidence
        )
        assert "memo_report_evidence_profile_required" in result.reason_codes
        assert result.readiness_profile is None
        assert not result.report_evidence_qualified and not result.decision_grade
    finally:
        conn.close()


@pytest.mark.parametrize("tamper_ticker", [False, True])
def test_another_issuer_model_cannot_supply_meli_report_scenarios(tamper_ticker: bool) -> None:
    from research.memo_model_evidence import MemoModelCommitment, MemoModelValue

    review = _grounded_review()
    model = MemoModelCommitment(
        run_id=1,
        input_sha256="a" * 64,
        receipt_sha256="b" * 64,
        research_snapshot_id="snapshot",
        snapshot_member_sha256="c" * 64,
        effective_inputs_sha256="d" * 64,
        model_output_sha256="e" * 64,
        scenario_acceptance_sha256="f" * 64,
    )
    if tamper_ticker:
        model = model.model_copy(update={"ticker": "MELI"})
    value = MemoModelValue(
        source="scenario",
        key="vps",
        scenario="bear",
        unit="USD_per_share",
        displayed_unit="USD_per_share",
        display_format="number1",
        displayed_value="12.3",
    )
    v2 = briefs.MemoContextReviewV2.model_validate(
        {
            **review.model_dump(exclude={"schema_version"}),
            "schema_version": "memo_context_review.v2",
        }
    )
    v2 = v2.model_copy(
        update={
            "model": model,
            "claims": tuple(
                claim.model_copy(update={"model_values": (value,)}) for claim in v2.claims
            ),
        }
    )
    verified = frozenset(claim.block_id for claim in v2.claims)
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_model_support_missing"):
        report_evidence.verify_memo_report_evidence(
            v2,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=verified,
            verified_model_claim_ids=verified,
        )


def test_historical_grade_stays_bounded_when_other_assessment_gates_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    historical: tuple[sqlite3.Connection, dict[str, object]],
) -> None:
    """Unit-test native grading policy; doubled gates are not financial proof."""
    from urllib.parse import quote

    from dcf.grade_evidence import DcfGradeEvidence
    from dcf.input_evidence import ModelInputReceipt, ModelInputRequest
    from dcf.readiness import ValuationReadiness
    from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
    from provenance.research_snapshot import ResearchSnapshotRequest
    from sources.report_financials import FinancialEvidenceReference, FinancialTableCell

    conn, payload = historical
    scope = build_analysis_scope(conn, AnalysisScopeRequest.model_validate(payload))
    at = scope.request.observed_through
    model_request = ModelInputRequest(
        recipe="not-admitted-policy-double",
        ticker="NU",
        research_snapshot_id="profile-wiring-only",
        financial_period_end=at.date(),
        facts={},
        assumptions={},
    )
    model_receipt = ModelInputReceipt(
        recipe=model_request.recipe,
        request=model_request,
        verified_at=at,
        snapshot_member_sha256="d" * 64,
        effective_inputs_sha256="e" * 64,
        required_keys=(),
        inputs=(),
        inventory_snapshot_ids=(),
    )
    reference = FinancialEvidenceReference(
        ticker="NU",
        concept="revenue",
        canonical_metric_cell_id="policy-cell",
        observation_id="policy-observation",
        canonical_resolution_revision_id="policy-resolution",
        metric_definition_revision_id="policy-definition",
        as_of=at,
    )

    def cell(*_args: object, **_kwargs: object) -> FinancialTableCell:
        return FinancialTableCell(
            concept="revenue", canonical_metric_cell_id="policy-cell", metric_id="revenue"
        )

    def pass_gate(*_args: object, **_kwargs: object) -> None:
        return None

    def raw(*_args: object) -> int:
        return 1

    def valuation(*_args: object, **_kwargs: object) -> ValuationReadiness:
        return ValuationReadiness(
            ticker="NU",
            purpose="analyst_memo",
            evaluated_at=at.isoformat(),
            ready=True,
            status="ready",
        )

    def grade(*_args: object) -> DcfGradeEvidence:
        return DcfGradeEvidence(
            status="available",
            ticker="NU",
            provenance={"model_input_receipt": model_receipt.model_dump(mode="json")},
        )

    def coverage(*_args: object) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        row = conn.execute(
            "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
            (model_request.research_snapshot_id,),
        ).fetchone()
        assert row is not None
        return ResearchSnapshotRequest.model_validate_json(row[0]), "d" * 64, ()

    monkeypatch.setattr(briefs, "_read_memo_financial_cell", cell)
    monkeypatch.setattr(briefs, "_raw_documents", raw)
    for owner in (
        "verify_memo_claim_population",
        "verify_memo_section_population",
        "verify_memo_snapshot_reference",
        "verify_memo_snapshot_node",
        "verify_reported_memo_claim",
    ):
        monkeypatch.setattr(briefs, owner, pass_gate)
    monkeypatch.setattr(briefs, "load_valuation_readiness", valuation)
    monkeypatch.setattr(briefs, "load_dcf_verification_evidence", grade)
    monkeypatch.setattr(briefs, "verify_source_coverage", coverage)
    anchor = (
        '<a href="/api/peek/canonical-financial?reference='
        + quote(reference.model_dump_json(), safe="")
        + '">Source</a>'
    )
    result = _assess_native_scope(
        tmp_path, monkeypatch, conn, scope, financial_anchor=anchor, ticker="NU"
    )
    assert result.reason_codes == ("historical_report_evidence_recipe_absent",)
    assert result.status == "degraded"
    assert result.readiness_scope == "historical_bounded"
    assert result.joint_mvp_status == "not_verified"
    assert not result.decision_grade


@pytest.mark.parametrize("primary_support", ["other_issuer", "model_only"])
def test_meli_topic_witness_requires_primary_company_source_support(primary_support: str) -> None:
    from research.memo_model_evidence import MemoModelValue

    review = _grounded_review()
    value = MemoModelValue(
        source="scenario",
        key="vps",
        scenario="bear",
        unit="USD_per_share",
        displayed_unit="USD_per_share",
        display_format="number1",
        displayed_value="12.3",
    )
    v2 = briefs.MemoContextReviewV2.model_validate(
        {
            **review.model_dump(exclude={"schema_version"}),
            "schema_version": "memo_context_review.v2",
        }
    )
    victim_id = _witness("credit")
    claims = tuple(
        claim.model_copy(
            update={
                "evidence_node_ids": (),
                "supporting_evidence": (
                    briefs.MemoScopedSupport(
                        context_id="other-issuer", evidence_node_ids=("other-node",)
                    ),
                )
                if primary_support == "other_issuer"
                else (),
                "model_values": (value,) if primary_support == "model_only" else (),
            }
        )
        if claim.block_id == victim_id
        else claim
        for claim in v2.claims
    )
    v2 = v2.model_copy(
        update={
            "claims": claims,
            "supporting_contexts": (
                briefs.MemoSupportingContext(
                    context_id="other-issuer",
                    issuer_id="issuer-other",
                    ticker="OTHER",
                    research_snapshot_id="other-snapshot",
                    member_set_sha256="d" * 64,
                ),
            )
            if primary_support == "other_issuer"
            else (),
        }
    )
    with pytest.raises(
        MemoClaimSupportError, match="memo_report_evidence_grounded_support_missing"
    ):
        report_evidence.verify_memo_report_evidence(
            v2,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=frozenset(claim.block_id for claim in claims),
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"readiness_profile": "complete_report_evidence/v1"},
        {"readiness_scope": "historical_bounded"},
        {"readiness_scope": "bounded_report"},
    ],
)
def test_readiness_metadata_cannot_combine_bounded_scope_and_decision_grade(
    metadata: dict[str, str],
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="bounded_brief_cannot_be_decision_grade"):
        briefs.DecisionBriefReadiness.model_validate(
            {
                "artifact_id": "metadata-test",
                "ticker": "NU",
                "body_sha256": "a" * 64,
                "evaluated_at": STAMP,
                "status": "verified",
                "decision_grade": True,
                "reason_codes": (),
                **metadata,
            }
        )


def _model_grounded_review() -> briefs.MemoContextReviewV2:
    from research.memo_model_evidence import MeliMemoModelCommitment

    payload = _grounded_review().model_dump(mode="json")
    payload["schema_version"] = "memo_context_review.v2"
    payload["model"] = MeliMemoModelCommitment(
        run_id=1,
        research_snapshot_id="snapshot",
        **dict.fromkeys(
            (
                "input_sha256",
                "receipt_sha256",
                "snapshot_member_sha256",
                "effective_inputs_sha256",
                "model_output_sha256",
                "scenario_acceptance_sha256",
                "sensitivity_sha256",
            ),
            "a" * 64,
        ),
    ).model_dump(mode="json")
    claims = cast(list[dict[str, object]], payload["claims"])
    for claim in claims:
        if claim["block_id"] == _witness("downside_scenarios"):
            claim["model_values"] = [
                {
                    "model": "meli_platform_sotp",
                    "source": "scenario",
                    "key": "vps",
                    "scenario": "bear",
                    "unit": "USD_per_share",
                    "displayed_unit": "USD_per_share",
                    "display_format": "number2",
                    "displayed_value": "100.00",
                }
            ]
        elif claim["block_id"] == _witness("valuation_sensitivity"):
            claim["model_values"] = [
                {
                    "model": "meli_platform_sotp",
                    "source": "sensitivity",
                    "key": "vps",
                    "wacc_delta": "0.01",
                    "credit_ke_delta": "0",
                    "unit": "USD_per_share",
                    "displayed_unit": "USD_per_share",
                    "display_format": "number2",
                    "displayed_value": "110.00",
                }
            ]
    return briefs.MemoContextReviewV2.model_validate(payload)


def test_report_accepts_closed_meli_result_selectors_after_native_verification() -> None:
    review = _model_grounded_review()
    assert (
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=frozenset(c.block_id for c in review.claims),
            verified_model_claim_ids=frozenset(c.block_id for c in review.claims if c.model_values),
        )
        == review.report_evidence
    )


@pytest.mark.parametrize("damage", ["base_only", "assumption_only", "unchanged_grid", "unverified"])
def test_report_requires_actual_downside_and_changed_rate_results(damage: str) -> None:
    review = _model_grounded_review()
    claims = list(review.claims)
    for i, claim in enumerate(claims):
        if not claim.model_values:
            continue
        value = claim.model_values[0]
        update: dict[str, object] = {}
        if damage == "base_only" and claim.block_id == _witness("downside_scenarios"):
            update = {"scenario": "base"}
        elif damage == "assumption_only" and claim.block_id == _witness("downside_scenarios"):
            update = {
                "source": "scenario_input",
                "key": "wacc",
                "unit": "fraction",
                "displayed_unit": "fraction",
            }
        elif damage == "unchanged_grid" and claim.block_id == _witness("valuation_sensitivity"):
            update = {"wacc_delta": "0", "credit_ke_delta": "0"}
        if update:
            claims[i] = claim.model_copy(
                update={"model_values": (value.model_copy(update=update),)}
            )
    review = review.model_copy(update={"claims": tuple(claims)})
    verified = frozenset(c.block_id for c in claims)
    with pytest.raises(MemoClaimSupportError, match="memo_report_evidence_model_support_missing"):
        report_evidence.verify_memo_report_evidence(
            review,
            scope_request=_scope_request(),
            ticker="MELI",
            verified_claim_ids=verified,
            verified_model_claim_ids=frozenset() if damage == "unverified" else verified,
        )
