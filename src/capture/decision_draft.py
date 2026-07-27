"""Decision Draft — the typed async parse of a landed capture (PRD
``docs/design/personal_investment_partner_prd.md`` §9.2, P2.1).

A free-form Telegram/mobile/web capture lands deterministically through
``capture.ingest`` — LLM-FREE, never blocked. This module is a SEPARATE
fire-and-forget tap (the same shape as ``research.proposals.detect_and_create_task``
/ the artifact-brief tap): it rides the already-landed ``analyst_notes`` row,
classifies the owner's intent with its OWN governed purpose
(``decision_draft_parse`` — NOT a branch inside ``capture.triage``, NOT a
second classifier competing on the send path), and — for anything more
consequential than an ordinary musing — writes ONE ``decision_drafts`` row
(migration 0195) the owner confirms/corrects/dismisses later.

Confirming/correcting (``capture.decision_draft_actions``) is the ONLY path
that creates/updates an Owner Decision. This module owns parsing + storage
only.

State machine actually exercised by this module (the full closed set is
``captured -> parsed -> awaiting_confirmation -> confirmed|corrected|
dismissed|expired|parse_failed`` — ``confirmed``/``corrected``/``dismissed``/
``expired`` are written by ``decision_draft_actions``, never here):

  * intent != 'musing', parse succeeds -> one row, status='awaiting_confirmation'.
  * intent == 'musing' -> NO row (PRD: "ordinary musing -> NO row").
  * a hard parse failure (LLM error after `call_llm_structured`'s own retry,
    or a validation failure) -> one row, status='parse_failed', draft_json
    NULL, original_text always intact.
  * a hard STOP (budget exhausted / setup broken) propagates per the repo's
    LLM exception policy — the poller tap catches it with the same
    blanket-except every sibling tap uses (coach_reply's dispatch call site),
    so a hard stop degrades to "no draft this time", never a broken capture.

Idempotent: :func:`parse_note` looks up an existing draft for the note first
(``idempotency_key = "note:<note_id>"``) — reprocessing the same note is a
no-op returning the existing row id, never a duplicate.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, field_validator

from capture.matcher import RosterIndex, load_roster
from llm.untrusted import spotlight
from user_state._db import now_iso, open_conn
from user_state.notes import get_note

log = logging.getLogger(__name__)

PURPOSE = "decision_draft_parse"

DraftIntent = Literal[
    "executed_change", "disposition", "rationale", "correction", "request", "musing"
]
INTENTS: tuple[str, ...] = (
    "executed_change",
    "disposition",
    "rationale",
    "correction",
    "request",
    "musing",
)
ACTION_VOCAB: frozenset[str] = frozenset(
    {"buy", "sell", "add", "trim", "hold", "pass", "watch", "promote"}
)
# Research-disposition actions (PRD §7.4/§8.1) — these reuse
# research.investment_decision_card.act_on_card when a card artifact is
# linked; see capture.decision_draft_actions.
DISPOSITION_ACTIONS: frozenset[str] = frozenset({"pass", "watch", "promote"})

STATUSES: tuple[str, ...] = (
    "captured",
    "parsed",
    "awaiting_confirmation",
    "confirmed",
    "corrected",
    "dismissed",
    "expired",
    "parse_failed",
)
# Statuses the mobile Inbox / desktop / Telegram treat as "still needs the
# owner's attention" (PRD §11.6 GET /api/decision-drafts).
PENDING_STATUSES: tuple[str, ...] = ("awaiting_confirmation",)


class DecisionDraft(BaseModel):
    """The ``draft_json`` content — the interpreted, owner-reviewable shape
    of one capture. Every field is optional except ``intent``/``parse_confidence``:
    the parser must prefer an ambiguous draft (empty ticker, an ``ambiguity``
    note) over a false consequential mutation (PRD §10.5)."""

    intent: DraftIntent
    ticker_candidates: list[str] = Field(default_factory=list[str])
    proposed_ticker: str | None = None
    proposed_action: str | None = None
    proposed_amount_usd: float | None = None
    proposed_amount_pct: float | None = None
    proposed_rationale: str | None = None
    linked_advice_artifact_id: int | None = None
    parse_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    ambiguity: list[str] = Field(default_factory=list[str])

    @field_validator("proposed_action")
    @classmethod
    def _closed_action_vocab(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in ACTION_VOCAB:
            raise ValueError(f"proposed_action must be one of {sorted(ACTION_VOCAB)}, got {v!r}")
        return v

    @field_validator("proposed_ticker")
    @classmethod
    def _upper_ticker(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else None

    @field_validator("ticker_candidates")
    @classmethod
    def _upper_candidates(cls, v: list[str]) -> list[str]:
        return [str(t).strip().upper() for t in v if str(t).strip()]


@dataclass(frozen=True, slots=True)
class DecisionDraftRow:
    """One persisted ``decision_drafts`` row, fully decoded."""

    id: int
    user_id: str
    source_note_id: int | None
    source_channel: str
    source_external_id: str | None
    source_provider_id: str | None
    idempotency_key: str
    original_text: str
    transcription_json: dict[str, object] | None
    draft: DecisionDraft | None
    parse_confidence: float | None
    status: str
    prompt_version: str | None
    model: str | None
    llm_call_id: int | None
    decision_id: int | None
    expires_at: str | None
    created_at: str
    updated_at: str
    confirmed_at: str | None
    dismissed_at: str | None
    ambiguity: list[str] = field(default_factory=list[str])


def _decode_json(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        decoded = json.loads(str(raw))
    except (ValueError, TypeError):
        return None
    return cast("dict[str, object]", decoded) if isinstance(decoded, dict) else None


def _row_to_dc(row: sqlite3.Row) -> DecisionDraftRow:
    draft_dict = _decode_json(row["draft_json"])
    draft: DecisionDraft | None = None
    if draft_dict is not None:
        try:
            draft = DecisionDraft.model_validate(draft_dict)
        except ValueError:
            draft = None
    return DecisionDraftRow(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        source_note_id=None if row["source_note_id"] is None else int(row["source_note_id"]),
        source_channel=str(row["source_channel"]),
        source_external_id=(
            None if row["source_external_id"] is None else str(row["source_external_id"])
        ),
        source_provider_id=(
            None
            if "source_provider_id" not in set(row.keys()) or row["source_provider_id"] is None
            else str(row["source_provider_id"])
        ),
        idempotency_key=str(row["idempotency_key"]),
        original_text=str(row["original_text"]),
        transcription_json=_decode_json(row["transcription_json"]),
        draft=draft,
        parse_confidence=(
            None if row["parse_confidence"] is None else float(row["parse_confidence"])
        ),
        status=str(row["status"]),
        prompt_version=None if row["prompt_version"] is None else str(row["prompt_version"]),
        model=None if row["model"] is None else str(row["model"]),
        llm_call_id=None if row["llm_call_id"] is None else int(row["llm_call_id"]),
        decision_id=None if row["decision_id"] is None else int(row["decision_id"]),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        confirmed_at=None if row["confirmed_at"] is None else str(row["confirmed_at"]),
        dismissed_at=None if row["dismissed_at"] is None else str(row["dismissed_at"]),
        ambiguity=list(draft.ambiguity) if draft is not None else [],
    )


def get_draft(
    draft_id: int,
    *,
    db_path: Path | str | None = None,
    connection: sqlite3.Connection | None = None,
) -> DecisionDraftRow | None:
    owns_connection = connection is None
    conn = open_conn(db_path) if connection is None else connection
    try:
        row = conn.execute("SELECT * FROM decision_drafts WHERE id = ?", (draft_id,)).fetchone()
        return None if row is None else _row_to_dc(row)
    except sqlite3.Error:
        return None
    finally:
        if owns_connection:
            conn.close()


def get_tracker_draft_group(
    draft_id: int,
    *,
    connection: sqlite3.Connection,
) -> tuple[DecisionDraftRow, list[DecisionDraftRow]] | None:
    """Read one tracker group through the caller's serialized transaction."""
    representative_row = connection.execute(
        "SELECT * FROM decision_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    if representative_row is None:
        return None
    representative = _row_to_dc(representative_row)
    if representative.source_channel != "tracker" or not representative.source_external_id:
        return representative, [representative]
    group_rows = connection.execute(
        "SELECT * FROM decision_drafts "
        "WHERE source_channel = 'tracker' AND source_external_id = ? ORDER BY id",
        (representative.source_external_id,),
    ).fetchall()
    return representative, [_row_to_dc(row) for row in group_rows]


def get_draft_by_idempotency_key(
    key: str, *, db_path: Path | str | None = None
) -> DecisionDraftRow | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM decision_drafts WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return None if row is None else _row_to_dc(row)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def get_draft_for_note(
    note_id: int, *, db_path: Path | str | None = None
) -> DecisionDraftRow | None:
    return get_draft_by_idempotency_key(f"note:{note_id}", db_path=db_path)


def list_pending_drafts(
    *, statuses: tuple[str, ...] = PENDING_STATUSES, db_path: Path | str | None = None
) -> list[DecisionDraftRow]:
    """Newest-first drafts still needing the owner's attention — the mobile
    Inbox / ``GET /api/decision-drafts`` read model. Best-effort ``[]`` on a
    missing table (a pre-migration DB must never 500 the Inbox)."""
    if not statuses:
        return []
    conn = open_conn(db_path)
    try:
        placeholders = ", ".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM decision_drafts WHERE status IN ({placeholders}) ORDER BY id DESC",
            statuses,
        ).fetchall()
        return [_row_to_dc(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _insert_row(
    *,
    user_id: str,
    source_note_id: int | None,
    source_channel: str,
    source_external_id: str | None,
    source_provider_id: str | None,
    idempotency_key: str,
    original_text: str,
    transcription_json: dict[str, object] | None,
    draft: DecisionDraft | None,
    parse_confidence: float | None,
    status: str,
    prompt_version: str | None,
    model: str | None,
    llm_call_id: int | None,
    expires_at: str | None,
    db_path: Path | str | None,
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO decision_drafts ("
            " user_id, source_note_id, source_channel, source_external_id, source_provider_id,"
            " idempotency_key, original_text, transcription_json, draft_json,"
            " parse_confidence, status, prompt_version, model, llm_call_id,"
            " expires_at, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                source_note_id,
                source_channel,
                source_external_id,
                source_provider_id,
                idempotency_key,
                original_text,
                json.dumps(transcription_json) if transcription_json is not None else None,
                draft.model_dump_json() if draft is not None else None,
                parse_confidence,
                status,
                prompt_version,
                model,
                llm_call_id,
                expires_at,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def create_draft_row(
    *,
    user_id: str = "bhanu",
    source_note_id: int | None,
    source_channel: str,
    source_external_id: str | None,
    idempotency_key: str,
    original_text: str,
    draft: DecisionDraft | None,
    status: str = "awaiting_confirmation",
    transcription_json: dict[str, object] | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    llm_call_id: int | None = None,
    expires_at: str | None = None,
    source_provider_id: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    """The one low-level writer both :func:`parse_note` (source_channel
    'telegram'/'web') and the tracker-fill branch
    (``decision_extractor.reconcile_decision_actions``, source_channel
    'tracker') use. Idempotent on ``idempotency_key`` — a second call with the
    same key returns the existing row's id without inserting."""
    if status not in STATUSES:
        raise ValueError(f"unknown decision_drafts status {status!r}; expected one of {STATUSES}")
    existing = get_draft_by_idempotency_key(idempotency_key, db_path=db_path)
    if existing is not None:
        return existing.id
    return _insert_row(
        user_id=user_id,
        source_note_id=source_note_id,
        source_channel=source_channel,
        source_external_id=source_external_id,
        source_provider_id=source_provider_id,
        idempotency_key=idempotency_key,
        original_text=original_text,
        transcription_json=transcription_json,
        draft=draft,
        parse_confidence=draft.parse_confidence if draft is not None else None,
        status=status,
        prompt_version=prompt_version,
        model=model,
        llm_call_id=llm_call_id,
        expires_at=expires_at,
        db_path=db_path,
    )


def set_draft_status(
    draft_id: int,
    status: str,
    *,
    decision_id: int | None = None,
    confirmed_at: str | None = None,
    dismissed_at: str | None = None,
    draft: DecisionDraft | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Transition an existing row. ``draft`` (when passed) overwrites
    ``draft_json`` with the corrected fields — ``original_text`` is NEVER
    touched here (PRD: "original text never overwritten")."""
    if status not in STATUSES:
        raise ValueError(f"unknown decision_drafts status {status!r}; expected one of {STATUSES}")
    conn = open_conn(db_path)
    try:
        sets = ["status = ?", "updated_at = ?"]
        params: list[object] = [status, now_iso()]
        if decision_id is not None:
            sets.append("decision_id = ?")
            params.append(decision_id)
        if confirmed_at is not None:
            sets.append("confirmed_at = ?")
            params.append(confirmed_at)
        if dismissed_at is not None:
            sets.append("dismissed_at = ?")
            params.append(dismissed_at)
        if draft is not None:
            sets.append("draft_json = ?")
            params.append(draft.model_dump_json())
        params.append(draft_id)
        conn.execute(f"UPDATE decision_drafts SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The governed decision_draft_parse purpose
# ---------------------------------------------------------------------------

_PROMPT_HEAD = """You read one free-form note a portfolio owner captured (by voice or text) \
about their OWN investing activity. Classify its intent and extract only what it \
clearly states or implies. Never invent a ticker, action, or number that is not \
grounded in the text.

Known tracked tickers (map a company name/obvious alias to one of these EXACT \
symbols; if the note names a company NOT in this list, still name it in \
`ticker_candidates` as free text but leave `proposed_ticker` null): {roster}

intent (exactly one):
  "executed_change" — the owner already bought/sold/added/trimmed/held a position.
  "disposition"      — an explicit Pass / Watch / Promote research call on a name
                        (NOT an executed trade).
  "rationale"         — the owner is explaining WHY behind a decision (no new
                        action stated).
  "correction"        — the owner is correcting/updating prior context (a wrong
                        fact, an outdated belief) — not a new action.
  "request"           — a question or ask directed at the assistant.
  "musing"            — none of the above; an ordinary passing thought.

Fields:
  intent: one of the six above.
  ticker_candidates: array of ticker/company mentions found (free text ok).
  proposed_ticker: the SINGLE resolved symbol from the known list, or null if
    ambiguous, absent, or not on the list.
  proposed_action: one of "buy"|"sell"|"add"|"trim"|"hold"|"pass"|"watch"|"promote",
    or null.
  proposed_amount_usd: a stated dollar amount, or null.
  proposed_amount_pct: a stated percent-of-position size, or null (never both
    guessed when only one unit was stated).
  proposed_rationale: a one-line paraphrase of the stated reasoning, or null.
  parse_confidence: your confidence in this whole extraction, 0.0-1.0.
  ambiguity: array of short strings naming anything unclear (e.g. "which
    ticker?", "buy or add?") — PREFER listing an ambiguity over guessing.

Return JSON ONLY, exactly this shape:
{{"intent": "...", "ticker_candidates": [...], "proposed_ticker": null, \
"proposed_action": null, "proposed_amount_usd": null, "proposed_amount_pct": null, \
"proposed_rationale": null, "parse_confidence": 0.0, "ambiguity": []}}

Note:
"""


def _build_prompt(text: str, roster_tickers: tuple[str, ...]) -> str:
    head = _PROMPT_HEAD.format(roster=", ".join(roster_tickers) or "(none tracked)")
    return head + spotlight(text, source="owner capture (Telegram/mobile/web)")


DraftCall = Callable[[str, "tuple[str, ...]"], "dict[str, object]"]


def _default_call(text: str, roster_tickers: tuple[str, ...]) -> dict[str, object]:
    """The real ``decision_draft_parse`` call. Lazily imports ``llm.structured``
    so importing this module (and the poller/tap chain) stays LLM-free until a
    landed note actually reaches this function."""
    from llm.structured import call_llm_structured

    payload = call_llm_structured(
        _build_prompt(text, roster_tickers),
        purpose=PURPOSE,
        expect="object",
        required_keys=("intent",),
    )
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else {}


def _coerce_draft(raw: dict[str, object], *, roster: RosterIndex) -> DecisionDraft | None:
    """Validate the model's raw JSON into a :class:`DecisionDraft`, dropping
    (never trusting) a ticker the roster doesn't confidently resolve — mirrors
    ``capture.decision_capture.extract_decision``'s drop-unknown-fields
    discipline. Returns None when the shape is unusable (parse_failed)."""
    intent = raw.get("intent")
    if intent not in INTENTS:
        return None
    known = set(roster.symbol_to_ticker.values()) | set(roster.phrase_to_ticker.values())
    proposed_ticker = raw.get("proposed_ticker")
    resolved_ticker: str | None = None
    if isinstance(proposed_ticker, str) and proposed_ticker.strip():
        candidate = proposed_ticker.strip().upper()
        if candidate in known:
            resolved_ticker = candidate
    candidates_raw = raw.get("ticker_candidates")
    candidates: list[str] = (
        [str(c) for c in cast("list[object]", candidates_raw)]
        if isinstance(candidates_raw, list)
        else []
    )
    action_raw = raw.get("proposed_action")
    action = action_raw if isinstance(action_raw, str) else None
    rationale_raw = raw.get("proposed_rationale")
    rationale = (
        rationale_raw.strip() if isinstance(rationale_raw, str) and rationale_raw.strip() else None
    )
    ambiguity_raw = raw.get("ambiguity")
    ambiguity: list[str] = (
        [str(a) for a in cast("list[object]", ambiguity_raw)]
        if isinstance(ambiguity_raw, list)
        else []
    )
    try:
        return DecisionDraft(
            intent=cast("DraftIntent", intent),
            ticker_candidates=candidates,
            proposed_ticker=resolved_ticker,
            proposed_action=action,
            proposed_amount_usd=_as_float(raw.get("proposed_amount_usd")),
            proposed_amount_pct=_as_float(raw.get("proposed_amount_pct")),
            proposed_rationale=rationale,
            parse_confidence=_as_float(raw.get("parse_confidence")) or 0.0,
            ambiguity=ambiguity,
        )
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _link_advice_artifact(ticker: str | None, *, db_path: Path | str | None) -> int | None:
    """The latest CURRENT ``investment_decision_card`` for the resolved ticker,
    else the latest CURRENT portfolio-scope ``incremental_dollar_recommendation``
    — the plausible advice the draft is confirming/correcting against (PRD
    §9.2 "proposed link to existing advice/card"). Best-effort None on any
    read failure; never blocks the draft write."""
    conn = open_conn(db_path)
    try:
        if ticker:
            row = conn.execute(
                "SELECT id FROM llm_artifacts WHERE purpose = 'investment_decision_card' "
                "AND UPPER(ticker) = ? AND superseded_by_id IS NULL "
                "ORDER BY generated_at DESC LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
            if row is not None:
                return int(row[0])
        row = conn.execute(
            "SELECT id FROM llm_artifacts WHERE purpose = 'incremental_dollar_recommendation' "
            "AND scope = 'portfolio' AND superseded_by_id IS NULL "
            "ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def parse_note(
    note_id: int,
    *,
    db_path: Path | str | None,
    repo_root: Path | None = None,
    call: DraftCall | None = None,
) -> int | None:
    """The fire-and-forget parse: load the landed note, classify + extract,
    link a plausible advice artifact, write the draft row.

    Returns the new/existing draft row id, or None when nothing was written
    (an ordinary musing, or the note is missing/not a musing). Idempotent —
    reprocessing the same note never duplicates a row. Propagates a genuine
    LLM hard stop (budget/setup) per the repo's exception policy; the caller
    (the poller tap) is responsible for the blanket-except that keeps a hard
    stop from ever affecting capture, mirroring every sibling tap."""
    del repo_root  # reserved for a future repo-root-scoped roster/cache; unused today
    note = get_note(note_id, db_path=db_path)
    if note is None or note.kind != "musing":
        return None
    idempotency_key = f"note:{note_id}"
    existing = get_draft_by_idempotency_key(idempotency_key, db_path=db_path)
    if existing is not None:
        return existing.id

    roster = load_roster(db_path)
    roster_tickers = tuple(
        sorted(set(roster.symbol_to_ticker.values()) | set(roster.phrase_to_ticker.values()))
    )
    caller = call or _default_call

    try:
        raw = caller(note.body, roster_tickers)
    except Exception as exc:
        from llm.cli import is_hard_stop

        if is_hard_stop(exc):
            raise
        log.warning({"event": "decision_draft_parse_failed", "note_id": note_id}, exc_info=True)
        return create_draft_row(
            source_note_id=note_id,
            source_channel=str((note.context or {}).get("channel") or "unknown"),
            source_external_id=None,
            idempotency_key=idempotency_key,
            original_text=note.body,
            draft=None,
            status="parse_failed",
            db_path=db_path,
        )

    draft = _coerce_draft(raw, roster=roster)
    if draft is None:
        return create_draft_row(
            source_note_id=note_id,
            source_channel=str((note.context or {}).get("channel") or "unknown"),
            source_external_id=None,
            idempotency_key=idempotency_key,
            original_text=note.body,
            draft=None,
            status="parse_failed",
            db_path=db_path,
        )

    if draft.intent == "musing":
        return None  # PRD: ordinary musing -> NO row

    linked = _link_advice_artifact(draft.proposed_ticker, db_path=db_path)
    if linked is not None:
        draft = draft.model_copy(update={"linked_advice_artifact_id": linked})

    channel = str((note.context or {}).get("channel") or "unknown")
    media_kind = (note.context or {}).get("media_kind")
    transcription_json = {"media_kind": media_kind} if media_kind else None

    return create_draft_row(
        source_note_id=note_id,
        source_channel=channel,
        source_external_id=None,
        idempotency_key=idempotency_key,
        original_text=note.body,
        draft=draft,
        status="awaiting_confirmation",
        transcription_json=transcription_json,
        prompt_version=None,  # defaults to "v1" via llm.prompt_versions
        model=None,
        db_path=db_path,
    )


def parse_note_for_eval(case: dict[str, object]) -> dict[str, object]:
    """The eval-harness production entry point (evals.golden_classifiers):
    a case dict carrying its own ``text`` + ``roster`` (a list of tracked
    symbols) -> the pinned fields the golden set grades. Mirrors
    ``capture.triage.classify_capture_triage_for_eval`` (case-embedded
    context so the golden set never needs a fixture DB)."""
    text = str(case.get("text") or "")
    roster_raw = case.get("roster")
    roster_tickers = (
        tuple(str(t).strip().upper() for t in cast("list[object]", roster_raw))
        if isinstance(roster_raw, list)
        else ()
    )
    roster = RosterIndex(symbol_to_ticker={t: t for t in roster_tickers}, phrase_to_ticker={})
    try:
        raw = _default_call(text, roster_tickers)
    except Exception:
        return {"intent": None, "parse_confidence": 0.0}
    draft = _coerce_draft(raw, roster=roster)
    if draft is None:
        return {"intent": None, "parse_confidence": 0.0}
    return draft.model_dump()


__all__ = [
    "ACTION_VOCAB",
    "DISPOSITION_ACTIONS",
    "INTENTS",
    "PENDING_STATUSES",
    "PURPOSE",
    "STATUSES",
    "DecisionDraft",
    "DecisionDraftRow",
    "create_draft_row",
    "get_draft",
    "get_draft_by_idempotency_key",
    "get_draft_for_note",
    "get_tracker_draft_group",
    "list_pending_drafts",
    "parse_note",
    "parse_note_for_eval",
    "set_draft_status",
]
