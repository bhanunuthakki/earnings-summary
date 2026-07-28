"""Portfolio-need-driven Discovery ranking (PRD §8.2, P1-B).

The 2026-07-19 freeze on the Discovery lane was lifted by owner ruling on
2026-07-23 conditional on exactly this: Discovery earns attention only by
corroborating names the owner is already evaluating and by surfacing
candidates that measurably improve the current book. This module is the
ranking engine that reorders the existing weighted ``score`` (``scoring.py``)
into that portfolio-need lens.

Rank inputs, in the PRD's priority order:

1. **Evaluation-list adjacency** (:func:`eval_adjacency`) — a candidate that
   shares a peer/industry/sector with a name already on the evaluation or
   watchlist corroborates that live diligence; it ranks above a cold name.
2. **Diversifier value** (the ``diversifier`` field) — a COARSE reuse of the
   candidate-fit/ΔSR machinery (``allocation.candidate_fit``) against the
   current book, always labeled ``preliminary`` — a raw Discovery name never
   gets the full evaluation-grade precision that machinery gives a name that
   has cleared onboarding.
3. **GARP consistency** (:func:`garp_grade`) — growth-at-a-reasonable-price:
   a great business at an indefensible multiple ranks below a good business
   at a fair price. FCF yield stands in for a P/E/PEG read (screens.py caches
   no multiple), and the reason string says so.
4. **Source signal strength** — the existing weighted ``score``
   (``scoring.score_candidate``), folded in normalized and de-prioritized
   (weight 0.5 vs adjacency's 1.5) so a signal-heavy but portfolio-irrelevant
   name cannot out-rank a name that actually corroborates live diligence.
5. **Evidence readiness / effort** (:func:`effort_estimate`) — surfaced as a
   chip, not a composite term: effort is about the owner's next click, not
   which name deserves attention first.
6. **First rejection reason** (:func:`first_rejection`) — stated up front,
   also not part of the composite (it is a warning label, not a score).

Everything here is pure and deterministic — no LLM calls, no network beyond
the (already-established) candidate-fit book assembly the caller does ONCE
and passes in. :func:`compute_need_rank` is the one I/O gatherer; every other
function in this module takes already-loaded inputs so the scoring logic
tests without files, a DB, or a network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from discovery.screens import TickerMetrics, load_ticker_metrics
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

Effort = Literal["light", "medium", "heavy"]

# --- calibration knobs (module constants; the guard tests pin that moving a
#     weight moves the rank, not these exact values) -----------------------

#: Composite term weights — the PRD's priority order made numeric. Adjacency's
#: weight is set high enough that ONE adjacency point (2.0 raw, the peer-of-
#: eval bonus) already outweighs the entire max signal contribution
#: (2.0 raw * 0.5 = 1.0), so a corroborating name always outranks a cold,
#: signal-heavy one at equal other legs.
ADJACENCY_WEIGHT: float = 1.5
DIVERSIFIER_WEIGHT: float = 1.0
GARP_WEIGHT: float = 1.0
SIGNAL_WEIGHT: float = 0.5

#: eval_adjacency: per-match contributions, capped.
ADJACENCY_PEER_BONUS: float = 2.0
ADJACENCY_INDUSTRY_BONUS: float = 1.0
ADJACENCY_SECTOR_BONUS: float = 0.5
ADJACENCY_CAP: float = 3.0
#: At most this many reasons surface on the card (priority order: peer,
#: industry, sector — the highest-value match always makes the cut).
ADJACENCY_MAX_REASONS: int = 4

#: garp_grade thresholds (PRD: "rev_yoy>=10 & fcf_yield>=0.03 & roic>=0.08").
GARP_GROWTH_STRONG: float = 0.10
GARP_VALUE_REASONABLE: float = 0.03
GARP_QUALITY_GOOD: float = 0.08
#: Banded-scoring leg thresholds (strong / ok) for the graded-between case.
_GARP_GROWTH_BAND: tuple[float, float] = (0.15, 0.05)
_GARP_VALUE_BAND: tuple[float, float] = (0.05, 0.02)
_GARP_QUALITY_BAND: tuple[float, float] = (0.15, 0.08)
GARP_MAX: float = 2.0

#: diversifier scaling: CandidateFit.fit centers at 1.0 (>1 accretive to the
#: book, <1 dilutive); map the coarse [LOW, HIGH] band onto [0, DIVERSIFIER_MAX]
#: so it sits on the same 0-2 scale as garp/adjacency-per-point.
DIVERSIFIER_SCALE_LOW: float = 0.85
DIVERSIFIER_SCALE_HIGH: float = 1.30
DIVERSIFIER_MAX: float = 2.0

#: signal normalization: a raw weighted score at/above this reference reads
#: as "strong" (normalized to 1.0, i.e. SIGNAL_MAX after the weight).
SIGNAL_NORM_REF: float = 4.0
SIGNAL_MAX: float = 2.0

#: first_rejection screen-gate style thresholds (mirrors screens.py's
#: quality_compounder ND/EBITDA gate).
REJECT_ND_EBITDA_MAX: float = 2.5

#: effort_estimate: enough common trading days to trust the diversifier leg.
EFFORT_LIGHT_MIN_OBS: int = 120


@dataclass(frozen=True, slots=True)
class NeedRank:
    """The portfolio-need decomposition for one candidate — persisted into
    ``discovery_candidates.score_json['need_rank']`` (an augmentation, never a
    new column/table)."""

    eval_adjacency: float
    adjacency_reasons: tuple[str, ...]
    diversifier: float | None
    diversifier_note: str | None
    preliminary: bool
    garp: float
    garp_reason: str
    signal: float
    effort: Effort
    first_rejection_reason: str | None
    composite: float
    version: int = 1


# --------------------------------------------------------------------------- #
# Pure scoring legs
# --------------------------------------------------------------------------- #


def eval_adjacency(
    peers: set[str],
    candidate_sector: str | None,
    candidate_industry: str | None,
    eval_names: dict[str, tuple[str | None, str | None]],
) -> tuple[float, tuple[str, ...]]:
    """Score a candidate's closeness to the active evaluation/watchlist.

    ``eval_names`` maps ticker (upper) -> (sector, industry) for every name on
    the evaluation/watchlist lists. Peers that are themselves under active
    evaluation are the strongest signal (+2 each); an industry match without a
    direct peer hit is next (+1); a sector-only match is weakest (+0.5).
    Capped at ``ADJACENCY_CAP`` and reasons capped at ``ADJACENCY_MAX_REASONS``
    (priority order, so the best evidence always survives the cap)."""
    peer_hits = sorted(t for t in peers if t in eval_names)
    reasons: list[str] = [f"peer of {t} (evaluation)" for t in peer_hits]
    score = ADJACENCY_PEER_BONUS * len(peer_hits)

    industry_hits: list[str] = []
    if candidate_industry:
        industry_hits = sorted(
            t
            for t, (_sec, ind) in eval_names.items()
            if ind == candidate_industry and t not in peer_hits
        )
    score += ADJACENCY_INDUSTRY_BONUS * len(industry_hits)
    reasons.extend(f"same industry as {t}" for t in industry_hits)

    sector_hits: list[str] = []
    if candidate_sector:
        sector_hits = sorted(
            t
            for t, (sec, ind) in eval_names.items()
            if sec == candidate_sector
            and ind != candidate_industry
            and t not in peer_hits
            and t not in industry_hits
        )
    score += ADJACENCY_SECTOR_BONUS * len(sector_hits)
    reasons.extend(f"same sector as {t}" for t in sector_hits)

    return min(score, ADJACENCY_CAP), tuple(reasons[:ADJACENCY_MAX_REASONS])


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _garp_leg(v: float | None, strong: float, ok: float) -> float:
    """One GARP leg's 0..1 contribution — missing data scores 0 (unknown is
    not "reasonable"), never guessed."""
    if v is None:
        return 0.0
    if v >= strong:
        return 1.0
    if v >= ok:
        return 0.55
    if v >= 0.0:
        return 0.25
    return 0.0


def garp_grade(m: TickerMetrics | None) -> tuple[float, str]:
    """Growth-at-a-reasonable-price grade from the cached screen bundle.

    ``fcf_yield_ttm`` stands in for a P/E/PEG multiple — ``screens.py`` caches
    no such multiple — and every reason string says so explicitly so the card
    never implies precision the read doesn't have."""
    if m is None:
        return 0.0, "no cached fundamentals for a GARP read"
    rev, fcf, roic = m.rev_yoy, m.fcf_yield_ttm, m.roic_ttm
    rev_s, fcf_s, roic_s = _pct(rev), _pct(fcf), _pct(roic)

    strong_growth = rev is not None and rev >= GARP_GROWTH_STRONG
    reasonable_value = fcf is not None and fcf >= GARP_VALUE_REASONABLE
    good_quality = roic is not None and roic >= GARP_QUALITY_GOOD

    if strong_growth and reasonable_value and good_quality:
        return GARP_MAX, (
            f"growth at a reasonable FCF yield (rev YoY {rev_s}, "
            f"FCF yield (proxy, no P/E cached) {fcf_s}, ROIC {roic_s})"
        )
    if strong_growth and (fcf is None or fcf < 0.0):
        return 0.5, (
            f"growth without a valuation anchor (rev YoY {rev_s}, FCF yield (proxy) {fcf_s})"
        )
    g = _garp_leg(rev, *_GARP_GROWTH_BAND)
    v = _garp_leg(fcf, *_GARP_VALUE_BAND)
    q = _garp_leg(roic, *_GARP_QUALITY_BAND)
    score = round((g + v + q) / 3.0 * GARP_MAX, 2)
    return score, (
        f"growth-valuation-quality profile: rev YoY {rev_s}, "
        f"FCF yield (proxy, no P/E cached) {fcf_s}, ROIC {roic_s}"
    )


