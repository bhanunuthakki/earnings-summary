"""Judge-agreement spot check: do YOU agree with the eval judge's verdicts?

The eval harness's judge (purpose=`eval_judge`, Haiku-pinned) is itself a
model — its scores are only trustworthy if a human spot-checking its verdicts
mostly agrees with them (directives/llm_evals_plan.md PR 2: "sample N
verdicts for manual confirm, record agreement rate"). This tool samples the
most recent judged eval cases, shows each verdict with its evidence, asks
agree / disagree / skip, and records the agreement rate as ONE
``prompt_calibration_scores`` row::

    purpose='eval_judge', prompt_version=<registry>, score=<agree fraction>,
    scored_by='manual:judge_spot_check', reason='n=8 target=bear_case ...'

so judge reliability shows up on the same calibration surface as everything
else. A weak rate (rule of thumb: <0.8) is the trigger for the per-purpose
judge-model escalation hook (``AuditSpec.judge_model`` in
src/evals/rubric_judge.py) — escalate AFTER measuring, not on vibes.

Usage:
    python execution/spot_check_eval_judge.py --purpose bear_case --n 8
    python execution/spot_check_eval_judge.py --n 10 --no-persist   # any purpose

Answers come from stdin (y = agree, n = disagree, s = skip, q = stop early),
so the tool also works piped: ``echo y y n | python ...`` grades three.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_SNIPPET_CHARS = 900  # terminal-friendly head of long texts


@dataclass(slots=True)
class JudgedCase:
    """One eval_case_results row that carries a judge verdict."""

    case_row_id: int
    purpose: str
    mode: str
    case_id: str
    question: str
    score: float
    passed: bool
    judge_verdict: str
    judge_rationale: str | None
    expected_json: str | None
    actual_json: str | None
    response_text: str | None


def load_judged_cases(db_path: Path, *, purpose: str | None, n: int) -> list[JudgedCase]:
    """The N most recent judged cases (judge_verdict NOT NULL), optionally
    scoped to one purpose. Newest first — recency over random sampling so a
    re-run audits the same window it just produced."""
    conn = connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT c.id AS case_row_id, r.purpose, r.mode, c.case_id, c.question,
                   c.score, c.passed, c.judge_verdict, c.judge_rationale,
                   c.expected_json, c.actual_json, c.response_text
            FROM eval_case_results c
            JOIN eval_runs r ON r.id = c.eval_run_id
            WHERE c.judge_verdict IS NOT NULL
        """
        params: list[object] = []
        if purpose:
            sql += " AND r.purpose = ?"
            params.append(purpose)
        sql += " ORDER BY c.id DESC LIMIT ?"
        params.append(n)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                raise RuntimeError(
                    "eval tables missing — run `alembic upgrade head` (0083_eval_runs)."
                ) from exc
            raise
    finally:
        conn.close()
    return [
        JudgedCase(
            case_row_id=row["case_row_id"],
            purpose=row["purpose"],
            mode=row["mode"],
            case_id=row["case_id"],
            question=row["question"],
            score=float(row["score"]),
            passed=bool(row["passed"]),
            judge_verdict=row["judge_verdict"],
            judge_rationale=row["judge_rationale"],
            expected_json=row["expected_json"],
            actual_json=row["actual_json"],
            response_text=row["response_text"],
        )
        for row in rows
    ]


def _snip(text: str | None) -> str:
    if not text:
        return "(none)"
    text = text.strip()
    if len(text) <= _SNIPPET_CHARS:
        return text
    return text[:_SNIPPET_CHARS] + f"\n  ...[{len(text)} chars total]"


