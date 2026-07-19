"""Stance scorecard (master build P2.5) — grade matured advisor memos.

The scoring job walks ``advisor_memos`` rows still ``pending`` whose horizon
has matured (``created_at + horizon_days <= now``) and grades each against
what actually happened:

* **Socratic** — the forced stance vs the ticker's dividend-adjusted price
  return over the horizon. SPY-relative when the tracker's benchmark series
  is reachable (``benchmark_basis='tracker_spy'`` — the cashflow-matched
  synthetic-SPY return from ``/api/portfolio/performance``, reusing the
  tracker's benchmark math per the directive), absolute otherwise
  (``'absolute'``, recorded honestly). Verdict rules, deliberately coarse:
  bullish stances (buy/add) are correct when the graded return clears
  +2pp, wrong below -2pp, mixed inside the deadband; bearish (trim/sell)
  mirrored; hold is correct unless the graded return is worse than -5pp
  (a hold is a decision NOT to trim — it fails only when staying put
  clearly cost something).
* **Swap check** — the SCREEN graded mechanically: the candidate's return
  minus the holding's over the horizon (``screen_validated`` /
  ``screen_refuted`` with the realized margin). The memo never directed a
  swap, so no stance is imputed.
* **Next-dollar** (§5.3a, tenet-2 Phase 5) — mechanical, mirroring swap
  check's style: the top-ranked pick in the memo's persisted
  ``context_json.model_rows`` (``advisor.memos._next_dollar_model_rows``)
  graded against SPY/absolute (``screen_validated``/``screen_refuted``). A
  memo persisted before ``model_rows`` existed is ``unscoreable`` with an
  explicit "legacy" reason — never guessed from prose.
* **Position review / guard override** (§5.3b) — a ``position_review`` memo
  whose ``context_json.verdict_source == 'guard_override'`` is graded as its
  own explicit hold at the horizon (``detail.guard_override = True``), so the
  behavioral guard's forced holds accrue a separately identifiable track
  record rather than being folded, unmarked, into the generic Socratic path.

A memo the run cannot grade YET — horizon immature, the price cache not
covering the window end, tracker down for a tracker-basis grade — simply
stays pending; the job is idempotent over the pending set and the weekly
cron retries.

Price source: ``data/historical/fmp/<T>_price_chart_10y_div_adj.json``
(newest-first daily rows with ``adjClose``). Per-ticker price LOOKUPS are
not "return math rebuilt" — benchmark math stays with the tracker.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from advisor.store import AdvisorMemoRow, insert_stance_score, list_memos, mark_scored
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import fetch_portfolio_analytics

log = logging.getLogger(__name__)

# Verdict deadband (percentage points) for directional stances.
DIRECTIONAL_DEADBAND_PP = 2.0
# A hold fails only below this (staying put clearly cost vs the basis).
HOLD_FLOOR_PP = -5.0

_BULLISH = ("buy", "add")
_BEARISH = ("trim", "sell")


@dataclass(slots=True)
class ScoreOutcome:
    """One scoring attempt's outcome (or deferral)."""

    memo_id: int
    scored: bool
    verdict: str | None = None
    defer_reason: str | None = None


def price_return_from_cache(
    repo_root: Path, ticker: str, start_date: str, end_date: str
) -> float | None:
    """Dividend-adjusted close-to-close return (%) between the last trading
    days at-or-before each date. None when the cache is missing, doesn't
    reach back to ``start_date``, or hasn't caught up to ``end_date`` yet
    (defer — never grade a window the data doesn't cover)."""
    path = (
        repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_price_chart_10y_div_adj.json"
    )
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list) or not rows:
        return None
    closes: dict[str, float] = {}
    newest = oldest = None
    for raw in cast("list[object]", rows):
        if not isinstance(raw, dict):
            continue
        rec = cast("dict[str, object]", raw)
        d, c = rec.get("date"), rec.get("adjClose")
        if not isinstance(d, str) or not isinstance(c, (int, float)) or c <= 0:
            continue
        closes[d[:10]] = float(c)
        newest = max(newest or d[:10], d[:10])
        oldest = min(oldest or d[:10], d[:10])
    if not closes or newest is None or oldest is None:
        return None
    if newest < end_date or oldest > start_date:
        return None  # cache doesn't cover the window — defer

    def at_or_before(target: str) -> float | None:
        best: str | None = None
        for d in closes:
            if d <= target and (best is None or d > best):
                best = d
        return closes[best] if best else None

    start_px, end_px = at_or_before(start_date), at_or_before(end_date)
    if start_px is None or end_px is None or start_px <= 0:
        return None
    return (end_px / start_px - 1.0) * 100.0


