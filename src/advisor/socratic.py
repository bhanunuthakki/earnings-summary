"""Socratic think-through (master build P2.4) — the only path to a stance.

The locked advisor posture: per-holding stances exist ON REQUEST only, and
only through this flow. It is deliberately two-step and owner-first:

1. **Questions** (`generate_questions`) — 3-5 pointed questions to the
   owner, grounded in the holding's actual numbers (thesis + bear anchors,
   sizing/alpha/valuation row, open notes). The questions must draw out the
   owner's CURRENT read, their horizon, and what would make them wrong —
   the memo is written from their answers, never around them.
2. **Decision memo** (`generate_decision_memo`) — after the owner answers,
   the one-page memo in the locked template: bull / bear /
   what-would-change-my-mind / stance-if-forced, with tracker
   sizing-and-alpha context. The stance line is parsed and persisted
   structurally (``advisor_memos.stance`` + ``horizon_days``,
   ``score_status='pending'``) so the P2.5 scorecard can grade it against
   subsequent prints and price. The Q&A transcript is appended to the memo
   body — the owner's own words are part of the record.

Persistence rides the P2.3 contract (memo row + advisor note + ledger
entry + backlinks). Failure policy likewise: hard stops propagate,
transient failures degrade to a skipped MemoResult.

The flow is STATELESS between the two steps: the questions ride the form
round-trip (no session table), so an abandoned think-through leaves no
debris and the memo step re-fetches fresh context at write time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from advisor.context import AdvisorContext, build_advisor_context, calibration_block
from advisor.memos import MemoResult, persist_memo
from advisor.store import STANCES
from identity import DEFAULT_USER_ID
from llm.cli import is_hard_stop
from llm_client import call_llm

log = logging.getLogger(__name__)

QUESTIONS_PURPOSE = "advisor_socratic_questions"
MEMO_PURPOSE = "advisor_socratic_memo"

MIN_QUESTIONS, MAX_QUESTIONS = 3, 5
DEFAULT_SOCRATIC_HORIZON_DAYS = 90


@dataclass(slots=True)
class SocraticPrelude:
    """Step 1's output: the questions + the context block they were grounded
    in (echoed into the memo step so both steps argue from the same facts)."""

    ticker: str
    questions: list[str]
    context_block: str


_QUESTIONS_PROMPT = """You are the owner's investment thinking partner, opening a Socratic
think-through on their holding {ticker}. Your ONLY output is questions —
no analysis, no answers, no preamble. The owner's answers will drive a
decision memo, so the questions must surface what only the owner knows.

{context}

{anchors}

{premortem}

Ask exactly {min_q}-{max_q} questions, numbered "1." through "{max_q}.", one per
line. Required coverage:
- their CURRENT read on the name (what do they believe the market is
  missing, in their own words),
- their intended HORIZON for this position,
- what specific, observable evidence would make them WRONG.

When the context includes a "documented calibration" block, make at least one
question challenge this conviction against the analyst's own track record — if
this name sits in a conviction cohort that has graded poorly, ask directly what
makes THIS call different from the ones that missed.

When a "Pre-mortem" block is present, make at least one question force the owner
to confront the STRONGEST parallel it draws to a bet he got wrong — ask directly
why THIS name escapes the failure that parallel names, and whether he will commit
to the drafted early-warning condition.

Make every question pointed and grounded in a number or fact from the
context above (cite it in the question). Never ask anything the data
already answers. No sub-questions, no preamble, no closing text."""


_MEMO_PROMPT = """You are the owner's investment thinking partner, writing the one-page
Socratic decision memo on {ticker}. The owner has answered your questions —
their answers are the spine of this memo; engage them directly, quote them
where it sharpens the point, and push back where the data disagrees.

{context}

{anchors}

**The think-through transcript:**
{qa_transcript}

Write the memo with exactly these sections:

## Bull
The strongest case FOR, in 2-3 evidence-anchored points (cite the numbers).

## Bear
The strongest case AGAINST, same discipline. Engage the owner's stated
read — where could THEY specifically be wrong? Challenge this conviction
against the analyst's documented calibration above: if the conviction cohort
this name sits in has historically graded poorly, name that track record here
rather than reasoning around it.

## What would change my mind
The 2-3 specific, observable triggers (a print, a KPI level, a competitive
event) that should flip the view — each checkable within the owner's
stated horizon.

## Stance if forced
One short paragraph weighing the above AGAINST the owner's answers and the
sizing context. Then end the memo with a single line in exactly this form:

STANCE: <one of buy | add | hold | trim | sell>