def _render_case(idx: int, total: int, case: JudgedCase, out: TextIO) -> None:
    out.write(f"\n{'=' * 72}\n")
    out.write(f"[{idx}/{total}] {case.purpose} ({case.mode}) · case {case.case_id}\n")
    out.write(f"  {case.question}\n")
    out.write(
        f"  judge said: score={case.score:.2f} "
        f"{'PASS' if case.passed else 'FAIL'} — {case.judge_rationale or '(no rationale)'}\n"
    )
    out.write(f"\n  expected/rubric:\n  {_snip(case.expected_json)}\n")
    out.write(f"\n  actual:\n  {_snip(case.actual_json)}\n")
    if case.actual_json is None and case.response_text:
        out.write(f"\n  raw judge response:\n  {_snip(case.response_text)}\n")
    out.write("\nAgree with the judge? [y]es / [n]o / [s]kip / [q]uit: ")
    out.flush()


@dataclass(slots=True)
class SpotCheckResult:
    agrees: int = 0
    disagrees: int = 0
    skipped: int = 0

    @property
    def graded(self) -> int:
        return self.agrees + self.disagrees

    @property
    def agreement_rate(self) -> float | None:
        if self.graded == 0:
            return None
        return self.agrees / self.graded


def run_spot_check(
    cases: list[JudgedCase],
    *,
    in_stream: TextIO,
    out_stream: TextIO,
) -> SpotCheckResult:
    """Walk the cases, collecting y/n/s answers from ``in_stream``. EOF or
    'q' stops early; whatever was graded so far still counts."""
    result = SpotCheckResult()
    total = len(cases)
    for idx, case in enumerate(cases, start=1):
        _render_case(idx, total, case, out_stream)
        line = in_stream.readline()
        if not line:  # EOF — piped input exhausted
            break
        answer = line.strip().lower()
        if answer in ("q", "quit"):
            break
        if answer in ("y", "yes"):
            result.agrees += 1
        elif answer in ("n", "no"):
            result.disagrees += 1
        else:
            result.skipped += 1
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--purpose",
        default=None,
        help="Scope to one eval purpose (default: every purpose with judged cases).",
    )
    parser.add_argument("--n", type=int, default=8, help="How many recent verdicts to sample.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo whose data/portfolio.db holds the eval tables. Default: this repo.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Don't write the agreement-rate calibration row (dry run).",
    )
    args = parser.parse_args(argv)

    db_path = args.repo_root.resolve() / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"ERROR: no DB at {db_path}", file=sys.stderr)
        return 1
    try:
        cases = load_judged_cases(db_path, purpose=args.purpose, n=max(1, args.n))
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not cases:
        print(
            "No judged eval cases found"
            + (f" for purpose {args.purpose!r}" if args.purpose else "")
            + " — run an eval first (execution/run_llm_evals.py)."
        )
        return 0

    result = run_spot_check(cases, in_stream=sys.stdin, out_stream=sys.stdout)
    rate = result.agreement_rate
    print(
        f"\n{'-' * 72}\n"
        f"Spot check: {result.graded} graded "
        f"(agree={result.agrees} disagree={result.disagrees} skip={result.skipped})"
    )
    if rate is None:
        print("No verdicts graded — nothing to record.")
        return 0
    print(f"Agreement rate: {rate:.2f}")
    if rate < 0.8:
        print(
            "Below the 0.8 rule of thumb — consider the per-purpose judge-model "
            "escalation hook (AuditSpec.judge_model in src/evals/rubric_judge.py)."
        )

    if not args.no_persist:
        from llm.calibration import CalibrationScore, record_score
        from llm.prompt_versions import prompt_version_for

        row_id = record_score(
            CalibrationScore(
                purpose="eval_judge",
                prompt_version=prompt_version_for("eval_judge"),
                score=rate,
                reason=(
                    f"n={result.graded} target={args.purpose or 'all'} "
                    f"agree={result.agrees} disagree={result.disagrees}"
                ),
                scored_by="manual:judge_spot_check",
            ),
            db_path=db_path,
        )
        print(
            f"Recorded agreement rate as prompt_calibration_scores row {row_id}"
            if row_id
            else "WARNING: calibration row not recorded (table missing?)"
        )
    print(json.dumps({"graded": result.graded, "agreement_rate": rate}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