def spy_return_from_tracker(
    start_date: str, end_date: str, *, api_url: str | None = None
) -> float | None:
    """SPY benchmark return (%) over the window, from the tracker's
    ``/performance`` series (cashflow-matched synthetic SPY book — the
    tracker owns ALL benchmark math). None when the tracker is down or the
    series carries no SPY points for the window."""
    analytics = fetch_portfolio_analytics(
        api_url=api_url, start_date=start_date, end_date=end_date, only={"performance"}
    )
    perf = analytics.performance
    if perf is None or not perf.points:
        return None
    return next(
        (p.spy_return_pct for p in reversed(perf.points) if p.spy_return_pct is not None), None
    )


def grade_directional(stance: str, graded_return_pp: float) -> str:
    """Deadband verdict for a directional stance against its graded return
    (excess vs SPY when available, absolute otherwise)."""
    if stance in _BULLISH:
        signed = graded_return_pp
    elif stance in _BEARISH:
        signed = -graded_return_pp
    elif stance == "hold":
        return "correct" if graded_return_pp >= HOLD_FLOOR_PP else "wrong"
    else:  # pragma: no cover — schema CHECK keeps stances in vocabulary
        return "mixed"
    if signed > DIRECTIONAL_DEADBAND_PP:
        return "correct"
    if signed < -DIRECTIONAL_DEADBAND_PP:
        return "wrong"
    return "mixed"


def _window(memo: AdvisorMemoRow) -> tuple[str, str, int]:
    horizon = memo.horizon_days or 90
    start = memo.created_at.date()
    end = start + timedelta(days=horizon)
    return start.isoformat(), end.isoformat(), horizon


def _next_dollar_model_rows_of(memo: AdvisorMemoRow) -> list[object] | None:
    """The persisted ``model_rows`` list off a next_dollar memo's context, or
    None when absent/empty/malformed — a legacy memo (persisted before
    ``advisor.memos._next_dollar_model_rows`` existed) has nothing structured
    to grade."""
    raw_rows = (memo.context or {}).get("model_rows")
    if isinstance(raw_rows, list) and raw_rows:
        return cast("list[object]", raw_rows)
    return None


def _score_next_dollar(
    repo_root: Path,
    memo: AdvisorMemoRow,
    raw_rows: list[object],
    *,
    start_date: str,
    end_date: str,
    horizon: int,
    api_url: str | None,
    db: Path,
) -> ScoreOutcome:
    """Mechanical grade for a next_dollar memo (§5.3a): the top-ranked pick's
    (``context_json.model_rows[0]``, persisted by
    ``advisor.memos._next_dollar_model_rows``) realized return vs SPY
    (``tracker_spy``) or absolute, mirroring ``swap_check``'s pair-relative
    style — ``screen_validated`` when the pick cleared the basis,
    ``screen_refuted`` otherwise. Caller (``score_memo``) has already verified
    ``raw_rows`` is a non-empty list and that the horizon has matured.
    """
    top_raw = raw_rows[0]
    top_ticker: str | None = None
    top_upside: float | None = None
    if isinstance(top_raw, dict):
        top = cast("dict[str, object]", top_raw)
        ticker_val = top.get("ticker")
        if isinstance(ticker_val, str) and ticker_val:
            top_ticker = ticker_val
        upside_val = top.get("upside_pct")
        if isinstance(upside_val, (int, float)) and not isinstance(upside_val, bool):
            top_upside = float(upside_val)

    if top_ticker is None:
        insert_stance_score(
            memo_id=memo.id,
            user_id=memo.user_id,
            verdict="unscoreable",
            benchmark_basis="none",
            horizon_days=horizon,
            start_date=start_date,
            end_date=end_date,
            detail={"reason": "top model_rows entry has no ticker"},
            db_path=db,
        )
        mark_scored(memo.id, status="unscoreable", db_path=db)
        return ScoreOutcome(memo_id=memo.id, scored=True, verdict="unscoreable")

    top_ret = price_return_from_cache(repo_root, top_ticker, start_date, end_date)
    if top_ret is None:
        return ScoreOutcome(
            memo_id=memo.id,
            scored=False,
            defer_reason=f"price cache doesn't cover {top_ticker} {start_date}->{end_date}",
        )
    spy_ret = spy_return_from_tracker(start_date, end_date, api_url=api_url)
    if spy_ret is not None:
        basis, margin = "tracker_spy", top_ret - spy_ret
    else:
        basis, margin = "absolute", top_ret
    verdict = "screen_validated" if margin > 0 else "screen_refuted"
    insert_stance_score(
        memo_id=memo.id,
        user_id=memo.user_id,
        verdict=verdict,
        benchmark_basis=basis,
        horizon_days=horizon,
        start_date=start_date,
        end_date=end_date,
        ticker=top_ticker,
        ticker_return_pct=top_ret,
        benchmark_return_pct=spy_ret,
        excess_return_pct=(top_ret - spy_ret) if spy_ret is not None else None,
        detail={
            "reason": "top-ranked next_dollar pick vs SPY/absolute",
            "top_ticker": top_ticker,
            "margin_pp": round(margin, 2),
            "upside_pct": top_upside,
        },
        db_path=db,
    )
    mark_scored(memo.id, status="scored", db_path=db)
    return ScoreOutcome(memo_id=memo.id, scored=True, verdict=verdict)