The stance is forced — pick one; "it depends" is not available. This memo
will be scored against the next {horizon_days} days of prints and price,
so commit to the call the evidence supports."""


def _ticker_context_block(ctx: AdvisorContext, ticker: str) -> str:
    """The holding's sizing-and-alpha row + valuation, prompt-ready."""
    t = ticker.upper()
    lines: list[str] = [f"**{t} — sizing and alpha context (tracker + models):**"]
    for r in ctx.audit_rows:
        if r.ticker != t:
            continue
        lines.append(
            f"- Weight {f'{r.weight_pct:.1f}%' if r.weight_pct is not None else '?'} of book · "
            f"thesis verdict {r.verdict or '?'} · "
            f"stated conviction {f'{r.conviction:g}/5' if r.conviction is not None else 'unstated'} · "
            f"target {f'{r.target_weight_pct:g}%' if r.target_weight_pct is not None else 'unstated'}"
        )
        if r.alpha_usd is not None:
            frac = (
                f" ({r.alpha_frac * 100.0:+.0f}% of position)" if r.alpha_frac is not None else ""
            )
            lines.append(f"- Window alpha vs SPY: ${r.alpha_usd:+,.0f}{frac}")
        if r.mismatch_reasons:
            lines.append(f"- Sizing tension: {'; '.join(r.mismatch_reasons)}")
    val = ctx.holdings_val.get(t)
    if val is not None:
        lines.append(
            f"- DCF upside {val.upside_pct:+.0f}% (fair value vs price, run {val.dcf_date or '?'})"
        )
    if len(lines) == 1:
        lines.append("- (no live sizing data — tracker offline and/or no DCF run)")
    block = "\n".join(lines)
    # The L1 calibration return path: the owner's own track record on this
    # conviction cohort rides into both the question and the memo step.
    calib = calibration_block(ctx, t)
    return f"{block}\n\n{calib}" if calib else block


def _anchors_block(repo_root: Path, ticker: str) -> str:
    """Thesis + bear + priors anchors (the IR anchor is deliberately omitted —
    company framing has no place in a flow about the OWNER's read)."""
    try:
        from llm.anchors import (
            compose_anchor_block,
            load_bear_anchor,
            load_owner_profile_anchor,
            load_priors_anchor,
            load_thesis_anchor,
            load_worldview_anchor,
        )

        # The owner's Worldview + affirmed profile facts belong here above all —
        # this flow is explicitly about the OWNER's read. Worldview is inert until
        # LEDGER_WORLDVIEW_ANCHOR is on; the profile anchor is inert until the
        # owner has affirmed at least one fact (§7.1 gated assertion).
        return compose_anchor_block(
            load_thesis_anchor(repo_root, ticker),
            load_bear_anchor(repo_root, ticker),
            "",
            load_priors_anchor(repo_root, ticker),
            load_worldview_anchor(repo_root),
            load_owner_profile_anchor(repo_root),
        )
    except Exception as exc:  # anchors must never block the flow
        log.debug({"event": "socratic_anchor_load_failed", "ticker": ticker, "error": str(exc)})
        return ""


_QUESTION_LINE_RX = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+\S)\s*$")


def parse_questions(raw: str) -> list[str]:
    """Numbered/bulleted lines -> question list, capped at MAX_QUESTIONS.
    Raises ValueError below MIN_QUESTIONS — a malformed completion must fail
    the step visibly rather than ship a degenerate think-through."""
    out: list[str] = []
    for line in raw.splitlines():
        m = _QUESTION_LINE_RX.match(line)
        if m:
            out.append(m.group(1).strip())
    if len(out) < MIN_QUESTIONS:
        raise ValueError(f"expected >= {MIN_QUESTIONS} questions, parsed {len(out)}")
    return out[:MAX_QUESTIONS]


