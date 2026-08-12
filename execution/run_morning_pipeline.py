"""Orchestrate the morning pipeline: news -> triggers -> feed -> validate.

The daily trigger driver (``run_triggers.py``) fires alerts and persists them;
the feed renderer (``build_alert_feed.py``) renders HTML from those persisted
alerts. On their own they are unchained: the driver runs at 04:00 via cron,
but the feed HTML is only rebuilt when manually invoked, so a 07:00 read shows
stale HTML. This orchestrator chains the stages into one scheduled run. (The
morning-digest render stage retired with the standalone /digest page,
2026-06-11 — the live Home rail serves that view straight from the DB.)

Twenty manifest-defined stages run in sequence as subprocess-isolated children.
The major phase boundaries are:

  0. news     -- ``fetch_news.py`` (ingest fresh per-ticker news into the
     ``news`` table so the material_news trigger has stories to classify;
     ``--news-source`` selects FMP / WebSearch+Opus / auto).
  0a. list_type -- ``sync_list_type_from_holdings.py --apply`` (sync
     tracked_companies.list_type to the tracker's holdings: held > $100 operating
     company => portfolio, unheld portfolio name => evaluation; runs before every
     downstream stage that reads the portfolio set; safe no-op on empty/absent
     holdings so a tracker outage never demotes the book).
  0b. decisions -- ``record_decisions.py`` (record memo verdicts into the
     ``decisions`` ledger + extract falsifiable "what would change my mind"
     conditions, so the decision_condition trigger evaluates fresh rows).
  0c. lifecycle -- ``sync_position_lifecycle.py`` (reconcile the
     position_entries entry/exit ledger against the portfolio list + tracker
     holdings, snapshotting the fresh stage-0b conditions at entry).
  1. triggers -- ``run_triggers.py`` (the long pole; fans LLM-backed sensors
     across the portfolio, cost-capped via ``--max-cost-usd``).
  2. feed     -- ``build_alert_feed.py``.
  3. validate -- ``run_validation_engine.py --gate`` (LAST so it never blocks a
     render): runs the population-level data checks and makes a HALT-severity
     result a failed stage, so egregious data lands in the pipeline exit code
     for monitoring. The standing machinery that *runs* the validation gate.

Resilience contract (the load-bearing behavior):

  * The orchestrator NEVER aborts early. A failed or timed-out stage is logged
    and the remaining stages still run. The feed is a read-only render over
    whatever alerts already exist, so a trigger failure must not leave the
    user staring at a stale feed -- the render runs regardless. Likewise a
    failed news fetch (stage 0) never blocks the trigger sweep: triggers run
    over whatever news already exists, degrading to none.
  * Each stage's stdout/stderr is captured and echoed under a stage header.
  * Exit code is the count of failed stages (0 = all good), reported only AFTER
    every non-skipped stage has been attempted. This lets cron / monitoring
    detect partial failure while still producing the best-effort feed.

Usage:
    python execution/run_morning_pipeline.py
    python execution/run_morning_pipeline.py --news-source websearch
    python execution/run_morning_pipeline.py --max-cost-usd 10 --skip-news
    python execution/run_morning_pipeline.py --user-id bhanu --db-path /tmp/x.db
    python execution/run_morning_pipeline.py --skip-triggers   # re-render only
    python execution/run_morning_pipeline.py --only stage_1_triggers
    python execution/run_morning_pipeline.py --from stage_0e_reprice

``--skip-triggers`` runs the feed render only -- useful for re-rendering after
manual approve/dismiss actions mutate the alert rows, without paying for
another trigger sweep (it skips stage 0 news too). ``--skip-news`` skips only
the news fetch.

This orchestrates the scripts via subprocess (process isolation, matching
the repo's drain-executor + daily-fetch-and-brief pattern) rather than importing
their ``main()``. Every repository child is built through the canonical managed
Python command helper, which preserves the parent interpreter and applies the
SQLite bootstrap before the target script starts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
# The repo root too, so the post-flight dead-man check can import
# ``execution.verify_daily_chain`` (a namespace package — ``execution/`` has no
# ``__init__.py``). Without it that import raises ModuleNotFoundError into a
# swallowing ``except``, and the artifact every external monitor keys off is
# silently never written.
sys.path.insert(0, str(PROJECT_ROOT))

from db_paths import configured_db_path  # noqa: E402
from llm import tracectx  # noqa: E402
from pipeline.morning_manifest import (  # noqa: E402
    STAGE_MANIFEST,
    STAGE_PREFLIGHT,
    ArgumentProfile,
    StageSpec,
    manifest_digest,
)
from pipeline.run_accounting import (  # noqa: E402
    PipelineRunSuppressedError,
    suppression_payload,
)
from runtime.python_process import managed_python_argv  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_USER_ID = os.environ.get("CIO_USER_ID", "bhanu")
DEFAULT_MAX_COST_USD = 10.0

# The typed manifest is the single source of truth for stage order and shape.
_ALL_STAGE_KEYS = tuple(spec.key for spec in STAGE_MANIFEST)

# News ingestion source for Stage 0. Default `auto` (self-healing): FMP first,
# falling back to WebSearch+Opus per ticker on refusal.
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


def _is_active(spec: StageSpec, args: argparse.Namespace) -> bool:
    """Return whether legacy skip flags permit a manifest stage."""

    return not any(bool(getattr(args, flag)) for flag in spec.skip_flags)


def _selection_closure(target: str) -> set[str]:
    """Return ``target`` and every transitive selection dependency."""

    by_key = {spec.key: spec for spec in STAGE_MANIFEST}
    selected: set[str] = set()

    def add(key: str) -> None:
        if key in selected:
            return
        selected.add(key)
        for dependency in by_key[key].selection_dependencies:
            add(dependency)

    add(target)
    return selected


def _selected_specs(args: argparse.Namespace) -> list[StageSpec]:
    """Apply activation metadata and focused selection to the manifest."""

    selected_keys = set(_ALL_STAGE_KEYS)
    if args.only is not None:
        selected_keys = _selection_closure(args.only)
    elif args.from_stage is not None:
        start = _ALL_STAGE_KEYS.index(args.from_stage)
        selected_keys = set(_ALL_STAGE_KEYS[start:])
        # Environment validation remains unconditional for every invocation.
        selected_keys.add(STAGE_PREFLIGHT)

    return [spec for spec in STAGE_MANIFEST if spec.key in selected_keys and _is_active(spec, args)]


def _expand_base_argv(spec: StageSpec, args: argparse.Namespace) -> list[str]:
    values = {
        "max_cost_usd": str(args.max_cost_usd),
        "news_source": str(args.news_source),
        "user_id": str(args.user_id),
    }
    return [token.format_map(values) for token in spec.base_argv]


def _dynamic_argv(spec: StageSpec, args: argparse.Namespace) -> list[str]:
    if args.db_path is None or spec.argument_profile is ArgumentProfile.NONE:
        return []
    if spec.argument_profile is ArgumentProfile.DB_PATH:
        return ["--db-path", str(args.db_path)]
    if spec.argument_profile is ArgumentProfile.DB:
        return ["--db", str(args.db_path)]
    if spec.argument_profile is ArgumentProfile.REPO_ROOT_FROM_DB:
        return ["--repo-root", str(args.db_path.parent.parent)]
    raise AssertionError(f"unsupported argument profile: {spec.argument_profile}")


def _build_stages(args: argparse.Namespace) -> list[_Stage]:
    """Resolve the typed manifest into ordered subprocess invocations."""

    exec_dir = PROJECT_ROOT / "execution"
    return [
        _Stage(
            key=spec.key,
            label=spec.label,
            argv=managed_python_argv(
                PROJECT_ROOT,
                exec_dir / spec.script,
                *_expand_base_argv(spec, args),
                *_dynamic_argv(spec, args),
                unbuffered=True,
            ),
            timeout_s=spec.timeout_s,
        )
        for spec in _selected_specs(args)
    ]


def _echo_captured_output(stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    """Write a subprocess's captured stdout/stderr to the parent's own streams.

    Shared by the normal-exit and timeout paths so a killed stage's partial
    output (whatever it flushed before the kill) is echoed exactly like a
    completed stage's would be -- see ``_run_stage``'s docstring. The stages
    run with ``text=True`` so both are ``str`` in practice; ``TimeoutExpired``
    types its attributes ``bytes | None``, so bytes are decoded defensively."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)


