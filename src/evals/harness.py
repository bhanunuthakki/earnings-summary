"""Shared eval-harness types, run math, and persistence orchestration.

Purpose-specific graders (evals.viewspec_compile today; evals.bear_case in
PR 2) produce ``CaseResult`` rows; this module rolls them into an
``EvalRunSummary`` and persists the whole run — eval tables + the one-row
bridge into ``prompt_calibration_scores`` that lets the existing
``summarize_by_prompt_version`` read side compare prompt versions for free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from calibration_guard import wilson_interval
from llm.calibration import CalibrationScore, record_score

log = logging.getLogger(__name__)


class EvalAbortError(RuntimeError):
    """The run cannot proceed for a non-quality reason (e.g. the target
    purpose is budget-skipped). Distinct from a case failing: aborting must
    not record a low score against the prompt."""


def now_naive_utc() -> datetime:
    """Repo convention: naive-UTC datetimes (aware stamps crash comparisons
    against the naive ones every store already holds)."""
    return datetime.now(UTC).replace(tzinfo=None)


# failure_stage marking a case whose JUDGE failed operationally (CLI error,
# quota, network) — an INFRA fact, not a quality fact. Such cases carry
# score=None and are excluded from avg_score/pass-rate math. Measured July
# 2026: 18 of 28 recorded "failures" were judge-call crashes scored 0.0, which
# dragged bear_case's apparent avg from 0.959 to 0.706 — the score was
# measuring the CLI outage, not the prompt.
JUDGE_INFRA_STAGE = "judge_infra"

# A set whose measured pass rate is at or above this has no headroom left: a
# better prompt cannot show up in it. Use it to cut cost or latency, or harden
# the set, rather than to claim a quality gain.
SATURATION_PASS_RATE = 0.95

# Separator for per-repeat case ids (``case@r2``) in a combined repeat run.
REPEAT_SEPARATOR = "@r"


@dataclass(slots=True)
class CaseResult:
    """One graded golden case — maps 1:1 onto an eval_case_results row.

    ``score=None`` means "not measured" (judge infra failure) — distinct from
    0.0, which is a real measured zero. The DB column is nullable REAL, so the
    distinction survives persistence."""

    case_id: str
    question: str
    passed: bool
    score: float | None
    expected_json: str | None = None
    actual_json: str | None = None
    failure_stage: str | None = None  # "compile" | "execute" | "mismatch" | "judge_infra" | None
    judge_verdict: str | None = None
    judge_rationale: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    latency_ms: int | None = None

    @property
    def is_infra_failure(self) -> bool:
        return self.failure_stage == JUDGE_INFRA_STAGE


@dataclass(slots=True)
class EvalRunSummary:
    """One harness invocation — maps onto an eval_runs row + its cases."""

    run_id: str
    purpose: str
    mode: str
    prompt_version: str
    model: str
    judge_model: str | None
    golden_set_sha: str | None
    started_at: datetime
    finished_at: datetime | None = None
    git_sha: str | None = None
    notes: str | None = None
    cases: list[CaseResult] = field(default_factory=list[CaseResult])

    @property
    def n_cases(self) -> int:
        return len(self.cases)

    @property
    def n_infra(self) -> int:
        """Cases whose judge failed operationally — counted, never scored."""
        return sum(1 for c in self.cases if c.is_infra_failure)

    @property
    def n_scored(self) -> int:
        return sum(1 for c in self.cases if c.score is not None)

    @property
    def n_pass(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def avg_score(self) -> float | None:
        """Mean over SCORED cases only. Infra-failed cases (score=None) say
        nothing about quality; averaging them in as zeros made a healthy
        prompt look broken exactly when the transport was down."""
        scored = [c.score for c in self.cases if c.score is not None]
        if not scored:
            return None
        return sum(scored) / len(scored)

    @property
    def n_measured(self) -> int:
        """Cases with a quality outcome (infra failures excluded)."""
        return sum(1 for c in self.cases if not c.is_infra_failure)

    @property
    def pass_rate(self) -> float | None:
        n = self.n_measured
        if n == 0:
            return None
        return sum(1 for c in self.cases if c.passed and not c.is_infra_failure) / n

    @property
    def pass_rate_ci95(self) -> tuple[float, float] | None:
        """Wilson 95% interval on the pass rate: how far the score could move
        by chance alone at this case count."""
        n = self.n_measured
        passed = sum(1 for c in self.cases if c.passed and not c.is_infra_failure)
        return wilson_interval(passed, n)

    @property
    def avg_score_ci95(self) -> tuple[float, float] | None:
        """Normal-approximation 95% interval on the mean scored case, clamped
        to [0, 1]. None below two scored cases."""
        scored = [c.score for c in self.cases if c.score is not None]
        if len(scored) < 2:
            return None
        mean = sum(scored) / len(scored)
        var = sum((x - mean) ** 2 for x in scored) / (len(scored) - 1)
        half = 1.96 * math.sqrt(var / len(scored))
        return max(0.0, mean - half), min(1.0, mean + half)

    @property
    def saturated(self) -> bool:
        rate = self.pass_rate
        return rate is not None and rate >= SATURATION_PASS_RATE

    def to_json_dict(self) -> dict[str, object]:
        """Stdout-friendly summary (cases included, transcripts truncated)."""
        cases: list[dict[str, object]] = []
        for c in self.cases:
            d = asdict(c)
            for key in ("prompt_text", "response_text"):
                v = d.get(key)
                if isinstance(v, str) and len(v) > 400:
                    d[key] = v[:400] + f"... [{len(v)} chars]"
            cases.append(d)
        return {
            "run_id": self.run_id,
            "purpose": self.purpose,
            "mode": self.mode,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "judge_model": self.judge_model,
            "golden_set_sha": self.golden_set_sha,
            "git_sha": self.git_sha,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "n_cases": self.n_cases,
            "n_pass": self.n_pass,
            "n_scored": self.n_scored,
            "n_infra": self.n_infra,
            "avg_score": self.avg_score,
            "avg_score_ci95": _rounded(self.avg_score_ci95),
            "pass_rate": self.pass_rate,
            "pass_rate_ci95": _rounded(self.pass_rate_ci95),
            "saturated": self.saturated,
            "cases": cases,
        }


def _rounded(interval: tuple[float, float] | None) -> list[float] | None:
    return None if interval is None else [round(interval[0], 4), round(interval[1], 4)]


def combine_repeats(runs: list[EvalRunSummary]) -> tuple[EvalRunSummary, dict[str, object]]:
    """Fold k repeats of one eval into a single run plus its noise report.

    Each repeat's cases keep their own row (``case@rN``), so the combined
    intervals reflect every observation. The noise report answers the
    question a single run cannot: how far does the score move by chance?
    ``flip_cases`` lists cases whose pass/fail changed between repeats.
    """
    if not runs:
        raise ValueError("combine_repeats needs at least one run")
    first = runs[0]
    cases: list[CaseResult] = []
    outcomes: dict[str, set[bool]] = {}
    for index, run in enumerate(runs, start=1):
        for case in run.cases:
            if not case.is_infra_failure:
                outcomes.setdefault(case.case_id, set()).add(case.passed)
            repeat_case = CaseResult(**asdict(case))
            repeat_case.case_id = f"{case.case_id}{REPEAT_SEPARATOR}{index}"
            cases.append(repeat_case)
    per_repeat = [r.avg_score for r in runs]
    measured = [a for a in per_repeat if a is not None]
    spread = (max(measured) - min(measured)) if measured else None
    flips = sorted(cid for cid, seen in outcomes.items() if len(seen) > 1)
    noise: dict[str, object] = {
        "repeats": len(runs),
        "per_repeat_avg_score": per_repeat,
        "per_repeat_pass_rate": [r.pass_rate for r in runs],
        "avg_score_spread": spread,
        "flip_cases": flips,
    }
    summary_note = f"repeats={len(runs)} spread={spread} flips={len(flips)}"
    notes = f"{first.notes}; {summary_note}" if first.notes else summary_note
    combined = EvalRunSummary(
        run_id=first.run_id,
        purpose=first.purpose,
        mode=first.mode,
        prompt_version=first.prompt_version,
        model=first.model,
        judge_model=first.judge_model,
        golden_set_sha=first.golden_set_sha,
        started_at=first.started_at,
        finished_at=runs[-1].finished_at,
        git_sha=first.git_sha,
        notes=notes,
        cases=cases,
    )
    return combined, noise


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_git_sha(repo_root: Path) -> str | None:
    """Best-effort short sha of the code under eval. None when git is
    unavailable (the run is still valid — just less comparable)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def persist_summary(summary: EvalRunSummary, *, db_path: Path) -> int:
    """Write the run + cases to the eval tables and bridge the run average
    into prompt_calibration_scores (scored_by='auto:eval_harness'). Returns
    the eval_runs row id. Raises on eval-table write failure (loud by
    design); the calibration bridge stays best-effort (its own contract)."""
    from evals import store

    if summary.mode == "capture_audit":
        # Defense in depth: capture-audit judge prompts embed private production
        # exchanges. Keep only capture IDs, hashes, scores, verdicts, and
        # rationale in the durable DB; the source text remains solely in the
        # retention-bounded private archive.
        for case in summary.cases:
            case.prompt_text = None
            case.response_text = None
            case.judge_verdict = None
            case.judge_rationale = None

    run_db_id = store.write_run(summary, db_path=db_path)
    avg = summary.avg_score
    if avg is not None:
        record_score(
            CalibrationScore(
                purpose=summary.purpose,
                prompt_version=summary.prompt_version,
                score=avg,
                reason=f"eval:{summary.mode} run {summary.run_id[:8]} n={summary.n_cases}",
                scored_by="auto:eval_harness",
            ),
            db_path=db_path,
        )
    log.info(
        {
            "event": "eval_run_persisted",
            "purpose": summary.purpose,
            "run_id": summary.run_id,
            "n_cases": summary.n_cases,
            "n_pass": summary.n_pass,
            "avg_score": avg,
            "eval_run_db_id": run_db_id,
        }
    )
    return run_db_id


def dumps_compact(payload: object) -> str:
    """Deterministic JSON for expected/actual columns (sorted keys so two
    runs' rows diff cleanly)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
