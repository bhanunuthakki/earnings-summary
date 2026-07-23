"""execution/refresh_portfolio_risk_snapshot.py — the AUTHORITATIVE risk writer.

Personal Investment Partner PRD §7.1 (P0-A): the Risk Budget's daily truth
stops depending on someone happening to open the Portfolio panel. This
script fetches the tracker analytics, derives drawdown + the full factor
roll-up (including the rate leg the hot render path skips), validates the
capture against ``RiskBudgetSnapshot`` (PRD §7.1.5), and persists via
``portfolio_risk_snapshot_store.write_snapshot`` — latest-view upsert plus
content-hash-deduped history append. Wired as morning-pipeline stage 0i,
also runnable by hand.

Invalid-refresh contract (§7.1.6): a failed/degenerate/implausible capture
writes NOTHING (the last valid snapshot and history stay untouched), logs a
structured ``invalid`` event, fires the ``data_feed_stale`` dead-man alert
(feed=risk_snapshot, one per day, signature-deduped), and exits 1.

CLI:
    python execution/refresh_portfolio_risk_snapshot.py
    python execution/refresh_portfolio_risk_snapshot.py --db-path /tmp/x.db

Exit 0 on a validated write (or a content-identical re-run, which dedupes);
1 on tracker-unavailable / degenerate payload / validation failure.
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
from integrations.portfolio_tracker_client import fetch_portfolio_analytics  # noqa: E402

# The PRD (§7.1.2) mandates reusing the panel's build primitives rather than
# duplicating the analytics->snapshot mapping here.
from pipeline.portfolio_panel import (  # noqa: E402
    _build_risk_snapshot,  # pyright: ignore[reportPrivateUsage]
    _rate_betas,  # pyright: ignore[reportPrivateUsage]
)
from portfolio_risk import compute_drawdown, factor_exposure_rollup  # noqa: E402
from portfolio_risk_snapshot_store import (  # noqa: E402
    RiskBudgetSnapshot,
    history_has_sha,
    snapshot_input_sha,
    write_snapshot,
)


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _fire_deadman(db_path: Path, *, reason: str) -> None:
    """One book-level 'data_feed_stale' alert per day for a failed risk
    refresh — the §7.1.6 explicit failed-ingestion event, surfaced where the
    owner actually looks instead of a cron log. Never raises into the run."""
    try:
        from alerts import store as alerts_store

        now = datetime.now(UTC).replace(tzinfo=None)
        sig = alerts_store.compute_signature_sha(
            "data_feed_stale",
            "PORTFOLIO",
            {"feed": "risk_snapshot", "date": now.date().isoformat()},
        )
        if alerts_store.find_by_signature(signature_sha=sig, db_path=db_path) is not None:
            return
        alerts_store.fire_alert(
            ticker="PORTFOLIO",
            trigger_kind="data_feed_stale",
            fired_at=now,
            evidence_json=json.dumps({"feed": "risk_snapshot", "reason": reason}),
            signature_sha=sig,
            db_path=db_path,
        )
        _log("deadman_fired", feed="risk_snapshot", reason=reason)
    except Exception as exc:
        _log("deadman_failed", error=type(exc).__name__)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="portfolio.db override")
    parser.add_argument("--api-url", type=str, default=None, help="tracker API base URL override")
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="per-endpoint tracker read timeout in seconds. This is a batch "
        "writer, not a render path: the beta/position-alpha endpoints take "
        "~22s warm on prod (measured 2026-07-23), which is exactly why the "
        "render path's 6s budget left the snapshot NULL for weeks.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = resolve_db_path(args.db_path)
    if db_path is None:
        _log("halt", reason="DB path not configured")
        return 1

    analytics = fetch_portfolio_analytics(api_url=args.api_url, timeout=args.timeout)
    fetch_errors = tuple(f"{k}: {v}" for k, v in analytics.errors.items())
    if not analytics.available:
        reason = "tracker unavailable"
        _log("invalid", reason=reason, errors=list(fetch_errors))
        _fire_deadman(db_path, reason=reason)
        return 1
    # The §7.1.6 degenerate-payload gate (same rule as the render path's
    # NULL-clobber fix, 2026-07-19 review G5): a capture must carry the
    # sections its substance comes from.
    if analytics.performance is None or analytics.positioning is None:
        reason = "partial tracker payload (performance/positioning missing)"
        _log("invalid", reason=reason, errors=list(fetch_errors))
        _fire_deadman(db_path, reason=reason)
        return 1

    drawdown = compute_drawdown(analytics.performance.points)
    rate_betas = _rate_betas(analytics.positioning.correlations, db_path)
    factor = factor_exposure_rollup(analytics.positioning.correlations, rate_betas)
    snap = _build_risk_snapshot(analytics, drawdown, factor)

    weights = tuple(
        float(r.weight_pct) for r in analytics.positioning.correlations if r.weight_pct is not None
    )
    concentration = analytics.positioning.concentration
    num_positions = (
        concentration.num_positions
        if concentration is not None and concentration.num_positions
        else len(analytics.positioning.correlations)
    )
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    as_of = snap.window_end or now_iso[:10]
    try:
        budget = RiskBudgetSnapshot(
            as_of=as_of,
            num_positions=num_positions,
            weights_pct=weights,
            source_freshness={
                "tracker_analytics": snap.window_end or now_iso,
                "fetched_at": now_iso,
            },
            source_errors=fetch_errors,
        )
        reasons = budget.validate_against(snap)
    except Exception as exc:  # pydantic rejects (e.g. zero positions)
        reasons = [f"schema: {exc.__class__.__name__}"]
    if reasons:
        _log("invalid", reason="; ".join(reasons))
        _fire_deadman(db_path, reason="; ".join(reasons))
        return 1

    sha = snapshot_input_sha(snap)
    deduped = history_has_sha(sha, db_path=db_path)
    if not write_snapshot(snap, db_path=db_path):
        reason = "snapshot write failed"
        _log("invalid", reason=reason)
        _fire_deadman(db_path, reason=reason)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "as_of": as_of,
                "num_positions": num_positions,
                "input_sha": sha,
                "history_deduped": deduped,
                "source_errors": list(fetch_errors),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