STATE_FILE = PROJECT_ROOT / ".tmp" / "morning_pipeline" / "state.json"
_CHECKPOINT_VERSION = 3


class CheckpointStateError(RuntimeError):
    """The resume checkpoint exists but cannot be trusted."""


def _parse_checkpoint(raw: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointStateError(f"invalid JSON in {STATE_FILE}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise CheckpointStateError(f"checkpoint must be a JSON object: {STATE_FILE}")
    return cast("dict[str, object]", decoded)


def _load_completed_stages(checkpoint_scope: str | None = None) -> set[str]:
    """Load matching completed stages from state.json if less than 18h old."""
    if not STATE_FILE.exists():
        return set()
    data = _parse_checkpoint(STATE_FILE.read_text(encoding="utf-8"))
    if data.get("version") != _CHECKPOINT_VERSION:
        return set()
    stored_scope = data.get("scope")
    if stored_scope is not None and not isinstance(stored_scope, str):
        raise CheckpointStateError(f"checkpoint scope must be a string or null: {STATE_FILE}")
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, int | float) or not math.isfinite(float(updated_at)):
        raise CheckpointStateError(f"checkpoint updated_at must be numeric: {STATE_FILE}")
    completed_stages_raw = data.get("completed_stages")
    if not isinstance(completed_stages_raw, list):
        raise CheckpointStateError(
            f"checkpoint completed_stages must be a list of strings: {STATE_FILE}"
        )
    completed_stage_values = cast("list[object]", completed_stages_raw)
    if not all(isinstance(stage_key, str) for stage_key in completed_stage_values):
        raise CheckpointStateError(
            f"checkpoint completed_stages must be a list of strings: {STATE_FILE}"
        )
    completed_stages = cast("list[str]", completed_stage_values)
    unknown = set(completed_stages).difference(_ALL_STAGE_KEYS)
    if unknown:
        raise CheckpointStateError(
            f"checkpoint contains unknown stage keys {sorted(unknown)}: {STATE_FILE}"
        )
    if checkpoint_scope is not None and stored_scope != checkpoint_scope:
        return set()
    if time.time() - updated_at > 18 * 3600:
        return set()
    return set(completed_stages)


def _record_completed_stage(stage_key: str, *, checkpoint_scope: str | None = None) -> None:
    """Atomically record a completed stage key to state.json."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    completed = _load_completed_stages(checkpoint_scope)
    completed.add(stage_key)
    payload = json.dumps(
        {
            "completed_stages": sorted(completed),
            "scope": checkpoint_scope,
            "updated_at": time.time(),
            "version": _CHECKPOINT_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    temporary = STATE_FILE.with_name(f".{STATE_FILE.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as checkpoint_file:
            checkpoint_file.write(payload)
            checkpoint_file.flush()
            os.fsync(checkpoint_file.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def _clear_completed_stages() -> None:
    """Remove a consumed checkpoint after a fully successful attempt."""
    STATE_FILE.unlink(missing_ok=True)


def _run_stage(stage: _Stage, *, checkpoint_scope: str | None = None) -> _StageResult:
    """Invoke one stage as a subprocess; echo its output under a header.

    Child-process failures never raise. ``subprocess.run`` is called with ``check=False`` so a
    non-zero child exit returns a CompletedProcess rather than raising; the
    only child-runtime exceptions left are ``TimeoutExpired`` and ``OSError``
    (spawn failure), both caught and turned into a failed ``_StageResult``.
    This is what guarantees the caller's loop never aborts early. A checkpoint
    persistence error converts the otherwise-successful stage to a loud failure
    so later stages still run without claiming progress that was not durable.

    ``capture_output=True`` buffers the child's stdout/stderr entirely in
    memory and only returns it on a *normal* exit. A killed-on-timeout child's
    ``TimeoutExpired`` exception carries whatever was captured before the kill
    on its own ``.stdout`` / ``.stderr`` attributes (Python drains the pipes
    before raising) -- this is echoed on a timeout too, so a future hang shows
    the last progress line(s) the child managed to flush (e.g. reprice's
    per-ticker ``reprice_ticker_done`` events, or fetch_news's per-ticker
    events) instead of a completely empty stage section in the cron log.
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
            # P1 trace context (llm.tracectx): stages are SUBPROCESSES, so an
            # in-process contextvar cannot reach them — the trace is propagated
            # through the environment (the same mechanism OTel uses across
            # process boundaries). Every llm_calls row the child writes is then
            # attributable to this stage, turning "which stage burns the
            # morning's tokens?" from ~15 hand-written queries into one GROUP BY.
            env=tracectx.child_env(stage_name=f"morning_pipeline.{stage.key}"),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - t0, 3)
        _echo_captured_output(exc.stdout, exc.stderr)
        return _fail(stage, f"timed out after {stage.timeout_s}s", elapsed)
    except OSError as exc:
        elapsed = round(time.monotonic() - t0, 3)
        return _fail(stage, f"spawn failed: {exc}", elapsed)

    elapsed = round(time.monotonic() - t0, 3)
    _echo_captured_output(proc.stdout, proc.stderr)

    if proc.returncode == 0:
        try:
            _record_completed_stage(stage.key, checkpoint_scope=checkpoint_scope)
        except (CheckpointStateError, OSError) as exc:
            return _fail(
                stage,
                f"checkpoint persistence failed: {type(exc).__name__}: {exc}",
                elapsed,
            )
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

    Stage 1's failure is deliberately tolerated -- the feed still renders --
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

    A stage that never ran (via the ``--skip-*`` flags) is reported as
    "skipped" so the summary always carries every stage key.
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
        help=f"Owner of the alerts / feed rows. Default: "
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
        help="Skip stage 1 (triggers) and run only the feed render. "
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
    parser.add_argument(
        "--skip-standup",
        action="store_true",
        help="Skip stage 1b (the proactive analyst standup). The standup composes "
        "an eval-gated, rate-limited advisory brief per surviving trip through the "
        "Ask engine — skip it to run the trigger sweep without the paid standup leg.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip stage 4 (the data-validation gate). The gate runs the "
        "validation engine and makes a HALT-severity result a failed stage "
        "(non-zero pipeline exit) so monitoring catches egregious data; skip it "
        "to run the pipeline without the data check.",
    )
    parser.add_argument(
        "--skip-pre-earnings-briefs",
        action="store_true",
        help="Skip stage 1c (pre-earnings brief generation). The stage is "
        "already a no-op outside each name's 7-day pre-ER window and is "
        "idempotent inside it; skip it to run a brief-free pipeline.",
    )
    parser.add_argument(
        "--skip-post-earnings-readouts",
        action="store_true",
        help="Skip stage 1d (portfolio-only persisted post-earnings readouts).",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--only",
        choices=_ALL_STAGE_KEYS,
        help="Run one stage and its transitive selection dependencies, in order.",
    )
    selection.add_argument(
        "--from",
        dest="from_stage",
        choices=_ALL_STAGE_KEYS,
        help="Run the manifest suffix beginning at this stage (plus preflight).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Supersede an active attempt and ignore the 18-hour resume checkpoint.",
    )
    return parser.parse_args(argv)


