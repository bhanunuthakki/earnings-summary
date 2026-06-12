"""Soft-rule evaluator: predicate-style YELLOW signals over financial / KPI series.

The hard `break_rules` evaluator (see thesis_evaluator.py) classifies a holding
as OK / WARN / BREACH against single-KPI threshold rules. That covers
catastrophic tripwires and per-ticker thresholded breakers, but misses the
softer "the curve is bending the wrong way" signals — e.g. "trailing-4Q
revenue growth decelerated by >200bps for 2 consecutive quarters". Those
signals shouldn't roll up to RED (the thesis isn't broken yet) but they
shouldn't be silent either.

This module consumes `break_rules_soft` from the holdings JSON, evaluates each
rule's predicate against financial_facts / kpi_facts, and returns per-rule
GREEN / YELLOW results. Hard rules still drive RED; soft rules drive YELLOW
in the rollup.

Predicate primitives (MVP):
  series_decel    — YoY-growth deceleration ≥ threshold_bps for N consecutive Q
  series_below    — metric below threshold for N consecutive Q
  series_above    — metric above threshold for N consecutive Q
  ratio_breach    — numerator/denominator in `direction` vs threshold for N Q
  compound        — AND/OR over child predicates

Out of scope here: new predicate types, LLM-authored rules, evaluator-level
caching. Add a primitive only when an actual holdings JSON needs it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status — soft rules emit only GREEN (not fired) or YELLOW (fired). RED is
# reserved for hard rules; mixing the two would let a single soft signal
# escalate to thesis-broken, which contradicts the design.
# ---------------------------------------------------------------------------


class SoftRuleStatus(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"


# ---------------------------------------------------------------------------
# Predicate schema — JSON-shaped, validated by Pydantic.
# ---------------------------------------------------------------------------


class PredicateType(StrEnum):
    SERIES_DECEL = "series_decel"
    SERIES_BELOW = "series_below"
    SERIES_ABOVE = "series_above"
    RATIO_BREACH = "ratio_breach"
    COMPOUND = "compound"


class FactSource(StrEnum):
    """Where to find the metric. `financial` → financial_facts.line_item;
    `kpi` → kpi_facts JOIN kpi_definitions.name."""

    FINANCIAL = "financial"
    KPI = "kpi"


class SoftRulePredicate(BaseModel):
    """JSON-shaped predicate. `type` discriminates `params`.

    Validation of params-per-type happens at evaluation time, not load time,
    so a malformed predicate fails loudly when it runs (with the rule name
    in the log) rather than blocking the whole holdings file from loading.
    """

    type: PredicateType
    # Free-form param bag — each predicate type reads the keys it needs. Kept
    # as `dict[str, Any]` so child predicates inside `compound` can themselves
    # be predicate-shaped without a recursive type.
    params: dict[str, Any] = Field(default_factory=dict)


class SoftRule(BaseModel):
    """One soft rule from the holdings JSON `break_rules_soft` array."""

    name: str = Field(min_length=1, max_length=120)
    predicate: SoftRulePredicate
    # Optional human-readable evidence template. May use {key} placeholders
    # filled from the predicate's evidence dict (see SoftRuleResult.evidence).
    # When omitted or unfillable, the evaluator falls back to a generic
    # description of what fired.
    evidence_template: str | None = None


@dataclass(frozen=True)
class SoftRuleResult:
    """One evaluated soft rule.

    `evidence` is the rendered string (template + predicate context). `details`
    is the structured payload used to re-render the result in the UI without
    re-fetching data and to debug why a rule did or did not fire.
    """

    rule_name: str
    status: SoftRuleStatus
    evidence: str
    details: dict[str, Any]
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# Loader — parse the on-disk `break_rules_soft` array. Kept here (not in
# thesis_evaluator.load_holdings_spec) so the soft-rule schema can evolve
# independently of the hard-rule loader, and so callers that only want soft
# rules don't pay the cost of loading the full HoldingsSpec.
# ---------------------------------------------------------------------------


def load_soft_rules(raw: list[Any] | None) -> list[SoftRule]:
    """Parse the JSON list into typed SoftRule objects.

    None / empty → []. Malformed entries are logged and skipped — one bad rule
    must never block the rest from running, because the evaluator runs in the
    monthly refresh path and silence is preferable to crashing the pipeline.
    """
    if not raw:
        return []
    out: list[SoftRule] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            log.warning({"event": "soft_rule_not_a_dict", "index": i})
            continue
        try:
            out.append(SoftRule.model_validate(entry))
        except Exception as exc:  # pydantic ValidationError + anything else
            log.warning({"event": "soft_rule_validation_failed", "index": i, "error": str(exc)})
    return out


# ---------------------------------------------------------------------------
# Series fetchers — mirror thesis_evaluator._fetch_kpi_history but return the
# value sequence ordered chronologically (oldest → newest) since the predicate
# logic needs both directions (YoY needs the past; "last N" needs the recent).
# ---------------------------------------------------------------------------


def _fetch_series(
    conn: sqlite3.Connection,
    ticker: str,
    metric: str,
    source: FactSource,
) -> list[tuple[datetime, float]]:
    """Return (period_end, value) tuples, chronological, for one metric.

    Restricted to standalone quarterly periods (Q1..Q4) to match the time-
    series-layer convention — FY/TTM aggregates double-count and would distort
    any series_decel / series_below check that assumes quarterly cadence.

    Best-effort: if the underlying table is missing, returns []. The rule
    will then evaluate to GREEN (insufficient data); a caller wanting a
    harder signal should add a coverage check upstream.
    """
    if source is FactSource.FINANCIAL:
        sql = (
            "SELECT period_end, value FROM financial_facts "
            "WHERE ticker = ? AND line_item = ? "
            "AND fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
            "ORDER BY period_end ASC"
        )
        params: tuple[Any, ...] = (ticker.upper(), metric)
    else:
        sql = (
            "SELECT kf.period_end, kf.value FROM kpi_facts kf "
            "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
            "WHERE kf.ticker = ? AND kd.name = ? "
            "AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
            "ORDER BY kf.period_end ASC"
        )
        params = (ticker.upper(), metric)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "soft_rule_fetch_failed", "metric": metric, "error": str(exc)})
        return []
    out: list[tuple[datetime, float]] = []
    for row in rows:
        # sqlite3.Row supports both index and key access; treat positionally
        # to avoid coupling to row_factory configuration on the caller's conn.
        period_raw = row[0]
        if isinstance(period_raw, str):
            try:
                period = datetime.fromisoformat(period_raw)
            except ValueError:
                try:
                    period = datetime.strptime(period_raw[:10], "%Y-%m-%d")
                except ValueError:
                    continue
        elif isinstance(period_raw, datetime):
            period = period_raw
        else:
            continue
        try:
            value = float(row[1])
        except (TypeError, ValueError):
            continue
        out.append((period, value))
    return out


# ---------------------------------------------------------------------------
# Predicate implementations
# ---------------------------------------------------------------------------


def _param(params: dict[str, Any], key: str, *, required: bool = True) -> Any:
    """Pull a param value; raise ValueError when required and missing."""
    if key in params:
        return params[key]
    if required:
        raise ValueError(f"missing required predicate param: {key}")
    return None


def _source_from_param(params: dict[str, Any]) -> FactSource:
    """Default to FINANCIAL when unspecified — the most common case for the
    canonical metrics (revenue, fcf, operating_cash_flow). Holdings JSONs that
    want KPI facts opt in explicitly with `source: "kpi"`."""
    raw = params.get("source", FactSource.FINANCIAL.value)
    try:
        return FactSource(str(raw))
    except ValueError as exc:
        raise ValueError(f"invalid source: {raw!r}") from exc


@dataclass(frozen=True)
class _PredOutcome:
    """Internal: one predicate's verdict + the context for evidence rendering."""

    fired: bool
    evidence_keys: dict[str, Any]
    description: str  # fallback used when evidence_template is missing/unfillable


