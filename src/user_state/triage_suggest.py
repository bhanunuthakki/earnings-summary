"""triage_route_suggest — second-chance routing for parked comments.

The Triage queue is machine work assigned to the human: every parked
``needs_triage`` comment renders a route-picker + Resolve + Dismiss and waits
for the owner to file it. The first classifier already declared no-fit
(Instrument Paradigm §1, closed under no-fit) — but "no confident fit at
classification time" is not "a human must pick from an 8-option menu". This
second pass asks a narrower question with the actual menu in hand: WHICH of
the routable intents would the owner pick, and how sure are we?

* ``high`` confidence → the sweep auto-routes (the machine does the filing;
  the note leaves the queue exactly as if the owner picked it) and stamps a
  receipt in context (``auto_routed``).
* ``low`` confidence → the suggestion is stored (``route_suggestion``) and the
  Triage row leads with ONE one-tap button ("Route to Data fix") instead of a
  bare menu.
* ``park`` (or any failure) → nothing changes; the row stays a manual pick.

Closed enum over ``ROUTABLE_INTENTS`` + ``park``, FAST tier, injected
``call=`` for tests — the ``capture_intent`` recipe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from user_state.notes import ROUTABLE_INTENTS

PURPOSE = "triage_route_suggest"

_CONFIDENCES: tuple[str, ...] = ("high", "low")

# (comment_text, context_line) -> the LLM's schema-validated dict.
SuggestCall = Callable[[str, str], "dict[str, object]"]

_INTENT_GLOSS: dict[str, str] = {
    "ask_question": "a question to answer about the company/report",
    "edit_thesis": "edit the written thesis prose",
    "edit_structured": "mutate structured fields (break rules, tiered KPIs, settings)",
    "drop_kpi": "stop tracking a KPI",
    "extract_kpi": "supply/extract a KPI value (a number with a source)",
    "curate_peers": "change the comparable-company set",
    "fix_data": "a wrong number / data-quality fix",
    "rewrite_section": "rewrite a report section's presentation",
}


@dataclass(frozen=True, slots=True)
class RouteSuggestion:
    intent: str | None  # a ROUTABLE_INTENTS member, or None (park — no call to make)
    confidence: str = "low"  # 'high' | 'low' (meaningless when intent is None)
    reason: str = ""


def _build_prompt(comment_text: str, context_line: str) -> str:
    menu = "\n".join(f"- {i}: {_INTENT_GLOSS.get(i, i)}" for i in ROUTABLE_INTENTS)
    ctx = f"Context: {context_line}\n\n" if context_line else ""
    return (
        "A comment on an equity-research report could not be auto-routed at first "
        "classification and was parked for human triage. Given the FULL routing menu, "
        "which intent would the analyst pick for it?\n\n"
        f"{menu}\n"
        "- park: none of the above fits; leave it for the human.\n\n"
        f"{ctx}"
        f"Comment: {comment_text[:800]}\n\n"
        "Answer 'high' confidence ONLY when the comment clearly commands one route; "
        "'low' when a route is plausible but a human should confirm.\n\n"
        'Return JSON ONLY: {"intent": "<menu key or park>", "confidence": "high|low", '
        '"reason": "<one line>"}'
    )


def _default_call(comment_text: str, context_line: str) -> dict[str, object]:
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        _build_prompt(comment_text, context_line),
        purpose=PURPOSE,
        expect="object",
        required_keys=("intent",),
    )
    return cast("dict[str, object]", obj) if isinstance(obj, dict) else {}


def suggest_route(
    comment_text: str,
    *,
    context_line: str = "",
    call: SuggestCall | None = None,
) -> RouteSuggestion:
    """Suggest a route for one parked comment. Fail closed to park (None) —
    a bad model answer must never file a comment somewhere wrong."""
    raw = (call or _default_call)(comment_text, context_line)
    raw_intent = raw.get("intent")
    intent = raw_intent.strip().lower() if isinstance(raw_intent, str) else ""
    if intent not in ROUTABLE_INTENTS:
        return RouteSuggestion(intent=None, reason=str(raw.get("reason") or "")[:200])
    raw_conf = raw.get("confidence")
    confidence = raw_conf.strip().lower() if isinstance(raw_conf, str) else "low"
    if confidence not in _CONFIDENCES:
        confidence = "low"
    return RouteSuggestion(
        intent=intent, confidence=confidence, reason=str(raw.get("reason") or "")[:200]
    )


def sweep_unsuggested(
    *,
    db_path: object = None,
    limit: int = 3,
    apply_high: bool = True,
    call: SuggestCall | None = None,
) -> dict[str, int]:
    """Run the second pass over open triage notes that don't carry a verdict
    yet. Per-item degrade: one classify failure skips that note (it stays a
    manual pick) and never breaks the sweep. Returns counters.

    ``apply_high=False`` stores every suggestion without auto-routing (the
    CLI's dry-ish mode); the idempotency latch is the stored context stamp —
    a note with ``route_suggestion`` / ``auto_routed`` / ``route_parked`` is
    never re-classified.
    """
    from pathlib import Path

    from user_state.notes import list_triage_notes, patch_note_context, route_triage_note

    dbp = db_path if db_path is None or isinstance(db_path, Path) else Path(str(db_path))
    stats = {"suggested": 0, "auto_routed": 0, "parked": 0, "skipped": 0, "errors": 0}
    notes = list_triage_notes(db_path=dbp)  # pyright: ignore[reportArgumentType]
    todo = [
        n
        for n in notes
        if not any(
            k in (n.context or {}) for k in ("route_suggestion", "auto_routed", "route_parked")
        )
    ][: max(0, limit)]
    for n in todo:
        ctx = n.context or {}
        context_line = " · ".join(
            str(v)
            for v in (n.ticker, ctx.get("tab"), ctx.get("selected_text"))
            if v and str(v).strip()
        )
        try:
            s = suggest_route(n.body, context_line=context_line, call=call)
        except Exception:
            stats["errors"] += 1
            continue
        if s.intent is None:
            patch_note_context(n.id, {"route_parked": {"reason": s.reason}}, db_path=dbp)
            stats["parked"] += 1
            continue
        if s.confidence == "high" and apply_high:
            patch_note_context(
                n.id,
                {"auto_routed": {"intent": s.intent, "reason": s.reason}},
                db_path=dbp,
            )
            route_triage_note(n.id, intent=s.intent, db_path=dbp)
            stats["auto_routed"] += 1
            continue
        patch_note_context(
            n.id,
            {"route_suggestion": {"intent": s.intent, "reason": s.reason}},
            db_path=dbp,
        )
        stats["suggested"] += 1
    return stats


__all__ = ["PURPOSE", "RouteSuggestion", "suggest_route", "sweep_unsuggested"]
