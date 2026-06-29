"""research_tasks / research_proposals store + the fire-and-forget detection tap.

Follows the codebase store convention (writers own a connection + commit; readers
best-effort ``[]`` on a missing table, the ``signals/store.py`` pattern). It owns
the two Phase-1 lifecycles and exposes ``act_on_proposal`` — the ONE action core
both the inbox HTMX route and the Telegram callback dispatch call (no logic
duplication).

The detection tap (``detect_and_create_task``) is fire-and-forget: it classifies
a freshly-landed musing and, on a wondering, writes a ``proposed`` task (the inert
chip). It NEVER auto-runs the research pass, and a detection failure NEVER affects
capture (it swallows errors and returns None).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from research.detect import DetectCall, detect_wondering
from user_state._db import now_iso, open_conn
from user_state.notes import get_note

TASK_STATUSES: tuple[str, ...] = (
    "proposed",
    "running",
    "drafted",
    "approved",
    "rejected",
    "superseded",
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


@dataclass(frozen=True, slots=True)
class ResearchTask:
    id: int
    note_id: int | None
    claim: str
    ticker: str | None
    status: str


def create_task(
    *, note_id: int | None, claim: str, ticker: str | None, db_path: Path | str | None = None
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO research_tasks (note_id, claim, ticker, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'proposed', ?, ?)",
            (note_id, claim, ticker, now, now),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _row_to_task(row: sqlite3.Row) -> ResearchTask:
    return ResearchTask(
        id=int(row["id"]),
        note_id=None if row["note_id"] is None else int(row["note_id"]),
        claim=str(row["claim"]),
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        status=str(row["status"]),
    )


def get_task(task_id: int, *, db_path: Path | str | None = None) -> ResearchTask | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _row_to_task(row)
    finally:
        conn.close()


def list_tasks(
    *, status: str | None = None, db_path: Path | str | None = None
) -> list[ResearchTask]:
    conn = open_conn(db_path)
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
    db_path: Path | str | None = None,
) -> int:
    conn = open_conn(db_path)
    try:
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO research_proposals "
            "(task_id, kind, ticker, title, body_md, evidence_json, source_note_ids, status, "
            " adversarial_verdict, budget_tier, provenance, tainted_by_proposal_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
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
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _row_to_proposal(row: sqlite3.Row) -> ResearchProposal:
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
    )


def get_proposal(proposal_id: int, *, db_path: Path | str | None = None) -> ResearchProposal | None:
    conn = open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM research_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return None if row is None else _row_to_proposal(row)
    finally:
        conn.close()


def list_proposals(
    *, status: str | None = "pending", db_path: Path | str | None = None
) -> list[ResearchProposal]:
    conn = open_conn(db_path)
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM research_proposals WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM research_proposals ORDER BY id DESC").fetchall()
        return [_row_to_proposal(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


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


def detect_and_create_task(
    note_id: int, *, db_path: Path | str | None = None, call: DetectCall | None = None
) -> int | None:
    """The fire-and-forget tap: classify a freshly-landed musing; on a wondering,
    create a ``proposed`` task (the inert chip). Returns the task id or None.
    NEVER raises — a detection failure must not affect capture."""
    note = get_note(note_id, db_path=db_path)
    if note is None or note.kind != "musing":
        return None
    try:
        # Captured musings are owner-authored — provenance 'derived'; the
        # 'contains_fetched' inert marker is only ever on research proposals.
        verdict = detect_wondering(note.body, kind=note.kind, provenance="derived", call=call)
    except Exception:
        return None
    if not verdict.is_wondering:
        return None
    claim = verdict.claim.strip() or note.body[:200]
    return create_task(note_id=note_id, claim=claim, ticker=verdict.ticker, db_path=db_path)
