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

from models.facts import Unit
from models.kpis import BreachStatus


class Comparator(StrEnum):
    """Closed enum of supported numeric comparators."""

    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    EQ = "eq"


class BreakRule(BaseModel):
    """One deterministic break rule from a holdings JSON.

    `consecutive_periods` defaults to 1 (instantaneous breach). The most-recent
    `consecutive_periods` kpi_facts values for `kpi_name` are inspected; the rule
    fires if all of them satisfy `comparator threshold`.
    """

    rule_id: str = Field(min_length=1, max_length=80)
    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    threshold: Decimal
    unit: Unit
    consecutive_periods: int = Field(ge=1, le=12, default=1)
    narrative: str = Field(min_length=1, max_length=500)


class HoldingsSpec(BaseModel):
    """Subset of holdings JSON used by the evaluator."""

    ticker: str
    thesis: str
    break_rules: list[BreakRule] = Field(default_factory=list)


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
    """Holding-level rollup of all rule evaluations."""

    ticker: str
    thesis: str
    overall_status: BreachStatus
    rule_evaluations: tuple[RuleEvaluation, ...]
    evaluated_at: datetime


def load_holdings_spec(holdings_dir: Path, ticker: str) -> HoldingsSpec:
    """Read `<holdings_dir>/<TICKER>.json` and return a typed HoldingsSpec.

    The on-disk JSON may have other fields (tier_1_kpis, break_conditions, etc.)
    used by the LLM skill — we deliberately ignore those and only consume
    `break_rules`. Missing `break_rules` returns an empty list (no rules to evaluate).
    """
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"Holdings spec not found: {path}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    rules_raw = payload.get("break_rules", [])
    return HoldingsSpec(
        ticker=payload["ticker"],
        thesis=payload["thesis"],
        break_rules=[BreakRule.model_validate(r) for r in rules_raw],
    )


def _fetch_kpi_history(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    n_periods: int,
) -> list[KpiObservation]:
    """Return up to `n_periods` most-recent kpi_facts observations for (ticker, kpi_name).

    Joined to kpi_definitions on name; ordered by period_end DESC so caller sees
    newest-first.
    """
    cur = conn.execute(
        "SELECT kf.period_end, kf.value, kf.unit "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ? "
        "ORDER BY kf.period_end DESC LIMIT ?",
        (ticker.upper(), kpi_name, n_periods),
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
    rule: BreakRule, observations: list[KpiObservation]
) -> RuleEvaluation:
    """Classify one rule given its observations.

    BREACH: have >= consecutive_periods observations and ALL match the rule.
    WARN:   any observation matches but not all consecutive_periods of them.
    OK:     no observation matches the rule.
    Returns the same OK status when there are zero observations (no data yet).
    """
    if not observations:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=(),
            detail="no kpi_facts observations available",
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
    BreachStatus.OK: 0,
    BreachStatus.WARN: 1,
    BreachStatus.BREACH: 2,
}


def _rollup_status(evaluations: list[RuleEvaluation]) -> BreachStatus:
    """Holding-level status = worst-rule status. Empty -> OK."""
    if not evaluations:
        return BreachStatus.OK
    return max(evaluations, key=lambda e: _STATUS_RANK[e.status]).status


def evaluate_ticker_thesis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    holdings_dir: Path,
) -> ThesisVerdict:
    """End-to-end: load rules, fetch history, evaluate, roll up. No DB writes."""
    spec = load_holdings_spec(holdings_dir, ticker)
    evaluations: list[RuleEvaluation] = []
    for rule in spec.break_rules:
        history = _fetch_kpi_history(
            conn, ticker, rule.kpi_name, rule.consecutive_periods
        )
        evaluations.append(evaluate_rule(rule, history))
    overall = _rollup_status(evaluations)
    return ThesisVerdict(
        ticker=spec.ticker.upper(),
        thesis=spec.thesis,
        overall_status=overall,
        rule_evaluations=tuple(evaluations),
        evaluated_at=datetime.now(),
    )


def _serialize_rule_evaluations(verdict: ThesisVerdict) -> str:
    """Render rule_evaluations as a stable JSON string for thesis_evaluations history."""
    payload = [
        {
            "rule_id": e.rule.rule_id,
            "kpi_name": e.rule.kpi_name,
            "comparator": e.rule.comparator.value,
            "threshold": str(e.rule.threshold),
            "consecutive_periods": e.rule.consecutive_periods,
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
