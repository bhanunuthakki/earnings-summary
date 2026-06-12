"""Agentic evidence follow-up for narrative ask turns (fund-grade build S7).

One-shot retrieval (``ask.grounding`` + the S4 pack router) guesses channels
and periods from the question BEFORE the model sees anything; complex
questions (multi-quarter comparisons, specific older filings) fail when that
first guess is wrong. This module closes the loop with a TWO-PASS design:

  pass 1   — the normal streamed narrative call, with a NEED-protocol block
             appended to the prompt: answer normally, or — when a specific
             retrievable piece of evidence is missing — reply with ONLY
             ``{"need": [{kind, ticker, period, query}]}``.
  retrieve — ``ask.grounding.gather_requested_evidence`` resolves each
             validated need through the existing channels (facts / filings /
             transcripts / portfolio packs), period-aware, deduped against
             what the model already saw, numbered into the same [n] system.
  pass 2   — a real ``llm.cli.call_llm`` (purpose ``ask_evidence_followup``,
             shared per-turn ``run_id``) over the augmented evidence. The
             model may request once more; after ``MAX_ROUNDS`` retrievals the
             prompt forces "answer with what exists".

Why two-pass instead of tool-streaming: the transport is a single-turn
claude-CLI subprocess (``-p --output-format json``); the evals plan already
judged stream-envelope/tool-loop reshaping not worth it (§2.5). Two-pass
keeps the transport untouched and makes every extra call a first-class
ledger row.

Budgets, all per-round: ≤``MAX_NEEDS_PER_ROUND`` needs, ≤6 new evidence
items / ~6KB added text (grounding's caps), ``ROUND_TIMEOUT_SECONDS`` call
latency, and a monthly purpose budget (alembic 0091, ``on_exceed='skip'``).

Failure policy — fail CLOSED to single-pass behavior, never break the turn:
* over budget / DB missing → ``followup_armed`` is False → the protocol is
  never offered; turns behave exactly as before S7.
* a response that LOOKS like a need-request but doesn't validate (after the
  shared ``llm.structured`` fence-tolerant parse) burns its round with no
  retrieval and the next pass is forced to answer with what exists.
* a follow-up call failure (transport, budget hard-stop) ends the loop with
  an explicit error — the engine surfaces it instead of showing raw JSON.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

from ask.grounding import (
    NEED_KINDS,
    EvidenceItem,
    EvidenceNeed,
    build_evidence_block,
    gather_requested_evidence,
)
from llm.cli import call_llm
from llm.structured import parse_json_payload
from llm_budget import should_skip_for_budget

log = logging.getLogger(__name__)

PURPOSE = "ask_evidence_followup"

# Hard cap on retrieval rounds per turn — after the second retrieval the
# prompt forces an answer from what exists (directive: "2 rounds, then
# answer with what exists").
MAX_ROUNDS = 2
MAX_NEEDS_PER_ROUND = 3
# Per-round latency budget: this is an interactive surface — the dock shows
# "fetching more evidence…" while the round runs, but a narrative answer
# should never sit behind the batch default (20 min).
ROUND_TIMEOUT_SECONDS = 300

_TICKER_RX = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


@dataclass(slots=True)
class FollowupOutcome:
    """How the loop ended: a real answer, or an explicit error (the engine
    must never surface raw need-request JSON as the user-facing answer)."""

    final_text: str | None
    items: list[EvidenceItem]  # the full augmented numbered evidence
    rounds: int  # retrieval rounds actually spent
    error: str | None = None
    new_items: int = 0
    run_id: str = field(default_factory=lambda: uuid4().hex)


def followup_armed(db_path: Path) -> bool:
    """May this turn offer the NEED protocol? Structural gate (a missing DB
    has nothing to retrieve) + the purpose's monthly budget in skip mode —
    over cap, turns silently fall back to one-shot retrieval."""
    if not db_path.exists():
        return False
    return should_skip_for_budget(PURPOSE, db_path=db_path) is None


def need_protocol_block(rounds_left: int = MAX_ROUNDS) -> str:
    """The pass-1/pass-2 prompt block that offers the evidence request."""
    kinds = ", ".join(sorted(NEED_KINDS))
    return f"""NEED MORE EVIDENCE? Default to answering now from the context and
