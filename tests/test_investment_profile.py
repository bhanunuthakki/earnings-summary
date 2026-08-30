"""Deterministic investment-profile projection and owner-review contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from research.investment_profile import (
    CompanyProfileLabel,
    EtfProfileInputs,
    EtfProfileLabel,
    EtfStyleEvidence,
    InvestmentProfileSuggestion,
    LabelReviewAction,
    MoatAssessment,
    MoatEvidenceCoverage,
    MoatLevel,
    ProfileLabelState,
    ProfileProjectionState,
    ValuationEvidence,
    derive_company_label_evidence,
    derive_etf_label_evidence,
    project_company_profile,
    project_etf_profile,
    record_label_review,
    resolve_label_presentations,
)

_REVIEWS_DDL = """
CREATE TABLE investment_profile_label_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    suggestion_fingerprint TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_REVIEWS_DDL)
    return conn


def test_moat_scale_has_four_business_durability_levels() -> None:
    assert [level.value for level in MoatLevel] == [
        "multi_business",
        "core_business",
        "narrow_conditional",
        "none_demonstrated",
    ]
    assert MoatLevel.MULTI_BUSINESS.display_label == "Multi-business moat"
    assert MoatLevel.NONE_DEMONSTRATED.display_label == "No demonstrated moat"


def test_insufficient_evidence_is_coverage_not_a_moat_level() -> None:
    assessment = MoatAssessment(
        level=None,
        evidence_coverage=MoatEvidenceCoverage.INSUFFICIENT,
        rationale="The available corpus does not cover durability across a full cycle.",
    )
    assert assessment.level is None

    with pytest.raises(ValidationError):
        MoatAssessment(
            level=MoatLevel.NONE_DEMONSTRATED,
            evidence_coverage=MoatEvidenceCoverage.INSUFFICIENT,
            rationale="Missing evidence must not become an adverse conclusion.",
        )


def test_qualitative_suggestion_rejects_dcf_owned_valuation_labels() -> None:
    with pytest.raises(ValidationError):
        InvestmentProfileSuggestion(
            labels=[CompanyProfileLabel.GARP],
            summary="A valuation label cannot be authored by the model.",
            moat=MoatAssessment(
                level=MoatLevel.CORE_BUSINESS,
                evidence_coverage=MoatEvidenceCoverage.SUFFICIENT,
                rationale="The primary engine has durable switching costs.",
            ),
        )


def test_dcf_projection_adds_garp_only_from_complete_rule_inputs() -> None:
    evidence = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.GROWTH_INFLECTION],
        qualitative_source_sha="card-sha",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=28.0,
            fcf_margin_pct=18.0,
            dcf_upside_pct=24.0,
        ),
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    assert set(evidence) == {
        CompanyProfileLabel.GROWTH_INFLECTION,
        CompanyProfileLabel.GARP,
    }
    assert evidence[CompanyProfileLabel.GARP].source_kind == "dcf_rule"

    missing_dcf = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.GROWTH_INFLECTION],
        qualitative_source_sha="card-sha",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=28.0,
            fcf_margin_pct=18.0,
            dcf_upside_pct=None,
        ),
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    assert CompanyProfileLabel.GARP not in missing_dcf


def test_expensive_elite_requires_growth_valuation_and_quality_evidence() -> None:
    valuation = ValuationEvidence(
        revenue_growth_yoy_pct=32.0,
        fcf_margin_pct=22.0,
        dcf_upside_pct=-18.0,
    )
    elite = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.LONG_TERM_COMPOUNDER],
        qualitative_source_sha="card-sha",
        valuation=valuation,
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    assert CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE in elite

    no_quality_case = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.GROWTH_INFLECTION],
        qualitative_source_sha="card-sha",
        valuation=valuation,
        moat_level=MoatLevel.NARROW_CONDITIONAL,
    )
    assert CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE not in no_quality_case


def test_owner_review_is_append_only_idempotent_and_does_not_change_suggestion() -> None:
    conn = _conn()
    evidence = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.LONG_TERM_COMPOUNDER],
        qualitative_source_sha="card-sha",
        valuation=ValuationEvidence(),
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    source = evidence[CompanyProfileLabel.LONG_TERM_COMPOUNDER]

    first = record_label_review(
        conn,
        ticker="acme",
        label=CompanyProfileLabel.LONG_TERM_COMPOUNDER,
        action=LabelReviewAction.RATIFY,
        suggestion_fingerprint=source.fingerprint,
        evidence=source.evidence,
    )
    second = record_label_review(
        conn,
        ticker="ACME",
        label=CompanyProfileLabel.LONG_TERM_COMPOUNDER,
        action=LabelReviewAction.RATIFY,
        suggestion_fingerprint=source.fingerprint,
        evidence=source.evidence,
    )
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM investment_profile_label_reviews").fetchone()[0] == 1

    presentation = resolve_label_presentations(conn, ticker="ACME", suggestions=evidence)
    assert len(presentation) == 1
    assert presentation[0].state is ProfileLabelState.OWNER_RATIFIED
    assert presentation[0].suggested is True


