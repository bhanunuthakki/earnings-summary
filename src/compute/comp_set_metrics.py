"""Bottoms-up comparable-set aggregate math (docs/design/comparable_sets_bottoms_up.md
section 5). Phase 1 scope: ``scope_type='comparable_set'`` rows only (industry/sector
pool-wide scopes are Phase 2).

Reads raw per-member quarterly statements from the LOCAL FMP caches under
``data/historical/fmp/`` -- exactly the ``screens.py::_sum_last4`` all-or-nothing
coverage gate, applied to ``netIncome``/``ebitda`` instead of the per-quarter
fraction ratios screens.py TTM-izes. Deliberately does NOT read
``key_metrics_ttm``/``ratios_ttm``'s pre-computed PE/EV-EBITDA fields (section 5.3):
a vendor ratio can't be un-computed to recover the raw earnings figure the
cap-weighted aggregate needs to sum, and different members' vendor TTM cutoffs can
silently drift apart, whereas summing our own last-4-quarters keeps every member's
"TTM" defined identically.

Two statistics per metric, always both, never collapsed to one (section 5.1):
  * ``median``     — the "typical name" number, robust to a single mega-cap.
  * ``aggregate``   — cap-weighted: ``sum(numerator_i) / sum(denominator_i)``, the
    "if you owned the whole set" number (matches the S&P bottom-up index-multiple
    construction).

Negative-earnings handling (section 5.2): PE/EV-EBITDA medians EXCLUDE members with
non-positive TTM earnings/EBITDA (the ratio is undefined/meaningless); aggregates
INCLUDE them in the summed denominator (an honest blended yield) — if the SET's
summed denominator is itself non-positive, the aggregate value is written as
``None`` with an explicit ``method_flags`` note, never a sentinel or a nonsense sign.

Coverage honesty (section 5.5): every row carries ``n_members``/``n_valid``/
``coverage_pct``; a thin row (<50% coverage) is written anyway, tagged
``method_flags: {"coverage": "thin"}`` — never dropped, never hidden by omission.

``context_only`` members (out-of-pool LLM peers with only a market-cap-level fetch,
section 3.1 Step C / section 1) are excluded from `n_members` entirely, not merely
excluded from the numerator sums — they structurally can never contribute a value
to any metric here, so counting them in the denominator would permanently and
misleadingly cap every set's coverage_pct below 100%.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import cast

from compute.comparable_sets import MetricClass

# Below this coverage fraction a row is tagged "thin" (still written, never
# dropped — the pipeline never decides thinness away by omission, section 5.5).
_THIN_COVERAGE_THRESHOLD = 0.5


@dataclass(slots=True)
class MemberFinancials:
    """One comp-set member's raw-component inputs for one ``as_of`` date."""

    ticker: str
    context_only: bool
    market_cap: float | None
    enterprise_value: float | None
    ev_approximated: bool
    ttm_net_income: float | None
    ttm_ebitda: float | None
    p_b: float | None
    p_tbv: float | None
    rev_yoy: float | None
    fcf_yield_ttm: float | None


@dataclass(slots=True)
class MetricResult:
    metric: str
    stat_type: str
    value: float | None
    n_members: int
    n_valid: int
    coverage_pct: float
    method_flags: dict[str, object] = field(default_factory=dict[str, object])


# ---------------------------------------------------------------------------
# Raw-cache readers (local copies of the screens.py pattern — duplicated on
# purpose per this repo's "duplicate simple shared logic" convention rather
# than importing a private helper cross-module)
# ---------------------------------------------------------------------------


def _load_records(fmp_dir: Path, ticker: str, suffix: str) -> list[dict[str, object]]:
    path = fmp_dir / f"{ticker.upper()}_{suffix}.json"
    if not path.exists():
        return []
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    records = [
        cast("dict[str, object]", r) for r in cast("list[object]", raw) if isinstance(r, dict)
    ]
    records.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return records


def _num(record: dict[str, object], key: str) -> float | None:
    v = record.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _sum_last4(records: list[dict[str, object]], key: str) -> float | None:
    """All-or-nothing TTM sum: None unless all of the latest 4 quarters have the
    field (a partial sum would silently understate — exact ``screens.py::_sum_last4``
    pattern, applied here to raw netIncome/ebitda instead of per-quarter fractions)."""
    vals = [_num(r, key) for r in records[:4]]
    if len(vals) < 4 or any(v is None for v in vals):
        return None
    return sum(cast("list[float]", vals))


def _rev_yoy(records: list[dict[str, object]]) -> float | None:
    if len(records) < 5:
        return None
    curr = _num(records[0], "revenue")
    base = _num(records[4], "revenue")
    if curr is None or base is None or base <= 0:
        return None
    return curr / base - 1


def _market_cap_as_of(fmp_dir: Path, ticker: str, as_of: date) -> tuple[float | None, date | None]:
    """Latest ``historical_market_cap`` record on or before ``as_of`` (records are
    date-DESC sorted, so the first match on/under the cutoff is the newest one)."""
    for r in _load_records(fmp_dir, ticker, "historical_market_cap"):
        raw_date = str(r.get("date") or "")[:10]
        try:
            record_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if record_date <= as_of:
            return _num(r, "marketCap"), record_date
    return None, None


