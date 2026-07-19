"""
src/surprise_sources.py
-----------------------
Multi-source intake for EPS / Revenue actual-vs-consensus (earnings "surprise")
records. Designed so the daily pipeline survives an FMP subscription lapse
without losing the headline beat/miss signal.

Source chain (priority order, first hit wins per release_date):

  1. fmp_calendar       Reads `data/historical/fmp/<TICKER>_earnings_calendar.json`
                        already on disk. Full 4 fields: epsActual, epsEstimated,
                        revenueActual, revenueEstimated. ~11 quarters of history
                        per ticker (probed against WIX 2026-05-13).

  2. yfinance           Live pull via `yfinance.Ticker(t).earnings_dates`. EPS
                        estimate vs reported only — yfinance does NOT publish
                        historical revenue actual-vs-estimate. ~25 quarters of
                        EPS history. Free, unauthenticated, fragile but durable.

Revenue widening (estimates-widening phase): Yahoo never publishes HISTORICAL
revenue estimates, but our own point-in-time archive does — every
`execution/fetch_yf_estimates.py` run snapshots the forward revenue consensus
to `data/historical/yfinance_snapshots/<date>/<T>_yf_estimates.json`. For a
reported quarter, the LAST snapshot taken after the quarter ended but before
the release carries that quarter's final 0q revenue consensus. The yfinance
source can therefore enrich its EPS-only hits with revenue actual (Yahoo
quarterly income statement) vs archived estimate — all fields still Yahoo-
sourced (`source_name="yfinance"`), no cross-vendor blending. Quarters
released before the archive started stay revenue-None: honest gap, never
interpolated. (Third-party historical-surprise sources remain a dead end:
stockanalysis.com was probed and does NOT expose a historical surprise table.)

Consumed by `execution/backfill_earnings_surprises.py` which merges per-ticker
hits into `data/surprise/<TICKER>_surprises.json` and registers them downstream.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from models.yf_payloads import YfEstimatesSnapshot

# --- Hit & Source dataclasses ------------------------------------------------


@dataclass(frozen=True)
class SurpriseHit:
    """A single (release_date, ticker) earnings-surprise observation.

    Fields are `Decimal | None` to preserve precision through any downstream
    arithmetic (beat-rate stats, scorecard). `None` distinguishes
    "source didn't publish this field" from "field was zero".
    """

    ticker: str
    release_date: date
    eps_estimate: Decimal | None
    eps_actual: Decimal | None
    revenue_estimate: Decimal | None
    revenue_actual: Decimal | None
    eps_surprise_pct: Decimal | None
    revenue_surprise_pct: Decimal | None
    num_analysts_eps: int | None
    num_analysts_revenue: int | None
    source_name: str
    source_url: str | None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def to_json(self) -> dict[str, object]:
        d = asdict(self)
        # ISO strings for JSON portability
        d["release_date"] = self.release_date.isoformat()
        d["fetched_at"] = self.fetched_at.isoformat()
        for k in (
            "eps_estimate",
            "eps_actual",
            "revenue_estimate",
            "revenue_actual",
            "eps_surprise_pct",
            "revenue_surprise_pct",
        ):
            v = d[k]
            d[k] = str(v) if isinstance(v, Decimal) else None
        return d


@dataclass(frozen=True)
class SurpriseSource:
    """Bulk-fetch source. `fetch_all(ticker)` returns every surprise record the
    source knows about for the ticker; the dispatcher merges by release_date."""

    name: str
    fetch_all: Callable[[str], list[SurpriseHit]]


# --- Decimal coercion + surprise computation --------------------------------


def _to_decimal(v: object) -> Decimal | None:
    """Coerce a JSON value to Decimal. Returns None for null/empty/non-numeric.

    Handles the three shapes seen across sources:
      - Python int/float (from json.loads and yfinance Series.iloc)
      - Numeric str ("1.55") from FMP JSON occasionally
      - None / NaN / empty
    """
    if v is None:
        return None
    if isinstance(v, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(v, (int, float)):
        # Reject NaN/Inf — they propagate poisonously through Decimal arithmetic
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None
    return None


def _surprise_pct(actual: Decimal | None, estimate: Decimal | None) -> Decimal | None:
    """Compute (actual − estimate) / |estimate| × 100, rounded to 2dp.

    Returns None when either input is None or estimate is exactly zero.
    `abs(estimate)` handles negative-EPS cases (loss-making companies) so
    sign of the surprise stays directionally correct.
    """
    if actual is None or estimate is None or estimate == 0:
        return None
    pct = (actual - estimate) / abs(estimate) * Decimal(100)
    return pct.quantize(Decimal("0.01"))


# --- FMP source -------------------------------------------------------------


def fmp_earnings_calendar_records(ticker: str, fmp_dir: Path) -> list[SurpriseHit]:
    """Read `<ticker>_earnings_calendar.json` from `fmp_dir`. Returns [] if the
    file is missing — caller is responsible for falling through to the next
    source."""
    path = fmp_dir / f"{ticker.upper()}_earnings_calendar.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    hits: list[SurpriseHit] = []
    for rec in cast("list[object]", raw):
        if not isinstance(rec, dict):
            continue
        d = cast("dict[str, object]", rec)
        date_str = d.get("date")
        if not isinstance(date_str, str):
            continue
        try:
            release_date = date.fromisoformat(date_str)
        except ValueError:
            continue
        eps_actual = _to_decimal(d.get("epsActual"))
        eps_estimate = _to_decimal(d.get("epsEstimated"))
        rev_actual = _to_decimal(d.get("revenueActual"))
        rev_estimate = _to_decimal(d.get("revenueEstimated"))
        # Skip forward-looking rows where neither actual is reported yet
        if eps_actual is None and rev_actual is None:
            continue
        hits.append(
            SurpriseHit(
                ticker=ticker.upper(),
                release_date=release_date,
                eps_estimate=eps_estimate,
                eps_actual=eps_actual,
                revenue_estimate=rev_estimate,
                revenue_actual=rev_actual,
                eps_surprise_pct=_surprise_pct(eps_actual, eps_estimate),
                revenue_surprise_pct=_surprise_pct(rev_actual, rev_estimate),
                num_analysts_eps=None,
                num_analysts_revenue=None,
                source_name="fmp_calendar",
                source_url=None,
            )
        )
    return hits


# --- yfinance source --------------------------------------------------------

_SNAP_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YF_ESTIMATES_SUFFIX = "_yf_estimates.json"
# A quarter's release lands ~25-45 days after period end; 110 days is a
# generous ceiling that still rejects matching a release to the WRONG quarter.
_MAX_RELEASE_LAG_DAYS = 110


def archived_yf_revenue_estimate(
    ticker: str,
    *,
    quarter_end: date,
    release_date: date,
    snapshots_dir: Path,
) -> tuple[Decimal, int | None, str] | None:
    """The final pre-release 0q revenue consensus from our own point-in-time
    yfinance archive: the LATEST snapshot dated strictly after ``quarter_end``
    and strictly before ``release_date``.

    That window is the only one where Yahoo's "current quarter" (0q) row
    unambiguously refers to the quarter being reported on ``release_date``
    (post-quarter, pre-release). Within-quarter snapshots are deliberately NOT
    used — around the prior release the 0q mapping is ambiguous, and a wrong
    quarter is worse than an honest None.

    Returns ``(avg_estimate, num_analysts, snapshot_date)`` or None (no
    snapshot in the window / no usable 0q row) — never interpolates.
    """
    if not snapshots_dir.exists():
        return None
    dates = sorted(
        (d.name for d in snapshots_dir.iterdir() if d.is_dir() and _SNAP_DATE_RX.match(d.name)),
        reverse=True,
    )
    q_end_s, release_s = quarter_end.isoformat(), release_date.isoformat()
    for snap_date in dates:
        if not (q_end_s < snap_date < release_s):
            continue
        path = snapshots_dir / snap_date / f"{ticker.upper()}{YF_ESTIMATES_SUFFIX}"
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            snap = YfEstimatesSnapshot.model_validate(raw)
        except Exception:
            continue
        row = next((r for r in snap.revenue_estimate if r.period == "0q"), None)
        if row is None or row.avg is None:
            continue
        est = _to_decimal(row.avg)
        if est is None:
            return None
        return est, row.numberOfAnalysts, snap_date
    return None


def _yfinance_quarterly_revenue(ticker: str) -> dict[date, Decimal]:
    """Reported quarterly Total Revenue by period end, via yfinance's
    quarterly income statement — the durable actuals source when FMP has
    lapsed. Degrades to {} on ANY failure (same isolation contract as
    the EPS pull below)."""
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        frame = cast("object", yf.Ticker(ticker.upper()).quarterly_income_stmt)
        loc = getattr(frame, "loc", None)
        if loc is None:
            return {}
        series = cast("object", loc["Total Revenue"])
        items = getattr(series, "items", None)
        if not callable(items):
            return {}
        out: dict[date, Decimal] = {}
        pairs = cast("list[tuple[object, object]]", list(cast("Iterable[object]", items())))
        for ts, value in pairs:
            ts_date = getattr(ts, "date", None)
            if not callable(ts_date):
                continue
            revenue = _to_decimal(value)
            if revenue is not None:
                out[cast("date", ts_date())] = revenue
        return out
    except Exception:
        return {}


def quarter_end_for_release(release_date: date, period_ends: Mapping[date, Decimal]) -> date | None:
    """Map a release date to the fiscal quarter it reported: the latest known
    period end strictly before the release, within the normal reporting lag.
    None when nothing matches — no guessing across long gaps."""
    candidates = [
        pe
        for pe in period_ends
        if pe < release_date and (release_date - pe).days <= _MAX_RELEASE_LAG_DAYS
    ]
    return max(candidates) if candidates else None


def enrich_hits_with_yf_revenue(
    hits: list[SurpriseHit],
    *,
    ticker: str,
    snapshots_dir: Path,
    quarterly_revenue: Mapping[date, Decimal],
) -> list[SurpriseHit]:
    """Fill revenue actual-vs-estimate on yfinance EPS hits from Yahoo-only
    inputs: actual = reported quarterly revenue, estimate = the archived
    pre-release 0q consensus (see ``archived_yf_revenue_estimate``). A hit
    whose quarter can't be mapped, or whose release predates the archive,
    passes through unchanged (honest gap). ``source_name`` stays "yfinance" —
    every enriched field is still Yahoo-sourced, so this is within-vendor
    enrichment, not a cross-source blend."""
    enriched: list[SurpriseHit] = []
    for hit in hits:
        if hit.revenue_actual is not None or hit.revenue_estimate is not None:
            enriched.append(hit)
            continue
        quarter_end = quarter_end_for_release(hit.release_date, quarterly_revenue)
        if quarter_end is None:
            enriched.append(hit)
            continue
        revenue_actual = quarterly_revenue.get(quarter_end)
        archived = archived_yf_revenue_estimate(
            ticker,
            quarter_end=quarter_end,
            release_date=hit.release_date,
            snapshots_dir=snapshots_dir,
        )
        revenue_estimate = archived[0] if archived else None
        num_analysts = archived[1] if archived else None
        if revenue_actual is None and revenue_estimate is None:
            enriched.append(hit)
            continue
        enriched.append(
            replace(
                hit,
                revenue_estimate=revenue_estimate,
                revenue_actual=revenue_actual,
                revenue_surprise_pct=_surprise_pct(revenue_actual, revenue_estimate),
                num_analysts_revenue=num_analysts,
            )
        )
    return enriched


def yfinance_earnings_dates_records(
    ticker: str,
    *,
    yf_snapshots_dir: Path | None = None,
    quarterly_revenue_loader: Callable[[str], dict[date, Decimal]] | None = None,
) -> list[SurpriseHit]:
    """Pull historical EPS surprise via yfinance.Ticker(t).earnings_dates.

    Yahoo publishes no historical revenue actual-vs-estimate, so revenue
    fields default to None. When ``yf_snapshots_dir`` (the point-in-time
    archive written by execution/fetch_yf_estimates.py) is provided, hits are
    enriched with revenue actual-vs-estimate where the archive allows — see
    ``enrich_hits_with_yf_revenue``; quarters the archive never observed stay
    None. Forward-dated rows (estimate present, actual absent) are skipped so
    the cache only contains reported quarters.

    Failure-tolerant: ImportError, network errors, schema changes, and empty
    responses all return []. The dispatcher continues to the next source.
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        return []
    try:
        tkr = yf.Ticker(ticker.upper())
        # getattr (not attribute access) so pyright sees Any|None — yfinance's
        # type annotation claims DataFrame, but the runtime occasionally returns
        # None when Yahoo's analyst-section payload is missing.
        df = getattr(tkr, "earnings_dates", None)
    except Exception:
        # Source is external (Yahoo) — schema changes, rate limits, and DNS
        # failures must NOT bubble out; the dispatcher relies on this isolation.
        return []
    if df is None or len(df) == 0:
        return []
    hits: list[SurpriseHit] = []
    # Iterate rows. yfinance column names: 'EPS Estimate', 'Reported EPS', 'Surprise(%)'
    # Index is a tz-aware DatetimeIndex named "Earnings Date" (release timestamp).
    for ts, row in df.iterrows():  # type: ignore[reportUnknownVariableType]
        try:
            release_date = ts.date()  # type: ignore[reportUnknownMemberType]
        except AttributeError:
            continue
        eps_estimate = _to_decimal(row.get("EPS Estimate"))  # type: ignore[reportUnknownMemberType]
        eps_actual = _to_decimal(row.get("Reported EPS"))  # type: ignore[reportUnknownMemberType]
        # Skip forward-looking rows (no reported EPS yet)
        if eps_actual is None:
            continue
        # yfinance publishes its own surprise%; we recompute to keep the
        # formula consistent across sources (uses abs(estimate) denom).
        hits.append(
            SurpriseHit(
                ticker=ticker.upper(),
                release_date=cast(date, release_date),
                eps_estimate=eps_estimate,
                eps_actual=eps_actual,
                revenue_estimate=None,
                revenue_actual=None,
                eps_surprise_pct=_surprise_pct(eps_actual, eps_estimate),
                revenue_surprise_pct=None,
                num_analysts_eps=None,
                num_analysts_revenue=None,
                source_name="yfinance",
                source_url=f"https://finance.yahoo.com/quote/{ticker.upper()}/analysis",
            )
        )
    if hits and yf_snapshots_dir is not None:
        loader = quarterly_revenue_loader or _yfinance_quarterly_revenue
        hits = enrich_hits_with_yf_revenue(
            hits,
            ticker=ticker,
            snapshots_dir=yf_snapshots_dir,
            quarterly_revenue=loader(ticker),
        )
    return hits


