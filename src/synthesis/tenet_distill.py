"""The Worldview distiller (P2) — owner-flagged musings → candidate Tenets.

The machine half of the Worldview: distil the musings the owner deliberately
flagged (On My Mind ``ladder`` = saved / incorporated) into candidate **Tenets**
— beliefs about *how they invest* — each cited to the musings it rests on. Every
distilled Tenet lands ``proposed`` (never ``current``): the owner approves in one
tap. Contradiction with a standing Tenet on the same ``scope_key`` is surfaced as
a **tension**, not silently overwritten.

Cost control (two gates, both before any spend):
  1. **Deterministic $0 triage** — only owner-flagged musings NOT already cited by
     a live Tenet reach the model; nothing to distil ⇒ zero LLM.
  2. **Owner-tapped** — never on a cron; the run is an explicit owner action, and
     the ``tenet_distill`` budget (0132) caps it in ``skip`` mode.

Readings never enter this path (only the owner's OWN words distil into a belief) —
which also keeps fetched/untrusted content out of the distill write path (the
safety firebreak). The LLM call is injected (``call=``) so the engine is
unit-testable without the CLI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from synthesis.insights import list_insights
from synthesis.tenets import current_tenet_for_scope, record_tenet
from user_state.notes import AnalystNoteRow, list_notes

# A distil call: musings -> [{"tenet","scope_key","citations"}, ...] | None.
ProposedTenet = dict[str, object]
DistillCall = Callable[[Sequence[AnalystNoteRow]], "list[ProposedTenet] | None"]

_FLAGGED_LADDERS = frozenset({"saved", "incorporated"})


def _cited_note_ids(db_path: Path | str | None) -> set[int]:
    """Every musing id already cited by a live (current OR proposed) Tenet — the
    $0 dedup so a re-run never re-distils the same material."""
    cited: set[int] = set()
    for status in ("current", "proposed"):
        for t in list_insights(kind="tenet", status=status, db_path=db_path):
            cited.update(t.source_note_ids)
    return cited


def candidate_musings(
    db_path: Path | str | None, *, user_id: str = DEFAULT_USER_ID
) -> list[AnalystNoteRow]:
    """Owner-flagged (saved/incorporated) captured musings not yet distilled — the
    deterministic triage set. Empty ⇒ the run short-circuits with zero LLM cost."""
    already = _cited_note_ids(db_path)
    out: list[AnalystNoteRow] = []
    for m in list_notes(user_id=user_id, kind="musing", db_path=db_path, limit=10_000):
        if m.source != "capture" or m.id in already:
            continue
        if str((m.context or {}).get("ladder") or "") in _FLAGGED_LADDERS:
            out.append(m)
    return out


def _build_prompt(musings: Sequence[AnalystNoteRow]) -> str:
    lines = [f"- [{m.id}] ({m.created_at:%Y-%m-%d}) {m.body}" for m in musings]
    return (
        "You are distilling an investor's OWN flagged musings into candidate "
        "TENETS — durable beliefs about HOW they invest (their method, discipline, "
        "biases) that generalize across holdings, NOT calls on a single name. Ground "
        "each Tenet ONLY in the musings below and cite the musing id(s) it rests on. "
        "Propose a Tenet only when the musings genuinely support a cross-situation "
        "principle; if they do not, return an empty list.\n\n"
        "Musings (id, date, text):\n" + "\n".join(lines) + "\n\n"
        "Return JSON ONLY: a list of objects "
        '{"tenet": "<one sentence, the belief in the investor\'s voice>", '
        '"scope_key": "<2-4 word kebab-case topic, e.g. exit-discipline>", '
        '"citations": [<the musing ids this rests on>]}'
    )


def _coerce_ids(raw: object) -> list[int]:
    out: list[int] = []
    if isinstance(raw, list):
        for c in cast("list[object]", raw):
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                out.append(c)
            elif isinstance(c, str) and c.strip().isdigit():
                out.append(int(c))
    return out


def _default_call(musings: Sequence[AnalystNoteRow]) -> list[ProposedTenet] | None:
    from llm.structured import StructuredParseError, call_llm_structured  # lazy: CI needs no CLI

    try:
        obj = call_llm_structured(
            _build_prompt(musings),
            purpose="tenet_distill",
            scope="worldview",
            expect="array",
        )
    except StructuredParseError:
        return None  # unparseable output degrades to "no proposals"; hard stops propagate
    if not isinstance(obj, list):
        return None
    return [cast("ProposedTenet", x) for x in cast("list[object]", obj) if isinstance(x, dict)]


def run_tenet_distill(
    db_path: Path | str | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    call: DistillCall | None = None,
) -> dict[str, int]:
    """Distil owner-flagged musings into ``proposed`` Tenets. Returns counts.
    $0 when nothing is flagged/undistilled; degrade-safe (a failed/empty call
    proposes nothing and leaves the Worldview untouched)."""
    counts = {"candidates": 0, "proposed": 0, "tensions": 0, "skipped_groundless": 0}
    musings = candidate_musings(db_path, user_id=user_id)
    counts["candidates"] = len(musings)
    if not musings:
        return counts  # deterministic $0 short-circuit — no LLM

    distil = call or _default_call
    try:
        proposals = distil(musings)
    except Exception:
        return counts  # a distil failure never mutates the Worldview
    if not proposals:
        return counts

    valid_ids = {m.id for m in musings}
    for p in proposals:
        body = str(p.get("tenet") or "").strip()
        cited = sorted(c for c in _coerce_ids(p.get("citations")) if c in valid_ids)
        # Deterministic grounding gate: a Tenet must cite a real flagged musing —
        # no fabricated "you believe X" that points at nothing it was given.
        if not body or not cited:
            counts["skipped_groundless"] += 1
            continue
        raw_scope = p.get("scope_key")
        scope_key = (
            str(raw_scope).strip() if isinstance(raw_scope, str) and raw_scope.strip() else None
        )
        from synthesis.tenets import scope_key_for

        resolved = scope_key_for(body, scope_key)
        tension = current_tenet_for_scope(resolved, db_path=db_path)
        record_tenet(
            body_md=body,
            scope_key=resolved,
            source_note_ids=cited,
            status="proposed",
            provenance="derived",
            tension_with=tension.id if tension is not None else None,
            db_path=db_path,
        )
        counts["proposed"] += 1
        if tension is not None:
            counts["tensions"] += 1
    return counts