def _ev_daily_approx(fmp_dir: Path, ticker: str, as_of: date) -> tuple[float | None, bool]:
    """``ev_daily ≈ ev_at_last_quarter_close + (market_cap_daily -
    market_cap_at_last_quarter_close)`` (section 5.3) — net debt assumed sticky
    intra-quarter. Returns ``(None, False)`` when either the quarterly EV snapshot
    or the daily market-cap series is unavailable."""
    km = _load_records(fmp_dir, ticker, "key_metrics_quarterly")
    if not km:
        return None, False
    ev_at_quarter = _num(km[0], "enterpriseValue")
    mcap_at_quarter = _num(km[0], "marketCap")
    if ev_at_quarter is None or mcap_at_quarter is None:
        return None, False
    mcap_daily, _ = _market_cap_as_of(fmp_dir, ticker, as_of)
    if mcap_daily is None:
        return None, False
    return ev_at_quarter + (mcap_daily - mcap_at_quarter), True


def _book_multiples(
    market_cap: float | None, balance_row: dict[str, object] | None
) -> tuple[float | None, float | None]:
    """P/B and P/TBV exactly as ``valuation_basis.py::_manual_book_multiple``."""
    if market_cap is None or balance_row is None:
        return None, None
    equity = _num(balance_row, "totalStockholdersEquity") or _num(balance_row, "totalEquity")
    if equity is None or equity <= 0:
        return None, None
    p_b = market_cap / equity
    goodwill = _num(balance_row, "goodwill") or 0.0
    intangibles = _num(balance_row, "intangibleAssets") or 0.0
    tangible_book = equity - goodwill - intangibles
    p_tbv = market_cap / tangible_book if tangible_book > 0 else None
    return p_b, p_tbv


def load_member_financials(
    fmp_dir: Path, ticker: str, *, context_only: bool, as_of: date
) -> MemberFinancials:
    """Raw-component bundle for one member as of ``as_of``. Every field is
    independently ``None``-able — a member missing one input still contributes
    whichever others it has (coverage honesty is per-metric, not per-member)."""
    ticker = ticker.upper()
    inc = _load_records(fmp_dir, ticker, "income_statement_quarterly")
    km = _load_records(fmp_dir, ticker, "key_metrics_quarterly")
    bal = _load_records(fmp_dir, ticker, "balance_sheet_quarterly")

    market_cap, _ = _market_cap_as_of(fmp_dir, ticker, as_of)
    enterprise_value, ev_approximated = _ev_daily_approx(fmp_dir, ticker, as_of)
    p_b, p_tbv = _book_multiples(market_cap, bal[0] if bal else None)

    return MemberFinancials(
        ticker=ticker,
        context_only=context_only,
        market_cap=market_cap,
        enterprise_value=enterprise_value,
        ev_approximated=ev_approximated,
        ttm_net_income=_sum_last4(inc, "netIncome"),
        ttm_ebitda=_sum_last4(inc, "ebitda"),
        p_b=p_b,
        p_tbv=p_tbv,
        rev_yoy=_rev_yoy(inc),
        fcf_yield_ttm=_sum_last4(km, "freeCashFlowYield"),
    )


# ---------------------------------------------------------------------------
# Median / cap-weighted-aggregate constructions
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _eligible(members: list[MemberFinancials]) -> list[MemberFinancials]:
    """Full members only — ``context_only`` peers can never resolve a value here
    (module docstring) so they're excluded from the denominator entirely, not just
    the numerator sums."""
    return [m for m in members if not m.context_only]


def _coverage_flags(n_valid: int, n_members: int) -> tuple[dict[str, object], float]:
    coverage = (n_valid / n_members) if n_members else 0.0
    flags: dict[str, object] = {}
    if coverage < _THIN_COVERAGE_THRESHOLD:
        flags["coverage"] = "thin"
    return flags, coverage


def _ratio_metric(
    metric: str,
    members: list[MemberFinancials],
    numerator: str,
    denominator: str,
    *,
    exclude_non_positive_denominator_from_median: bool,
    undefined_aggregate_flag: str,
) -> list[MetricResult]:
    """Shared PE / EV-EBITDA construction: ``numerator``/``denominator`` are
    ``MemberFinancials`` attribute names (e.g. "market_cap"/"ttm_net_income")."""
    eligible = _eligible(members)
    n_members = len(eligible)

    median_vals: list[float] = []
    for m in eligible:
        num = getattr(m, numerator)
        den = getattr(m, denominator)
        if num is None or den is None:
            continue
        if exclude_non_positive_denominator_from_median and den <= 0:
            continue
        median_vals.append(num / den)
    median_flags, median_coverage = _coverage_flags(len(median_vals), n_members)
    median_result = MetricResult(
        metric,
        "median",
        _median(median_vals),
        n_members,
        len(median_vals),
        median_coverage,
        median_flags,
    )

    agg_valid = 0
    sum_num = 0.0
    sum_den = 0.0
    for m in eligible:
        num = getattr(m, numerator)
        den = getattr(m, denominator)
        if num is None or den is None:
            continue
        agg_valid += 1
        sum_num += num
        sum_den += den
    agg_flags, agg_coverage = _coverage_flags(agg_valid, n_members)
    if agg_valid == 0 or sum_den <= 0:
        agg_value = None
        agg_flags[undefined_aggregate_flag] = True
    else:
        agg_value = sum_num / sum_den
    agg_result = MetricResult(
        metric, "aggregate", agg_value, n_members, agg_valid, agg_coverage, agg_flags
    )
    return [median_result, agg_result]