def test_dcf_label_removal_creates_review_without_overwriting_ratification() -> None:
    conn = _conn()
    prior = derive_company_label_evidence(
        qualitative_labels=[],
        qualitative_source_sha="card-sha",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=25.0,
            fcf_margin_pct=12.0,
            dcf_upside_pct=22.0,
        ),
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    garp = prior[CompanyProfileLabel.GARP]
    record_label_review(
        conn,
        ticker="ACME",
        label=CompanyProfileLabel.GARP,
        action=LabelReviewAction.RATIFY,
        suggestion_fingerprint=garp.fingerprint,
        evidence=garp.evidence,
    )

    after_dcf = derive_company_label_evidence(
        qualitative_labels=[],
        qualitative_source_sha="card-sha",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=25.0,
            fcf_margin_pct=12.0,
            dcf_upside_pct=-8.0,
        ),
        moat_level=MoatLevel.CORE_BUSINESS,
    )
    presentation = resolve_label_presentations(conn, ticker="ACME", suggestions=after_dcf)

    assert len(presentation) == 1
    assert presentation[0].label is CompanyProfileLabel.GARP
    assert presentation[0].state is ProfileLabelState.REVIEW_SUGGESTED
    assert presentation[0].suggested is False


def test_material_evidence_change_returns_ratified_label_to_review() -> None:
    conn = _conn()
    initial = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.TURNAROUND],
        qualitative_source_sha="card-v1",
        valuation=ValuationEvidence(),
        moat_level=MoatLevel.NARROW_CONDITIONAL,
    )
    suggestion = initial[CompanyProfileLabel.TURNAROUND]
    record_label_review(
        conn,
        ticker="ACME",
        label=CompanyProfileLabel.TURNAROUND,
        action=LabelReviewAction.RATIFY,
        suggestion_fingerprint=suggestion.fingerprint,
        evidence=suggestion.evidence,
    )

    revised = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.TURNAROUND],
        qualitative_source_sha="card-v2",
        valuation=ValuationEvidence(),
        moat_level=MoatLevel.NARROW_CONDITIONAL,
    )
    presentation = resolve_label_presentations(conn, ticker="ACME", suggestions=revised)

    assert presentation[0].state is ProfileLabelState.REVIEW_SUGGESTED
    assert presentation[0].suggested is True


def test_rejected_suggestion_stays_suppressed_until_material_evidence_changes() -> None:
    conn = _conn()
    initial = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.TURNAROUND],
        qualitative_source_sha="card-v1",
        valuation=ValuationEvidence(),
        moat_level=MoatLevel.NARROW_CONDITIONAL,
    )
    suggestion = initial[CompanyProfileLabel.TURNAROUND]
    record_label_review(
        conn,
        ticker="ACME",
        label=CompanyProfileLabel.TURNAROUND,
        action=LabelReviewAction.REJECT,
        suggestion_fingerprint=suggestion.fingerprint,
        evidence=suggestion.evidence,
    )
    assert resolve_label_presentations(conn, ticker="ACME", suggestions=initial) == []

    revised = derive_company_label_evidence(
        qualitative_labels=[CompanyProfileLabel.TURNAROUND],
        qualitative_source_sha="card-v2",
        valuation=ValuationEvidence(),
        moat_level=MoatLevel.NARROW_CONDITIONAL,
    )
    presentation = resolve_label_presentations(conn, ticker="ACME", suggestions=revised)
    assert presentation[0].state is ProfileLabelState.REVIEW_SUGGESTED


