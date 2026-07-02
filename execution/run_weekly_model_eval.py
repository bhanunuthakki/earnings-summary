"""Weekly model-eval loop orchestrator — harvest → sweep → AUTO-SWITCH.

This is the activated, closed-loop version of the model-downgrade eval
(directives/model_eval_loop.md). One run does three things in order:

  1. HARVEST a bounded, rotating sample of tickers: force the incumbent model to
     re-run on real prompts (with LLM_CAPTURE_DIR set, in a SUBPROCESS) so fresh
     prompt cases land in the capture dir. Rotating by ISO week keeps weekly cost
     bounded while covering the universe over time.
  2. SWEEP: evaluate every cheaper candidate per active purpose against the
     captured prompts and INSERT verdicts into model_eval_verdicts (run in THIS
     process with capture OFF, so eval traffic never re-enters the corpus).
  3. APPLY: turn accumulated verdicts into action — write a model_pin_override
     after N consecutive SWITCH_DOWN, revert after N consecutive KEEP_INCUMBENT.
     This is the auto-switch. It is conservative (a streak, not one run) and
     reversible; every decision is logged to model_pin_overrides + alerts, and
     every verdict carries its full per-case judge rationale in summary_json — so
     the whole loop is auditable from the DB alone.

Capture isolation: the harvest sets LLM_CAPTURE_DIR only in the harvest
subprocess env; this orchestrator's own process never has it set, so the sweep's
candidate runs are not captured.

Usage:
    python execution/run_weekly_model_eval.py --repo-root <MAIN>
    python execution/run_weekly_model_eval.py --repo-root <MAIN> --dry-run-switches
    python execution/run_weekly_model_eval.py --repo-root <MAIN> --skip-harvest \\
        --tickers NU,MELI    # eval-only over whatever's already captured
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(
    0, str(PROJECT_ROOT / "execution")
)  # for run_model_eval_sweep + apply_model_switches

log = logging.getLogger("run_weekly_model_eval")

# Harvest commands per ticker. Each forces a real incumbent LLM call so the
# prompt is captured (not served from the artifact cache). Keep this list to
# purposes whose harvest is known-reliable; extend as new purposes are wired.
#   - build_artifacts --force-refresh  -> bear_case (+ recent_developments)
#   - extract_company_description --refresh -> company_description
_HARVEST_STEPS: tuple[tuple[str, list[str]], ...] = (
    ("bear_case", ["execution/build_artifacts.py", "--enable-llm", "--force-refresh"]),
    ("company_description", ["execution/extract_company_description.py", "--refresh"]),
)


def _tracked_tickers(repo_root: Path) -> list[str]:
    """Discover tracked tickers from the DB (tracked_companies), newest-activity
    first. Empty list if unavailable — the caller then requires --tickers."""
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "tracked_companies" not in tables:
                return []
            rows = conn.execute(
                "SELECT DISTINCT ticker FROM tracked_companies ORDER BY ticker"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception as exc:
        log.warning("ticker discovery failed: %s", exc)
        return []


def _rotating_sample(tickers: list[str], size: int, week: int) -> list[str]:
    """Pick ``size`` tickers, rotating the window by ISO week so the whole
    universe is covered over consecutive weeks (deterministic, no RNG)."""
    if not tickers or size <= 0:
        return []
    if size >= len(tickers):
        return list(tickers)
    start = (week * size) % len(tickers)
    rotated = tickers[start:] + tickers[:start]
    return rotated[:size]


def _harvest(ticker: str, capture_dir: Path, repo_root: Path, timeout_s: int) -> None:
    """Run the harvest steps for one ticker in subprocesses with LLM_CAPTURE_DIR
    set. Failures are logged and skipped — a thin capture is better than aborting
    the whole weekly run."""
    env = dict(os.environ)
    env["LLM_CAPTURE_DIR"] = str(capture_dir)
    for purpose, cmd in _HARVEST_STEPS:
        full = [
            sys.executable,
            str(PROJECT_ROOT / cmd[0]),
            "--ticker",
            ticker,
            *cmd[1:],
            "--repo-root",
            str(repo_root),
        ]
        log.info("  harvest %s/%s ...", ticker, purpose)
        try:
            subprocess.run(full, env=env, cwd=str(repo_root), timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            log.warning("  harvest %s/%s timed out (%ds) — skipped", ticker, purpose, timeout_s)
        except Exception as exc:
            log.warning("  harvest %s/%s failed: %s", ticker, purpose, exc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weekly model-eval loop: harvest -> sweep -> auto-switch."
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--capture-dir", type=Path, default=None, help="default <repo>/data/llm_capture"
    )
    parser.add_argument(
        "--tickers", help="comma list; default = rotating sample of tracked tickers"
    )
    parser.add_argument(
        "--sample-size", type=int, default=2, help="tickers to harvest this week (default 2)"
    )
    parser.add_argument(
        "--skip-harvest", action="store_true", help="eval only whatever is already captured"
    )
    parser.add_argument("--skip-apply", action="store_true", help="sweep only; do not auto-switch")
    parser.add_argument(
        "--dry-run-switches", action="store_true", help="evaluate switches but write no overrides"
    )
    parser.add_argument("--consecutive-switch", type=int, default=3)
    parser.add_argument("--consecutive-keep", type=int, default=3)
    parser.add_argument(
        "--harvest-timeout", type=int, default=2400, help="per harvest step seconds"
    )
    parser.add_argument(
        "--sweep-limit",
        type=int,
        default=16,
        help="CAP on prompt cases per purpose (default 16 = the RISKY tier size, "
        "so the sweep's per-tier n — RISKY 16 / default 12 — governs; lower it "
        "only for quick manual runs)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo_root = args.repo_root.resolve()
    capture_dir = args.capture_dir or (repo_root / "data" / "llm_capture")
    db_path = repo_root / "data" / "portfolio.db"

    # Capture must be OFF in THIS process (the sweep runs here). The harvest sets
    # it only in its subprocess env. Guard against an inherited value.
    if os.environ.pop("LLM_CAPTURE_DIR", None):
        log.warning("cleared inherited LLM_CAPTURE_DIR from this process (sweep must not capture)")

    import db as _db

    if db_path.exists():
        _db.set_db_path(db_path)

    log.info(
        "=== weekly model-eval loop %s ===", datetime.now(UTC).replace(tzinfo=None).isoformat()
    )

    # ---- 1. Harvest ----
    if not args.skip_harvest:
        if args.tickers:
            sample = [t.strip() for t in str(args.tickers).split(",") if t.strip()]
        else:
            tracked = _tracked_tickers(repo_root)
            # ISO week number for deterministic rotation.
            week = datetime.now(UTC).isocalendar().week
            sample = _rotating_sample(tracked, args.sample_size, week)
        if not sample:
            log.warning(
                "no harvest tickers (pass --tickers or populate tracked_companies); skipping harvest"
            )
        else:
            log.info("harvest sample (%d): %s", len(sample), ", ".join(sample))
            capture_dir.mkdir(parents=True, exist_ok=True)
            for ticker in sample:
                _harvest(ticker, capture_dir, repo_root, args.harvest_timeout)

    # ---- 2. Sweep (capture OFF) ----
    from run_model_eval_sweep import run_sweep

    log.info("--- sweep ---")
    verdicts = run_sweep(
        capture_dir=capture_dir,
        db_path=db_path,
        purposes=None,
        judges=["claude", "gemini"],
        limit=args.sweep_limit,
        lookback_days=30,
        min_n=4,
        parity_threshold=0.8,
        timeout_seconds=None,
        persist=True,
    )
    log.info("sweep wrote %d verdict(s)", len(verdicts))

    # ---- 3. Apply (auto-switch) ----
    if args.skip_apply:
        log.info("--skip-apply: not applying switches")
        return 0

    from apply_model_switches import evaluate_switches

    log.info("--- apply switches (auto=%s) ---", not args.dry_run_switches)
    results = evaluate_switches(
        db_path,
        consecutive_switch=args.consecutive_switch,
        consecutive_keep=args.consecutive_keep,
        dry_run=args.dry_run_switches,
    )
    for r in results:
        log.info("  %s  %s -> %s  (%s)", r.action, r.purpose, r.model or "code-pin", r.reason)
    switched = sum(1 for r in results if r.action in ("SWITCHED_DOWN", "DRY_RUN_SWITCH"))
    reverted = sum(1 for r in results if r.action in ("REVERTED", "DRY_RUN_REVERT"))
    log.info(
        "=== done: %d verdict(s), %d switched, %d reverted ===", len(verdicts), switched, reverted
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
