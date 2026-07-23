"""Deterministic Incremental Dollar frontier (PRD
``docs/design/personal_investment_partner_prd.md`` §7.4, P0.3 deterministic
layer ONLY). No LLM call lives here — the governed selection over this
frontier is P0.4, out of scope for this module.

:func:`build_frontier` composes existing components without duplicating their
math: the eligible universe comes from :mod:`allocation.eligibility`, the
factor scores from :func:`allocation.model.build_next_dollar_model`, the
zone/cap sizing from :mod:`allocation.concentration`, and the diversifier leg
for evaluation names from the materialized candidate-fit cache
(``sharpe_delta_bps``). It identifies, at minimum: a highest-expected-return
plan, a best-diversifier plan, a balanced plan, and a retain-cash plan
(always present) — plus the ineligible names and why.

Cash-funded sizing (NOT ``allocation.what_if``'s pro-rata reallocation): for
dollars ``D`` deployed into a name already worth ``V`` on a book worth ``T``,

    resulting_weight_pct = (V + D) / (T + D_total) * 100

where ``D_total`` is the SUM of every dollar deployed in that one plan (a
single name for ``highest_return``/``best_diversifier``, up to two for
``balanced``) — new cash grows the book denominator; it never implies selling
existing positions (contrast ``what_if``'s ``(1-w)*book + w*candidate``
pro-rata blend, which is a different question: "what if I already funded this
from existing holdings").

Sizing never crosses the next Concentration Zone boundary (unless the name
already sits above it) and never breaches an AFFIRMED owner human-capital cap
(a ``status='proposed'`` fact never caps — only ``list_facts(status=
'affirmed')`` rows are read). Degrade-don't-crash: a down tracker or an empty
weights cache yields fewer plans and a `degraded` entry, never a raise; with
no eligible security the frontier is the retain-cash plan alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from allocation.candidate_fit import CandidateFit
from allocation.concentration import TRIM_ASSESSMENT_THRESHOLD_PCT, ZONE_BOUNDS, classify_zone
from allocation.eligibility import DecisionReadyAssessment, assess_universe
from allocation.model import NextDollarModel, build_next_dollar_model
from candidate_fit_cache import read_materialized_candidate_fit
from integrations.portfolio_tracker_client import fetch_live_portfolio
from owner_profile.models import HumanCapitalBucket
from owner_profile.store import list_facts
from portfolio_weights import read_materialized_weights, read_materialized_weights_as_of

__all__ = [
    "DeterministicFrontier",
    "FrontierPlan",
    "PlanAllocation",
    "build_frontier",
]

# A dollar-rounding tolerance well under a cent's meaningful precision, used
# only to decide whether a cap actually bound (vs. floating noise).
_EPS = 0.005

# (bucket_name, cap_pct, member_tickers) — the shape _human_capital_caps
# returns and _size_allocation consumes.
_BucketCap = tuple[str, float, tuple[str, ...]]


@dataclass(slots=True, frozen=True)
class PlanAllocation:
    """One name's slice of one frontier plan."""

    ticker: str
    dollars: float
    resulting_weight_pct: float
    zone: str | None


@dataclass(slots=True, frozen=True)
class FrontierPlan:
    """One point on the deterministic frontier — never opinionated about
    which plan is "preferred" (that judgment is P0.4's governed LLM); this is
    the menu, not the choice."""

    kind: Literal["balanced", "highest_return", "best_diversifier", "retain_cash"]
    allocations: tuple[PlanAllocation, ...]
    cash_retained_usd: float
    rationale_facts: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DeterministicFrontier:
    """The full §7.4 deterministic layer output for one cash amount — the
    exact input the P0.4 governed selection will read (and whose
    ``input_sha`` becomes that stage's ``llm_artifacts.input_sha256``)."""

    as_of: str
    cash_usd: float
    plans: tuple[FrontierPlan, ...]
    ineligible: tuple[tuple[str, tuple[str, ...]], ...]
    eligible_tickers: tuple[str, ...]
    input_sha: str
    source_freshness: dict[str, str]
    degraded: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #


