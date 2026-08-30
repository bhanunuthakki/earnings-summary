"""Typed investment-profile vocabulary, deterministic labels, and owner review.

The qualitative company-shape suggestion is authored inside the existing
Investment Decision Card.  Valuation-aware labels are derived here from
admitted, already-materialized numbers.  Owner review is the only persisted
state in this module; the current system suggestion remains a recomputable
projection over its evidence authorities.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from identity import DEFAULT_USER_ID

PROFILE_RULE_VERSION = "company_profile_rules.v1"
ETF_PROFILE_RULE_VERSION = "etf_profile_rules.v1"


class CompanyProfileLabel(StrEnum):
    LONG_TERM_COMPOUNDER = "long_term_compounder"
    GARP = "garp"
    ELITE_GROWTH_EXPENSIVE = "elite_growth_expensive"
    TURNAROUND = "turnaround"
    NARRATIVE_RERATING = "narrative_rerating"
    GROWTH_INFLECTION = "growth_inflection"
    CASH_YIELD_VALUE = "cash_yield_value"
    OPTIONALITY = "optionality"

    @property
    def display_label(self) -> str:
        return _COMPANY_LABEL_DISPLAY[self]


_COMPANY_LABEL_DISPLAY: dict[CompanyProfileLabel, str] = {
    CompanyProfileLabel.LONG_TERM_COMPOUNDER: "Long-term compounder",
    CompanyProfileLabel.GARP: "GARP",
    CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE: "Elite growth / expensive",
    CompanyProfileLabel.TURNAROUND: "Turnaround",
    CompanyProfileLabel.NARRATIVE_RERATING: "Narrative re-rating",
    CompanyProfileLabel.GROWTH_INFLECTION: "Growth inflection",
    CompanyProfileLabel.CASH_YIELD_VALUE: "Cash-yield value",
    CompanyProfileLabel.OPTIONALITY: "Optionality",
}

QUALITATIVE_COMPANY_LABELS = frozenset(
    {
        CompanyProfileLabel.LONG_TERM_COMPOUNDER,
        CompanyProfileLabel.TURNAROUND,
        CompanyProfileLabel.NARRATIVE_RERATING,
        CompanyProfileLabel.GROWTH_INFLECTION,
        CompanyProfileLabel.CASH_YIELD_VALUE,
        CompanyProfileLabel.OPTIONALITY,
    }
)


class EtfProfileLabel(StrEnum):
    CORE_BETA = "core_beta"
    FACTOR_SLEEVE = "factor_sleeve"
    THEMATIC_EXPOSURE = "thematic_exposure"
    DIVERSIFIER = "diversifier"
    DEFENSIVE_HEDGE = "defensive_hedge"
    INCOME = "income"
    TACTICAL_CYCLICAL = "tactical_cyclical"

    @property
    def display_label(self) -> str:
        return _ETF_LABEL_DISPLAY[self]


_ETF_LABEL_DISPLAY: dict[EtfProfileLabel, str] = {
    EtfProfileLabel.CORE_BETA: "Core beta",
    EtfProfileLabel.FACTOR_SLEEVE: "Factor sleeve",
    EtfProfileLabel.THEMATIC_EXPOSURE: "Thematic exposure",
    EtfProfileLabel.DIVERSIFIER: "Diversifier",
    EtfProfileLabel.DEFENSIVE_HEDGE: "Defensive / hedge",
    EtfProfileLabel.INCOME: "Income",
    EtfProfileLabel.TACTICAL_CYCLICAL: "Tactical / cyclical",
}


class MoatLevel(StrEnum):
    MULTI_BUSINESS = "multi_business"
    CORE_BUSINESS = "core_business"
    NARROW_CONDITIONAL = "narrow_conditional"
    NONE_DEMONSTRATED = "none_demonstrated"

    @property
    def display_label(self) -> str:
        return _MOAT_DISPLAY[self]


_MOAT_DISPLAY: dict[MoatLevel, str] = {
    MoatLevel.MULTI_BUSINESS: "Multi-business moat",
    MoatLevel.CORE_BUSINESS: "Core-business moat",
    MoatLevel.NARROW_CONDITIONAL: "Narrow / conditional moat",
    MoatLevel.NONE_DEMONSTRATED: "No demonstrated moat",
}


class MoatEvidenceCoverage(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class MoatAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: MoatLevel | None
    evidence_coverage: MoatEvidenceCoverage
    rationale: str = Field(min_length=1, max_length=1200)
    supporting_evidence: list[str] = Field(default_factory=list[str], max_length=6)
    counter_evidence: list[str] = Field(default_factory=list[str], max_length=6)

    @model_validator(mode="after")
    def evidence_and_conclusion_are_distinct(self) -> Self:
        if self.evidence_coverage is MoatEvidenceCoverage.INSUFFICIENT and self.level is not None:
            raise ValueError("insufficient evidence cannot carry a moat conclusion")
        if self.evidence_coverage is not MoatEvidenceCoverage.INSUFFICIENT and self.level is None:
            raise ValueError("a moat conclusion is required when evidence is sufficient or partial")
        return self


class InvestmentProfileSuggestion(BaseModel):
    """The model-authored, non-valuation portion of a company profile."""

    model_config = ConfigDict(frozen=True)

    labels: list[CompanyProfileLabel] = Field(
        default_factory=list[CompanyProfileLabel], max_length=4
    )
    summary: str = Field(min_length=1, max_length=600)
    moat: MoatAssessment

    @model_validator(mode="after")
    def labels_are_unique_and_qualitative(self) -> Self:
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("investment profile labels must be unique")
        invalid = set(self.labels) - QUALITATIVE_COMPANY_LABELS
        if invalid:
            raise ValueError(
                "valuation-aware labels are derived deterministically: "
                + ", ".join(sorted(label.value for label in invalid))
            )
        return self


class ValuationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    revenue_growth_yoy_pct: float | None = None
    fcf_margin_pct: float | None = None
    dcf_upside_pct: float | None = None


class EtfStyleEvidence(BaseModel):
    """One admitted ETF style-loading observation from the materialized workup."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    key: Literal["value", "size", "momentum"]
    beta: float
    r_squared: float = Field(ge=0.0, le=1.0)


