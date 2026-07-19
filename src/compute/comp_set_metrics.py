"""Bottoms-up comparable-set aggregate metrics
(docs/design/comparable_sets_bottoms_up.md §5).

Phase 1 shipped `scope_type='comparable_set'` rows; Phase 2 reuses the same
`compute_comparable_set_metrics` for the pool-wide `'industry'`/`'sector'`
slice rows (§6 — callers pass `comparable_sets.pool_scope_slices` output) and
adds the `'fmp_snapshot'` reference rows in `compute.comp_set_drift`.

Provenance (Phase 2): every computed MetricRow carries a `derived`-kind
FactLocator (construction formula + member coverage in `display`; the exact
membership stays queryable in comparable_set_members) and every persisted row
serializes it into comp_set_metrics_daily.locator (alembic 0172) — the
per-#911 locator rule applied to this table; FMP snapshot rows get
`vendor_field` locators in `comp_set_drift`.

Split out from ``compute.comparable_sets`` (which owns set *resolution* —
who's in the set) because this module owns set *measurement* — what the
set's numbers are, for a given ``as_of_date``. Different concern, different
inputs (raw per-member FMP quarterly/daily cache files rather than the
tracked_companies/profile join).

Per §5.3: ``ttm_net_income``/``ttm_ebitda`` are summed from the last 4
*reported* ``income_statement_quarterly.json`` rows (own the same
all-4-present-or-None discipline as ``discovery.screens._sum_last4`` —
duplicated here rather than imported, per repo convention: shared logic this
small is meant to be duplicated, not modularized, so each caller can evolve
its own edge-case handling independently). ``market_cap``/``enterprise_value``
come from the **daily** ``historical_market_cap.json`` series (so the
multiple moves with the market every day even though earnings only update on
the member's own earnings date); EV on a non-quarter-end day is approximated
via ``ev_daily ≈ ev_at_last_quarter_close + (market_cap_daily -
market_cap_at_last_quarter_close)`` (net debt assumed sticky intra-quarter),
flagged ``ev_daily_approximated: true``.

Currency guard (a real finding while validating this module against
production data — HDB and IBN, both Indian-bank ADRs in NU's resolved comp
set, report ``income_statement_quarterly``/``balance_sheet_quarterly`` in INR
while their ADR ``historical_market_cap``/price series is USD; summing INR
net income against a USD market cap silently produced a nonsense sub-1x
aggregate P/E for NU's set during Phase 1 validation). Per AGENTS.md
("currency must be explicit on every numeric field... never silently
merge"), every currency-denominated raw input (``ttm_net_income``,
``ttm_ebitda``, ``equity``, ``tangible_book``) is null'd out — never
silently converted or coerced — when the member's ``reportedCurrency`` isn't
``"USD"``. This naturally reduces that member's ``n_valid``/``coverage_pct``
for those metrics (the existing "hide-don't-stub" coverage mechanism, §5.5)
rather than requiring a separate exclusion list. ``rev_yoy``/
``fcf_yield_ttm`` are same-currency ratios (FMP's own `freeCashFlowYield` is
already dimensionless; revenue YoY is a same-currency ratio) and are NOT
gated on this check.

Naming overlap with ``compute.metrics_engine`` (docs/design/
bottoms_up_metrics_engine.md, merged 2026-07): that engine's ``revenue_yoy``
formula is the SAME construction as this module's ``rev_yoy`` metric
((rev_t - rev_t-4q) / rev_t-4q). Not reused directly: metrics_engine resolves
its inputs from the ``financial_facts`` table, populated only for tickers
whose documents have been extracted (``ANALYZED_LIST_TYPES`` —
portfolio/watchlist/evaluation); most comparable-set members are
``index_member`` pool tickers with no ``financial_facts`` rows at all (their
only data is the raw FMP cache). This module reads the FMP cache directly for
every member uniformly instead, so a set's subject and its `index_member`
peers are computed the same way. Likewise, ``compute_ebitda`` in
metrics_engine derives EBITDA as `operating_income + D&A` from
`financial_facts`; this module sums FMP's own reported per-quarter `ebitda`
field directly (§5.3's explicit decision) — the two are expected to closely
agree but are not cross-checked in Phase 1.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from compute.comparable_sets import ComparableSetMember, MetricClass
from models.facts import DerivedRef, FactLocator
from pipeline.locators import derived_locator

# ---------------------------------------------------------------------------
# Per-member raw inputs
# ---------------------------------------------------------------------------


@dataclass
class MemberSnapshot:
    """One member's raw inputs as-of a date. Any field is None when the
    underlying cache doesn't support it for that member/date — never a
    fabricated 0."""

    ticker: str
    market_cap: float | None = None
    enterprise_value: float | None = None
    ev_daily_approximated: bool = False
    ttm_net_income: float | None = None
    ttm_ebitda: float | None = None
    equity: float | None = None
    tangible_book: float | None = None
    rev_yoy_pct: float | None = None
    fcf_yield_ttm: float | None = None
    # True when a non-USD `reportedCurrency` caused ttm_net_income/ttm_ebitda/
    # equity/tangible_book to be None'd out rather than converted (see module
    # docstring's HDB/IBN finding) -- surfaced onto the metric row's
    # method_flags so a thin/zero result is traceable to currency, not just
    # "missing data."
    non_usd_currency: bool = False


@dataclass
class MetricRow:
    metric: str
    stat_type: str  # 'median'|'aggregate'
    value: float | None
    n_members: int
    n_valid: int
    coverage_pct: float
    method_flags: dict[str, object]
    # Provenance (Phase 2, alembic 0172): a `derived`-kind FactLocator for
    # computed rows, `vendor_field` for FMP snapshot rows. Attached by
    # compute_comparable_set_metrics / comp_set_drift; serialized by
    # upsert_metric_rows.
    locator: FactLocator | None = None


def _load_series(repo_root: Path, ticker: str, suffix: str) -> list[dict[str, object]]:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_{suffix}.json"
    if not path.exists():
        return []
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    rows = cast("list[object]", raw)
    return [cast("dict[str, object]", r) for r in rows if isinstance(r, dict)]


def _as_of_or_before(rows: list[dict[str, object]], as_of: date) -> list[dict[str, object]]:
    """Rows with a `date` field on/before `as_of`, sorted newest-first."""
    as_of_iso = as_of.isoformat()
    filtered = [r for r in rows if str(r.get("date") or "") and str(r.get("date")) <= as_of_iso]
    filtered.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return filtered


def _sum_last4(rows_desc: list[dict[str, object]], key: str) -> float | None:
    """Sum `key` over the 4 newest rows; None unless all 4 are present.
    Mirrors `discovery.screens._sum_last4`'s all-4-present-or-None gate
    (duplicated per repo convention, not imported — see module docstring)."""
    if len(rows_desc) < 4:
        return None
    vals = [_float(r.get(key)) for r in rows_desc[:4]]
    if any(v is None for v in vals):
        return None
    return sum(v for v in vals if v is not None)


def _is_usd(row: dict[str, object] | None) -> bool:
    """True when `row`'s `reportedCurrency` is USD or absent (the common
    case for a domestic US filer's cache — the field isn't always present on
    every endpoint). False only on an EXPLICIT non-USD value — never assumed
    true blind. See module docstring's HDB/IBN currency-guard finding."""
    if row is None:
        return True
    currency = row.get("reportedCurrency")
    return not isinstance(currency, str) or currency.strip().upper() in ("", "USD")


