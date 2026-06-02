"""Evaluate per-ticker thesis break rules against kpi_facts and write verdict to thesis_state.

Each holding has a `break_rules` section in `micro_thesis/holdings/<TICKER>.json`
encoding deterministic rules of the form: "KPI X comparator threshold for N
consecutive periods." This module joins those rules against the most-recent
N kpi_facts rows per KPI, classifies each rule (OK / WATCH / BREACH), and
returns the holding-level verdict (worst-rule wins).

Trend rules ("declining QoQ") and derived metrics ("net adds = delta") are
out of scope for the MVP — flagged via UNSUPPORTED_RULE in validation_issues.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from compute.kpi_resolver import resolve_kpi_definition_name
from compute.soft_rule_evaluator import (
    SoftRule,
    SoftRuleResult,
    SoftRuleStatus,
    evaluate_soft_rules,
    load_soft_rules,
)
from models.facts import Unit
from models.kpis import BreachStatus


class Comparator(StrEnum):
    """Closed enum of supported numeric comparators."""

    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    EQ = "eq"


class RuleTier(StrEnum):
    """Two-tier rule taxonomy.

    UNIVERSAL: catastrophic tripwires that apply to every holding (e.g. outright
    revenue decline). Kept intentionally narrow — the noisy GAAP-margin universals
    were removed in favor of per-ticker thresholds because SBC-heavy software,
    capex-cycle pharma, and banks all distort GAAP op/net margin in different ways.

    BUSINESS_MODEL: per-ticker breakers that reflect the actual unit economics of
    the business (sub-ARR contribution margin for RBRK, NIM/efficiency ratio for
    NU, FRE growth for BN, etc.). These are the rules that should fire FIRST when
    the thesis is genuinely breaking.
    """

    UNIVERSAL = "universal"
    BUSINESS_MODEL = "business_model"


class BreakRule(BaseModel):
    """One deterministic break rule from a holdings JSON.

    `consecutive_periods` defaults to 1 (instantaneous breach). The most-recent
    `consecutive_periods` kpi_facts values for `kpi_name` are inspected; the rule
    fires if all of them satisfy `comparator threshold`.

    `tier` distinguishes catastrophic tripwires (universal) from per-ticker
    thesis breakers (business_model). Defaults to business_model so rules added
    only to the per-ticker list inherit the correct tag without explicit marking.
    """

    rule_id: str = Field(min_length=1, max_length=80)
    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    threshold: Decimal
    unit: Unit
    consecutive_periods: int = Field(ge=1, le=12, default=1)
    narrative: str = Field(min_length=1, max_length=500)
    tier: RuleTier = RuleTier.BUSINESS_MODEL


class HoldingsSpec(BaseModel):
    """Subset of holdings JSON used by the evaluator.

    Two parallel arrays of hard rules. `break_rules` carries the (narrow)
    universal tripwires shared across holdings; `business_model_rules` carries
    the per-ticker breakers that reflect the actual unit economics. Tier is
    assigned at load time based on which array a rule came from — the evaluator
    merges them into a single sequence before fetching history.

    `soft_rules` is the predicate-style YELLOW signals from `break_rules_soft`
    in the on-disk JSON. They never drive RED — the rollup escalates only to
    WARN when any soft rule fires (see `evaluate_ticker_thesis`).
    """

    ticker: str
    thesis: str
    break_rules: list[BreakRule] = Field(default_factory=list)
    business_model_rules: list[BreakRule] = Field(default_factory=list)
    soft_rules: list[SoftRule] = Field(default_factory=list)


@dataclass(frozen=True)
class KpiObservation:
    """One historical kpi_facts value pulled for evaluation."""

    period_end: datetime
    value: Decimal
    unit: Unit


@dataclass(frozen=True)
class RuleEvaluation:
    """Per-rule outcome with the evidence that drove it."""

    rule: BreakRule
    status: BreachStatus
    observations: tuple[KpiObservation, ...]
    detail: str


@dataclass(frozen=True)
class ThesisVerdict:
    """Holding-level rollup of all rule evaluations.

    `soft_rule_results` are predicate-style YELLOW signals. They never bubble
    a verdict to BREACH — only WARN when any one is YELLOW and no hard rule
    breached. See `_rollup_with_soft` for the precedence.
    """

    ticker: str
    thesis: str
    overall_status: BreachStatus
    rule_evaluations: tuple[RuleEvaluation, ...]
    evaluated_at: datetime
    soft_rule_results: tuple[SoftRuleResult, ...] = ()


def load_holdings_spec(holdings_dir: Path, ticker: str) -> HoldingsSpec:
    """Read `<holdings_dir>/<TICKER>.json` and return a typed HoldingsSpec.

    The on-disk JSON may have other fields (tier_1_kpis, break_conditions, etc.)
    used by the LLM skill — we deliberately ignore those and only consume
    `break_rules` + `business_model_rules`. Missing arrays return empty (no rules
    to evaluate).

    Rules in `break_rules` are tagged tier=universal at load time; rules in
    `business_model_rules` are tagged tier=business_model. Any explicit `tier`
    in the on-disk JSON is overridden by the array the rule lives in — the JSON
    layout is the source of truth, not a redundant field.
    """
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"Holdings spec not found: {path}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    universal_raw = payload.get("break_rules", []) or []
    business_raw = payload.get("business_model_rules", []) or []
    soft_raw = payload.get("break_rules_soft", []) or []
    universal_rules = [_load_rule(r, RuleTier.UNIVERSAL) for r in universal_raw]
    business_rules = [_load_rule(r, RuleTier.BUSINESS_MODEL) for r in business_raw]
    soft_rules = load_soft_rules(soft_raw if isinstance(soft_raw, list) else [])
    return HoldingsSpec(
        ticker=payload["ticker"],
        thesis=payload["thesis"],
        break_rules=universal_rules,
        business_model_rules=business_rules,
        soft_rules=soft_rules,
    )


def _load_rule(raw: dict[str, object], tier: RuleTier) -> BreakRule:
    """Validate one rule dict, forcing the tier based on its parent array.

    `raw` may not be a dict at runtime (malformed JSON) — Pydantic raises a
    ValidationError there which propagates naturally. We strip any caller-set
    `tier` so the array placement always wins.
    """
    cleaned = {k: v for k, v in raw.items() if k != "tier"}
    rule = BreakRule.model_validate(cleaned)
    return rule.model_copy(update={"tier": tier})


def _fetch_kpi_history(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    n_periods: int,
) -> list[KpiObservation] | None:
    """Return up to `n_periods` most-recent kpi_facts observations for the rule's KPI.

    The rule's ``kpi_name`` is first resolved to the canonical
    ``kpi_definitions.name`` via the shared resolver, so a short break-rule label
    ("Monthly ARPAC") reaches the richest definition ("Monthly ARPAC (USD)")
    rather than exact-matching a sparse fragmented duplicate (the bug PR #195
    fixed for the §3 chart). No resolvable definition → no observations (caller
    treats that as OK / no-data), exactly as an exact-name miss did.

    Coexisting rows for one period (an LLM brief value plus the issuer's later
    IR-spreadsheet restatement) are deduped to the latest-ingested source per
    logical key — highest ``source_doc_id`` wins — matching the §3 chart loader
    and the §2 ledger so the consecutive-periods check sees one observation per
    period. Joined to kpi_definitions on name; ordered by period_end DESC so the
    caller sees newest-first.
    """
    resolved_name = resolve_kpi_definition_name(conn, ticker, kpi_name)
    if resolved_name is None:
        # Unresolvable: no kpi_facts definition matches this rule's KPI name
        # (e.g. a derived "... YoY change (bps)" series the pipeline hasn't
        # materialized, or a metric never extracted). Signal None so the caller
        # marks the rule UNRESOLVED rather than silently OK.
        return None
    cur = conn.execute(
        "SELECT kf.period_end, kf.value, kf.unit "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ? "
        "  AND kf.source_doc_id = ("
        "      SELECT MAX(k2.source_doc_id) FROM kpi_facts k2 "
        "      WHERE k2.ticker = kf.ticker "
        "        AND k2.kpi_definition_id = kf.kpi_definition_id "
        "        AND k2.period_end = kf.period_end "
        "        AND k2.fiscal_period_type = kf.fiscal_period_type) "
        "ORDER BY kf.period_end DESC LIMIT ?",
        (ticker.upper(), resolved_name, n_periods),
    )
    out: list[KpiObservation] = []
    for row in cur.fetchall():
        period = row["period_end"]
        if isinstance(period, str):
            period = datetime.fromisoformat(period)
        out.append(
            KpiObservation(
                period_end=period,
                value=Decimal(str(row["value"])),
                unit=Unit(row["unit"]),
            )
        )
    return out


def _compare(value: Decimal, comparator: Comparator, threshold: Decimal) -> bool:
    """Apply a comparator. No fallback — unsupported comparators raise upstream."""
    if comparator is Comparator.LT:
        return value < threshold
    if comparator is Comparator.LE:
        return value <= threshold
    if comparator is Comparator.GT:
        return value > threshold
    if comparator is Comparator.GE:
        return value >= threshold
    if comparator is Comparator.EQ:
        return value == threshold
    raise ValueError(f"Unhandled comparator: {comparator}")


def evaluate_rule(
    rule: BreakRule, observations: list[KpiObservation] | None
) -> RuleEvaluation:
    """Classify one rule given its observations.

    UNRESOLVED: ``observations is None`` — the rule's KPI didn't resolve to any
                kpi_facts definition, so the breaker can't be evaluated. Kept
                distinct from OK so an unevaluable breaker isn't read as passing.
    BREACH: have >= consecutive_periods observations and ALL match the rule.
    WARN:   any observation matches but not all consecutive_periods of them.
    OK:     no observation matches the rule (incl. resolved-but-no-rows-yet).
    """
    if observations is None:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.UNRESOLVED,
            observations=(),
            detail=(
                "unresolved: rule KPI has no matching kpi_facts definition — "
                "needs a derived or extracted series for this metric"
            ),
        )
    if not observations:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=(),
            detail="resolved, but no observations on file yet",
        )
    matches = [
        _compare(obs.value, rule.comparator, rule.threshold) for obs in observations
    ]
    matching_count = sum(matches)
    obs_tuple = tuple(observations)

    if matching_count == 0:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=obs_tuple,
            detail=f"none of last {len(observations)} obs match",
        )
    if matching_count >= rule.consecutive_periods and all(
        matches[: rule.consecutive_periods]
    ):
        latest = observations[0]
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.BREACH,
            observations=obs_tuple,
            detail=(
                f"breach: {rule.kpi_name}={latest.value} {rule.comparator.value} "
                f"{rule.threshold} for {rule.consecutive_periods} consecutive periods"
            ),
        )
    return RuleEvaluation(
        rule=rule,
        status=BreachStatus.WARN,
        observations=obs_tuple,
        detail=f"{matching_count}/{len(observations)} obs match (not yet consecutive)",
    )


_STATUS_RANK: dict[BreachStatus, int] = {
    # UNRESOLVED ranks with OK for the worst-rule rollup (a breaker we can't
    # evaluate must not raise a false BREACH); the §2 panel surfaces it per-rule
    # so the data gap stays visible.
    BreachStatus.UNRESOLVED: 0,
    BreachStatus.OK: 0,
    BreachStatus.WARN: 1,
    BreachStatus.BREACH: 2,
}


def _rollup_status(evaluations: list[RuleEvaluation]) -> BreachStatus:
    """Holding-level status from hard rules only = worst-rule status. Empty -> OK.

    UNRESOLVED never becomes the OVERALL verdict: it ranks with OK, but ``max``
    can surface an UNRESOLVED element on an otherwise all-clear holding. The
    overall is the worst EVALUABLE status (OK/WARN/BREACH); per-rule UNRESOLVED
    stays visible in the §2 panel so the data gap isn't hidden.
    """
    if not evaluations:
        return BreachStatus.OK
    worst = max(evaluations, key=lambda e: _STATUS_RANK[e.status]).status
    return BreachStatus.OK if worst is BreachStatus.UNRESOLVED else worst


def _rollup_with_soft(
    hard_evaluations: list[RuleEvaluation],
    soft_results: list[SoftRuleResult],
) -> BreachStatus:
    """Combined rollup: hard BREACH wins; else any soft YELLOW → WARN; else OK.

    Hard WARN (some-but-not-all consecutive periods matched) is preserved as
    WARN. Soft rules never escalate past WARN — that's a design contract:
    "the curve is bending" is a watch signal, not a thesis-broken signal.
    """
    hard_status = _rollup_status(hard_evaluations)
    if hard_status is BreachStatus.BREACH:
        return BreachStatus.BREACH
    any_soft_fired = any(r.status is SoftRuleStatus.YELLOW for r in soft_results)
    if any_soft_fired:
        return BreachStatus.WARN
    return hard_status


def evaluate_ticker_thesis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    holdings_dir: Path,
) -> ThesisVerdict:
    """End-to-end: load rules, fetch history, evaluate, roll up. No DB writes.

    Universal tripwires (`spec.break_rules`) are evaluated before per-ticker
    business-model rules (`spec.business_model_rules`); the §2 renderer relies
    on this order to keep catastrophic breakers visually first.
    """
    spec = load_holdings_spec(holdings_dir, ticker)
    evaluations: list[RuleEvaluation] = []
    for rule in (*spec.break_rules, *spec.business_model_rules):
        history = _fetch_kpi_history(
            conn, ticker, rule.kpi_name, rule.consecutive_periods
        )
        evaluations.append(evaluate_rule(rule, history))
    soft_results = evaluate_soft_rules(spec.ticker.upper(), spec.soft_rules, conn)
    overall = _rollup_with_soft(evaluations, soft_results)
    return ThesisVerdict(
        ticker=spec.ticker.upper(),
        thesis=spec.thesis,
        overall_status=overall,
        rule_evaluations=tuple(evaluations),
        evaluated_at=datetime.now(),
        soft_rule_results=tuple(soft_results),
    )


def _serialize_soft_rule_results(verdict: ThesisVerdict) -> str | None:
    """Render soft rule results as a stable JSON string for the soft column.

    Returns None when there are no soft results, so the column stays NULL for
    holdings without `break_rules_soft` and the renderer can distinguish
    "no rules" from "rules evaluated, all green".
    """
    if not verdict.soft_rule_results:
        return None
    payload = [
        {
            "rule_name": r.rule_name,
            "status": r.status.value,
            "evidence": r.evidence,
            "evaluated_at": r.evaluated_at.isoformat(),
            "details": r.details,
        }
        for r in verdict.soft_rule_results
    ]
    return json.dumps(payload, separators=(",", ":"), default=str)


def _serialize_rule_evaluations(verdict: ThesisVerdict) -> str:
    """Render rule_evaluations as a stable JSON string for thesis_evaluations history.

    `tier` is included so the §2 renderer can split universal tripwires from
    per-ticker business-model breakers without re-reading the holdings JSON.
    Older persisted rows without `tier` are treated as business_model at parse
    time (see report.sections.thesis._parse_evaluation).
    """
    payload = [
        {
            "rule_id": e.rule.rule_id,
            "kpi_name": e.rule.kpi_name,
            "comparator": e.rule.comparator.value,
            "threshold": str(e.rule.threshold),
            "consecutive_periods": e.rule.consecutive_periods,
            "tier": e.rule.tier.value,
            "status": e.status.value,
            "detail": e.detail,
            "narrative": e.rule.narrative,
            "observations": [
                {
                    "period_end": obs.period_end.isoformat(),
                    "value": str(obs.value),
                    "unit": obs.unit.value,
                }
                for obs in e.observations
            ],
        }
        for e in verdict.rule_evaluations
    ]
    return json.dumps(payload, separators=(",", ":"))


def persist_verdict(
    conn: sqlite3.Connection,
    verdict: ThesisVerdict,
    *,
    run_id: str | None = None,
) -> None:
    """Update thesis_state.breach_status (current snapshot) AND append to thesis_evaluations (history).

    `thesis_state` is mutable (current-state row per ticker). `thesis_evaluations`
    is append-only — every evaluation produces a new row keyed by evaluated_at.
    """
    # Upsert: a thesis_state row may not exist yet (e.g. ticker added via raw SQL
    # bypassing the track_company → onboard_ticker → seed flow). New rows get an
    # empty raw_json placeholder; the holdings JSON file remains the source of truth.
    conn.execute(
        "INSERT INTO thesis_state "
        "(ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
        "VALUES (?, ?, ?, ?, '{}', ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "    breach_status = excluded.breach_status, "
        "    last_updated  = excluded.last_updated",
        (
            verdict.ticker,
            verdict.thesis,
            verdict.overall_status.value,
            verdict.evaluated_at,
            verdict.evaluated_at,  # ingested_at = evaluated_at for newly-created rows
        ),
    )
    # soft_rule_results_json is added by migration 0053. Older DBs without the
    # column will be missed by Alembic's auto-discovery only if someone bypasses
    # the migration path entirely — we detect that case and fall back to the
    # pre-0053 INSERT so the evaluator stays runnable on a stale schema.
    has_soft_col = any(
        row[1] == "soft_rule_results_json"
        for row in conn.execute("PRAGMA table_info(thesis_evaluations)").fetchall()
    )
    if has_soft_col:
        conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, "
            "soft_rule_results_json, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                _serialize_soft_rule_results(verdict),
                run_id,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                run_id,
            ),
        )
    conn.commit()