def test_company_projection_recomputes_dcf_labels_without_regenerating_or_mutating_card() -> None:
    conn = _conn()
    conn.executescript(
        """
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            purpose TEXT NOT NULL,
            content_json TEXT,
            input_sha256 TEXT NOT NULL,
            superseded_by_id INTEGER
        );
        """
    )
    card = {
        "investment_profile": {
            "labels": ["long_term_compounder"],
            "summary": "A durable core engine with a long reinvestment runway.",
            "moat": {
                "level": "core_business",
                "evidence_coverage": "sufficient",
                "rationale": "The core network has durable density advantages.",
            },
        }
    }
    conn.execute(
        "INSERT INTO llm_artifacts VALUES (7,'ACME','investment_decision_card',?,?,NULL)",
        (json.dumps(card), "card-input-v2"),
    )

    first = project_company_profile(
        conn,
        ticker="ACME",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=30.0,
            fcf_margin_pct=20.0,
            dcf_upside_pct=20.0,
        ),
    )
    second = project_company_profile(
        conn,
        ticker="ACME",
        valuation=ValuationEvidence(
            revenue_growth_yoy_pct=30.0,
            fcf_margin_pct=20.0,
            dcf_upside_pct=-20.0,
        ),
    )

    assert [item.label for item in first.labels] == [
        CompanyProfileLabel.LONG_TERM_COMPOUNDER,
        CompanyProfileLabel.GARP,
    ]
    assert [item.label for item in second.labels] == [
        CompanyProfileLabel.LONG_TERM_COMPOUNDER,
        CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE,
    ]
    assert first.refresh_fingerprint != second.refresh_fingerprint
    assert first.source_artifact_id == second.source_artifact_id == 7
    assert conn.execute("SELECT COUNT(*) FROM investment_profile_label_reviews").fetchone()[0] == 0


def test_etf_seed_labels_are_derived_from_typed_fund_and_book_evidence() -> None:
    evidence = derive_etf_label_evidence(
        EtfProfileInputs(
            profile_available=True,
            asset_class="equity",
            sector_label="Energy",
            expense_ratio=0.001,
            distribution_yield=0.035,
            style_evidence_available=True,
            style_loadings=[
                EtfStyleEvidence(key="value", beta=0.48, r_squared=0.32),
            ],
            book_evidence_available=True,
            diversification_multiplier=1.2,
            overlap_multiplier=1.08,
            sharpe_delta_bps=18.0,
            whatif_evidence_available=True,
            vol_before_ann=0.15,
            vol_after_ann=0.14,
        )
    )

    assert set(evidence) == {
        EtfProfileLabel.FACTOR_SLEEVE,
        EtfProfileLabel.THEMATIC_EXPOSURE,
        EtfProfileLabel.DIVERSIFIER,
        EtfProfileLabel.DEFENSIVE_HEDGE,
        EtfProfileLabel.INCOME,
        EtfProfileLabel.TACTICAL_CYCLICAL,
    }
    assert evidence[EtfProfileLabel.DIVERSIFIER].summary == (
        "Low book correlation and limited look-through duplication"
    )


def test_etf_core_beta_requires_an_exact_broad_index_and_low_fee() -> None:
    evidence = derive_etf_label_evidence(
        EtfProfileInputs(
            profile_available=True,
            asset_class="equity",
            benchmark_index="S&P 500 Index",
            expense_ratio=0.0003,
            style_evidence_available=True,
            book_evidence_available=False,
            whatif_evidence_available=False,
        )
    )
    assert set(evidence) == {EtfProfileLabel.CORE_BETA}

    unrecognized = derive_etf_label_evidence(
        EtfProfileInputs(
            profile_available=True,
            asset_class="equity",
            benchmark_index="An approximately broad benchmark",
            expense_ratio=0.0003,
            style_evidence_available=True,
            book_evidence_available=False,
            whatif_evidence_available=False,
        )
    )
    assert EtfProfileLabel.CORE_BETA not in unrecognized


def test_etf_projection_is_pending_without_evidence_and_owner_review_is_reused() -> None:
    conn = _conn()
    pending = project_etf_profile(conn, ticker="VDE", inputs=EtfProfileInputs())
    assert pending.state is ProfileProjectionState.UNAVAILABLE
    assert pending.evidence_coverage.value == "insufficient"
    assert pending.labels == []

    inputs = EtfProfileInputs(
        profile_available=True,
        distribution_yield=0.04,
        style_evidence_available=False,
        book_evidence_available=False,
        whatif_evidence_available=False,
    )
    first = project_etf_profile(conn, ticker="VDE", inputs=inputs)
    income = first.labels[0]
    assert income.label is EtfProfileLabel.INCOME
    record_label_review(
        conn,
        ticker="VDE",
        label=income.label,
        action=LabelReviewAction.RATIFY,
        suggestion_fingerprint=income.suggestion_fingerprint,
        evidence={"source_kind": income.source_kind},
    )
    ratified = project_etf_profile(conn, ticker="VDE", inputs=inputs)
    assert ratified.state is ProfileProjectionState.OWNER_RATIFIED
    assert ratified.labels[0].state is ProfileLabelState.OWNER_RATIFIED