def _eval_series_decel(
    conn: sqlite3.Connection, ticker: str, params: dict[str, Any]
) -> _PredOutcome:
    """YoY growth deceleration ≥ threshold_bps for `periods` consecutive quarters.

    Deceleration at Q is `YoY(Q-1) - YoY(Q)` in bps (positive = slowing). The
    predicate fires when every one of the last `periods` quarters shows a
    deceleration ≥ threshold_bps.

    Needs at minimum `periods + 5` value observations (1 each for YoY at the
    most recent `periods+1` quarters, each YoY needing a value 4Q prior).
    """
    metric = str(_param(params, "metric"))
    source = _source_from_param(params)
    periods = int(_param(params, "periods"))
    threshold_bps = float(_param(params, "threshold_bps"))
    if periods < 1:
        raise ValueError("series_decel periods must be >= 1")
    series = _fetch_series(conn, ticker, metric, source)
    if len(series) < periods + 5:
        return _PredOutcome(
            fired=False,
            evidence_keys={"metric": metric, "have": len(series), "need": periods + 5},
            description=(
                f"insufficient data for series_decel({metric}): "
                f"have {len(series)} quarters, need {periods + 5}"
            ),
        )
    # YoY at index i requires value at i-4. Compute YoY for the last
    # periods+1 indices so we can take periods consecutive deltas.
    yoy: list[tuple[datetime, float]] = []
    for idx in range(len(series) - periods - 1, len(series)):
        cur_period, cur_val = series[idx]
        _, base_val = series[idx - 4]
        if base_val == 0:
            return _PredOutcome(
                fired=False,
                evidence_keys={"metric": metric, "zero_base_at": cur_period.isoformat()},
                description=f"series_decel({metric}): zero base value blocks YoY",
            )
        growth_pct = (cur_val / base_val - 1.0) * 100.0
        yoy.append((cur_period, growth_pct))
    # Deltas: positive = decel (growth getting smaller).
    decels: list[tuple[datetime, float]] = []
    for i in range(1, len(yoy)):
        _, prev_yoy = yoy[i - 1]
        cur_period, cur_yoy = yoy[i]
        decel_bps = (prev_yoy - cur_yoy) * 100.0  # pct → bps
        decels.append((cur_period, decel_bps))
    fired = all(d_bps >= threshold_bps for _, d_bps in decels)
    last_decel = decels[-1] if decels else (None, 0.0)
    second_decel = decels[-2] if len(decels) >= 2 else (None, 0.0)
    return _PredOutcome(
        fired=fired,
        evidence_keys={
            "metric": metric,
            "periods": periods,
            "threshold_bps": threshold_bps,
            "first_bps": round(decels[0][1], 0) if decels else 0,
            "second_bps": round(decels[-1][1], 0) if decels else 0,
            "last_yoy_pct": round(yoy[-1][1], 2),
            "prior_yoy_pct": round(yoy[-2][1], 2) if len(yoy) >= 2 else None,
            "decel_series_bps": [round(d, 0) for _, d in decels],
            "last_period": (last_decel[0].isoformat() if last_decel[0] else None),
        },
        description=(
            f"{metric} YoY decelerated by "
            f"{', '.join(f'{round(d)}bps' for _, d in decels)} "
            f"over last {periods} quarters (threshold {round(threshold_bps)}bps); "
            f"recent YoY {yoy[-1][1]:.1f}%"
            + (f" vs prior {yoy[-2][1]:.1f}%" if len(yoy) >= 2 else "")
            + ("; FIRED" if fired else "; ok")
            + (f". prev_decel={round(second_decel[1])}bps" if second_decel[0] is not None else "")
        ),
    )


