"""Compose a grounded, cited standup brief through the Ask engine.

A trip alone is not a message — the standup turns each ``StandupSignal`` into a
question framed in the analyst's own terms ("a condition YOU set just tripped"),
runs it through the production ask path (``ask.engine.respond_turn`` with the
portfolio pack — the same call the eval replay drives), and keeps the grounded,
``[n]``-cited narrative answer. The engine's router pulls the relevant portfolio
packs (decisions, DCF, calibration, journal) plus document grounding, so the
brief reasons over the same evidence an interactive Ask turn would.

Two deliberate constraints:

  * the framing never asks for a buy/sell STANCE — the portfolio system prompt
    redirects stances to the Socratic flow, and an unprompted advisor pushing a
    stance is exactly the failure mode the eval gate guards against. It asks
    instead whether the evidence invalidates the thesis / is worth re-opening.
  * the engine seam is injected (``events_fn``) so tests drive a fake event
    stream and never launch the real ``claude -p`` subprocess (conftest blocks
    it); production passes ``prod_events_fn``.

The composed answer carries no persistence side effect here — the orchestrator
owns writing it into the standup thread, after the eval gate clears it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from standup.memory import PriorConclusion
from standup.signals import StandupSignal

log = logging.getLogger(__name__)

# (question, tickers) -> the engine's event stream as a list of dicts.
EventsFn = Callable[[str, list[str]], list[dict[str, object]]]

# Shared tail appended to every framing: the PM-to-PM voice + grounding + the
# stance redirect, so the composed brief lands inside the rubric's bars.
_FRAMING_TAIL = (
    " Keep it tight, PM-to-PM. Ground every specific claim in the numbered "
    "evidence and cite it with [n]; name the disconfirming evidence too, and "
    "flag where the evidence is thin or stale rather than papering over it. End "
    "with the single most useful next thing to check. Do NOT give a buy / sell / "
    "trim stance — assess what the evidence says and whether the decision is "
    "worth re-opening."
)


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    """A composed, not-yet-gated standup brief."""

    question: str  # the engine-facing framing (what the answer responded to)
    answer: str  # the grounded narrative brief
    citations: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


def _memory_caveat(prior: PriorConclusion | None) -> str:
    """The hazard-guarded prior-conclusion preamble (memory stub). Empty when
    there is no recorded prior for this name."""
    if prior is None or not prior.conclusion.strip():
        return ""
    return (
        f"Your last recorded read on this name ({prior.recorded_on}) was: "
        f'"{prior.conclusion}". Treat that as a PRIOR, not as fact — the evidence '
        "below may have already moved it; re-derive from today's evidence rather "
        "than re-asserting the prior. "
    )


def frame_question(signal: StandupSignal, *, prior: PriorConclusion | None = None) -> str:
    """The engine-facing question for a trip — in the analyst's own terms, with
    the memory prior (when present) and the shared grounding/stance tail."""
    ev = signal.evidence
    ticker = signal.ticker or "the book"
    if signal.kind == "decision_condition":
        latest = ev.get("latest_value")
        latest_str = f"{latest:g}" if isinstance(latest, (int, float)) else "?"
        body = (
            f"A falsifiable condition you wrote for {ticker} just tripped: "
            f"{ev.get('condition_label')} — latest reading {latest_str} "
            f"{ev.get('unit') or ''} as of {ev.get('period_end')}. You set this as a "
            f"'what would change my mind' bar for {ev.get('decision_summary')}. "
            "Brief me: does the latest evidence genuinely invalidate that thesis or "
            "is this noise, and is the decision worth re-opening?"
        )
    elif signal.kind == "dcf_staleness":
        mis = ev.get("mispricing_pct")
        mis_str = f"{mis:+.0f}%" if isinstance(mis, (int, float)) else "?"
        fv = ev.get("npv_per_share")
        fv_str = f"${fv:,.2f}" if isinstance(fv, (int, float)) else "?"
        body = (
            f"Your DCF for {ticker} was last valued {ev.get('valuation_date')} "
            f"({ev.get('age_days')}d ago) and the price has since moved to {mis_str} "
            f"versus that {fv_str} fair value. Brief me: what has changed since the "
            "valuation, do the model's assumptions still hold, and is it worth "
            "re-deriving the fair value?"
        )
    elif signal.kind == "journal_open_item":
        scope = signal.ticker or "the book"
        body = (
            f"An open item in your journal for {scope} has been unresolved since "
            f"{str(ev.get('created_at') or '')[:10]} ({ev.get('age_days')}d): "
            f'"{ev.get("body")}". Brief me: does the latest evidence resolve it, '
            "what is still missing, and what is the specific next thing to check?"
        )
    elif signal.kind == "position_drift":
        cur = ev.get("current_pct")
        tgt = ev.get("target_pct")
        drift = ev.get("drift_pp")
        cur_str = f"{cur:.1f}%" if isinstance(cur, (int, float)) else "?"
        tgt_str = f"{tgt:g}%" if isinstance(tgt, (int, float)) else "?"
        drift_str = f"{drift:+.1f}pp" if isinstance(drift, (int, float)) else "?"
        body = (
            f"{ticker} has drifted to {cur_str} of the book versus your stated "
            f"{tgt_str} target ({drift_str}). Brief me: does this drift reflect a "
            "thesis or conviction change worth acting on, or is it just price moves "
            "— and what do conviction, valuation, and your decision ledger say?"
        )
    else:  # defensive: an unknown kind still gets a grounded ask off the headline
        body = f"{signal.headline} Brief me on what the latest evidence says."
    return _memory_caveat(prior) + " ".join(body.split()) + _FRAMING_TAIL


def _first(events: list[dict[str, object]], kind: str) -> dict[str, object] | None:
    return next((e for e in events if e.get("type") == kind), None)


def _citation_items(events: list[dict[str, object]]) -> list[dict[str, object]]:
    cites = _first(events, "citations")
    if cites is None:
        return []
    raw = cites.get("items")
    if not isinstance(raw, list):
        return []
    return [
        cast("dict[str, object]", it) for it in cast("list[object]", raw) if isinstance(it, dict)
    ]


def compose(
    signal: StandupSignal,
    *,
    events_fn: EventsFn,
    prior: PriorConclusion | None = None,
) -> ComposedMessage | None:
    """Frame the trip, run it through the engine, and return the grounded
    narrative brief. None when the engine produced no usable narrative answer
    (routed to a data view, errored, or returned empty) — the orchestrator
    records that as a retryable compose failure, not a delivery."""
    question = frame_question(signal, prior=prior)
    tickers = [signal.ticker] if signal.ticker else []
    try:
        events = events_fn(question, tickers)
    except Exception as exc:
        log.warning(
            {
                "event": "standup_compose_engine_failed",
                "signal": signal.signature,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None
    final = _first(events, "final")
    if final is None or str(final.get("route")) != "narrative":
        return None
    answer = str(final.get("text") or "").strip()
    if not answer:
        return None
    return ComposedMessage(question=question, answer=answer, citations=_citation_items(events))


def prod_events_fn(db_path: Path, repo_root: Path) -> EventsFn:
    """The production engine seam: each call drives ``respond_turn`` with the
    portfolio pack and collects its event stream (the eval-replay pattern,
    ``evals.ask_loop._prod_events``). Imports lazily so the standup package
    stays importable without pulling the full engine stack."""

    def _run(question: str, tickers: list[str]) -> list[dict[str, object]]:
        from ask.context import build_portfolio_pack
        from ask.engine import AskTurn, respond_turn

        pack = build_portfolio_pack(repo_root, db_path)
        turn = AskTurn(text=question, tickers=list(tickers))
        return list(respond_turn(turn, pack, db_path=db_path, repo_root=repo_root))

    return _run


__all__ = ["ComposedMessage", "EventsFn", "compose", "frame_question", "prod_events_fn"]
