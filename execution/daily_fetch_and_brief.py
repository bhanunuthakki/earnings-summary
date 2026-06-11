"""Daily worker: drain the brief_dirty queue, refresh DCFs, regenerate briefs.

Runs after the FMP/SEC fetch crons (which write to financial_facts via the
canonical extractors). The fact-table writes flip
`tracked_companies.brief_dirty = 1` via the SQL triggers from migration 0026.

For each dirty ticker, this worker checks three gates before doing real work:

  A. Tier-cadence gate (this gate is the tier-aware scalability work):
     processing_tier P1 always runs daily, P2 only if last_built_at > 7d,
     P3 only if last_built_at > 30d. This filters the daily tick down to a
     manageable subset of the ~2.4k tracked universe. Bypassed with
     --ignore-tier (preserves the pre-tier behavior of running on every
     dirty ticker).
  B. Material-change gate: hash (max financial_facts.period_end + max
     kpi_facts.period_end + transcripts count + commitments count + holdings
     JSON bytes + DCF workbook mtime). Compare to `last_brief_hash`. If they
     match AND last_built_at < 7 days ago → skip rebuild (clear brief_dirty).
  C. Evaluation cadence: if list_type='evaluation' AND last_built_at < 7
     days ago → skip rebuild. (Portfolio remains daily.)

If no gate skips, runs the synthesize → publish chain:

  thesis evaluator    → writes a fresh thesis_evaluations row
  match_commitments   → fills Say-Do outcomes for periods that just landed
  refresh_dcf         → re-reads the DCF workbook, recomputes PV / over-under
  build_artifacts     → regenerates the brief (HTML / MD / JSON / xlsx)
  sweep_output_history→ moves prior dated artifacts into output/research/<T>/archive/

Then clears brief_dirty, records last_built_at and last_brief_hash. Each step
is run as a subprocess so a failure in one ticker doesn't poison the worker's
Python state.

Usage:
    python execution/daily_fetch_and_brief.py                   # dirty tickers due per tier cadence
    python execution/daily_fetch_and_brief.py --ticker META     # force-refresh one (bypasses all gates)
    python execution/daily_fetch_and_brief.py --enable-llm      # populate §8/§9 LLM sections
    python execution/daily_fetch_and_brief.py --force           # bypass gates B + C for queued tickers
    python execution/daily_fetch_and_brief.py --ignore-tier     # bypass tier-cadence gate (run on all dirty)
    python execution/daily_fetch_and_brief.py --eval-cadence-days 14  # tune evaluation cadence
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from llm_artifact_store import mark_artifacts_dirty_for_fact_change  # noqa: E402
from pipeline.tier_runner import tickers_due_for_refresh  # noqa: E402

DEFAULT_EVAL_CADENCE_DAYS = 7
DEFAULT_NO_CHANGE_TTL_DAYS = 7


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        all_tickers = _resolve_tickers(conn, args)

    tickers, skipped_by_tier = _apply_tier_filter(all_tickers, repo_root, args)

    if skipped_by_tier:
        print(
            json.dumps(
                {"event": "tier_skip_summary", "skipped_by_tier": skipped_by_tier}
            )
        )

    if not tickers:
        print(json.dumps({"event": "no_dirty_tickers"}))
        return 0

    results: list[dict[str, object]] = []
    for ticker in tickers:
        result = _refresh_one_ticker(ticker, repo_root, db_path, args)
        results.append(result)

    print(
        json.dumps(
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "tickers": results,
                "skipped_by_tier": skipped_by_tier,
            },
            indent=2,
        )
    )
    return 0


def _apply_tier_filter(
    candidate_tickers: list[str], repo_root: Path, args: argparse.Namespace
) -> tuple[list[str], dict[str, int]]:
    """Intersect the candidate set with the tickers due per the daily-tier cadence.

    With `--ignore-tier`, `--ticker`, or `--force`: returns every candidate
    (the tier gate is bypassed). Per-tier skip counts are logged for cron
    visibility.
    """
    # Explicit single-ticker or force flags bypass the tier gate; the user is
    # asking for an unconditional rebuild.
    if args.ignore_tier or args.ticker or args.force:
        return (list(candidate_tickers), {})

    due = set(tickers_due_for_refresh(repo_root, "daily"))
    accepted: list[str] = []
    skipped: list[str] = []
    for t in candidate_tickers:
        if t.upper() in due:
            accepted.append(t)
        else:
            skipped.append(t)

    # Per-tier breakdown of what we skipped, for cron log readability.
    skipped_by_tier: dict[str, int] = {}
    if skipped:
        db_path = repo_root / "data" / "portfolio.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" * len(skipped))
            rows = conn.execute(
                f"SELECT UPPER(ticker) AS ticker, processing_tier "
                f"FROM tracked_companies WHERE UPPER(ticker) IN ({placeholders})",
                [s.upper() for s in skipped],
            ).fetchall()
            for r in rows:
                tier = (r["processing_tier"] or "P3").upper()
                skipped_by_tier[tier] = skipped_by_tier.get(tier, 0) + 1
    return (accepted, skipped_by_tier)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ticker", help="Force-refresh a specific ticker, ignoring brief_dirty + bypassing gates")
    g.add_argument(
        "--all-tracked", action="store_true", help="Refresh every tracked ticker (heavy)"
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--enable-llm",
        action="store_true",
        help="Pass --enable-llm to build_artifacts (populates §8 recent_developments + §9 bear_case)",
    )
    p.add_argument(
        "--renderer",
        choices=("default", "workspace", "both"),
        default="both",
        help=(
            "Which HTML renderer build_artifacts emits. 'default' writes only the "
            "legacy {DATE}_report.html; 'workspace' writes only the tabbed "
            "{DATE}_workspace.html; 'both' (default) writes both. Default is 'both' so "
            "the dashboard/comments stack gets the workspace artifact it globs for "
            "while build_earnings_calendar still finds report.html."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="If > 0, cap the number of tickers refreshed this run (useful for testing).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Bypass material-change gate (B) and evaluation-cadence gate (C). Rebuilds every queued ticker.",
    )
    p.add_argument(
        "--ignore-tier",
        action="store_true",
        help="Bypass tier-cadence gate (A) — run on every dirty ticker regardless of processing_tier. "
        "Preserves pre-tier behavior; gates B + C still apply.",
    )
    p.add_argument(
        "--eval-cadence-days",
        type=int,
        default=DEFAULT_EVAL_CADENCE_DAYS,
        help=f"Minimum days between rebuilds for list_type='evaluation' (default {DEFAULT_EVAL_CADENCE_DAYS}).",
    )
    p.add_argument(
        "--no-change-ttl-days",
        type=int,
        default=DEFAULT_NO_CHANGE_TTL_DAYS,
        help=f"Max age in days for a 'same content' skip to apply (default {DEFAULT_NO_CHANGE_TTL_DAYS}).",
    )
    return p.parse_args()


def _resolve_tickers(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    """Tickers to refresh this run. Explicit > all-tracked > brief_dirty queue + P1."""
    cursor = conn.cursor()
    if args.ticker:
        return [args.ticker.upper()]
    if args.all_tracked:
        cursor.execute(
            f"SELECT ticker FROM tracked_companies "
            f"WHERE list_type IN {db.ACTIVE_LIST_TYPES_SQL} "
            f"AND (archived_at IS NULL) "
            f"ORDER BY ticker"
        )
    elif _has_processing_tier_column(conn):
        # Dirty queue plus every P1 name. The tier-cadence gate documents "P1
        # always runs daily", but a dirty-only candidate set leaves P1 out on
        # days when no fact write happens to land (e.g. every statement
        # endpoint tier-blocked on the free FMP plan). Gates B + C still make
        # a no-change P1 day a cheap skip — and the skip itself records
        # last_built_at, which is what the dashboard's daily-freshness strip
        # measures.
        cursor.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE (brief_dirty = 1 OR UPPER(COALESCE(processing_tier, '')) = 'P1') "
            "AND (archived_at IS NULL) "
            "ORDER BY ticker"
        )
    else:
        cursor.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE brief_dirty = 1 AND (archived_at IS NULL) "
            "ORDER BY ticker"
        )
    rows = cursor.fetchall()
    tickers = [str(r["ticker"]).upper() for r in rows]
    if args.limit > 0:
        tickers = tickers[: args.limit]
    return tickers


def _has_processing_tier_column(conn: sqlite3.Connection) -> bool:
    """Mirror of tier_runner's guard — pre-0054 DBs lack processing_tier."""
    cur = conn.execute("PRAGMA table_info(tracked_companies)")
    return any(row[1] == "processing_tier" for row in cur.fetchall())


