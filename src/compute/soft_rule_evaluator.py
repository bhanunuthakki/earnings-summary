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
  trajectory      — linear-fit the last `lookback_prints` and project whether the
                     trend crosses `threshold` within `horizon_prints` future prints
                     (red-team PR2: `directives/monthly_red_team.md` Phase 1
                     "Trajectory WARN")

Every primitive above accepts a metric spec. Bare metric specs (`metric` on
series_below/above/decel, `kpi_name` on trajectory, numerator/denominator on
ratio_breach) default to the raw series (`derived: "level"`, implicit). Setting
`derived: "delta"` evaluates consecutive-print DIFFERENCES instead of levels —
e.g. NU's "net adds" is delta(Total customers). A cumulative series that is
non-monotonic or has a >1000x unit-scale jump between adjacent prints can't be
trusted to produce a real delta — the predicate reports UNRESOLVED with a
data-quality reason rather than compute a garbage number (red-team PR2 item 2).

Status is three-valued: GREEN (evaluated, didn't fire), YELLOW (evaluated,
fired), UNRESOLVED (couldn't be evaluated — insufficient data, a malformed
predicate, or a data-quality guard tripped). UNRESOLVED is never silently
collapsed into GREEN — Phase 1's "Prose-rule encoding" contract requires a
rule leg with no data to stay visible. RED is reserved for hard rules; soft
rules never escalate the holding past WARN, whether they fire or are
unresolved (see `thesis_evaluator._rollup_with_soft`).

Out of scope here: new predicate types beyond `trajectory`, LLM-authored
rules, evaluator-level caching. Add a primitive only when an actual holdings
JSON needs it.
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, NamedTuple, cast

from pydantic import BaseModel, Field

