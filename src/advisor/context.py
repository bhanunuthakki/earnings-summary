"""Deterministic context assembly for advisor memos (master build P2.3).

Everything the memo prompts cite is computed here, LLM-free, from three
sources of record: the portfolio DB (holdings, verdicts, DCF runs, sizing
intents, open notes), the tracker API (weights, alpha, positioning,
performance — never recomputed client-side), and the holdings JSONs (thesis
one-liners). The same numbers persist into each memo's ``context_json`` so
the P2.5 scorecard can grade what the advisor actually saw.

The swap screen is the deterministic half of the swap-discipline check: per
holding, the best fresh-DCF watchlist/evaluation alternative by upside
margin, with the clearing bar explicit. The LLM memo is only spent on pairs
that clear the bar — the screen itself renders on the Memos panel whether or
not any memo was generated.

DCF upside here is ``fair value / price − 1`` (positive = undervalued),
computed from each run's own npv_per_share + live_price — the same
convention-proof recompute as the cockpit (over_under_pct is never read).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from calibration_guard import is_confident, rate_phrase, rate_phrase_ci
from decision_calibration import (
    CalibrationStats,
    Expectancy,
    bucket_for_conviction,
    build_calibration,
    realized_magnitudes,
)
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import (
    LivePortfolio,
    PortfolioAnalytics,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)
from pipeline.allocation_decisions_panel import (
    SizingAuditRow,
    build_sizing_audit_rows,
    latest_verdicts,
    portfolio_holdings,
)
from pipeline.research_cockpit import latest_dcf_runs
from user_state.notes import list_notes
from user_state.sizing import list_intents

# Candidate lists eligible for the swap screen (index members are tracked but
# unresearched — no thesis, no DCF — so they never enter).
_CANDIDATE_LISTS = ("watchlist", "evaluation")

# Plausibility ceiling on candidate DCF upside. Sweep-built watchlist DCFs
# carry occasional unit/currency artifacts (observed 2026-06-10 on prod: TSM
# +3570%, STNE +519%, JPM +194% — TWD/BRL statements vs USD ADR prices, and
# bank FCFFs the generic builder can't value). A researched name genuinely
# priced at a free double is rarer than a mis-modeled DCF, so candidates above
# the ceiling are excluded from screens/memos and surfaced as a data-quality
# footnote instead of as opportunities.
IMPLAUSIBLE_UPSIDE_PCT = 100.0


@dataclass(frozen=True, slots=True)
class TickerValuation:
    """One ticker's latest usable DCF read."""

    ticker: str
    upside_pct: float  # (npv_per_share / live_price - 1) * 100; + = undervalued
    dcf_date: str | None
    list_type: str
    verdict: str | None


@dataclass(frozen=True, slots=True)
class SwapCandidate:
    """One holding's best external alternative per the deterministic screen."""

    holding: str
    holding_upside_pct: float
    holding_dcf_date: str | None
    candidate: str
    candidate_upside_pct: float
    candidate_dcf_date: str | None
    candidate_list: str
    margin_pp: float
    cleared: bool


@dataclass(slots=True)
class AdvisorContext:
    """Everything one memo run reads, fetched once."""

    repo_root: Path
    audit_rows: list[SizingAuditRow]
    holdings_val: dict[str, TickerValuation]
    candidates_val: dict[str, TickerValuation]
    live: LivePortfolio
    analytics: PortfolioAnalytics
    open_notes_block: str
    generated_at: str
    # The analyst's own graded track record (L1 — the calibration return path);
    # None when the decisions ledger is absent. Defaulted so callers/tests that
    # assemble a context by hand need not supply it.
    calibration: CalibrationStats | None = None


def _dcf_age_ok(dcf_date: str | None, *, max_age_days: int, today: datetime) -> bool:
    if not dcf_date:
        return False
    try:
        run = datetime.fromisoformat(dcf_date[:10]).replace(tzinfo=UTC)
    except ValueError:
        return False
    return (today - run).days <= max_age_days


