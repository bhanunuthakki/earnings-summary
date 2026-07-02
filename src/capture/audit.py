"""Capture audit-log writer (alembic 0116) — the durable summary-level trail.

Raw transcripts are transient; this log is what survives. The cardinal rule is
that it must NOT become a second uncontrolled copy of the user's raw thought:
every row stores a SHORT paraphrase (``utterance_summary``, never the raw words)
and a content HASH of any prompt (``prompt_sha256``, never the prompt body). It
records one row per meaningful pipeline action (captured / structured / landed /
researched / synthesized / failed), with the LLM purpose, model, and cost, so
the loop is auditable and its spend is measurable for evals.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from user_state._db import now_iso, open_conn

# The actions a capture / research / synthesis step records. Code-validated
# (no DB CHECK) — same reversible-vocab posture as NOTE_KINDS.
AUDIT_ACTIONS: tuple[str, ...] = (
    "captured",
    "structured",
    "landed",
    "matched",
    "researched",
    "synthesized",
    "failed",
    "purged",
    "tapped",
)

# Fallback-summary length; the hard cap below is a separate, looser safety net
# so a caller's deliberate (already-short) summary is never clipped, but a raw
# transcript accidentally passed as the summary cannot be dumped verbatim.
_SUMMARY_LEN = 160
_SUMMARY_HARD_CAP = 600


def summarize_utterance(text: str, *, max_len: int = _SUMMARY_LEN) -> str:
    """A deterministic SHORT paraphrase: collapse whitespace to one line, clip.

    Callers SHOULD pass an LLM-written summary; this guarantees the audit log
    never stores the full raw utterance even when no summarizer ran (the privacy
    floor: the log paraphrases the signal, not the thought)."""
    one = " ".join((text or "").split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1].rstrip() + "…"


def prompt_hash(prompt: str | None) -> str | None:
    """Hex sha256 of a prompt body. The audit log stores this HASH, never the
    body — so the trail can prove *which* prompt ran without copying sensitive
    thought into a second store."""
    if prompt is None:
        return None
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()


def write_audit(
    *,
    channel: str,
    action: str,
    utterance_summary: str,
    session_id: int | None = None,
    note_id: int | None = None,
    detail: str | None = None,
    purpose: str | None = None,
    model: str | None = None,
    prompt_sha256: str | None = None,
    run_id: str | None = None,
    cost_usd: float = 0.0,
    db_path: Path | str | None = None,
) -> int:
    """Append one audit row; return its id.

    ``utterance_summary`` is defensively clipped to a hard cap so a raw
    transcript handed in by mistake can never be stored verbatim — pass a real
    summary. ``action`` must be one of ``AUDIT_ACTIONS``."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r}; expected one of {AUDIT_ACTIONS}")
    summary = summarize_utterance(utterance_summary, max_len=_SUMMARY_HARD_CAP)
    conn = open_conn(db_path)
    try:
        stamp = now_iso()
        cur = conn.execute(
            """
            INSERT INTO capture_audit_log
                (session_id, note_id, channel, utterance_summary, action, detail,
                 purpose, model, prompt_sha256, run_id, cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                note_id,
                channel,
                summary,
                action,
                detail,
                purpose,
                model,
                prompt_sha256,
                run_id,
                float(cost_usd),
                stamp,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def audit_cost_in_window(
    *,
    since: str | None = None,
    until: str | None = None,
    db_path: Path | str | None = None,
) -> float:
    """Sum ``cost_usd`` over an optional [since, until] naive-UTC window — the
    per-window spend the budget tier and the evals panel read."""
    conn = open_conn(db_path)
    try:
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM capture_audit_log{where}",
            params,
        ).fetchone()
        return float(row[0] or 0.0)
    finally:
        conn.close()


def recent_tap_counts(*, days: int = 7, db_path: Path | str | None = None) -> dict[str, int]:
    """Outcome counts for the intent tap over the last ``days``.

    Keys are the ``detail`` outcome families written by
    ``research.proposals.detect_and_create_task``: ``chip`` (a wondering became a
    research task), ``engage`` (an artifact brief/stress intent, routed to the
    artifact pipeline), ``trust_zone`` (pre-gate filtered, zero tokens),
    ``observation`` (the classifier said flat observation), ``error``. This is the
    tap's liveness signal — without it a dormant tap and a broken tap look identical.
    """
    from datetime import timedelta

    from user_state._db import now_naive_utc

    cutoff = (now_naive_utc() - timedelta(days=days)).isoformat()
    conn = open_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT detail, COUNT(*) FROM capture_audit_log
            WHERE action = 'tapped' AND created_at >= ?
            GROUP BY detail
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    counts = {"chip": 0, "engage": 0, "trust_zone": 0, "observation": 0, "error": 0}
    for detail, n in rows:
        key = str(detail or "")
        if key.startswith("task:"):
            counts["chip"] += int(n)
        elif key.startswith("engage:"):
            counts["engage"] += int(n)
        elif key.startswith("error:"):
            counts["error"] += int(n)
        elif key in counts:
            counts[key] += int(n)
    return counts
