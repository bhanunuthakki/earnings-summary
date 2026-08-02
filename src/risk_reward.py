"""Risk-budget allocator (L7): does each position earn the risk it consumes?

Nothing in the book joined risk contribution to reward contribution. Three pure
engines sat one seam apart: ``allocation.book_risk`` (per-name share of total
book risk off a shrunk covariance), the DCF bull/base/bear range
(``dcf.scenario_reward`` — asymmetry-aware expected return on the L6 price-leg-
fresh price), and the owner's recorded conviction (``position_sizing_intent``).
This module joins them into one **risk-parity-gap** ranking: each name's share of
book RISK vs its share of expected REWARD vs the stated CONVICTION, flagging the
mismatches ("22% of book risk but 9% of expected reward, rated 3/5").

Honesty is load-bearing. The reward leg is DCF-dependent, so a name whose DCF is
**missing or stale** is marked *low-confidence* and its gap is shown but NOT
scored as a confident mismatch — a confident-but-wrong flag computed off a stale
fair value is worse than no flag. The conviction-vs-risk signal (B below) does
not touch the DCF, so it still fires honestly on a low-confidence row.

The join is pure (``build_gap_rows`` over already-assembled inputs);
``build_risk_reward_gap`` assembles the inputs (covariance from the price cache,
DCF rows + conviction intents from the DB).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import median

from allocation.book_risk import BookRisk, build_book_risk
from dcf.latest import latest_dcf_rows_from_db
from dcf.scenario_reward import scenario_reward
from identity import DEFAULT_USER_ID
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Ranking thresholds — coarse by design (they rank attention, they don't measure
# anything), each gated on its inputs, each emitting an explainable chip.
GAP_FLAG_PP = 5.0  # risk_share - reward_share (pp) to count as a parity gap
RISK_FLOOR_PP = 6.0  # only flag names carrying a material share of book risk
LOW_CONVICTION = 2.0  # at/below this, conviction is "low"
HIGH_CONVICTION = 4.0  # at/above this, conviction is "high"
CONV_REWARD_MIN_PCT = 5.0  # a high-conviction name the DCF expects less than this from is a tension
# Winsorize the expected return that feeds the reward-SHARE denominator, so one
# deep-value/stale DCF (a +400% point estimate) can't swamp every other name's
# share and manufacture false "over-risked" flags. Mirrors the next-dollar
# model's RET_CLAMP rationale; the true expected return is still displayed.
REWARD_CLAMP = 1.0  # +-100%

# Freshness — the reward leg's confidence. The fair value moves on a fundamentals
# refresh (stale past a month, mirroring dcf_coverage_panel.STALE_DAYS); the price
# leg is re-priced daily by L6, so a price older than a week is itself a tell.
REWARD_STALE_DAYS = 30
PRICE_STALE_DAYS = 7

CONVICTION_KIND = "conviction"


@dataclass(slots=True)
class _Reward:
    """The asymmetry-aware reward leg for one name, with its confidence."""

    expected_return: float | None  # fraction; None when there is no usable DCF reward
    has_scenarios: bool
    low_confidence: bool
    confidence_reason: str | None
    detail: str | None


@dataclass(slots=True)
class RiskRewardGapRow:
    """One position's risk vs reward vs conviction line, scored for attention."""

    ticker: str
    weight_pct: float  # modeled-book weight (renormalized over the covariance names)
    risk_share_pct: float  # share of total book risk (sums to ~100%)
    marginal_vol_ann_pct: float  # annualized marginal vol (waterfall substrate)
    expected_return_pct: float | None  # asymmetric DCF expected return
    reward_share_pct: float | None  # share of the book's measured expected upside
    gap_pct: float | None  # risk_share - reward_share (+ = over-risked for the reward)
    conviction: float | None  # latest recorded conviction (1-5)
    has_scenarios: bool  # the reward used a bull/base/bear range (not just the base point)
    low_confidence: bool  # reward leg is DCF-stale/missing → gap not confidently scored
    confidence_reason: str | None
    reward_detail: str | None
    mismatch_score: float = 0.0
    mismatch_reasons: list[str] = field(default_factory=list[str])


