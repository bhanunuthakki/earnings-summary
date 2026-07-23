"""``act_on_recommendation`` — the ONE action core for the Incremental Dollar
Recommendation's owner verbs (PRD §11.5 decisions changes).

Mirrors ``research.proposals.act_on_proposal``'s "ONE action core" pattern: a
pure function with no Flask/Telegram import, so the same three verbs are
reachable from both the HTTP route (``execution/comments_server.py``) and any
future Telegram dispatcher without duplicating the write logic.

``save_intent`` and ``hold_accountable`` both write a ``decisions`` row linked
via the NEW ``advice_artifact_id`` column (migration 0188) — distinct from the
older ``source_artifact_id`` lens-provenance chain (0046). The decision is
recorded ``decided_by='owner', scope='portfolio'`` (a cash-allocation call is
a portfolio-level move, not anchored to one ticker — PRD §7.4's plans can span
multiple names or be cash-only), which satisfies ``ck_decisions_source_present``
without needing ``source_artifact_id``/``source_memo_id`` (0130's "owner rows
need no lens provenance" precedent).

``recommendation_kind`` is derived from the artifact's ``status`` — NEVER
research dispositions ('pass'/'watch'/'promote', PRD §7.4's OTHER vocabulary
for a different flow) — deploy/deploy_partial -> 'allocate', retain_all/defer
-> 'hold'. ``decision_extractor._KIND_DIRECTION`` (the only place kind drives
behavior) has no entry for 'allocate'/'hold', so
``reconcile_decision_actions`` degrades them to its HOLD/AVOID branch rather
than ever mis-mapping to a buy/sell fill match (verified at migration 0188).

Idempotent: a second ``save_intent``/``hold_accountable`` on the same
artifact_id is a no-op (returns the existing decision's status) rather than a
duplicate row — checked by ``advice_artifact_id`` (NOT the old
``source_artifact_id`` UNIQUE index, which this write path never touches).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from db_paths import resolve_db_path

__all__ = ["ACTION_VERBS", "act_on_recommendation"]

PURPOSE = "incremental_dollar_recommendation"

ActionVerb = Literal["save_intent", "hold_accountable", "dismiss"]
ACTION_VERBS: tuple[str, ...] = ("save_intent", "hold_accountable", "dismiss")

# artifact status -> decisions.recommendation_kind. Deliberately excludes the
# research-disposition vocabulary ('pass'/'watch'/'promote') — those belong to
# a different flow (research proposals) and must never be written here.
_STATUS_TO_KIND: dict[str, str] = {
    "deploy_all": "allocate",
    "deploy_partial": "allocate",
    "retain_all": "hold",
    "defer": "hold",
}


class RecommendationActionError(RuntimeError):
    """Raised when the action cannot be performed (unknown verb, missing
    artifact, or artifact not of this purpose)."""


def _open(db_path: Path | str | None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    if path is None or not Path(path).exists():
        raise RecommendationActionError("portfolio DB not available")
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def _load_artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, purpose, content_json, generated_at FROM llm_artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise RecommendationActionError(f"no llm_artifacts row for id={artifact_id}")
    if row["purpose"] != PURPOSE:
        raise RecommendationActionError(
            f"artifact {artifact_id} has purpose={row['purpose']!r}, expected {PURPOSE!r}"
        )
    return row


def _existing_decision_id(conn: sqlite3.Connection, artifact_id: int, verb: str) -> int | None:
    """A prior decision written by THIS verb for THIS artifact — the
    idempotency check. ``save_intent`` and ``hold_accountable`` both write a
    decisions row, but distinguished by ``user_action_kind`` ('' for a plain
    save, 'hold_accountable' for the graded marker) so re-tapping either verb
    is a no-op against its own prior write, not the other verb's."""
    action_kind = "hold_accountable" if verb == "hold_accountable" else None
    row = conn.execute(
        "SELECT id FROM decisions WHERE advice_artifact_id = ? "
        "AND COALESCE(user_action_kind, '') = COALESCE(?, '')",
        (artifact_id, action_kind),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def act_on_recommendation(
    artifact_id: int,
    verb: ActionVerb | str,
    *,
    db_path: Path | str | None = None,
    notes: str | None = None,
) -> str:
    """The ONE action core for the three owner verbs on a recommendation
    artifact. Returns a short status string:

      'save_intent'      -> 'saved' (new decision) | 'already_saved'
      'hold_accountable' -> 'marked_accountable' (new decision) | 'already_marked'
      'dismiss'           -> 'dismissed' (no decision row written — the
                              artifact itself IS the advice history)

    Raises :class:`RecommendationActionError` for an unknown verb or a
    missing/mismatched-purpose artifact — never a silent no-op on a bad
    input.
    """
    if verb not in ACTION_VERBS:
        raise RecommendationActionError(f"unknown verb {verb!r}; expected one of {ACTION_VERBS}")

    conn = _open(db_path)
    try:
        artifact = _load_artifact(conn, artifact_id)

        if verb == "dismiss":
            return "dismissed"

        existing_id = _existing_decision_id(conn, artifact_id, verb)
        if existing_id is not None:
            return "already_saved" if verb == "save_intent" else "already_marked"

        try:
            decoded = json.loads(artifact["content_json"] or "{}")
        except (TypeError, ValueError):
            decoded = {}
        content: dict[str, object] = (
            cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}
        )
        status = str(content.get("status") or "")
        kind = _STATUS_TO_KIND.get(status, "hold")
        rationale = str(content.get("central_hypothesis") or "")[:512]

        now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        action_kind = "hold_accountable" if verb == "hold_accountable" else None
        cur = conn.execute(
            """
            INSERT INTO decisions (
                ticker, recommendation_kind, decided_by, scope,
                advice_artifact_id, rationale_excerpt, user_notes,
                user_action_kind, made_at, created_at
            ) VALUES (NULL, ?, 'owner', 'portfolio', ?, ?, ?, ?, ?, ?)
            """,
            (kind, artifact_id, rationale, notes, action_kind, now, now),
        )
        conn.commit()
        _ = cur.lastrowid
        return "marked_accountable" if verb == "hold_accountable" else "saved"
    finally:
        conn.close()
