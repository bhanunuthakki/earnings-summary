"""Advisor memo generators (master build P2.3): next-dollar + swap discipline.

Two memo kinds, one persistence contract:

* **Next-dollar** (`generate_next_dollar_memo`) — the portfolio-level
  allocation memo: where would incremental capital do the most work, argued
  from the sizing audit, valuation gaps, tracker book structure, and the
  owner's open notes. Runs on demand (Memos panel button) and from the
  monthly cron.
* **Swap checks** (`run_swap_checks`) — the deterministic screen
  (``context.screen_swap_candidates``) finds holdings whose best fresh-DCF
  watchlist/evaluation alternative clears them by the margin bar; only
  cleared pairs spend an LLM call, each producing a discipline memo asking
  whether the apparent edge survives what the screen can't see.

Advisor posture (locked in the directive): every prompt forbids directives —
memos are evidence + framing. Per-holding *stances* exist only via the
Socratic flow (P2.4); these memos persist with ``stance=NULL``.

Memory everywhere: each memo writes (1) an ``advisor_memos`` row — the
durable, scoreable record; (2) an ``analyst_notes`` row (source='advisor')
so the priors anchor carries the memo's one-line conclusion into every later
prompt; and (3), for ticker-scoped memos, a ``thesis_ledger_entries`` row
(entry_kind='advisor_memo') so the decisions timeline shows it.

Failure policy follows the repo's LLM call-exception taxonomy
(``llm.cli.is_hard_stop``): budget/setup hard stops PROPAGATE; transient
call failures degrade to a skipped MemoResult with the reason recorded —
one flaky call never aborts a multi-memo run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from advisor.context import (
    AdvisorContext,
    SwapCandidate,
    book_block,
    build_advisor_context,
    candidates_block,
    holdings_block,
    open_notes_block,
    screen_swap_candidates,
    swap_context_json,
    tax_block,
    thesis_one_liner,
)
from advisor.store import insert_memo, link_memory
from identity import DEFAULT_USER_ID
from llm.cli import is_hard_stop
from llm_client import call_llm
from user_state.ledger import append_entry
from user_state.notes import create_note

log = logging.getLogger(__name__)

NEXT_DOLLAR_PURPOSE = "advisor_next_dollar"
SWAP_CHECK_PURPOSE = "advisor_swap_check"

# Default scoring horizon stamped on every memo (P2.5 grades each memo's
# named tickers against price/prints this many days out).
DEFAULT_HORIZON_DAYS = 90


@dataclass(slots=True)
class MemoResult:
    """Outcome of one memo attempt — generated or skipped-with-reason."""

    ok: bool
    kind: str
    memo_id: int | None = None
    ticker: str | None = None
    counter_ticker: str | None = None
    title: str | None = None
    skipped_reason: str | None = None


_NEXT_DOLLAR_PROMPT = """You are the owner's investment thinking partner, writing the periodic
"next-dollar" allocation memo over their actual book. You NEVER instruct —
no position sizes, no orders, no "you should". Evidence and framing only:
"the case for X is..., the case against is..., what to verify first is...".
The owner decides.

{book}

{holdings}

{candidates}

{notes}

Write a 500-750 word memo with exactly these sections:

## Where the next dollar works hardest
2-3 candidate destinations for incremental capital — existing holdings to
deepen or external names from the candidate table. For each: the case FOR
(cite the specific numbers above: valuation gap, stated-vs-actual sizing
tension, alpha), the case AGAINST (what would make this wrong), and the one
thing to verify before acting.

## What the book is telling you
1-2 structural observations from the data — concentration, sector tilt,
policy drift, tax placement. Observations, not instructions.

## Engaging your open notes
If any open question or watch-item above can be advanced with the data at
hand, do it explicitly and name the note. If none can, say so in one line.

Voice: terse, PM-to-PM, every claim anchored to a number from the context.
No hedging boilerplate; flag genuine uncertainty specifically."""


_SWAP_CHECK_PROMPT = """You are the owner's investment thinking partner running a swap-discipline
check: does swapping out of {holding} into {candidate} clear a HIGH bar, or
is the apparent edge illusory? You NEVER instruct — evidence + framing; the
owner decides. A swap memo that says "the evidence does not clear the bar"
is a fully successful outcome.

