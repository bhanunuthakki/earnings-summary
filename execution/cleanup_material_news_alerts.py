"""One-time cleanup: dismiss pending material_news alerts that predate the
2026-07-30 v3 signal-quality gate.

The v2 classifier scored TOPICAL relevance, so opinion pieces and
post-earnings recap headlines about thesis-relevant topics fired as alerts
(owner walkthrough 2026-07-30: the LMND CFO think-piece, the MSFT
"stock rises on strong earnings" recap — "low quality/noise signals"). The
trigger now vetoes commentary at scan time (``triggers.material_news`` v3:
event-type taxonomy + relevance floor 0.7); this script retires the pending
rows persisted before that gate existed. They cannot be re-scored without
re-running the LLM over stale news, and pending news alerts older than the
24h recency window are settled noise by definition.

Dismissing an alert also CANCELS its still-pending child ``queued_actions``
in the same transaction (the v2 trigger queued two templated actions per
story — retired with it), mirroring ``cleanup_condition_alerts.py``.

Dry-run by default; ``--apply`` performs the update. Idempotent: only
``status='pending'`` rows are touched, so a re-run finds nothing. One summary
line per alert + a count on stdout; structured JSON events on stderr.

Usage:
    python execution/cleanup_material_news_alerts.py --db-path data/portfolio.db
    python execution/cleanup_material_news_alerts.py --db-path data/portfolio.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DISMISS_REASON = "auto: pre-v3 topical-relevance news noise (2026-07-30)"


def _emit(event: dict[str, object]) -> None:
    """One structured-log line to stderr (stdout is reserved for the summary)."""
    sys.stderr.write(json.dumps(event, default=str) + "\n")


def _now_iso() -> str:
    # Naive-UTC, the repo-wide convention (matches alerts.store stamps).
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _has_queued_actions(conn: sqlite3.Connection) -> bool:
    return {"alert_id", "status", "cancelled_at"} <= _columns(conn, "queued_actions")


def _cancel_child_actions(conn: sqlite3.Connection, alert_id: int, *, apply_changes: bool) -> int:
    """Cancel the still-pending queued_actions under one alert (same
    transaction as its dismissal — the caller commits). Dry-run counts only."""
    if not _has_queued_actions(conn):
        return 0
    if not apply_changes:
        row = conn.execute(
            "SELECT COUNT(*) FROM queued_actions WHERE alert_id = ? AND status = 'pending'",
            (alert_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    cur = conn.execute(
        "UPDATE queued_actions SET status = 'cancelled', cancelled_at = ? "
        "WHERE alert_id = ? AND status = 'pending'",
        (_now_iso(), alert_id),
    )
    return int(cur.rowcount)


def _headline(evidence_json: object) -> str:
    """Best-effort headline for the per-alert summary line."""
    if not isinstance(evidence_json, str) or not evidence_json:
        return ""
    try:
        decoded: object = json.loads(evidence_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(decoded, dict):
        return ""
    headline = cast("dict[str, object]", decoded).get("headline")
    return headline if isinstance(headline, str) else ""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-path", type=Path, required=True, help="Path to the portfolio SQLite DB."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the dismissals. Default is a dry run that only reports.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    apply_changes = bool(args.apply)

    db_path = cast("Path", args.db_path)
    if not db_path.exists():
        _emit({"event": "db_missing", "db_path": str(db_path)})
        return 1

    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.WRITER, schema_preflight=True)
    conn.row_factory = sqlite3.Row
    try:
        alert_cols = _columns(conn, "alerts")
        if not {"status", "dismissed_at", "dismiss_reason"} <= alert_cols:
            _emit(
                {
                    "event": "schema_missing",
                    "error": "alerts table lacks status/dismissed_at/dismiss_reason",
                }
            )
            return 1

        rows = conn.execute(
            "SELECT id, ticker, fired_at, evidence_json FROM alerts "
            "WHERE status = 'pending' AND trigger_kind = 'material_news' "
            "ORDER BY id"
        ).fetchall()

        dismissed = 0
        for row in rows:
            alert_id = int(row["id"])
            ticker = str(row["ticker"] or "").upper()
            headline = _headline(row["evidence_json"])
            dismissed += 1
            # A dismissed alert must not leave live approve buttons behind:
            # cancel its still-pending child queued_actions in the SAME
            # transaction as the dismissal.
            child_count = _cancel_child_actions(conn, alert_id, apply_changes=apply_changes)
            verb = "DISMISS" if apply_changes else "WOULD-DISMISS"
            child_note = f"; cancelling {child_count} pending action(s)" if child_count else ""
            headline_note = f" — {headline!r}" if headline else ""
            sys.stdout.write(f"{verb} alert {alert_id} {ticker}{headline_note}{child_note}\n")
            _emit(
                {
                    "event": "material_news_alert_dismissed"
                    if apply_changes
                    else "dry_run_dismiss",
                    "alert_id": alert_id,
                    "ticker": ticker,
                    "fired_at": row["fired_at"],
                    "child_actions_cancelled": child_count,
                }
            )
            if apply_changes:
                conn.execute(
                    "UPDATE alerts SET status = 'dismissed', dismissed_at = ?, "
                    "dismiss_reason = ? WHERE id = ? AND status = 'pending'",
                    (_now_iso(), DISMISS_REASON, alert_id),
                )
        if apply_changes:
            conn.commit()

        mode = "apply" if apply_changes else "dry-run"
        sys.stdout.write(f"{mode}: {dismissed} of {len(rows)} pending material_news alerts\n")
        _emit(
            {
                "event": "cleanup_summary",
                "mode": mode,
                "dismissed": dismissed,
                "scanned": len(rows),
            }
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
