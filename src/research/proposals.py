"""research_tasks / research_proposals store + the fire-and-forget detection tap.

Follows the codebase store convention (writers own a connection + commit; readers
best-effort ``[]`` on a missing table, the ``signals/store.py`` pattern). It owns
the two Phase-1 lifecycles and exposes ``act_on_proposal`` — the ONE action core
both the inbox HTMX route and the Telegram callback dispatch call (no logic
duplication).

The detection tap (``detect_and_create_task``) is fire-and-forget: it classifies
a freshly-landed musing by INTENT (``research.intent.classify_intent``) and routes
— a ``wondering`` runs a SECOND routing triage (``research.triage.classify_triage``,
B7) that decides ``answer_now`` / ``belief_candidate`` / ``research_task`` (only the
last of these writes a ``proposed`` task — the inert chip); a
``brief_artifact``/``stress_artifact`` records an engage-intent on the note for the
artifact pipeline; an ``observation`` is inert. It NEVER auto-runs the research
pass, and a detection failure NEVER affects capture (it swallows errors, returns
None).

The ``research_tasks`` columns ``estimated_cost_usd`` and
``task_metadata_json`` map to the API fields ``estimated_cost_usd`` and
decoded ``metadata`` respectively:
``estimated_cost_usd`` holds the triage's STATED cost estimate for the RUN button/
packet line, and ``task_metadata_json`` holds a small JSON metadata blob
(``{"session_prompt": ..., "packeted_at": ...,
"unanswered_weeks": ...}``) — see :func:`_encode_task_metadata` /
:func:`_decode_task_metadata` and
:func:`set_task_extras`.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from research.intent import IntentCall, classify_intent
from research.triage import TriageCall, build_session_prompt, classify_triage, estimate_cost_usd
from user_state._db import now_iso, open_conn, open_read_conn
from user_state.notes import get_note, patch_note_context

TASK_STATUSES: tuple[str, ...] = (
    "proposed",
    "running",
    "drafted",
    "approved",
    "rejected",
    "superseded",
    # Swept by execution/expire_stale_research.py: a 'proposed' wondering nobody
    # ran for N days leaves the queue so pending counts stay trustworthy.
    "expired",
)
PROPOSAL_VERBS: tuple[str, ...] = ("approve", "further", "steer", "reject")

# The detection tap runs by default (the regex pre-gate keeps it cheap — only
# wondering-shaped musings reach the LLM). Set LEDGER_RESEARCH_TAP=0 to disable.
_TAP_OFF = frozenset({"0", "false", "no", ""})


def tap_enabled() -> bool:
    return os.environ.get("LEDGER_RESEARCH_TAP", "1").strip().lower() not in _TAP_OFF


# The research RUN (the expensive web pass) is OFF by default — detection produces
# inert chips, but nothing spends research $ until the owner opts in. The run route
# and the "Research it" button are both gated on this.
_RUN_ON = frozenset({"1", "true", "yes", "on"})


def research_run_enabled() -> bool:
    return os.environ.get("LEDGER_RESEARCH_RUN", "0").strip().lower() in _RUN_ON


def _encode_task_metadata(metadata: dict[str, object]) -> str | None:
    """Encode task metadata for the ``task_metadata_json`` TEXT column.
    Empty metadata encodes to None (NULL), matching the column's nullable
    intent rather than storing a noisy ``'{}'`` on every legacy row."""
    return json.dumps(metadata) if metadata else None


def _decode_task_metadata(raw: object) -> dict[str, object]:
    """Guarded decode: NULL / pre-B7 garbage / non-dict shapes -> {} (never
    raises — a bad row must not take the Ledger down)."""
    if raw is None:
        return {}
    try:
        decoded: object = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}
    return cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}


@dataclass(frozen=True, slots=True)
class ResearchTask:
    id: int
    note_id: int | None
    claim: str
    ticker: str | None
    status: str
    # B7: the triage's stated cost estimate and decoded task metadata. Both
    # default so every pre-B7 call site building a ResearchTask
    # by hand (tests, mostly) keeps working unchanged.
    estimated_cost_usd: float | None = None
    metadata: dict[str, object] = field(default_factory=dict[str, object])
    # B7: needed by the weekly-packet's expiring-research query (age since
    # creation); naive-UTC ISO text, like every other stamp in this store.
    created_at: str = ""


def create_task(
    *,
    note_id: int | None,
    claim: str,
    ticker: str | None,
    estimated_cost_usd: float | None = None,
    metadata: dict[str, object] | None = None,
    db_path: Path | str | None = None,
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO research_tasks "
            "(note_id, claim, ticker, status, estimated_cost_usd, task_metadata_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)",
            (
                note_id,
                claim,
                ticker,
                estimated_cost_usd,
                _encode_task_metadata(metadata or {}),
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def set_task_extras(
    task_id: int,
    *,
    estimated_cost_usd: float | None = None,
    session_prompt: str | None = None,
    packeted_at: str | None = None,
    unanswered_weeks: int | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Merge-patch a task's extras (read-merge-write on the
    ``task_metadata_json`` blob, like :func:`user_state.notes.patch_note_context`).
    Only the passed fields change; everything else in the metadata blob (and an
    explicit ``estimated_cost_usd`` NULL-vs-unset) is preserved. A missing task is a silent
    no-op — this is best-effort bookkeeping, never a write anyone blocks on."""
    task = get_task(task_id, db_path=db_path)
    if task is None:
        return
    metadata = dict(task.metadata)
    if session_prompt is not None:
        metadata["session_prompt"] = session_prompt
    if packeted_at is not None:
        metadata["packeted_at"] = packeted_at
    if unanswered_weeks is not None:
        metadata["unanswered_weeks"] = unanswered_weeks
    new_cost = estimated_cost_usd if estimated_cost_usd is not None else task.estimated_cost_usd
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE research_tasks SET estimated_cost_usd = ?, task_metadata_json = ?, "
            "updated_at = ? WHERE id = ?",
            (new_cost, _encode_task_metadata(metadata), now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> ResearchTask:
    # sqlite3.Row has no real __contains__ (bare `in row` tests VALUES, not
    # column names — a classic footgun), so materialize .keys() once, like
    # _row_to_proposal below.
    keys = set(row.keys())
    return ResearchTask(
        id=int(row["id"]),
        note_id=None if row["note_id"] is None else int(row["note_id"]),
        claim=str(row["claim"]),
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        status=str(row["status"]),
        estimated_cost_usd=(
            float(cast("float", row["estimated_cost_usd"]))
            if "estimated_cost_usd" in keys and row["estimated_cost_usd"] is not None
            else None
        ),
        metadata=(
            _decode_task_metadata(row["task_metadata_json"]) if "task_metadata_json" in keys else {}
        ),
        created_at=str(row["created_at"]) if "created_at" in keys else "",
    )


def get_task(task_id: int, *, db_path: Path | str | None = None) -> ResearchTask | None:
    conn = open_read_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _row_to_task(row)
    finally:
        conn.close()


def get_task_for_note(note_id: int, *, db_path: Path | str | None = None) -> ResearchTask | None:
    """The newest research_task for a note, or None. The idempotency key for the
    On My Mind 'incorporate' action — one note must never spawn two tasks."""
    conn = open_read_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM research_tasks WHERE note_id = ? ORDER BY id DESC LIMIT 1",
            (note_id,),
        ).fetchone()
        return None if row is None else _row_to_task(row)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def get_tasks_for_notes(
    note_ids: list[int], *, db_path: Path | str | None = None
) -> dict[int, ResearchTask]:
    """BATCHED note_id → newest ResearchTask, in ONE query (no N+1 across a feed
    page). The On My Mind feed calls this once per page to render the Wondering
    badge inline. Best-effort ``{}`` on a missing table."""
    ids = [int(n) for n in note_ids]
    if not ids:
        return {}
    conn = open_read_conn(db_path)
    try:
        placeholders = ", ".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM research_tasks WHERE note_id IN ({placeholders}) ORDER BY id ASC",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    # id ASC + last-write-wins → the newest task per note_id survives.
    out: dict[int, ResearchTask] = {}
    for r in rows:
        task = _row_to_task(r)
        if task.note_id is not None:
            out[task.note_id] = task
    return out