def load_member_snapshot(repo_root: Path, ticker: str, as_of: date) -> MemberSnapshot:
    """Compute one member's raw metric inputs as-of `as_of`, from its FMP
    cache files. Missing inputs are None, never fabricated. Currency-
    denominated raw fields are None'd out (not converted) for a non-USD
    reporter — see module docstring."""
    ticker = ticker.upper()
    snap = MemberSnapshot(ticker=ticker)

    mcap_series = _as_of_or_before(_load_series(repo_root, ticker, "historical_market_cap"), as_of)
    if mcap_series:
        snap.market_cap = _float(mcap_series[0].get("marketCap"))

    km_q = _as_of_or_before(_load_series(repo_root, ticker, "key_metrics_quarterly"), as_of)
    km_is_usd = _is_usd(km_q[0] if km_q else None)
    if km_q and snap.market_cap is not None and km_is_usd:
        latest_km = km_q[0]
        ev_at_quarter = _float(latest_km.get("enterpriseValue"))
        mcap_at_quarter = _float(latest_km.get("marketCap"))
        if ev_at_quarter is not None and mcap_at_quarter is not None:
            snap.enterprise_value = ev_at_quarter + (snap.market_cap - mcap_at_quarter)
            snap.ev_daily_approximated = True
    # freeCashFlowYield is FMP's own ratio (FCF / market cap, both in the
    # member's own reporting currency) -- dimensionless, no currency guard.
    snap.fcf_yield_ttm = _sum_last4(km_q, "freeCashFlowYield")

    inc_q = _as_of_or_before(_load_series(repo_root, ticker, "income_statement_quarterly"), as_of)
    inc_is_usd = _is_usd(inc_q[0] if inc_q else None)
    if inc_is_usd:
        snap.ttm_net_income = _sum_last4(inc_q, "netIncome")
        snap.ttm_ebitda = _sum_last4(inc_q, "ebitda")
    else:
        snap.non_usd_currency = True
    if len(inc_q) >= 5:
        # rev_yoy is a same-currency ratio -- safe regardless of reporting
        # currency, no guard needed.
        rev_t = _float(inc_q[0].get("revenue"))
        rev_t4 = _float(inc_q[4].get("revenue"))
        if rev_t is not None and rev_t4 is not None and rev_t4 > 0:
            snap.rev_yoy_pct = (rev_t - rev_t4) / rev_t4 * 100.0

    bal_q = _as_of_or_before(_load_series(repo_root, ticker, "balance_sheet_quarterly"), as_of)
    bal_is_usd = _is_usd(bal_q[0] if bal_q else None)
    if bal_q and snap.market_cap is not None and bal_is_usd:
        row = bal_q[0]
        equity = _float(row.get("totalStockholdersEquity")) or _float(row.get("totalEquity"))
        if equity is not None and equity > 0:
            snap.equity = equity
            goodwill = _float(row.get("goodwill")) or 0.0
            intangibles = _float(row.get("intangibleAssets")) or 0.0
            tangible = equity - goodwill - intangibles
            if tangible > 0:
                snap.tangible_book = tangible
    elif bal_q and not bal_is_usd:
        snap.non_usd_currency = True

    return snap