def score_memo(
    repo_root: Path,
    memo: AdvisorMemoRow,
    *,
    now: datetime | None = None,
    api_url: str | None = None,
    db_path: Path | None = None,
) -> ScoreOutcome:
    """Grade one pending memo, or defer. Writes the stance_scores row +
    flips ``score_status`` when a grade (or unscoreable verdict) lands."""
    db = db_path or (repo_root / "data" / "portfolio.db")
    today = (now or datetime.now(UTC)).date().isoformat()

    if memo.kind == "next_dollar":
        raw_rows = _next_dollar_model_rows_of(memo)
        if raw_rows is None:
            # Legacy memo, persisted before model_rows existed — nothing ever
            # becomes gradeable for it, so (matching the pre-§5.3a posture)
            # it's marked unscoreable immediately rather than sitting in the
            # pending queue until a horizon that will never help it matures.
            insert_stance_score(
                memo_id=memo.id,
                user_id=memo.user_id,
                verdict="unscoreable",
                benchmark_basis="none",
                horizon_days=memo.horizon_days or 90,
                detail={
                    "reason": (
                        "legacy next_dollar memo predates the persisted model_rows "
                        "structure (§5.3a) — nothing structured to grade"
                    )
                },
                db_path=db,
            )
            mark_scored(memo.id, status="unscoreable", db_path=db)
            return ScoreOutcome(memo_id=memo.id, scored=True, verdict="unscoreable")
        start_date, end_date, horizon = _window(memo)
        if end_date > today:
            return ScoreOutcome(
                memo_id=memo.id, scored=False, defer_reason=f"horizon matures {end_date}"
            )
        return _score_next_dollar(
            repo_root, memo, raw_rows, start_date=start_date, end_date=end_date,
            horizon=horizon, api_url=api_url, db=db,
        )  # fmt: skip

    start_date, end_date, horizon = _window(memo)
    if end_date > today:
        return ScoreOutcome(
            memo_id=memo.id, scored=False, defer_reason=f"horizon matures {end_date}"
        )

    if not memo.ticker:
        insert_stance_score(
            memo_id=memo.id,
            user_id=memo.user_id,
            verdict="unscoreable",
            benchmark_basis="none",
            horizon_days=horizon,
            start_date=start_date,
            end_date=end_date,
            detail={"reason": "no ticker on a scoreable kind"},
            db_path=db,
        )
        mark_scored(memo.id, status="unscoreable", db_path=db)
        return ScoreOutcome(memo_id=memo.id, scored=True, verdict="unscoreable")

    ticker_ret = price_return_from_cache(repo_root, memo.ticker, start_date, end_date)
    if ticker_ret is None:
        return ScoreOutcome(
            memo_id=memo.id,
            scored=False,
            defer_reason=f"price cache doesn't cover {memo.ticker} {start_date}->{end_date}",
        )

    if memo.kind == "swap_check":
        if not memo.counter_ticker:
            verdict, counter_ret = "unscoreable", None
            detail: dict[str, object] = {"reason": "swap memo without counter_ticker"}
        else:
            counter_ret = price_return_from_cache(
                repo_root, memo.counter_ticker, start_date, end_date
            )
            if counter_ret is None:
                return ScoreOutcome(
                    memo_id=memo.id,
                    scored=False,
                    defer_reason=f"price cache doesn't cover {memo.counter_ticker}",
                )
            realized_margin = counter_ret - ticker_ret
            verdict = "screen_validated" if realized_margin > 0 else "screen_refuted"
            detail = {"realized_margin_pp": round(realized_margin, 2)}
        insert_stance_score(
            memo_id=memo.id,
            user_id=memo.user_id,
            verdict=verdict,
            benchmark_basis="none",  # pair-relative; no index benchmark involved
            horizon_days=horizon,
            start_date=start_date,
            end_date=end_date,
            ticker=memo.ticker,
            counter_ticker=memo.counter_ticker,
            ticker_return_pct=ticker_ret,
            counter_return_pct=counter_ret,
            detail=detail,
            db_path=db,
        )
        mark_scored(
            memo.id, status="scored" if verdict != "unscoreable" else "unscoreable", db_path=db
        )
        return ScoreOutcome(memo_id=memo.id, scored=True, verdict=verdict)

    # position_review / guard_override (§5.3b): the behavioral guard's forced
    # hold IS a stance — score it exactly like any hold, at its horizon, but
    # tag the detail so this memo's grade is separately identifiable as the
    # guard's OWN track record (never silently folded, indistinguishable, into
    # the generic Socratic path below). Without this explicit branch a
    # guard_override review is graded correctly by luck of stance="hold" being
    # set at persist time, but with no marker letting a downstream reader
    # (the decision journal, the calibration scorecard) single out "how has
    # the guard's hold actually done" as its own question.
    memo_context = memo.context or {}
    if memo.kind == "position_review" and memo_context.get("verdict_source") == "guard_override":
        spy_ret = spy_return_from_tracker(start_date, end_date, api_url=api_url)
        if spy_ret is not None:
            basis, graded = "tracker_spy", ticker_ret - spy_ret
        else:
            basis, graded = "absolute", ticker_ret
        verdict = grade_directional("hold", graded)
        insert_stance_score(
            memo_id=memo.id,
            user_id=memo.user_id,
            verdict=verdict,
            benchmark_basis=basis,
            horizon_days=horizon,
            start_date=start_date,
            end_date=end_date,
            ticker=memo.ticker,
            ticker_return_pct=ticker_ret,
            benchmark_return_pct=spy_ret,
            excess_return_pct=(ticker_ret - spy_ret) if spy_ret is not None else None,
            detail={
                "stance": "hold",
                "graded_return_pp": round(graded, 2),
                "guard_override": True,
                "reason": "behavioral guard forced hold — graded as the guard's own track record",
            },
            db_path=db,
        )
        mark_scored(memo.id, status="scored", db_path=db)
        return ScoreOutcome(memo_id=memo.id, scored=True, verdict=verdict)

    # Socratic: stance vs (excess over tracker-SPY, else absolute).
    if memo.stance is None:
        insert_stance_score(
            memo_id=memo.id,
            user_id=memo.user_id,
            verdict="unscoreable",
            benchmark_basis="none",
            horizon_days=horizon,
            start_date=start_date,
            end_date=end_date,
            ticker=memo.ticker,
            ticker_return_pct=ticker_ret,
            detail={"reason": "no stance line was parsed from the memo"},
            db_path=db,
        )
        mark_scored(memo.id, status="unscoreable", db_path=db)
        return ScoreOutcome(memo_id=memo.id, scored=True, verdict="unscoreable")

    spy_ret = spy_return_from_tracker(start_date, end_date, api_url=api_url)
    if spy_ret is not None:
        basis, graded = "tracker_spy", ticker_ret - spy_ret
    else:
        basis, graded = "absolute", ticker_ret
    verdict = grade_directional(memo.stance, graded)
    insert_stance_score(
        memo_id=memo.id,
        user_id=memo.user_id,
        verdict=verdict,
        benchmark_basis=basis,
        horizon_days=horizon,
        start_date=start_date,
        end_date=end_date,
        ticker=memo.ticker,
        ticker_return_pct=ticker_ret,
        benchmark_return_pct=spy_ret,
        excess_return_pct=(ticker_ret - spy_ret) if spy_ret is not None else None,
        detail={"stance": memo.stance, "graded_return_pp": round(graded, 2)},
        db_path=db,
    )
    mark_scored(memo.id, status="scored", db_path=db)
    return ScoreOutcome(memo_id=memo.id, scored=True, verdict=verdict)


