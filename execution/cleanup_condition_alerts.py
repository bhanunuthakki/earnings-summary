"""One-time cleanup: dismiss pending decision_condition alerts that predate
the 2026-07-15 alert-quality gates.

Provenance check against prod (2026-07-15) showed 10 of the 11 pending
"falsifier breach" alerts were ADVISOR-authored decisions on EVALUATION-list
names — tripwires the owner never wrote, on names holding no money — and
several fired on observations months older than the decision itself (the
condition was already breached when it was attached; the "breach" was never
news). The trigger now gates both classes at scan time
(``triggers.decision_condition``); this script retires the rows persisted
before that gate existed.

A pending decision_condition alert is dismissed when it fails either gate:

  - **relevance**: the sourcing decision is not owner-authored AND the ticker
    is not an active portfolio holding, or
  - **freshness**: the breaching observation is stale — its period_end is not
    strictly after the condition's attach-time baseline (when the evidence
    carries one), or, for legacy evidence with no baseline, it is older than
    ``--stale-days`` (default 135) at run time.

Dry-run by default; ``--apply`` performs the update. Idempotent: only
``status='pending'`` rows are touched, so a re-run finds nothing. One summary
line per alert + a count on stdout; structured JSON events on stderr.

Usage:
    python execution/cleanup_condition_alerts.py --db-path data/portfolio.db
    python execution/cleanup_condition_alerts.py --db-path data/portfolio.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

DISMISS_REASON = "auto: pre-gate advisor/evaluation tripwire noise (2026-07-15)"
DEFAULT_STALE_DAYS = 135


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


def _decided_by(conn: sqlite3.Connection, evidence: dict[str, object], has_col: bool) -> str | None:
    """The sourcing decision's author: evidence key first (post-gate rows),
    then the decisions table (pre-gate rows lack the evidence key)."""
    from_evidence = evidence.get("decided_by")
    if isinstance(from_evidence, str) and from_evidence:
        return from_evidence
    decision_id = evidence.get("decision_id")
    if not isinstance(decision_id, int) or not has_col:
        return None
    try:
        row = conn.execute(
            "SELECT decided_by FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _is_portfolio(conn: sqlite3.Connection, ticker: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM tracked_companies "
            "WHERE ticker = ? AND list_type = 'portfolio' AND archived_at IS NULL",
            (ticker.upper(),),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _is_stale(evidence: dict[str, object], *, now: datetime, stale_days: int) -> bool:
    """Mirror of the trigger's freshness gate, replayed from stored evidence."""
    period_end = evidence.get("period_end")
    if not isinstance(period_end, str) or not period_end:
        return False  # can't judge — leave it to the relevance gate
    baseline = evidence.get("baseline_period_end")
    if isinstance(baseline, str) and baseline:
        return period_end[:10] <= baseline[:10]
    try:
        observed = datetime.fromisoformat(period_end[:10])
    except ValueError:
        return False
    return observed < now - timedelta(days=stale_days)


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry run (the default; kept for scriptability).",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"Freshness fallback for evidence with no baseline (default {DEFAULT_STALE_DAYS}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.apply and args.dry_run:
        _emit({"event": "conflicting_flags", "error": "--apply and --dry-run are exclusive"})
        return 2
    apply_changes = bool(args.apply)

    db_path = cast("Path", args.db_path)
    if not db_path.exists():
        _emit({"event": "db_missing", "db_path": str(db_path)})
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        alert_cols = _columns(conn, "alerts")
        if not {"status", "dismissed_at", "dismiss_reason"} <= alert_cols:
            _emit(
                {
                    "event": "schema_missing",
                    "error": "alerts table lacks status/dismissed_at/dismiss_reason "
                    "(need alembic 0142)",
                }
            )
            return 1
        has_decided_by = "decided_by" in _columns(conn, "decisions")

        rows = conn.execute(
            "SELECT id, ticker, fired_at, evidence_json FROM alerts "
            "WHERE status = 'pending' AND trigger_kind = 'decision_condition' "
            "ORDER BY id"
        ).fetchall()

        now = datetime.now(UTC).replace(tzinfo=None)
        dismissed = 0
        kept = 0
        for row in rows:
            alert_id = int(row["id"])
            ticker = str(row["ticker"] or "").upper()
            try:
                decoded: object = json.loads(row["evidence_json"] or "{}")
            except (ValueError, TypeError):
                decoded = {}
            evidence = cast("dict[str, object]", decoded) if isinstance(decoded, dict) else {}

            decided_by = _decided_by(conn, evidence, has_decided_by)
            portfolio = _is_portfolio(conn, ticker)
            reasons: list[str] = []
            if decided_by != "owner" and not portfolio:
                reasons.append("advisor condition on non-portfolio name")
            if _is_stale(evidence, now=now, stale_days=int(args.stale_days)):
                reasons.append("stale breach observation")

            if not reasons:
                kept += 1
                sys.stdout.write(
                    f"KEEP    alert {alert_id} {ticker} "
                    f"(decided_by={decided_by or '?'}, portfolio={portfolio})\n"
                )
                continue

            dismissed += 1
            verb = "DISMISS" if apply_changes else "WOULD-DISMISS"
            sys.stdout.write(
                f"{verb} alert {alert_id} {ticker} "
                f"(decided_by={decided_by or '?'}, portfolio={portfolio}; "
                f"{'; '.join(reasons)})\n"
            )
            _emit(
                {
                    "event": "condition_alert_dismissed" if apply_changes else "dry_run_dismiss",
                    "alert_id": alert_id,
                    "ticker": ticker,
                    "decided_by": decided_by,
                    "portfolio": portfolio,
                    "reasons": reasons,
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
        sys.stdout.write(
            f"{mode}: {dismissed} dismissed, {kept} kept "
            f"(of {len(rows)} pending decision_condition alerts)\n"
        )
        _emit(
            {
                "event": "cleanup_summary",
                "mode": mode,
                "dismissed": dismissed,
                "kept": kept,
                "scanned": len(rows),
            }
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