**The deterministic screen** (DCF upside = fair value / price - 1, each from
the run's own price):
- {holding}: upside {holding_upside:+.0f}% (run {holding_dcf_date})
- {candidate} ({candidate_list}): upside {candidate_upside:+.0f}% (run {candidate_dcf_date})
- Margin: {margin:+.0f}pp against a clearing bar of {bar:.0f}pp

**The holding today:**
{holding_line}

{tax}

{holding_thesis}

{candidate_thesis}

{notes}

Write a 350-550 word memo with exactly these sections:

## The screen's case
Restate the margin and what it implies, in one short paragraph.

## What the screen can't see
The 2-3 strongest reasons the margin overstates the edge: DCF assumption
asymmetry (whose fair value is built on more aggressive inputs?), thesis
quality and durability difference, evidence depth (how long has each name
been researched?), and anything the owner's notes flag.

## What would have to be true to swap
The specific, checkable facts that would justify acting on this margin.

## Where the evidence leans
One paragraph. State which way the evidence points and the 1-2 facts that
would settle it — framing, not a directive."""


def _memo_summary_line(body_md: str, cap: int = 200) -> str:
    """First substantive prose line of the memo — the note-sized conclusion."""
    for raw in body_md.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-", "*", ">", "|")):
            continue
        return line[:cap]
    return body_md.strip().replace("\n", " ")[:cap]


def persist_memo(
    *,
    db_path: Path,
    user_id: str,
    kind: str,
    ticker: str | None,
    counter_ticker: str | None,
    title: str,
    body_md: str,
    context: dict[str, object],
    write_ledger: bool,
    stance: str | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> MemoResult:
    """The memory-everywhere write path: memo row -> advisor note -> (ticker
    memos) ledger entry -> backlinks. Note/ledger failures degrade — the memo
    row is the system of record and must survive a partial memory write.
    Public: the Socratic flow (P2.4) persists through the same path, passing
    the parsed stance + owner-chosen horizon."""
    memo = insert_memo(
        user_id=user_id,
        kind=kind,
        ticker=ticker,
        counter_ticker=counter_ticker,
        title=title,
        body_md=body_md,
        context=context,
        stance=stance,
        horizon_days=horizon_days,
        db_path=db_path,
    )
    note_id: int | None = None
    ledger_id: int | None = None
    summary = _memo_summary_line(body_md)
    try:
        note = create_note(
            user_id=user_id,
            ticker=ticker,
            kind="observation",
            body=f"[advisor memo #{memo.id} · {kind}] {title} — {summary}",
            source="advisor",
            source_ref=f"advisor_memo:{memo.id}",
            context={"memo_id": memo.id, "kind": kind},
            db_path=db_path,
        )
        note_id = note.id
    except Exception as exc:
        log.warning({"event": "advisor_note_write_failed", "memo_id": memo.id, "error": str(exc)})
    if write_ledger and ticker:
        try:
            entry = append_entry(
                user_id=user_id,
                ticker=ticker,
                entry_kind="advisor_memo",
                body=f"{title} (memo #{memo.id}) — {summary}",
                db_path=db_path,
            )
            ledger_id = entry.id
        except Exception as exc:
            log.warning(
                {"event": "advisor_ledger_write_failed", "memo_id": memo.id, "error": str(exc)}
            )
    if note_id is not None or ledger_id is not None:
        link_memory(memo.id, note_id=note_id, ledger_entry_id=ledger_id, db_path=db_path)
    return MemoResult(
        ok=True,
        kind=kind,
        memo_id=memo.id,
        ticker=ticker,
        counter_ticker=counter_ticker,
        title=title,
    )


def generate_next_dollar_memo(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    ctx: AdvisorContext | None = None,
    force_budget_bypass: bool = False,
) -> MemoResult:
    """Generate + persist the next-dollar allocation memo. Hard stops (budget
    cap with block mode, missing CLI) propagate; transient LLM failures return
    a skipped MemoResult."""
    db_path = repo_root / "data" / "portfolio.db"
    context = ctx or build_advisor_context(repo_root, user_id=user_id, api_url=api_url)
    prompt = _NEXT_DOLLAR_PROMPT.format(
        book=book_block(context),
        holdings=holdings_block(context),
        candidates=candidates_block(context),
        notes=context.open_notes_block or "(no open notes)",
    )
    try:
        body = call_llm(
            prompt,
            purpose=NEXT_DOLLAR_PURPOSE,
            scope="portfolio",
            force_budget_bypass=force_budget_bypass,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "next_dollar_llm_failed", "error": str(exc)})
        return MemoResult(
            ok=False, kind="next_dollar", skipped_reason=f"{type(exc).__name__}: {exc}"
        )
    if not body.strip():
        return MemoResult(ok=False, kind="next_dollar", skipped_reason="empty LLM response")
    title = f"Next-dollar memo · {context.generated_at[:10]}"
    memo_context: dict[str, object] = {
        "generated_at": context.generated_at,
        "holdings": [
            {
                "ticker": r.ticker,
                "weight_pct": r.weight_pct,
                "verdict": r.verdict,
                "conviction": r.conviction,
                "target_weight_pct": r.target_weight_pct,
                "mismatch_score": r.mismatch_score,
            }
            for r in context.audit_rows
        ],
        "candidate_upsides": {t: v.upside_pct for t, v in sorted(context.candidates_val.items())},
        "holding_upsides": {t: v.upside_pct for t, v in sorted(context.holdings_val.items())},
    }
    return persist_memo(
        db_path=db_path,
        user_id=user_id,
        kind="next_dollar",
        ticker=None,
        counter_ticker=None,
        title=title,
        body_md=body,
        context=memo_context,
        write_ledger=False,  # portfolio-level: the ledger is ticker-keyed by schema
    )


def run_swap_checks(
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    margin_pp: float = 15.0,
    max_memos: int = 2,
    max_dcf_age_days: int = 365,
    ctx: AdvisorContext | None = None,
    force_budget_bypass: bool = False,
) -> list[MemoResult]:
    """Run the swap-discipline screen and memo the pairs that clear the bar.

    Returns one MemoResult per attempted pair (possibly empty when nothing
    clears — the normal, healthy outcome). Pairs are walked widest-margin
    first; each holding and each candidate gets at most one memo per run, and
    at most ``max_memos`` LLM calls are spent.
    """
    db_path = repo_root / "data" / "portfolio.db"
    context = ctx or build_advisor_context(repo_root, user_id=user_id, api_url=api_url)
    screen = screen_swap_candidates(
        context.holdings_val,
        context.candidates_val,
        margin_pp=margin_pp,
        max_dcf_age_days=max_dcf_age_days,
    )
    cleared = [s for s in screen if s.cleared]
    results: list[MemoResult] = []
    seen_holdings: set[str] = set()
    seen_candidates: set[str] = set()
    for pair in cleared:
        if len(results) >= max_memos:
            break
        if pair.holding in seen_holdings or pair.candidate in seen_candidates:
            continue
        seen_holdings.add(pair.holding)
        seen_candidates.add(pair.candidate)
        results.append(
            _swap_memo(
                context,
                pair,
                db_path=db_path,
                user_id=user_id,
                margin_pp=margin_pp,
                force_budget_bypass=force_budget_bypass,
            )
        )
    return results


def _holding_audit_line(ctx: AdvisorContext, ticker: str) -> str:
    for r in ctx.audit_rows:
        if r.ticker == ticker.upper():
            bits = [
                f"weight {f'{r.weight_pct:.1f}%' if r.weight_pct is not None else '?'}",
                f"verdict {r.verdict or '?'}",
                (
                    f"conviction {r.conviction:g}/5"
                    if r.conviction is not None
                    else "conviction unstated"
                ),
                (
                    f"window alpha {r.alpha_usd:+,.0f}"
                    if r.alpha_usd is not None
                    else "window alpha ?"
                ),
            ]
            return f"- {r.ticker}: " + " · ".join(bits)
    return f"- {ticker}: (no audit row)"


def _thesis_section(ctx: AdvisorContext, ticker: str, label: str) -> str:
    line = thesis_one_liner(ctx, ticker)
    return f"**{label} thesis:** {line}" if line else f"**{label} thesis:** (none recorded)"


def _swap_memo(
    ctx: AdvisorContext,
    pair: SwapCandidate,
    *,
    db_path: Path,
    user_id: str,
    margin_pp: float,
    force_budget_bypass: bool,
) -> MemoResult:
    notes_h = open_notes_block(db_path, user_id=user_id, char_cap=900)
    prompt = _SWAP_CHECK_PROMPT.format(
        holding=pair.holding,
        candidate=pair.candidate,
        holding_upside=pair.holding_upside_pct,
        holding_dcf_date=pair.holding_dcf_date or "?",
        candidate_upside=pair.candidate_upside_pct,
        candidate_dcf_date=pair.candidate_dcf_date or "?",
        candidate_list=pair.candidate_list,
        margin=pair.margin_pp,
        bar=margin_pp,
        holding_line=_holding_audit_line(ctx, pair.holding),
        tax=tax_block(ctx, pair.holding) or "(tracker offline — no tax-placement context)",
        holding_thesis=_thesis_section(ctx, pair.holding, pair.holding),
        candidate_thesis=_thesis_section(ctx, pair.candidate, pair.candidate),
        notes=notes_h or "(no open notes)",
    )
    try:
        body = call_llm(
            prompt,
            purpose=SWAP_CHECK_PURPOSE,
            ticker=pair.holding,
            force_budget_bypass=force_budget_bypass,
        )
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning(
            {
                "event": "swap_check_llm_failed",
                "pair": f"{pair.holding}->{pair.candidate}",
                "error": str(exc),
            }
        )
        return MemoResult(
            ok=False,
            kind="swap_check",
            ticker=pair.holding,
            counter_ticker=pair.candidate,
            skipped_reason=f"{type(exc).__name__}: {exc}",
        )
    if not body.strip():
        return MemoResult(
            ok=False,
            kind="swap_check",
            ticker=pair.holding,
            counter_ticker=pair.candidate,
            skipped_reason="empty LLM response",
        )
    title = (
        f"Swap check · {pair.holding} vs {pair.candidate} "
        f"({pair.margin_pp:+.0f}pp vs {margin_pp:.0f}pp bar)"
    )
    return persist_memo(
        db_path=db_path,
        user_id=user_id,
        kind="swap_check",
        ticker=pair.holding,
        counter_ticker=pair.candidate,
        title=title,
        body_md=body,
        context=swap_context_json(pair, margin_pp=margin_pp),
        write_ledger=True,
    )