def _sha(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ro_conn(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Owner-affirmed human-capital caps (proposed facts never cap)
# --------------------------------------------------------------------------- #


def _human_capital_caps(conn: sqlite3.Connection | None) -> list[_BucketCap]:
    """Every AFFIRMED ``human_capital.<bucket>`` capacity fact, as
    ``(bucket_name, cap_pct, member_tickers)``. Mirrors
    ``research.capacity_moments._human_capital_breaches``'s read pattern.
    ``status='proposed'`` facts are never read here — ``list_facts`` is
    called with ``status="affirmed"`` explicitly, so an inferred-but-
    unratified cap can never bind a plan."""
    if conn is None:
        return []
    try:
        rows = list_facts(conn, status="affirmed", category="capacity")
    except Exception:
        return []
    out: list[_BucketCap] = []
    for row in rows:
        if not row.key.startswith("human_capital."):
            continue
        try:
            bucket = HumanCapitalBucket.model_validate(row.value)
        except Exception:
            continue
        if bucket.members:
            out.append(
                (row.key.removeprefix("human_capital."), bucket.cap_pct, tuple(bucket.members))
            )
    return out


# --------------------------------------------------------------------------- #
# Cash-funded cap math
# --------------------------------------------------------------------------- #


def _max_dollars_for_cap(existing_value: float, total_value: float, cap_pct: float) -> float:
    """Max additional dollars addable to a position worth ``existing_value``
    (part of a book worth ``total_value``) so that, under the cash-funded
    formula ``(existing_value + D) / (total_value + D) <= cap_pct/100``, the
    cap is not crossed. ``D`` grows both the numerator and the denominator —
    the same growth-not-reallocation model the module docstring states.
    Returns 0.0 when the position is already at/above the cap (no room);
    ``math.inf`` when the cap can never bind (``cap_pct >= 100``)."""
    if cap_pct >= 100.0:
        return math.inf
    k = cap_pct / 100.0
    denom = 1.0 - k
    if denom <= 0.0:
        return math.inf
    d_max = (k * total_value - existing_value) / denom
    return max(d_max, 0.0)


def _resulting_weight_pct(v_i: float, d_i: float, total_value: float, d_total: float) -> float:
    denom = total_value + d_total
    if denom <= 0.0:
        return 0.0
    return (v_i + d_i) / denom * 100.0


def _zone_boundary_pct(weight_pct: float) -> float | None:
    """The upper bound of ``weight_pct``'s CURRENT Concentration Zone — the
    "next zone boundary" sizing must not cross (PRD §7.4). ``None`` when the
    name is already in the top (``exceptional``) zone, which has no upper
    bound — nothing to cap against from the zone leg."""
    for lo, hi, _zone in ZONE_BOUNDS:
        if weight_pct >= lo and (hi is None or weight_pct < hi):
            return hi
    return None


def _size_allocation(
    ticker: str,
    cash_available: float,
    *,
    weights: Mapping[str, float],
    total_value: float,
    buckets: Sequence[_BucketCap],
) -> tuple[float, list[str]]:
    """Dollars to allocate to ``ticker`` out of ``cash_available``: the min of
    the requested cash, the next-zone-boundary cap, and every affirmed
    human-capital bucket cap ``ticker`` is a member of. Never exceeds
    ``cash_available``; never goes negative. Returns
    ``(dollars, extra_rationale_facts)`` — the facts explain a binding cap,
    empty when the full requested cash was granted."""
    v_i = weights.get(ticker, 0.0) * total_value
    w0_pct = (v_i / total_value * 100.0) if total_value > 0.0 else 0.0
    caps: list[tuple[float, str]] = [(cash_available, "requested cash")]

    zone_hi = _zone_boundary_pct(w0_pct)
    if zone_hi is not None:
        zone_room = _max_dollars_for_cap(v_i, total_value, zone_hi)
        caps.append((zone_room, f"the next Concentration Zone boundary ({zone_hi:g}%)"))

    for name, cap_pct, members in buckets:
        if ticker not in members:
            continue
        vb = sum(weights.get(m, 0.0) for m in members) * total_value
        bucket_room = _max_dollars_for_cap(vb, total_value, cap_pct)
        caps.append((bucket_room, f"the affirmed human-capital cap '{name}' ({cap_pct:g}%)"))

    dollars, label = min(caps, key=lambda kv: kv[0])
    dollars = max(0.0, min(dollars, cash_available))
    facts: list[str] = []
    if label != "requested cash" and dollars < cash_available - _EPS:
        facts.append(
            f"{ticker} sizing capped at ${dollars:,.2f} by {label}, not the full "
            f"${cash_available:,.2f} requested"
        )
    return dollars, facts


def _trim_assessment_fact(ticker: str, resulting_pct: float, zone: str | None) -> str | None:
    if resulting_pct < TRIM_ASSESSMENT_THRESHOLD_PCT:
        return None
    return (
        f"{ticker} would reach {resulting_pct:.1f}% of the book (zone={zone or 'unknown'}) — "
        f"at/above the {TRIM_ASSESSMENT_THRESHOLD_PCT:g}% trim-assessment threshold"
    )


# --------------------------------------------------------------------------- #
# Plan construction
# --------------------------------------------------------------------------- #


def _build_single_name_plan(
    kind: Literal["highest_return", "best_diversifier"],
    ticker: str,
    headline: str,
    *,
    cash: float,
    weights: Mapping[str, float],
    total_value: float,
    buckets: Sequence[_BucketCap],
) -> FrontierPlan:
    dollars, cap_facts = _size_allocation(
        ticker, cash, weights=weights, total_value=total_value, buckets=buckets
    )
    v_i = weights.get(ticker, 0.0) * total_value
    resulting_pct = _resulting_weight_pct(v_i, dollars, total_value, dollars)
    zone = classify_zone(resulting_pct)
    zone_name = zone.zone if zone is not None else None
    facts = [headline, *cap_facts]
    trim_fact = _trim_assessment_fact(ticker, resulting_pct, zone_name)
    if trim_fact is not None:
        facts.append(trim_fact)
    allocations = (
        (
            PlanAllocation(
                ticker=ticker,
                dollars=round(dollars, 2),
                resulting_weight_pct=resulting_pct,
                zone=zone_name,
            ),
        )
        if dollars > 0.0
        else ()
    )
    retained = round(cash - dollars, 2) if dollars > 0.0 else round(cash, 2)
    return FrontierPlan(
        kind=kind, allocations=allocations, cash_retained_usd=retained, rationale_facts=tuple(facts)
    )


def _build_balanced_plan(
    candidates: Sequence[tuple[str, str]],
    *,
    cash: float,
    weights: Mapping[str, float],
    total_value: float,
    buckets: Sequence[_BucketCap],
) -> FrontierPlan:
    """``candidates`` is ``(ticker, headline)`` pairs, best-first. Funds the
    first, then the remainder (if any) into the second — "may split across
    top-2 names when the top name's zone cap binds" (PRD §7.4)."""
    facts: list[str] = []
    sized: dict[str, float] = {}
    remaining = cash
    for ticker, headline in candidates:
        if remaining <= _EPS:
            break
        dollars, cap_facts = _size_allocation(
            ticker, remaining, weights=weights, total_value=total_value, buckets=buckets
        )
        if dollars <= 0.0:
            facts.extend(cap_facts)
            continue
        sized[ticker] = dollars
        remaining = round(remaining - dollars, 2)
        facts.append(headline)
        facts.extend(cap_facts)

    d_total = sum(sized.values())
    allocations: list[PlanAllocation] = []
    for ticker, dollars in sized.items():
        v_i = weights.get(ticker, 0.0) * total_value
        resulting_pct = _resulting_weight_pct(v_i, dollars, total_value, d_total)
        zone = classify_zone(resulting_pct)
        zone_name = zone.zone if zone is not None else None
        trim_fact = _trim_assessment_fact(ticker, resulting_pct, zone_name)
        if trim_fact is not None:
            facts.append(trim_fact)
        allocations.append(
            PlanAllocation(
                ticker=ticker,
                dollars=round(dollars, 2),
                resulting_weight_pct=resulting_pct,
                zone=zone_name,
            )
        )
    if not facts:
        facts.append("no eligible name had room for the balanced plan — cash retained")
    retained = round(cash - d_total, 2)
    return FrontierPlan(
        kind="balanced",
        allocations=tuple(allocations),
        cash_retained_usd=retained,
        rationale_facts=tuple(facts),
    )


# --------------------------------------------------------------------------- #
# Candidate selection (factor readings -> best ticker(s))
# --------------------------------------------------------------------------- #


def _pick_highest_return(model: NextDollarModel | None) -> tuple[str, str] | None:
    if model is None:
        return None
    # The comprehension's `if r.ret is not None` narrows `r.ret` for the
    # element expression within the same comprehension (pyright supports
    # this), so the tuple below never touches a possibly-None `.ret`.
    scored = [(r.ret.z, r.ticker, r.ret.detail) for r in model.rows if r.ret is not None]
    if not scored:
        return None
    z, ticker, detail = max(scored, key=lambda t: (t[0], t[1]))
    headline = f"{ticker} ranks best on expected return: z={z:+.2f} ({detail})"
    return ticker, headline


def _pick_diversifier(
    model: NextDollarModel | None,
    fit_cache: Mapping[str, CandidateFit],
    *,
    eligible: Mapping[str, DecisionReadyAssessment],
) -> tuple[str, str] | None:
    candidates: list[tuple[float, str, str]] = []
    if model is not None:
        for r in model.rows:
            if r.div is not None:
                candidates.append(
                    (
                        r.div.z,
                        r.ticker,
                        f"{r.ticker} is the best diversifier among holdings: "
                        f"z={r.div.z:+.2f} ({r.div.detail})",
                    )
                )
    for ticker, assessment in eligible.items():
        if assessment.portfolio_fit_status != "scored":
            continue
        fit = fit_cache.get(ticker)
        if fit is None or fit.sharpe_delta_bps is None:
            continue
        # bps -> a roughly comparable scale to the held-name div z-score so
        # both legs can be ranked on one list; a documented approximation,
        # never claimed to be the same unit.
        candidates.append(
            (
                fit.sharpe_delta_bps / 100.0,
                ticker,
                f"{ticker} improves book Sharpe by {fit.sharpe_delta_bps:+.0f}bps at the "
                "default what-if weight",
            )
        )
    if not candidates:
        return None
    _score, ticker, headline = max(candidates, key=lambda c: (c[0], c[1]))
    return ticker, headline


def _pick_balanced(model: NextDollarModel | None, *, n: int = 2) -> list[tuple[str, str]]:
    if model is None:
        return []
    ranked = sorted(model.rows, key=lambda r: (-r.score, r.ticker))[:n]
    return [
        (r.ticker, f"{r.ticker} has the best blended next-dollar score: {r.score:+.2f}")
        for r in ranked
    ]


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def _ineligible_tuple(
    assessments: Mapping[str, DecisionReadyAssessment],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted((t, a.blocking_reasons) for t, a in assessments.items() if not a.eligible))


def build_frontier(
    db_path: Path, repo_root: Path, *, cash_usd: float, api_url: str | None = None
) -> DeterministicFrontier:
    """The §7.4 deterministic frontier for ``cash_usd`` of new cash. No LLM
    call — P0.4 governs the selection over this output. Never raises: every
    external read (tracker, weights cache, factor model, owner-profile
    facts) degrades to a `degraded` entry and a smaller frontier rather than
    an exception; with zero eligible names the frontier is the retain-cash
    plan alone."""
    now = datetime.now(UTC).replace(tzinfo=None)
    as_of = now.isoformat()
    cash = max(0.0, float(cash_usd)) if math.isfinite(cash_usd) else 0.0
    degraded: list[str] = []
    source_freshness: dict[str, str] = {}

    assessments = assess_universe(db_path, repo_root)
    eligible = {t: a for t, a in assessments.items() if a.eligible}
    ineligible = _ineligible_tuple(assessments)
    for a in assessments.values():
        source_freshness.update(a.source_freshness)
    eligible_tickers = tuple(sorted(eligible))

    def _frontier(
        plans: Sequence[FrontierPlan], *, extra_degraded: Sequence[str] = ()
    ) -> DeterministicFrontier:
        all_degraded = (*degraded, *extra_degraded)
        payload = {
            "as_of": as_of,
            "cash_usd": cash,
            "eligible_tickers": eligible_tickers,
            "ineligible": ineligible,
            "source_freshness": source_freshness,
            "degraded": all_degraded,
            "plans": [
                {
                    "kind": p.kind,
                    "cash_retained_usd": p.cash_retained_usd,
                    "allocations": [
                        {
                            "ticker": a.ticker,
                            "dollars": a.dollars,
                            "resulting_weight_pct": a.resulting_weight_pct,
                            "zone": a.zone,
                        }
                        for a in p.allocations
                    ],
                    "rationale_facts": p.rationale_facts,
                }
                for p in plans
            ],
        }
        return DeterministicFrontier(
            as_of=as_of,
            cash_usd=cash,
            plans=tuple(plans),
            ineligible=ineligible,
            eligible_tickers=eligible_tickers,
            input_sha=_sha(payload),
            source_freshness=source_freshness,
            degraded=tuple(all_degraded),
        )

    if not eligible:
        retain_only = FrontierPlan(
            kind="retain_cash",
            allocations=(),
            cash_retained_usd=cash,
            rationale_facts=("no eligible security clears the decision-ready evidence gate",),
        )
        return _frontier([retain_only])

    weights = read_materialized_weights(repo_root)
    if not weights:
        degraded.append(
            "portfolio weights cache is empty — current weights assumed zero for all names"
        )
    weights_as_of = read_materialized_weights_as_of(repo_root)
    if weights_as_of:
        source_freshness["weights"] = weights_as_of

    live = fetch_live_portfolio(api_url=api_url)
    total_value = (
        live.total_market_value if live.available and live.total_market_value > 0.0 else None
    )
    if total_value is None:
        degraded.append("tracker unavailable or book total is zero — resulting-weight math skipped")
        retain_only = FrontierPlan(
            kind="retain_cash",
            allocations=(),
            cash_retained_usd=cash,
            rationale_facts=(
                "book total value is unavailable (tracker unreachable) — cannot compute "
                "resulting weights for a funded plan",
            ),
        )
        return _frontier([retain_only])

    live_values = {t: weights.get(t, 0.0) * total_value for t in eligible_tickers}
    conn = _ro_conn(db_path)
    buckets = _human_capital_caps(conn)
    if conn is not None:
        conn.close()

    model = build_next_dollar_model(db_path, repo_root, list(eligible_tickers), live_values)
    sole = eligible_tickers[0] if len(eligible_tickers) == 1 else None
    if model is None:
        if sole is not None:
            degraded.append(
                f"insufficient factor data to rank {sole} — sized without relative comparison"
            )
        else:
            degraded.append(
                "no deterministic factor data (DCF/diversification/macro) for the eligible frontier"
            )

    fit_cache = read_materialized_candidate_fit(repo_root)

    plans: list[FrontierPlan] = []

    hr = _pick_highest_return(model)
    if hr is None and sole is not None:
        hr = (sole, f"{sole} is the only decision-ready name in the eligible frontier")
    if hr is not None:
        ticker, headline = hr
        plans.append(
            _build_single_name_plan(
                "highest_return",
                ticker,
                headline,
                cash=cash,
                weights=weights,
                total_value=total_value,
                buckets=buckets,
            )
        )

    div = _pick_diversifier(model, fit_cache, eligible=eligible)
    if div is not None:
        ticker, headline = div
        plans.append(
            _build_single_name_plan(
                "best_diversifier",
                ticker,
                headline,
                cash=cash,
                weights=weights,
                total_value=total_value,
                buckets=buckets,
            )
        )

    balanced_candidates = _pick_balanced(model)
    if not balanced_candidates and sole is not None:
        balanced_candidates = [(sole, f"{sole} is the only eligible name; balanced defaults to it")]
    if balanced_candidates:
        plans.append(
            _build_balanced_plan(
                balanced_candidates,
                cash=cash,
                weights=weights,
                total_value=total_value,
                buckets=buckets,
            )
        )

    retain_facts = ["cash always retains the option to wait for better evidence or pricing"]
    if not plans:
        retain_facts.append("no deterministic factor data available to size a funded plan")
    plans.append(
        FrontierPlan(
            kind="retain_cash",
            allocations=(),
            cash_retained_usd=cash,
            rationale_facts=tuple(retain_facts),
        )
    )

    return _frontier(plans)
