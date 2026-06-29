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

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field

from compute.kpi_resolver import (
    ANNUAL_FACT_PERIOD_TYPES,
    reporting_cadence_for,
    resolve_kpi_definition_name,
)
from compute.soft_rule_evaluator import (
    SoftRule,
    SoftRuleResult,
    SoftRuleStatus,
    evaluate_soft_rules,
    load_soft_rules,
)
from models.facts import Unit
from models.kpis import BreachStatus
from models.unit_convert import convert_unit
from provenance.overrides import KPI as OVERRIDE_KPI
from provenance.overrides import OverrideAction, active_scalar_override_map


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
    # 1000 to match the say_do narrative cap (src/compute/say_do.py): a detailed
    # recalibration rationale (e.g. NU's requirement-relative capital-cushion
    # narrative) runs past the original 500 and shouldn't fail rule loading.
    narrative: str = Field(min_length=1, max_length=1000)
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


def fetch_kpi_observations(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    n_periods: int,
) -> list[KpiObservation] | None:
    """Public alias of the history fetcher for external rule evaluators.

    The decision-condition trigger (triggers.decision_condition) evaluates
    falsifiable conditions through the SAME resolver/dedup/cadence path as
    break rules, so its comparisons can never drift from this module's —
    the same guarantee ``convert_unit`` gives the unit dimension (#317/#320).
    """
    return _fetch_kpi_history(conn, ticker, kpi_name, n_periods)


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

    **Cadence-aware**: when the resolved definition is annual-cadence (e.g. NU's
    Basel III capital adequacy ratio, disclosed once a year in the 20-F) only
    fiscal-year-end (``FY``) rows are read, so ``n_periods`` counts YEARS, not the
    last N rows of mixed cadence. An annual metric with genuine interim prints
    (NU CAR has a few) therefore evaluates "2 consecutive periods" as two annual
    values, never a year-end value paired with an interim one. Quarterly KPIs are
    unchanged (every period type is read, as before).

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
    # Annual-cadence breakers count YEARS: restrict to FY rows so the
    # consecutive-periods window is annual, never a mix of year-end + interim.
    period_filter = ""
    params: list[object] = [ticker.upper(), resolved_name]
    if reporting_cadence_for(conn, ticker, resolved_name) == "annual":
        placeholders = ",".join("?" * len(ANNUAL_FACT_PERIOD_TYPES))
        period_filter = f" AND kf.fiscal_period_type IN ({placeholders})"
        params.extend(ANNUAL_FACT_PERIOD_TYPES)
    params.append(n_periods)
    cur = conn.execute(
        "SELECT kf.period_end, kf.fiscal_period_type, kf.value, kf.unit "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ?" + period_filter + " "
        "  AND kf.source_doc_id = ("
        "      SELECT MAX(k2.source_doc_id) FROM kpi_facts k2 "
        "      WHERE k2.ticker = kf.ticker "
        "        AND k2.kpi_definition_id = kf.kpi_definition_id "
        "        AND k2.period_end = kf.period_end "
        "        AND k2.fiscal_period_type = kf.fiscal_period_type) "
        "ORDER BY kf.period_end DESC LIMIT ?",
        params,
    )
    # Company-doc overrides win over the FMP/LLM row for this (period, type):
    # a 'replace' substitutes the authoritative value, a 'drop' omits the period.
    ov_map = active_scalar_override_map(
        conn, ticker=ticker, fact_kind=OVERRIDE_KPI, fact_key=resolved_name
    )
    out: list[KpiObservation] = []
    for row in cur.fetchall():
        pe_str = str(row["period_end"])[:10]
        ftype = str(row["fiscal_period_type"])
        ov = ov_map.get((pe_str, ftype))
        if ov is not None and ov.action == OverrideAction.DROP.value:
            continue
        period = row["period_end"]
        if isinstance(period, str):
            period = datetime.fromisoformat(period)
        if ov is not None and ov.value is not None:
            value = Decimal(str(ov.value))
            unit = Unit(ov.unit) if ov.unit else Unit(row["unit"])
        else:
            value = Decimal(str(row["value"]))
            unit = Unit(row["unit"])
        out.append(KpiObservation(period_end=period, value=value, unit=unit))
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


# Unit reconciliation: a rule's threshold is in the rule's declared `unit`, but the
# kpi_facts that satisfy it may be stored in a different unit (the LLM extractor's
# per-call guess), so comparing the bare numbers misfires — `115_000_000 < 80` (raw
# dollars vs a `<80` millions threshold) could never breach. `evaluate_rule`
# reconciles every observation to the rule's unit via the shared `convert_unit`
# (imported above) before comparing, and persists the reconciled value so the §2
# brief panel reads in the threshold's unit. That same helper runs at persist time in
# `pipeline.kpi_persistence`, so a fact's stored unit and the unit it's compared in
# can't drift. Cross-family pairs (a money magnitude vs a proportion vs a raw count)
# have no valid conversion → the rule is surfaced UNRESOLVED, not mis-compared.


