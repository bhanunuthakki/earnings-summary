"""One-off: rebuild workspace briefs for portfolio + eval after transcript backfill.

Triggered by the 2026-05-21 raw→processed transcript move (77 files) +
backfill of BHP/WGS (3 new files). The workspace renderer reads transcripts
from `transcripts/processed/`, so the moves only show up after a rebuild.

Uses --enable-llm with extended news-cache TTL (30d) to avoid re-running the
expensive WebSearch pass — news cache files date from May 13 (outside 7d
default), but content hasn't shifted enough to warrant a fresh fetch.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary")
BUILD_SCRIPT = PROJECT_ROOT / "execution" / "build_artifacts.py"
LOG_PATH = Path(__file__).resolve().parent / "rebuild_briefs_2026_05_21.log"

PORTFOLIO = ["AMZN", "BN", "GOOG", "MELI", "META", "NOW", "NU", "NVO", "RBRK", "VEEV", "WIX"]
EVALUATION = ["ABNB", "BHP", "BKNG", "CGEH", "DLO", "FCX", "FIGR", "LLY", "NTDOY", "NTRA",
              "SOFI", "TEM", "TMO", "UBER", "WGS"]


def main() -> int:
    tickers = PORTFOLIO + EVALUATION
    log = LOG_PATH.open("w", encoding="utf-8")
    log.write(f"=== rebuild_briefs_2026_05_21 started at {datetime.now().isoformat()} ===\n")
    log.write(f"tickers ({len(tickers)}): {tickers}\n\n")
    log.flush()

    start = time.time()
    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    for i, ticker in enumerate(tickers, 1):
        t0 = time.time()
        cmd = [
            sys.executable,
            str(BUILD_SCRIPT),
            "--ticker", ticker,
            "--renderer", "workspace",
            "--enable-llm",
            "--news-cache-ttl-days", "30",
            "--allow-untracked",
            "--repo-root", str(PROJECT_ROOT),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               encoding="utf-8", errors="replace")
            elapsed = time.time() - t0
            if r.returncode == 0:
                successes.append(ticker)
                log.write(f"[{i}/{len(tickers)}] OK   {ticker} ({elapsed:.1f}s)\n")
            else:
                failures.append((ticker, f"exit {r.returncode}: {(r.stderr or '')[-500:]}"))
                log.write(f"[{i}/{len(tickers)}] FAIL {ticker} exit {r.returncode} ({elapsed:.1f}s)\n")
                log.write(f"  STDERR tail: {(r.stderr or '')[-500:]}\n")
        except subprocess.TimeoutExpired:
            failures.append((ticker, "timeout"))
            log.write(f"[{i}/{len(tickers)}] TIMEOUT {ticker}\n")
        except Exception as e:
            failures.append((ticker, str(e)))
            log.write(f"[{i}/{len(tickers)}] EXC {ticker}: {e}\n")
        log.flush()

    total = time.time() - start
    log.write(f"\n=== rebuild done in {total:.1f}s ===\n")
    log.write(f"successes: {len(successes)}\n")
    log.write(f"failures: {len(failures)}\n")
    for t, reason in failures:
        log.write(f"  {t}: {reason}\n")
    log.flush()
    log.close()
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
