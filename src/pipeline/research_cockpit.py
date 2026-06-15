"""Research cockpit — the landing screen (master build P1.2).

One dense row per holding answering "which holding needs my attention today?"
without leaving the Overview tab:

* **Thesis health** — verdict badge from the latest ``thesis_evaluations`` row
  (breached/warned rule names in the hover), plus tier-1 KPI deltas (latest vs
  prior quarter, toned by any matching break-rule status).
* **Valuation** — price & day move from the FMP profile cache on disk, the DCF
  fair-value gap recomputed from the latest ``dcf_runs`` row's own price + fair
  value (the stored ``over_under_pct`` mixes two writer conventions, and a
  fresher external price would silently mix currencies), and the PEG ratio from
  the §Valuation cache where present.
* **Events** — next earnings date from the ``expected_earnings`` table (0082,
  the canonical calendar materialized daily by
  ``execution/refresh_expected_earnings.py``), falling back per ticker to the
  FMP earnings-calendar cache on disk (a read-only twin of
  ``sources.earnings_calendar``: a render path must not write ``source_calls``
  telemetry rows); unreviewed (pending) alerts, documents fetched since the
  last report build, and open report comments.

Evaluation-list names get a thinner row variant (no KPI chips, tighter type)
and their own sort: attention ordering is near-meaningless for names with no
thesis rules (it degenerates to alphabetical), so the evaluation table orders
by a transparent next-dollar attractiveness score instead — DCF upside ×
revenue growth × FCF margin × PEG, each a small band table, the factor math
shipped verbatim as the score chip's hover title (see
:func:`eval_attractiveness`). The old per-column ops-freshness tables shrink
to one staleness dot per row with the detail in its hover title.

Built directly over ``data/portfolio.db`` + the on-disk FMP caches. Every
enrichment query degrades to empty on a missing table/column
(``sqlite3.OperationalError``) so minimal hand-rolled test schemas and partial
DBs render a valid — just sparser — cockpit. Pure reads, no mutations, no
network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import cast

from allocation.candidate_fit import fit_tone
from candidate_fit_cache import read_materialized_candidate_fit
from cockpit_fundamentals import (
    compute_from_db as _compute_fundamentals,
)
from cockpit_fundamentals import (
    read_materialized_fundamentals,
)
from expected_earnings import upcoming_by_ticker
from pipeline.dashboard_status import DashboardRow, build_dashboard_rows
from report.renderers.numfmt import fmt_date, fmt_pct, fmt_pp, fmt_reltime

# Worst-wins ordering for thesis verdicts and rule statuses; doubles as the
# attention sort key (breach floats to the top of the cockpit).
_STATUS_RANK: dict[str, int] = {"breach": 0, "broken": 0, "warn": 1, "watch": 1, "unresolved": 2}
_STATUS_TONE: dict[str, str] = {
    "breach": "bad",
    "broken": "bad",
    "warn": "warn",
    "watch": "warn",
    "ok": "ok",
    "intact": "ok",
}

# Staleness-dot thresholds (days). The FMP cycle is daily and builds are
# weekly-ish, so a few quiet days is normal; beyond these the cron is broken.
_FMP_WARN_DAYS, _FMP_BAD_DAYS = 3.0, 14.0
_BUILD_WARN_DAYS, _BUILD_BAD_DAYS = 10.0, 30.0

_MAX_KPI_CHIPS = 3


@dataclass(frozen=True)
class KpiDelta:
    """One tier-1 KPI's move between its two most recent facts."""

    name: str
    unit: str
    latest_value: float
    prior_value: float
    latest_period: str  # ISO date
    prior_period: str  # ISO date
    tone: str  # "bad" | "warn" | "ok" | "neutral" (from a matching break rule)

    @property
    def is_pp(self) -> bool:
        return "percent" in self.unit.lower() or self.unit.strip() in {"%", "pp"}

    @property
    def magnitude(self) -> float:
        """Sort key: pp move for percent KPIs, relative % move otherwise."""
        if self.is_pp:
            return abs(self.latest_value - self.prior_value)
        if self.prior_value == 0:
            return 0.0
        return abs((self.latest_value - self.prior_value) / abs(self.prior_value)) * 100.0

    @property
    def delta_display(self) -> str:
        if self.is_pp:
            return fmt_pp(self.latest_value - self.prior_value)
        if self.prior_value == 0:
            return "n/a"
        rel = (self.latest_value - self.prior_value) / abs(self.prior_value) * 100.0
        return fmt_pct(rel, signed=True)


@dataclass(frozen=True)
class CockpitRow:
    """Everything one holding's cockpit row renders."""

    base: DashboardRow
    name: str | None = None
    # Valuation
    price: float | None = None
    day_move_pct: float | None = None  # percent points (FMP changePercentage)
    price_asof: str | None = None  # ISO datetime (profile-cache mtime)
    fv_gap_pct: float | None = None  # live_price/npv_per_share - 1, in % (+ = above FV)
    fair_value: float | None = None  # dcf_runs.npv_per_share
    dcf_price: float | None = None  # price the gap was computed against
    dcf_date: str | None = None  # dcf_runs.valuation_date
    peg_ratio: float | None = None
    # Events
    next_earnings: str | None = None  # ISO date
    pending_alerts: int = 0
    new_docs: int = 0
    # Eval-screen fundamentals (PR7): the thin/evaluation table swaps the
    # mostly-empty portfolio columns for screen-relevant ones.
    rev_yoy_pct: float | None = None  # latest quarter vs the year-ago quarter
    fcf_margin_pct: float | None = None  # TTM, percent points
    # Next-dollar attractiveness (evaluation rows only — portfolio rows keep
    # the attention sort and never carry a score).
    attractiveness: float | None = None
    attractiveness_why: str | None = None  # the factor math, chip hover verbatim
    attractiveness_partial: bool = False  # at least one factor input missing
    # Portfolio fit (evaluation rows only): how the name would sit in the held
    # book — the materialized candidate_fit.json scalar (PR3), sibling to the
    # standalone attractiveness score. The full breakdown lives in the Fit-chip
    # peek; see allocation.candidate_fit / candidate_fit_cache.
    fit: float | None = None
    fit_why: str | None = None  # the fit-factor math, chip hover verbatim
    fit_partial: bool = False  # at least one fit factor's inputs unavailable
    # Thesis detail
    kpi_deltas: list[KpiDelta] = field(default_factory=list["KpiDelta"])
    rule_summary: str | None = None  # breached/warned rule names for the badge hover
    evaluated_at: str | None = None

    @property
    def attention_key(self) -> tuple[int, int, int, str]:
        status = (self.base.breach_status or "").lower()
        return (
            _STATUS_RANK.get(status, 3),
            -self.pending_alerts,
            -self.new_docs,
            self.base.ticker,
        )


