#!/usr/bin/env python
"""Resumable background sweep: onboard FMP + build --enable-llm briefs for the watchlist.

Runs MAIN's scripts (PROJECT_ROOT-rooted; they write MAIN's data/portfolio.db) via MAIN's
venv python with cwd=MAIN. State (progress JSON + per-ticker logs) lives in a stable dir
OUTSIDE the repo (SWEEP_STATE_DIR) so the sweep is resumable across sessions and survives
worktree cleanup. Continue-on-failure. Per-step timeouts.

Phases:
  onboard - onboard_ticker.py --industry-template auto --skip-ir for not-yet-onboarded-ok names
  build   - build_artifacts.py --enable-llm ... --flavor evaluation for onboarded, data-bearing
            names not yet built-ok. Inline retry up to 3x to heal a degraded bear_case.
  heal    - re-build names whose latest provenance bear_case != ok (final straggler pass)
  all     - onboard (all) -> build (all) -> heal
  report  - print a status digest from progress + provenance and exit (no work)

Usage:
  python sweep.py --phase onboard
  python sweep.py --phase build --max-new 12
  python sweep.py --phase heal
  python sweep.py --phase report
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAIN = Path(r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary")
PY = MAIN / "venv" / "Scripts" / "python.exe"
ONBOARD = MAIN / "execution" / "onboard_ticker.py"
BUILD = MAIN / "execution" / "build_artifacts.py"
DB = MAIN / "data" / "portfolio.db"
FMP_DIR = MAIN / "data" / "historical" / "fmp"

# State lives OUTSIDE the repo/worktree so the sweep is resumable across sessions
# and survives worktree cleanup — any session that runs this driver shares the same
# progress + logs. Override with the SWEEP_STATE_DIR env var.
SWEEP_STATE = Path(
    os.environ.get(
        "SWEEP_STATE_DIR",
        r"C:\Users\Bhanu\.gemini\antigravity\scratch\sweep_state",
    )
)
LOGDIR = SWEEP_STATE / "logs"
SWEEP_STATE.mkdir(parents=True, exist_ok=True)
LOGDIR.mkdir(parents=True, exist_ok=True)
PROGRESS = SWEEP_STATE / "sweep_progress.json"
STATUS = SWEEP_STATE / "sweep_status.txt"
DRIVER_LOG = LOGDIR / "driver.log"

ONBOARD_TIMEOUT = 1800  # 30 min/ticker hard cap
BUILD_TIMEOUT = 3000  # 50 min/ticker hard cap
BUILD_ATTEMPTS = 3  # heal a degraded bear_case (cheap; cached sections serve)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_driver(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    try:
        with open(DRIVER_LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_progress() -> dict:
    if PROGRESS.exists():
        try:
            data = json.loads(PROGRESS.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"started": now_iso(), "onboard": {}, "build": {}}


def save_progress(p: dict) -> None:
    tmp = PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS)


def write_status(p: dict, current: str = "") -> None:
    ob, bd = p["onboard"], p["build"]
    onboarded = sum(1 for v in ob.values() if v.get("status") == "ok")
    fmp_data = sum(1 for v in ob.values() if v.get("has_data"))
    built = sum(1 for v in bd.values() if v.get("status") == "ok")
    bear_ok = sum(1 for v in bd.values() if v.get("bear_case") == "ok")
    line = (
        f"[{now_iso()}] {current} | onboarded={onboarded} fmp_data={fmp_data} "
        f"built={built} bear_ok={bear_ok}"
    )
    STATUS.write_text(line + "\n", encoding="utf-8")
    log_driver(line)


def watchlist_tickers() -> list[str]:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT ticker FROM tracked_companies WHERE list_type='watchlist' "
        "AND archived_at IS NULL ORDER BY ticker"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def fmp_coverage(ticker: str) -> tuple[str | None, int]:
    upto = None
    try:
        con = sqlite3.connect(str(DB))
        row = con.execute(
            "SELECT fmp_data_upto FROM tracked_companies WHERE ticker=?", (ticker,)
        ).fetchone()
        con.close()
        upto = row[0] if row else None
    except sqlite3.Error:
        pass
    nfiles = len(list(FMP_DIR.glob(f"{ticker}_*.json"))) if FMP_DIR.exists() else 0
    return upto, nfiles


def bear_case_status(ticker: str) -> str:
    try:
        con = sqlite3.connect(str(DB))
        row = con.execute(
            "SELECT sections_status FROM brief_provenance_log WHERE ticker=? "
            "ORDER BY id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        con.close()
    except sqlite3.Error:
        return "unknown"
    if not row or not row[0]:
        return "unknown"
    try:
        d = json.loads(row[0])
        v = d.get("bear_case") if isinstance(d, dict) else None
        return v if isinstance(v, str) else "unknown"
    except json.JSONDecodeError:
        return "unknown"


def latest_artifact(ticker: str) -> str | None:
    d = MAIN / "output" / "research" / ticker
    if not d.exists():
        return None
    cands = sorted(d.glob("*_workspace.html"))
    return str(cands[-1]) if cands else None


def db_built_ok(ticker: str) -> bool:
    """True if MAIN already holds a good build (bear_case ok + a workspace artifact)
    for this ticker, so a fresh session can skip the expensive re-build even when the
    progress JSON is unavailable."""
    return bear_case_status(ticker) == "ok" and latest_artifact(ticker) is not None


def run_step(cmd: list[str], logpath: Path, timeout: int) -> tuple[int, float, bool]:
    start = time.time()
    with open(logpath, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"\n\n===== {now_iso()} CMD: {' '.join(str(c) for c in cmd)} =====\n")
        fh.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=str(MAIN), stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
            )
            return proc.returncode, round(time.time() - start, 1), False
        except subprocess.TimeoutExpired:
            fh.write(f"\n[driver] TIMEOUT after {timeout}s\n")
            return -9, round(time.time() - start, 1), True
        except OSError as exc:
            fh.write(f"\n[driver] ERROR: {exc!r}\n")
            return -1, round(time.time() - start, 1), False


def do_onboard(ticker: str, p: dict) -> None:
    logpath = LOGDIR / f"onboard_{ticker}.log"
    cmd = [
        str(PY),
        str(ONBOARD),
        "--ticker",
        ticker,
        "--industry-template",
        "auto",
        "--skip-ir",
    ]
    rc, elapsed, timed_out = run_step(cmd, logpath, ONBOARD_TIMEOUT)
    upto, nfiles = fmp_coverage(ticker)
    p["onboard"][ticker] = {
        "status": "ok" if rc == 0 else ("timeout" if timed_out else "fail"),
        "rc": rc,
        "elapsed": elapsed,
        "fmp_data_upto": upto,
        "fmp_files": nfiles,
        "has_data": bool(upto) or nfiles >= 5,
        "ts": now_iso(),
        "log": str(logpath),
    }
    save_progress(p)
    log_driver(
        f"onboard {ticker}: rc={rc} elapsed={elapsed}s fmp_upto={upto} "
        f"files={nfiles} has_data={p['onboard'][ticker]['has_data']}"
    )


def do_build(ticker: str, p: dict) -> dict:
    logpath = LOGDIR / f"build_{ticker}.log"
    cmd = [
        str(PY),
        str(BUILD),
        "--ticker",
        ticker,
        "--enable-llm",
        "--renderer",
        "workspace",
        "--repo-root",
        str(MAIN),
        "--allow-untracked",
        "--trigger",
        "on_demand",
        "--flavor",
        "evaluation",
    ]
    last: dict = {}
    for attempt in range(1, BUILD_ATTEMPTS + 1):
        rc, elapsed, timed_out = run_step(cmd, logpath, BUILD_TIMEOUT)
        bc = bear_case_status(ticker)
        last = {
            "status": "ok" if rc == 0 else ("timeout" if timed_out else "fail"),
            "rc": rc,
            "elapsed": elapsed,
            "bear_case": bc,
            "attempts": attempt,
            "artifact": latest_artifact(ticker),
            "ts": now_iso(),
            "log": str(logpath),
        }
        p["build"][ticker] = last
        save_progress(p)
        log_driver(f"build {ticker} attempt {attempt}: rc={rc} bear={bc} elapsed={elapsed}s")
        if rc == 0 and bc == "ok":
            break
        if rc != 0 and attempt >= 2:
            # Don't hammer a hard-crashing build (vs. a cheap bear_case re-heal).
            break
    return last


def phase_onboard(tickers: list[str], p: dict, max_new: int) -> None:
    done = 0
    for i, t in enumerate(tickers, 1):
        if p["onboard"].get(t, {}).get("status") == "ok":
            continue
        if max_new and done >= max_new:
            break
        write_status(p, f"onboarding {t} ({i}/{len(tickers)})")
        do_onboard(t, p)
        done += 1
    write_status(p, "onboard phase checkpoint")


def phase_build(tickers: list[str], p: dict, max_new: int) -> None:
    done = 0
    for i, t in enumerate(tickers, 1):
        if not p["onboard"].get(t, {}).get("has_data"):
            continue
        bd = p["build"].get(t, {})
        if bd.get("status") == "ok" and bd.get("bear_case") == "ok":
            continue
        if db_built_ok(t):
            p["build"][t] = {
                "status": "ok",
                "bear_case": "ok",
                "attempts": 0,
                "artifact": latest_artifact(t),
                "ts": now_iso(),
                "note": "skipped: already built (DB state)",
            }
            save_progress(p)
            continue
        if max_new and done >= max_new:
            break
        write_status(p, f"building {t} ({i}/{len(tickers)})")
        do_build(t, p)
        done += 1
    write_status(p, "build phase checkpoint")


def phase_heal(tickers: list[str], p: dict) -> None:
    for t in tickers:
        if not p["onboard"].get(t, {}).get("has_data"):
            continue
        bc = bear_case_status(t)
        if bc != "ok":
            write_status(p, f"healing {t} (bear={bc})")
            do_build(t, p)
    write_status(p, "heal phase done")


def phase_report(tickers: list[str], p: dict) -> None:
    ob, bd = p["onboard"], p["build"]
    print(f"=== SWEEP REPORT {now_iso()} ===")
    print(f"started: {p.get('started')}")
    onboarded = [t for t in tickers if ob.get(t, {}).get("status") == "ok"]
    no_data = [t for t in tickers if t in ob and not ob[t].get("has_data")]
    not_onboarded = [t for t in tickers if t not in ob]
    built_ok = [t for t in tickers if bd.get(t, {}).get("status") == "ok"]
    bear_ok = [t for t in tickers if bd.get(t, {}).get("bear_case") == "ok"]
    bear_bad = [
        t
        for t in tickers
        if bd.get(t, {}).get("status") == "ok" and bd.get(t, {}).get("bear_case") != "ok"
    ]
    print(f"onboarded_ok: {len(onboarded)}/{len(tickers)}")
    print(f"no_data (FMP empty/thin): {len(no_data)} -> {','.join(no_data)}")
    print(f"not_yet_onboarded: {len(not_onboarded)} -> {','.join(not_onboarded)}")
    print(f"built_ok: {len(built_ok)}  bear_ok: {len(bear_ok)}")
    print(f"bear_degraded (needs heal): {len(bear_bad)} -> {','.join(bear_bad)}")
    print("--- per-ticker ---")
    for t in tickers:
        o = ob.get(t, {})
        b = bd.get(t, {})
        print(
            f"{t:6s} onboard={o.get('status', '-'):7s} files={o.get('fmp_files', '-')!s:>3} "
            f"upto={o.get('fmp_data_upto') or '-':10s} | build={b.get('status', '-'):7s} "
            f"bear={b.get('bear_case', '-'):11s} att={b.get('attempts', '-')}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["onboard", "build", "heal", "all", "report"], default="all")
    ap.add_argument("--max-new", type=int, default=0, help="cap new tickers this run (0=all)")
    ap.add_argument("--only", default="", help="comma-separated subset of tickers")
    args = ap.parse_args()

    for path in (PY, ONBOARD, BUILD, DB):
        if not path.exists():
            print(f"[driver] FATAL missing required path: {path}", flush=True)
            return 2

    tickers = watchlist_tickers()
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        tickers = [t for t in tickers if t in wanted]

    p = load_progress()
    p.setdefault("onboard", {})
    p.setdefault("build", {})
    log_driver(f"sweep start phase={args.phase} tickers={len(tickers)} max_new={args.max_new}")

    if args.phase == "report":
        phase_report(tickers, p)
        return 0
    if args.phase in ("onboard", "all"):
        phase_onboard(tickers, p, args.max_new)
    if args.phase in ("build", "all"):
        phase_build(tickers, p, args.max_new)
    if args.phase in ("heal", "all"):
        phase_heal(tickers, p)

    write_status(p, f"SWEEP COMPLETE (phase={args.phase})")
    log_driver("sweep done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