@dataclass(slots=True)
class ScoreRunResult:
    """One scoring run's tally."""

    scored: int = 0
    deferred: int = 0
    outcomes: list[ScoreOutcome] | None = None


def run_scoring(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    now: datetime | None = None,
    api_url: str | None = None,
) -> ScoreRunResult:
    """Grade every pending memo that can be graded today. Never raises on a
    single memo's failure — it defers and the weekly cron retries."""
    db = repo_root / "data" / "portfolio.db"
    try:
        memos = [
            m
            for m in list_memos(user_id=user_id, limit=500, db_path=db)
            if m.score_status == "pending"
        ]
    except (sqlite3.OperationalError, FileNotFoundError, RuntimeError):
        return ScoreRunResult(outcomes=[])
    outcomes: list[ScoreOutcome] = []
    for memo in memos:
        try:
            outcomes.append(score_memo(repo_root, memo, now=now, api_url=api_url, db_path=db))
        except Exception as exc:
            log.warning({"event": "stance_scoring_failed", "memo_id": memo.id, "error": str(exc)})
            outcomes.append(
                ScoreOutcome(memo_id=memo.id, scored=False, defer_reason=f"error: {exc}")
            )
    return ScoreRunResult(
        scored=sum(1 for o in outcomes if o.scored),
        deferred=sum(1 for o in outcomes if not o.scored),
        outcomes=outcomes,
    )