@dataclass(slots=True)
class RiskRewardGap:
    """The ranked risk-parity-gap table + the provenance of how it was built."""

    rows: list[RiskRewardGapRow]
    portfolio_vol_ann: float | None
    weights_source: str  # "tracker" | "equal"
    prices_through: date | None
    cov_obs: int | None
    shrinkage: float | None
    valued_names: int  # names with a usable (non-None) DCF reward
    notes: list[str] = field(default_factory=list[str])
    hidden_reason: str | None = None  # set when no covariance could be built


# --------------------------------------------------------------------------- #
# The pure join + scoring
# --------------------------------------------------------------------------- #


def _score_row(
    *,
    risk_share_pct: float,
    reward_share_pct: float | None,
    gap_pct: float | None,
    expected_return_pct: float | None,
    conviction: float | None,
    low_confidence: bool,
    confidence_reason: str | None,
    median_risk: float,
) -> tuple[float, list[str]]:
    """Score one position's risk/reward/conviction tension. Confident reward
    flags (A, C) are withheld on a low-confidence reward leg; the conviction-vs-
    risk flag (B) does not depend on the DCF and still fires. Every point has a
    chip."""
    pts = 0.0
    reasons: list[str] = []
    confident_reward = reward_share_pct is not None and not low_confidence

    # A. Risk-parity gap — consumes more of the book's risk than of its reward.
    if (
        confident_reward
        and gap_pct is not None
        and gap_pct >= GAP_FLAG_PP
        and (risk_share_pct >= RISK_FLOOR_PP)
    ):
        pts += min(gap_pct, 25.0) * 0.5
        reasons.append(
            f"{risk_share_pct:.0f}% of book risk vs {reward_share_pct:.0f}% of expected reward"
        )

    # B. Conviction undersized vs risk — low conviction carrying a high risk
    # share. Independent of the DCF, so it fires even on a low-confidence row.
    if (
        conviction is not None
        and conviction <= LOW_CONVICTION
        and risk_share_pct >= max(RISK_FLOOR_PP, median_risk)
    ):
        pts += (3.0 - conviction) * 2.0
        reasons.append(f"conviction {conviction:g}/5 but {risk_share_pct:.0f}% of book risk")

    # C. Conviction outruns the DCF — high conviction the valuation doesn't back.
    if (
        confident_reward
        and conviction is not None
        and conviction >= HIGH_CONVICTION
        and expected_return_pct is not None
        and expected_return_pct < CONV_REWARD_MIN_PCT
    ):
        pts += (conviction - 3.0) * 1.5
        reasons.append(
            f"rated {conviction:g}/5 but the DCF expects only {expected_return_pct:+.0f}%"
        )

    # Honest framing: a material-risk name whose reward we can't trust is shown,
    # not silently scored — the gap is named as not-scored rather than computed.
    if low_confidence and risk_share_pct >= RISK_FLOOR_PP and confidence_reason:
        reasons.append(f"reward leg low-confidence ({confidence_reason}); gap not scored")

    return round(pts, 1), reasons


