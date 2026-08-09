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
from synthesis.insights import InsightRow, list_insights
from synthesis.semantic_tension import detect_semantic_tension
from synthesis.tenets import current_tenet_for_scope, list_tenets, record_tenet
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


def _build_prompt(musings: Sequence[AnalystNoteRow], existing: Sequence[InsightRow]) -> str:
    """The v2 distill prompt (owner pushback 2026-07-19: the v1 output read as
    a pile of situational lessons, several in unflagged conflict). Three bars
    v1 lacked: (1) IDENTITY-LEVEL — a Tenet is who the investor is, not a note
    from one episode; (2) CONDITIONAL where the real tension lives — "default
    X; when <condition>, Y" instead of two flat contradictory commands; (3)
    REVISE, don't stack — the standing Worldview rides in, and an overlapping
    or conflicting belief must reuse that Tenet's scope_key (a revision that
    reconciles both) rather than land as a parallel sibling the same-slug
    tension check can never see."""
    lines = [f"- [{m.id}] ({m.created_at:%Y-%m-%d}) {m.body}" for m in musings]
    standing = (
        "\n".join(f"- [{t.scope_key}] {' '.join(t.body_md.split())}" for t in existing)
        or "(none yet)"
    )
    return (
        "You are distilling an investor's OWN flagged musings into candidate "
        "TENETS — durable, identity-level beliefs about HOW they invest (their "
        "method, discipline, temperament, known biases) that generalize across "
        "holdings and market regimes. NOT calls on a single name, NOT a lesson "
        "from one episode restated as a rule.\n\n"
        "The quality bar for a Tenet:\n"
        "- Durable: still true in five years, e.g. 'rational bull: I want to own "
        "long-term prosperity driven by technology'.\n"
        "- Nuanced, not absolute: when the musings pull in two directions (e.g. "
        "'let winners run' vs 'trim concentrated winners'), do NOT emit both as "
        "flat rules — emit ONE conditional belief that names when each side "
        "applies ('default X; when <condition>, Y').\n"
        "- Few: propose at most 3, and only when the musings genuinely support a "
        "cross-situation principle. An empty list is a good answer.\n\n"
        "STANDING TENETS (the investor's current Worldview):\n" + standing + "\n\n"
        "If a candidate overlaps or conflicts with a standing Tenet, you MUST "
        "reuse that Tenet's exact scope_key and write the body as a REVISION "
        "that reconciles the standing belief with the new evidence — never "
        "propose a parallel belief on a fresh scope_key that quietly contradicts "
        "a standing one.\n\n"
        "Ground each Tenet ONLY in the musings below and cite the musing id(s) "
        "it rests on.\n\n"
        "Musings (id, date, text):\n" + "\n".join(lines) + "\n\n"
        "Return JSON ONLY: a list of objects "
        '{"tenet": "<one or two sentences, the belief in the investor\'s voice>", '
        '"scope_key": "<2-4 word kebab-case topic, e.g. exit-discipline — or the '
        'EXACT standing scope_key when revising>", '
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


def _default_call(
    musings: Sequence[AnalystNoteRow], existing: Sequence[InsightRow]
) -> list[ProposedTenet] | None:
    from llm.contracts import TENET_BATCH_SCHEMA
    from llm.structured import StructuredParseError, call_llm_structured  # lazy: CI needs no CLI

    try:
        obj = call_llm_structured(
            _build_prompt(musings, existing),
            purpose="tenet_distill",
            scope="worldview",
            expect="array",
            schema=TENET_BATCH_SCHEMA,
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
    proposes nothing and leaves the Worldview untouched).

    ``tensions`` counts EVERY overlap detected while landing a proposal — the
    free slug probe (``current_tenet_for_scope``) plus, when that finds
    nothing, the semantic probe (B5, ``synthesis.semantic_tension`` — catches
    a same-belief tenet under a DIFFERENT scope_key slug, e.g. the prod 20/31
    duplicate). ``tensions_semantic`` is the semantic-only subset, kept
    distinct so the two detection sources stay independently observable."""
    counts = {
        "candidates": 0,
        "proposed": 0,
        "tensions": 0,
        "tensions_semantic": 0,
        "skipped_groundless": 0,
    }
    musings = candidate_musings(db_path, user_id=user_id)
    counts["candidates"] = len(musings)
    if not musings:
        return counts  # deterministic $0 short-circuit — no LLM

    distil: DistillCall
    if call is not None:
        distil = call
    else:
        # The standing Worldview rides into the prompt so the model revises
        # (same scope_key) instead of stacking a quiet contradiction.
        current = list_tenets(status="current", db_path=db_path)

        def _distil_with_worldview(m: Sequence[AnalystNoteRow]) -> list[ProposedTenet] | None:
            return _default_call(m, current)

        distil = _distil_with_worldview

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
        if tension is None:
            # The free slug probe found nothing — try the semantic probe
            # (B5) before concluding there's no overlap: two tenets can be
            # the same underlying belief under different scope_key slugs.
            # detect_semantic_tension is itself fail-open (never raises),
            # but the call is wrapped again here as defense in depth — a
            # tension-detection failure must never skip the landing.
            try:
                semantic = detect_semantic_tension(
                    body, exclude_scope_key=resolved, db_path=db_path
                )
            except Exception:
                semantic = None
            if semantic is not None:
                counts["tensions_semantic"] += 1
            tension = semantic
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