# --------------------------------------------------------------------------- #
# Evaluation-list attractiveness (the next-dollar sort)
# --------------------------------------------------------------------------- #
# "Which screen name deserves the next research dollar?" — a transparent
# product of band-table multipliers in the dashboard.inbox_rank style: NO ML,
# and no price-history/covariance work on a render path (the allocation
# model's expensive legs stay out). Every input is already on the row:
#
#     score = dcf (fair-value upside) x growth (Rev YoY) x fcf (TTM margin)
#           x peg (PEG ratio)
#
# Each factor is named with its input in the why string that ships as the
# score chip's hover title, so the math is reproducible by eye. A missing
# input contributes ``_MISSING_FACTOR`` and marks the row partial: sparse
# names sink below comparably-scored full-data ones instead of vanishing,
# while staying above known-bad fundamentals (unexplored beats known-bad).

_MISSING_FACTOR = 0.85

# (threshold, multiplier) rows, best-first: the first row whose threshold the
# value meets (>=) wins; below every band falls to the floor. Upside is
# npv_per_share / live_price - 1 in %, the run's own price — the same
# convention (and the same currency-consistency argument) as
# :func:`latest_dcf_runs` and ``allocation.model._dcf_upside``.
_DCF_BANDS: tuple[tuple[float, float], ...] = (
    (50.0, 1.8),
    (25.0, 1.5),
    (10.0, 1.25),
    (-10.0, 1.0),
    (-30.0, 0.7),
)
_DCF_FLOOR = 0.5
_GROWTH_BANDS: tuple[tuple[float, float], ...] = (
    (30.0, 1.6),
    (20.0, 1.4),
    (10.0, 1.2),
    (0.0, 1.0),
    (-10.0, 0.75),
)
_GROWTH_FLOOR = 0.55
_FCF_BANDS: tuple[tuple[float, float], ...] = ((25.0, 1.3), (15.0, 1.15), (5.0, 1.0), (0.0, 0.9))
_FCF_FLOOR = 0.7
# PEG is lower-better (growth-adjusted cheapness): <= thresholds, best-first.
_PEG_BANDS: tuple[tuple[float, float], ...] = ((1.0, 1.2), (2.0, 1.0), (3.0, 0.9))
_PEG_FLOOR = 0.8

# Chip tone thresholds (render-side): hi = worth a look, lo = sinking.
_ATTRACT_HI, _ATTRACT_LO = 1.25, 0.75

# Human labels for the breakdown peek; the ``why`` string still uses the bare
# keys ("dcf", "growth", …) because that chip-tooltip format is pinned by tests.
_FACTOR_LABELS: dict[str, str] = {
    "dcf": "DCF upside",
    "growth": "Revenue growth",
    "fcf": "FCF margin",
    "peg": "PEG",
}


@dataclass(frozen=True)
class AttractivenessFactor:
    """One factor's contribution to the next-dollar score."""

    key: str  # "dcf" | "growth" | "fcf" | "peg" — the why-string token
    label: str  # human label for the breakdown peek ("DCF upside")
    multiplier: float  # band multiplier (``_MISSING_FACTOR`` when the input is absent)
    detail: str  # the input value, formatted ("+32.0% upside"); "n/a" when missing
    missing: bool


@dataclass(frozen=True)
class AttractivenessBreakdown:
    """The full next-dollar attractiveness decomposition for one name: a
    multiplier per factor, their product (``score``), and the reproducible
    ``why`` string. The Score-chip peek renders ``factors``;
    :func:`eval_attractiveness` is the (score, why, partial) view of this."""

    factors: list[AttractivenessFactor]
    score: float
    why: str
    partial: bool


def attractiveness_tone(score: float) -> str:
    """'hi' (worth a look) / 'lo' (sinking) / '' (neutral). The chip and the
    breakdown peek share this so they tone the same score identically."""
    if score >= _ATTRACT_HI:
        return "hi"
    if score <= _ATTRACT_LO:
        return "lo"
    return ""


def _band(value: float, bands: tuple[tuple[float, float], ...], floor: float) -> float:
    for threshold, mult in bands:
        if value >= threshold:
            return mult
    return floor


def _peg_band(value: float) -> float:
    for threshold, mult in _PEG_BANDS:
        if value <= threshold:
            return mult
    return _PEG_FLOOR