def _eval_series_threshold(
    conn: sqlite3.Connection,
    ticker: str,
    params: dict[str, Any],
    *,
    direction: Literal["below", "above"],
) -> _PredOutcome:
    """series_below / series_above — all of the last N quarters on one side of threshold."""
    metric = str(_param(params, "metric"))
    source = _source_from_param(params)
    threshold = float(_param(params, "threshold"))
    periods = int(_param(params, "periods"))
    if periods < 1:
        raise ValueError(f"series_{direction} periods must be >= 1")
    series = _fetch_series(conn, ticker, metric, source)
    if len(series) < periods:
        return _PredOutcome(
            fired=False,
            evidence_keys={"metric": metric, "have": len(series), "need": periods},
            description=(
                f"insufficient data for series_{direction}({metric}): "
                f"have {len(series)} quarters, need {periods}"
            ),
        )
    window = series[-periods:]

    def hit(v: float) -> bool:
        return v < threshold if direction == "below" else v > threshold

    fired = all(hit(v) for _, v in window)
    return _PredOutcome(
        fired=fired,
        evidence_keys={
            "metric": metric,
            "threshold": threshold,
            "periods": periods,
            "direction": direction,
            "last_value": round(window[-1][1], 4),
            "values": [round(v, 4) for _, v in window],
            "last_period": window[-1][0].isoformat(),
        },
        description=(
            f"{metric} {direction} {threshold:g} for {periods} consecutive Q "
            f"(values: {', '.join(f'{v:g}' for _, v in window)})" + ("; FIRED" if fired else "; ok")
        ),
    )