def build_gap_rows(
    book: BookRisk,
    rewards: Mapping[str, _Reward],
    convictions: Mapping[str, float],
) -> tuple[list[RiskRewardGapRow], int]:
    """Join risk shares × reward legs × conviction into the ranked rows.

    Returns ``(rows, valued_names)``. Reward shares distribute the book's
    *measured* expected upside — the sum of positive weight×expected-return
    contributions over the names that carry a DCF reward — so a name without a
    DCF reads as low-confidence with no reward share, not a fabricated zero."""
    contrib: dict[str, float] = {
        t: book.weights.get(t, 0.0) * max(-REWARD_CLAMP, min(REWARD_CLAMP, r.expected_return))
        for t, r in rewards.items()
        if r.expected_return is not None and t in book.weights
    }
    gross_upside = sum(v for v in contrib.values() if v > 0.0)
    risk_shares = {t: book.risk_share.get(t, 0.0) * 100.0 for t in book.tickers}
    median_risk = median(risk_shares.values()) if risk_shares else 0.0

    rows: list[RiskRewardGapRow] = []
    valued = 0
    for t in book.tickers:
        rs = risk_shares[t]
        reward = rewards.get(t) or _Reward(None, False, True, "no DCF on file", None)
        er_pct = reward.expected_return * 100.0 if reward.expected_return is not None else None
        if reward.expected_return is not None:
            valued += 1
        reward_share = (
            contrib[t] / gross_upside * 100.0 if t in contrib and gross_upside > 0.0 else None
        )
        gap = rs - reward_share if reward_share is not None else None
        conviction = convictions.get(t)
        score, reasons = _score_row(
            risk_share_pct=rs,
            reward_share_pct=reward_share,
            gap_pct=gap,
            expected_return_pct=er_pct,
            conviction=conviction,
            low_confidence=reward.low_confidence,
            confidence_reason=reward.confidence_reason,
            median_risk=median_risk,
        )
        rows.append(
            RiskRewardGapRow(
                ticker=t,
                weight_pct=book.weights.get(t, 0.0) * 100.0,
                risk_share_pct=rs,
                marginal_vol_ann_pct=book.marginal_vol_ann.get(t, 0.0) * 100.0,
                expected_return_pct=er_pct,
                reward_share_pct=reward_share,
                gap_pct=gap,
                conviction=conviction,
                has_scenarios=reward.has_scenarios,
                low_confidence=reward.low_confidence,
                confidence_reason=reward.confidence_reason,
                reward_detail=reward.detail,
                mismatch_score=score,
                mismatch_reasons=reasons,
            )
        )
    rows.sort(key=lambda r: (-r.mismatch_score, -r.risk_share_pct, r.ticker))
    return rows, valued


# --------------------------------------------------------------------------- #
# Input assembly (DB + price cache)
# --------------------------------------------------------------------------- #


def _parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def _dcf_reward_legs(db_path: Path, tickers: Sequence[str], today: date) -> dict[str, _Reward]:
    """Asymmetry-aware reward leg per ticker, with freshness-derived confidence.

    Reads the latest TOP-LEVEL (unsegmented, current-version) ``dcf_runs`` row
    per name through the canonical shared reader (``dcf.latest``, PR:
    canonical latest_dcf_run reader) — a segment or superseded row can't win
    this reward leg. The L6 price-leg-fresh ``live_price`` and the value-of-
    record ``npv_per_share`` feed ``dcf.scenario_reward``; ``valuation_date``
    (fair-value leg) and ``live_price_at`` (price leg) drive the low-
    confidence framing. A name with no row simply gets no entry (the caller
    treats it as 'no DCF on file').

    A sanity-flagged run (migration 0182) skips the fair-value leg entirely —
    ``scenario_reward`` is never called, so ``expected_return`` reads None,
    same shape as "no usable price / fair value" — an unreviewed outlier
    valuation must not drive the risk-parity-gap ranking any more than it may
    drive eligibility (``allocation.eligibility``)."""
    want = {t.upper() for t in tickers}
    out: dict[str, _Reward] = {}
    for t, row in latest_dcf_rows_from_db(db_path).items():
        if t not in want:
            continue
        if row.sanity_flag:
            out[t] = _Reward(
                None, False, True, f"DCF sanity-flagged (outlier: {row.sanity_flag!r})", None
            )
            continue
        reward = scenario_reward(
            price=row.live_price,
            base_fv=row.npv_per_share,
            snapshot_json=row.assumption_snapshot_json,
        )
        if reward is None:
            out[t] = _Reward(None, False, True, "DCF has no usable price / fair value", None)
            continue
        val_date = _parse_date(row.valuation_date)
        priced_date = _parse_date(row.live_price_at)
        stale_bits: list[str] = []
        if val_date is None:
            stale_bits.append("fair value undated")
        elif (today - val_date).days > REWARD_STALE_DAYS:
            stale_bits.append(f"fair value {(today - val_date).days}d stale")
        if priced_date is None:
            stale_bits.append("price never recorded")
        elif (today - priced_date).days > PRICE_STALE_DAYS:
            stale_bits.append(f"price {(today - priced_date).days}d stale")
        low_conf = bool(stale_bits)
        out[t] = _Reward(
            expected_return=reward.expected_return,
            has_scenarios=reward.has_scenarios,
            low_confidence=low_conf,
            confidence_reason="; ".join(stale_bits) if stale_bits else None,
            detail=reward.detail,
        )
    return out