def load_valuations(
    conn: sqlite3.Connection,
) -> tuple[dict[str, TickerValuation], dict[str, TickerValuation]]:
    """(holdings, candidates) valuation maps from the latest DCF run per ticker,
    joined with each name's tracked list and latest thesis verdict. Tickers
    without a positive fair value AND price are dropped (no usable upside)."""
    verdicts = latest_verdicts(conn)
    try:
        cur = conn.execute(
            "SELECT ticker, list_type FROM tracked_companies "
            "WHERE archived_at IS NULL AND list_type IN ('portfolio', 'watchlist', 'evaluation')"
        )
        lists = {str(r[0]).upper(): str(r[1]) for r in cur.fetchall()}
    except sqlite3.OperationalError:
        lists = {}
    holdings: dict[str, TickerValuation] = {}
    candidates: dict[str, TickerValuation] = {}
    for ticker, (_gap, fv, px, dcf_date) in latest_dcf_runs(conn).items():
        t = ticker.upper()
        list_type = lists.get(t)
        if list_type is None or fv is None or px is None or fv <= 0 or px <= 0:
            continue
        val = TickerValuation(
            ticker=t,
            upside_pct=(fv / px - 1.0) * 100.0,
            dcf_date=dcf_date,
            list_type=list_type,
            verdict=verdicts.get(t),
        )
        if list_type == "portfolio":
            holdings[t] = val
        elif list_type in _CANDIDATE_LISTS:
            candidates[t] = val
    return holdings, candidates


def screen_swap_candidates(
    holdings_val: dict[str, TickerValuation],
    candidates_val: dict[str, TickerValuation],
    *,
    margin_pp: float = 15.0,
    max_dcf_age_days: int = 365,
    max_upside_pct: float = IMPLAUSIBLE_UPSIDE_PCT,
    now: datetime | None = None,
) -> list[SwapCandidate]:
    """The deterministic swap screen: each holding's single best alternative.

    A candidate qualifies when its DCF is fresh (<= ``max_dcf_age_days``), its
    thesis is not breached, and its upside is plausible (<= ``max_upside_pct``
    — see IMPLAUSIBLE_UPSIDE_PCT); the best candidate is the one with the
    highest upside margin over the holding. ``cleared`` marks margins >=
    ``margin_pp`` — the discipline bar a swap must clear before an LLM memo is
    even considered. Sorted by margin, widest first.
    """
    today = now or datetime.now(UTC)
    eligible = [
        c
        for c in candidates_val.values()
        if c.verdict != "breach"
        and c.upside_pct <= max_upside_pct
        and _dcf_age_ok(c.dcf_date, max_age_days=max_dcf_age_days, today=today)
    ]
    best = max(eligible, key=lambda c: c.upside_pct) if eligible else None
    out: list[SwapCandidate] = []
    for h in holdings_val.values():
        if best is None:
            continue
        margin = best.upside_pct - h.upside_pct
        out.append(
            SwapCandidate(
                holding=h.ticker,
                holding_upside_pct=h.upside_pct,
                holding_dcf_date=h.dcf_date,
                candidate=best.ticker,
                candidate_upside_pct=best.upside_pct,
                candidate_dcf_date=best.dcf_date,
                candidate_list=best.list_type,
                margin_pp=margin,
                cleared=margin >= margin_pp,
            )
        )
    out.sort(key=lambda s: -s.margin_pp)
    return out