class EtfProfileInputs(BaseModel):
    """Typed current evidence consumed by the deterministic ETF classifier."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)

    profile_available: bool = False
    asset_class: str | None = None
    benchmark_index: str | None = None
    sector_label: str | None = None
    expense_ratio: float | None = Field(default=None, ge=0.0)
    distribution_yield: float | None = Field(default=None, ge=0.0)
    style_evidence_available: bool = False
    style_loadings: list[EtfStyleEvidence] = Field(default_factory=list[EtfStyleEvidence])
    book_evidence_available: bool = False
    diversification_multiplier: float | None = Field(default=None, ge=0.0)
    overlap_multiplier: float | None = Field(default=None, ge=0.0)
    sharpe_delta_bps: float | None = None
    whatif_evidence_available: bool = False
    vol_before_ann: float | None = Field(default=None, ge=0.0)
    vol_after_ann: float | None = Field(default=None, ge=0.0)


class EtfProfileLabelEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: EtfProfileLabel
    source_kind: Literal["etf_rule"] = "etf_rule"
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=240)
    evidence: dict[str, object]


LabelSourceKind = Literal["qualitative_synthesis", "dcf_rule"]


class ProfileLabelEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: CompanyProfileLabel
    source_kind: LabelSourceKind
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: dict[str, object]


class LabelReviewAction(StrEnum):
    RATIFY = "ratify"
    REJECT = "reject"
    RETIRE = "retire"


class ProfileLabelState(StrEnum):
    SYSTEM_SUGGESTED = "system_suggested"
    OWNER_RATIFIED = "owner_ratified"
    REVIEW_SUGGESTED = "review_suggested"


class ProfileLabelPresentation(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: CompanyProfileLabel
    display_label: str
    state: ProfileLabelState
    suggested: bool
    suggestion_fingerprint: str
    source_kind: str


class ProfileProjectionState(StrEnum):
    SYSTEM_SUGGESTED = "system_suggested"
    OWNER_RATIFIED = "owner_ratified"
    REVIEW_SUGGESTED = "review_suggested"
    UNAVAILABLE = "unavailable"


class EtfEvidenceCoverage(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class EtfProfileLabelPresentation(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: EtfProfileLabel
    display_label: str
    state: ProfileLabelState
    suggested: bool
    suggestion_fingerprint: str
    source_kind: str
    evidence_summary: str


class EtfProfileProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    labels: list[EtfProfileLabelPresentation]
    summary: str
    state: ProfileProjectionState
    evidence_coverage: EtfEvidenceCoverage
    evidence_gaps: list[str] = Field(default_factory=list[str])
    source_artifact_id: None = None
    refresh_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompanyProfileProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    labels: list[ProfileLabelPresentation]
    summary: str
    state: ProfileProjectionState
    moat: MoatAssessment
    source_artifact_id: int | None = None
    refresh_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _sha(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _coarse_band(value: float | None, cuts: tuple[float, ...]) -> str:
    if value is None:
        return "missing"
    for index, cut in enumerate(cuts):
        if value < cut:
            return f"b{index}"
    return f"b{len(cuts)}"


def _evidence(
    label: CompanyProfileLabel,
    source_kind: LabelSourceKind,
    payload: dict[str, object],
) -> ProfileLabelEvidence:
    canonical = {"rule_version": PROFILE_RULE_VERSION, "label": label.value, **payload}
    return ProfileLabelEvidence(
        label=label,
        source_kind=source_kind,
        fingerprint=_sha(canonical),
        evidence=canonical,
    )


def derive_company_label_evidence(
    *,
    qualitative_labels: list[CompanyProfileLabel],
    qualitative_source_sha: str,
    valuation: ValuationEvidence,
    moat_level: MoatLevel | None,
) -> dict[CompanyProfileLabel, ProfileLabelEvidence]:
    """Return the current label suggestion from typed qualitative and DCF evidence.

    The valuation rules are intentionally conservative and fail closed.  They
    require all named inputs; a missing or unreviewed DCF therefore cannot add a
    valuation label.  Coarse evidence bands keep a rejected label from being
    re-proposed for immaterial numeric movement.
    """

    result: dict[CompanyProfileLabel, ProfileLabelEvidence] = {}
    for label in qualitative_labels:
        if label not in QUALITATIVE_COMPANY_LABELS:
            continue
        result[label] = _evidence(
            label,
            "qualitative_synthesis",
            {"qualitative_source_sha": qualitative_source_sha},
        )

    growth = valuation.revenue_growth_yoy_pct
    margin = valuation.fcf_margin_pct
    upside = valuation.dcf_upside_pct
    if growth is not None and margin is not None and upside is not None:
        valuation_bands: dict[str, object] = {
            "growth_band": _coarse_band(growth, (15.0, 25.0, 50.0)),
            "fcf_margin_band": _coarse_band(margin, (5.0, 15.0, 30.0)),
            "dcf_upside_band": _coarse_band(upside, (-10.0, 0.0, 10.0, 25.0)),
        }
        if growth >= 15.0 and margin >= 5.0 and upside >= 10.0:
            result[CompanyProfileLabel.GARP] = _evidence(
                CompanyProfileLabel.GARP,
                "dcf_rule",
                valuation_bands,
            )
        quality_supported = (
            CompanyProfileLabel.LONG_TERM_COMPOUNDER in qualitative_labels
            or moat_level in {MoatLevel.MULTI_BUSINESS, MoatLevel.CORE_BUSINESS}
        )
        if quality_supported and growth >= 25.0 and margin >= 5.0 and upside <= -10.0:
            result[CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE] = _evidence(
                CompanyProfileLabel.ELITE_GROWTH_EXPENSIVE,
                "dcf_rule",
                {**valuation_bands, "quality_supported": True},
            )
    return result


_BROAD_MARKET_BENCHMARKS = frozenset(
    {
        "crsp us total market index",
        "ftse global all cap index",
        "msci acwi index",
        "msci usa index",
        "russell 1000 index",
        "s&p 500 index",
        "s&p total market index",
    }
)
_CYCLICAL_SECTORS = frozenset(
    {
        "energy",
        "financial services",
        "financials",
        "industrials",
        "materials",
        "real estate",
    }
)


def _normalized_identity(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def _etf_evidence(
    label: EtfProfileLabel,
    *,
    summary: str,
    payload: dict[str, object],
) -> EtfProfileLabelEvidence:
    canonical = {"rule_version": ETF_PROFILE_RULE_VERSION, "label": label.value, **payload}
    return EtfProfileLabelEvidence(
        label=label,
        fingerprint=_sha(canonical),
        summary=summary,
        evidence=canonical,
    )


def derive_etf_label_evidence(
    inputs: EtfProfileInputs,
) -> dict[EtfProfileLabel, EtfProfileLabelEvidence]:
    """Classify ETF shape from typed current fund and book evidence only.

    Each rule fails closed when one of its named authorities is unavailable.
    Fingerprints intentionally record threshold outcomes instead of exact noisy
    values so immaterial market movement does not churn owner review.
    """

    result: dict[EtfProfileLabel, EtfProfileLabelEvidence] = {}
    qualifying_loadings = sorted(
        (
            loading
            for loading in inputs.style_loadings
            if loading.r_squared >= 0.10 and abs(loading.beta) >= 0.30
        ),
        key=lambda loading: loading.key,
    )
    benchmark = _normalized_identity(inputs.benchmark_index)
    sector = _normalized_identity(inputs.sector_label)
    asset_class = _normalized_identity(inputs.asset_class)

    if (
        inputs.profile_available
        and inputs.style_evidence_available
        and asset_class == "equity"
        and benchmark in _BROAD_MARKET_BENCHMARKS
        and inputs.expense_ratio is not None
        and inputs.expense_ratio <= 0.0025
        and not qualifying_loadings
    ):
        result[EtfProfileLabel.CORE_BETA] = _etf_evidence(
            EtfProfileLabel.CORE_BETA,
            summary="Low-cost equity exposure to a recognized broad-market index",
            payload={
                "benchmark": benchmark,
                "expense_band": _coarse_band(inputs.expense_ratio, (0.001, 0.0025)),
                "qualifying_style_loadings": [],
            },
        )

    if inputs.style_evidence_available and qualifying_loadings:
        loading_payload = [
            {
                "key": loading.key,
                "beta_band": _coarse_band(abs(loading.beta), (0.30, 0.60, 1.0)),
                "direction": "positive" if loading.beta > 0 else "negative",
                "r2_band": _coarse_band(loading.r_squared, (0.10, 0.30, 0.60)),
            }
            for loading in qualifying_loadings
        ]
        result[EtfProfileLabel.FACTOR_SLEEVE] = _etf_evidence(
            EtfProfileLabel.FACTOR_SLEEVE,
            summary="Material measured style loading distinguishes the fund from broad beta",
            payload={"loadings": loading_payload},
        )

    if inputs.profile_available and sector and sector not in {"broad market", "diversified"}:
        result[EtfProfileLabel.THEMATIC_EXPOSURE] = _etf_evidence(
            EtfProfileLabel.THEMATIC_EXPOSURE,
            summary=f"Published fund profile targets {inputs.sector_label}",
            payload={"sector_label": sector},
        )

    if (
        inputs.book_evidence_available
        and inputs.diversification_multiplier is not None
        and inputs.diversification_multiplier >= 1.10
        and inputs.overlap_multiplier is not None
        and inputs.overlap_multiplier >= 1.0
    ):
        result[EtfProfileLabel.DIVERSIFIER] = _etf_evidence(
            EtfProfileLabel.DIVERSIFIER,
            summary="Low book correlation and limited look-through duplication",
            payload={
                "diversification_band": _coarse_band(
                    inputs.diversification_multiplier, (1.0, 1.10, 1.20)
                ),
                "overlap_band": _coarse_band(inputs.overlap_multiplier, (0.92, 1.0, 1.08)),
            },
        )

    if (
        inputs.book_evidence_available
        and inputs.whatif_evidence_available
        and inputs.diversification_multiplier is not None
        and inputs.diversification_multiplier >= 1.20
        and inputs.sharpe_delta_bps is not None
        and inputs.sharpe_delta_bps > 0
        and inputs.vol_before_ann is not None
        and inputs.vol_after_ann is not None
        and inputs.vol_after_ann < inputs.vol_before_ann
    ):
        result[EtfProfileLabel.DEFENSIVE_HEDGE] = _etf_evidence(
            EtfProfileLabel.DEFENSIVE_HEDGE,
            summary="Strong diversification with modeled volatility reduction and Sharpe improvement",
            payload={
                "diversification_band": _coarse_band(
                    inputs.diversification_multiplier, (1.0, 1.10, 1.20)
                ),
                "sharpe_delta_direction": "positive",
                "volatility_direction": "lower",
            },
        )

    if (
        inputs.profile_available
        and inputs.distribution_yield is not None
        and inputs.distribution_yield >= 0.03
    ):
        result[EtfProfileLabel.INCOME] = _etf_evidence(
            EtfProfileLabel.INCOME,
            summary="Published distribution yield is at least 3%",
            payload={
                "distribution_yield_band": _coarse_band(
                    inputs.distribution_yield, (0.03, 0.05, 0.08)
                )
            },
        )

    if inputs.profile_available and sector in _CYCLICAL_SECTORS:
        result[EtfProfileLabel.TACTICAL_CYCLICAL] = _etf_evidence(
            EtfProfileLabel.TACTICAL_CYCLICAL,
            summary=f"Published sector exposure is cyclical: {inputs.sector_label}",
            payload={"sector_label": sector},
        )
    return result


def _table_exists(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='investment_profile_label_reviews'"
        ).fetchone()
        is not None
    )


def record_label_review(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    label: CompanyProfileLabel | EtfProfileLabel,
    action: LabelReviewAction,
    suggestion_fingerprint: str,
    evidence: dict[str, object],
    reviewed_by: str = DEFAULT_USER_ID,
) -> int:
    """Append one idempotent owner review without mutating the suggestion."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if len(suggestion_fingerprint) != 64:
        raise ValueError("suggestion fingerprint must be SHA-256")
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    idempotency_key = _sha(
        {
            "ticker": symbol,
            "label": label.value,
            "action": action.value,
            "suggestion_fingerprint": suggestion_fingerprint,
        }
    )
    conn.execute(
        "INSERT OR IGNORE INTO investment_profile_label_reviews "
        "(ticker,label,action,suggestion_fingerprint,evidence_json,reviewed_by,reviewed_at,"
        "idempotency_key) VALUES (?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        (
            symbol,
            label.value,
            action.value,
            suggestion_fingerprint,
            evidence_json,
            reviewed_by,
            idempotency_key,
        ),
    )
    row = conn.execute(
        "SELECT id FROM investment_profile_label_reviews WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("investment profile review was not persisted")
    return int(row[0])


def _latest_reviews(
    conn: sqlite3.Connection, ticker: str
) -> dict[CompanyProfileLabel, sqlite3.Row]:
    if not _table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT r.* FROM investment_profile_label_reviews r "
        "JOIN (SELECT label, MAX(id) AS latest_id FROM investment_profile_label_reviews "
        "WHERE UPPER(ticker)=? GROUP BY label) latest ON latest.latest_id=r.id",
        (ticker.upper(),),
    ).fetchall()
    result: dict[CompanyProfileLabel, sqlite3.Row] = {}
    for row in rows:
        try:
            result[CompanyProfileLabel(str(row["label"]))] = row
        except ValueError:
            continue
    return result


