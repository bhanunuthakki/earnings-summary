"""Investment Decision Card (PRD ``docs/design/personal_investment_partner_prd.md``
Â§8.1, P1.1). The typed ``InvestmentDecisionCard`` separates company hypothesis,
security setup, portfolio fit, disconfirming case, evidence readiness,
suggested disposition, and uncertainty into seven distinct sections so
opening an evaluation answers all seven Â§8.1 questions at once.

Flow (see :func:`generate_card`):

1. Deterministic inputs FIRST â€” :func:`allocation.eligibility.assess_eligibility`
   (the evidence_readiness section is COPIED from its
   :class:`~allocation.eligibility.DecisionReadyAssessment`: ``decision_ready``
   = ``assessment.eligible``, ``blockers`` = ``assessment.blocking_reasons``),
   the latest ``dcf_runs`` row (an outlier ``sanity_flag`` independently forces
   ``decision_ready=False`` and a caveat), the current bear-case artifact, the
   authored/system thesis (``compute.thesis_evaluator.load_holdings_spec``),
   the comp set (optional, None-safe), candidate fit, and the expected
   Concentration Zone for a hypothetical 3%-of-book add.
2. ONE governed structured call (``call_llm_structured``,
   purpose=``investment_decision_card``) composes the seven sections'
   JUDGMENT text from those grounded facts.
3. ``model_validate`` + a deterministic post-pass that OVERWRITES
   ``evidence_readiness``, ``hypothesis_origin``, and the security's
   ``current_price``/``price_as_of``/outlier caveat from the deterministic
   inputs â€” **the LLM can never set ``decision_ready=True`` when the
   assessment says blocked**, and can never invent a price.
4. Persist via ``llm_artifact_store.upsert`` â€” scope='ticker',
   purpose='investment_decision_card', cached on (assessment.input_sha,
   thesis hash, dcf run ref, bear artifact id, fit computed_at).

Error handling (Â§10.6, mirrors ``allocation.recommendation_artifact``):
budget-exceeded and transient LLM failures degrade to a labeled
deterministic-minimal card when the deterministic inputs suffice (a thesis
is on file); otherwise ``generate_card`` returns an explicit
``CardResult(failure_reason=...)`` â€” never a fake-empty artifact. Any
``is_hard_stop`` exception (auth/setup) propagates loud.

``act_on_card`` is the ONE action core for the four owner verbs
(pass/watch/research_further/promote â€” PRD Â§8.1 disposition semantics),
reachable from both the HTTP route and any future Telegram dispatcher.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

import llm_artifact_store
from allocation.candidate_fit import CandidateFit
from allocation.concentration import classify_zone
from allocation.eligibility import DecisionReadyAssessment, assess_eligibility
from candidate_fit_cache import read_materialized_candidate_fit, read_materialized_fit_meta
from compute.thesis_evaluator import HoldingsSpec, load_holdings_spec
from db_paths import resolve_db_path
from identity import DEFAULT_USER_ID
from llm.cli import LLMBudgetExceeded, is_hard_stop
from llm.prompt_versions import prompt_version_for
from llm.structured import StructuredParseError, call_llm_structured
from llm_budget import should_skip_for_budget
from models.companies import ListType
from portfolio_weights import read_materialized_weights
from research.investment_profile import (
    InvestmentProfileSuggestion,
    MoatAssessment,
    MoatEvidenceCoverage,
    MoatLevel,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

__all__ = [
    "ACTION_VERBS",
    "PURPOSE",
    "CardActionError",
    "CardResult",
    "CompanyHypothesis",
    "DisconfirmingCase",
    "EvidenceReadiness",
    "InvestmentDecisionCard",
    "InvestmentProfileSuggestion",
    "MoatAssessment",
    "MoatEvidenceCoverage",
    "MoatLevel",
    "PortfolioFit",
    "SecuritySetup",
    "Uncertainty",
    "act_on_card",
    "generate_card",
]

PURPOSE = "investment_decision_card"
ENGINE_VERSION = "v2"

# A hypothetical add sized as 3% of book value â€” the Â§8.1 "expected
# Concentration Zone if funded" preview. Not a recommendation of size; purely
# an illustrative zone read (mirrors allocation.recommendation's cash-funded
# resulting-weight formula, applied to a fixed 3% rather than a live cash
# amount, since the card has no cash figure to work from).
_HYPOTHETICAL_ADD_FRACTION = 0.03


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


class CompanyHypothesis(BaseModel):
    directional_thesis: str = ""
    operating_mechanism: str = ""
    key_kpis: list[str] = Field(default_factory=list[str])
    confirming_evidence: list[str] = Field(default_factory=list[str])
    disconfirming_evidence: list[str] = Field(default_factory=list[str])


class SecuritySetup(BaseModel):
    current_price: float | None = None
    price_as_of: str | None = None
    valuation_range: str = ""
    appears_priced_in: str = ""
    caveats: list[str] = Field(default_factory=list[str])


class PortfolioFit(BaseModel):
    expected_role: str = ""
    candidate_fit_summary: str = ""
    correlated_exposure: str = ""
    expected_zone_if_funded: str | None = None


class DisconfirmingCase(BaseModel):
    bear_hypothesis: str = ""
    evidence_that_would_confirm_it: str = ""
    next_proof_point: str = ""


class EvidenceReadiness(BaseModel):
    available_source_classes: list[str] = Field(default_factory=list[str])
    stale_or_missing: list[str] = Field(default_factory=list[str])
    decision_ready: bool = False
    blockers: list[str] = Field(default_factory=list[str])


class Uncertainty(BaseModel):
    confidence_verbal: Literal["low", "moderate", "high"] = "low"
    justification: str = ""
    what_would_change_it: str = ""


def _unavailable_profile_suggestion() -> InvestmentProfileSuggestion:
    """Backward-compatible boundary for artifacts created before profile v2."""

    return InvestmentProfileSuggestion(
        labels=[],
        summary="Investment profile has not yet been synthesized from the governed corpus.",
        moat=MoatAssessment(
            level=None,
            evidence_coverage=MoatEvidenceCoverage.INSUFFICIENT,
            rationale="The current artifact predates the governed moat assessment.",
        ),
    )


class InvestmentDecisionCard(BaseModel):
    """The full Â§8.1 structured output â€” the Pydantic BOUNDARY model between
    the LLM's raw JSON and everything downstream (persistence, routes,
    disposition). ``evidence_readiness``, ``hypothesis_origin``, and the
    price fields on ``security_setup`` are ALWAYS overwritten by
    :func:`generate_card`'s deterministic post-pass after ``model_validate`` â€”
    never trust these three as LLM-authored in a persisted artifact."""

    ticker: str = Field(min_length=1)
    as_of: str = Field(min_length=1)
    input_sha: str = Field(min_length=1)
    engine_version: str = ENGINE_VERSION
    prompt_version: str = "v1"
    source_refs: list[str] = Field(default_factory=list[str])
    hypothesis_origin: Literal["user_authored", "system_drafted", "missing"] = "missing"

    company_hypothesis: CompanyHypothesis
    security_setup: SecuritySetup
    portfolio_fit: PortfolioFit
    investment_profile: InvestmentProfileSuggestion = Field(
        default_factory=_unavailable_profile_suggestion
    )
    disconfirming_case: DisconfirmingCase
    evidence_readiness: EvidenceReadiness
    suggested_disposition: Literal["pass", "watch", "research_further", "promote"]
    uncertainty: Uncertainty

    def validate_grounding(self, *, allowed_refs: set[str]) -> list[str]:
        """Â§10.5 structural checks that go beyond schema shape: required
        sections carry real content, the three top-level judgments are
        distinct (not one sentence copy-pasted across sections), and every
        cited source_ref resolves against what the generator actually
        gathered. Returns human-readable reasons; empty means "passes"."""
        reasons: list[str] = []
        if not self.company_hypothesis.directional_thesis.strip():
            reasons.append("company_hypothesis.directional_thesis is empty")
        if not self.security_setup.appears_priced_in.strip():
            reasons.append("security_setup.appears_priced_in is empty")
        if not self.portfolio_fit.expected_role.strip():
            reasons.append("portfolio_fit.expected_role is empty")
        if not self.disconfirming_case.bear_hypothesis.strip():
            reasons.append("disconfirming_case.bear_hypothesis is empty")

        distinct_texts = {
            self.company_hypothesis.directional_thesis.strip(),
            self.security_setup.appears_priced_in.strip(),
            self.portfolio_fit.expected_role.strip(),
        }
        if len(distinct_texts) < 3 and all(distinct_texts):
            reasons.append(
                "company_hypothesis/security_setup/portfolio_fit are not distinct â€” "
                "at least two sections repeat the same text verbatim"
            )

        for ref in self.source_refs:
            if ref in allowed_refs:
                continue
            if any(ref in allowed for allowed in allowed_refs):
                continue
            reasons.append(f"source_refs cites {ref!r}, not present in the gathered inputs")
        return reasons


class _InvestmentDecisionCardDraft(BaseModel):
    """Only model-authored fields; deterministic identity/readiness fields are injected later."""

    company_hypothesis: CompanyHypothesis
    security_setup: SecuritySetup
    portfolio_fit: PortfolioFit
    investment_profile: InvestmentProfileSuggestion
    disconfirming_case: DisconfirmingCase
    suggested_disposition: Literal["pass", "watch", "research_further", "promote"]
    uncertainty: Uncertainty
    source_refs: list[str] = Field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class CardResult:
    """The generator's output. ``card`` is populated for every outcome except
    an explicit failure (deterministic inputs insufficient to even build a
    fallback) â€” never a fake-empty artifact standing in for a real failure."""

    artifact_id: int | None
    card: InvestmentDecisionCard | None
    cache_hit: bool
    selection_mode: Literal["llm", "deterministic_fallback", "failed"] = "llm"
    degraded_reasons: tuple[str, ...] = ()
    failure_reason: str | None = None


# --------------------------------------------------------------------------- #
# Deterministic input gathering
# --------------------------------------------------------------------------- #


def _sha(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _lookup_list_type(db_path: Path, ticker: str, *, user_id: str = DEFAULT_USER_ID) -> str | None:
    conn = _ro_conn(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT list_type FROM tracked_companies "
            "WHERE user_id = ? AND UPPER(ticker) = ? AND archived_at IS NULL",
            (user_id, ticker),
        ).fetchone()
        return str(row["list_type"]) if row is not None and row["list_type"] else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _dcf_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
    except sqlite3.Error:
        return set()


def _latest_dcf_row(db_path: Path, ticker: str) -> sqlite3.Row | None:
    """Local mirror of ``allocation.eligibility._latest_dcf_row`` â€” see that
    module's docstring note on why this small query template is duplicated
    per-caller rather than shared (repo convention: duplicate simple shared
    logic, don't modularize)."""
    conn = _ro_conn(db_path)
    if conn is None:
        return None
    try:
        cols = _dcf_columns(conn)
        if not cols:
            return None
        is_latest_pred = "COALESCE(is_latest, 1) = 1" if "is_latest" in cols else "1 = 1"
        seg_pred = "COALESCE(segment_name, '') = ''" if "segment_name" in cols else "1 = 1"
        sanity_sel = "sanity_flag" if "sanity_flag" in cols else "NULL AS sanity_flag"
        price_at_sel = "live_price_at" if "live_price_at" in cols else "NULL AS live_price_at"
        return conn.execute(
            f"SELECT ticker, valuation_date, npv_per_share, live_price, "
            f"{price_at_sel}, {sanity_sel}, created_at, id FROM dcf_runs "
            f"WHERE UPPER(ticker) = ? AND {is_latest_pred} AND {seg_pred} "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _hypothesis_origin(
    spec: HoldingsSpec | None,
) -> Literal["user_authored", "system_drafted", "missing"]:
    if spec is None or not spec.thesis:
        return "missing"
    return "system_drafted" if "STUB:" in spec.thesis else "user_authored"


def _expected_zone_if_funded(repo_root: Path, ticker: str) -> str | None:
    weights = read_materialized_weights(repo_root)
    current_frac = weights.get(ticker.upper(), 0.0)
    funded_frac = (current_frac + _HYPOTHETICAL_ADD_FRACTION) / (1.0 + _HYPOTHETICAL_ADD_FRACTION)
    assessment = classify_zone(funded_frac * 100.0)
    return assessment.zone if assessment is not None else None


@dataclass(frozen=True, slots=True)
class _Inputs:
    assessment: DecisionReadyAssessment
    dcf_row: sqlite3.Row | None
    dcf_outlier: bool
    bear_artifact: llm_artifact_store.Artifact | None
    spec: HoldingsSpec | None
    fit: CandidateFit | None
    fit_asof: str | None
    comp_set_summary: str | None
    expected_zone: str | None
    hypothesis_origin: Literal["user_authored", "system_drafted", "missing"]
    input_sha: str
    cache_inputs: list[str]


def _gather_inputs(db_path: Path, repo_root: Path, ticker: str, *, list_type: str) -> _Inputs:
    assessment = assess_eligibility(db_path, repo_root, ticker, list_type=list_type)
    dcf_row = _latest_dcf_row(db_path, ticker)
    dcf_outlier = bool(dcf_row is not None and dcf_row["sanity_flag"])

    bear_artifact = llm_artifact_store.read_current(
        ticker=ticker, purpose="bear_case", scope="ticker", db_path=db_path
    )
    if bear_artifact is not None and not llm_artifact_store.artifact_is_fresh(bear_artifact):
        bear_artifact = None

    holdings_dir = repo_root / "micro_thesis" / "holdings"
    spec: HoldingsSpec | None
    try:
        spec = load_holdings_spec(holdings_dir, ticker)
    except FileNotFoundError:
        spec = None
    except (ValueError, ValidationError):
        spec = None

    comp_set_summary: str | None = None
    try:
        from report.sections.comp_set_context import load_comp_set_context

        comp_set = load_comp_set_context(ticker, db_path=db_path, repo_root=repo_root)
        if comp_set is not None:
            comp_set_summary = (
                f"{comp_set.n_members} comp-set peer(s), {comp_set.metric_class} metric class"
                + (f", industry={comp_set.industry}" if comp_set.industry else "")
                + (f", benchmark ETF={comp_set.benchmark_etf}" if comp_set.benchmark_etf else "")
            )
    except Exception:  # comp set is optional and best-effort â€” never blocks the card
        comp_set_summary = None

    fit_cache = read_materialized_candidate_fit(repo_root)
    fit = fit_cache.get(ticker.upper())
    fit_meta = read_materialized_fit_meta(repo_root)
    fit_asof_raw = fit_meta.get("computed_at")
    fit_asof = str(fit_asof_raw) if isinstance(fit_asof_raw, str) else None

    expected_zone = _expected_zone_if_funded(repo_root, ticker)
    origin = _hypothesis_origin(spec)

    thesis_hash = hashlib.sha256((spec.thesis if spec else "").encode("utf-8")).hexdigest()
    dcf_ref = f"{dcf_row['id']}:{dcf_row['valuation_date']}" if dcf_row is not None else ""
    bear_ref = str(bear_artifact.id) if bear_artifact is not None else ""
    cache_inputs = [assessment.input_sha, thesis_hash, dcf_ref, bear_ref, fit_asof or ""]

    payload = {
        "assessment_sha": assessment.input_sha,
        "thesis_hash": thesis_hash,
        "dcf_ref": dcf_ref,
        "bear_ref": bear_ref,
        "fit_asof": fit_asof or "",
    }
    return _Inputs(
        assessment=assessment,
        dcf_row=dcf_row,
        dcf_outlier=dcf_outlier,
        bear_artifact=bear_artifact,
        spec=spec,
        fit=fit,
        fit_asof=fit_asof,
        comp_set_summary=comp_set_summary,
        expected_zone=expected_zone,
        hypothesis_origin=origin,
        input_sha=_sha(payload),
        cache_inputs=cache_inputs,
    )


def _build_evidence_readiness(inputs: _Inputs) -> EvidenceReadiness:
    a = inputs.assessment
    blockers = list(a.blocking_reasons)
    decision_ready = a.eligible and not inputs.dcf_outlier
    if inputs.dcf_outlier and not any("sanity_flag" in b for b in blockers):
        blockers.append("latest DCF run carries a sanity_flag â€” valuation not decision-ready")
    available = sorted(a.source_freshness)
    if inputs.bear_artifact is not None:
        available.append("bear_case")
    if inputs.comp_set_summary is not None:
        available.append("comp_set")
    if inputs.fit is not None:
        available.append("candidate_fit")
    return EvidenceReadiness(
        available_source_classes=available,
        stale_or_missing=list(a.warning_reasons),
        decision_ready=decision_ready,
        blockers=blockers,
    )


def _allowed_refs(inputs: _Inputs) -> set[str]:
    refs: set[str] = set(inputs.assessment.source_freshness)
    refs.update(inputs.assessment.blocking_reasons)
    refs.update(inputs.assessment.warning_reasons)
    if inputs.spec is not None:
        refs.add(inputs.spec.thesis)
        refs.update(
            r.rule_id for r in (*inputs.spec.break_rules, *inputs.spec.business_model_rules)
        )
    if inputs.fit is not None:
        refs.add(inputs.fit.why)
    if inputs.comp_set_summary is not None:
        refs.add(inputs.comp_set_summary)
    if inputs.dcf_row is not None:
        refs.add(str(inputs.dcf_row["valuation_date"]))
    return refs


# --------------------------------------------------------------------------- #
# Prompt composition
# --------------------------------------------------------------------------- #


def _build_prompt(
    ticker: str,
    inputs: _Inputs,
    *,
    corrective_reasons: list[str] | None = None,
) -> str:
    a = inputs.assessment
    dcf_block = "(no DCF run on file)"
    if inputs.dcf_row is not None:
        fv = inputs.dcf_row["npv_per_share"]
        px = inputs.dcf_row["live_price"]
        dcf_block = (
            f"valuation_date={inputs.dcf_row['valuation_date']}, "
            f"fair_value_per_share={fv}, live_price={px}"
            + (", SANITY_FLAG SET â€” treat as an unreviewed outlier" if inputs.dcf_outlier else "")
        )
    thesis_block = (
        f"{inputs.spec.thesis} (origin={inputs.hypothesis_origin})"
        if inputs.spec is not None
        else "(no thesis on file)"
    )
    breakers = (
        [r.rule_id for r in (*inputs.spec.break_rules, *inputs.spec.business_model_rules)]
        if inputs.spec is not None
        else []
    )
    breakers_block = "\n".join(f"- {r}" for r in breakers) or "(none on file)"
    bear_block = (
        (inputs.bear_artifact.content_md or "")[:2000]
        if inputs.bear_artifact is not None
        else "(no bear case on file)"
    )
    fit_block = inputs.fit.why if inputs.fit is not None else "(no candidate-fit score on file)"
    comp_block = inputs.comp_set_summary or "(no comparable set on file)"
    zone_block = inputs.expected_zone or "(zone unknown)"
    freshness_block = (
        "\n".join(f"- {k}: {v}" for k, v in sorted(a.source_freshness.items())) or "(none)"
    )
    blocking_block = (
        "\n".join(f"- {r}" for r in a.blocking_reasons) or "(none â€” currently eligible)"
    )

    corrective_block = ""
    if corrective_reasons:
        corrective_block = (
            "\n\nYOUR PRIOR ATTEMPT FAILED THESE CHECKS â€” fix every one:\n"
            + "\n".join(f"- {r}" for r in corrective_reasons)
        )

    return f"""You are drafting the Investment Decision Card for {ticker}.

THESIS ON FILE:
{thesis_block}

DISCONFIRMING BREAK RULES ON FILE:
{breakers_block}

LATEST DCF RUN:
{dcf_block}

BEAR CASE ON FILE (verbatim excerpt, may be empty):
{bear_block}

CANDIDATE FIT (portfolio-fit factor decomposition, may be empty for a held name):
{fit_block}

COMPARABLE SET:
{comp_block}

EXPECTED CONCENTRATION ZONE IF FUNDED (hypothetical 3%-of-book add):
{zone_block}

SOURCE FRESHNESS (cite only these keys, or facts verbatim from the blocks
above, in source_refs):
{freshness_block}

DECISION-READY GATE â€” currently {"BLOCKED" if not a.eligible else "ELIGIBLE"}. Blocking reasons
(you do NOT control this â€” evidence_readiness is computed deterministically
and will overwrite anything you write here):
{blocking_block}

VALIDATION CONSTRAINTS (a violation forces a deterministic fallback):
- Every material claim in company_hypothesis/security_setup/portfolio_fit must
  trace to a fact in the blocks above â€” never invent a figure or event.
- company_hypothesis, security_setup, and portfolio_fit must be DISTINCT
  judgments, not the same sentence repeated across sections.
- investment_profile.labels is a multi-select qualitative classification. Use
  only long_term_compounder, turnaround, narrative_rerating,
  growth_inflection, cash_yield_value, and optionality. Do not assign garp or elite_growth_expensive;
  those valuation-aware labels are derived deterministically from admitted DCF
  and financial inputs after this call.
- investment_profile.moat.level is exactly one of multi_business (durable
  advantages across multiple economically meaningful businesses),
  core_business (durable advantage in the primary economic engine),
  narrow_conditional (advantage limited by product, geography, customer set,
  or cycle), or none_demonstrated (the evidence supports no durable advantage).
  Missing evidence is not none_demonstrated: set level=null and
  evidence_coverage=insufficient instead.
- disconfirming_case.bear_hypothesis must present a REAL case against the
  thesis (use the bear case above if present), never a token caveat.
- uncertainty.justification and uncertainty.what_would_change_it must never
  be empty or boilerplate.
- NEVER state a numeric probability ("62% chance") â€” use qualitative
  confidence language instead (house style: "This is my read because...",
  "The main reason I could be wrong is...").
- source_refs may only cite the SOURCE FRESHNESS keys above or facts that
  appear verbatim in the blocks above â€” never an invented citation.
- Do NOT populate an "evidence_readiness" field â€” it is computed
  deterministically and any value you write is discarded.
{corrective_block}

Respond with ONE JSON object matching this shape exactly:
{{
  "company_hypothesis": {{
    "directional_thesis": "...",
    "operating_mechanism": "...",
    "key_kpis": ["..."],
    "confirming_evidence": ["..."],
    "disconfirming_evidence": ["..."]
  }},
  "security_setup": {{
    "valuation_range": "...",
    "appears_priced_in": "...",
    "caveats": ["..."]
  }},
  "portfolio_fit": {{
    "expected_role": "...",
    "candidate_fit_summary": "...",
    "correlated_exposure": "..."
  }},
  "investment_profile": {{
    "labels": ["long_term_compounder" | "turnaround" | "narrative_rerating" | "growth_inflection" | "cash_yield_value" | "optionality", ...],
    "summary": "...",
    "moat": {{
      "level": "multi_business" | "core_business" | "narrow_conditional" | "none_demonstrated" | null,
      "evidence_coverage": "sufficient" | "partial" | "insufficient",
      "rationale": "...",
      "supporting_evidence": ["..."],
      "counter_evidence": ["..."]
    }}
  }},
  "disconfirming_case": {{
    "bear_hypothesis": "...",
    "evidence_that_would_confirm_it": "...",
    "next_proof_point": "..."
  }},
  "suggested_disposition": "pass" | "watch" | "research_further" | "promote",
  "uncertainty": {{
    "confidence_verbal": "low" | "moderate" | "high",
    "justification": "The main reason I could be wrong is ...",
    "what_would_change_it": "..."
  }},
  "source_refs": ["<source_freshness key or verbatim fact>", ...]
}}"""


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #


def _deterministic_card(
    ticker: str,
    inputs: _Inputs,
    *,
    as_of: str,
    extra_degraded: str | None = None,
) -> InvestmentDecisionCard | None:
    """Â§8.1's mechanical fallback: built ONLY from grounded facts, no LLM
    prose. A deterministically BLOCKED assessment persists an explicit blocker
    card even when no thesis is on file, because the missing thesis is itself
    the decision-readiness result. An eligible assessment still may not
    fabricate a missing thesis."""
    if (inputs.spec is None or not inputs.spec.thesis) and inputs.assessment.eligible:
        return None

    thesis = (
        inputs.spec.thesis
        if inputs.spec is not None and inputs.spec.thesis
        else "No directional hypothesis is on file; this evaluation is explicitly blocked."
    )

    degraded_note = f" ({extra_degraded})" if extra_degraded else ""
    fv = px = None
    valuation_range = "(no DCF run on file)"
    if inputs.dcf_row is not None:
        fv = inputs.dcf_row["npv_per_share"]
        px = inputs.dcf_row["live_price"]
        if fv is not None and px is not None:
            valuation_range = f"fair value ${float(fv):.2f} vs live price ${float(px):.2f}"

    breakers = (
        [r.rule_id for r in (*inputs.spec.break_rules, *inputs.spec.business_model_rules)]
        if inputs.spec is not None
        else list(inputs.assessment.blocking_reasons)
    )
    payload: dict[str, object] = {
        "ticker": ticker,
        "as_of": as_of,
        "input_sha": inputs.input_sha,
        "engine_version": ENGINE_VERSION,
        "prompt_version": prompt_version_for(PURPOSE),
        "source_refs": [],
        "hypothesis_origin": inputs.hypothesis_origin,
        "company_hypothesis": {
            "directional_thesis": thesis,
            "operating_mechanism": f"mechanical fallback{degraded_note} â€” no LLM judgment applied",
            "key_kpis": [],
            "confirming_evidence": [],
            "disconfirming_evidence": breakers,
        },
        "security_setup": {
            "valuation_range": valuation_range,
            "appears_priced_in": "mechanical fallback â€” no LLM read of what is priced in",
            "caveats": ["this is a mechanical fallback â€” no LLM judgment was applied"],
        },
        "portfolio_fit": {
            "expected_role": "mechanical fallback â€” no LLM portfolio-fit judgment applied",
            "candidate_fit_summary": inputs.fit.why if inputs.fit is not None else "no fit on file",
            "correlated_exposure": inputs.comp_set_summary or "no comparable set on file",
            "expected_zone_if_funded": inputs.expected_zone,
        },
        "investment_profile": _unavailable_profile_suggestion().model_dump(mode="json"),
        "disconfirming_case": {
            "bear_hypothesis": (
                (inputs.bear_artifact.content_md or "")[:500]
                if inputs.bear_artifact is not None
                else "no bear case on file â€” this is the weakest point of a mechanical fallback"
            ),
            "evidence_that_would_confirm_it": "see the bear case artifact (if any) for specifics",
            "next_proof_point": "the next scheduled earnings print",
        },
        "evidence_readiness": _build_evidence_readiness(inputs).model_dump(mode="json"),
        "suggested_disposition": "research_further",
        "uncertainty": {
            "confidence_verbal": "low",
            "justification": f"mechanical fallback{degraded_note} â€” no synthesized confidence",
            "what_would_change_it": "a governed LLM read of the same evidence",
        },
    }
    return InvestmentDecisionCard.model_validate(payload)


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def _render_markdown(card: InvestmentDecisionCard) -> str:
    lines = [
        f"# Investment Decision Card â€” {card.ticker} ({card.as_of})",
        "",
        f"**Suggested disposition:** {card.suggested_disposition}  |  "
        f"**Confidence:** {card.uncertainty.confidence_verbal}  |  "
        f"**Decision-ready:** {card.evidence_readiness.decision_ready}",
        "",
        "## Company hypothesis",
        card.company_hypothesis.directional_thesis,
        "",
        "## Security setup",
        f"{card.security_setup.valuation_range} â€” {card.security_setup.appears_priced_in}",
        "",
        "## Portfolio fit",
        card.portfolio_fit.expected_role,
        "",
        "## Investment profile",
        ", ".join(label.display_label for label in card.investment_profile.labels)
        or "No qualitative profile labels",
        card.investment_profile.summary,
        (
            card.investment_profile.moat.level.display_label
            if card.investment_profile.moat.level is not None
            else "Moat evidence insufficient"
        ),
        card.investment_profile.moat.rationale,
        "",
        "## Disconfirming case",
        card.disconfirming_case.bear_hypothesis,
        "",
        "## Evidence readiness",
        f"blockers: {', '.join(card.evidence_readiness.blockers) or '(none)'}",
        "",
        "## Uncertainty",
        f"{card.uncertainty.justification}",
    ]
    return "\n".join(lines)


def _call_and_validate(
    ticker: str,
    inputs: _Inputs,
    *,
    as_of: str,
    db_path: Path,
    corrective_reasons: list[str] | None = None,
) -> tuple[InvestmentDecisionCard, list[str]]:
    prompt = _build_prompt(ticker, inputs, corrective_reasons=corrective_reasons)
    payload = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        ticker=ticker,
        scope="ticker",
        expect="object",
        required_keys=(
            "company_hypothesis",
            "security_setup",
            "portfolio_fit",
            "investment_profile",
            "disconfirming_case",
            "suggested_disposition",
            "uncertainty",
        ),
        schema=TypeAdapter(_InvestmentDecisionCardDraft),
        db_path=db_path,
    )
    # Test doubles and older internal injectors may still return the decoded
    # dict; validate that compatibility seam through the same exact contract.
    draft = (
        payload
        if isinstance(payload, _InvestmentDecisionCardDraft)
        else _InvestmentDecisionCardDraft.model_validate(payload)
    )
    raw = cast("dict[str, object]", draft.model_dump())
    raw["ticker"] = ticker
    raw["as_of"] = as_of
    raw["input_sha"] = inputs.input_sha
    raw["engine_version"] = ENGINE_VERSION
    raw["prompt_version"] = prompt_version_for(PURPOSE)
    # hypothesis_origin, evidence_readiness, and security_setup's price fields
    # are ALWAYS overwritten below regardless of what the LLM sent â€” set here
    # only so model_validate has a structurally valid placeholder to parse.
    raw["hypothesis_origin"] = inputs.hypothesis_origin
    raw["evidence_readiness"] = _build_evidence_readiness(inputs).model_dump(mode="json")
    try:
        card = InvestmentDecisionCard.model_validate(raw)
    except Exception as exc:
        raise StructuredParseError(
            f"{PURPOSE}: LLM JSON failed schema validation: {exc}", raw_head=str(raw)[:500]
        ) from exc
    card = _apply_deterministic_overrides(card, inputs)
    reasons = card.validate_grounding(allowed_refs=_allowed_refs(inputs))
    return card, reasons


def _apply_deterministic_overrides(
    card: InvestmentDecisionCard, inputs: _Inputs
) -> InvestmentDecisionCard:
    """The post-validation pass Â§8.1 requires: evidence_readiness and
    hypothesis_origin are NEVER LLM-authored in the persisted artifact, and
    the security's price fields are always the real ``dcf_runs`` values (or
    None), never an LLM-invented number. A DCF outlier appends a caveat
    regardless of what the LLM wrote."""
    price = None
    price_as_of = None
    if inputs.dcf_row is not None:
        raw_price = inputs.dcf_row["live_price"]
        price = float(raw_price) if raw_price is not None else None
        raw_at = inputs.dcf_row["live_price_at"]
        price_as_of = str(raw_at) if raw_at else None

    caveats = list(card.security_setup.caveats)
    outlier_note = "latest DCF run carries a sanity_flag â€” treat the valuation as an outlier"
    if inputs.dcf_outlier and outlier_note not in caveats:
        caveats.append(outlier_note)

    security_setup = card.security_setup.model_copy(
        update={"current_price": price, "price_as_of": price_as_of, "caveats": caveats}
    )
    return card.model_copy(
        update={
            "evidence_readiness": _build_evidence_readiness(inputs),
            "hypothesis_origin": inputs.hypothesis_origin,
            "security_setup": security_setup,
        }
    )


def generate_card(db_path: Path, repo_root: Path, ticker: str) -> CardResult:
    """The full P1.1 governed pipeline for one ticker. Never raises except
    for a genuine hard stop (auth/setup) â€” every other failure mode
    degrades to a labeled deterministic fallback, or an explicit
    ``CardResult(failure_reason=...)`` when even that isn't groundable."""
    ticker = ticker.upper()
    now = datetime.now(UTC).replace(tzinfo=None)
    as_of = now.isoformat()

    list_type = _lookup_list_type(db_path, ticker)
    if list_type is None:
        return CardResult(
            artifact_id=None,
            card=None,
            cache_hit=False,
            selection_mode="failed",
            failure_reason=f"{ticker} is not an active tracked company",
        )

    inputs = _gather_inputs(db_path, repo_root, ticker, list_type=list_type)
    degraded: list[str] = []

    if not inputs.assessment.eligible:
        blockers = "; ".join(inputs.assessment.blocking_reasons[:5])
        degraded.append(f"deterministically blocked: {blockers}")
        card = _deterministic_card(
            ticker,
            inputs,
            as_of=as_of,
            extra_degraded="deterministically blocked; no LLM call made",
        )
        if card is None:
            return CardResult(
                artifact_id=None,
                card=None,
                cache_hit=False,
                selection_mode="failed",
                degraded_reasons=tuple(degraded),
                failure_reason="blocked assessment could not produce an explicit blocker card",
            )
        artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
        return CardResult(
            artifact_id=artifact_id,
            card=card,
            cache_hit=cache_hit,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )

    skip_check = should_skip_for_budget(PURPOSE, db_path=db_path)
    if skip_check is not None:
        degraded.append(f"forgone due to budget ({skip_check.reason or 'over cap'})")
        card = _deterministic_card(ticker, inputs, as_of=as_of, extra_degraded="budget forgone")
        if card is None:
            return CardResult(
                artifact_id=None,
                card=None,
                cache_hit=False,
                selection_mode="failed",
                degraded_reasons=tuple(degraded),
                failure_reason="budget forgone and no thesis on file for a deterministic fallback",
            )
        artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
        return CardResult(
            artifact_id=artifact_id,
            card=card,
            cache_hit=cache_hit,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )

    try:
        card, reasons = _call_and_validate(ticker, inputs, as_of=as_of, db_path=db_path)
        if reasons:
            card, reasons = _call_and_validate(
                ticker, inputs, as_of=as_of, db_path=db_path, corrective_reasons=reasons
            )
        if reasons:
            degraded.append(f"LLM output rejected: {'; '.join(reasons[:5])}")
            card = _deterministic_card(
                ticker,
                inputs,
                as_of=as_of,
                extra_degraded="LLM output failed grounding validation twice",
            )
            if card is None:
                return CardResult(
                    artifact_id=None,
                    card=None,
                    cache_hit=False,
                    selection_mode="failed",
                    degraded_reasons=tuple(degraded),
                    failure_reason="; ".join(reasons[:5]),
                )
            artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
            return CardResult(
                artifact_id=artifact_id,
                card=card,
                cache_hit=cache_hit,
                selection_mode="deterministic_fallback",
                degraded_reasons=tuple(degraded),
            )
    except LLMBudgetExceeded as exc:
        degraded.append(f"budget exceeded: {exc}")
        card = _deterministic_card(ticker, inputs, as_of=as_of, extra_degraded="budget exceeded")
        if card is None:
            return CardResult(
                artifact_id=None,
                card=None,
                cache_hit=False,
                selection_mode="failed",
                degraded_reasons=tuple(degraded),
                failure_reason=f"budget exceeded and no thesis on file: {exc}",
            )
        artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
        return CardResult(
            artifact_id=artifact_id,
            card=card,
            cache_hit=cache_hit,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        degraded.append(f"transient LLM failure: {exc}")
        card = _deterministic_card(
            ticker, inputs, as_of=as_of, extra_degraded="transient LLM failure"
        )
        if card is None:
            return CardResult(
                artifact_id=None,
                card=None,
                cache_hit=False,
                selection_mode="failed",
                degraded_reasons=tuple(degraded),
                failure_reason=f"transient LLM failure and no thesis on file: {exc}",
            )
        artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
        return CardResult(
            artifact_id=artifact_id,
            card=card,
            cache_hit=cache_hit,
            selection_mode="deterministic_fallback",
            degraded_reasons=tuple(degraded),
        )

    artifact_id, cache_hit = _persist(card, inputs, db_path=db_path)
    return CardResult(
        artifact_id=artifact_id,
        card=card,
        cache_hit=cache_hit,
        selection_mode="llm",
        degraded_reasons=tuple(degraded),
    )


def _persist(
    card: InvestmentDecisionCard, inputs: _Inputs, *, db_path: Path
) -> tuple[int | None, bool]:
    return llm_artifact_store.upsert(
        llm_artifact_store.UpsertRequest(
            ticker=card.ticker,
            scope="ticker",
            purpose=PURPOSE,
            content_json=card.model_dump(mode="json"),
            content_md=_render_markdown(card),
            prompt_version=card.prompt_version,
            cache_inputs=cast("list[bytes | str]", inputs.cache_inputs),
        ),
        db_path=db_path,
    )


# --------------------------------------------------------------------------- #
# Disposition action core (PRD Â§8.1 disposition semantics)
# --------------------------------------------------------------------------- #

ActionVerb = Literal["pass", "watch", "research_further", "promote"]
ACTION_VERBS: tuple[str, ...] = ("pass", "watch", "research_further", "promote")

# research_further deliberately excluded: it writes a research_tasks row, NOT
# a decisions row (PRD Â§8.1: "this is not an Owner Decision").
_DECISION_VERBS: tuple[str, ...] = ("pass", "watch", "promote")


class CardActionError(RuntimeError):
    """Raised when the disposition cannot be applied (unknown verb, missing
    artifact, or artifact not of this purpose)."""


def _open(db_path: Path | str | None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        raise CardActionError("portfolio DB not available")
    conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, purpose, ticker, content_json FROM llm_artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise CardActionError(f"no llm_artifacts row for id={artifact_id}")
    if row["purpose"] != PURPOSE:
        raise CardActionError(
            f"artifact {artifact_id} has purpose={row['purpose']!r}, expected {PURPOSE!r}"
        )
    if not row["ticker"]:
        raise CardActionError(f"artifact {artifact_id} carries no ticker")
    return row


def _existing_decision_id(conn: sqlite3.Connection, artifact_id: int, kind: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM decisions WHERE advice_artifact_id = ? AND recommendation_kind = ?",
        (artifact_id, kind),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _hypothesis_excerpt(content_json: str | None) -> str:
    try:
        decoded = json.loads(content_json or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(decoded, dict):
        return ""
    company = cast("dict[str, object]", decoded).get("company_hypothesis")
    if not isinstance(company, dict):
        return ""
    text = cast("dict[str, object]", company).get("directional_thesis")
    return str(text)[:512] if text else ""


def act_on_card(
    artifact_id: int,
    verb: ActionVerb | str,
    *,
    db_path: Path | str | None = None,
    notes: str | None = None,
    _connection: sqlite3.Connection | None = None,
) -> str:
    """The ONE action core for the four Â§8.1 owner verbs. Returns a short
    status string:

      'pass'              -> 'pass_recorded' (new decision + archived) | 'already_pass'
      'watch'             -> 'watch_recorded' (new decision + watchlist) | 'already_watch'
      'promote'           -> 'promote_recorded' (new decision only) | 'already_promote'
      'research_further'  -> 'research_task_created:<id>' | 'already_proposed:<id>'
                              (NO decision row â€” PRD Â§8.1: not an Owner Decision)

    Raises :class:`CardActionError` for an unknown verb or a missing/
    mismatched-purpose artifact â€” never a silent no-op on a bad input.
    Idempotent: a second call for the same (artifact_id, verb) does not
    write a duplicate row.
    """
    if verb not in ACTION_VERBS:
        raise CardActionError(f"unknown verb {verb!r}; expected one of {ACTION_VERBS}")

    owns_connection = _connection is None
    conn = _open(db_path) if _connection is None else _connection
    try:
        artifact = _load_artifact(conn, artifact_id)
        ticker = str(artifact["ticker"]).upper()
        rationale = _hypothesis_excerpt(artifact["content_json"])

        if verb == "research_further":
            if not owns_connection:
                raise CardActionError("research_further cannot share an owner-decision transaction")
            claim = rationale or f"Research further: {ticker} (Investment Decision Card)"
            existing = conn.execute(
                "SELECT id FROM research_tasks WHERE UPPER(ticker) = ? AND claim = ? "
                "AND status = 'proposed'",
                (ticker, claim),
            ).fetchone()
            if existing is not None:
                return f"already_proposed:{int(existing['id'])}"
            from research.proposals import create_task

            task_id = create_task(note_id=None, claim=claim, ticker=ticker, db_path=db_path)
            return f"research_task_created:{task_id}"

        kind = verb  # 'pass' | 'watch' | 'promote' â€” the recommendation_kind verbatim
        existing_id = _existing_decision_id(conn, artifact_id, kind)
        if existing_id is not None:
            return f"already_{kind}"

        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        if verb == "pass":
            conn.execute(
                "UPDATE tracked_companies SET archived_at = ?, brief_dirty = 0 "
                "WHERE user_id = ? AND UPPER(ticker) = ? AND archived_at IS NULL",
                (now, DEFAULT_USER_ID, ticker),
            )
        elif verb == "watch":
            conn.execute(
                "UPDATE tracked_companies SET list_type = ?, "
                "brief_dirty = 0 "
                "WHERE user_id = ? AND UPPER(ticker) = ? AND archived_at IS NULL",
                (ListType.WATCHLIST.value, DEFAULT_USER_ID, ticker),
            )
        # 'promote': list_type stays 'evaluation' â€” the decision row IS the
        # marker (PRD Â§8.1: "does not claim the security is owned").

        conn.execute(
            """
            INSERT INTO decisions (
                ticker, recommendation_kind, decided_by, scope,
                advice_artifact_id, rationale_excerpt, user_notes,
                made_at, created_at
            ) VALUES (?, ?, 'owner', 'ticker', ?, ?, ?, ?, ?)
            """,
            (ticker, kind, artifact_id, rationale, notes, now, now),
        )
        if owns_connection:
            conn.commit()
        return f"{kind}_recorded"
    finally:
        if owns_connection:
            conn.close()
