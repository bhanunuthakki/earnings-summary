"""Run capture retention and the calibration graders on the existing weekly window.

Six rungs score LLM outputs and write ``prompt_calibration_scores`` rows, each
tagged with the central prompt-version registry
(``src/llm/prompt_versions.py``) so the dashboard's A/B view has a version
dimension. Three OUTCOME graders over matured outputs vs realized data:

  * ``grade_predictions.py`` -- management-prediction EXTRACTION quality: the
    fraction of due predictions well-formed enough to grade against realized
    ``kpi_facts`` (deterministic; no LLM).
  * ``grade_decisions.py``   -- decision-audit outcomes vs realized price moves.
  * ``grade_bear_cases.py``  -- bear-hypothesis materialization.

Plus seven QUALITY rungs (llm_evals_plan §3 PR 3) — ``run_llm_evals.py``
rubric audits over the week's fresh artifacts (``--since-days``):
``bear_case``, ``transcript_summary``, ``advisor_next_dollar``,
``ask_advisory_answer`` (the conversational ask path's prose answers from
``ask_turns`` — close_the_loops L3), and ``calibration_coach`` (the monthly
scorecard prose — close_the_loops L8), plus capture audits for
``material_news_classification`` and ``earnings_tone_diff``. Each also writes an ``eval_runs`` row with
per-case judge evidence; a week with no fresh artifacts is a clean no-op.

A final rung (tenet-2 Phase 4, docs/design/tenet2_advisory_program.md §3.3:
"behavioral: after each grading batch") runs LAST, AFTER every grader above has
had a chance to write fresh ``outcome_label``/``process_quality`` rows: the
behavioral-rules distiller (``execution/run_behavior_distill.py`` ->
``synthesis.behavior_distill.run_behavior_distill``) re-derives candidate
behavioral ``owner_profile_facts`` from the now-freshly-graded corpus. Its own
internals already implement the per-item degrade pattern (a transient LLM
failure defers the whole distill run + tallies it, retried for free next week;
a hard stop -- LLMBudgetExceeded / LLMSetupError -- propagates as a non-zero
exit, which this orchestrator's always-attempt/never-raise ``_run_grader``
wrapper reports as FAILED rather than crashing the batch), mirroring the
post-#814 ``attach_conditions`` reference pattern. Window registered in
``directives/llm_quota_scheduling.md`` (runs inside this job's existing Sun
10:30 America/Los_Angeles window -- the daytime slot the weekly eval rung
was re-registered to on 2026-07-13 after the 03:30 registry-drift incident,
well clear of the 03:00-05:00 PT protected band).

These were manual CLIs that nothing ran, so ``prompt_calibration_scores`` stayed
empty even though the machinery was correct (v6 re-grade, LLM pass-through: "the
loop has never produced a score"). This orchestrator runs all of them on a
schedule (``cron/grade_calibration.task.xml``, weekly) so the loop is fed
without a human remembering to run each tool.

Resilience contract (mirrors ``run_morning_pipeline``): every grader is
attempted even when an earlier one fails or times out; each child's output is
echoed under a header; the process exit code is the count of FAILED graders
(0 = all good), so cron / monitoring can detect a partial failure. The graders
resolve their own DB (``data/portfolio.db`` under the repo root) and are run with
``cwd`` = the repo root, so no per-grader DB plumbing is needed here.

Usage:
    python execution/run_calibration_grading.py
    python execution/run_calibration_grading.py --skip bear_cases
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.python_process import managed_python_prefix  # noqa: E402

# The deterministic graders are quick SQLite sweeps; bear_cases calls an LLM
# judge per due hypothesis, so it gets the long pole's headroom.
_FAST_TIMEOUT_S = 600
_BEAR_TIMEOUT_S = 1800


class StageStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class _Grader:
    """One grader: a stable key + the script to run (+ args) + a wall-clock cap."""

    key: str
    script: str
    timeout_s: int
    args: tuple[str, ...] = ()


# How far back the weekly eval-audit rungs look. One day of overlap over a
# weekly cron so an artifact written while last week's run was in flight
# isn't skipped forever.
_EVAL_AUDIT_SINCE_DAYS = "8"

# Run order: the two deterministic graders first (cheap, no external calls),
# then the LLM-backed bear grader, then the three rubric-audit eval rungs
# (llm_evals_plan §3 PR 3: audit-mode judging of the week's fresh artifacts —
# Haiku judge per artifact, eval_judge budget; an empty week exits 0 without
# writing a run).
_GRADERS: tuple[_Grader, ...] = (
    # Provider-free retention sweep runs even when capture traffic is quiet.
    _Grader("capture_retention", "prune_llm_capture.py", _FAST_TIMEOUT_S),
    _Grader("predictions", "grade_predictions.py", _FAST_TIMEOUT_S),
    _Grader("decisions", "grade_decisions.py", _FAST_TIMEOUT_S),
    # grade_bear_cases.py has a `required=True` mutually-exclusive scope group
    # (--ticker | --all-portfolio); invoking it with no scope exits 2 (argparse)
    # before grading anything, which silently failed this rung on every weekly
    # run. --all-portfolio grades every portfolio holding's matured hypotheses.
    _Grader("bear_cases", "grade_bear_cases.py", _BEAR_TIMEOUT_S, ("--all-portfolio",)),
    _Grader(
        "eval_bear_case",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "bear_case", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    _Grader(
        "eval_transcript_summary",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "transcript_summary", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    _Grader(
        "eval_advisor_next_dollar",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "advisor_next_dollar", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    _Grader(
        "eval_ask_advisory_answer",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "ask_advisory_answer", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    # Calibration coach scorecards (close_the_loops L8): audits the freshly
    # generated monthly scorecard prose. A week with no new scorecard exits 0
    # without writing a run (the scorecard is monthly; most weeks are no-ops).
    _Grader(
        "eval_calibration_coach",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "calibration_coach", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    # The two morning trigger purposes ingest the highest-risk untrusted text
    # (news and transcripts). Their private capture corpus is populated by the
    # 04:00 wrapper and judged here in the existing weekly eval window.
    _Grader(
        "eval_material_news_capture",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        (
            "--purpose",
            "material_news_classification",
            "--since-days",
            _EVAL_AUDIT_SINCE_DAYS,
        ),
    ),
    _Grader(
        "eval_earnings_tone_capture",
        "run_llm_evals.py",
        _BEAR_TIMEOUT_S,
        ("--purpose", "earnings_tone_diff", "--since-days", _EVAL_AUDIT_SINCE_DAYS),
    ),
    # Behavioral-rules distiller (tenet-2 Phase 4) -- LAST, so it re-derives
    # from the corpus AFTER every grader above has had a chance to write
    # fresh outcome_label/process_quality rows this batch (§3.3: "behavioral:
    # after each grading batch"). Its own internals implement the per-item
    # degrade pattern (transient LLM failure -> defer + tally, retried next
    # week; a hard stop propagates as a non-zero exit, reported FAILED here
    # rather than crashing the batch) -- see synthesis.behavior_distill.
    _Grader("behavior_distill", "run_behavior_distill.py", _FAST_TIMEOUT_S),
)
_GRADER_KEYS = tuple(g.key for g in _GRADERS)


@dataclass(slots=True)
class _GraderResult:
    key: str
    status: StageStatus
    exit_code: int | None
    elapsed_seconds: float
    error: str | None = None


def _run_grader(grader: _Grader) -> _GraderResult:
    """Invoke one grader as a subprocess; echo its output under a header.

    Never raises: ``subprocess.run(check=False)`` returns a non-zero exit rather
    than raising, and ``TimeoutExpired`` / ``OSError`` are caught — so the
    caller's loop attempts every grader regardless of an earlier failure.
    """
    sys.stdout.write(
        f"\n{'=' * 72}\n=== calibration grader: {grader.key} ({grader.script})\n{'=' * 72}\n"
    )
    sys.stdout.flush()

    argv = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / grader.script),
        *grader.args,
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=grader.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            grader, f"timed out after {grader.timeout_s}s", round(time.monotonic() - t0, 3)
        )
    except OSError as exc:
        return _fail(grader, f"spawn failed: {exc}", round(time.monotonic() - t0, 3))

    elapsed = round(time.monotonic() - t0, 3)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode == 0:
        sys.stdout.write(f"[{grader.key}] OK (exit 0, {elapsed}s)\n")
        return _GraderResult(grader.key, StageStatus.OK, 0, elapsed)
    return _fail(grader, f"exited {proc.returncode}", elapsed, exit_code=proc.returncode)


def _fail(
    grader: _Grader, reason: str, elapsed: float, *, exit_code: int | None = None
) -> _GraderResult:
    sys.stderr.write(f"\n!!! [{grader.key}] FAILED - {reason}\n")
    sys.stdout.write(f"[{grader.key}] FAILED - {reason} ({elapsed}s)\n")
    return _GraderResult(grader.key, StageStatus.FAILED, exit_code, elapsed, error=reason)


def _summarize(results: list[_GraderResult], *, elapsed_seconds: float) -> dict[str, object]:
    by_key = {r.key: r.status.value for r in results}
    summary: dict[str, object] = {
        key: by_key.get(key, StageStatus.SKIPPED.value) for key in _GRADER_KEYS
    }
    summary["elapsed_seconds"] = elapsed_seconds
    return summary


def _build_graders(skip: set[str]) -> list[_Grader]:
    return [g for g in _GRADERS if g.key not in skip]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=_GRADER_KEYS,
        help="Skip a rung by key (repeatable): " + " | ".join(_GRADER_KEYS) + ".",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    t0 = time.monotonic()

    graders = _build_graders(set(args.skip))
    # Always-attempt contract: no early return — _run_grader never raises.
    results = [_run_grader(g) for g in graders]

    summary = _summarize(results, elapsed_seconds=round(time.monotonic() - t0, 3))
    sys.stdout.write("\n" + json.dumps(summary, indent=2) + "\n")

    # Exit code = number of failed graders (skipped graders are not failures).
    return sum(1 for r in results if r.status is StageStatus.FAILED)


if __name__ == "__main__":
    sys.exit(main())