def _latest_etf_reviews(
    conn: sqlite3.Connection, ticker: str
) -> dict[EtfProfileLabel, sqlite3.Row]:
    if not _table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT r.* FROM investment_profile_label_reviews r "
        "JOIN (SELECT label, MAX(id) AS latest_id FROM investment_profile_label_reviews "
        "WHERE UPPER(ticker)=? GROUP BY label) latest ON latest.latest_id=r.id",
        (ticker.upper(),),
    ).fetchall()
    result: dict[EtfProfileLabel, sqlite3.Row] = {}
    for row in rows:
        try:
            result[EtfProfileLabel(str(row["label"]))] = row
        except ValueError:
            continue
    return result


def resolve_label_presentations(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    suggestions: dict[CompanyProfileLabel, ProfileLabelEvidence],
) -> list[ProfileLabelPresentation]:
    """Merge recomputable suggestions with append-only owner decisions."""

    reviews = _latest_reviews(conn, ticker)
    presentations: list[ProfileLabelPresentation] = []
    for label in CompanyProfileLabel:
        suggestion = suggestions.get(label)
        review = reviews.get(label)
        if suggestion is not None:
            state = ProfileLabelState.SYSTEM_SUGGESTED
            if review is not None:
                action = LabelReviewAction(str(review["action"]))
                same_fingerprint = str(review["suggestion_fingerprint"]) == suggestion.fingerprint
                if action is LabelReviewAction.RATIFY:
                    state = (
                        ProfileLabelState.OWNER_RATIFIED
                        if same_fingerprint
                        else ProfileLabelState.REVIEW_SUGGESTED
                    )
                elif same_fingerprint:
                    continue
                else:
                    state = ProfileLabelState.REVIEW_SUGGESTED
            presentations.append(
                ProfileLabelPresentation(
                    label=label,
                    display_label=label.display_label,
                    state=state,
                    suggested=True,
                    suggestion_fingerprint=suggestion.fingerprint,
                    source_kind=suggestion.source_kind,
                )
            )
            continue
        if (
            review is not None
            and LabelReviewAction(str(review["action"])) is LabelReviewAction.RATIFY
        ):
            presentations.append(
                ProfileLabelPresentation(
                    label=label,
                    display_label=label.display_label,
                    state=ProfileLabelState.REVIEW_SUGGESTED,
                    suggested=False,
                    suggestion_fingerprint=str(review["suggestion_fingerprint"]),
                    source_kind="owner_review",
                )
            )
    return presentations


