"""Orchestrate the morning dashboard pipeline: news -> triggers -> digest -> feed.

The daily trigger driver (``run_triggers.py``) fires alerts and persists them;
the dashboard renderers (``build_morning_digest.py`` + ``build_alert_feed.py``)
render HTML from those persisted alerts. On their own they are unchained: the
driver runs at 04:00 via cron, but the digest HTML is only rebuilt when
manually invoked, so a 07:00 read shows stale HTML. This orchestrator chains
the stages into one scheduled run.

Four stages run in sequence as subprocess-isolated children:

  0. news     -- ``fetch_news.py`` (ingest fresh per-ticker news into the
     ``news`` table so the material_news trigger has stories to classify;
     ``--news-source`` selects FMP / WebSearch+Opus / auto).
  1. triggers -- ``run_triggers.py`` (the long pole; fans LLM-backed sensors
     across the portfolio, cost-capped via ``--max-cost-usd``).
  2. digest   -- ``build_morning_digest.py`` for today.
  3. feed     -- ``build_alert_feed.py``.

Resilience contract (the load-bearing behavior):

  * The orchestrator NEVER aborts early. A failed or timed-out stage is logged
    and the remaining stages still run. The digest + feed are read-only renders
    over whatever alerts already exist, so a trigger failure must not leave the
    user staring at a stale digest -- the renders run regardless. Likewise a
    failed news fetch (stage 0) never blocks the trigger sweep: triggers run
    over whatever news already exists, degrading to none.
  * Each stage's stdout/stderr is captured and echoed under a stage header.
  * Exit code is the count of failed stages (0 = all good), reported only AFTER
    every non-skipped stage has been attempted. This lets cron / monitoring
    detect partial failure while still producing the best-effort digest.

Usage:
    python execution/run_morning_pipeline.py
    python execution/run_morning_pipeline.py --news-source websearch
    python execution/run_morning_pipeline.py --max-cost-usd 10 --skip-news
    python execution/run_morning_pipeline.py --user-id bhanu --db-path /tmp/x.db
    python execution/run_morning_pipeline.py --skip-triggers   # re-render only

``--skip-triggers`` runs stages 2 + 3 only -- useful for re-rendering the
digest/feed after manual approve/dismiss actions mutate the alert rows, without
paying for another trigger sweep (it skips stage 0 news too). ``--skip-news``
skips only the news fetch.

This orchestrates the scripts via subprocess (process isolation, matching
the repo's drain-executor + daily-fetch-and-brief pattern) rather than importing
their ``main()``. The child interpreter is ``sys.executable`` -- the exact
interpreter running this orchestrator -- so the children never depend on PATH
resolution differing from the parent's.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_USER_ID = os.environ.get("CIO_USER_ID", "bhanu")
DEFAULT_MAX_COST_USD = 10.0

# Per-stage wall-clock caps. Stage 1 fans LLM-backed sensors across the whole
# portfolio and is the long pole -- 30 min mirrors the slice-1 guidance. The
# two render stages are pure SQLite reads + HTML writes; 5 min is generous.
_TRIGGERS_TIMEOUT_S = 1800
_RENDER_TIMEOUT_S = 300
# Stage 0 (news) is fast on the FMP path, but an `auto`/`websearch` run can make
# one Opus web call per fallback ticker, so it gets more headroom than a render.
_NEWS_TIMEOUT_S = 600

# Canonical stage keys, in run order. Used to build the final summary so a
# skipped stage still appears (as "skipped") even though it never ran.
STAGE_NEWS = "stage_0_news"
STAGE_TRIGGERS = "stage_1_triggers"
STAGE_DIGEST = "stage_2_digest"
STAGE_FEED = "stage_3_feed"
_ALL_STAGE_KEYS = (STAGE_NEWS, STAGE_TRIGGERS, STAGE_DIGEST, STAGE_FEED)

# News ingestion source for Stage 0. Default `auto` (self-healing): FMP first,
# falling back to WebSearch+Opus per ticker on refusal — so the material_news
# trigger keeps eating regardless of FMP's free-tier news policy. (The plan's
# one-shot probe couldn't determine that policy with no FMP key configured.)
DEFAULT_NEWS_SOURCE = "auto"
NEWS_SOURCES = ("fmp", "websearch", "auto")


class StageStatus(StrEnum):
    """Outcome of a single pipeline stage."""

    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class _Stage:
    """One pipeline stage: a labeled, time-bounded subprocess invocation."""

    key: str
    label: str
    argv: list[str]
    timeout_s: int


@dataclass(slots=True)
class _StageResult:
    """Outcome of running one ``_Stage``.

    ``exit_code`` is None when the child never produced one (timeout or spawn
    failure); ``error`` carries the human-readable reason in that case.
    """

    key: str
    status: StageStatus
    exit_code: int | None
    elapsed_seconds: float
    error: str | None = None


def _stage_args_for(args: argparse.Namespace, *, include_max_cost: bool) -> list[str]:
    """Common pass-through args shared by every stage.

    ``--user-id`` and ``--db-path`` go to all three scripts; ``--max-cost-usd``
    is meaningful only to the trigger stage (the renderers have no such flag).
    ``--db-path`` is omitted when unset so each script applies its own default
    DB resolution rather than receiving a literal ``None``.
    """
    passthrough: list[str] = ["--user-id", args.user_id]
    if include_max_cost:
        passthrough += ["--max-cost-usd", str(args.max_cost_usd)]
    if args.db_path is not None:
        passthrough += ["--db-path", str(args.db_path)]
    return passthrough


def _build_stages(args: argparse.Namespace) -> list[_Stage]:
    """Construct the ordered stage list from CLI args.

    Stage 1 is omitted entirely when ``--skip-triggers`` is set; the digest +
    feed stages always run.
    """
    py = sys.executable
    exec_dir = PROJECT_ROOT / "execution"
    stages: list[_Stage] = []

    # Stage 0 -- news fetch, BEFORE triggers so the morning's fresh news is
    # classified in the same run. Skipped by --skip-news, and implicitly by
    # --skip-triggers (the re-render-only path: no point fetching news that won't
    # be classified). The news fetcher takes neither --user-id nor --max-cost-usd,
    # so it does not use _stage_args_for; only --db-path is forwarded (when set).
    if not args.skip_triggers and not args.skip_news:
        db_path_args = ["--db-path", str(args.db_path)] if args.db_path is not None else []
        stages.append(
            _Stage(
                key=STAGE_NEWS,
                label="Stage 0 - news fetch (fetch_news.py)",
                argv=[
                    py,
                    str(exec_dir / "fetch_news.py"),
                    "--source",
                    args.news_source,
                    *db_path_args,
                ],
                timeout_s=_NEWS_TIMEOUT_S,
            )
        )

    if not args.skip_triggers:
        stages.append(
            _Stage(
                key=STAGE_TRIGGERS,
                label="Stage 1 - triggers (run_triggers.py)",
                argv=[
                    py,
                    str(exec_dir / "run_triggers.py"),
                    *_stage_args_for(args, include_max_cost=True),
                ],
                timeout_s=_TRIGGERS_TIMEOUT_S,
            )
        )

    stages.append(
        _Stage(
            key=STAGE_DIGEST,
            label="Stage 2 - morning digest (build_morning_digest.py)",
            argv=[
                py,
                str(exec_dir / "build_morning_digest.py"),
                *_stage_args_for(args, include_max_cost=False),
            ],
            timeout_s=_RENDER_TIMEOUT_S,
        )
    )
    stages.append(
        _Stage(
            key=STAGE_FEED,
            label="Stage 3 - alert feed (build_alert_feed.py)",
            argv=[
                py,
                str(exec_dir / "build_alert_feed.py"),
                *_stage_args_for(args, include_max_cost=False),
            ],
            timeout_s=_RENDER_TIMEOUT_S,
        )
    )
    return stages


def _run_stage(stage: _Stage) -> _StageResult:
    """Invoke one stage as a subprocess; echo its output under a header.

    Never raises. ``subprocess.run`` is called with ``check=False`` so a
    non-zero child exit returns a CompletedProcess rather than raising; the
    only runtime exceptions left are ``TimeoutExpired`` and ``OSError``
    (spawn failure), both caught and turned into a failed ``_StageResult``.
    This is what guarantees the caller's loop never aborts early.
    """
    sys.stdout.write(f"\n{'=' * 72}\n=== {stage.label}\n{'=' * 72}\n")
    sys.stdout.flush()

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            stage.argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=stage.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t0, 3)
        return _fail(stage, f"timed out after {stage.timeout_s}s", elapsed)
    except OSError as exc:
        elapsed = round(time.monotonic() - t0, 3)
        return _fail(stage, f"spawn failed: {exc}", elapsed)

    elapsed = round(time.monotonic() - t0, 3)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode == 0:
        sys.stdout.write(f"[{stage.key}] OK (exit 0, {elapsed}s)\n")
        return _StageResult(
            key=stage.key,
            status=StageStatus.OK,
            exit_code=0,
            elapsed_seconds=elapsed,
        )
    return _fail(
        stage,
        f"exited {proc.returncode}",
        elapsed,
        exit_code=proc.returncode,
    )


def _fail(
    stage: _Stage,
    reason: str,
    elapsed: float,
    *,
    exit_code: int | None = None,
) -> _StageResult:
    """Emit a prominent failure banner (stderr) and a status line (stdout).

    Stage 1's failure is deliberately tolerated -- the digest/feed still run --
    so it must be loud in the cron log rather than buried in the child's
    captured output.
    """
    sys.stderr.write(f"\n!!! [{stage.key}] FAILED - {reason}\n")
    sys.stdout.write(f"[{stage.key}] FAILED - {reason} ({elapsed}s)\n")
    return _StageResult(
        key=stage.key,
        status=StageStatus.FAILED,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=reason,
    )


def _summarize(results: list[_StageResult], *, elapsed_seconds: float) -> dict[str, object]:
    """Build the final summary dict keyed by the canonical stage keys.

    A stage that never ran (only stage 1, via ``--skip-triggers``) is reported
    as "skipped" so the summary always carries all three keys.
    """
    by_key = {r.key: r.status.value for r in results}
    summary: dict[str, object] = {
        key: by_key.get(key, StageStatus.SKIPPED.value) for key in _ALL_STAGE_KEYS
    }
    summary["elapsed_seconds"] = elapsed_seconds
    return summary


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_MAX_COST_USD,
        help=f"Per-run LLM cost cap (USD) passed to the trigger stage "
        f"(default {DEFAULT_MAX_COST_USD}). Ignored by the render stages.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help=f"Owner of the alerts / digest / feed rows. Default: "
        f"{DEFAULT_USER_ID!r}. Passed through to every stage.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path for every stage. When unset, each "
        "script applies its own default (data/portfolio.db under the repo root).",
    )
    parser.add_argument(
        "--skip-triggers",
        action="store_true",
        help="Skip stage 1 (triggers) and run only the digest + feed renders. "
        "Useful for re-rendering after manual approve/dismiss actions. Also "
        "skips stage 0 (news), since there is nothing to classify.",
    )
    parser.add_argument(
        "--news-source",
        choices=NEWS_SOURCES,
        default=DEFAULT_NEWS_SOURCE,
        help=f"Stage 0 news source (default: {DEFAULT_NEWS_SOURCE!r}). 'auto' runs "
        f"FMP and falls back to WebSearch+Opus per ticker on refusal; 'websearch' "
        f"once FMP's news is cut off; 'fmp' to disable the LLM fallback.",
    )
    parser.add_argument(
        "--skip-news",
        action="store_true",
        help="Skip stage 0 (news fetch) only — triggers still run over whatever "
        "news rows already exist.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    t0 = time.monotonic()

    stages = _build_stages(args)

    # Always-attempt contract: iterate every built stage with no early return.
    # _run_stage never raises, so a failure / timeout in one stage cannot stop
    # the next from running.
    results: list[_StageResult] = [_run_stage(stage) for stage in stages]

    summary = _summarize(results, elapsed_seconds=round(time.monotonic() - t0, 3))
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")

    # Exit code = number of failed stages (skipped stages are not failures and
    # were never added to `results`). Reported only after all stages ran.
    return sum(1 for r in results if r.status is StageStatus.FAILED)


if __name__ == "__main__":
    sys.exit(main())