def _latest_convictions(db_path: Path, tickers: Sequence[str], user_id: str) -> dict[str, float]:
    """Latest recorded conviction (1–5) per ticker from position_sizing_intent."""
    try:
        from user_state.sizing import list_intents

        intents = list_intents(user_id=user_id, db_path=db_path)
    except (sqlite3.OperationalError, ImportError):
        return {}
    want = {t.upper() for t in tickers}
    out: dict[str, float] = {}
    for row in intents:  # newest-first; first per ticker wins
        t = row.ticker.upper()
        if (
            row.intent_kind == CONVICTION_KIND
            and row.intent_value is not None
            and t in want
            and t not in out
        ):
            out[t] = row.intent_value
    return out


# C6 (2026-07-19 plan): the conviction leg starved when position_sizing_intent
# was empty even though the owner ALREADY typed a conviction at decision time
# (position_entries.entry_conviction). Owner-authored text maps to the 1-5
# scale and backfills any name without a sizing intent — a live read labeled
# "entry record", never an import (per the owner-authored-pre-affirmed ruling
# a copy would be redundant state, and per this module's honesty bar the flag
# text must say which source fired).
_ENTRY_CONVICTION_SCALE: dict[str, float] = {"high": 4.0, "medium": 3.0, "low": 2.0}


def _entry_convictions(db_path: Path, tickers: Sequence[str]) -> dict[str, float]:
    """entry_conviction (text) → 1-5 scale for OPEN position_entries rows.
    Unknown/NULL text and closed positions are skipped; DB errors degrade to
    an empty map (the leg simply stays starved for those names)."""
    want = {t.upper() for t in tickers}
    out: dict[str, float] = {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        try:
            rows = conn.execute(
                "SELECT ticker, entry_conviction FROM position_entries "
                "WHERE exit_date IS NULL AND entry_conviction IS NOT NULL "
                "ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    for ticker, text in rows:
        t = str(ticker or "").upper()
        value = _ENTRY_CONVICTION_SCALE.get(str(text or "").strip().lower())
        if t in want and t not in out and value is not None:
            out[t] = value
    return out


def build_risk_reward_gap(
    db_path: Path,
    repo_root: Path,
    weights: Mapping[str, float],
    *,
    weights_source: str,
    user_id: str = DEFAULT_USER_ID,
    today: date | None = None,
) -> RiskRewardGap:
    """Assemble the risk-parity-gap table for the book described by ``weights``
    (ticker -> fraction of book). ``hidden_reason`` is set (rows empty) when the
    covariance can't be built — fewer than two priced names, too little overlap,
    or a degenerate matrix."""
    today = today or date.today()
    tickers = [t.upper() for t in weights]
    book = build_book_risk(repo_root, tickers, weights)
    if book.hidden_reason is not None:
        return RiskRewardGap(
            rows=[],
            portfolio_vol_ann=None,
            weights_source=weights_source,
            prices_through=None,
            cov_obs=None,
            shrinkage=None,
            valued_names=0,
            notes=[],
            hidden_reason=book.hidden_reason,
        )
    rewards = _dcf_reward_legs(db_path, book.tickers, today)
    intents = _latest_convictions(db_path, book.tickers, user_id)
    entry_fallback = _entry_convictions(db_path, book.tickers)
    # A recorded sizing intent always outranks the entry-time text; the
    # fallback only fills names the intent table has never seen (C6).
    convictions = {**entry_fallback, **intents}
    rows, valued = build_gap_rows(book, rewards, convictions)

    notes: list[str] = []
    fallback_named = sorted(t for t in entry_fallback if t not in intents and t in convictions)
    if fallback_named:
        notes.append(
            "conviction from entry record (no sizing intent yet): " + ", ".join(fallback_named)
        )
    if book.dropped:
        notes.append(
            "outside the covariance: "
            + "; ".join(f"{t} ({reason})" for t, reason in sorted(book.dropped.items()))
        )
    return RiskRewardGap(
        rows=rows,
        portfolio_vol_ann=book.portfolio_vol_ann,
        weights_source=weights_source,
        prices_through=book.prices_through,
        cov_obs=book.cov_obs,
        shrinkage=book.shrinkage,
        valued_names=valued,
        notes=notes,
    )