def _eval_ratio_breach(
    conn: sqlite3.Connection, ticker: str, params: dict[str, Any]
) -> _PredOutcome:
    """Ratio of two series vs a threshold for N consecutive quarters.

    `threshold` is a fraction (0.15 for 15%), to match the level the analyst
    writes when expressing "fcf/revenue < 15% for 2Q" — keeps the holdings
    JSON free of unit-conversion footnotes.

    `numerator` and `denominator` are sibling metric specs:
      `{name: "free_cash_flow", source: "financial"}`
    Defaulting source=financial when omitted.
    """
    num_spec = _metric_spec(_param(params, "numerator"))
    den_spec = _metric_spec(_param(params, "denominator"))
    threshold = float(_param(params, "threshold"))
    direction: Literal["below", "above"] = str(_param(params, "direction"))  # type: ignore[assignment]
    if direction not in ("below", "above"):
        raise ValueError(f"ratio_breach direction must be 'below' or 'above', got {direction!r}")
    periods = int(params.get("periods", 1))
    if periods < 1:
        raise ValueError("ratio_breach periods must be >= 1")

    num_series = _fetch_series(conn, ticker, num_spec[0], num_spec[1])
    den_series = _fetch_series(conn, ticker, den_spec[0], den_spec[1])
    num_map = {pe: v for pe, v in num_series}
    den_map = {pe: v for pe, v in den_series}
    # Inner-join on period_end so we only compute the ratio where both sides
    # have a value — avoids a phantom "fired" when one side has stale data.
    paired = [
        (pe, num_map[pe] / den_map[pe])
        for pe in sorted(num_map.keys() & den_map.keys())
        if den_map[pe] != 0
    ]
    if len(paired) < periods:
        return _PredOutcome(
            fired=False,
            evidence_keys={
                "numerator": num_spec[0],
                "denominator": den_spec[0],
                "have": len(paired),
                "need": periods,
            },
            description=(
                f"insufficient data for ratio_breach({num_spec[0]}/{den_spec[0]}): "
                f"have {len(paired)} paired quarters, need {periods}"
            ),
        )
    window = paired[-periods:]

    def hit(r: float) -> bool:
        return r < threshold if direction == "below" else r > threshold

    fired = all(hit(r) for _, r in window)
    return _PredOutcome(
        fired=fired,
        evidence_keys={
            "numerator": num_spec[0],
            "denominator": den_spec[0],
            "threshold": threshold,
            "threshold_pct": round(threshold * 100.0, 2),
            "direction": direction,
            "periods": periods,
            "last_ratio": round(window[-1][1], 4),
            "last_ratio_pct": round(window[-1][1] * 100.0, 2),
            "ratios_pct": [round(r * 100.0, 2) for _, r in window],
            "last_period": window[-1][0].isoformat(),
        },
        description=(
            f"{num_spec[0]} / {den_spec[0]} {direction} {threshold * 100:.1f}% "
            f"for {periods} consecutive Q "
            f"(ratios: {', '.join(f'{r * 100:.1f}%' for _, r in window)})"
            + ("; FIRED" if fired else "; ok")
        ),
    )


def _metric_spec(raw: Any) -> tuple[str, FactSource]:
    """Coerce a numerator/denominator entry into (name, source).

    Accepts either a bare string (defaults to financial) or a {name, source}
    dict. Strict on shape — invalid entries raise so the rule fails loud and
    the analyst fixes the JSON rather than silently no-op'ing.
    """
    if isinstance(raw, str):
        return (raw, FactSource.FINANCIAL)
    if isinstance(raw, dict):
        spec_dict: dict[str, Any] = cast("dict[str, Any]", raw)
        name_val = spec_dict.get("name")
        if not isinstance(name_val, str) or not name_val:
            raise ValueError(f"metric spec missing `name`: {raw!r}")
        source_raw = spec_dict.get("source", FactSource.FINANCIAL.value)
        try:
            return (name_val, FactSource(str(source_raw)))
        except ValueError as exc:
            raise ValueError(f"invalid source in metric spec: {source_raw!r}") from exc
    raise ValueError(f"metric spec must be str or dict, got {type(raw).__name__}: {raw!r}")