# ---------------------------------------------------------------------------
# §5 median/aggregate construction
# ---------------------------------------------------------------------------


def _coverage_row(
    metric: str, stat_type: str, value: float | None, n_members: int, n_valid: int
) -> MetricRow:
    coverage = (n_valid / n_members) if n_members else 0.0
    flags: dict[str, object] = {}
    if coverage < 0.5:
        flags["coverage"] = "thin"
    return MetricRow(
        metric=metric,
        stat_type=stat_type,
        value=value,
        n_members=n_members,
        n_valid=n_valid,
        coverage_pct=coverage,
        method_flags=flags,
    )


def _tag_non_usd(row: MetricRow, snapshots: list[MemberSnapshot]) -> MetricRow:
    """Attach `excluded_non_usd_n` when any member's currency-denominated
    inputs were None'd out (see module docstring) — coverage honesty: a thin
    row is traceable to a currency mismatch, not generic missing data."""
    n = sum(1 for s in snapshots if s.non_usd_currency)
    if n:
        row.method_flags["excluded_non_usd_n"] = n
    return row


def _pe_rows(snapshots: list[MemberSnapshot], n_members: int) -> list[MetricRow]:
    # Median: exclude ttm_net_income <= 0 (PE undefined/meaningless negative).
    member_pes = [
        s.market_cap / s.ttm_net_income
        for s in snapshots
        if s.market_cap is not None and s.ttm_net_income is not None and s.ttm_net_income > 0
    ]
    median_row = _coverage_row(
        "pe_ttm",
        "median",
        statistics.median(member_pes) if member_pes else None,
        n_members,
        len(member_pes),
    )

    # Aggregate: include every member with both market_cap and ttm_net_income
    # (even if <= 0) -- the sum of a lossmaking member's negative earnings is
    # the honest blended yield of owning the whole set.
    agg_inputs = [
        (s.market_cap, s.ttm_net_income)
        for s in snapshots
        if s.market_cap is not None and s.ttm_net_income is not None
    ]
    agg_row = _coverage_row("pe_ttm", "aggregate", None, n_members, len(agg_inputs))
    if agg_inputs:
        sum_mcap = sum(m for m, _ in agg_inputs)
        sum_earnings = sum(e for _, e in agg_inputs)
        if sum_earnings > 0:
            agg_row.value = sum_mcap / sum_earnings
        else:
            agg_row.method_flags["aggregate_pe_undefined_negative_denominator"] = True
    return [_tag_non_usd(median_row, snapshots), _tag_non_usd(agg_row, snapshots)]


