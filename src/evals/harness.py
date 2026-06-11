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
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

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


@dataclass(slots=True)
class CaseResult:
    """One graded golden case — maps 1:1 onto an eval_case_results row."""

    case_id: str
    question: str
    passed: bool
    score: float
    expected_json: str | None = None
    actual_json: str | None = None
    failure_stage: str | None = None  # "compile" | "execute" | "mismatch" | None
    judge_verdict: str | None = None
    judge_rationale: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    latency_ms: int | None = None


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
    def n_pass(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def avg_score(self) -> float | None:
        if not self.cases:
            return None
        return sum(c.score for c in self.cases) / len(self.cases)

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
            "avg_score": self.avg_score,
            "cases": cases,
        }


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