def resolve_etf_label_presentations(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    suggestions: dict[EtfProfileLabel, EtfProfileLabelEvidence],
) -> list[EtfProfileLabelPresentation]:
    """Merge deterministic ETF suggestions with the shared owner-review ledger."""

    reviews = _latest_etf_reviews(conn, ticker)
    presentations: list[EtfProfileLabelPresentation] = []
    for label in EtfProfileLabel:
        suggestion = suggestions.get(label)
        review = reviews.get(label)
        if suggestion is not None:
            state = ProfileLabelState.SYSTEM_SUGGESTED
            if review is not None:
                action = LabelReviewAction(str(review["action"]))
                same_fingerprint = str(review["suggestion_fingerprint"]) == suggestion.fingerprint
                if action is LabelReviewAction.RATIFY:
                    state = (
                        ProfileLabelState.OWNER_RATIFIED
                        if same_fingerprint
                        else ProfileLabelState.REVIEW_SUGGESTED
                    )
                elif same_fingerprint:
                    continue
                else:
                    state = ProfileLabelState.REVIEW_SUGGESTED
            presentations.append(
                EtfProfileLabelPresentation(
                    label=label,
                    display_label=label.display_label,
                    state=state,
                    suggested=True,
                    suggestion_fingerprint=suggestion.fingerprint,
                    source_kind=suggestion.source_kind,
                    evidence_summary=suggestion.summary,
                )
            )
            continue
        if (
            review is not None
            and LabelReviewAction(str(review["action"])) is LabelReviewAction.RATIFY
        ):
            presentations.append(
                EtfProfileLabelPresentation(
                    label=label,
                    display_label=label.display_label,
                    state=ProfileLabelState.REVIEW_SUGGESTED,
                    suggested=False,
                    suggestion_fingerprint=str(review["suggestion_fingerprint"]),
                    source_kind="owner_review",
                    evidence_summary="Current evidence no longer supports this previously ratified label.",
                )
            )
    return presentations