def _ev_ebitda_rows(snapshots: list[MemberSnapshot], n_members: int) -> list[MetricRow]:
    member_vals = [
        s.enterprise_value / s.ttm_ebitda
        for s in snapshots
        if s.enterprise_value is not None and s.ttm_ebitda is not None and s.ttm_ebitda > 0
    ]
    median_row = _coverage_row(
        "ev_ebitda_ttm",
        "median",
        statistics.median(member_vals) if member_vals else None,
        n_members,
        len(member_vals),
    )
    if any(s.ev_daily_approximated for s in snapshots):
        median_row.method_flags["ev_daily_approximated"] = True

    agg_inputs = [
        (s.enterprise_value, s.ttm_ebitda)
        for s in snapshots
        if s.enterprise_value is not None and s.ttm_ebitda is not None
    ]
    agg_row = _coverage_row("ev_ebitda_ttm", "aggregate", None, n_members, len(agg_inputs))
    if agg_inputs:
        sum_ev = sum(e for e, _ in agg_inputs)
        sum_ebitda = sum(e for _, e in agg_inputs)
        if sum_ebitda > 0:
            agg_row.value = sum_ev / sum_ebitda
        else:
            agg_row.method_flags["aggregate_ev_ebitda_undefined_negative_denominator"] = True
    if any(s.ev_daily_approximated for s in snapshots):
        agg_row.method_flags["ev_daily_approximated"] = True
    return [_tag_non_usd(median_row, snapshots), _tag_non_usd(agg_row, snapshots)]


def _book_multiple_rows(
    metric: str,
    field_name: str,  # 'equity'|'tangible_book'
    snapshots: list[MemberSnapshot],
    n_members: int,
) -> list[MetricRow]:
    def _denom(s: MemberSnapshot) -> float | None:
        return s.equity if field_name == "equity" else s.tangible_book

    member_vals = [
        s.market_cap / d
        for s in snapshots
        if s.market_cap is not None and (d := _denom(s)) is not None
    ]
    median_row = _coverage_row(
        metric,
        "median",
        statistics.median(member_vals) if member_vals else None,
        n_members,
        len(member_vals),
    )
    agg_inputs = [
        (s.market_cap, d)
        for s in snapshots
        if s.market_cap is not None and (d := _denom(s)) is not None
    ]
    agg_row = _coverage_row(metric, "aggregate", None, n_members, len(agg_inputs))
    if agg_inputs:
        sum_mcap = sum(m for m, _ in agg_inputs)
        sum_denom = sum(d for _, d in agg_inputs)
        if sum_denom > 0:
            agg_row.value = sum_mcap / sum_denom
    return [_tag_non_usd(median_row, snapshots), _tag_non_usd(agg_row, snapshots)]


def _rev_yoy_row(snapshots: list[MemberSnapshot], n_members: int) -> MetricRow:
    vals = [s.rev_yoy_pct for s in snapshots if s.rev_yoy_pct is not None]
    return _coverage_row(
        "rev_yoy", "median", statistics.median(vals) if vals else None, n_members, len(vals)
    )


def _fcf_yield_row(snapshots: list[MemberSnapshot], n_members: int) -> MetricRow:
    vals = [s.fcf_yield_ttm for s in snapshots if s.fcf_yield_ttm is not None]
    return _coverage_row(
        "fcf_yield_ttm", "median", statistics.median(vals) if vals else None, n_members, len(vals)
    )


# Human-legible construction formula per (metric, stat_type) — the `display`
# of the derived locator every computed row carries. Kept next to the code
# that implements each construction so they can't drift silently.
_METRIC_CONSTRUCTION: dict[tuple[str, str], str] = {
    ("pe_ttm", "median"): "median of member market_cap / TTM net income (4 reported quarters)",
    ("pe_ttm", "aggregate"): "sum(member market_cap) / sum(member TTM net income)",
    ("ev_ebitda_ttm", "median"): "median of member enterprise_value / TTM EBITDA",
    ("ev_ebitda_ttm", "aggregate"): "sum(member enterprise_value) / sum(member TTM EBITDA)",
    ("p_b", "median"): "median of member market_cap / totalStockholdersEquity",
    ("p_b", "aggregate"): "sum(member market_cap) / sum(member totalStockholdersEquity)",
    ("p_tbv", "median"): "median of member market_cap / (equity - goodwill - intangibles)",
    ("p_tbv", "aggregate"): "sum(member market_cap) / sum(member tangible book)",
    ("rev_yoy", "median"): "median of member (rev_t - rev_t-4q) / rev_t-4q",
    ("fcf_yield_ttm", "median"): "median of member 4-quarter freeCashFlowYield sums",
}