def build_advisor_context(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
) -> AdvisorContext:
    """Fetch + assemble everything one memo run reads. Tracker failures
    degrade (the established client contract); DB reads tolerate missing
    tables via the underlying readers."""
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        holdings = portfolio_holdings(conn)
        verdicts = latest_verdicts(conn)
        dcf_gaps = latest_dcf_runs(conn)
        holdings_val, candidates_val = load_valuations(conn)
    finally:
        conn.close()
    try:
        intents = list_intents(user_id=user_id, db_path=db_path)
    except (sqlite3.OperationalError, FileNotFoundError, RuntimeError):
        intents = []
    live = fetch_live_portfolio(api_url=api_url)
    # exit_quality is opt-in (the sell-side magnitude for batting-vs-slugging,
    # L-seam 1); the other four are the established advisor reads.
    analytics = fetch_portfolio_analytics(
        api_url=api_url,
        only={"position_alpha", "positioning", "performance", "policy", "exit_quality"},
    )
    audit_rows = build_sizing_audit_rows(
        holdings, verdicts, dcf_gaps, intents, live, analytics.position_alpha
    )
    return AdvisorContext(
        repo_root=repo_root,
        audit_rows=audit_rows,
        holdings_val=holdings_val,
        candidates_val=candidates_val,
        live=live,
        analytics=analytics,
        open_notes_block=open_notes_block(db_path, user_id=user_id),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        # Feed the realized window magnitudes so the calibration block carries
        # the dollar view (win SIZE), not just the hit rate.
        calibration=build_calibration(
            db_path=db_path,
            magnitudes_by_ticker=realized_magnitudes(
                analytics.position_alpha, analytics.exit_quality
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Markdown blocks (what the prompts cite)
# --------------------------------------------------------------------------- #


def _fmt(v: float | None, spec: str = "+.1f", suffix: str = "") -> str:
    return f"{v:{spec}}{suffix}" if v is not None else "?"


def holdings_block(ctx: AdvisorContext) -> str:
    """One line per holding: the sizing audit's columns, prompt-ready."""
    lines: list[str] = ["**Holdings (sizing audit):**"]
    for r in ctx.audit_rows:
        upside = ctx.holdings_val.get(r.ticker)
        bits = [
            f"weight {_fmt(r.weight_pct, '.1f', '%')}",
            f"verdict {r.verdict or '?'}",
            f"conviction {f'{r.conviction:g}/5' if r.conviction is not None else 'unstated'}",
            f"target {f'{r.target_weight_pct:g}%' if r.target_weight_pct is not None else 'unstated'}",
            f"DCF upside {_fmt(upside.upside_pct if upside else None, '+.0f', '%')}",
            f"window alpha {_fmt(r.alpha_usd, '+,.0f')}",
        ]
        if r.mismatch_reasons:
            bits.append(f"tension: {'; '.join(r.mismatch_reasons[:2])}")
        lines.append(f"- **{r.ticker}** — " + " · ".join(bits))
    return "\n".join(lines)


def book_block(ctx: AdvisorContext) -> str:
    """Concentration / sector / performance one-liners from the tracker."""
    lines: list[str] = ["**Book structure (tracker):**"]
    pos = ctx.analytics.positioning
    if pos is not None and pos.concentration is not None:
        c = pos.concentration
        lines.append(
            f"- Concentration: top1 {_fmt(c.top1_weight_pct, '.0f', '%')} · "
            f"top5 {_fmt(c.top5_weight_pct, '.0f', '%')} · "
            f"effective holdings {_fmt(c.effective_holdings, '.1f')}"
        )
    if pos is not None and pos.by_sector:
        top = sorted(pos.by_sector, key=lambda b: -(b.weight_pct or 0.0))[:4]
        lines.append(
            "- Sector tilt: "
            + ", ".join(f"{b.label} {_fmt(b.weight_pct, '.0f', '%')}" for b in top)
        )
    if pos is not None and pos.by_account_type:
        lines.append(
            "- Account mix: "
            + ", ".join(f"{b.label} {_fmt(b.weight_pct, '.0f', '%')}" for b in pos.by_account_type)
        )
    perf = ctx.analytics.performance
    if perf is not None and perf.points:
        last = perf.points[-1]
        lines.append(
            f"- Window TWR {perf.start_date} → {perf.end_date}: portfolio "
            f"{_fmt(last.portfolio_return_pct)}% vs SPY {_fmt(last.spy_return_pct)}% "
            f"vs QQQ {_fmt(last.qqq_return_pct)}%"
        )
    policy = ctx.analytics.policy
    if policy is not None and policy.weights:
        lines.append(
            "- Policy mix: "
            + ", ".join(f"{w.ticker} {_fmt(w.weight_pct, '.0f', '%')}" for w in policy.weights)
        )
    if len(lines) == 1:
        lines.append("- (tracker offline — no live book structure available)")
    return "\n".join(lines)


def candidates_block(ctx: AdvisorContext, *, top_n: int = 8, max_dcf_age_days: int = 365) -> str:
    """Top external names by DCF upside, with thesis one-liners."""
    today = datetime.now(UTC)
    fresh = [
        c
        for c in ctx.candidates_val.values()
        if c.verdict != "breach"
        and c.upside_pct <= IMPLAUSIBLE_UPSIDE_PCT
        and _dcf_age_ok(c.dcf_date, max_age_days=max_dcf_age_days, today=today)
    ]
    fresh.sort(key=lambda c: -c.upside_pct)
    n_implausible = sum(
        1 for c in ctx.candidates_val.values() if c.upside_pct > IMPLAUSIBLE_UPSIDE_PCT
    )
    lines = ["**Top external candidates by DCF upside (watchlist + evaluation, fresh runs):**"]
    for c in fresh[:top_n]:
        thesis = thesis_one_liner(ctx, c.ticker)
        lines.append(
            f"- **{c.ticker}** ({c.list_type}) — upside {c.upside_pct:+.0f}% "
            f"(run {c.dcf_date or '?'}){f' — {thesis}' if thesis else ''}"
        )
    if len(lines) == 1:
        lines.append("- (no fresh external DCF runs)")
    if n_implausible:
        lines.append(
            f"- ({n_implausible} name(s) excluded: DCF upside > +{IMPLAUSIBLE_UPSIDE_PCT:.0f}% "
            "is more likely a mis-modeled DCF than an opportunity)"
        )
    return "\n".join(lines)


_THESIS_CACHE_LIMIT = 140


def thesis_one_liner(ctx: AdvisorContext, ticker: str) -> str:
    path = ctx.repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    thesis = cast("dict[str, object]", payload).get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        return ""
    flat = " ".join(thesis.split())
    return flat[:_THESIS_CACHE_LIMIT] + ("…" if len(flat) > _THESIS_CACHE_LIMIT else "")


def open_notes_block(db_path: Path, *, user_id: str = DEFAULT_USER_ID, char_cap: int = 1800) -> str:
    """All OPEN analyst notes across the book — the portfolio-wide sibling of
    the per-ticker priors anchor. Same framing contract: engage, don't
    re-litigate. Empty string when there are none / the table is missing."""
    try:
        rows = list_notes(user_id=user_id, status="open", limit=40, db_path=db_path)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [
        "**The owner's open notes (engage, don't re-litigate — answer a question "
        "when the data permits, flag confirmation/contradiction of watch-items):**"
    ]
    for r in rows:
        body = " ".join(r.body.split())
        if len(body) > 200:
            body = body[:197].rstrip() + "..."
        scope = r.ticker or "portfolio"
        lines.append(f"- [{scope} · {r.kind}] {body} (since {r.created_at.date().isoformat()})")
    block = "\n".join(lines)
    if len(block) > char_cap:
        block = block[:char_cap].rstrip() + "\n[...truncated]"
    return block


def tax_block(ctx: AdvisorContext, ticker: str) -> str:
    """Tax-placement framing for one holding (context, not an engine): the
    unrealized P&L being realized on a sale and which tax buckets hold it."""
    if not ctx.live.available:
        return ""
    for p in ctx.live.positions:
        if (p.ticker or "").upper() != ticker.upper():
            continue
        treatments = sorted({lot.tax_treatment for lot in p.accounts})
        pnl = (
            f"${p.unrealized_pnl:+,.0f} unrealized"
            if p.unrealized_pnl is not None
            else "unrealized P&L unknown"
        )
        return (
            f"**Tax placement:** selling {ticker.upper()} realizes {pnl}; "
            f"held in: {', '.join(treatments) or 'unknown'} account(s). A taxable-account "
            "sale converts paper gain into a current tax bill — the swap margin must "
            "clear that drag too."
        )
    return ""


def _ticker_conviction_bucket(ctx: AdvisorContext, ticker: str) -> tuple[str, str] | None:
    """The conviction cohort this name's stated stance belongs to (read off the
    sizing audit's conviction column), with a human descriptor. None when no
    conviction is on file for the name."""
    t = ticker.upper()
    for r in ctx.audit_rows:
        if r.ticker == t and r.conviction is not None:
            bucket = bucket_for_conviction(r.conviction)
            return bucket, f"a {bucket}-conviction name (you rate it {r.conviction:g}/5)"
    return None


def calibration_block(ctx: AdvisorContext, ticker: str) -> str:
    """The analyst's own forecasting track record, framed to CHALLENGE the
    conviction on this name — the L1 calibration return path into the advisor.

    Reuses ``decision_calibration`` wholesale plus the shared minimum-n guard,
    so a thin ledger is hedged rather than asserted, and the owner is never
    confronted from a sparse denominator. Empty string when nothing is graded
    yet (the prompts then fall back to evidence alone)."""
    stats = ctx.calibration
    if stats is None or stats.graded == 0:
        return ""
    lines = [
        "**Your documented calibration (challenge this conviction against your own "
        "track record — don't flatter it):**",
        f"- Overall: {rate_phrase('graded calls', stats.overall_hit_rate, stats.graded)}, "
        f"of {stats.total} logged.",
    ]
    # By conviction, each rate widened with its Wilson 95% CI (L-seam 3) — a
    # band, not a point estimate that a thin denominator would flatter.
    conv = [
        rate_phrase_ci(b.conviction, b.hit_rate, b.correct, b.graded)
        for b in stats.by_conviction
        if b.graded
    ]
    if conv:
        lines.append("- By conviction: " + "; ".join(conv) + ".")
    # Proper-scoring Brier on the conviction language itself (L-seam 2) — only
    # asserted once the denominator clears the min-n guard.
    cc = stats.conviction_calibration
    if (
        cc is not None
        and cc.brier is not None
        and cc.baseline_brier is not None
        and is_confident(cc.n)
    ):
        verdict = (
            "your conviction discriminates — it beats a flat base-rate guess"
            if cc.beats_baseline
            else "your conviction does NOT beat a flat base-rate guess — the labels add "
            "little signal over your average"
        )
        lines.append(
            f"- Conviction Brier {cc.brier:.3f} vs {cc.baseline_brier:.3f} baseline "
            f"(n={cc.n}): {verdict}."
        )
    # The dollar view: batting is above, this is slugging (L-seam 1).
    exp_clause = _expectancy_clause(stats.expectancy, lead="Across your graded calls")
    if exp_clause:
        lines.append(f"- {exp_clause}")
    if stats.reversals_vindicated or stats.reversals_cost:
        lines.append(
            f"- Reversals: {stats.reversals_vindicated} vindicated vs "
            f"{stats.reversals_cost} cost — weigh whether overriding your own call "
            "has usually paid off."
        )
    cohort = _ticker_conviction_bucket(ctx, ticker)
    if cohort is not None:
        bucket, descriptor = cohort
        cb = next((b for b in stats.by_conviction if b.conviction == bucket), None)
        if cb is not None and cb.graded:
            line = (
                f"- {ticker.upper()} is {descriptor}: "
                + rate_phrase_ci(
                    f"your {bucket}-conviction calls have graded",
                    cb.hit_rate,
                    cb.correct,
                    cb.graded,
                )
                + ". If that rate undercuts the conviction here, the burden is to say why "
                "THIS one is different."
            )
            cohort_exp = _expectancy_clause(cb.expectancy, lead=f"Those {bucket}-conviction calls")
            if cohort_exp:
                line += f" ({cohort_exp})"
            lines.append(line)
        else:
            lines.append(
                f"- {ticker.upper()} is {descriptor}, but you have no graded "
                f"{bucket}-conviction calls yet (n=0) — one to calibrate going forward."
            )
    return "\n".join(lines)


def _expectancy_clause(exp: Expectancy | None, *, lead: str) -> str:
    """One honest batting-vs-slugging clause (L-seam 1) — the dollar SIZE of the
    wins, not just the hit rate. Empty when there is no magnitude data or the
    denominator is below the min-n guard (a slugging ratio on n=2 is noise)."""
    if exp is None or not is_confident(exp.n):
        return ""
    bits = [f"expected {_signed_money(exp.expectancy)} realized alpha/call"]
    if exp.avg_win is not None and exp.avg_loss is not None:
        slug = f", slugging {exp.slugging:.1f}x" if exp.slugging is not None else ""
        bits.append(
            f"winners avg {_signed_money(exp.avg_win)} vs losers {_signed_money(-exp.avg_loss)}{slug}"
        )
    return f"{lead}: " + "; ".join(bits) + f" (n={exp.n})"


def _signed_money(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def swap_context_json(s: SwapCandidate, *, margin_pp: float) -> dict[str, object]:
    """The screen numbers a swap memo persists for later scoring."""
    d = dict(asdict(s))
    d["margin_bar_pp"] = margin_pp
    return d
