"""capture_triage — the PRIMARY gate on the capture->answer path (B3).

``is_answerable_capture`` (onmymind.respond) is a question-shaped-text regex: it
answers "does this READ like a question" but has no idea what the owner
actually believes. A musing like "doubling down on MELI here" reads as a flat
declarative — the regex files it as inert — even when it directly contradicts
a standing concentration Tenet. This module replaces the regex as the primary
routing decision with a closed three-way call, grounded in exactly what the
owner's current Worldview/decisions/recent musings SHOW:

  * ``answer_now``    — the note asks a question / requests information the
                         platform can answer (the old regex's positive case).
  * ``contradiction``  — the note cuts against a SHOWN tenet, stance, open
                         decision, or recent musing. Cites exactly one shown id
                         token (``T<id>``/``S<id>``/``N<id>``/``D<id>``);
                         ``answer_capture`` composes a deterministic challenge
                         from the resolved conflict WITHOUT a second LLM call.
  * ``plain``         — everything else (observation, todo, watchlist add).

Grounding gate (``synthesis.tenet_distill`` verbatim pattern): the valid token
set is built server-side from exactly what was rendered into the prompt, so a
fabricated ``conflicts_with`` citation (or one pointing at nothing shown) can
never manufacture a "contradiction" out of thin air — it downgrades to the
regex-derived fallback route instead.

Fail-open, never-raising (the ``reply.py`` precedent, but stricter): ANY
exception from the call layer — hard stop or transient — degrades to the
regex fallback with ``degraded=True``, because this classifier sits on the
capture-landing path where an answer failure must never block the capture
that already landed (mirrors ``answer_capture``'s own outer try/except one
layer up). The regex (``is_answerable_capture``) is demoted from primary gate
to this fallback's oracle — still worth keeping for exactly the moment the
LLM layer is unavailable.

The context builder degrades block-by-block (a missing v_decision_journal view
on an older DB, an empty Worldview, a fresh DB with no musings yet) rather than
failing the whole gather — each source is optional, none is load-bearing for
the others. ``evals/golden/capture_triage.json`` is the quality bar.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from synthesis.insights import list_insights
from synthesis.tenets import list_tenets
from user_state._db import open_conn
from user_state.notes import list_notes

log = logging.getLogger(__name__)

PURPOSE = "capture_triage"

ROUTES: tuple[str, ...] = ("answer_now", "contradiction", "plain")


class _TriageWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["answer_now", "contradiction", "plain"]
    conflicts_with: str | None = Field(default=None, max_length=40)
    why: str = Field(default="", max_length=300)


_TRIAGE_ADAPTER = TypeAdapter(_TriageWire)

# How much of the rendered Shown-items block the prompt carries — a budget,
# not a per-section limit; sections are dropped whole once the budget is hit
# so the token set handed to the grounding gate always matches what was shown.
_MAX_CONTEXT_CHARS = 4000

_T = TypeVar("_T")

# (note_body, context_text) -> the LLM's schema-validated dict.
TriageCall = Callable[[str, str], "dict[str, object]"]


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    route: str  # one of ROUTES
    conflict_token: str | None = None  # e.g. "T12" / "S7" / "N54" / "D3"
    why: str = ""
    # Resolved from conflict_token against the SAME rows rendered into the
    # prompt — never re-derived from the model's own words.
    conflict_kind: str | None = None  # tenet | stance | musing | decision
    conflict_id: int | None = None
    conflict_body: str = ""
    conflict_as_of: str = ""
    conflict_ticker: str | None = None  # decisions only — the challenge's kind-label needs it
    # True only on the transient-failure path (an exception from the call
    # layer) — distinguishes "the model degraded" from "the model said plain".
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class _Conflict:
    kind: str
    id: int
    body: str
    as_of: str
    ticker: str | None = None


@dataclass(frozen=True, slots=True)
class _TenetLike:
    id: int
    scope_key: str
    body: str
    as_of: str


@dataclass(frozen=True, slots=True)
class _MusingLike:
    id: int
    body: str


@dataclass(frozen=True, slots=True)
class _DecisionLike:
    id: int
    ticker: str
    direction: str
    falsifier: str


def _safe_list(fn: Callable[[], list[_T]]) -> list[_T]:
    """Run one context-gathering block; degrade to ``[]`` on ANY failure (a
    missing table/view, a corrupt row, a pre-migration DB) rather than let one
    optional source sink the whole gather."""
    try:
        return fn()
    except Exception:
        return []


def _render_sections(
    tenets: Sequence[_TenetLike],
    stances: Sequence[_TenetLike],
    musings: Sequence[_MusingLike],
    decisions: Sequence[_DecisionLike],
) -> tuple[str, dict[str, _Conflict]]:
    """Render the Shown-items block + the token->conflict map the grounding
    gate validates against. A section header is informational (no token of its
    own); the char budget is enforced by DROPPING trailing blocks whole, so a
    token that survives truncation is always one the model actually saw."""
    blocks: list[tuple[str | None, str, _Conflict | None]] = []
    if tenets:
        blocks.append((None, "Current tenets:", None))
        for t in tenets:
            tok = f"T{t.id}"
            line = f"[{tok}] ({t.scope_key}) {t.body[:140]} (since {t.as_of[:10]})"
            blocks.append((tok, line, _Conflict("tenet", t.id, t.body, t.as_of)))
    if stances:
        blocks.append((None, "Current stances:", None))
        for s in stances:
            tok = f"S{s.id}"
            line = f"[{tok}] ({s.scope_key}) {s.body[:140]} (since {s.as_of[:10]})"
            blocks.append((tok, line, _Conflict("stance", s.id, s.body, s.as_of)))
    if musings:
        blocks.append((None, "Recent musings:", None))
        for m in musings:
            tok = f"N{m.id}"
            line = f"[{tok}] {m.body[:120]}"
            blocks.append((tok, line, _Conflict("musing", m.id, m.body, "")))
    if decisions:
        blocks.append((None, "Open decisions:", None))
        for d in decisions:
            tok = f"D{d.id}"
            falsifier = d.falsifier or "—"
            line = f"[{tok}] {d.ticker} {d.direction} — falsifier: {falsifier}"
            body = f"{d.ticker} {d.direction} decision — falsifier: {falsifier}"
            blocks.append((tok, line, _Conflict("decision", d.id, body, "", ticker=d.ticker)))

    lines: list[str] = []
    tokens: dict[str, _Conflict] = {}
    total = 0
    for tok, line, conflict in blocks:
        cost = len(line) + 1
        if total + cost > _MAX_CONTEXT_CHARS:
            break
        lines.append(line)
        total += cost
        if tok is not None and conflict is not None:
            tokens[tok] = conflict
    return "\n".join(lines), tokens


def _fetch_open_decisions(db_path: Path) -> list[_DecisionLike]:
    """Open (ungraded) decisions off ``v_decision_journal`` (alembic 0179).
    Best-effort: a pre-0179 DB (view absent) or any read error degrades to
    ``[]`` — never the reason the whole gather fails."""
    conn = open_conn(db_path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            "SELECT decision_id, ticker, recommendation_kind, recommendation_value, falsifier "
            "FROM v_decision_journal WHERE outcome_label IS NULL "
            "ORDER BY made_at DESC LIMIT 20"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[_DecisionLike] = []
    for r in rows:
        # recommendation_kind is the direction enum ('add'/'trim'/'hold'/'sell'/
        # 'initiate'/'avoid'); recommendation_value is a FLOAT (size pct) — never
        # the direction text.
        direction = str(r["recommendation_kind"] or "")
        out.append(
            _DecisionLike(
                id=int(r["decision_id"]),
                ticker=str(r["ticker"] or ""),
                direction=direction,
                falsifier=str(r["falsifier"] or ""),
            )
        )
    return out


def _gather_from_db(note_id: int | None, db_path: Path) -> tuple[str, dict[str, _Conflict]]:
    tenets = _safe_list(
        lambda: [
            _TenetLike(r.id, r.scope_key, r.body_md, r.as_of)
            for r in list_tenets(status="current", db_path=db_path)
        ]
    )
    stances = _safe_list(
        lambda: [
            _TenetLike(r.id, r.scope_key, r.body_md, r.as_of)
            for r in list_insights(kind="stance", status="current", db_path=db_path)
        ]
    )

    def _musings() -> list[_MusingLike]:
        rows = [r for r in list_notes(kind="musing", limit=11, db_path=db_path) if r.id != note_id]
        return [_MusingLike(r.id, r.body) for r in rows[:10]]

    musings = _safe_list(_musings)
    decisions = _safe_list(lambda: _fetch_open_decisions(db_path))
    return _render_sections(tenets, stances, musings, decisions)


def _gather_from_case(context: dict[str, object]) -> tuple[str, dict[str, _Conflict]]:
    """The eval-harness equivalent of :func:`_gather_from_db` — the golden
    case embeds its OWN context lists (no DB) so a case is reproducible
    without a fixture DB. Same rendering, same char budget, same grounding
    token set."""

    def _list_of(key: str) -> list[dict[str, object]]:
        raw = context.get(key)
        if not isinstance(raw, list):
            return []
        raw_items = cast("list[object]", raw)
        return [cast("dict[str, object]", x) for x in raw_items if isinstance(x, dict)]

    tenets = [
        _TenetLike(
            int(cast("int", t.get("id", 0))),
            str(t.get("scope_key") or ""),
            str(t.get("body") or ""),
            str(t.get("as_of") or ""),
        )
        for t in _list_of("tenets")
    ]
    stances = [
        _TenetLike(
            int(cast("int", s.get("id", 0))),
            str(s.get("scope_key") or ""),
            str(s.get("body") or ""),
            str(s.get("as_of") or ""),
        )
        for s in _list_of("stances")
    ]
    musings = [
        _MusingLike(int(cast("int", m.get("id", 0))), str(m.get("body") or ""))
        for m in _list_of("musings")
    ]
    decisions = [
        _DecisionLike(
            int(cast("int", d.get("id", 0))),
            str(d.get("ticker") or ""),
            str(d.get("direction") or ""),
            str(d.get("falsifier") or ""),
        )
        for d in _list_of("decisions")
    ]
    return _render_sections(tenets, stances, musings, decisions)


def _build_prompt(note_body: str, context_text: str) -> str:
    shown = context_text or "(nothing on record yet)"
    return (
        "An investor just captured a thought into their research journal. Given the "
        "note plus the CURRENTLY SHOWN items below (their standing beliefs, open "
        "decisions, and recent musings), pick exactly ONE route:\n"
        "- answer_now: the note asks a question or requests information the platform "
        'can answer (e.g. "What\'s my cost basis on MELI?", "should I trim NU here?").\n'
        "- contradiction: the note takes a position that cuts against ONE of the shown "
        "items (a tenet, a stance, an open decision, or a recent musing) — cite exactly "
        "one shown id token in `conflicts_with`.\n"
        "- plain: everything else — observations, todos, watchlist adds, neutral musings.\n\n"
        '`conflicts_with` MUST be one of the id tokens shown below (e.g. "T12"), or null. '
        "Never invent one. When unsure between contradiction and plain, choose plain.\n\n"
        f"Shown items:\n{shown}\n\n"
        f"Note: {note_body[:800]}\n\n"
        'Return JSON ONLY: {"route": "answer_now|contradiction|plain", '
        '"conflicts_with": "<id token or null>", "why": "<one sentence or empty>"}'
    )


def _default_call(note_body: str, context_text: str) -> dict[str, object]:
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        _build_prompt(note_body, context_text),
        purpose=PURPOSE,
        expect="object",
        required_keys=("route",),
        schema=_TRIAGE_ADAPTER,
    )
    return _TriageWire.model_validate(obj).model_dump()


def _regex_fallback(note_body: str, *, degraded: bool = False) -> TriageVerdict:
    """The demoted primary gate, now the transient/invalid-verdict fallback.
    Lazy import: :mod:`onmymind.respond` imports this module at top level, so
    the reverse import must be deferred past module-init to avoid a cycle."""
    from onmymind.respond import is_answerable_capture

    route = "answer_now" if is_answerable_capture(note_body) else "plain"
    return TriageVerdict(route=route, degraded=degraded)


def _resolve_verdict(
    raw: dict[str, object], note_body: str, tokens: dict[str, _Conflict]
) -> TriageVerdict:
    raw_route = raw.get("route")
    route = raw_route.strip().lower() if isinstance(raw_route, str) else ""
    why = str(raw.get("why") or "")[:300]
    if route not in ROUTES:
        return _regex_fallback(note_body)
    if route != "contradiction":
        return TriageVerdict(route=route, why=why)
    raw_token = raw.get("conflicts_with")
    token = raw_token.strip() if isinstance(raw_token, str) else ""
    # Grounding gate (tenet_distill pattern, verbatim): the citation must name
    # a token that was ACTUALLY rendered into this prompt — never re-derived
    # from the model's own claim about what it saw.
    conflict = tokens.get(token) if token else None
    if conflict is None:
        return _regex_fallback(note_body)
    return TriageVerdict(
        route="contradiction",
        conflict_token=token,
        why=why,
        conflict_kind=conflict.kind,
        conflict_id=conflict.id,
        conflict_body=conflict.body,
        conflict_as_of=conflict.as_of,
        conflict_ticker=conflict.ticker,
    )


def classify_capture_triage(
    note_body: str,
    *,
    note_id: int | None = None,
    db_path: Path | str,
    call: TriageCall | None = None,
) -> TriageVerdict:
    """Classify one landed capture: answer it, challenge it, or leave it plain.

    NEVER raises — this sits on the capture-landing path (``answer_capture``
    calls it before spending on the answer engine), so a bad model response,
    a budget cap, or a missing CLI must degrade to the regex-derived fallback
    with ``degraded=True``, not blow up the capture that already landed.
    """
    context_text, tokens = _gather_from_db(note_id, Path(db_path))
    try:
        raw = (call or _default_call)(note_body, context_text)
    except Exception:
        log.warning({"event": "capture_triage_transient", "note_id": note_id}, exc_info=True)
        return _regex_fallback(note_body, degraded=True)
    return _resolve_verdict(raw, note_body, tokens)


def classify_capture_triage_for_eval(case: dict[str, object]) -> dict[str, object]:
    """The eval-harness production entry point (golden_classifiers): a case
    dict carrying its own ``text`` + ``context`` (tenets/stances/musings/
    decisions) -> the pinned fields the golden set grades. Mirrors
    ``classify_intent_for_eval`` (text -> pinned fields), but the context is
    case-embedded rather than DB-sourced so the golden set never needs a
    fixture DB."""
    text = str(case.get("text") or "")
    raw_context = case.get("context")
    context: dict[str, object] = (
        cast("dict[str, object]", raw_context) if isinstance(raw_context, dict) else {}
    )
    context_text, tokens = _gather_from_case(context)
    try:
        raw = _default_call(text, context_text)
    except Exception:
        log.warning({"event": "capture_triage_transient", "note_id": None}, exc_info=True)
        verdict = _regex_fallback(text, degraded=True)
        return {"route": verdict.route, "conflicts_with": verdict.conflict_token}
    verdict = _resolve_verdict(raw, text, tokens)
    return {"route": verdict.route, "conflicts_with": verdict.conflict_token}


__all__ = [
    "PURPOSE",
    "ROUTES",
    "TriageCall",
    "TriageVerdict",
    "classify_capture_triage",
    "classify_capture_triage_for_eval",
]