def _compute_brief_hash(conn: sqlite3.Connection, ticker: str, repo_root: Path) -> str:
    """SHA256 of the material inputs that feed a brief.

    Stable when nothing meaningful has changed since the last build:
      - max(financial_facts.period_end) for the ticker
      - max(kpi_facts.period_end) for the ticker
      - COUNT(transcripts) for the ticker
      - COUNT(management_commitments) for the ticker
      - holdings JSON bytes (catches thesis/KPI/break-rule edits)
      - DCF workbook mtime (catches user workbook edits)

    Live price is deliberately excluded — it ticks daily and would never
    let the hash stabilize. The brief's DCF over/under % shifting by 0.x%
    isn't worth re-running the LLM sections.
    """
    h = hashlib.sha256()
    for sql in (
        "SELECT COALESCE(MAX(period_end), '') FROM financial_facts WHERE UPPER(ticker)=?",
        "SELECT COALESCE(MAX(period_end), '') FROM kpi_facts WHERE UPPER(ticker)=?",
    ):
        row = conn.execute(sql, (ticker.upper(),)).fetchone()
        h.update(b"\x00")
        h.update(str(row[0] if row else "").encode())
    for sql in (
        "SELECT COUNT(*) FROM transcripts WHERE UPPER(ticker)=?",
        "SELECT COUNT(*) FROM management_commitments WHERE UPPER(ticker)=?",
    ):
        row = conn.execute(sql, (ticker.upper(),)).fetchone()
        h.update(b"\x00")
        h.update(str(row[0] if row else 0).encode())

    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    h.update(b"\x00")
    if holdings_path.exists():
        try:
            h.update(holdings_path.read_bytes())
        except OSError:
            h.update(b"holdings_unreadable")

    dcf_path = repo_root / "dcf" / f"{ticker.upper()}.xlsx"
    h.update(b"\x00")
    if dcf_path.exists():
        try:
            h.update(str(dcf_path.stat().st_mtime).encode())
        except OSError:
            h.update(b"dcf_unreadable")

    return h.hexdigest()