evidence above. Only when a specific retrievable piece of evidence is
missing AND would materially change the answer, reply with ONLY this JSON
object — no prose, no markdown fences, nothing before or after it:

{{"need": [{{"kind": "<one of: {kinds}>", "ticker": "<TICKER, optional>", "period": "<e.g. Q1 2025 or FY2024, optional>", "query": "<what you need, a few words>"}}]}}

- at most {MAX_NEEDS_PER_ROUND} entries, each naming ONE specific thing.
- "fact" = a metric/KPI series by name; "filing" = 10-K/10-Q sections (use
  "period" for a specific fiscal year); "transcript" = earnings-call
  passages (use "period" to reach calls older than the latest two);
  holdings/conviction/dcf/decisions/journal/performance are the analyst's
  portfolio stores.
- You have {rounds_left} retrieval round(s) left this turn. Never mix prose
  with the JSON, and never request what the evidence above already shows."""


def parse_need_request(text: str) -> list[EvidenceNeed] | None:
    """Classify one model response: ``None`` = a normal answer; a list = a
    need-request (possibly empty when every entry was unusable — the caller
    burns the round and forces an answer next pass, fail-closed).

    Validation rides the shared structured-output helper (#446):
    ``parse_json_payload`` is fence-tolerant and strict about shape."""
    head = text.strip()
    if not (head.startswith("{") or head.startswith("```")):
        return None
    try:
        payload = parse_json_payload(head, expect="object", required_keys=("need",))
    except ValueError:
        return None
    raw_need = cast("dict[str, object]", payload).get("need")
    if not isinstance(raw_need, list):
        return []
    out: list[EvidenceNeed] = []
    for entry in cast("list[object]", raw_need)[:MAX_NEEDS_PER_ROUND]:
        if not isinstance(entry, dict):
            continue
        entry_d = cast("dict[str, object]", entry)
        kind = str(entry_d.get("kind") or "").strip().lower()
        if kind not in NEED_KINDS:
            log.info({"event": "ask_followup_need_kind_dropped", "kind": kind[:40]})
            continue
        ticker_raw = str(entry_d.get("ticker") or "").strip().upper()
        ticker = ticker_raw if _TICKER_RX.match(ticker_raw) else None
        period = str(entry_d.get("period") or "").strip() or None
        query = " ".join(str(entry_d.get("query") or "").split())[:200]
        out.append(EvidenceNeed(kind=kind, ticker=ticker, period=period, query=query))
    return out


def _needs_note(needs: list[EvidenceNeed]) -> str:
    bits: list[str] = []
    for n in needs:
        part = " ".join(p for p in (n.ticker, n.kind, n.period) if p)
        bits.append(part or n.kind)
    return "; ".join(bits)[:120]


def _compose_followup_prompt(
    question: str,
    *,
    base_context: str,
    items: list[EvidenceItem],
    thread_text: str,
    retrieved_note: str,
    rounds_left: int,
) -> str:
    """The pass-2/3 prompt: same shape as pass 1 (context + evidence + prior
    thread + question), plus the follow-up framing. ``rounds_left == 0``
    forces an answer."""
    evidence_block = build_evidence_block(items)
    followup_note = (
        "EVIDENCE FOLLOW-UP: you previously requested additional evidence for "
        f"this question. {retrieved_note} Newly retrieved items carry the "
        "highest [n] numbers in the EVIDENCE block above. The Read tool is NOT "
        "available in this pass — ground every claim in the numbered evidence."
    )
    if rounds_left > 0:
        followup_note += "\n\n" + need_protocol_block(rounds_left)
    else:
        followup_note += (
            "\n\nAnswer the question now. Do NOT request more evidence — if "
            "something is still missing, say plainly what is missing and answer "
            "with what exists."
        )
    parts = [p for p in (base_context, evidence_block, followup_note) if p]
    return (
        "\n\n".join(parts)
        + "\n\n---\n\nPRIOR THREAD:\n"
        + (thread_text or "(first turn)")
        + "\n\n---\n\nUSER:\n"
        + question
    )


def run_followup_rounds(
    *,
    question: str,
    needs: list[EvidenceNeed],
    items: list[EvidenceItem],
    base_context: str,
    thread_text: str,
    repo_root: Path,
    db_path: Path,
    scope_tickers: list[str],
    ledger_ticker: str | None,
) -> Generator[dict[str, object], None, FollowupOutcome]:
    """Drive the retrieval rounds for one turn whose pass-1 response was a
    need-request. Yields engine event frames (stage progress); returns the
    outcome via the generator return value (``yield from`` in the engine).

    Every follow-up call is a real ``call_llm`` with this module's purpose
    and a per-turn ``run_id`` shared across rounds, so the ledger shows
    "this turn's loop cost $X across N calls"."""
    outcome = FollowupOutcome(final_text=None, items=list(items), rounds=0)
    current_needs = needs
    while True:
        outcome.rounds += 1
        note = _needs_note(current_needs)
        yield {
            "type": "stage",
            "stage": "retrieving",
            "route": "narrative",
            "note": f"fetching more evidence… (round {outcome.rounds}/{MAX_ROUNDS}"
            + (f": {note}" if note else "")
            + ")",
        }
        new_items = (
            gather_requested_evidence(
                current_needs,
                question=question,
                repo_root=repo_root,
                db_path=db_path,
                scope_tickers=scope_tickers,
                existing=outcome.items,
            )
            if current_needs
            else []
        )
        outcome.items = outcome.items + new_items
        outcome.new_items += len(new_items)

        if not current_needs:
            # The request looked like a need but no entry was usable —
            # fail closed: burn the round, force a single-pass answer.
            rounds_left = 0
            retrieved_note = "That request could not be parsed into retrievals."
        else:
            rounds_left = MAX_ROUNDS - outcome.rounds
            retrieved_note = (
                f"Retrieved {len(new_items)} new item(s) for: {note}."
                if new_items
                else f"Nothing new could be retrieved for: {note}."
            )
        yield {
            "type": "stage",
            "stage": "answering",
            "route": "narrative",
            "note": f"grounded on {len(outcome.items)} source"
            f"{'s' if len(outcome.items) != 1 else ''} ({len(new_items)} new)",
        }
        prompt = _compose_followup_prompt(
            question,
            base_context=base_context,
            items=outcome.items,
            thread_text=thread_text,
            retrieved_note=retrieved_note,
            rounds_left=rounds_left,
        )
        try:
            text = call_llm(
                prompt,
                purpose=PURPOSE,
                ticker=ledger_ticker,
                scope="ask",
                run_id=outcome.run_id,
                timeout_seconds=ROUND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Hard stops (budget/setup) and transients end the loop the same
            # way on this interactive surface: an explicit error frame —
            # never raw JSON shown as the answer, never a crashed stream.
            log.warning(
                {
                    "event": "ask_followup_call_failed",
                    "round": outcome.rounds,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            outcome.error = f"evidence follow-up failed ({type(exc).__name__})"
            return outcome
        again = parse_need_request(text)
        if again is None:
            outcome.final_text = text.strip()
            return outcome
        if outcome.rounds >= MAX_ROUNDS:
            log.warning({"event": "ask_followup_round_cap_exhausted"})
            outcome.error = "the model kept requesting evidence after the round cap"
            return outcome
        current_needs = again


__all__ = [
    "MAX_NEEDS_PER_ROUND",
    "MAX_ROUNDS",
    "PURPOSE",
    "ROUND_TIMEOUT_SECONDS",
    "FollowupOutcome",
    "followup_armed",
    "need_protocol_block",
    "parse_need_request",
    "run_followup_rounds",
]
