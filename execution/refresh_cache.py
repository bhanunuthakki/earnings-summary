"""Idempotent FMP cache refresh — runs at app startup or on manual invocation.

Reads `fmp_endpoint_status.last_pulled` per ticker (max across endpoints) and
re-pulls anything older than its tier's cadence:

    portfolio / watchlist / none  -> 1 day
    index_member / etf            -> 30 days

Fast-exit: if no ticker is stale, the script returns 0 in <1 second without
touching the network.

Usage:
    python execution/refresh_cache.py                        # default: respect cadence
    python execution/refresh_cache.py --dry-run              # report stale tickers, no fetch
    python execution/refresh_cache.py --force                # ignore cadence, refresh everything
    python execution/refresh_cache.py --tickers AAPL,MSFT    # just these (still cadence-gated)
    python execution/refresh_cache.py --only portfolio       # restrict tier
    python execution/refresh_cache.py --background           # detach and exit immediately

Designed to be called from src/main.py at startup as a non-blocking subprocess
(see _spawn_background_refresh below). Manual invocation is the same script.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"
LOG_DIR = PROJECT_ROOT / ".tmp" / "cacher"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Hours before a ticker is considered stale and triggers a re-pull.
_REFRESH_CADENCE_HOURS: dict[str, int] = {
    "portfolio": 24,
    "watchlist": 24,
    "none": 24,
    "index_member": 24 * 30,
    "etf": 24 * 30,
}

_VALID_TIER_FILTERS: frozenset[str] = frozenset(_REFRESH_CADENCE_HOURS.keys())


def _stale_tickers(
    conn: sqlite3.Connection,
    *,
    only: frozenset[str] | None,
    explicit_tickers: list[str] | None,
    force: bool,
    now: datetime,
) -> list[tuple[str, str, datetime | None]]:
    """Return [(ticker, list_type, last_pulled_or_None), ...] for stale rows."""
    cur = conn.cursor()
    sql = (
        "SELECT tc.ticker, tc.list_type, MAX(fes.last_pulled) AS last_pulled "
        "FROM tracked_companies tc "
        "LEFT JOIN fmp_endpoint_status fes "
        "  ON fes.ticker = tc.ticker AND fes.status = 'ok' "
        "WHERE 1=1 "
    )
    params: list[object] = []
    if only:
        placeholders = ",".join("?" for _ in only)
        sql += f"AND tc.list_type IN ({placeholders}) "
        params.extend(only)
    if explicit_tickers:
        placeholders = ",".join("?" for _ in explicit_tickers)
        sql += f"AND tc.ticker IN ({placeholders}) "
        params.extend(t.upper() for t in explicit_tickers)
    sql += "GROUP BY tc.ticker, tc.list_type"
    cur.execute(sql, params)

    out: list[tuple[str, str, datetime | None]] = []
    for row in cur.fetchall():
        ticker, list_type, last_pulled_str = row
        last_pulled: datetime | None = None
        if last_pulled_str is not None:
            try:
                last_pulled = datetime.fromisoformat(last_pulled_str)
            except ValueError:
                last_pulled = None
        if force or last_pulled is None:
            out.append((ticker, list_type, last_pulled))
            continue
        cadence_h = _REFRESH_CADENCE_HOURS.get(list_type, 24 * 30)
        if (now - last_pulled) >= timedelta(hours=cadence_h):
            out.append((ticker, list_type, last_pulled))
    return out


def _build_save_fmp_command(tickers: list[str], force_snapshot: bool) -> list[str]:
    """Construct the save_fmp_data.py invocation for these stale tickers."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "save_fmp_data.py"),
        "--tickers",
        ",".join(tickers),
        "--skip-existing",
    ]
    if force_snapshot:
        cmd.append("--force-snapshot")
    return cmd


def run_refresh(
    *,
    db_path: str,
    only: frozenset[str] | None,
    explicit_tickers: list[str] | None,
    force: bool,
    dry_run: bool,
) -> dict[str, object]:
    """Identify stale tickers and shell out to save_fmp_data.py if any."""
    now = datetime.now()
    conn = sqlite3.connect(db_path)
    try:
        stale = _stale_tickers(
            conn,
            only=only,
            explicit_tickers=explicit_tickers,
            force=force,
            now=now,
        )
    finally:
        conn.close()

    by_tier: dict[str, int] = {}
    for _, lt, _ in stale:
        by_tier[lt] = by_tier.get(lt, 0) + 1

    summary: dict[str, object] = {
        "now": now.isoformat(timespec="seconds"),
        "force": force,
        "dry_run": dry_run,
        "stale_count": len(stale),
        "stale_by_tier": by_tier,
    }
    if not stale or dry_run:
        summary["fetcher_exit_code"] = None
        return summary

    tickers = [t for t, _, _ in stale]
    cmd = _build_save_fmp_command(tickers, force_snapshot=force)
    log_path = LOG_DIR / f"refresh_{now.strftime('%Y%m%dT%H%M%S')}.log"
    summary["log_path"] = str(log_path.relative_to(PROJECT_ROOT))
    summary["fetcher_command"] = cmd
    with log_path.open("w", encoding="utf-8") as logf:
        logf.write(f"# refresh_cache: {len(tickers)} stale tickers\n")
        logf.write(f"# command: {' '.join(cmd)}\n\n")
        logf.flush()
        proc = subprocess.run(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            check=False,
        )
    summary["fetcher_exit_code"] = proc.returncode
    return summary


def _spawn_background(args: argparse.Namespace) -> int:
    """Re-exec self in --background-child mode and detach."""
    forwarded = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "refresh_cache.py"),
        "--background-child",
    ]
    if args.force:
        forwarded.append("--force")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.tickers:
        forwarded += ["--tickers", args.tickers]
    if args.only:
        forwarded += ["--only", args.only]
    if args.db != str(DB_PATH):
        forwarded += ["--db", args.db]

    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survives parent exit
        creationflags = 0x00000008 | 0x00000200

    log_path = LOG_DIR / f"background_{datetime.now().strftime('%Y%m%dT%H%M%S')}.log"
    with log_path.open("w", encoding="utf-8") as logf:
        subprocess.Popen(
            forwarded,
            stdout=logf,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
        )
    print(json.dumps({"spawned_background": True, "log": str(log_path)}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH), help="Path to portfolio.db")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Ignore cadence; re-pull every ticker in scope (and snapshot every tier)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report stale tickers; do not invoke the fetcher",
    )
    ap.add_argument(
        "--tickers",
        help="Comma-separated tickers (still cadence-gated unless --force)",
    )
    ap.add_argument(
        "--only",
        choices=sorted(_VALID_TIER_FILTERS),
        help="Restrict scope to one list_type tier",
    )
    ap.add_argument(
        "--background",
        action="store_true",
        help="Detach a child refresh process and exit immediately (for app-startup hooks)",
    )
    ap.add_argument(
        "--background-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = ap.parse_args()

    if args.background and not args.background_child:
        return _spawn_background(args)

    explicit = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else None
    )
    only = frozenset({args.only}) if args.only else None

    summary = run_refresh(
        db_path=args.db,
        only=only,
        explicit_tickers=explicit,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, default=str))
    code = summary.get("fetcher_exit_code")
    if isinstance(code, int):
        return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