def effort_estimate(has_metrics: bool, price_obs: int | None) -> Effort:
    """Estimated research effort to reach a useful conclusion — MORE cached
    data means LESS owner effort. Not a composite term (the PRD keeps this a
    chip, not a rank input): a thin-history name isn't worth less, it's just
    more work."""
    if has_metrics and price_obs is not None and price_obs >= EFFORT_LIGHT_MIN_OBS:
        return "light"
    if has_metrics or (price_obs is not None and price_obs > 0):
        return "medium"
    return "heavy"


def first_rejection(m: TickerMetrics | None) -> str | None:
    """The first screen-gate style check the candidate would fail, stated
    with the number — or None when nothing obvious is wrong (an evaluation
    still might reject it; this is only the deterministic red flag)."""
    if m is None:
        return None
    if m.nd_to_ebitda_ttm is not None and m.nd_to_ebitda_ttm > REJECT_ND_EBITDA_MAX:
        return (
            f"leverage {m.nd_to_ebitda_ttm:.1f}x ND/EBITDA exceeds the "
            f"{REJECT_ND_EBITDA_MAX:g}x gate"
        )
    if m.rev_yoy is not None and m.rev_yoy_prior is not None and m.rev_yoy < m.rev_yoy_prior:
        return (
            f"revenue growth decelerating ({_pct(m.rev_yoy)} vs "
            f"{_pct(m.rev_yoy_prior)} a year-ago print)"
        )
    if m.op_margin_ttm is not None and m.op_margin_ttm < 0.0:
        return f"operating margin negative ({_pct(m.op_margin_ttm)})"
    return None


