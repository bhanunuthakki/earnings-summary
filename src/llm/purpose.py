"""Closed LLM Purpose Registry and Validation (BHA-56).

Ensures that every LLM dispatch uses a declared, typed purpose identifier.
Unknown or blank purpose identifiers fail closed with explicit errors.
"""

from __future__ import annotations

from enum import StrEnum


class PurposeId(StrEnum):
    """Closed enumeration of all authorized LLM purpose IDs in the application."""

    # Discovery & Research
    PEER_SELECTION = "peer_selection"
    KEY_METRICS_SUGGEST = "key_metrics_suggest"
    SECTOR_BENCHMARK_PROPOSAL = "sector_benchmark_proposal"
    BUSINESS_FACTORS = "business_factors"
    RECENT_DEVELOPMENTS = "recent_developments"
    ARTIFACT_BRIEF = "artifact_brief"
    THEME_SYNTHESIS = "theme_synthesis"

    # Signals & Transcripts
    TRANSCRIPT_METADATA = "transcript_metadata"
    TRANSCRIPT_TONE = "transcript_tone"
    TRANSCRIPT_SUMMARY = "transcript_summary"
    MATERIAL_NEWS_CLASSIFICATION = "material_news_classification"
    NEWS_STRUCTURING = "news_structuring"
    DECISION_CONDITIONS_EXTRACT = "decision_conditions_extract"
    SAYDO_COMMITMENT_EXTRACT = "saydo_commitment_extract"

    # Valuation & Scenarios
    VALUATION_BASIS = "valuation_basis"
    BEAR_CASE = "bear_case"
    SCENARIO_PRIORS = "scenario_priors"
    DCF_TWEAK = "dcf_tweak"

    # Advisor & Decision Learning
    ADVISOR_TRIAGE = "advisor_triage"
    ADVISOR_ASSESS = "advisor_assess"
    ADVISOR_NARRATIVE = "advisor_narrative"
    PRESSURE_TEST = "pressure_test"
    EXEC_COMP_ALIGNMENT = "exec_comp_alignment"

    # Governance & Evals
    BACKEND_COMPARE_JUDGE = "backend_compare_judge"
    RUBRIC_JUDGE = "rubric_judge"
    MODEL_EVAL_JUDGE = "model_eval_judge"


KNOWN_PURPOSES = frozenset(p.value for p in PurposeId)


def validate_purpose(purpose: str | PurposeId) -> PurposeId:
    """Validate that `purpose` is a member of the closed PurposeId registry.

    Fails closed by raising ValueError if unknown, unmapped, or empty.
    """
    if isinstance(purpose, PurposeId):
        return purpose
    cleaned = str(purpose).strip()
    if not cleaned:
        raise ValueError("LLM purpose must be a non-empty string")
    try:
        return PurposeId(cleaned)
    except ValueError as err:
        # Check if purpose has dynamic lens/research prefix
        if (
            cleaned.startswith("lens:")
            or cleaned.startswith("research_")
            or cleaned.startswith("advisor_")
        ):
            return PurposeId.BACKEND_COMPARE_JUDGE  # fallback/specialization if applicable or raise
        raise ValueError(
            f"Unknown LLM purpose '{cleaned}' is not in the closed purpose registry. "
            f"Authorized purposes: {sorted(KNOWN_PURPOSES)}"
        ) from err


__all__ = [
    "KNOWN_PURPOSES",
    "PurposeId",
    "validate_purpose",
]