def attractiveness_breakdown(
    *,
    dcf_upside_pct: float | None,
    rev_yoy_pct: float | None,
    fcf_margin_pct: float | None,
    peg_ratio: float | None,
) -> AttractivenessBreakdown:
    """The next-dollar attractiveness decomposition: a band multiplier per
    factor, their product, and the reproducible ``why`` string.
    :func:`eval_attractiveness` is the (score, why, partial) view of this; the
    Score-chip peek renders the factor rows.

    A non-positive PEG is treated as missing (the §Valuation builder gates on
    positive forward growth, so a negative ratio is cache garbage, not signal).
    """
    raw: list[tuple[str, float, str]] = []
    if dcf_upside_pct is None:
        raw.append(("dcf", _MISSING_FACTOR, "n/a"))
    else:
        raw.append(
            (
                "dcf",
                _band(dcf_upside_pct, _DCF_BANDS, _DCF_FLOOR),
                f"{fmt_pct(dcf_upside_pct, signed=True)} upside",
            )
        )
    if rev_yoy_pct is None:
        raw.append(("growth", _MISSING_FACTOR, "n/a"))
    else:
        raw.append(
            (
                "growth",
                _band(rev_yoy_pct, _GROWTH_BANDS, _GROWTH_FLOOR),
                f"{fmt_pct(rev_yoy_pct, signed=True)} YoY",
            )
        )
    if fcf_margin_pct is None:
        raw.append(("fcf", _MISSING_FACTOR, "n/a"))
    else:
        raw.append(
            (
                "fcf",
                _band(fcf_margin_pct, _FCF_BANDS, _FCF_FLOOR),
                f"{fmt_pct(fcf_margin_pct)} margin",
            )
        )
    if peg_ratio is None or peg_ratio <= 0:
        raw.append(("peg", _MISSING_FACTOR, "n/a"))
    else:
        raw.append(("peg", _peg_band(peg_ratio), f"{peg_ratio:.1f}"))

    score = 1.0
    for _, mult, _ in raw:
        score *= mult
    partial = any(detail == "n/a" for _, _, detail in raw)
    why = (
        " x ".join(f"{name} {mult:.2f} ({detail})" for name, mult, detail in raw)
        + f" = {score:.2f}"
    )
    factors = [
        AttractivenessFactor(
            key=name,
            label=_FACTOR_LABELS[name],
            multiplier=mult,
            detail=detail,
            missing=detail == "n/a",
        )
        for name, mult, detail in raw
    ]
    return AttractivenessBreakdown(factors=factors, score=score, why=why, partial=partial)


def eval_attractiveness(
    *,
    dcf_upside_pct: float | None,
    rev_yoy_pct: float | None,
    fcf_margin_pct: float | None,
    peg_ratio: float | None,
) -> tuple[float, str, bool]:
    """(score, why, partial) — the flat view over :func:`attractiveness_breakdown`.

    Public: tests pin the band edges and the why format; the render path
    surfaces ``why`` verbatim as the chip tooltip.
    """
    bd = attractiveness_breakdown(
        dcf_upside_pct=dcf_upside_pct,
        rev_yoy_pct=rev_yoy_pct,
        fcf_margin_pct=fcf_margin_pct,
        peg_ratio=peg_ratio,
    )
    return bd.score, bd.why, bd.partial


def _dcf_upside_pct(fair_value: float | None, dcf_price: float | None) -> float | None:
    """npv_per_share / live_price − 1, in %, against the run's own price — the
    same currency-consistent convention as ``allocation.model._dcf_upside``.
    None when either leg is missing or non-positive (cache garbage)."""
    if fair_value is not None and fair_value > 0 and dcf_price is not None and dcf_price > 0:
        return (fair_value / dcf_price - 1.0) * 100.0
    return None