def _check_skip_gates(
    conn: sqlite3.Connection,
    ticker: str,
    repo_root: Path,
    *,
    force: bool,
    eval_cadence_days: int,
    no_change_ttl_days: int,
) -> tuple[bool, str | None, str]:
    """Apply gates B (material change) and C (eval cadence).

    Returns (skip, reason_if_skip, current_hash). The current_hash is always
    computed so we can persist it on a successful rebuild.
    """
    current_hash = _compute_brief_hash(conn, ticker, repo_root)
    if force:
        return (False, None, current_hash)

    row = conn.execute(
        "SELECT list_type, last_built_at, last_brief_hash FROM tracked_companies "
        "WHERE UPPER(ticker)=?",
        (ticker.upper(),),
    ).fetchone()
    if row is None:
        return (False, None, current_hash)

    list_type = row["list_type"] if hasattr(row, "keys") else row[0]
    last_built_at_str = row["last_built_at"] if hasattr(row, "keys") else row[1]
    last_brief_hash = row["last_brief_hash"] if hasattr(row, "keys") else row[2]

    last_built_at: datetime | None = None
    if last_built_at_str:
        try:
            last_built_at = datetime.fromisoformat(str(last_built_at_str))
        except ValueError:
            last_built_at = None

    now = datetime.now()

    # Gate C: evaluation cadence — weekly rebuild even if dirty
    if list_type == "evaluation" and last_built_at is not None:
        age = now - last_built_at
        if age < timedelta(days=eval_cadence_days):
            return (True, f"evaluation_cadence (age={age.days}d < {eval_cadence_days}d)", current_hash)

    # Gate B: material-change — if hash matches AND build is fresh, skip
    if (
        last_brief_hash
        and current_hash == last_brief_hash
        and last_built_at is not None
    ):
        age = now - last_built_at
        if age < timedelta(days=no_change_ttl_days):
            return (True, f"no_material_change (age={age.days}d, hash matches)", current_hash)

    return (False, None, current_hash)