def _load_current_suggestion(
    conn: sqlite3.Connection, ticker: str
) -> tuple[int, str, InvestmentProfileSuggestion] | None:
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(llm_artifacts)")}
    except sqlite3.Error:
        return None
    if not {"id", "ticker", "purpose", "content_json", "input_sha256"} <= columns:
        return None
    current = "AND superseded_by_id IS NULL" if "superseded_by_id" in columns else ""
    try:
        row = conn.execute(
            "SELECT id,input_sha256,content_json FROM llm_artifacts "
            "WHERE UPPER(ticker)=? AND purpose='investment_decision_card' "
            f"{current} ORDER BY id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        raw: object = json.loads(str(row[2] or "{}"))
        if not isinstance(raw, dict):
            return None
        profile = cast("dict[str, object]", raw).get("investment_profile")
        if not isinstance(profile, dict):
            return None
        suggestion = InvestmentProfileSuggestion.model_validate(profile)
    except (json.JSONDecodeError, ValueError):
        return None
    return int(row[0]), str(row[1]), suggestion


def project_company_profile(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    valuation: ValuationEvidence,
) -> CompanyProfileProjection:
    """Recompute the current visible profile from the latest evidence.

    This is the DCF refresh loop: the no-cache Evaluation request supplies the
    latest admitted DCF/fundamental values, so a changed valuation immediately
    changes the deterministic label projection without an LLM call or another
    persisted derived row.
    """

    loaded = _load_current_suggestion(conn, ticker)
    if loaded is None:
        source_artifact_id = None
        source_sha = "no-current-investment-profile"
        suggestion = _unavailable_profile_suggestion_for_projection()
    else:
        source_artifact_id, source_sha, suggestion = loaded

    label_evidence = derive_company_label_evidence(
        qualitative_labels=suggestion.labels,
        qualitative_source_sha=source_sha,
        valuation=valuation,
        moat_level=suggestion.moat.level,
    )
    labels = resolve_label_presentations(conn, ticker=ticker, suggestions=label_evidence)
    if any(item.state is ProfileLabelState.REVIEW_SUGGESTED for item in labels):
        state = ProfileProjectionState.REVIEW_SUGGESTED
    elif labels and all(item.state is ProfileLabelState.OWNER_RATIFIED for item in labels):
        state = ProfileProjectionState.OWNER_RATIFIED
    elif labels:
        state = ProfileProjectionState.SYSTEM_SUGGESTED
    else:
        state = ProfileProjectionState.UNAVAILABLE
    fingerprint = _sha(
        {
            "rule_version": PROFILE_RULE_VERSION,
            "ticker": ticker.upper(),
            "source_artifact_id": source_artifact_id,
            "source_sha": source_sha,
            "labels": [
                {
                    "label": item.label.value,
                    "state": item.state.value,
                    "suggested": item.suggested,
                    "fingerprint": item.suggestion_fingerprint,
                }
                for item in labels
            ],
            "moat": suggestion.moat.model_dump(mode="json"),
            "valuation": valuation.model_dump(mode="json"),
        }
    )
    return CompanyProfileProjection(
        labels=labels,
        summary=suggestion.summary,
        state=state,
        moat=suggestion.moat,
        source_artifact_id=source_artifact_id,
        refresh_fingerprint=fingerprint,
    )


def project_etf_profile(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    inputs: EtfProfileInputs,
) -> EtfProfileProjection:
    """Project the current ETF shape without an LLM call or persisted suggestion."""

    suggestions = derive_etf_label_evidence(inputs)
    labels = resolve_etf_label_presentations(conn, ticker=ticker, suggestions=suggestions)
    evidence_axes = {
        "fund profile": inputs.profile_available,
        "style loadings": inputs.style_evidence_available,
        "candidate vs book": inputs.book_evidence_available,
        "portfolio what-if": inputs.whatif_evidence_available,
    }
    available_count = sum(evidence_axes.values())
    if available_count >= 3:
        coverage = EtfEvidenceCoverage.SUFFICIENT
    elif available_count:
        coverage = EtfEvidenceCoverage.PARTIAL
    else:
        coverage = EtfEvidenceCoverage.INSUFFICIENT
    gaps = [name for name, available in evidence_axes.items() if not available]

    if any(item.state is ProfileLabelState.REVIEW_SUGGESTED for item in labels):
        state = ProfileProjectionState.REVIEW_SUGGESTED
    elif labels and all(item.state is ProfileLabelState.OWNER_RATIFIED for item in labels):
        state = ProfileProjectionState.OWNER_RATIFIED
    elif labels:
        state = ProfileProjectionState.SYSTEM_SUGGESTED
    else:
        state = ProfileProjectionState.UNAVAILABLE
    if labels:
        summary = (
            "Current evidence supports " + ", ".join(item.display_label for item in labels) + "."
        )
    elif coverage is EtfEvidenceCoverage.INSUFFICIENT:
        summary = "ETF profile is pending current fund and book evidence."
    else:
        summary = "Current evidence does not support a seeded ETF profile label."
    fingerprint = _sha(
        {
            "rule_version": ETF_PROFILE_RULE_VERSION,
            "ticker": ticker.upper(),
            "inputs": inputs.model_dump(mode="json"),
            "labels": [
                {
                    "label": item.label.value,
                    "state": item.state.value,
                    "suggested": item.suggested,
                    "fingerprint": item.suggestion_fingerprint,
                }
                for item in labels
            ],
        }
    )
    return EtfProfileProjection(
        labels=labels,
        summary=summary,
        state=state,
        evidence_coverage=coverage,
        evidence_gaps=gaps,
        refresh_fingerprint=fingerprint,
    )


def _unavailable_profile_suggestion_for_projection() -> InvestmentProfileSuggestion:
    return InvestmentProfileSuggestion(
        labels=[],
        summary="Profile synthesis is pending governed brief and earnings evidence.",
        moat=MoatAssessment(
            level=None,
            evidence_coverage=MoatEvidenceCoverage.INSUFFICIENT,
            rationale="No current structured investment-profile synthesis is available.",
        ),
    )


__all__ = [
    "ETF_PROFILE_RULE_VERSION",
    "CompanyProfileLabel",
    "CompanyProfileProjection",
    "EtfEvidenceCoverage",
    "EtfProfileInputs",
    "EtfProfileLabel",
    "EtfProfileLabelEvidence",
    "EtfProfileLabelPresentation",
    "EtfProfileProjection",
    "EtfStyleEvidence",
    "InvestmentProfileSuggestion",
    "LabelReviewAction",
    "MoatAssessment",
    "MoatEvidenceCoverage",
    "MoatLevel",
    "ProfileLabelEvidence",
    "ProfileLabelPresentation",
    "ProfileLabelState",
    "ProfileProjectionState",
    "ValuationEvidence",
    "derive_company_label_evidence",
    "derive_etf_label_evidence",
    "project_company_profile",
    "project_etf_profile",
    "record_label_review",
    "resolve_etf_label_presentations",
    "resolve_label_presentations",
]