def compute_attractiveness(
    conn: sqlite3.Connection, repo_root: Path, ticker: str
) -> AttractivenessBreakdown | None:
    """One evaluation name's next-dollar breakdown, recomputed from the SAME
    readers :func:`build_cockpit_rows` uses — the latest DCF run, the
    materialized fundamentals cache (falling back to the DB scan), and the
    §Valuation PEG cache — so the Score-chip peek can never disagree with the
    cockpit row it annotates. None when the ticker isn't a tracked, non-archived
    name (the peek route 404s)."""
    t = ticker.strip().upper()
    if not t:
        return None
    try:
        row = conn.execute(
            "SELECT 1 FROM tracked_companies WHERE UPPER(ticker) = ? AND archived_at IS NULL",
            (t,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    _gap, fair_value, dcf_price, _date = latest_dcf_runs(conn).get(t, (None, None, None, None))
    fundamentals = read_materialized_fundamentals(repo_root)
    if not fundamentals:
        fundamentals = _eval_fundamentals(conn)
    rev_yoy, fcf_margin = fundamentals.get(t, (None, None))
    return attractiveness_breakdown(
        dcf_upside_pct=_dcf_upside_pct(fair_value, dcf_price),
        rev_yoy_pct=rev_yoy,
        fcf_margin_pct=fcf_margin,
        peg_ratio=_peg_ratio(repo_root, t),
    )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_cockpit_rows(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, list[CockpitRow]]:
    """Assemble ``{"portfolio": [...], "evaluation": [...]}``. Portfolio is
    attention-sorted (breach first, then pending alerts, then new docs, then
    ticker); evaluation is sorted by next-dollar attractiveness, score
    descending (see :func:`eval_attractiveness`) — for held names "needs me
    today" beats "deserves new money", for screen names it's the reverse.

    Layers batch enrichment queries + per-ticker disk-cache reads over the
    existing :func:`build_dashboard_rows` (which stays the ``/api/dashboard``
    contract).
    """
    ref = now or datetime.now(UTC)
    base_rows = build_dashboard_rows(conn, repo_root)
    tickers = [r.ticker for rows in base_rows.values() for r in rows]
    portfolio_tickers = {r.ticker for r in base_rows.get("portfolio", [])}

    names = _company_names(conn, tickers)
    alerts = _pending_alert_counts(conn)
    dcf = latest_dcf_runs(conn)
    docs = _new_doc_counts(conn)
    evals = _latest_evaluations(conn)
    kpi_facts = _tier1_kpi_deltas(conn, portfolio_tickers, as_of=ref.date())
    # Fast path: precomputed cache written by the morning pipeline (stage 0d).
    # Falls back to the live DB scan when the cache is absent (fresh install,
    # test env, dev run without the morning pipeline).
    fundamentals = read_materialized_fundamentals(repo_root)
    if not fundamentals:
        fundamentals = _eval_fundamentals(conn)
    # Portfolio fit per evaluation name (PR3): a pure disk read of the cache the
    # morning pipeline materialised (stage 0f). Absent (fresh install / no
    # pipeline run) → no Fit chip, exactly like the fundamentals cache.
    candidate_fit = read_materialized_candidate_fit(repo_root)
    # Canonical calendar first (one batch query); the per-ticker FMP-cache file
    # read below remains as fallback for pre-0082 DBs / not-yet-refreshed rows.
    next_er = {t: d.isoformat() for t, d in upcoming_by_ticker(conn, ref.date()).items()}

    out: dict[str, list[CockpitRow]] = {}
    for list_type, rows in base_rows.items():
        built: list[CockpitRow] = []
        for row in rows:
            t = row.ticker
            price, day_move, price_asof = profile_quote(repo_root, t)
            rule_summary, evaluated_at, rule_tones = evals.get(t, (None, None, {}))
            deltas = _toned(kpi_facts.get(t, []), rule_tones) if t in portfolio_tickers else []
            fv_gap, fair_value, dcf_price, dcf_date = dcf.get(t, (None, None, None, None))
            rev_yoy, fcf_margin = fundamentals.get(t, (None, None))
            peg = _peg_ratio(repo_root, t)
            score: float | None = None
            why: str | None = None
            partial = False
            fit: float | None = None
            fit_why: str | None = None
            fit_partial = False
            if list_type == "evaluation":
                upside = _dcf_upside_pct(fair_value, dcf_price)
                score, why, partial = eval_attractiveness(
                    dcf_upside_pct=upside,
                    rev_yoy_pct=rev_yoy,
                    fcf_margin_pct=fcf_margin,
                    peg_ratio=peg,
                )
                cf = candidate_fit.get(t)
                if cf is not None:
                    fit, fit_why, fit_partial = cf.fit, cf.why, cf.partial
            built.append(
                CockpitRow(
                    base=row,
                    name=names.get(t),
                    price=price,
                    day_move_pct=day_move,
                    price_asof=price_asof,
                    fv_gap_pct=fv_gap,
                    fair_value=fair_value,
                    dcf_price=dcf_price,
                    dcf_date=dcf_date,
                    peg_ratio=peg,
                    next_earnings=next_er.get(t) or next_earnings(repo_root, t, ref),
                    pending_alerts=alerts.get(t, 0),
                    new_docs=docs.get(t, 0),
                    kpi_deltas=deltas,
                    rule_summary=rule_summary,
                    evaluated_at=evaluated_at,
                    rev_yoy_pct=rev_yoy,
                    fcf_margin_pct=fcf_margin,
                    attractiveness=score,
                    attractiveness_why=why,
                    attractiveness_partial=partial,
                    fit=fit,
                    fit_why=fit_why,
                    fit_partial=fit_partial,
                )
            )
        if list_type == "evaluation":
            built.sort(
                key=lambda r: (
                    -(r.attractiveness if r.attractiveness is not None else 0.0),
                    r.base.ticker,
                )
            )
        else:
            built.sort(key=lambda r: r.attention_key)
        out[list_type] = built
    return out


def _safe_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[sqlite3.Row]:
    """Run an enrichment query; a missing table/column degrades to no rows
    (hand-rolled test schemas + partial DBs render a sparser cockpit)."""
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return []
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


# _eval_fundamentals is kept as a thin wrapper around cockpit_fundamentals so
# the existing test imports and direct callers continue to work unchanged.
# The build_cockpit_rows render path calls read_materialized_fundamentals (a
# disk read) and only falls back here when no cache file exists.
def _eval_fundamentals(conn: sqlite3.Connection) -> dict[str, tuple[float | None, float | None]]:
    """(rev_yoy_pct, fcf_margin_pct) per ticker — delegates to cockpit_fundamentals.compute_from_db."""
    return _compute_fundamentals(conn)


def _company_names(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    rows = _safe_rows(
        conn,
        f"SELECT ticker, name FROM tracked_companies WHERE ticker IN ({marks})",
        tuple(tickers),
    )
    return {str(r["ticker"]): str(r["name"]) for r in rows if r["name"]}


def _pending_alert_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = _safe_rows(
        conn, "SELECT ticker, COUNT(*) AS n FROM alerts WHERE status='pending' GROUP BY ticker"
    )
    return {str(r["ticker"]): int(r["n"]) for r in rows}


def latest_dcf_runs(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float | None, float | None, float | None, str | None]]:
    """ticker -> (fv_gap_pct, npv_per_share, live_price, valuation_date), latest
    run per ticker. The gap is RECOMPUTED as live_price / npv_per_share − 1
    rather than read from ``over_under_pct`` — the bank/holdco builders stored
    that column in a different convention (percent upside) than refresh_dcf's
    documented ratio, and the row's own two fields are convention-proof and
    currency-consistent with each other. Public: the allocation-decisions
    panel (P2.2) reads the same gap so the two surfaces can never disagree."""
    rows = _safe_rows(
        conn,
        "SELECT ticker, valuation_date, npv_per_share, live_price "
        "FROM dcf_runs ORDER BY ticker, created_at DESC, id DESC",
    )
    out: dict[str, tuple[float | None, float | None, float | None, str | None]] = {}
    for r in rows:
        t = str(r["ticker"])
        if t in out:
            continue
        fv = float(r["npv_per_share"]) if r["npv_per_share"] is not None else None
        px = float(r["live_price"]) if r["live_price"] is not None else None
        gap = (px / fv - 1.0) * 100.0 if fv is not None and fv > 0 and px is not None else None
        out[t] = (gap, fv, px, str(r["valuation_date"]) if r["valuation_date"] else None)
    return out


def latest_dcf_scenarios(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float | None, float | None]]:
    """ticker -> (bull_fv_per_share, bear_fv_per_share) from the latest run's
    bull/base/bear scenario block (``assumption_snapshot_json``), or both None
    when the run has no range. Pairs with ``latest_dcf_runs`` so the sizing audit
    can read the *asymmetry* of the range, not just the base point estimate —
    parsed via the single producer ``dcf.scenario_reward.parse_scenario_fair_values``
    so the card, the next-dollar model and the audit can't diverge. A missing
    ``assumption_snapshot_json`` column (old data dirs) degrades to no scenarios."""
    from dcf.scenario_reward import parse_scenario_fair_values

    rows = _safe_rows(
        conn,
        "SELECT ticker, assumption_snapshot_json FROM dcf_runs "
        "ORDER BY ticker, created_at DESC, id DESC",
    )
    out: dict[str, tuple[float | None, float | None]] = {}
    for r in rows:
        t = str(r["ticker"])
        if t in out:
            continue
        fair_values = parse_scenario_fair_values(r["assumption_snapshot_json"])
        out[t] = (fair_values.get("bull"), fair_values.get("bear"))
    return out


def _new_doc_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Documents fetched after the ticker's last report build. ``julianday``
    bridges the two timestamp spellings in play ('T' vs space separator)."""
    rows = _safe_rows(
        conn,
        "SELECT tc.ticker AS ticker, COUNT(d.id) AS n "
        "FROM tracked_companies tc JOIN documents d ON d.ticker = tc.ticker "
        "WHERE tc.last_built_at IS NOT NULL "
        "  AND julianday(d.fetched_at) > julianday(tc.last_built_at) "
        "GROUP BY tc.ticker",
    )
    return {str(r["ticker"]): int(r["n"]) for r in rows}


def _latest_evaluations(
    conn: sqlite3.Connection,
) -> dict[str, tuple[str | None, str | None, dict[str, str]]]:
    """ticker -> (rule summary for the badge hover, evaluated_at,
    kpi_name -> worst rule status) from the latest thesis evaluation."""
    rows = _safe_rows(
        conn,
        "SELECT ticker, evaluated_at, rule_evaluations_json "
        "FROM thesis_evaluations ORDER BY ticker, evaluated_at DESC",
    )
    out: dict[str, tuple[str | None, str | None, dict[str, str]]] = {}
    for r in rows:
        t = str(r["ticker"])
        if t in out:
            continue
        tones: dict[str, str] = {}
        flagged: dict[str, list[str]] = {"breach": [], "warn": []}
        try:
            rules = json.loads(str(r["rule_evaluations_json"] or "[]"))
        except json.JSONDecodeError:
            rules = []
        if isinstance(rules, list):
            for rule in cast("list[object]", rules):
                if not isinstance(rule, dict):
                    continue
                rec = cast("dict[str, object]", rule)
                kpi = rec.get("kpi_name")
                status = str(rec.get("status") or "").lower()
                if not isinstance(kpi, str) or not kpi:
                    continue
                prev = tones.get(kpi)
                if prev is None or _STATUS_RANK.get(status, 3) < _STATUS_RANK.get(prev, 3):
                    tones[kpi] = status
                if status in flagged and kpi not in flagged[status]:
                    flagged[status].append(kpi)
        parts = [f"{status}: {', '.join(kpis)}" for status, kpis in flagged.items() if kpis]
        summary = " · ".join(parts) if parts else None
        out[t] = (summary, str(r["evaluated_at"]) if r["evaluated_at"] else None, tones)
    return out


def _tier1_kpi_deltas(
    conn: sqlite3.Connection, tickers: set[str], as_of: date | None = None
) -> dict[str, list[KpiDelta]]:
    """Latest-vs-prior move per tier-1 KPI definition (portfolio names only).
    Superseded facts are excluded; tone is stamped later from the evaluation's
    rule statuses (the definitions carry no good-direction signal).

    ``as_of`` (default: no filter) drops facts whose ``period_end`` is in the
    future relative to that date, so a guidance/forecast row stamped with a
    forward period (e.g. a next-FY estimate) cannot masquerade as the latest
    *actual* and skew the move. The render path passes the cockpit's ``now``."""
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    as_of_clause = "  AND date(f.period_end) <= ? " if as_of is not None else ""
    params: tuple[str, ...] = tuple(sorted(tickers))
    if as_of is not None:
        params = (*params, as_of.isoformat())
    rows = _safe_rows(
        conn,
        "SELECT f.ticker AS ticker, d.id AS def_id, d.name AS name, f.unit AS unit, "
        "       f.period_end AS period_end, f.value AS value "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE d.threshold_tier = 'tier_1_break' AND f.ticker IN (" + marks + ") "
        "  AND f.id NOT IN (SELECT supersedes_id FROM kpi_facts "
        "                   WHERE supersedes_id IS NOT NULL) "
        + as_of_clause
        + "ORDER BY f.ticker, d.id, f.period_end DESC",
        params,
    )
    series: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for r in rows:
        key = (str(r["ticker"]), int(r["def_id"]))
        bucket = series.setdefault(key, [])
        if len(bucket) < 2:
            bucket.append(r)
    out: dict[str, list[KpiDelta]] = {}
    for (ticker, _def_id), pair in series.items():
        if len(pair) < 2:
            continue
        latest, prior = pair
        try:
            latest_v, prior_v = float(latest["value"]), float(prior["value"])
        except (TypeError, ValueError):
            continue
        out.setdefault(ticker, []).append(
            KpiDelta(
                name=str(latest["name"]),
                unit=str(latest["unit"] or ""),
                latest_value=latest_v,
                prior_value=prior_v,
                latest_period=str(latest["period_end"])[:10],
                prior_period=str(prior["period_end"])[:10],
                tone="neutral",
            )
        )
    return out


def _toned(deltas: list[KpiDelta], rule_tones: dict[str, str]) -> list[KpiDelta]:
    """Largest movers first, capped, with each chip toned by any break rule that
    references the same KPI name in the latest evaluation."""
    toned = [
        KpiDelta(
            name=d.name,
            unit=d.unit,
            latest_value=d.latest_value,
            prior_value=d.prior_value,
            latest_period=d.latest_period,
            prior_period=d.prior_period,
            tone=_STATUS_TONE.get(rule_tones.get(d.name, ""), "neutral"),
        )
        for d in deltas
    ]
    toned.sort(key=lambda d: (-d.magnitude, d.name))
    return toned[:_MAX_KPI_CHIPS]


# --------------------------------------------------------------------------- #
# Disk caches (read-only; no source_calls logging on a render path)
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def profile_quote(repo_root: Path, ticker: str) -> tuple[float | None, float | None, str | None]:
    """(price, day-move %, as-of) from ``data/historical/fmp/<T>_profile.json``.
    As-of is the file mtime — the price is from the last FMP cycle, which the
    staleness dot already surfaces; a render path must not hit the network.
    Public: the ticker hover mini-card (pipeline.peeks) reads the same quote
    so it can never disagree with the cockpit row it annotates."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    payload = _read_json(path)
    rec: dict[str, object] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        rec = cast("dict[str, object]", payload[0])
    elif isinstance(payload, dict):
        rec = cast("dict[str, object]", payload)
    if rec is None:
        return None, None, None
    raw_price = rec.get("price")
    price = float(raw_price) if isinstance(raw_price, (int, float)) and raw_price > 0 else None
    raw_move = rec.get("changePercentage", rec.get("changesPercentage"))
    move = float(raw_move) if isinstance(raw_move, (int, float)) else None
    asof = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    return price, move, asof


def _peg_ratio(repo_root: Path, ticker: str) -> float | None:
    """PEG from the §Valuation cache (``data/valuation_basis/<T>.json``). A
    tolerant field pluck, not ``compute.valuation_basis.load`` — the cockpit
    must survive cache-shape drift and must not import the LLM compute path."""
    payload = _read_json(repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json")
    if not isinstance(payload, dict):
        return None
    v = cast("dict[str, object]", payload).get("peg_ratio")
    return float(v) if isinstance(v, (int, float)) else None


def next_earnings(repo_root: Path, ticker: str, now: datetime) -> str | None:
    """Earliest calendar date >= today from the FMP earnings-calendar cache.
    Read-only twin of ``sources.earnings_calendar._try_fmp_cache`` minus its
    ``source_calls`` logging (a dashboard render is not a sourcing decision).
    Public: shared with the ticker hover mini-card (pipeline.peeks).
    FALLBACK only: ``build_cockpit_rows`` prefers the ``expected_earnings``
    table; this covers pre-0082 DBs and tickers the refresher hasn't seen."""
    payload = _read_json(
        repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_earnings_calendar.json"
    )
    if not isinstance(payload, list):
        return None
    today = now.date().isoformat()
    upcoming: list[str] = []
    for item in cast("list[object]", payload):
        if not isinstance(item, dict):
            continue
        ds = cast("dict[str, object]", item).get("date")
        if isinstance(ds, str) and len(ds) >= 10 and ds[:10] >= today:
            upcoming.append(ds[:10])
    return min(upcoming) if upcoming else None


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #


def render_research_cockpit(
    rows_by_list: dict[str, list[CockpitRow]],
    *,
    now: datetime | None = None,
) -> str:
    """The cockpit fragment: a full-density Portfolio table, a thinner
    Evaluation table, and the cockpit's own ``<style>`` block (shared tokens
    via ``var(--ok)``/``--warn``/``--bad`` from the shell palette)."""
    ref = now or datetime.now(UTC)
    portfolio = rows_by_list.get("portfolio", [])
    evaluation = rows_by_list.get("evaluation", [])
    return "".join(
        [
            _COCKPIT_CSS,
            _render_list_section("Portfolio", portfolio, ref, thin=False),
            _render_list_section("Evaluation", evaluation, ref, thin=True),
        ]
    )


def _render_list_section(title: str, rows: list[CockpitRow], now: datetime, *, thin: bool) -> str:
    if not rows:
        body = f"<p class='empty'>No {escape(title.lower())} tickers.</p>"
    else:
        head = (
            "<thead><tr>"
            "<th>Ticker</th><th>Thesis</th>"
            # The eval table's sort key leads its numeric columns: next-dollar
            # attractiveness, factor math in each chip's hover.
            + (
                "<th class='num' title='next-dollar attractiveness: DCF upside x Rev growth "
                "x FCF margin x PEG (hover a score for its factor math; dashed = partial "
                "data)'>Score</th>"
                # Sibling to Score: how the name would sit in the HELD book —
                # marginal Sharpe x diversification x factor exposure x sector
                # (>1 accretive, <1 dilutive). Click for the breakdown.
                "<th class='num' title='portfolio fit to the held book: marginal Sharpe "
                "x diversification x factor exposure x sector (>1 accretive, <1 dilutive; "
                "click for the breakdown; dashed = partial data)'>Fit</th>"
                if thin
                else ""
            )
            + ("<th>Tier-1 moves</th>" if not thin else "")
            + "<th class='num'>Price</th><th class='num'>vs DCF FV</th>"
            "<th class='num'>PEG</th>"
            # Eval rows trade the (mostly empty for them) Tier-1 column for
            # screen fundamentals (PR7).
            + (
                "<th class='num' title='latest quarter vs the year-ago quarter'>Rev YoY</th>"
                "<th class='num' title='trailing-twelve-month FCF margin'>FCF mgn</th>"
                if thin
                else ""
            )
            + "<th>Next ER</th><th>Inbox</th>"
            "<th class='dot-col' title='Ops freshness'>&#9679;</th>"
            "</tr></thead>"
        )
        body_rows = "".join(_render_row(r, now, thin=thin) for r in rows)
        cls = "cockpit-table cockpit-thin" if thin else "cockpit-table"
        body = f"<table class='{cls}'>{head}<tbody>{body_rows}</tbody></table>"
    return (
        f"<section class='list-section cockpit-section'>"
        f"<h2>{escape(title)} <span class='count'>({len(rows)})</span></h2>"
        f"{body}</section>"
    )


def _render_row(row: CockpitRow, now: datetime, *, thin: bool) -> str:
    t = escape(row.base.ticker)
    name_attr = f" title='{escape(row.name)}'" if row.name else ""
    cells = [
        f"<td class='ticker'{name_attr}><a href='/ticker/{t}'>{t}</a></td>",
        f"<td>{_verdict_badge(row, now)}</td>",
    ]
    if thin:
        cells.append(f"<td class='num'>{_score_cell(row)}</td>")
        cells.append(f"<td class='num'>{_fit_cell(row)}</td>")
    if not thin:
        cells.append(f"<td class='kpi-moves'>{_kpi_chips(row.kpi_deltas, row.base.ticker)}</td>")
    cells.extend(
        [
            f"<td class='num'>{_price_cell(row)}</td>",
            f"<td class='num'>{_fv_gap_cell(row)}</td>",
            f"<td class='num'>{_peg_cell(row)}</td>",
        ]
    )
    if thin:
        cells.extend(
            [
                f"<td class='num'>{_signed_pct_cell(row.rev_yoy_pct)}</td>",
                f"<td class='num'>{_plain_pct_cell(row.fcf_margin_pct)}</td>",
            ]
        )
    cells.extend(
        [
            f"<td>{_earnings_cell(row, now)}</td>",
            f"<td>{_inbox_cell(row)}</td>",
            f"<td class='dot-col'>{_staleness_dot(row, now)}</td>",
        ]
    )
    return "<tr>" + "".join(cells) + "</tr>"


def _score_cell(row: CockpitRow) -> str:
    """The attractiveness chip — a peek doorway (UX9). A plain click opens the
    factor breakdown (DCF upside · Rev growth · FCF margin · PEG, each band
    multiplier) in the shared popover; the full factor math stays in the hover
    ``title`` as the fast fallback; ``/ticker/<T>`` is the real href for
    middle-click / new tab (mirrors the alert/doc pills and the staleness dot)."""
    if row.attractiveness is None:
        return _muted()
    tone = attractiveness_tone(row.attractiveness)
    cls = "k-chip k-chip-mono" + (" k-chip-ok" if tone == "hi" else "")
    if row.attractiveness_partial:
        cls += " chip-partial"
    title = f" title='{escape(row.attractiveness_why)}'" if row.attractiveness_why else ""
    t = escape(row.base.ticker)
    return (
        f"<a class='{cls}' href='/ticker/{t}' "
        f"data-peek-url='/api/peek/score?ticker={t}' "
        f"data-peek-title='Why score · {t}'{title}>{row.attractiveness:.2f}</a>"
    )


def _fit_cell(row: CockpitRow) -> str:
    """The portfolio-fit chip — a peek doorway (UX9), sibling to the score chip.
    A plain click opens the fit breakdown (marginal Sharpe · diversification ·
    factor exposure · sector) from the materialized cache; the factor math stays
    in the hover ``title``; ``/ticker/<T>`` is the real href for middle-click.
    Centered at 1.0 — ``fit-hi`` accretive to the book, ``fit-lo`` dilutive."""
    if row.fit is None:
        return _muted()
    tone = fit_tone(row.fit)
    cls = "k-chip k-chip-mono"
    if tone == "hi":
        cls += " k-chip-ok"
    elif tone == "lo":
        cls += " k-chip-warn"
    if row.fit_partial:
        cls += " chip-partial"
    title = f" title='{escape(row.fit_why)}'" if row.fit_why else ""
    t = escape(row.base.ticker)
    return (
        f"<a class='{cls}' href='/ticker/{t}' "
        f"data-peek-url='/api/peek/fit?ticker={t}' "
        f"data-peek-title='Portfolio fit · {t}'{title}>{row.fit:.2f}</a>"
    )


def _signed_pct_cell(v: float | None) -> str:
    if v is None:
        return _muted()
    tone = "pos" if v >= 0 else "neg"
    return f"<span class='{tone}'>{escape(fmt_pct(v, signed=True))}</span>"


def _plain_pct_cell(v: float | None) -> str:
    if v is None:
        return _muted()
    tone = "pos" if v >= 0 else "neg"
    return f"<span class='{tone}'>{escape(fmt_pct(v))}</span>"


def _muted(text: str = "&mdash;") -> str:
    return f"<span class='muted'>{text}</span>"


def _verdict_badge(row: CockpitRow, now: datetime) -> str:
    status = row.base.breach_status
    if status is None:
        return _muted()
    tone = _STATUS_TONE.get(status.lower(), "muted")
    bits: list[str] = []
    if row.rule_summary:
        bits.append(row.rule_summary)
    if row.evaluated_at:
        bits.append(f"evaluated {fmt_reltime(row.evaluated_at, now=now)}")
    title = f" title='{escape(' — '.join(bits))}'" if bits else ""
    pill_tone = f" k-pill-{tone}" if tone in ("ok", "warn", "bad") else ""
    return f"<span class='k-pill{pill_tone}'{title}>{escape(status)}</span>"


def _kpi_chips(deltas: list[KpiDelta], ticker: str) -> str:
    """Each tier-1 move is a doorway (Law 2): a ``data-ask-q`` button that opens
    the KPI as a chart in Ask. The question uses RELATIVE-window phrasing ("…,
    last 12 quarters") — the ViewSpec compiler parses period counts, not ISO
    date ranges (nl_compile) — so the period dates live only in the hover
    ``title``, never in the question. The bare-name form (unit parenthetical
    stripped, untruncated) resolves to the right kpi token; the visible chip
    text keeps the 18-char ellipsis."""
    if not deltas:
        return _muted()
    chips: list[str] = []
    for d in deltas:
        metric = d.name.split(" (")[0]
        short = metric if len(metric) <= 18 else metric[:17] + "…"
        title = (
            f"{d.name}: {d.prior_value:g} → {d.latest_value:g} {d.unit} "
            f"({d.prior_period} → {d.latest_period})"
        )
        ask_q = f"{metric} for {ticker}, last 12 quarters"
        chip_tone = f" k-chip-{d.tone}" if d.tone in ("bad", "warn", "ok") else ""
        chips.append(
            f"<button type='button' class='k-chip k-chip-mono kpi-move{chip_tone}' "
            f"data-ask-q='{escape(ask_q, quote=True)}' title='{escape(title)}'>"
            f"{escape(short)} <b>{escape(d.delta_display)}</b></button>"
        )
    return "".join(chips)


def _price_cell(row: CockpitRow) -> str:
    if row.price is None:
        return _muted()
    move = ""
    if row.day_move_pct is not None:
        tone = "pos" if row.day_move_pct >= 0 else "neg"
        move = f" <span class='{tone}'>{escape(fmt_pct(row.day_move_pct, signed=True))}</span>"
    title = (
        f" title='last FMP quote {escape(fmt_reltime(row.price_asof))}'" if row.price_asof else ""
    )
    return f"<span{title}>${row.price:,.2f}{move}</span>"


def _fv_gap_cell(row: CockpitRow) -> str:
    if row.fv_gap_pct is None:
        return _muted()
    # over_under > 0 — price ABOVE fair value (rich); < 0 — below (cheap).
    tone = "neg" if row.fv_gap_pct > 0 else "pos"
    bits: list[str] = []
    if row.fair_value is not None and row.dcf_price is not None:
        bits.append(f"DCF FV ${row.fair_value:,.2f} vs ${row.dcf_price:,.2f}")
    if row.dcf_date:
        bits.append(f"run {row.dcf_date}")
    title = f" title='{escape(' — '.join(bits))}'" if bits else ""
    return f"<span class='{tone}'{title}>{escape(fmt_pct(row.fv_gap_pct, signed=True))}</span>"


def _peg_cell(row: CockpitRow) -> str:
    if row.peg_ratio is None:
        return _muted()
    return f"{row.peg_ratio:.1f}"


def _earnings_cell(row: CockpitRow, now: datetime) -> str:
    if row.next_earnings is None:
        return _muted()
    days = (datetime.fromisoformat(row.next_earnings).date() - now.date()).days
    cls = " class='er-soon'" if days <= 7 else ""
    rel = "today" if days == 0 else f"in {days}d"
    return (
        f"<span{cls} title='{escape(rel)}'>"
        f"{escape(fmt_date(row.next_earnings, include_year=False))}</span>"
    )


def _inbox_cell(row: CockpitRow) -> str:
    pills: list[str] = []
    if row.pending_alerts:
        # data-peek-url: the pill peeks the pending cards in place (UX9); the
        # /feed href stays the real destination for middle-click / new tab.
        t = escape(row.base.ticker)
        pills.append(
            f"<a class='k-pill k-pill-bad cockpit-count' href='/feed?ticker={t}&status=pending' "
            f"data-peek-url='/api/peek/alerts?ticker={t}&status=pending' "
            f"data-peek-title='Pending alerts · {t}' "
            f"title='unreviewed alerts'>{row.pending_alerts} alert"
            f"{'s' if row.pending_alerts != 1 else ''}</a>"
        )
    if row.new_docs:
        # Doorway (Law 2): the pill peeks the documents fetched since the last
        # build in place (the same rows the count was derived from — UX9),
        # mirroring the alerts pill. ``/#holding=`` stays the real href for
        # middle-click / new tab.
        t = escape(row.base.ticker)
        pills.append(
            f"<a class='k-pill k-pill-accent cockpit-count' href='/#holding={t}' "
            f"data-peek-url='/api/peek/documents?ticker={t}' "
            f"data-peek-title='New documents · {t}' "
            f"title='documents fetched since the last report build'>"
            f"{row.new_docs} new doc{'s' if row.new_docs != 1 else ''}</a>"
        )
    if row.base.open_comments_count:
        n = row.base.open_comments_count
        pills.append(
            f"<span class='k-pill k-pill-warn' title='open report comments'>"
            f"{n} comment{'s' if n != 1 else ''}</span>"
        )
    return f"<span class='cell-pills'>{''.join(pills)}</span>" if pills else _muted()


def _staleness_dot(row: CockpitRow, now: datetime) -> str:
    """One dot summarising ops freshness; the per-column detail the old status
    tables carried lives in the hover title. Clicking peeks the per-source
    provenance card (UX9d) — ages + inline refresh — with /#system as the
    real href for middle-click."""
    fmp_age = _age_days(row.base.fmp_last_pulled, now)
    build_age = _age_days(row.base.last_build_at, now)
    tone = "ok"
    if fmp_age is None or build_age is None:
        tone = "bad"
    else:
        if fmp_age > _FMP_WARN_DAYS or build_age > _BUILD_WARN_DAYS:
            tone = "warn"
        if fmp_age > _FMP_BAD_DAYS or build_age > _BUILD_BAD_DAYS:
            tone = "bad"
    detail = [
        f"FMP {fmt_reltime(row.base.fmp_last_pulled, now=now) if row.base.fmp_last_pulled else 'never'}",
        f"build {fmt_reltime(row.base.last_build_at, now=now) if row.base.last_build_at else 'never'}",
    ]
    transcript = row.base.last_transcript
    if transcript and transcript.period_end:
        qa = ""
        if transcript.has_qa_section is True:
            qa = " (Q&A)"
        elif transcript.has_qa_section is False:
            qa = " (no Q&A)"
        detail.append(f"transcript {transcript.period_end}{qa}")
    t = escape(row.base.ticker)
    return (
        f"<a class='stale-dot dot-{tone}' href='/#system' "
        f"data-peek-url='/api/peek/provenance?ticker={t}' "
        f"data-peek-title='Data provenance · {t}' "
        f"title='{escape(' · '.join(detail))}'>&#9679;</a>"
    )


def _age_days(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ref = now if now.tzinfo else now.replace(tzinfo=UTC)
    return (ref - dt).total_seconds() / 86400.0


_COCKPIT_CSS = """
<style>
.cockpit-section h2 { display: flex; align-items: baseline; gap: 6px; }
.cockpit-table td { white-space: nowrap; }
.cockpit-table td.kpi-moves { white-space: normal; }
/* The Evaluation table is secondary to the Portfolio table — caption-tier
   type + tighter padding marks it as the lower-importance grid. */
.cockpit-thin td, .cockpit-thin th { padding: 4px 10px; font-size: var(--fs-caption); }
/* Five reinvented badge/chip systems collapsed onto the kit (controls.py): the
   status verdict → .k-pill (filled tone); the KPI-move / attractiveness-score /
   portfolio-fit doorways → .k-chip-mono (outline mono, + tone); the alert/doc/
   comment counts → .k-pill. Only LAYOUT survives locally: the score/fit chips
   are <a> peek-doorways (reset the anchor underline + give the kit hover); the
   KPI move's inter-chip margin; the count-pill flex row; and the "partial data"
   dashed border (a semantic the kit chip has no tone for). */
a.k-chip { text-decoration: none; }
a.k-chip:hover { color: var(--fg); border-color: var(--border-2); }
.chip-partial { border-style: dashed; }
.kpi-move { margin: 1px 4px 1px 0; }
.cell-pills { display: inline-flex; gap: 4px; flex-wrap: wrap; }
.er-soon { color: var(--warn); font-weight: 600; }
.stale-dot { font-size: var(--fs-micro); cursor: help; }
a.stale-dot { text-decoration: none; cursor: pointer; }
.dot-col { text-align: center; width: 28px; }
.dot-ok { color: var(--ok); }
.dot-warn { color: var(--warn); }
.dot-bad { color: var(--bad); }
td.pos, span.pos { color: var(--ok); }
td.neg, span.neg { color: var(--bad); }
</style>
""".strip()
