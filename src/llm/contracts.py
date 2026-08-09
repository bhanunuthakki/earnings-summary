"""Purpose-specific schemas for governed structured LLM boundaries.

TypedDict adapters deliberately preserve the existing plain-dict interfaces while
making the provider boundary type-checked.  Deterministic grounding and allowlist
checks remain in each feature after validation.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from pydantic import TypeAdapter


class ArtifactBriefPayload(TypedDict):
    takeaways: list[str]
    bull: NotRequired[str]
    bear: NotRequired[str]
    changes_mind: NotRequired[str]
    second_order: NotRequired[str]
    portfolio_map: NotRequired[str]


class DcfTweakPayload(TypedDict):
    param: str
    new_value: float | None
    confidence: NotRequired[Literal["high", "low"]]


class DecisionExtractionPayload(TypedDict):
    ticker: str | None
    direction: str | None
    size_pct: NotRequired[float | None]
    conviction: NotRequired[str | None]
    falsifier: NotRequired[str | None]


class WonderingPayload(TypedDict):
    is_wondering: bool
    claim: str
    ticker: NotRequired[str | None]
    suggested_artifacts: NotRequired[list[Literal["memo", "dcf", "thesis", "view"]]]


class IntentPayload(TypedDict):
    intent: Literal["observation", "wondering", "brief_artifact", "stress_artifact"]
    claim: NotRequired[str]
    ticker: NotRequired[str | None]


class ResearchTriagePayload(TypedDict):
    route: Literal["answer_now", "belief_candidate", "research_task"]
    why: NotRequired[str]


class ResearchAssessPayload(TypedDict):
    refuted: bool
    confidence: Literal["high", "medium", "low"]
    rationale: NotRequired[str]


class ResearchNarrativePayload(TypedDict):
    title: str
    body_md: str


class ResearchCodeSpecPayload(TypedDict):
    title: str
    description: str
    change_plan: list[str]
    files_touched: NotRequired[list[str]]


class ThesisEntryPayload(TypedDict):
    entry_kind: str
    body: str


class QualityScorePayload(TypedDict):
    id: int
    quality_score: float


class BehaviorCitationPayload(TypedDict):
    decision_id: int
    outcome: Literal["correct", "wrong", "mixed", "unfalsifiable"]


class BehaviorRulePayload(TypedDict):
    key: str
    rule_text: str
    citations: list[BehaviorCitationPayload]
    confidence_note: NotRequired[str]


class ExitPostmortemPayload(TypedDict):
    exit_reason: str
    lessons: str
    outcome_vs_thesis: Literal["played_out", "broke", "mixed", "unrelated"]


class SemanticTensionPayload(TypedDict):
    tension_with: str | None
    why: NotRequired[str]


class SessionCandidatePayload(TypedDict):
    type: Literal[
        "musing", "resolved_question", "contradiction", "tenet_revision", "stance_revision"
    ]
    text: str
    scope_key: NotRequired[str | None]
    ticker: NotRequired[str | None]
    resolves: NotRequired[str | None]
    conflicts_with: NotRequired[str | None]
    citations: list[str]
    why: NotRequired[str]


class TenetAccountabilityPayload(TypedDict):
    upheld: list[str | int]
    violated: list[str | int]
    est_cost_usd: float | None
    one_liner: str


class TenetPayload(TypedDict):
    tenet: str
    scope_key: str
    citations: list[int | str]


class ThemeSynthesisPayload(TypedDict):
    stance: str
    citations: list[int | str]


class TopicVerdictPayload(TypedDict):
    relevant: bool
    rationale: str


class ToneVerdictPayload(TypedDict):
    score: float
    rationale: str


class TranscriptTonePayload(TypedDict):
    tone: dict[str, ToneVerdictPayload]


class TriageSuggestionPayload(TypedDict):
    intent: str
    confidence: Literal["high", "low"]
    reason: NotRequired[str]


class AnnualLetterPayload(TypedDict):
    letter_md: str


ARTIFACT_BRIEF_SCHEMA = TypeAdapter(ArtifactBriefPayload)
DCF_TWEAK_SCHEMA = TypeAdapter(DcfTweakPayload)
DECISION_EXTRACTION_SCHEMA = TypeAdapter(DecisionExtractionPayload)
WONDERING_SCHEMA = TypeAdapter(WonderingPayload)
INTENT_SCHEMA = TypeAdapter(IntentPayload)
RESEARCH_TRIAGE_SCHEMA = TypeAdapter(ResearchTriagePayload)
RESEARCH_ASSESS_SCHEMA = TypeAdapter(ResearchAssessPayload)
RESEARCH_NARRATIVE_SCHEMA = TypeAdapter(ResearchNarrativePayload)
RESEARCH_CODE_SPEC_SCHEMA = TypeAdapter(ResearchCodeSpecPayload)
THESIS_ENTRY_SCHEMA = TypeAdapter(ThesisEntryPayload)
QUALITY_SCORE_BATCH_SCHEMA = TypeAdapter(list[QualityScorePayload])
BEHAVIOR_RULE_BATCH_SCHEMA = TypeAdapter(list[BehaviorRulePayload])
EXIT_POSTMORTEM_SCHEMA = TypeAdapter(ExitPostmortemPayload)
SEMANTIC_TENSION_SCHEMA = TypeAdapter(SemanticTensionPayload)
SESSION_CANDIDATE_BATCH_SCHEMA = TypeAdapter(list[SessionCandidatePayload])
TENET_ACCOUNTABILITY_SCHEMA = TypeAdapter(TenetAccountabilityPayload)
TENET_BATCH_SCHEMA = TypeAdapter(list[TenetPayload])
THEME_SYNTHESIS_SCHEMA = TypeAdapter(ThemeSynthesisPayload)
TRANSCRIPT_TONE_SCHEMA = TypeAdapter(TranscriptTonePayload)
TRANSCRIPT_TOPIC_SCHEMA = TypeAdapter(dict[str, TopicVerdictPayload])
TRIAGE_SUGGESTION_SCHEMA = TypeAdapter(TriageSuggestionPayload)
ANNUAL_LETTER_SCHEMA = TypeAdapter(AnnualLetterPayload)
RISK_FACTOR_CATEGORIES_SCHEMA = TypeAdapter(dict[str, str])