def _diversifier_scaled(diversifier: float | None) -> float:
    """Map a raw CandidateFit.fit (~[0.6, 1.4], centered 1.0) onto
    [0, DIVERSIFIER_MAX]; None (not computed) contributes 0, never a guess."""
    if diversifier is None:
        return 0.0
    span = DIVERSIFIER_SCALE_HIGH - DIVERSIFIER_SCALE_LOW
    frac = (diversifier - DIVERSIFIER_SCALE_LOW) / span if span > 0 else 0.0
    return max(0.0, min(1.0, frac)) * DIVERSIFIER_MAX


def _signal_normalized(base_score: float) -> float:
    """Map the existing weighted ``score`` (scoring.py, unbounded above
    ENTRY_THRESHOLD) onto [0, SIGNAL_MAX] so it sits on the same scale as the
    other legs before the composite weight is applied."""
    frac = max(0.0, min(1.0, base_score / SIGNAL_NORM_REF)) if SIGNAL_NORM_REF > 0 else 0.0
    return frac * SIGNAL_MAX


def composite_score(
    *, eval_adj: float, diversifier: float | None, garp: float, base_score: float
) -> float:
    """``1.5*eval_adjacency + 1.0*(diversifier scaled) + 1.0*garp +
    0.5*signal_normalized`` — the PRD's priority order made numeric (module
    docstring). ``diversifier=None`` (not yet computed for this candidate)
    contributes 0, never a guess."""
    return round(
        ADJACENCY_WEIGHT * eval_adj
        + DIVERSIFIER_WEIGHT * _diversifier_scaled(diversifier)
        + GARP_WEIGHT * garp
        + SIGNAL_WEIGHT * _signal_normalized(base_score),
        4,
    )


