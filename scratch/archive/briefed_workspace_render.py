"""Rebuild workspace.html with --enable-llm for portfolio + evaluation tickers.

Targets the 25 actively-tracked companies (11 portfolio + 14 evaluation).
NU and GOOG already have 2026-05-18 workspace.html with LLM content; the
others get a fresh full-quality build.

After rebuild, re-runs the date-based archive sweep so any stale _report.html
gets pushed into archive/.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
MAIN_REPO = Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary")
RESEARCH_DIR = MAIN_REPO / "output" / "research"
LOG_PATH = WORKTREE / "scratch" / "briefed_workspace_render.log"
BUILD_SCRIPT = WORKTREE / "execution" / "build_artifacts.py"

# Portfolio + Evaluation tickers (list_type in ('portfolio', 'evaluation')).
# NU and GOOG already have a 2026-05-18 LLM build; re-included for consistency.
BRIEFED = [
    # portfolio (11)
    "AMZN", "BN", "GOOG", "MELI", "META", "NOW", "NU", "NVO", "RBRK", "VEEV", "WIX",
    # evaluation (14)
    "ABNB", "BHP", "BKNG", "CGEH", "DLO", "FCX", "FIGR", "NTDOY", "NTRA",
    "SOFI", "TEM", "TMO", "UBER", "WGS",
]


def main() -> int:
    log = LOG_PATH.open("w", encoding="utf-8")
    log.write(f"=== briefed_workspace_render started at {datetime.now().isoformat()} ===\n")
    log.write(f"tickers ({len(BRIEFED)}): {BRIEFED}\n\n")
    log.flush()

    start = time.time()
    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    for i, ticker in enumerate(BRIEFED, 1):
        t0 = time.time()
        cmd = [
            sys.executable,
            str(BUILD_SCRIPT),
            "--ticker", ticker,
            "--renderer", "workspace",
            "--enable-llm",
            "--repo-root", str(MAIN_REPO),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            elapsed = time.time() - t0
            if r.returncode == 0:
                successes.append(ticker)
                log.write(f"[{i}/{len(BRIEFED)}] OK  {ticker} ({elapsed:.1f}s)\n")
            else:
                failures.append((ticker, f"exit {r.returncode}: {r.stderr[-500:]}"))
                log.write(f"[{i}/{len(BRIEFED)}] FAIL {ticker} exit {r.returncode}\n")
                log.write(f"  STDERR tail: {r.stderr[-500:]}\n")
        except subprocess.TimeoutExpired:
            failures.append((ticker, "timeout"))
            log.write(f"[{i}/{len(BRIEFED)}] TIMEOUT {ticker}\n")
        except Exception as e:
            failures.append((ticker, str(e)))
            log.write(f"[{i}/{len(BRIEFED)}] EXC {ticker}: {e}\n")
        log.flush()

    total = time.time() - start
    log.write(f"\n=== rebuild done in {total:.1f}s ===\n")
    log.write(f"successes: {len(successes)}\n")
    log.write(f"failures: {len(failures)}\n")
    for t, reason in failures:
        log.write(f"  {t}: {reason}\n")
    log.flush()

    log.write(f"\n=== archive sweep starting at {datetime.now().isoformat()} ===\n")
    log.flush()
    archived_total = run_archive_sweep(log)
    log.write(f"=== archive sweep done: archived {archived_total} files ===\n")

    log.write(f"\n=== briefed_workspace_render finished at {datetime.now().isoformat()} ===\n")
    log.close()
    return 0 if not failures else 1


def run_archive_sweep(log) -> int:
    """Re-run the date-based archive sweep across ALL tickers (briefed + others).

    Reason for scoping wide: some non-portfolio tickers (ABNB, AMAT, AMD, AMZN)
    got a workspace.html earlier in this session and may now have a same-day
    duplicate _report.html lingering from a stale prior date.
    """
    date_rx = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
    total = 0
    for ticker_dir in sorted(RESEARCH_DIR.iterdir()):
        if not ticker_dir.is_dir() or ticker_dir.name == "archive":
            continue
        files_by_date: dict[str, list[Path]] = {}
        for f in ticker_dir.iterdir():
            if not f.is_file():
                continue
            m = date_rx.match(f.name)
            if not m:
                continue
            files_by_date.setdefault(m.group(1), []).append(f)
        if not files_by_date:
            continue
        latest_date = max(files_by_date.keys())
        to_archive = [
            f for d, fs in files_by_date.items() if d != latest_date for f in fs
        ]
        if not to_archive:
            continue
        archive_dir = ticker_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        for f in to_archive:
            dst = archive_dir / f.name
            if dst.exists():
                dst.unlink()
            shutil.move(str(f), str(dst))
            total += 1
        log.write(f"  {ticker_dir.name}: archived {len(to_archive)} (kept {latest_date})\n")
        log.flush()
    return total


if __name__ == "__main__":
    raise SystemExit(main())