def evaluate_rule(rule: BreakRule, observations: list[KpiObservation] | None) -> RuleEvaluation:
    """Classify one rule given its observations.

    Observations are first reconciled to the rule's declared unit (see
    ``convert_unit``) so the comparison runs on the same dimension as the
    threshold and the persisted evidence reads in the threshold's unit.

    UNRESOLVED: ``observations is None`` — the rule's KPI didn't resolve to any
                kpi_facts definition; OR an observation's stored unit can't be
                converted to the rule's unit (cross-family mismatch). Either way
                the breaker can't be evaluated — kept distinct from OK so an
                unevaluable breaker isn't read as passing.
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
    # Reconcile each observation to the rule's declared unit so the comparison
    # — and the evidence we persist for the §2 brief panel — is dimensionally
    # consistent with the threshold. A cross-family unit (e.g. a money magnitude
    # where the rule expects a percentage) has no valid conversion: surface it as
    # UNRESOLVED rather than compare blindly, keeping the raw observations as
    # evidence so the data gap is visible.
    reconciled: list[KpiObservation] = []
    for obs in observations:
        converted = convert_unit(obs.value, obs.unit, rule.unit)
        if converted is None:
            return RuleEvaluation(
                rule=rule,
                status=BreachStatus.UNRESOLVED,
                observations=tuple(observations),
                detail=(
                    f"unresolved: observation unit {obs.unit.value!r} is not "
                    f"convertible to rule unit {rule.unit.value!r} — fix the "
                    f"rule's declared unit or the KPI's stored unit"
                ),
            )
        reconciled.append(
            KpiObservation(period_end=obs.period_end, value=converted, unit=rule.unit)
        )
    observations = reconciled
    matches = [_compare(obs.value, rule.comparator, rule.threshold) for obs in observations]
    matching_count = sum(matches)
    obs_tuple = tuple(observations)

    if matching_count == 0:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=obs_tuple,
            detail=f"none of last {len(observations)} obs match",
        )
    if matching_count >= rule.consecutive_periods and all(matches[: rule.consecutive_periods]):
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
        history = _fetch_kpi_history(conn, ticker, rule.kpi_name, rule.consecutive_periods)
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


def refresh_thesis_mirror(
    conn: sqlite3.Connection,
    ticker: str,
    holdings_dir: Path,
    *,
    ingested_at: datetime | None = None,
) -> bool:
    """Re-sync the thesis_state *content mirror* from the on-disk holdings JSON.

    `thesis_state.raw_json` + `thesis` are a cache of
    `micro_thesis/holdings/<TICKER>.json`, first seeded by migration 0008. The
    file is the source of truth; this function makes the cache match it again.

    Ownership split (deliberate, to avoid two writers fighting over a column):
      * the evaluator (`persist_verdict`) owns ``breach_status`` + ``last_updated``
        — those are *evaluation* state, derived from kpi_facts, not file content;
      * this function owns ``thesis`` + ``raw_json`` + ``ingested_at`` — the
        *file* content and when it was last mirrored.

    Reads the file, upserts the three content columns, and returns True if the
    stored content actually differed (i.e. the row was stale/absent) so callers
    can report what they fixed. Comparison is on the *parsed* payload, not the
    raw string, because `json.dumps` separators/key-order are not stable across
    writers. Does NOT commit — the caller owns the transaction.

    Raises FileNotFoundError when the holdings file is missing, leaving any
    existing row untouched so a deleted/renamed file never silently blanks the
    mirror.
    """
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"Holdings spec not found: {path}")
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    raw_json = json.dumps(payload)
    file_thesis = payload.get("thesis") if isinstance(payload.get("thesis"), str) else None

    prior = conn.execute(
        "SELECT thesis, raw_json FROM thesis_state WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    drifted = True
    if prior is not None:
        stored: object
        try:
            stored = json.loads(prior["raw_json"]) if prior["raw_json"] else {}
        except json.JSONDecodeError:
            stored = None
        drifted = stored != payload or prior["thesis"] != file_thesis

    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "    thesis      = excluded.thesis, "
        "    raw_json    = excluded.raw_json, "
        "    ingested_at = excluded.ingested_at",
        (ticker.upper(), file_thesis, raw_json, ingested_at or datetime.now()),
    )
    return drifted


def persist_verdict(
    conn: sqlite3.Connection,
    verdict: ThesisVerdict,
    *,
    run_id: str | None = None,
    holdings_dir: Path | None = None,
) -> None:
    """Update thesis_state.breach_status (current snapshot) AND append to thesis_evaluations (history).

    `thesis_state` is mutable (current-state row per ticker). `thesis_evaluations`
    is append-only — every evaluation produces a new row keyed by evaluated_at.

    When ``holdings_dir`` is supplied, the thesis_state *content mirror*
    (``thesis`` + ``raw_json``) is also re-synced from the on-disk holdings JSON
    via `refresh_thesis_mirror`, so an evaluation can never leave the mirror
    drifted from the file it was evaluated against. Omitting ``holdings_dir``
    preserves the historical behavior — breach_status/history only, raw_json
    left as-is — for callers that don't have the file at hand.
    """
    # Upsert: a thesis_state row may not exist yet (e.g. ticker added via raw SQL
    # bypassing the track_company → onboard_ticker → seed flow). New rows get an
    # empty raw_json placeholder; `holdings_dir` (when passed) re-mirrors it below.
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
    # Keep the content mirror in lockstep with the file this verdict was built
    # from. A missing file (ticker tracked, thesis not yet authored) leaves the
    # placeholder mirror untouched — breach_status/history are already written.
    if holdings_dir is not None:
        with contextlib.suppress(FileNotFoundError):
            refresh_thesis_mirror(conn, verdict.ticker, holdings_dir)
    conn.commit()
