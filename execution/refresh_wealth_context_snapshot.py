"""execution/refresh_wealth_context_snapshot.py — daily wealth aggregates pull.

Personal Investment Partner PRD §7.6 (P0-F, owner-ratified 2026-07-23): one
aggregates-only observation of the household balance sheet per day, from the
live tracker (investment total + tax-treatment allocation) and the sibling
wealthplan plan (cash / illiquid / home equity + label-only cash-need band),
appended idempotently to ``wealth_context_snapshot_history`` (0187). Wired
as morning-pipeline stage 0j, also runnable by hand.

Scope: NO item-level holdings, NO transaction detail, NO compensation
figures. Advice-time reads stay live — this table is trend, drift-alert
substrate, and aged fallback only. Telegram redaction excludes all of it.

Failure contract (§7.6.4): when NEITHER source responds, or the composed
snapshot fails plausibility validation, nothing is written (the last valid
row stands), a structured event logs the reason, the ``data_feed_stale``
dead-man fires (feed=wealth_context, one per day), and the exit code is 1.
One source down is a WARNING, not a failure — the snapshot records which
source answered per field.

CLI:
    python execution/refresh_wealth_context_snapshot.py
    python execution/refresh_wealth_context_snapshot.py --db-path /tmp/x.db
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_paths import resolve_db_path  # noqa: E402
from integrations.portfolio_tracker_client import fetch_live_portfolio  # noqa: E402
from integrations.wealth_context import (  # noqa: E402
    DEFAULT_WEALTHPLAN_ROOT,
    build_wealth_context_snapshot,
    load_wealthplan_starting,
)
from wealth_context_store import append_snapshot  # noqa: E402


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _fire_deadman(db_path: Path, *, reason: str) -> None:
    """One book-level 'data_feed_stale' alert per day for a failed wealth
    observation — the §7.6.4 explicit failed-ingestion event. Never raises."""
    try:
        from alerts import store as alerts_store

        now = datetime.now(UTC).replace(tzinfo=None)
        sig = alerts_store.compute_signature_sha(
            "data_feed_stale",
            "PORTFOLIO",
            {"feed": "wealth_context", "date": now.date().isoformat()},
        )
        if alerts_store.find_by_signature(signature_sha=sig, db_path=db_path) is not None:
            return
        alerts_store.fire_alert(
            ticker="PORTFOLIO",
            trigger_kind="data_feed_stale",
            fired_at=now,
            evidence_json=json.dumps({"feed": "wealth_context", "reason": reason}),
            signature_sha=sig,
            db_path=db_path,
        )
        _log("deadman_fired", feed="wealth_context", reason=reason)
    except Exception as exc:
        _log("deadman_failed", error=type(exc).__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="portfolio.db override")
    parser.add_argument("--api-url", type=str, default=None, help="tracker API base URL override")
    parser.add_argument(
        "--wealthplan-root",
        type=Path,
        default=DEFAULT_WEALTHPLAN_ROOT,
        help="sibling wealthplan checkout to borrow models/plan from",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = resolve_db_path(args.db_path)
    if db_path is None:
        _log("halt", reason="DB path not configured")
        return 1

    live = fetch_live_portfolio(api_url=args.api_url)
    tracker_total: float | None = None
    tracker_buckets: dict[str, float] | None = None
    tracker_as_of: str | None = None
    if live.available and live.total_market_value > 0:
        tracker_total = float(live.total_market_value)
        tracker_buckets = dict(live.by_tax_treatment)
        tracker_as_of = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    else:
        _log("source_degraded", source="tracker", error=live.error)

    wealthplan = load_wealthplan_starting(args.wealthplan_root)
    if wealthplan is None:
        _log("source_degraded", source="wealthplan", error="plan unavailable")

    snap = build_wealth_context_snapshot(
        tracker_total=tracker_total,
        tracker_by_tax_treatment=tracker_buckets,
        tracker_as_of=tracker_as_of,
        wealthplan=wealthplan,
        cash_need_root=args.wealthplan_root,
    )
    if snap is None:
        reason = "no source responded (tracker and wealthplan both unavailable)"
        _log("invalid", reason=reason)
        _fire_deadman(db_path, reason=reason)
        return 1
    reasons = snap.validate_plausible()
    if reasons:
        _log("invalid", reason="; ".join(reasons))
        _fire_deadman(db_path, reason="; ".join(reasons))
        return 1

    written, deduped = append_snapshot(snap, db_path=db_path)
    if not written and not deduped:
        reason = "append failed (DB unavailable or pre-0187 schema)"
        _log("invalid", reason=reason)
        _fire_deadman(db_path, reason=reason)
        return 1
    print(
        json.dumps(
            {
                "status": "already_done" if deduped else "ok",
                "as_of": snap.as_of,
                "input_sha": snap.input_sha(),
                "sources": snap.sources,
                "warnings": list(snap.warnings),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
