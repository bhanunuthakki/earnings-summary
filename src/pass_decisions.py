"""Capture errors of omission — pass/avoid as first-class decisions (L11 PR1).

The decisions ledger only ever filled from HELD-name artifacts and Socratic
memos, so a name the owner PASSED on left no graded trace and the calibration
loop (L1/L8) could never see the omission side — a passed name that later
triples is the largest hidden cost there is. This module is the authoring path
that closes that gap: it records "I passed on X because Y / I'd revisit if Z" as
a first-class ``decisions`` row (``recommendation_kind='avoid'``, schema 0110),
wired from the discovery "dismissed" action and a manual entry path.

  record_pass_decision — one idempotent write. The ``reason`` becomes the row's
      rationale_excerpt (the WHY); the optional ``revisit_text`` is stored on
      ``decisions.source_prose`` — NOT extracted here. The existing
      ``attach_conditions`` / ``attach_qualitative_conditions`` rungs (morning
      pipeline stage 0b) pull the numeric AND qualitative falsifiable conditions
      out of it on their next pass, so the avoid reuses L9's news/earnings bridge
      and the numeric decision_condition trigger UNCHANGED — and the request path
      never pays an LLM round-trip.

Idempotency: a dismiss-sourced avoid keys on ``source_dismissal_id`` (the
candidate id, partial UNIQUE in 0110). Re-dismissing the same candidate UPDATES
its reason/revisit text and re-opens extraction (stamps cleared) rather than
inserting a duplicate. A manual pass (no candidate) always inserts — each is a
distinct judgement at a distinct time.

Best-effort against a missing DB / pre-0110 table (the decision_extractor
posture): returns None rather than raising, so a dismiss never fails because the
ledger isn't migrated yet.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from clock import now_iso
from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# The lens labels distinguishing where a pass was authored — they ride the
# decisions.source_lens column and surface in the decision_condition trigger's
# memo ("the 2026-06-14 AVOID decision (discovery_dismissal)").
LENS_DISCOVERY_DISMISSAL = "discovery_dismissal"
LENS_MANUAL_PASS = "manual_pass"


@dataclass(frozen=True, slots=True)
class PassResult:
    """The outcome of one authoring call: the row id and whether it was a fresh
    insert or an idempotent update of an existing dismiss-sourced avoid."""

    decision_id: int
    created: bool


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Best-effort open requiring the 0110 ``decisions`` schema (the
    ``source_prose`` column). None when the DB or that column is absent — a
    pass authored against an un-migrated ledger is silently skipped, never a
    raised error on the dismiss path."""
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "source_prose" not in cols:
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _now_iso() -> str:
    # Naive-UTC, the repo-wide convention (matches decision_conditions).
    return now_iso()


def _clean(text: str | None, *, cap: int) -> str | None:
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    return stripped[:cap] if stripped else None


def record_pass_decision(
    *,
    ticker: str,
    reason: str | None,
    revisit_text: str | None = None,
    source_dismissal_id: int | None = None,
    source_lens: str = LENS_MANUAL_PASS,
    conviction: str | None = None,
    made_at: datetime | None = None,
    db_path: Path | str | None = None,
) -> PassResult | None:
    """Record (or idempotently refresh) a pass/avoid decision.

    ``reason`` is the WHY (→ rationale_excerpt); ``revisit_text`` is the optional
    "what would change my mind" prose (→ source_prose, extracted later by the
    morning-pipeline attach rungs). Returns a :class:`PassResult`, or None when
    the DB/0110 schema is unavailable or the ticker is empty.

    A dismiss-sourced avoid (``source_dismissal_id`` set) is idempotent: a second
    call on the same candidate UPDATEs reason/revisit text and clears the
    extraction stamps so conditions re-extract. A manual pass always inserts.
    """
    symbol = ticker.strip().upper()
    if not symbol:
        return None
    reason_x = _clean(reason, cap=512)
    prose_x = _clean(revisit_text, cap=4000)
    conviction_x = conviction if conviction in {"low", "medium", "high"} else None
    made_iso = made_at.replace(tzinfo=None).isoformat() if made_at is not None else _now_iso()

    conn = _open(db_path)
    if conn is None:
        return None
    try:
        if source_dismissal_id is not None:
            existing = conn.execute(
                "SELECT id FROM decisions WHERE source_dismissal_id = ?",
                (int(source_dismissal_id),),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE decisions
                    SET rationale_excerpt = ?, source_prose = ?, source_lens = ?,
                        conviction = ?,
                        decision_conditions = NULL, conditions_extracted_at = NULL,
                        qualitative_conditions = NULL,
                        qualitative_conditions_extracted_at = NULL
                    WHERE id = ?
                    """,
                    (reason_x, prose_x, source_lens, conviction_x, int(existing["id"])),
                )
                conn.commit()
                return PassResult(decision_id=int(existing["id"]), created=False)

        cur = conn.execute(
            """
            INSERT INTO decisions(
                ticker, recommendation_kind, recommendation_value, conviction,
                source_artifact_id, source_memo_id, source_dismissal_id,
                source_lens, rationale_excerpt, source_prose,
                made_at, outcome_label, created_at
            ) VALUES (?, 'avoid', NULL, ?, NULL, NULL, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                symbol,
                conviction_x,
                int(source_dismissal_id) if source_dismissal_id is not None else None,
                source_lens,
                reason_x,
                prose_x,
                made_iso,
                _now_iso(),
            ),
        )
        conn.commit()
        return PassResult(decision_id=int(cur.lastrowid or 0), created=True)
    except sqlite3.Error as exc:
        log.warning({"event": "record_pass_decision_failed", "ticker": symbol, "error": str(exc)})
        return None
    finally:
        conn.close()


__all__ = [
    "LENS_DISCOVERY_DISMISSAL",
    "LENS_MANUAL_PASS",
    "PassResult",
    "record_pass_decision",
]