def compute_pe(members: list[MemberFinancials]) -> list[MetricResult]:
    return _ratio_metric(
        "pe_ttm",
        members,
        "market_cap",
        "ttm_net_income",
        exclude_non_positive_denominator_from_median=True,
        undefined_aggregate_flag="aggregate_pe_undefined_negative_denominator",
    )


def compute_ev_ebitda(members: list[MemberFinancials]) -> list[MetricResult]:
    return _ratio_metric(
        "ev_ebitda_ttm",
        members,
        "enterprise_value",
        "ttm_ebitda",
        exclude_non_positive_denominator_from_median=True,
        undefined_aggregate_flag="aggregate_ev_ebitda_undefined_negative_denominator",
    )


def _median_only_metric(
    metric: str, members: list[MemberFinancials], field_name: str
) -> list[MetricResult]:
    eligible = _eligible(members)
    n_members = len(eligible)
    vals = [getattr(m, field_name) for m in eligible if getattr(m, field_name) is not None]
    flags, coverage = _coverage_flags(len(vals), n_members)
    return [
        MetricResult(
            metric,
            "median",
            _median(cast("list[float]", vals)),
            n_members,
            len(vals),
            coverage,
            flags,
        )
    ]


def compute_rev_yoy(members: list[MemberFinancials]) -> list[MetricResult]:
    return _median_only_metric("rev_yoy", members, "rev_yoy")


def compute_fcf_yield(members: list[MemberFinancials]) -> list[MetricResult]:
    return _median_only_metric("fcf_yield_ttm", members, "fcf_yield_ttm")


def compute_pb_ptbv(members: list[MemberFinancials]) -> list[MetricResult]:
    return _median_only_metric("p_b", members, "p_b") + _median_only_metric(
        "p_tbv", members, "p_tbv"
    )


def compute_metrics_for_set(
    members: list[MemberFinancials],
    metric_class: MetricClass,
    *,
    method_flags_passthrough: dict[str, object] | None = None,
) -> list[MetricResult]:
    """The full metric list for one comp set, gated by ``metric_class`` per section
    5.4: EV/EBITDA is not computed AT ALL for ``financial`` (nor, in this phase-1
    simplification, for ``reit`` — the doc's own note that EV/EBITDA is flagged
    not-meaningful for REITs and its P/FFO-proxy replacement is explicitly deferred
    to phase 2, section 10/13); P/B + P/TBV are financial-class-only (not specified
    for operating/reit in phase 1, so not fabricated here). PE is always computed
    (never suppressed, section 5.4)."""
    results = compute_pe(members)
    if metric_class == MetricClass.OPERATING:
        results += compute_ev_ebitda(members)
    if metric_class == MetricClass.FINANCIAL:
        results += compute_pb_ptbv(members)
    results += compute_rev_yoy(members)
    results += compute_fcf_yield(members)
    if method_flags_passthrough:
        for r in results:
            r.method_flags.update(method_flags_passthrough)
    return results


# ---------------------------------------------------------------------------
# Persistence — comp_set_metrics_daily upsert
# ---------------------------------------------------------------------------


def persist_metrics_daily(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    as_of_date: date,
    results: list[MetricResult],
    method_version: int,
) -> int:
    """Upsert every result row via the ``(scope_type, scope_key, as_of_date,
    metric, stat_type, method_version)`` unique constraint (section 8) — a re-run
    over an already-written date refreshes ``value``/``coverage_pct``/``computed_at``
    in place rather than duplicating rows."""
    now = datetime.now()
    cur = conn.cursor()
    for r in results:
        method_flags_json = json.dumps(r.method_flags) if r.method_flags else None
        cur.execute(
            "INSERT INTO comp_set_metrics_daily "
            "(scope_type, scope_key, as_of_date, metric, stat_type, value, n_members, "
            " n_valid, coverage_pct, method_version, method_flags, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_key, as_of_date, metric, stat_type, method_version) "
            "DO UPDATE SET value=excluded.value, n_members=excluded.n_members, "
            "n_valid=excluded.n_valid, coverage_pct=excluded.coverage_pct, "
            "method_flags=excluded.method_flags, computed_at=excluded.computed_at",
            (
                scope_type,
                scope_key,
                as_of_date,
                r.metric,
                r.stat_type,
                r.value,
                r.n_members,
                r.n_valid,
                r.coverage_pct,
                method_version,
                method_flags_json,
                now,
            ),
        )
    conn.commit()
    return len(results)