def _record_run(
    db_path: Path,
    *,
    start: bool,
    run_id: str | None = None,
    failed: bool = False,
    error_summary: str | None = None,
    invocation_inputs: dict[str, str | float | bool] | None = None,
    force: bool = False,
) -> str | None:
    """Wrap start_run / end_run so run accounting failures never crash the pipeline."""
    try:
        from models.runs import StageStatus as RunStatus
        from pipeline.run_accounting import end_run, start_run

        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        try:
            if start:
                return start_run(
                    conn,
                    directive="run_morning_pipeline",
                    ticker_scope=[],
                    invocation_inputs=invocation_inputs,
                    force=force,
                    deduplicate_completed=True,
                )
            if run_id is not None:
                status = RunStatus.FAILED if failed else RunStatus.OK
                end_run(conn, run_id, status, error_summary)
        finally:
            conn.close()
    except PipelineRunSuppressedError:
        raise
    except Exception as exc:
        sys.stderr.write(f"WARNING: run_accounting failed: {exc}\n")
    return None


def _checkpoint_scope(
    args: argparse.Namespace,
    *,
    db_path: Path,
    stages: list[_Stage],
    run_date: date,
) -> str:
    """Bind resume state to one calendar run and exact manifest contract."""
    return json.dumps(
        {
            "db_path": str(db_path.resolve()),
            "manifest_digest": manifest_digest(STAGE_MANIFEST),
            "max_cost_usd": args.max_cost_usd,
            "news_source": args.news_source,
            "run_date": run_date.isoformat(),
            "stages": [stage.key for stage in stages],
            "user_id": args.user_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    t0 = time.monotonic()

    # Resolve DB path early — needed for run accounting.
    configured_path = configured_db_path(PROJECT_ROOT)
    db_path = args.db_path.expanduser().resolve() if args.db_path is not None else configured_path
    args.db_path = db_path
    return _run_pipeline(args, db_path=db_path, t0=t0)


def _run_pipeline(args: argparse.Namespace, *, db_path: Path, t0: float) -> int:
    """Run stages without holding a coarse database mutex across child work."""
    run_date = date.today()
    # Record the pipeline start in ingestion_runs so the dead-man post-flight
    # (verify_daily_chain.py) can confirm it ran today.
    run_id: str | None = None
    if db_path.exists():
        try:
            run_id = _record_run(
                db_path,
                start=True,
                invocation_inputs={
                    "max_cost_usd": args.max_cost_usd,
                    "news_source": args.news_source,
                    "only": args.only or "",
                    "from_stage": args.from_stage or "",
                    "run_date": run_date.isoformat(),
                    "skip_news": args.skip_news,
                    "skip_standup": args.skip_standup,
                    "skip_triggers": args.skip_triggers,
                    "skip_validation": args.skip_validation,
                    "user_id": args.user_id,
                },
                force=args.force,
            )
        except PipelineRunSuppressedError as exc:
            print(json.dumps(suppression_payload(exc)))
            return 0

    stages = _build_stages(args)
    checkpoint_scope = _checkpoint_scope(
        args,
        db_path=db_path,
        stages=stages,
        run_date=run_date,
    )
    completed: set[str] = set() if args.force else _load_completed_stages(checkpoint_scope)
    runnable = [stage for stage in stages if stage.key not in completed]

    # Always-attempt contract: iterate every built stage with no early return.
    # _run_stage never raises, so a failure / timeout in one stage cannot stop
    # the next from running.
    results = [_run_stage(stage, checkpoint_scope=checkpoint_scope) for stage in runnable]

    summary = _summarize(results, elapsed_seconds=round(time.monotonic() - t0, 3))
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")

    failed_count = sum(1 for r in results if r.status is StageStatus.FAILED)
    if failed_count == 0:
        _clear_completed_stages()

    # Close the ingestion_runs row so the dead-man sees a terminal status today.
    if run_id is not None and db_path.exists():
        failed_names = [r.key for r in results if r.status is StageStatus.FAILED]
        err = f"failed stages: {', '.join(failed_names)}" if failed_names else None
        _record_run(
            db_path, start=False, run_id=run_id, failed=bool(failed_count), error_summary=err
        )

    # Post-flight dead-man check: write .tmp/daily_chain_status.json with
    # today's pipeline verdict so the cron_health panel and external monitors
    # can confirm the chain ran (even when the pipeline had partial failures).
    # Runs AFTER end_run so ingestion_runs reflects the terminal status.
    try:
        from execution.verify_daily_chain import main as _vdc_main

        _vdc_main(["--quiet", "--db-path", str(db_path)])
    except Exception as exc:
        # Deliberately non-fatal: a monitoring artifact must not fail the run
        # that produced good data. But it gets the same "!!!" marker a failed
        # stage does — the previous bare WARNING let a permanently broken
        # import (and a never-written artifact) read as routine log noise.
        sys.stderr.write(
            f"\n!!! [post_flight_verify_daily_chain] FAILED - {type(exc).__name__}: {exc}\n"
        )

    # Exit code = number of failed stages (skipped stages are not failures and
    # were never added to `results`). Reported only after all stages ran.
    return failed_count


if __name__ == "__main__":
    sys.exit(main())