_STANCE_RX = re.compile(r"^\s*STANCE:\s*([a-z]+)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_stance(body_md: str) -> str | None:
    """The memo's final STANCE line -> validated stance, or None when absent /
    out of vocabulary (the memo still persists; it just isn't direction-scoreable)."""
    matches = _STANCE_RX.findall(body_md)
    if not matches:
        return None
    stance = matches[-1].lower()
    return stance if stance in STANCES else None


def generate_questions(
    repo_root: Path,
    ticker: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    ctx: AdvisorContext | None = None,
) -> SocraticPrelude:
    """Step 1: the 3-5 owner-first questions. Raises on hard stops AND on an
    unparseable completion — there is no degraded mode for a flow the owner
    is sitting in front of; the UI surfaces the error and offers retry."""
    t = ticker.upper()
    context = ctx or build_advisor_context(repo_root, user_id=user_id, api_url=api_url)
    ctx_block = _ticker_context_block(context, t)
    premortem = _premortem_block(repo_root, t, api_url=api_url)
    prompt = _QUESTIONS_PROMPT.format(
        ticker=t,
        context=ctx_block,
        anchors=_anchors_block(repo_root, t) or "(no anchors cached for this name)",
        premortem=premortem or "(no pre-mortem — too thin a track record, or nothing grounded)",
        min_q=MIN_QUESTIONS,
        max_q=MAX_QUESTIONS,
    )
    raw = call_llm(prompt, purpose=QUESTIONS_PURPOSE, ticker=t)
    return SocraticPrelude(ticker=t, questions=parse_questions(raw), context_block=ctx_block)


def _premortem_block(repo_root: Path, ticker: str, *, api_url: str | None) -> str:
    """The L8 decision-moment pre-mortem (calibration_coach.premortem_block) —
    the L1 calibration injection point EXTENDED with "the ways this resembles
    bets you got wrong" + drafted early-warning conditions, composed + eval-gated
    off the same Opus purpose. Best-effort: it must never block the flow the
    owner is sitting in front of, so any non-hard-stop failure degrades to "" and
    the questions ride on the calibration block alone. Hard stops propagate
    (budget/setup), matching the questions call's own posture."""
    try:
        from calibration_coach import premortem_block

        # code_root = repo_root: the served checkout carries evals/rubrics for the gate.
        return premortem_block(repo_root, ticker, code_root=repo_root, api_url=api_url)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.debug({"event": "socratic_premortem_failed", "ticker": ticker, "error": str(exc)})
        return ""


def generate_decision_memo(
    repo_root: Path,
    ticker: str,
    *,
    questions: list[str],
    answers: list[str],
    horizon_days: int = DEFAULT_SOCRATIC_HORIZON_DAYS,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    ctx: AdvisorContext | None = None,
) -> MemoResult:
    """Step 2: the decision memo from the owner's answers. Persists kind
    'socratic' with the parsed stance + horizon (P2.5's scoreable unit) plus
    the Q&A transcript appended to the body. Transient LLM failures degrade
    to a skipped MemoResult; hard stops propagate."""
    t = ticker.upper()
    if len(questions) != len(answers):
        raise ValueError(f"{len(questions)} questions but {len(answers)} answers")
    if not any(a.strip() for a in answers):
        raise ValueError("at least one answer must be non-empty — the memo is owner-first")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    context = ctx or build_advisor_context(repo_root, user_id=user_id, api_url=api_url)
    qa_lines = [
        f"Q{i + 1}: {q}\nA{i + 1}: {a.strip() or '(no answer)'}"
        for i, (q, a) in enumerate(zip(questions, answers, strict=True))
    ]
    qa_transcript = "\n\n".join(qa_lines)
    prompt = _MEMO_PROMPT.format(
        ticker=t,
        context=_ticker_context_block(context, t),
        anchors=_anchors_block(repo_root, t) or "(no anchors cached for this name)",
        qa_transcript=qa_transcript,
        horizon_days=horizon_days,
    )
    try:
        body = call_llm(prompt, purpose=MEMO_PURPOSE, ticker=t)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning({"event": "socratic_memo_llm_failed", "ticker": t, "error": str(exc)})
        return MemoResult(
            ok=False, kind="socratic", ticker=t, skipped_reason=f"{type(exc).__name__}: {exc}"
        )
    if not body.strip():
        return MemoResult(ok=False, kind="socratic", ticker=t, skipped_reason="empty LLM response")
    stance = parse_stance(body)
    if stance is None:
        log.warning({"event": "socratic_stance_missing", "ticker": t})
    full_body = f"{body.strip()}\n\n---\n\n## Think-through transcript\n\n{qa_transcript}"
    title = f"Socratic think-through · {t} · {context.generated_at[:10]}"
    memo_context: dict[str, object] = {
        "questions": questions,
        "answers": answers,
        "horizon_days": horizon_days,
        "stance": stance,
        "holding_upside_pct": (
            context.holdings_val[t].upside_pct if t in context.holdings_val else None
        ),
    }
    return persist_memo(
        db_path=repo_root / "data" / "portfolio.db",
        user_id=user_id,
        kind="socratic",
        ticker=t,
        counter_ticker=None,
        title=title,
        body_md=full_body,
        context=memo_context,
        write_ledger=True,
        stance=stance,
        horizon_days=horizon_days,
    )
