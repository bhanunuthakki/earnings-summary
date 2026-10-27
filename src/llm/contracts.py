"""Purpose-specific schemas for governed structured LLM boundaries.

TypedDict adapters deliberately preserve the existing plain-dict interfaces while
making the provider boundary type-checked.  Deterministic grounding and allowlist
checks remain in each feature after validation.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, NotRequired, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


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


class _ClosedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecentDevelopmentEventPayload(_ClosedPayload):
    category: Literal["material_news", "sector_context", "watch_this_week"]
    tag: Literal["CAPITAL", "LEGAL", "INFRA", "PRODUCT", "EARNINGS", "NARRATIVE", "NEWS"]
    headline: str = Field(min_length=1, max_length=500)
    published_at: str = Field(pattern=r"^20\d{2}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?$")
    publisher: str = Field(min_length=1, max_length=120)
    url: str = Field(pattern=r"^https?://[^\s]+$")
    factual_summary: str = Field(min_length=1, max_length=2_000)
    thesis_implication: str = Field(max_length=2_000)
    tone: Literal["pos", "opt", "neu", "neg"]

    @field_validator("published_at")
    @classmethod
    def _valid_publication_date(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("published_at must be a real calendar date and optional time") from exc
        return value


class RecentDevelopmentsPayload(_ClosedPayload):
    """Typed, cited events plus explicit search coverage for the report."""

    search_status: Literal["events_found", "no_qualifying_events"]
    window_start: str = Field(pattern=r"^20\d{2}-\d{2}-\d{2}$")
    window_end: str = Field(pattern=r"^20\d{2}-\d{2}-\d{2}$")
    queries_run: list[str] = Field(min_length=1, max_length=2)
    sources_opened: list[str] = Field(min_length=1, max_length=7)
    events: list[RecentDevelopmentEventPayload] = Field(max_length=11)

    @model_validator(mode="after")
    def _status_matches_events(self) -> Self:
        if self.search_status == "events_found" and not self.events:
            raise ValueError("events_found requires at least one event")
        if self.search_status == "no_qualifying_events" and self.events:
            raise ValueError("no_qualifying_events must not carry events")
        if self.window_start > self.window_end:
            raise ValueError("search window start must not follow its end")
        opened_urls = set(self.sources_opened)
        for event in self.events:
            if not self.window_start <= event.published_at[:10] <= self.window_end:
                raise ValueError("event publication date must fall within the searched window")
            if event.url not in opened_urls:
                raise ValueError("every event URL must appear in sources_opened")
        return self

    @field_validator("window_start", "window_end")
    @classmethod
    def _valid_window_date(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("search window dates must be real calendar dates") from exc
        return value


RECENT_DEVELOPMENTS_SCHEMA = TypeAdapter(RecentDevelopmentsPayload)


class TranscriptMetadataPayload(_ClosedPayload):
    """A legitimate metadata decision; provider failure is represented by an exception."""

    status: Literal["identified", "unknown"]
    ticker: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    quarter: Literal["Q1", "Q2", "Q3", "Q4"] | None = None

    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)

    @field_validator("ticker", mode="before")
    @classmethod
    def _uppercase_ticker(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _status_matches_fields(self) -> Self:
        values = (self.ticker, self.quarter, self.fiscal_year)
        if self.status == "identified" and any(value is None for value in values):
            raise ValueError("identified metadata requires ticker, quarter, and fiscal_year")
        if self.status == "unknown" and any(value is not None for value in values):
            raise ValueError("unknown metadata must not carry ticker, quarter, or fiscal_year")
        return self

    def legacy_value(self) -> str:
        if self.status == "unknown":
            return "UNKNOWN"
        assert self.ticker is not None and self.quarter is not None and self.fiscal_year is not None
        return f"{self.ticker}_{self.quarter}_{self.fiscal_year}"


class PressureTestPayload(_ClosedPayload):
    strongest_counter: str = Field(min_length=1, max_length=2_000)
    contradicting_evidence: list[str] = Field(max_length=20)
    mgmt_credibility_check: str = Field(min_length=1, max_length=4_000)
    thesis_assumptions: list[str] = Field(min_length=1, max_length=20)
    conviction_rating: Literal["high", "medium", "low"]
    conviction_reasoning: str = Field(min_length=1, max_length=2_000)
    evidence_gap: str | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def _empty_evidence_names_the_gap(self) -> Self:
        if not self.contradicting_evidence and not self.evidence_gap:
            raise ValueError("empty contradicting_evidence requires an explicit evidence_gap")
        return self


RiskFactorCategory = Literal[
    "regulatory",
    "operational",
    "technology",
    "financial",
    "macro",
    "litigation",
    "competition",
    "product",
    "other",
]


class RiskFactorDiffPayload(_ClosedPayload):
    outcome: Literal["material_change", "no_material_change"]
    summary: str | None = Field(default=None, max_length=1_500)

    @model_validator(mode="after")
    def _outcome_matches_summary(self) -> Self:
        if self.outcome == "material_change" and not self.summary:
            raise ValueError("material_change requires a non-empty summary")
        if self.outcome == "no_material_change" and self.summary is not None:
            raise ValueError("no_material_change must have a null summary")
        return self


class DcfSegmentGrowth(_ClosedPayload):
    near_term_growth: float = Field(ge=-1.0, le=2.0)
    terminal_growth: float = Field(ge=-0.10, le=0.10)

    @model_validator(mode="after")
    def _growth_fades(self) -> Self:
        if self.near_term_growth <= self.terminal_growth:
            raise ValueError("near_term_growth must exceed terminal_growth")
        return self


class DcfAssumptionsPayload(_ClosedPayload):
    dcf_applicable: bool
    business_model: Literal[
        "operating", "bank", "insurer", "asset_manager", "royalty", "reit", "other"
    ]
    valuation_model: Literal["fcff_dcf", "bank_excess_return", "holdco_sotp", "new"]
    valuation_model_suggestion: str = Field(max_length=1_000)
    segments: dict[str, DcfSegmentGrowth] = Field(min_length=1, max_length=50)
    near_term_op_margin: float = Field(ge=-1.0, le=1.0)
    terminal_op_margin: float = Field(ge=-1.0, le=1.0)
    tax_rate: float = Field(ge=0.0, le=0.60)
    capex_pct_revenue_2026: float = Field(ge=0.0, le=1.0)
    terminal_capex_da: float = Field(ge=0.0, le=5.0)
    terminal_method: Literal["Exit multiple", "Perpetuity"]
    exit_basis: Literal["EV/EBITDA", "EV/Sales", "EV/EBIT", "EV/FCF"]
    exit_multiple: float = Field(ge=0.5, le=75.0)
    terminal_growth_g: float = Field(ge=-0.05, le=0.10)
    narrative: str = Field(min_length=1, max_length=4_000)
    reasoning: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def _valuation_archetype_is_consistent(self) -> Self:
        if self.dcf_applicable != (self.valuation_model == "fcff_dcf"):
            raise ValueError("dcf_applicable must be true exactly for valuation_model=fcff_dcf")
        if self.valuation_model == "new" and not self.valuation_model_suggestion:
            raise ValueError("valuation_model=new requires valuation_model_suggestion")
        if self.valuation_model != "new" and self.valuation_model_suggestion:
            raise ValueError("valuation_model_suggestion is only valid for valuation_model=new")
        return self


RESEARCH_ASSESS_SCHEMA = TypeAdapter(ResearchAssessPayload)
RESEARCH_NARRATIVE_SCHEMA = TypeAdapter(ResearchNarrativePayload)
RESEARCH_CODE_SPEC_SCHEMA = TypeAdapter(ResearchCodeSpecPayload)
THESIS_ENTRY_SCHEMA = TypeAdapter(ThesisEntryPayload)
QUALITY_SCORE_BATCH_SCHEMA = TypeAdapter(list[QualityScorePayload])
EXIT_POSTMORTEM_SCHEMA = TypeAdapter(ExitPostmortemPayload)
SEMANTIC_TENSION_SCHEMA = TypeAdapter(SemanticTensionPayload)
SESSION_CANDIDATE_BATCH_SCHEMA = TypeAdapter(list[SessionCandidatePayload])
TENET_ACCOUNTABILITY_SCHEMA = TypeAdapter(TenetAccountabilityPayload)
TENET_BATCH_SCHEMA = TypeAdapter(list[TenetPayload])
THEME_SYNTHESIS_SCHEMA = TypeAdapter(ThemeSynthesisPayload)
TRANSCRIPT_TONE_SCHEMA = TypeAdapter(TranscriptTonePayload)
TRANSCRIPT_TOPIC_SCHEMA = TypeAdapter(dict[str, TopicVerdictPayload])
TRANSCRIPT_METADATA_SCHEMA = TypeAdapter(TranscriptMetadataPayload)
PRESSURE_TEST_SCHEMA = TypeAdapter(PressureTestPayload)
RISK_FACTOR_CATEGORIES_SCHEMA = TypeAdapter(dict[str, RiskFactorCategory])
RISK_FACTOR_DIFF_SCHEMA = TypeAdapter(RiskFactorDiffPayload)
DCF_ASSUMPTIONS_SCHEMA = TypeAdapter(DcfAssumptionsPayload)