def _record_skip(
    db_path: Path, ticker: str, current_hash: str, reason: str
) -> None:
    """On gate skip: clear brief_dirty + record last_built_at + last_brief_hash.

    Clearing the dirty flag is intentional — the gate is saying "nothing new to
    build". Recording last_built_at + hash means subsequent runs will keep
    skipping until either content changes (hash flips) or the TTL elapses.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE tracked_companies "
            "SET brief_dirty = 0, last_built_at = ?, last_brief_hash = ? "
            "WHERE UPPER(ticker) = ?",
            (now, current_hash, ticker.upper()),
        )
        conn.commit()


def _refresh_one_ticker(
    ticker: str, repo_root: Path, db_path: Path, args: argparse.Namespace
) -> dict[str, object]:
    """Run the synth-and-publish chain for one ticker; clear dirty flag on success.

    Skips the chain entirely (per gates B + C) when content hasn't changed
    materially OR an evaluation ticker is within its cadence window. Per-ticker
    `--ticker` overrides bypass gates implicitly via `args.force`-equivalent.
    """
    # Per-ticker explicit invocation = force (bypass gates)
    forced = bool(args.force or args.ticker)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        skip, reason, current_hash = _check_skip_gates(
            conn,
            ticker,
            repo_root,
            force=forced,
            eval_cadence_days=args.eval_cadence_days,
            no_change_ttl_days=args.no_change_ttl_days,
        )

    if skip:
        _record_skip(db_path, ticker, current_hash, reason or "skipped")
        return {"ticker": ticker, "status": "skipped", "skip_reason": reason}

    stages: list[dict[str, object]] = []

    stages.append(
        _run_step(
            "thesis_evaluator",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "run_thesis_evaluator.py"),
                "--ticker",
                ticker,
            ],
            cwd=repo_root,
        )
    )
    stages.append(
        _run_step(
            "match_commitments",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "match_commitments.py"),
                "--ticker",
                ticker,
            ],
            cwd=repo_root,
        )
    )
    stages.append(
        _run_step(
            "refresh_dcf",
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "refresh_dcf.py"),
                "--ticker",
                ticker,
                "--repo-root",
                str(repo_root),
            ],
            cwd=repo_root,
        )
    )
    # Mark fact-dependent LLM artifacts dirty so the build regenerates them
    # rather than hitting a stale cache. Cheap (one UPDATE per ticker) and
    # only matters when --enable-llm is set, but harmless otherwise.
    flipped = mark_artifacts_dirty_for_fact_change(
        ticker=ticker,
        reason="brief_rebuild",
        db_path=db_path,
    )
    if flipped:
        sys.stderr.write(
            json.dumps({
                "event": "llm_artifacts_marked_dirty",
                "ticker": ticker,
                "count": flipped,
            }) + "\n"
        )

    build_cmd = [
        sys.executable,
        str(PROJECT_ROOT / "execution" / "build_artifacts.py"),
        "--ticker",
        ticker,
        "--repo-root",
        str(repo_root),
        "--allow-untracked",
        "--renderer",
        args.renderer,
    ]
    if args.enable_llm:
        build_cmd.append("--enable-llm")
    stages.append(_run_step("build_artifacts", build_cmd, cwd=repo_root))

    # Only sweep when build_artifacts succeeded — otherwise a stale prior
    # artifact is the only thing the user has to look at.
    if stages[-1]["exit_code"] == 0:
        stages.append(
            _run_step(
                "sweep_output_history",
                [
                    sys.executable,
                    str(PROJECT_ROOT / "execution" / "sweep_output_history.py"),
                    "--ticker",
                    ticker,
                    "--repo-root",
                    str(repo_root),
                ],
                cwd=repo_root,
            )
        )

    overall_ok = all(s["exit_code"] == 0 for s in stages)
    if overall_ok:
        # Re-compute hash AFTER build because match_commitments / refresh_dcf
        # may have written new rows that should be reflected.
        with sqlite3.connect(str(db_path)) as conn:
            final_hash = _compute_brief_hash(conn, ticker, repo_root)
            conn.execute(
                "UPDATE tracked_companies "
                "SET brief_dirty = 0, last_built_at = ?, last_brief_hash = ? "
                "WHERE UPPER(ticker) = ?",
                (datetime.now().isoformat(timespec="seconds"), final_hash, ticker.upper()),
            )
            conn.commit()
    return {"ticker": ticker, "status": "ok" if overall_ok else "failed", "stages": stages}


def _run_step(name: str, cmd: list[str], cwd: Path) -> dict[str, object]:
    """Run one step. Captures exit code + truncated stderr; never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "exit_code": -1, "error": "timeout after 600s"}
    except OSError as e:
        return {"name": name, "exit_code": -1, "error": f"spawn failed: {e}"}
    return {
        "name": name,
        "exit_code": proc.returncode,
        "stderr_tail": _tail(proc.stderr, 400) if proc.returncode != 0 else "",
    }


def _tail(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return "…" + s[-n:]


if __name__ == "__main__":
    raise SystemExit(main())