from compute.kpi_resolver import resolve_kpi_definition_name, semantic_series_identity_sql
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import KPI as OVERRIDE_KPI
from provenance.overrides import active_scalar_override_map

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status — soft rules emit GREEN (evaluated, not fired), YELLOW (evaluated,
# fired), or UNRESOLVED (couldn't be evaluated — never silently GREEN). RED is
# reserved for hard rules; mixing the two would let a single soft signal
# escalate to thesis-broken, which contradicts the design.
# ---------------------------------------------------------------------------


class SoftRuleStatus(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    UNRESOLVED = "unresolved"


# ---------------------------------------------------------------------------
# Predicate schema — JSON-shaped, validated by Pydantic.
# ---------------------------------------------------------------------------


class PredicateType(StrEnum):
    SERIES_DECEL = "series_decel"
    SERIES_BELOW = "series_below"
    SERIES_ABOVE = "series_above"
    RATIO_BREACH = "ratio_breach"
    COMPOUND = "compound"
    TRAJECTORY = "trajectory"


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
        resolved_name = resolve_kpi_definition_name(conn, ticker, metric)
        if resolved_name is None:
            return []
        if active_scalar_override_map(
            conn, ticker=ticker, fact_kind=OVERRIDE_KPI, fact_key=resolved_name
        ):
            log.warning(
                "soft_rule_kpi_unreviewed_override",
                extra={"ticker": ticker.upper(), "kpi_name": resolved_name},
            )
            return []
        fact_relation = canonical_fact_relation(conn, "kpi_facts")
        semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
        semantic_where += " AND " + semantic_series_identity_sql(
            conn, fact_relation=fact_relation.sql
        )
        if fact_relation.selection_mode == "legacy_pre_cutover":
            sql = (
                "WITH ranked AS ("
                "SELECT kf.id, kf.period_end, kf.fiscal_period_type, kf.value, "
                "kf.kpi_definition_id, "
                "ROW_NUMBER() OVER (PARTITION BY kf.kpi_definition_id, kf.period_end, "
                "kf.fiscal_period_type ORDER BY kf.id DESC) AS rn "
                f"FROM {fact_relation.sql} kf "
                "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
                "WHERE kf.ticker = ? AND kd.name = ? "
                "AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4')) "
                "SELECT kf.period_end, kf.value "
                "FROM ranked kf "
                f"{semantic_join} "
                "WHERE kf.rn = 1 AND " + semantic_where + " ORDER BY kf.period_end ASC"
            )
        else:
            sql = (
                "SELECT kf.period_end, kf.value "
                f"FROM {fact_relation.sql} kf "
                "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
                f"{semantic_join} "
                "WHERE kf.ticker = ? AND kd.name = ? AND "
                + semantic_where
                + " AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4') "
                "ORDER BY kf.period_end ASC"
            )
        try:
            rows = conn.execute(sql, (ticker.upper(), resolved_name)).fetchall()
        except sqlite3.Error as exc:
            log.warning({"event": "soft_rule_fetch_failed", "metric": metric, "error": str(exc)})
            return []
        out: list[tuple[datetime, float]] = []
        for row in rows:
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
# Derived-metric layer — `derived: "delta"` turns a level series into its
# consecutive-print differences (NU's "net adds" = delta(Total customers)).
# Guarded: a cumulative series that decreases, or jumps by >1000x between
# adjacent prints (a raw-count row landing inside a millions-scale series —
# exactly the NU "Total customers" def 641 corruption the red-team audit
# found), can't be trusted to produce a real delta. The guard scans the FULL
# fetched history (not just the caller's lookback window) so a single bad
# print anywhere in the series blocks every delta computed through it until
# the underlying data is fixed (execution/fix_kpi_series.py) — fail closed,
# never a garbage delta.
# ---------------------------------------------------------------------------

_UNIT_JUMP_RATIO = 1000.0


def _has_unit_jump(prev_v: float, cur_v: float, *, ratio_limit: float = _UNIT_JUMP_RATIO) -> bool:
    """True when `cur_v` is a >ratio_limit× jump from `prev_v` in either direction."""
    lo, hi = sorted((abs(prev_v), abs(cur_v)))
    if lo == 0:
        return hi > 0 and hi > ratio_limit
    return hi / lo > ratio_limit


def _fetch_series_with_derived(
    conn: sqlite3.Connection,
    ticker: str,
    metric: str,
    source: FactSource,
    derived: str,
) -> tuple[list[tuple[datetime, float]] | None, str | None]:
    """Return (series, None) on success, or (None, reason) when a data-quality
    guard blocks a `derived: "delta"` computation. `derived == "level"` (the
    default) always succeeds — it's the raw series, unguarded (existing
    predicates already tolerate a noisy level series; only a DELTA over a
    cumulative series risks manufacturing a nonsense number from a unit error).
    """
    raw = _fetch_series(conn, ticker, metric, source)
    if derived == "level":
        return raw, None
    if derived != "delta":
        raise ValueError(f"unsupported derived transform: {derived!r}")
    if len(raw) < 2:
        return [], None
    for (prev_p, prev_v), (cur_p, cur_v) in itertools.pairwise(raw):
        if cur_v < prev_v:
            return None, (
                f"non-monotonic {metric}: {cur_v:g} at {cur_p.date().isoformat()} < "
                f"{prev_v:g} at {prev_p.date().isoformat()} — cumulative series should "
                "not decrease; delta not computed"
            )
        if _has_unit_jump(prev_v, cur_v):
            return None, (
                f"unit discontinuity in {metric}: {prev_v:g} at {prev_p.date().isoformat()} "
                f"-> {cur_v:g} at {cur_p.date().isoformat()} (>{_UNIT_JUMP_RATIO:g}x jump) — "
                "likely a scale error; delta not computed"
            )
    deltas = [(cur_p, cur_v - prev_v) for (_, prev_v), (cur_p, cur_v) in itertools.pairwise(raw)]
    return deltas, None


class _MetricSpec(NamedTuple):
    name: str
    source: FactSource
    derived: str  # "level" | "delta"


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


def _source_from_param(
    params: dict[str, Any], *, default: FactSource = FactSource.FINANCIAL
) -> FactSource:
    """Default to `default` (FINANCIAL for most predicates) when unspecified.
    Holdings JSONs that want KPI facts opt in explicitly with `source: "kpi"`."""
    raw = params.get("source", default.value)
    try:
        return FactSource(str(raw))
    except ValueError as exc:
        raise ValueError(f"invalid source: {raw!r}") from exc


def _derived_from_param(params: dict[str, Any]) -> str:
    """`derived` opt-in: "level" (default, raw series) or "delta" (consecutive-
    print differences — see the derived-metric layer above)."""
    raw = str(params.get("derived", "level"))
    if raw not in ("level", "delta"):
        raise ValueError(f"invalid derived transform: {raw!r} (must be 'level' or 'delta')")
    return raw


@dataclass(frozen=True)
class _PredOutcome:
    """Internal: one predicate's verdict + the context for evidence rendering.

    `fired` is three-valued: True (fired → YELLOW), False (evaluated, didn't
    fire → GREEN), None (couldn't be evaluated — insufficient data or a
    data-quality guard tripped → UNRESOLVED, never silently GREEN).
    """

    fired: bool | None
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
    derived = _derived_from_param(params)
    periods = int(_param(params, "periods"))
    threshold_bps = float(_param(params, "threshold_bps"))
    if periods < 1:
        raise ValueError("series_decel periods must be >= 1")
    series, guard_reason = _fetch_series_with_derived(conn, ticker, metric, source, derived)
    if series is None:
        return _PredOutcome(
            fired=None,
            evidence_keys={"metric": metric, "derived": derived},
            description=f"unresolved: {guard_reason}",
        )
    if len(series) < periods + 5:
        return _PredOutcome(
            fired=None,
            evidence_keys={"metric": metric, "have": len(series), "need": periods + 5},
            description=(
                f"unresolved: insufficient data for series_decel({metric}): "
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
                fired=None,
                evidence_keys={"metric": metric, "zero_base_at": cur_period.isoformat()},
                description=f"unresolved: series_decel({metric}): zero base value blocks YoY",
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
    derived = _derived_from_param(params)
    threshold = float(_param(params, "threshold"))
    periods = int(_param(params, "periods"))
    if periods < 1:
        raise ValueError(f"series_{direction} periods must be >= 1")
    series, guard_reason = _fetch_series_with_derived(conn, ticker, metric, source, derived)
    if series is None:
        return _PredOutcome(
            fired=None,
            evidence_keys={"metric": metric, "derived": derived},
            description=f"unresolved: {guard_reason}",
        )
    if len(series) < periods:
        return _PredOutcome(
            fired=None,
            evidence_keys={"metric": metric, "have": len(series), "need": periods},
            description=(
                f"unresolved: insufficient data for series_{direction}({metric}): "
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

    num_series, num_guard = _fetch_series_with_derived(
        conn, ticker, num_spec.name, num_spec.source, num_spec.derived
    )
    den_series, den_guard = _fetch_series_with_derived(
        conn, ticker, den_spec.name, den_spec.source, den_spec.derived
    )
    if num_series is None or den_series is None:
        reason = num_guard if num_series is None else den_guard
        return _PredOutcome(
            fired=None,
            evidence_keys={"numerator": num_spec.name, "denominator": den_spec.name},
            description=f"unresolved: {reason}",
        )
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
            fired=None,
            evidence_keys={
                "numerator": num_spec.name,
                "denominator": den_spec.name,
                "have": len(paired),
                "need": periods,
            },
            description=(
                f"unresolved: insufficient data for ratio_breach({num_spec.name}/{den_spec.name}): "
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
            "numerator": num_spec.name,
            "denominator": den_spec.name,
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
            f"{num_spec.name} / {den_spec.name} {direction} {threshold * 100:.1f}% "
            f"for {periods} consecutive Q "
            f"(ratios: {', '.join(f'{r * 100:.1f}%' for _, r in window)})"
            + ("; FIRED" if fired else "; ok")
        ),
    )


def _metric_spec(raw: Any) -> _MetricSpec:
    """Coerce a numerator/denominator entry into a `_MetricSpec`.

    Accepts either a bare string (defaults to financial, `derived: "level"`)
    or a `{name, source, derived}` dict. Strict on shape — invalid entries
    raise so the rule fails loud and the analyst fixes the JSON rather than
    silently no-op'ing.
    """
    if isinstance(raw, str):
        return _MetricSpec(raw, FactSource.FINANCIAL, "level")
    if isinstance(raw, dict):
        spec_dict: dict[str, Any] = cast("dict[str, Any]", raw)
        name_val = spec_dict.get("name")
        if not isinstance(name_val, str) or not name_val:
            raise ValueError(f"metric spec missing `name`: {raw!r}")
        source_raw = spec_dict.get("source", FactSource.FINANCIAL.value)
        derived_raw = str(spec_dict.get("derived", "level"))
        if derived_raw not in ("level", "delta"):
            raise ValueError(f"invalid derived transform in metric spec: {derived_raw!r}")
        try:
            return _MetricSpec(name_val, FactSource(str(source_raw)), derived_raw)
        except ValueError as exc:
            raise ValueError(f"invalid source in metric spec: {source_raw!r}") from exc
    raise ValueError(f"metric spec must be str or dict, got {type(raw).__name__}: {raw!r}")


def _kleene_and(vals: list[bool | None]) -> bool | None:
    """Three-valued AND: any definite False wins; else any Unknown → Unknown;
    else True. Standard Kleene logic — lets a compound rule stay UNRESOLVED
    (not silently GREEN) when one leg can't be evaluated but no leg has
    definitively cleared the bar."""
    if any(v is False for v in vals):
        return False
    if any(v is None for v in vals):
        return None
    return True


def _kleene_or(vals: list[bool | None]) -> bool | None:
    """Three-valued OR: any definite True wins; else any Unknown → Unknown;
    else False."""
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def _eval_compound(conn: sqlite3.Connection, ticker: str, params: dict[str, Any]) -> _PredOutcome:
    """AND / OR over child predicates, three-valued (Kleene) so an unresolved
    child never gets silently absorbed into a plain green/fired verdict.

    Example: NU's "net adds <5M/Q AND Brazil flagship penetration declining
    QoQ" — the penetration leg has zero rows on file (def 639), so it's
    UNRESOLVED. Even if the net-adds leg fires, `AND(True, None) == None` —
    the compound renders UNRESOLVED (visible, amber), never a false GREEN
    that would hide the fact that one leg genuinely can't be checked yet.
    """
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
    child_fired = [c.fired for c in child_outcomes]
    fired = _kleene_and(child_fired) if op == "and" else _kleene_or(child_fired)
    fired_count = sum(1 for f in child_fired if f is True)
    unresolved_count = sum(1 for f in child_fired if f is None)
    if fired is None:
        outcome_word = "UNRESOLVED"
    elif fired:
        outcome_word = "FIRED"
    else:
        outcome_word = "ok"
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
            f"{fired_count} fired, {unresolved_count} unresolved, "
            f"{len(children) - fired_count - unresolved_count} clear; {outcome_word}"
        ),
    )


# ---------------------------------------------------------------------------
# trajectory — linear-fit the last `lookback_prints` and project whether the
# trend crosses `threshold` within `horizon_prints` future prints. This is
# the Phase 1 "Trajectory WARN" contract: MELI's NIMAL glided 22.7% → 17.8%
# YoY (-490bps/yr) toward its encoded 15%-floor rule but read plain OK,
# because the hard rule only fires once the floor is actually crossed. A
# trajectory soft rule surfaces the approach BEFORE the crossing.
# ---------------------------------------------------------------------------

_TRAJECTORY_COMPARATORS = ("lt", "le", "gt", "ge")


def _trajectory_hit(v: float, comparator: str, threshold: float) -> bool:
    if comparator == "lt":
        return v < threshold
    if comparator == "le":
        return v <= threshold
    if comparator == "gt":
        return v > threshold
    return v >= threshold  # "ge"


_COMPARATOR_SYMBOL = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _linear_fit(xs: list[int], ys: list[float]) -> tuple[float, float]:
    """OLS slope + intercept for y = slope*x + intercept. `xs` are always
    distinct consecutive integers (0..n-1) here so the denominator is never
    zero for n >= 2 — no degenerate branch needed."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0, mean_y
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _add_quarters(period: datetime, n: int) -> datetime:
    """Project `period` forward by `n` quarters (n may be 0)."""
    month0 = period.month - 1 + 3 * n
    year = period.year + month0 // 12
    month = month0 % 12 + 1
    day = min(period.day, 28)  # side-step month-length overflow (Q-end dates only)
    return period.replace(year=year, month=month, day=day)


def _quarter_label(period: datetime) -> str:
    q = (period.month - 1) // 3 + 1
    return f"Q{q}'{period.year % 100:02d}"


def _eval_trajectory(conn: sqlite3.Connection, ticker: str, params: dict[str, Any]) -> _PredOutcome:
    """Linear-fit the last `lookback_prints` of `kpi_name` and project forward.

    Fires (WARN) when the fitted trend crosses `threshold` within
    `horizon_prints` future prints. Insufficient data (fewer than
    `lookback_prints` observations) or a blocked `derived: "delta"` guard
    → UNRESOLVED, never a silent non-fire.
    """
    kpi_name = str(_param(params, "kpi_name"))
    source = _source_from_param(params, default=FactSource.KPI)
    derived = _derived_from_param(params)
    threshold = float(_param(params, "threshold"))
    comparator = str(_param(params, "comparator")).lower()
    if comparator not in _TRAJECTORY_COMPARATORS:
        raise ValueError(
            f"trajectory comparator must be one of {_TRAJECTORY_COMPARATORS}, got {comparator!r}"
        )
    lookback = int(params.get("lookback_prints", 4))
    if lookback < 3:
        raise ValueError("trajectory lookback_prints must be >= 3")
    horizon = int(params.get("horizon_prints", 2))
    if horizon < 1:
        raise ValueError("trajectory horizon_prints must be >= 1")

    series, guard_reason = _fetch_series_with_derived(conn, ticker, kpi_name, source, derived)
    if series is None:
        return _PredOutcome(
            fired=None,
            evidence_keys={"kpi_name": kpi_name, "derived": derived},
            description=f"unresolved: {guard_reason}",
        )
    if len(series) < lookback:
        return _PredOutcome(
            fired=None,
            evidence_keys={"kpi_name": kpi_name, "have": len(series), "need": lookback},
            description=(
                f"unresolved: insufficient data for trajectory({kpi_name}): "
                f"have {len(series)} prints, need {lookback}"
            ),
        )
    window = series[-lookback:]
    periods = [p for p, _ in window]
    ys = [v for _, v in window]
    xs = list(range(lookback))
    slope, intercept = _linear_fit(xs, ys)
    last_value = ys[-1]
    last_period = periods[-1]
    symbol = _COMPARATOR_SYMBOL[comparator]
    already_violating = _trajectory_hit(last_value, comparator, threshold)

    trip_h: int | None = None
    trip_value: float | None = None
    for h in range(1, horizon + 1):
        projected = intercept + slope * (lookback - 1 + h)
        if _trajectory_hit(projected, comparator, threshold):
            trip_h = h
            trip_value = projected
            break

    fired = trip_h is not None
    trip_period_label = _quarter_label(_add_quarters(last_period, trip_h)) if trip_h else None
    last_period_label = _quarter_label(last_period)

    if fired and already_violating and trip_h == 1:
        description = (
            f"{kpi_name} already {symbol}{threshold:g} as of {last_period_label} "
            f"(trend {slope:+.2f}/print over last {lookback}Q continues); trip {trip_period_label}"
        )
    elif fired:
        description = (
            f"{kpi_name} trajectory ({slope:+.2f}/print over last {lookback}Q, last={last_value:.2f} "
            f"at {last_period_label}) projects {symbol}{threshold:g} by {trip_period_label}"
        )
    else:
        description = (
            f"{kpi_name} trajectory ({slope:+.2f}/print over last {lookback}Q, last={last_value:.2f} "
            f"at {last_period_label}) not projected {symbol}{threshold:g} within {horizon} prints"
        )

    return _PredOutcome(
        fired=fired,
        evidence_keys={
            "kpi_name": kpi_name,
            "threshold": threshold,
            "comparator": comparator,
            "lookback_prints": lookback,
            "horizon_prints": horizon,
            "slope_per_period": round(slope, 4),
            "last_value": round(last_value, 4),
            "last_period": last_period_label,
            "trip_period": trip_period_label,
            "trip_h": trip_h,
            "trip_value": round(trip_value, 4) if trip_value is not None else None,
            "already_violating": already_violating,
        },
        description=description,
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
    PredicateType.TRAJECTORY: _eval_trajectory,
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


class _NoneAsMissing(dict[str, Any]):
    """A dict-for-`str.format` where a `None` value counts as missing.

    Several evidence keys are conditionally populated — `trajectory`'s
    `trip_period`/`trip_h`/`trip_value` are `None` when the rule doesn't fire.
    A bare `.format(**keys)` would happily stringify that as the literal text
    "None" (`str.format` doesn't raise on a present-but-None value), producing
    a nonsense sentence like "...projects <15% by None". Treating None as
    absent routes those templates through the same missing-key fallback that
    already covers a template referencing a key the predicate didn't expose.
    """

    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        if value is None:
            raise KeyError(key)
        return value


def _render_evidence(template: str | None, outcome: _PredOutcome) -> str:
    """Render the evidence string from the template + predicate context.

    Falls back to the predicate's `description` when the template is missing,
    references a key the predicate didn't expose, or references a key whose
    value is `None` (see `_NoneAsMissing`). The analyst seeing "fired but
    template borked" still gets a useful sentence — never an empty string, and
    never a literal "None" leaking into the brief.
    """
    if not template:
        return outcome.description
    try:
        return template.format_map(_NoneAsMissing(outcome.evidence_keys))
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
    logged and surfaced as UNRESOLVED — never GREEN — with the error in its
    evidence, so a malformed rule stays visibly broken rather than reading as
    "checked, all clear". Hard failures here must never break the thesis
    evaluator pipeline; other rules in the same call still evaluate.
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
                    status=SoftRuleStatus.UNRESOLVED,
                    evidence=f"soft rule '{rule.name}' did not evaluate: {exc}",
                    details={"error": str(exc)},
                    evaluated_at=now,
                )
            )
            continue
        if outcome.fired is None:
            status = SoftRuleStatus.UNRESOLVED
        elif outcome.fired:
            status = SoftRuleStatus.YELLOW
        else:
            status = SoftRuleStatus.GREEN
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
