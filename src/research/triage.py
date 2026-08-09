"""research_triage — routing triage for a positive wondering verdict (B7).

Program-review finding (2026-07-19): 13 of 17 wonderings ever detected sat in
``research_tasks`` as ``proposed`` and silently expired — every wondering,
regardless of shape, became a task nobody ran. This module adds a SECOND,
short Haiku call (behind ``research.intent.classify_intent``'s ``wondering``
verdict) that routes the musing to the cheapest adequate handling instead of
defaulting every question straight into the research queue:

  * ``answer_now``       — the platform can very likely answer this directly
                            from data it already owns (cost basis, positions,
                            KPIs, DCF runs, filings on file) — no live
                            research pass needed. No task is created;
                            ``research.proposals.detect_and_create_task`` just
                            stamps ``context['research_route']='answer_now'``
                            and gets out of the way. This does NOT itself
                            trigger an answer: the INDEPENDENT ``capture_triage``
                            / ``onmymind.respond.answer_capture`` tap (B3) already
                            runs its own answer-or-not gate on every landed
                            capture ("one brain, two mouths" — see that
                            module's docstring), so a musing this route calls
                            answerable gets answered by that tap on its own
                            merits. If capture_triage's gate disagrees (e.g. a
                            plain/contradiction verdict), NOTHING answers the
                            capture — that is an accepted, documented gap
                            between two independently-gated taps, not a bug
                            this PR closes; a stricter integration (routing
                            straight into the answer engine) is future work.
  * ``belief_candidate``  — not a research question at all: a belief/opinion/
                            standing view. No task; flags the musing into the
                            Worldview distill ladder
                            (``context['ladder']='saved'``) so B4's
                            ``tenet_distill`` sweep (``_FLAGGED_LADDERS``)
                            picks it up on its next pass.
  * ``research_task``     — today's behavior: create the inert ``proposed``
                            chip. Now carries a STATED cost estimate
                            (``estimate_cost_usd``) and a formatted deep-
                            session prompt block (``build_session_prompt``)
                            for the B8 Claude-session bridge skill.

Fails OPEN to ``research_task`` on ANY error, timeout, or unparseable/unknown
response — the safe default is today's inert chip, never a silently-dropped
wondering (the exact failure mode this PR exists to close).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

PURPOSE = "research_triage"

ROUTES: tuple[str, ...] = ("answer_now", "belief_candidate", "research_task")

# What the platform can plausibly answer from OWNED data, without a live
# research pass — handed to the model as a capability hint so it doesn't
# route e.g. "what's my cost basis on NU" into a $-spending research task.
CAPABILITY_HINTS: tuple[str, ...] = (
    "cost basis / lots / realized-unrealized P&L",
    "current positions, weights, and portfolio risk",
    "stored KPIs and financial history per ticker",
    "existing DCF runs and valuation models",
    "ingested SEC filings and transcripts already on file",
)

TriageCall = Callable[[str], "dict[str, object]"]

# Deterministic, I/O-free cost hint (the plan's fallback: "a deterministic
# estimate table (per task kind, coarse dollars) inside your module is
# fine"). Deliberately simpler than research.tier.resolve_tier's actual
# run-time cap (which needs live portfolio-weight + hot-flag I/O and can run
# up to the $2.00 deep-tier ceiling) — this tap is fire-and-forget off the
# capture path and must stay pure/cheap. The number here is an approximate
# "~$x" shown on the RUN button/packet line, not a spend guarantee; the real
# spend is still governed by tier.py at RUN time
# (research.run.run_research_task -> resolve_tier).
_COST_TICKER_USD = 0.40
_COST_GENERAL_USD = 0.25


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    route: str  # one of ROUTES
    why: str = ""
    gate: str = ""  # llm | fail_open


def estimate_cost_usd(ticker: str | None) -> float:
    """A coarse, deterministic 'stated cost' for the RUN button/packet line —
    a ticker-scoped wondering (a targeted web pass) costs more than a general
    one. Pure, no I/O — safe to call from the fire-and-forget capture tap."""
    return _COST_TICKER_USD if ticker else _COST_GENERAL_USD


def build_session_prompt(musing: str, *, ticker: str | None) -> str:
    """A formatted markdown prompt block for the '-> Claude session' route —
    raw material for the B8 bridge skill. Deliberately self-contained (a
    fresh session has no memory of this capture) but SHORT — it names what to
    bring back, not a full research brief (that is the research_task's own
    job on the RUN path)."""
    caps = "\n".join(f"- {c}" for c in CAPABILITY_HINTS)
    ticker_line = f"Ticker: {ticker}\n" if ticker else ""
    return (
        f"# Research this wondering\n\n{ticker_line}"
        f"**The question:** {musing.strip()}\n\n"
        "**What the platform already has on file** (check before re-deriving):\n"
        f"{caps}\n\n"
        "**Bring back:** a short, sourced answer — cite where each fact came "
        "from, flag anything you could not verify, and note what would change "
        "the answer if you are wrong."
    )


def _build_prompt(musing: str, *, ticker: str | None, cost_hint_usd: float) -> str:
    caps = "\n".join(f"- {c}" for c in CAPABILITY_HINTS)
    return (
        "An investor's wondering was just flagged as research-worthy. Route it to "
        "exactly ONE of:\n"
        "- answer_now: the platform can very likely answer this directly from data "
        "it already owns (no live research pass needed). The platform owns:\n"
        f"{caps}\n"
        "- belief_candidate: this is not really a research question — it is a "
        "belief, opinion, or standing view the owner is stating or refining (it "
        "belongs in the Worldview, not a research queue).\n"
        "- research_task: it genuinely needs a live research pass (a fresh web or "
        "filing lookup, an open question the platform's own data cannot settle).\n\n"
        f"Ticker: {ticker or '(none)'}\nWondering: {musing}\n"
        f"A live research pass for this would cost roughly ${cost_hint_usd:.2f}.\n\n"
        'Return JSON ONLY: {"route": "answer_now|belief_candidate|research_task", '
        '"why": "<one short line>"}'
    )


def _default_call(prompt: str) -> dict[str, object]:
    from llm.contracts import RESEARCH_TRIAGE_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        expect="object",
        required_keys=("route",),
        schema=RESEARCH_TRIAGE_SCHEMA,
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def classify_triage(
    musing: str, *, ticker: str | None = None, call: TriageCall | None = None
) -> TriageVerdict:
    """Route a wondering. Fails OPEN to 'research_task' on any error, timeout,
    or unparseable/unknown response — a triage failure must never make a
    wondering vanish silently (the exact bug this PR fixes)."""
    cost_hint = estimate_cost_usd(ticker)
    runner = call or _default_call
    try:
        raw = runner(_build_prompt(musing, ticker=ticker, cost_hint_usd=cost_hint))
    except Exception:
        return TriageVerdict(route="research_task", why="triage call failed", gate="fail_open")
    raw_route = raw.get("route")
    route = raw_route.strip().lower() if isinstance(raw_route, str) else ""
    if route not in ROUTES:
        return TriageVerdict(route="research_task", why="unrecognized route", gate="fail_open")
    return TriageVerdict(route=route, why=str(raw.get("why") or "")[:300], gate="llm")


__all__ = [
    "CAPABILITY_HINTS",
    "PURPOSE",
    "ROUTES",
    "TriageCall",
    "TriageVerdict",
    "build_session_prompt",
    "classify_triage",
    "estimate_cost_usd",
]
