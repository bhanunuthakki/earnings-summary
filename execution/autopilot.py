"""
execution/autopilot.py
----------------------
End-to-end portfolio refresh. Designed to be the single command Windows Task
Scheduler invokes monthly (and the single command a human runs on demand).

Stages, in order:
  1. PULL    — `check_quarterly_releases.py` against portfolio + watchlist
                Aggregator chain (free) writes any newly-published quarter into
                `transcripts/raw/`.
  2. PROCESS — `src/main.py` auto-discovers everything in raw/processed and
                produces per-quarter LLM summaries + pairwise SayDo + master
                PDFs. Idempotent — already-cached entries are skipped.
  3. TRACK   — `update_thesis_tracker.py --all` refreshes
                `micro_thesis/thesis-tracker-<TICKER>-<DATE>.md` for each
                holding that has a `holdings/<TICKER>.json` schema.
  4. MEMO    — `generate_memo.py --portfolio` writes one styled HTML memo
                per portfolio name (NOT watchlist) into `transcripts/memos/`.
                Uses transcript summaries + SayDo + thesis + tracker + IR-doc
                listing + FMP news (last 14 days) — degrades gracefully when
                any of those are absent (so AMZN, with no thesis JSON yet,
                still gets a meaningful memo).

Each stage is a subprocess so a failure in one doesn't poison the next stage's
Python interpreter state. Stage stdout/stderr is tee'd into per-stage logs and
a single `RunReport` JSON summarising what was attempted, what succeeded, and
what to investigate.

CLI:
  python execution/autopilot.py                  # full chain
  python execution/autopilot.py --skip-pull      # skip PULL stage
  python execution/autopilot.py --only memo      # run a single stage
  python execution/autopilot.py --dry-run        # print plan, do nothing
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CRON_LOG_DIR = PROJECT_ROOT / ".tmp" / "cron_logs"
CRON_RUN_DIR = PROJECT_ROOT / ".tmp" / "cron_runs"


class StageStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StageResult:
    name: str
    status: StageStatus
    started_at: str
    completed_at: str
    exit_code: int | None
    log_path: str | None
    note: str = ""


@dataclass
class RunReport:
    run_id: str
    started_at: str
    completed_at: str
    args: dict
    stages: list[StageResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagePlan:
    name: str
    description: str
    cmd: list[str]


def _build_stages(args: argparse.Namespace) -> list[StagePlan]:
    pull_cmd = [
        sys.executable, "-u", str(SCRIPT_DIR / "check_quarterly_releases.py"),
        "--include-watchlist",
        "--days", str(args.pull_days),
        "--limit-quarters", str(args.pull_limit_quarters),
    ]
    if args.with_audio_fallback:
        pull_cmd.append("--with-audio-fallback")

    process_cmd = [sys.executable, "-u", str(PROJECT_ROOT / "src" / "main.py")]

    track_cmd = [sys.executable, "-u", str(SCRIPT_DIR / "update_thesis_tracker.py"), "--all"]

    memo_cmd = [
        sys.executable, "-u", str(SCRIPT_DIR / "generate_memo.py"),
        "--portfolio",
        "--news-days", str(args.memo_news_days),
    ]

    consolidate_cmd = [sys.executable, "-u", str(SCRIPT_DIR / "consolidate_outputs.py")]

    return [
        StagePlan("pull",        "Aggregator-chain pull of any new portfolio + watchlist quarters", pull_cmd),
        StagePlan("process",     "main.py: per-quarter summaries + SayDo + master PDFs",            process_cmd),
        StagePlan("track",       "update_thesis_tracker.py --all (10 holdings with JSON schema)",   track_cmd),
        StagePlan("memo",        "generate_memo.py --portfolio (HTML memos for 11 names, Claude web search)", memo_cmd),
        StagePlan("consolidate", "consolidate_outputs.py: outputs/<TICKER>/ + index.html + DB register",       consolidate_cmd),
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _run_stage(stage: StagePlan, run_id: str, env: dict[str, str]) -> StageResult:
    started = _dt.datetime.now(_dt.UTC)
    log_path = CRON_LOG_DIR / f"{run_id}__{stage.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[{stage.name}] start ({stage.description})", flush=True)
    print(f"[{stage.name}] cmd: {' '.join(stage.cmd)}", flush=True)
    print(f"[{stage.name}] log: {log_path.relative_to(PROJECT_ROOT)}", flush=True)

    try:
        with log_path.open("w", encoding="utf-8") as logf:
            proc = subprocess.run(
                stage.cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=18 * 60 * 60,  # 18h per stage cap
            )
        completed = _dt.datetime.now(_dt.UTC)
        status = StageStatus.OK if proc.returncode == 0 else StageStatus.FAILED
        print(f"[{stage.name}] end exit={proc.returncode} elapsed={(completed - started).total_seconds():.0f}s", flush=True)
        return StageResult(
            name=stage.name, status=status,
            started_at=started.isoformat(), completed_at=completed.isoformat(),
            exit_code=proc.returncode, log_path=str(log_path.relative_to(PROJECT_ROOT)),
        )
    except subprocess.TimeoutExpired:
        completed = _dt.datetime.now(_dt.UTC)
        return StageResult(
            name=stage.name, status=StageStatus.FAILED,
            started_at=started.isoformat(), completed_at=completed.isoformat(),
            exit_code=None, log_path=str(log_path.relative_to(PROJECT_ROOT)),
            note="timeout (>18h)",
        )
    except Exception as e:
        completed = _dt.datetime.now(_dt.UTC)
        return StageResult(
            name=stage.name, status=StageStatus.FAILED,
            started_at=started.isoformat(), completed_at=completed.isoformat(),
            exit_code=None, log_path=str(log_path.relative_to(PROJECT_ROOT)),
            note=f"{type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end portfolio + watchlist refresh autopilot.")
    parser.add_argument("--pull-days", type=int, default=45,
                        help="check_quarterly_releases.py --days (default 45)")
    parser.add_argument("--pull-limit-quarters", type=int, default=4,
                        help="check_quarterly_releases.py --limit-quarters (default 4)")
    parser.add_argument("--memo-news-days", type=int, default=14,
                        help="generate_memo.py --news-days (default 14)")
    parser.add_argument("--with-audio-fallback", action="store_true",
                        help="Pass --with-audio-fallback to PULL stage (off by default)")
    parser.add_argument("--skip-pull", action="store_true", help="Skip PULL stage")
    parser.add_argument("--skip-process", action="store_true", help="Skip PROCESS stage")
    parser.add_argument("--skip-track", action="store_true", help="Skip TRACK stage")
    parser.add_argument("--skip-memo", action="store_true", help="Skip MEMO stage")
    parser.add_argument("--skip-consolidate", action="store_true", help="Skip CONSOLIDATE stage")
    parser.add_argument("--only", choices=["pull", "process", "track", "memo", "consolidate"],
                        help="Run only one stage (mutually exclusive with --skip-*)")
    parser.add_argument("--dry-run", action="store_true", help="Print stage plan, run nothing")
    args = parser.parse_args()

    started = _dt.datetime.now(_dt.UTC)
    run_id = f"autopilot_{started.strftime('%Y%m%dT%H%M%SZ')}"
    print(f"[{run_id}] starting", flush=True)

    stages = _build_stages(args)
    if args.only:
        stages = [s for s in stages if s.name == args.only]
    skip = {"pull": args.skip_pull, "process": args.skip_process,
            "track": args.skip_track, "memo": args.skip_memo,
            "consolidate": args.skip_consolidate}

    if args.dry_run:
        print(f"[{run_id}] dry-run plan:")
        for s in stages:
            mark = "skip" if skip.get(s.name) else "run "
            print(f"  [{mark}] {s.name}: {s.description}")
            print(f"           {' '.join(s.cmd)}")
        return 0

    env = os.environ.copy()
    # Ensure Python sees the project src/ on the import path for any subprocess
    # that doesn't already adjust sys.path itself.
    env["PYTHONPATH"] = (str(PROJECT_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")).strip(os.pathsep)

    report = RunReport(
        run_id=run_id,
        started_at=started.isoformat(),
        completed_at="",
        args=vars(args),
    )

    for stage in stages:
        if skip.get(stage.name):
            report.stages.append(StageResult(
                name=stage.name, status=StageStatus.SKIPPED,
                started_at=_dt.datetime.now(_dt.UTC).isoformat(),
                completed_at=_dt.datetime.now(_dt.UTC).isoformat(),
                exit_code=None, log_path=None, note="skipped via --skip-*",
            ))
            print(f"[{stage.name}] skipped via --skip-{stage.name}", flush=True)
            continue
        result = _run_stage(stage, run_id, env)
        report.stages.append(result)
        # Halt the chain if a stage hard-fails — downstream stages depend on the
        # earlier ones and would just report the same gap.
        if result.status == StageStatus.FAILED:
            print(f"[{stage.name}] HARD FAIL — halting chain. Investigate {result.log_path}.", flush=True)
            break

    report.completed_at = _dt.datetime.now(_dt.UTC).isoformat()
    CRON_RUN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CRON_RUN_DIR / f"{run_id}.json"
    out_path.write_text(json.dumps(report.__dict__, indent=2, default=lambda o: o.__dict__), encoding="utf-8")

    summary = " ".join(f"{s.name}={s.status.value}" for s in report.stages)
    print(f"[{run_id}] done. {summary}", flush=True)
    print(f"[{run_id}] report: {out_path.relative_to(PROJECT_ROOT)}", flush=True)
    failed = sum(1 for s in report.stages if s.status == StageStatus.FAILED)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