def _eval_compound(conn: sqlite3.Connection, ticker: str, params: dict[str, Any]) -> _PredOutcome:
    """AND / OR over child predicates."""
    op = str(_param(params, "op")).lower()
    if op not in ("and", "or"):
        raise ValueError(f"compound op must be 'and' or 'or', got {op!r}")
    children_raw = _param(params, "predicates")
    if not isinstance(children_raw, list) or not children_raw:
        raise ValueError("compound predicates must be a non-empty list")
    children: list[SoftRulePredicate] = []
    for entry in cast("list[Any]", children_raw):
        children.append(SoftRulePredicate.model_validate(entry))
    child_outcomes = [_evaluate_predicate(conn, ticker, c) for c in children]
    if op == "and":
        fired = all(c.fired for c in child_outcomes)
    else:
        fired = any(c.fired for c in child_outcomes)
    return _PredOutcome(
        fired=fired,
        evidence_keys={
            "op": op,
            "children": [
                {
                    "type": c_def.type.value,
                    "fired": c_out.fired,
                    "description": c_out.description,
                }
                for c_def, c_out in zip(children, child_outcomes, strict=True)
            ],
        },
        description=(
            f"compound {op.upper()} over {len(children)} children: "
            f"{sum(c.fired for c in child_outcomes)}/{len(children)} fired"
            + ("; FIRED" if fired else "; ok")
        ),
    )


# Predicate handler type — closes over `direction` for series_below/above so the
# two share one implementation without a runtime branch on each call.
_PredHandler = Callable[[sqlite3.Connection, str, dict[str, Any]], _PredOutcome]


def _series_below_handler(
    conn: sqlite3.Connection, ticker: str, params: dict[str, Any]
) -> _PredOutcome:
    return _eval_series_threshold(conn, ticker, params, direction="below")


def _series_above_handler(
    conn: sqlite3.Connection, ticker: str, params: dict[str, Any]
) -> _PredOutcome:
    return _eval_series_threshold(conn, ticker, params, direction="above")


_PREDICATE_DISPATCH: dict[PredicateType, _PredHandler] = {
    PredicateType.SERIES_DECEL: _eval_series_decel,
    PredicateType.SERIES_BELOW: _series_below_handler,
    PredicateType.SERIES_ABOVE: _series_above_handler,
    PredicateType.RATIO_BREACH: _eval_ratio_breach,
    PredicateType.COMPOUND: _eval_compound,
}


def _evaluate_predicate(
    conn: sqlite3.Connection, ticker: str, pred: SoftRulePredicate
) -> _PredOutcome:
    """Dispatch one predicate to its handler. Closed-set — unknown types raise."""
    fn = _PREDICATE_DISPATCH.get(pred.type)
    if fn is None:
        raise ValueError(f"unsupported predicate type: {pred.type}")
    return fn(conn, ticker, pred.params)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def _render_evidence(template: str | None, outcome: _PredOutcome) -> str:
    """Render the evidence string from the template + predicate context.

    Falls back to the predicate's `description` when the template is missing
    or references a key the predicate didn't expose. The analyst seeing
    "fired but template borked" still gets a useful sentence — never an empty
    string in the brief.
    """
    if not template:
        return outcome.description
    try:
        return template.format(**outcome.evidence_keys)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning(
            {"event": "soft_rule_template_render_failed", "error": str(exc), "template": template}
        )
        return outcome.description


def evaluate_soft_rules(
    ticker: str,
    rules: list[SoftRule],
    conn: sqlite3.Connection,
) -> list[SoftRuleResult]:
    """Evaluate every soft rule for `ticker` and return per-rule results.

    Errors in a single rule (invalid predicate, missing required param) are
    logged and skipped — the rule is treated as GREEN with the error in its
    evidence so the analyst sees something rather than silently losing the
    rule. Hard failures here must never break the thesis evaluator pipeline.
    """
    now = datetime.now()
    out: list[SoftRuleResult] = []
    for rule in rules:
        try:
            outcome = _evaluate_predicate(conn, ticker, rule.predicate)
        except Exception as exc:
            log.warning(
                {
                    "event": "soft_rule_eval_failed",
                    "rule": rule.name,
                    "error": str(exc),
                }
            )
            out.append(
                SoftRuleResult(
                    rule_name=rule.name,
                    status=SoftRuleStatus.GREEN,
                    evidence=f"soft rule '{rule.name}' did not evaluate: {exc}",
                    details={"error": str(exc)},
                    evaluated_at=now,
                )
            )
            continue
        status = SoftRuleStatus.YELLOW if outcome.fired else SoftRuleStatus.GREEN
        evidence = _render_evidence(rule.evidence_template, outcome)
        out.append(
            SoftRuleResult(
                rule_name=rule.name,
                status=status,
                evidence=evidence,
                details={
                    "predicate_type": rule.predicate.type.value,
                    "fired": outcome.fired,
                    **outcome.evidence_keys,
                },
                evaluated_at=now,
            )
        )
    return out