def _derived_locator_for(row: MetricRow) -> FactLocator:
    """The `derived`-kind locator for one computed row (per-#911 rule):
    construction formula + coverage in `display`; the row's method_flags keys
    as the locator's method_flags. Per-member DerivedInputRefs are
    deliberately omitted — membership is already first-class queryable in
    comparable_set_members, and duplicating up-to-dozens of refs into every
    daily row would bloat the table for no render gain."""
    construction = _METRIC_CONSTRUCTION.get(
        (row.metric, row.stat_type), f"{row.stat_type} {row.metric}"
    )
    return derived_locator(
        derived=DerivedRef(
            display=(
                f"{construction} over {row.n_valid}/{row.n_members} members "
                "(bottoms-up from per-member FMP cache)"
            ),
            method_flags=sorted(str(k) for k in row.method_flags),
        )
    )


def compute_comparable_set_metrics(
    repo_root: Path,
    members: list[ComparableSetMember],
    metric_class: MetricClass,
    as_of: date,
) -> list[MetricRow]:
    """§5: median + cap-weighted aggregate rows for one comparable set as-of
    `as_of`. `context_only` members (§3.1 Step C) never contribute — they're
    roster-visible but excluded from every computation here."""
    contributing = [m.member_ticker for m in members if not m.context_only]
    n_members = len(contributing)
    if metric_class == "reit":
        # §10/§13: P/FFO-proxy not built in Phase 1; EV/EBITDA flagged n/m for
        # REITs too. Rather than emit a misleading pe_ttm/ev_ebitda_ttm row
        # for a metric class this module doesn't model, skip entirely.
        return []

    snapshots = [load_member_snapshot(repo_root, t, as_of) for t in contributing]

    rows: list[MetricRow] = list(_pe_rows(snapshots, n_members))
    if metric_class == "operating":
        rows.extend(_ev_ebitda_rows(snapshots, n_members))
    elif metric_class == "financial":
        rows.extend(_book_multiple_rows("p_b", "equity", snapshots, n_members))
        rows.extend(_book_multiple_rows("p_tbv", "tangible_book", snapshots, n_members))
    rows.append(_rev_yoy_row(snapshots, n_members))
    rows.append(_fcf_yield_row(snapshots, n_members))
    for row in rows:
        row.locator = _derived_locator_for(row)
    return rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert_metric_rows(
    conn: sqlite3.Connection,
    scope_type: str,
    scope_key: str,
    as_of: date,
    method_version: int,
    rows: list[MetricRow],
) -> int:
    """Upsert `rows` into comp_set_metrics_daily, keyed on the table's unique
    constraint (scope_type, scope_key, as_of_date, metric, stat_type,
    method_version). Returns the number of rows written."""
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    n = 0
    for row in rows:
        conn.execute(
            "INSERT INTO comp_set_metrics_daily "
            "(scope_type, scope_key, as_of_date, metric, stat_type, value, "
            " n_members, n_valid, coverage_pct, method_version, method_flags, computed_at, "
            " locator) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_key, as_of_date, metric, stat_type, method_version) "
            "DO UPDATE SET value=excluded.value, n_members=excluded.n_members, "
            "n_valid=excluded.n_valid, coverage_pct=excluded.coverage_pct, "
            "method_flags=excluded.method_flags, computed_at=excluded.computed_at, "
            "locator=excluded.locator",
            (
                scope_type,
                scope_key,
                as_of.isoformat(),
                row.metric,
                row.stat_type,
                row.value,
                row.n_members,
                row.n_valid,
                row.coverage_pct,
                method_version,
                json.dumps(row.method_flags),
                now_iso,
                row.locator.to_json() if row.locator is not None else None,
            ),
        )
        n += 1
    conn.commit()
    return n


def _float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None
