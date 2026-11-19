"""Purpose-specific schemas for governed structured LLM boundaries.

TypedDict adapters deliberately preserve the existing plain-dict interfaces while
making the provider boundary type-checked.  Deterministic grounding and allowlist
checks remain in each feature after validation.
"""

from __future__ import annotations

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
        # Growth fades toward a mature rate. A declining segment (negative near-term
        # rate) fades the other way: its decline eases toward a higher terminal rate.
        if self.near_term_growth >= 0 and self.near_term_growth <= self.terminal_growth:
            raise ValueError("near_term_growth must exceed terminal_growth")
        if self.near_term_growth < 0 and self.terminal_growth < self.near_term_growth:
            raise ValueError("a declining segment cannot decline faster at maturity")
        return self


DcfAnchorClass = Literal[
    "reported_fact", "management_statement", "calculation", "external_estimate", "judgment"
]


class DcfAssumptionRecord(_ClosedPayload):
    """Why one assumption has its value (forecast-assumption method record)."""

    field: str = Field(min_length=1, max_length=200)
    anchor_class: DcfAnchorClass
    anchor: str = Field(min_length=1, max_length=800)
    counter_argument: str = Field(min_length=1, max_length=600)
    confidence: Literal["high", "medium", "low"]
    observable: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def _judgment_is_not_high_confidence(self) -> Self:
        if self.anchor_class == "judgment" and self.confidence == "high":
            raise ValueError("a judgment anchor cannot carry high confidence")
        return self


_DCF_OPTIONAL_RECORD_FIELDS = frozenset(
    {
        "tax_rate",
        "capex_pct_revenue_2026",
        "terminal_capex_da",
        "exit_multiple",
        "terminal_growth_g",
    }
)


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
    # Prompt v3. Defaults keep stored v2 payloads valid; the refresh guardrail
    # requires them for a new FCFF result.
    consensus_comparison: str = Field(default="", max_length=1_500)
    assumption_records: list[DcfAssumptionRecord] = Field(
        default_factory=list["DcfAssumptionRecord"], max_length=60
    )

    def required_record_fields(self) -> frozenset[str]:
        """Assumptions that need a record: the ones that decide an FCFF value."""
        if self.valuation_model != "fcff_dcf":
            return frozenset()
        terminal = (
            "exit_multiple" if self.terminal_method == "Exit multiple" else "terminal_growth_g"
        )
        return frozenset(
            {f"segments.{name}.near_term_growth" for name in self.segments}
            | {"near_term_op_margin", "terminal_op_margin", terminal}
        )

    def assumption_record_issues(self) -> list[str]:
        """Method violations in the records. Empty means every required field is covered."""
        required = self.required_record_fields()
        allowed = (
            required
            | _DCF_OPTIONAL_RECORD_FIELDS
            | {f"segments.{name}.terminal_growth" for name in self.segments}
        )
        fields = [record.field for record in self.assumption_records]
        issues = [
            f"duplicate record for {f}" for f in sorted({f for f in fields if fields.count(f) > 1})
        ]
        issues += [f"record for unknown field {f}" for f in sorted(set(fields) - allowed)]
        issues += [f"missing record for {f}" for f in sorted(required - set(fields))]
        if required and not self.consensus_comparison:
            issues.append("consensus_comparison is empty")
        return issues

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
TRANSCRIPT_TONE_SCHEMA = TypeAdapter(TranscriptTonePayload)
TRANSCRIPT_TOPIC_SCHEMA = TypeAdapter(dict[str, TopicVerdictPayload])
TRANSCRIPT_METADATA_SCHEMA = TypeAdapter(TranscriptMetadataPayload)
PRESSURE_TEST_SCHEMA = TypeAdapter(PressureTestPayload)
RISK_FACTOR_CATEGORIES_SCHEMA = TypeAdapter(dict[str, RiskFactorCategory])
RISK_FACTOR_DIFF_SCHEMA = TypeAdapter(RiskFactorDiffPayload)
DCF_ASSUMPTIONS_SCHEMA = TypeAdapter(DcfAssumptionsPayload)
