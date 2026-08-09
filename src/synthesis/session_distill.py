"""The session-distillation tap (B4) — the keystone of the 2026-07-19 program
overhaul's LLM-native memory rebuild.

Conversations — thin in-app Ask threads AND deep Claude Code sessions bridged
in via ``execution/land_session_notes.py``'s ``transcript`` kind — feed ONE
distillation tap instead of each surface growing its own bespoke synthesis
path. A conversation that has gone idle (Ask) or was landed whole (a bridged
transcript) is read once, turned into AT MOST 5 typed candidates by a single
structured LLM call, and each candidate is deterministically validated before
anything is written:

  * every candidate must cite a real turn shown to the model (the grounding
    gate, mirroring ``synthesis.tenet_distill``'s citation check) — a
    candidate citing nothing real is discarded, never guessed into existence;
  * a ``resolved_question``/``contradiction`` must reference a note/insight
    actually shown in the prompt, not a fabricated id;
  * a ``tenet_revision`` reusing a ``scope_key`` must reuse one that was
    actually shown as a standing tenet.

**Belief revisions AUTO-ADOPT with undo** (owner ruling, 2026-07-19 program
review): a distilled ``tenet_revision``/``stance_revision`` lands ``current``
immediately — superseding the prior belief on that scope — and is announced
via ``synthesis.adoption_notify.announce_adoption`` with a one-tap Revert
button. This is a deliberate departure from ``tenet_distill``'s
propose-then-approve queue: that path exists for owner-flagged (saved/
incorporated) musings, a much smaller and already-curated set; this path
processes the FULL conversational stream, so ratify-first would just become a
second inbox nobody clears. Ratify-first walks remain the law only where
#939 requires them (owner-authored data imports) — a session-distilled belief
revision is machine-derived, so it takes the auto-adopt-with-undo lane.

Two source kinds feed ``candidate_sessions``:

  * **ask** — an ``ask_sessions`` thread idle ≥4h with ≥2 user turns and no
    prior distill pass (``distilled_at IS NULL``). Turns cite as ``[U<n>]``/
    ``[A<n>]`` tokens (n = the turn's 1-based position in the thread).
  * **raw** — a ``raw_capture_sessions`` row landed via the ``transcript``
    bridge kind (``status='captured'``, non-empty transcript, never
    distilled). The bridged transcript isn't split into per-speaker turns at
    land time, so it cites as a single ``[T1]`` token.

Degrade-safety follows the repo's quota-scheduling contract
(``directives/llm_quota_scheduling.md`` rule 3): a transient LLM failure
(``not llm.cli.is_hard_stop``) defers that session — it is NOT marked
distilled, so the next sweep retries it — and tallies ``deferred_transient``.
A hard stop (budget block / missing CLI) propagates so the runner can exit
loudly (``execution/run_session_distill.py`` turns it into exit 2). One bad
session never sinks the whole sweep, but a hard stop is systemic (it will
fail identically for every remaining session), so it is NOT swallowed at the
aggregate level either.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from ask.store import list_undistilled_sessions as _ask_list_undistilled
from ask.store import load_turns as _load_turns
from ask.store import mark_session_distilled as _ask_mark_distilled
from capture.ingest import IngestResult, ingest_capture
from capture.sessions import get_session as _get_raw_session
from capture.sessions import list_undistilled_captured as _raw_list_undistilled
from capture.sessions import mark_distilled as _raw_mark_distilled
from synthesis.insights import InsightRow, get_insight, list_insights, record_insight
from synthesis.semantic_tension import detect_semantic_tension
from synthesis.tenets import current_tenet_for_scope, list_tenets, record_tenet, scope_key_for
from user_state.notes import AnalystNoteRow, list_notes, patch_note_context, resolve_note

log = logging.getLogger(__name__)

PURPOSE = "session_distill"

# A distill call: the rendered prompt -> a list of typed candidate dicts, or
# None on "nothing to distil". Injected so the engine is unit-testable
# without the CLI (mirrors synthesis.tenet_distill.DistillCall).
Candidate = dict[str, object]
DistillCall = Callable[[str], "list[Candidate] | None"]

_OPEN_NOTE_KINDS: tuple[str, ...] = ("question", "musing")
_MAX_CANDIDATES = 5
_MAX_OPEN_NOTES = 15


@dataclass(frozen=True, slots=True)
class _Turn:
    """One citeable unit shown to the model: ``[token] text``."""

    token: str
    role: str
    text: str


@dataclass(frozen=True, slots=True)
class SessionRef:
    """One session eligible for distillation. ``session_id`` is always a str
    (ask ids are already uuid4 hex; raw ids are coerced from int) so callers
    never branch on source to format a log line, receipt key, or CLI arg."""

    source: str  # "ask" | "raw"
    session_id: str

    @property
    def landing_channel(self) -> str:
        """The ``ingest_capture`` channel for anything landed FROM this
        session (a supporting musing, a resolved-question note, ...)."""
        return "ask" if self.source == "ask" else "claude_session"

    def turns(self, *, db_path: Path | str) -> list[_Turn]:
        """The ordered, tokenized turns shown to the model. An ``ask`` thread
        yields one token per real turn (``U``/``A`` prefix + 1-based
        position); a bridged ``raw`` transcript — not split into per-speaker
        turns at land time — yields a single ``T1`` token over the whole
        text."""
        if self.source == "ask":
            rows = _load_turns(self.session_id, db_path=Path(db_path))
            out: list[_Turn] = []
            for i, t in enumerate(rows, start=1):
                prefix = "U" if t.role == "user" else "A"
                out.append(_Turn(token=f"{prefix}{i}", role=t.role, text=t.text))
            return out
        row = _get_raw_session(int(self.session_id), db_path=db_path)
        text = row.transcript if row is not None else ""
        return [_Turn(token="T1", role="transcript", text=text)]


def candidate_sessions(db_path: Path | str, *, now: datetime | None = None) -> list[SessionRef]:
    """The deterministic candidate set: idle/undistilled Ask threads plus
    landed/undistilled bridged transcripts. ``now`` is injectable so a test
    can simulate "N hours idle" without backdating rows via raw SQL."""
    ask_rows = _ask_list_undistilled(idle_hours=4, min_user_turns=2, now=now, db_path=Path(db_path))
    raw_rows = _raw_list_undistilled(db_path=db_path)
    return [SessionRef(source="ask", session_id=s.id) for s in ask_rows] + [
        SessionRef(source="raw", session_id=str(r.id)) for r in raw_rows
    ]


def _build_prompt(
    turns_text: str,
    existing_tenets: Sequence[InsightRow],
    existing_stances: Sequence[InsightRow],
    open_notes: Sequence[AnalystNoteRow],
) -> str:
    """The distill prompt: one conversation in, at most 5 typed candidates
    out, every one grounded in a turn actually shown. Mirrors
    ``tenet_distill``'s v2 quality bars for the ``tenet_revision`` type
    (identity-level, conditional phrasing, revise-don't-stack) since a
    session-distilled Tenet is held to the same durability bar as a
    flagged-musing one — only the adoption posture (auto vs proposed)
    differs."""
    tenets_block = (
        "\n".join(
            f"- [T{t.id}] ({t.scope_key}) {' '.join(t.body_md.split())}" for t in existing_tenets
        )
        or "(none yet)"
    )
    stances_block = (
        "\n".join(
            f"- [S{s.id}] ({s.scope_key}) {' '.join(s.body_md.split())}" for s in existing_stances
        )
        or "(none yet)"
    )
    notes_block = (
        "\n".join(
            f"- [N{n.id}] {(n.body.splitlines()[0] if n.body else '')[:140]}" for n in open_notes
        )
        or "(none open)"
    )
    return (
        "You are reading ONE conversation between the owner (an equity-research "
        "investor) and an assistant — either a thin Ask thread or a bridged deep "
        "working session. Distil it into AT MOST 5 candidates the owner's memory "
        "should absorb. Ground EVERY candidate in a turn actually shown below by "
        "citing its exact bracket token (e.g. [U3], [A4], or [T1] for a bridged "
        "transcript) in `citations` — a candidate citing nothing real is "
        "discarded, never guessed into existence. When in doubt, emit a "
        "musing, not a revision.\n\n"
        "Candidate types:\n"
        "- musing: a stray thought worth keeping, not yet a settled belief or "
        "decision. The safe default.\n"
        "- resolved_question: the conversation settled a standing OPEN "
        "question/musing shown below; put its exact [N<id>] token in "
        "`resolves`.\n"
        "- contradiction: the conversation surfaces tension with a shown "
        "standing tenet or stance; put its exact [T<id>]/[S<id>] token in "
        "`conflicts_with`.\n"
        "- tenet_revision: an IDENTITY-LEVEL, durable belief about HOW the "
        "owner invests (method, discipline, temperament) that generalizes "
        "across holdings and regimes — NOT a call on one name, NOT a lesson "
        "from a single episode restated as a rule. When the evidence pulls in "
        "two directions, phrase ONE conditional belief ('default X; when "
        "<condition>, Y') rather than two flat contradictory rules. If it "
        "overlaps a shown standing tenet, reuse that tenet's EXACT "
        "`scope_key` (a revision, not a fork); a genuinely new belief leaves "
        "`scope_key` null.\n"
        "- stance_revision: a call on ONE holding — `ticker` is required.\n\n"
        "STANDING TENETS (identity-level beliefs):\n" + tenets_block + "\n\n"
        "STANDING STANCES:\n" + stances_block + "\n\n"
        "OPEN QUESTIONS / MUSINGS (id + first line):\n" + notes_block + "\n\n"
        "CONVERSATION (one bracket token per citeable turn):\n" + turns_text + "\n\n"
        "Return JSON ONLY: a list of at most 5 objects, each "
        '{"type": "musing"|"resolved_question"|"contradiction"|"tenet_revision"|'
        '"stance_revision", "text": "<the belief/musing/resolution, in the '
        'owner\'s voice>", "scope_key": "<exact standing tenet scope_key, or '
        'null>", "ticker": "<TICKER, for stance_revision>", "resolves": '
        '"<[N<id>] token, or null>", "conflicts_with": "<[T<id>]/[S<id>] '
        'token, or null>", "citations": ["<turn token>", ...], "why": "<one '
        'sentence>"}. An empty list is a good answer when nothing durable '
        "happened."
    )


def _default_call(prompt: str) -> list[Candidate] | None:
    """The live call — lazy-imported so unit tests never need the CLI. Raises
    on a parse/call failure (deliberately NOT swallowed to ``None`` here): the
    caller's transient/hard-stop classification decides whether to defer-and-
    retry or propagate, and a swallowed parse failure would otherwise mark a
    session permanently distilled without ever having actually distilled it."""
    from llm.contracts import SESSION_CANDIDATE_BATCH_SCHEMA
    from llm.structured import call_llm_structured

    obj = call_llm_structured(
        prompt,
        purpose=PURPOSE,
        scope="session_distill",
        expect="array",
        schema=SESSION_CANDIDATE_BATCH_SCHEMA,
    )
    if not isinstance(obj, list):
        return None
    return [cast("Candidate", x) for x in cast("list[object]", obj) if isinstance(x, dict)]


def _coerce_tokens(raw: object) -> list[str]:
    out: list[str] = []
    if isinstance(raw, list):
        for c in cast("list[object]", raw):
            if isinstance(c, str):
                token = c.strip().strip("[]")
                if token:
                    out.append(token)
    return out


def _land_musing(text: str, *, channel: str, db_path: Path | str) -> IngestResult:
    """Land a supporting musing through the SAME LLM-free capture pipeline
    every other channel uses (capture.ingest.ingest_capture) — a
    tenet/stance revision is always grounded in a durably-landed note, never
    a floating LLM claim with no note_id to cite."""
    return ingest_capture(channel=channel, media_kind="text", text=text, db_path=db_path)


def _announce(
    row: InsightRow, *, kind: str, db_path: Path | str, repo_root: Path | str | None
) -> None:
    """Best-effort Telegram adoption announcement + revert receipt (the other
    B4 workstream's module). Lazy-imported and swallowed: its absence
    mid-development, or any failure in it, must never break distillation —
    the belief has already landed by the time this is called."""
    try:
        from synthesis.adoption_notify import announce_adoption

        announce_adoption(row, kind=kind, db_path=db_path, repo_root=repo_root)
    except Exception:
        log.debug({"event": "session_distill_announce_skipped", "kind": kind, "insight_id": row.id})


def _mark_distilled(ref: SessionRef, *, db_path: Path | str) -> None:
    if ref.source == "ask":
        _ask_mark_distilled(ref.session_id, db_path=Path(db_path))
    else:
        _raw_mark_distilled(int(ref.session_id), db_path=db_path)


def distill_session(
    ref: SessionRef,
    *,
    db_path: Path | str,
    call: DistillCall | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, int]:
    """Distil one session: one structured call, then per-candidate
    deterministic grounding + landing. Returns per-session counts.

    A transient LLM failure defers the session (NOT marked distilled — the
    next sweep retries it); a hard stop propagates (systemic, so retrying is
    pointless — the runner should fail loudly instead)."""
    counts = {
        "musings": 0,
        "resolved": 0,
        "tensions": 0,
        # B5: overlap detected while auto-landing a brand-new tenet_revision
        # candidate (no shown scope_key reused) — distinct from `tensions`
        # above, which is the "contradiction" candidate TYPE's counter and
        # means something different (the model itself flagged conflict with
        # a shown item). `tenet_tensions` is the slug-or-semantic probe run
        # on every NEW tenet landing; `tensions_semantic` is its
        # semantic-only subset, kept observable separately (mirrors
        # tenet_distill's tensions/tensions_semantic pair).
        "tenet_tensions": 0,
        "tensions_semantic": 0,
        "adopted_tenets": 0,
        "adopted_stances": 0,
        "skipped_groundless": 0,
        "deferred_transient": 0,
    }
    turns = [t for t in ref.turns(db_path=db_path) if t.text.strip()]
    if not turns:
        _mark_distilled(ref, db_path=db_path)
        return counts

    existing_tenets = list_tenets(status="current", db_path=db_path)
    existing_stances = list_insights(kind="stance", status="current", db_path=db_path)
    open_notes = [
        n
        for n in list_notes(status="open", db_path=db_path, limit=200)
        if n.kind in _OPEN_NOTE_KINDS
    ][:_MAX_OPEN_NOTES]

    turns_text = "\n".join(f"[{t.token}] {t.text}" for t in turns)
    prompt = _build_prompt(turns_text, existing_tenets, existing_stances, open_notes)

    distil: DistillCall = call if call is not None else _default_call

    from llm.cli import is_hard_stop

    try:
        proposals = distil(prompt)
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning(
            {
                "event": "session_distill_transient",
                "source": ref.source,
                "session_id": ref.session_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        counts["deferred_transient"] += 1
        return counts  # NOT marked distilled — retried next sweep

    if not proposals:
        _mark_distilled(ref, db_path=db_path)
        return counts

    valid_turn_tokens = {t.token for t in turns}
    valid_scope_keys = {t.scope_key for t in existing_tenets}
    valid_insight_tokens = {f"T{t.id}" for t in existing_tenets} | {
        f"S{s.id}" for s in existing_stances
    }
    valid_note_tokens = {f"N{n.id}" for n in open_notes}
    channel = ref.landing_channel

    for cand in proposals[:_MAX_CANDIDATES]:
        # A DistillCall (injected or _default_call) always yields real dicts —
        # _default_call itself filters isinstance(x, dict) on the raw parsed
        # JSON array before returning — so no isinstance re-check here.
        ctype = str(cand.get("type") or "").strip()
        text = str(cand.get("text") or "").strip()
        grounded = sorted(
            c for c in _coerce_tokens(cand.get("citations")) if c in valid_turn_tokens
        )
        # Deterministic grounding gate FIRST (tenet_distill pattern): every
        # candidate must cite real, shown material or it's discarded outright
        # — no fabricated "you said X" pointing at nothing it was given.
        if not text or not grounded:
            counts["skipped_groundless"] += 1
            continue

        if ctype == "musing":
            _land_musing(text, channel=channel, db_path=db_path)
            counts["musings"] += 1

        elif ctype == "resolved_question":
            token = str(cand.get("resolves") or "").strip().strip("[]")
            if token not in valid_note_tokens:
                counts["skipped_groundless"] += 1
                continue
            resolve_note(int(token[1:]), resolution_note=text, db_path=db_path)
            counts["resolved"] += 1

        elif ctype == "contradiction":
            token = str(cand.get("conflicts_with") or "").strip().strip("[]")
            if token not in valid_insight_tokens:
                counts["skipped_groundless"] += 1
                continue
            # No public synthesis.insights helper stamps a tension onto an
            # existing row without a direct meta mutation, and insights.py is
            # the sibling B4 agent's file — never edited here. Land the
            # contradiction as a musing carrying a `tension_ref` context
            # patch instead (a durable, queryable pointer at the conflicting
            # insight) rather than silently dropping the signal.
            landed = _land_musing(text, channel=channel, db_path=db_path)
            if landed.note_id is not None:
                patch_note_context(
                    landed.note_id, {"tension_ref": {"insight_token": token}}, db_path=db_path
                )
            counts["tensions"] += 1

        elif ctype == "tenet_revision":
            raw_scope = cand.get("scope_key")
            scope_key: str | None = None
            if isinstance(raw_scope, str) and raw_scope.strip():
                candidate_scope = raw_scope.strip()
                if candidate_scope not in valid_scope_keys:
                    # A scope_key not among the shown standing tenets is a
                    # fabricated "revision" reference — grounding failure.
                    counts["skipped_groundless"] += 1
                    continue
                scope_key = candidate_scope
            landed = _land_musing(text, channel=channel, db_path=db_path)
            if landed.note_id is None:
                counts["skipped_groundless"] += 1
                continue
            tension_id: int | None = None
            if scope_key is None:
                # A genuinely NEW tenet (no shown scope_key reused) — run the
                # same slug-then-semantic tension probe tenet_distill runs
                # before landing (B5): the derived scope might collide with a
                # standing tenet outright (free), else check semantically —
                # the gap that let prod tenets 20/31 land as silent
                # duplicates under different slugs. A detected tension still
                # lands (auto-adopt per the owner ruling); it just carries
                # tension_with so the worldview tension badge shows. Wrapped
                # again here as defense in depth (on top of
                # detect_semantic_tension's own fail-open contract) — a
                # tension-detection failure must never skip the landing.
                resolved_scope = scope_key_for(text, None)
                slug_match = current_tenet_for_scope(resolved_scope, db_path=db_path)
                if slug_match is not None:
                    tension_id = slug_match.id
                    counts["tenet_tensions"] += 1
                else:
                    try:
                        semantic_match = detect_semantic_tension(
                            text, exclude_scope_key=resolved_scope, db_path=db_path
                        )
                    except Exception:
                        semantic_match = None
                    if semantic_match is not None:
                        tension_id = semantic_match.id
                        counts["tenet_tensions"] += 1
                        counts["tensions_semantic"] += 1
            tenet_row = record_tenet(
                body_md=text,
                scope_key=scope_key,
                source_note_ids=(landed.note_id,),
                status="current",
                provenance="session_distill",
                tension_with=tension_id,
                db_path=db_path,
            )
            counts["adopted_tenets"] += 1
            _announce(tenet_row, kind="tenet", db_path=db_path, repo_root=repo_root)

        elif ctype == "stance_revision":
            ticker = str(cand.get("ticker") or "").strip().upper()
            if not ticker:
                counts["skipped_groundless"] += 1
                continue
            landed = _land_musing(text, channel=channel, db_path=db_path)
            if landed.note_id is None:
                counts["skipped_groundless"] += 1
                continue
            new_id = record_insight(
                scope_key=ticker,
                kind="stance",
                body_md=text,
                source_note_ids=[landed.note_id],
                watermark_id=landed.note_id,
                provenance="session_distill",
                status="current",
                db_path=db_path,
            )
            counts["adopted_stances"] += 1
            stance_row = get_insight(new_id, db_path=db_path)
            if stance_row is not None:
                _announce(stance_row, kind="stance", db_path=db_path, repo_root=repo_root)

        else:
            counts["skipped_groundless"] += 1

    _mark_distilled(ref, db_path=db_path)
    return counts


def run_session_distill(
    db_path: Path | str,
    *,
    call: DistillCall | None = None,
    now: datetime | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, int]:
    """Sweep every candidate session, aggregating counts. A per-session bug
    is logged and skipped (one bad session never sinks the sweep) — but a
    hard stop propagates out of the sweep entirely: it is a systemic
    condition (blown budget / missing CLI) that will fail identically for
    every remaining session, so continuing would just burn through the rest
    of the candidate set for nothing."""
    from llm.cli import is_hard_stop

    refs = candidate_sessions(db_path, now=now)
    total: dict[str, int] = {
        "sessions": len(refs),
        "musings": 0,
        "resolved": 0,
        "tensions": 0,
        "tenet_tensions": 0,
        "tensions_semantic": 0,
        "adopted_tenets": 0,
        "adopted_stances": 0,
        "skipped_groundless": 0,
        "deferred_transient": 0,
    }
    for ref in refs:
        try:
            counts = distill_session(ref, db_path=db_path, call=call, repo_root=repo_root)
        except Exception as exc:
            if is_hard_stop(exc):
                raise
            log.error(
                {
                    "event": "session_distill_session_error",
                    "source": ref.source,
                    "session_id": ref.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
    return total