# --- Source registry + dispatcher -------------------------------------------


def default_sources(fmp_dir: Path, *, yf_snapshots_dir: Path | None = None) -> list[SurpriseSource]:
    """Build the standard source chain. `fmp_dir` is injected so worktree-based
    runs can target the main repo's data dir. `yf_snapshots_dir` (optional, the
    yfinance point-in-time archive root) enables revenue enrichment on the
    yfinance fallback — omitted, behavior is unchanged EPS-only."""
    return [
        SurpriseSource(
            name="fmp_calendar",
            fetch_all=lambda t: fmp_earnings_calendar_records(t, fmp_dir),
        ),
        SurpriseSource(
            name="yfinance",
            fetch_all=lambda t: yfinance_earnings_dates_records(
                t, yf_snapshots_dir=yf_snapshots_dir
            ),
        ),
    ]


def _merge_key(d: date) -> date:
    """Merge tolerance: release dates from different sources can differ by ±1
    day (timezone normalization). We round both to the same day for the merge
    pass — finer dedup is unnecessary because no ticker reports twice in a
    given week.

    Currently passes through unchanged; the function exists so a future
    relaxation (e.g. ±2 day window) can be applied in one place.
    """
    return d


def fetch_surprises_with_fallback(
    ticker: str,
    *,
    sources: list[SurpriseSource],
) -> tuple[list[SurpriseHit], list[str]]:
    """Walk sources in priority order, merge by release_date with first-source-wins.

    Returns (merged_hits_sorted_oldest_first, source_names_tried).

    Merge rule: if two sources have records with the same `_merge_key(release_date)`,
    the earlier source's record is kept entirely — we DO NOT field-level merge,
    because mixing estimates across sources (different consensus pools) muddies
    the beat/miss signal. The lower-priority source effectively only contributes
    NEW release dates the higher-priority source missed.
    """
    merged: dict[date, SurpriseHit] = {}
    tried: list[str] = []
    for src in sources:
        tried.append(src.name)
        for hit in src.fetch_all(ticker):
            key = _merge_key(hit.release_date)
            if key not in merged:
                merged[key] = hit
    out = sorted(merged.values(), key=lambda h: h.release_date)
    return out, tried