def ensure_task_for_note(
    note_id: int,
    *,
    claim: str | None = None,
    ticker: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Find-or-create the research_task for a note (IDEMPOTENT per note_id).

    The On My Mind 'incorporate-into-research' button and the capture-time auto-tap
    (:func:`detect_and_create_task`) both converge here, so one wondering never
    yields two tasks — ``create_task`` has no uniqueness guard, so idempotency is
    enforced in code (single-user localhost; no cross-process race to guard).
    Returns the task id, or None when the note is missing.
    """
    existing = get_task_for_note(note_id, db_path=db_path)
    if existing is not None:
        return existing.id
    note = get_note(note_id, db_path=db_path)
    if note is None:
        return None
    text = (claim or note.body).strip()
    resolved_claim = text[:500] or note.body[:200]
    return create_task(
        note_id=note_id,
        claim=resolved_claim,
        ticker=ticker if ticker is not None else note.ticker,
        db_path=db_path,
    )


def list_tasks(
    *, status: str | None = None, db_path: Path | str | None = None
) -> list[ResearchTask]:
    conn = open_read_conn(db_path)
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM research_tasks WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM research_tasks ORDER BY id DESC").fetchall()
        return [_row_to_task(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def set_task_status(task_id: int, status: str, *, db_path: Path | str | None = None) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r}; expected one of {TASK_STATUSES}")
    conn = open_conn(db_path)
    try:
        conn.execute(
            "UPDATE research_tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


PROPOSAL_STATUSES: tuple[str, ...] = (
    "pending",
    "approved",
    "researching",
    "steered",
    "rejected",
    "superseded",
)
_VERB_STATUS: dict[str, str] = {
    "approve": "approved",
    "further": "researching",
    "steer": "steered",
    "reject": "rejected",
}


@dataclass(frozen=True, slots=True)
class ResearchProposal:
    id: int
    task_id: int | None
    kind: str
    ticker: str | None
    title: str
    body_md: str
    evidence_json: str
    status: str
    adversarial_verdict: str | None
    budget_tier: str | None
    provenance: str
    tainted_by_proposal_id: int | None
    artifact_json: str | None = None
    canonical_content_json: str | None = None
    canonical_content_sha256: str | None = None
    proposal_revision: int = 0
    target_precondition_sha256: str | None = None
    target_postcondition_sha256: str | None = None
    ask_exchange_request_id: str | None = None
    actionable_at: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    # The analyst_notes ids that seeded this proposal (the "↩ from your note"
    # backlink). Parsed from the DB's source_note_ids JSON column; empty when
    # absent or unparseable.
    source_note_ids: list[int] = field(default_factory=list[int])


def create_proposal(
    *,
    task_id: int | None,
    kind: str,
    ticker: str | None,
    title: str,
    body_md: str,
    evidence_json: str = "[]",
    source_note_ids: str = "[]",
    budget_tier: str | None = None,
    adversarial_verdict: str | None = None,
    provenance: str = "derived",
    tainted_by_proposal_id: int | None = None,
    artifact_json: str | None = None,
    canonical_content_json: str | None = None,
    canonical_content_sha256: str | None = None,
    target_precondition_sha256: str | None = None,
    ask_exchange_request_id: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(research_proposals)")}
        has_approval_authority = "canonical_content_json" in columns
        governed_values = (
            canonical_content_json,
            canonical_content_sha256,
            target_precondition_sha256,
            ask_exchange_request_id,
        )
        if not has_approval_authority and any(value is not None for value in governed_values):
            raise RuntimeError("governed proposal approval requires database migration 0006")
        if has_approval_authority:
            cur = conn.execute(
                "INSERT INTO research_proposals "
                "(task_id, kind, ticker, title, body_md, evidence_json, source_note_ids, status, "
                " adversarial_verdict, budget_tier, provenance, tainted_by_proposal_id, "
                " artifact_json, canonical_content_json, canonical_content_sha256, "
                " target_precondition_sha256, ask_exchange_request_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    kind,
                    ticker,
                    title,
                    body_md,
                    evidence_json,
                    source_note_ids,
                    adversarial_verdict,
                    budget_tier,
                    provenance,
                    tainted_by_proposal_id,
                    artifact_json,
                    canonical_content_json,
                    canonical_content_sha256,
                    target_precondition_sha256,
                    ask_exchange_request_id,
                    now,
                    now,
                ),
            )
        else:
            cur = conn.execute(
                "INSERT INTO research_proposals "
                "(task_id, kind, ticker, title, body_md, evidence_json, source_note_ids, status, "
                " adversarial_verdict, budget_tier, provenance, tainted_by_proposal_id, "
                " artifact_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    kind,
                    ticker,
                    title,
                    body_md,
                    evidence_json,
                    source_note_ids,
                    adversarial_verdict,
                    budget_tier,
                    provenance,
                    tainted_by_proposal_id,
                    artifact_json,
                    now,
                    now,
                ),
            )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _parse_note_ids(raw: object) -> list[int]:
    """Guarded decode of the source_note_ids JSON column: a JSON int array →
    its ints; NULL / garbage / non-list shapes → [] (never raises — a bad row
    must not take the Ledger panel down)."""
    if raw is None:
        return []
    try:
        decoded: object = json.loads(str(raw))
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    items = cast("list[object]", decoded)
    return [v for v in items if isinstance(v, int) and not isinstance(v, bool)]


def _row_to_proposal(row: sqlite3.Row) -> ResearchProposal:
    keys = set(row.keys())
    return ResearchProposal(
        id=int(row["id"]),
        task_id=None if row["task_id"] is None else int(row["task_id"]),
        kind=str(row["kind"]),
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        title=str(row["title"]),
        body_md="" if row["body_md"] is None else str(row["body_md"]),
        evidence_json="[]" if row["evidence_json"] is None else str(row["evidence_json"]),
        status=str(row["status"]),
        adversarial_verdict=(
            None if row["adversarial_verdict"] is None else str(row["adversarial_verdict"])
        ),
        budget_tier=None if row["budget_tier"] is None else str(row["budget_tier"]),
        provenance=str(row["provenance"]),
        tainted_by_proposal_id=(
            None if row["tainted_by_proposal_id"] is None else int(row["tainted_by_proposal_id"])
        ),
        artifact_json=None if row["artifact_json"] is None else str(row["artifact_json"]),
        canonical_content_json=(
            None
            if "canonical_content_json" not in keys or row["canonical_content_json"] is None
            else str(row["canonical_content_json"])
        ),
        canonical_content_sha256=(
            None
            if "canonical_content_sha256" not in keys or row["canonical_content_sha256"] is None
            else str(row["canonical_content_sha256"])
        ),
        proposal_revision=(
            int(row["proposal_revision"])
            if "proposal_revision" in keys and row["proposal_revision"] is not None
            else 0
        ),
        target_precondition_sha256=(
            None
            if "target_precondition_sha256" not in keys or row["target_precondition_sha256"] is None
            else str(row["target_precondition_sha256"])
        ),
        target_postcondition_sha256=(
            None
            if "target_postcondition_sha256" not in keys
            or row["target_postcondition_sha256"] is None
            else str(row["target_postcondition_sha256"])
        ),
        ask_exchange_request_id=(
            None
            if "ask_exchange_request_id" not in keys or row["ask_exchange_request_id"] is None
            else str(row["ask_exchange_request_id"])
        ),
        actionable_at=(
            None
            if "actionable_at" not in keys or row["actionable_at"] is None
            else str(row["actionable_at"])
        ),
        invalidated_at=(
            None
            if "invalidated_at" not in keys or row["invalidated_at"] is None
            else str(row["invalidated_at"])
        ),
        invalidation_reason=(
            None
            if "invalidation_reason" not in keys or row["invalidation_reason"] is None
            else str(row["invalidation_reason"])
        ),
        source_note_ids=(
            _parse_note_ids(row["source_note_ids"]) if "source_note_ids" in keys else []
        ),
    )


def get_proposal(proposal_id: int, *, db_path: Path | str | None = None) -> ResearchProposal | None:
    conn = open_read_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM research_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else _row_to_proposal(row)
    finally:
        conn.close()


def list_proposals(
    *,
    status: str | None = "pending",
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[ResearchProposal]:
    db_conn = conn or open_read_conn(db_path)
    try:
        if status is not None:
            rows = db_conn.execute(
                "SELECT * FROM research_proposals WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = db_conn.execute("SELECT * FROM research_proposals ORDER BY id DESC").fetchall()
        return [_row_to_proposal(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is None:
            db_conn.close()


def act_on_proposal(
    proposal_id: int,
    verb: str,
    *,
    steer_text: str | None = None,
    db_path: Path | str | None = None,
) -> str:
    """The ONE action core for the 4 inbox/Telegram verbs. Returns the new status.

    SAFE BY CONSTRUCTION: it only flips the proposal's status (+ records a steer
    note). 'approve' marks the inert drafted memo 'approved' — it does NOT write
    any live artifact, fact, or DCF (that is a separate, explicit later step), so
    a one-tap approve can never trigger a lethal-trifecta write. No web, no fetch.
    """
    if verb not in PROPOSAL_VERBS:
        raise ValueError(f"unknown verb {verb!r}; expected one of {PROPOSAL_VERBS}")
    status = _VERB_STATUS[verb]
    conn = open_conn(db_path)
    try:
        if verb == "steer" and steer_text:
            conn.execute(
                "UPDATE research_proposals "
                "SET status = ?, body_md = COALESCE(body_md, '') || ?, updated_at = ? WHERE id = ?",
                (status, f"\n\n---\n**Owner steer:** {steer_text.strip()}", now_iso(), proposal_id),
            )
        else:
            conn.execute(
                "UPDATE research_proposals SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), proposal_id),
            )
        conn.commit()
    finally:
        conn.close()
    return status


def _audit_tap(
    *,
    note_id: int,
    channel: str,
    detail: str,
    llm_ran: bool,
    db_path: Path | str | None,
) -> None:
    """Best-effort tap-outcome audit row (action='tapped'). Without this trail a
    dormant tap and a broken tap are indistinguishable (2026-07-02 audit: zero
    detect calls ever, no way to tell which). Never raises."""
    try:
        from capture.audit import write_audit

        write_audit(
            channel=channel,
            action="tapped",
            utterance_summary=f"capture tap: {detail}",
            note_id=note_id,
            detail=detail,
            purpose="capture_intent" if llm_ran else None,
            db_path=db_path,
        )
    except Exception:  # observability must never affect capture
        pass


def _route_wondering(
    *,
    note_id: int,
    claim: str,
    ticker: str | None,
    channel: str,
    llm_ran: bool,
    triage_call: TriageCall | None,
    db_path: Path | str | None,
) -> int | None:
    """B7: the routing triage over a positive wondering verdict. Fails OPEN to
    ``research_task`` (today's pre-B7 behavior) on any triage error — see
    :func:`research.triage.classify_triage`. Returns the new task id for the
    ``research_task`` route, else None (``answer_now`` / ``belief_candidate``
    create no task)."""
    triage = classify_triage(claim, ticker=ticker, call=triage_call)

    if triage.route == "answer_now":
        # No task; the INDEPENDENT capture_triage/onmymind.respond answer tap
        # (B3) already runs its own answer-or-not gate on every landed
        # capture — this route just gets out of its way (see this module's
        # docstring + research.triage's module docstring for the interplay).
        # research_route_why (added post-#971) is a separate field, not a
        # reshape of research_route — two tests pin research_route as a bare
        # string (test_research_proposals.py), and the feed read model
        # (onmymind.feed._wondering_route_and_why) only needs the reason for
        # the badge tooltip, not a structural change to what's already pinned.
        with contextlib.suppress(Exception):
            patch_note_context(
                note_id,
                {"research_route": "answer_now", "research_route_why": triage.why},
                db_path=db_path,
            )
        _audit_tap(
            note_id=note_id, channel=channel, detail="answer_now", llm_ran=llm_ran, db_path=db_path
        )
        return None

    if triage.route == "belief_candidate":
        # No task; flag into the Worldview distill ladder (B4's tenet_distill
        # sweep reads context['ladder'] in _FLAGGED_LADDERS).
        with contextlib.suppress(Exception):
            patch_note_context(
                note_id,
                {
                    "ladder": "saved",
                    "research_route": "belief_candidate",
                    "research_route_why": triage.why,
                },
                db_path=db_path,
            )
        _audit_tap(
            note_id=note_id,
            channel=channel,
            detail="belief_candidate",
            llm_ran=llm_ran,
            db_path=db_path,
        )
        return None

    # research_task (explicit route or triage fail-open) — today's pre-B7
    # behavior, now carrying a stated cost + a formatted deep-session prompt.
    estimated_cost_usd = estimate_cost_usd(ticker)
    session_prompt = build_session_prompt(claim, ticker=ticker)
    task_id = create_task(
        note_id=note_id,
        claim=claim,
        ticker=ticker,
        estimated_cost_usd=estimated_cost_usd,
        metadata={"session_prompt": session_prompt},
        db_path=db_path,
    )
    _audit_tap(
        note_id=note_id, channel=channel, detail=f"task:{task_id}", llm_ran=llm_ran, db_path=db_path
    )
    return task_id


def detect_and_create_task(
    note_id: int,
    *,
    db_path: Path | str | None = None,
    call: IntentCall | None = None,
    triage_call: TriageCall | None = None,
    channel: str = "capture",
) -> int | None:
    """The fire-and-forget tap: classify a freshly-landed musing by INTENT and route.
    A ``wondering`` runs the B7 routing triage (``research.triage.classify_triage``):
    ``answer_now`` and ``belief_candidate`` create NO task (see :func:`_route_wondering`);
    only ``research_task`` creates a ``proposed`` task (the inert chip) and returns its
    id. A ``brief_artifact``/``stress_artifact`` records an engage-intent on the note
    (the artifact pipeline picks it up) and returns None; an ``observation`` is inert.
    NEVER raises — a detection failure must not affect capture.

    Every invocation on a real musing leaves one ``tapped`` audit row whose
    ``detail`` names the outcome (task:<id> | answer_now | belief_candidate |
    engage:<mode> | observation | trust_zone | error:<Type>) — the tap's
    liveness signal."""
    note = get_note(note_id, db_path=db_path)
    if note is None or note.kind != "musing":
        return None
    try:
        # Captured musings are owner-authored — provenance 'derived'; the
        # 'contains_fetched' inert marker is only ever on research proposals.
        verdict = classify_intent(note.body, kind=note.kind, provenance="derived", call=call)
    except Exception as exc:
        _audit_tap(
            note_id=note_id,
            channel=channel,
            detail=f"error:{type(exc).__name__}",
            llm_ran=True,
            db_path=db_path,
        )
        return None

    llm_ran = verdict.gate == "llm"

    if verdict.is_wondering:
        claim = verdict.claim.strip() or note.body[:200]
        return _route_wondering(
            note_id=note_id,
            claim=claim,
            ticker=verdict.ticker,
            channel=channel,
            llm_ran=llm_ran,
            triage_call=triage_call,
            db_path=db_path,
        )

    if verdict.is_artifact:
        mode = "stress" if verdict.intent == "stress_artifact" else "brief"
        _record_engage_intent(note_id, verdict, mode, db_path=db_path)
        _audit_tap(
            note_id=note_id,
            channel=channel,
            detail=f"engage:{mode}",
            llm_ran=llm_ran,
            db_path=db_path,
        )
        return None

    # observation — inert. 'trust_zone' distinguishes a pre-gate-filtered non-musing
    # (no LLM) from a model-classified flat observation, for the tap-health line.
    _audit_tap(
        note_id=note_id,
        channel=channel,
        detail="trust_zone" if verdict.gate == "trust_zone" else "observation",
        llm_ran=llm_ran,
        db_path=db_path,
    )
    return None


def _record_engage_intent(
    note_id: int, verdict: object, mode: str, *, db_path: Path | str | None
) -> None:
    """Stamp the artifact-engage intent onto the note's context so the artifact
    pipeline (Phase 2/3) can bind and brief it. Best-effort — never blocks the tap."""
    from research.intent import IntentVerdict

    if not isinstance(verdict, IntentVerdict):
        return
    patch_note_context(
        note_id,
        {
            "engage_intent": {
                "intent": verdict.intent,
                "mode": mode,
                "claim": verdict.claim,
                "ticker": verdict.ticker,
            }
        },
        db_path=db_path,
    )