# --------------------------------------------------------------------------- #
# I/O gatherer
# --------------------------------------------------------------------------- #


def _load_peers(fmp_dir: Path, ticker: str) -> set[str]:
    """``{T}_peers.json`` (``save_fmp_data.py``'s ``stock-peers`` cache) as an
    upper-cased ticker set. Payload shapes shift across FMP versions
    (mirrors ``report.sections.p3_data._fmp_peer_pool``'s tolerance); []/absent
    degrades to an empty set, never a crash."""
    path = fmp_dir / f"{ticker.upper()}_peers.json"
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(parsed, list) or not parsed:
        return set()
    raw = cast("list[object]", parsed)
    first = raw[0]
    if isinstance(first, dict):
        rec = cast("dict[str, object]", first)
        if "peersList" in rec:
            lst = rec.get("peersList")
            if isinstance(lst, list):
                return {str(p).upper() for p in cast("list[object]", lst)}
            return set()
        out: set[str] = set()
        for entry in raw:
            if isinstance(entry, dict):
                erec = cast("dict[str, object]", entry)
                if erec.get("symbol") is not None:
                    out.add(str(erec["symbol"]).upper())
        return out
    return {str(p).upper() for p in raw if isinstance(p, str)}


def load_eval_names(
    db_path: Path, fmp_dir: Path, *, user_id: str = DEFAULT_USER_ID
) -> dict[str, tuple[str | None, str | None]]:
    """``{ticker: (sector, industry)}`` for every active evaluation/watchlist
    name — the adjacency leg's corroboration set. Best-effort: a missing DB
    or table degrades to ``{}`` (adjacency then scores 0 for everyone, never a
    crash)."""
    if not db_path.exists():
        return {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT ticker FROM tracked_companies WHERE user_id = ? "
            "AND list_type IN ('evaluation', 'watchlist') AND archived_at IS NULL",
            (user_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, tuple[str | None, str | None]] = {}
    for r in rows:
        t = str(r[0]).upper()
        m = load_ticker_metrics(fmp_dir, t, None)
        out[t] = (m.sector, m.industry)
    return out


def _price_obs_count(repo_root: Path, ticker: str) -> int | None:
    """Trading-day observation count from the local price cache — cheap
    (read + len), used only for the effort estimate, not a covariance read."""
    from allocation.price_history import load_daily_closes

    closes = load_daily_closes(ticker, repo_root)
    return len(closes) if closes else None


def compute_diversifier(
    repo_root: Path,
    ticker: str,
    book: object,
    *,
    sector: str | None,
) -> tuple[float | None, str | None]:
    """The coarse diversifier leg: one candidate through the existing
    candidate-fit machinery against an already-assembled ``book``
    (``allocation.candidate_fit.BookContext``, passed as ``object`` here to
    keep this module import-light for callers that never compute the
    diversifier leg). Never raises — a price-history/covariance edge case
    degrades to ``(None, None)`` (mirrors the precedent in
    ``allocation.recommendation`` / ``allocation.model`` for this exact class
    of "never crash the ranker over a numeric edge case")."""
    from allocation.candidate_fit import BookContext, compute_candidate_fit

    if not isinstance(book, BookContext):  # pragma: no cover - defensive
        return None, None
    try:
        fits = compute_candidate_fit(
            repo_root,
            [ticker],
            book,
            sectors={ticker: sector} if sector else {},
        )
    except Exception:  # precedent: allocation.model / allocation.recommendation
        return None, None
    fit = fits.get(ticker.upper())
    if fit is None:
        return None, None
    note = f"fit {fit.fit:.2f}x vs the current book" + (" (partial read)" if fit.partial else "")
    return fit.fit, note


def compute_need_rank(
    repo_root: Path,
    db_path: Path,
    ticker: str,
    base_score: float,
    *,
    book: object = None,
    eval_names: dict[str, tuple[str | None, str | None]] | None = None,
    coarse_fit: bool = False,
    user_id: str = DEFAULT_USER_ID,
) -> NeedRank:
    """Gather every input and score one candidate's ``NeedRank``.

    ``eval_names`` and ``book`` are cheap to reuse across every candidate in
    one discovery run — pass them in (computed once by the caller) rather
    than letting every call re-derive them. The diversifier leg only runs
    when ``coarse_fit=True`` AND a ``book`` (``BookContext``) is supplied —
    callers gate this to the interim top ~25 by cost (a price-history read
    per name)."""
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    metrics = load_ticker_metrics(fmp_dir, ticker, None)
    has_metrics = metrics.roic_ttm is not None or metrics.rev_yoy is not None

    peers = _load_peers(fmp_dir, ticker)
    names = (
        eval_names if eval_names is not None else load_eval_names(db_path, fmp_dir, user_id=user_id)
    )
    adj_score, adj_reasons = eval_adjacency(peers, metrics.sector, metrics.industry, names)

    garp_score, garp_reason = garp_grade(metrics if has_metrics else None)

    diversifier: float | None = None
    diversifier_note: str | None = None
    if coarse_fit and book is not None:
        diversifier, diversifier_note = compute_diversifier(
            repo_root, ticker, book, sector=metrics.sector
        )

    price_obs = _price_obs_count(repo_root, ticker)
    effort = effort_estimate(has_metrics, price_obs)
    reject = first_rejection(metrics if has_metrics else None)

    composite = composite_score(
        eval_adj=adj_score, diversifier=diversifier, garp=garp_score, base_score=base_score
    )

    return NeedRank(
        eval_adjacency=adj_score,
        adjacency_reasons=adj_reasons,
        diversifier=diversifier,
        diversifier_note=diversifier_note,
        preliminary=diversifier is not None,
        garp=garp_score,
        garp_reason=garp_reason,
        signal=round(base_score, 4),
        effort=effort,
        first_rejection_reason=reject,
        composite=composite,
        version=1,
    )


def need_rank_to_json(rank: NeedRank) -> dict[str, object]:
    """``NeedRank`` -> the JSON shape persisted at ``score_json['need_rank']``."""
    return {
        "v": rank.version,
        "eval_adjacency": rank.eval_adjacency,
        "adjacency_reasons": list(rank.adjacency_reasons),
        "diversifier": rank.diversifier,
        "diversifier_note": rank.diversifier_note,
        "preliminary": rank.preliminary,
        "garp": rank.garp,
        "garp_reason": rank.garp_reason,
        "signal": rank.signal,
        "effort": rank.effort,
        "first_rejection_reason": rank.first_rejection_reason,
        "composite": rank.composite,
    }


def _as_float(v: object, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _as_int(v: object, default: int = 1) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def need_rank_from_json(raw: object) -> NeedRank | None:
    """The inverse of :func:`need_rank_to_json`; ``None`` on anything that
    isn't a well-formed need_rank blob (an old candidate's score_json simply
    lacks the key, and the panel falls back to the legacy score)."""
    if not isinstance(raw, dict):
        return None
    d = cast("dict[str, object]", raw)
    try:
        effort_raw = str(d.get("effort") or "medium")
        if effort_raw not in ("light", "medium", "heavy"):
            effort_raw = "medium"
        effort: Effort = effort_raw
        reasons_raw = d.get("adjacency_reasons")
        reasons = (
            tuple(str(r) for r in cast("list[object]", reasons_raw))
            if isinstance(reasons_raw, list)
            else ()
        )
        diversifier = d.get("diversifier")
        return NeedRank(
            eval_adjacency=_as_float(d.get("eval_adjacency")),
            adjacency_reasons=reasons,
            diversifier=float(diversifier) if isinstance(diversifier, (int, float)) else None,
            diversifier_note=(
                str(d["diversifier_note"]) if d.get("diversifier_note") is not None else None
            ),
            preliminary=bool(d.get("preliminary", False)),
            garp=_as_float(d.get("garp")),
            garp_reason=str(d.get("garp_reason") or ""),
            signal=_as_float(d.get("signal")),
            effort=effort,
            first_rejection_reason=(
                str(d["first_rejection_reason"])
                if d.get("first_rejection_reason") is not None
                else None
            ),
            composite=_as_float(d.get("composite")),
            version=_as_int(d.get("v")),
        )
    except (TypeError, ValueError):
        return None
