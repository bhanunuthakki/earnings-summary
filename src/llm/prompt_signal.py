"""The improvement signal that steers prompt A/B proposals
(meta_eval_governance.md §4.2 — designed there, never built).

§4.2 specified the proposer's third input as "the improvement signal: recent
judge rationales where this purpose's incumbent LOST a facet, recent eval
failures, and format-retry counts". The shipped driver passed the literal string
``"(operator-initiated; see recent verdict rationales)"`` instead
(``run_prompt_ab.py``), so the proposer never saw evidence of what was actually
weak — it could only guess from the prompt text. This module supplies the real
thing.

It also produces the **deficit** score the cycle uses to weight which purpose to
experiment on. A purpose that is expensive AND scoring badly is worth more
experiment budget than one that is merely expensive.

Honesty rule (the [[feedback-silent-degradation-class]] lesson): a purpose with
NO eval coverage has an UNKNOWN deficit, not a zero one. Scoring it 0.0 would
read as "this prompt is perfect" and would quietly starve exactly the purposes
nobody has ever measured. Uncovered purposes get an explicit neutral prior,
``has_eval_coverage=False``, and a line in the rendered text saying so.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

WINDOW_DAYS = 60

# Deficit blend. Eval evidence dominates; operational noise is a small nudge.
_W_SCORE = 0.6
_W_FAIL = 0.3
_W_OPS = 0.1

# What an unmeasured purpose is worth. Deliberately mid-range: high enough that
# uncovered purposes still get drawn (they are where the unknown-unknowns live),
# low enough that a measured-bad purpose outranks a measured-nothing one.
NEUTRAL_PRIOR_DEFICIT = 0.35

MAX_RATIONALES = 10
MAX_FAILURES = 6


@dataclass(frozen=True, slots=True)
class ImprovementSignal:
    """Evidence of where a purpose's prompt is losing, plus the derived deficit."""

    purpose: str
    text: str
    deficit: float
    has_eval_coverage: bool
    avg_eval_score: float | None
    fail_rate: float | None
    error_rate: float
    n_rationales: int
    n_failures: int
    n_infra_excluded: int = 0

    @property
    def is_evidence_backed(self) -> bool:
        """True when the deficit came from measurements rather than the prior.

        Callers log this; a cycle steered entirely by priors is a cycle telling
        you to go harvest evals, not a cycle that found something.
        """
        return self.has_eval_coverage or self.n_rationales > 0


def _cutoff(days: int) -> str:
    """Naive-UTC cutoff — stored stamps are naive-UTC (repo convention); an
    aware stamp compares lexicographically WRONG against them."""
    return (datetime.now(UTC) - timedelta(days=days)).replace(tzinfo=None).isoformat()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


# An eval row whose JUDGE crashed is an INFRA fact, not a prompt-quality fact.
# On this platform they arrive as failure_stage='judge' at score 0.0 with a
# rationale that starts "judge call failed:". Counting them as quality failures
# would (a) tell the proposer a healthy prompt is broken and (b) inflate the
# deficit so the cycle spends its budget on whichever purpose the CLI happened
# to fail on most. They are excluded from the quality math and COUNTED, so an
# eval set that is mostly infra rubble is visible rather than silently thin.
_INFRA_RATIONALE_PREFIX = "judge call failed"

_QUALITY_ONLY = (
    "NOT (LOWER(COALESCE(ecr.judge_rationale, '')) LIKE ? "
    "     OR (ecr.failure_stage = 'judge' AND COALESCE(ecr.score, 0) = 0))"
)
_INFRA_LIKE = f"{_INFRA_RATIONALE_PREFIX}%"


def _eval_evidence(
    conn: sqlite3.Connection, purpose: str
) -> tuple[float | None, float | None, list[str], int]:
    """(avg_score, fail_rate, failure notes, n_infra_excluded) from the eval harness.

    ``eval_case_results.eval_run_id`` is an INTEGER FK to ``eval_runs.id`` — NOT
    the text ``eval_runs.run_id``. Joining on the text column silently returns
    zero rows, which would look exactly like "this purpose has no failures".
    """
    if not (_has_table(conn, "eval_case_results") and _has_table(conn, "eval_runs")):
        return None, None, [], 0
    row = conn.execute(
        f"""
        SELECT AVG(ecr.score) AS avg_score,
               SUM(CASE WHEN ecr.passed = 0 THEN 1 ELSE 0 END) AS fails,
               COUNT(*) AS n
        FROM eval_case_results ecr
        JOIN eval_runs er ON er.id = ecr.eval_run_id
        WHERE er.purpose = ? AND {_QUALITY_ONLY}
        """,
        (purpose, _INFRA_LIKE),
    ).fetchone()
    infra_row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM eval_case_results ecr
        JOIN eval_runs er ON er.id = ecr.eval_run_id
        WHERE er.purpose = ? AND NOT ({_QUALITY_ONLY})
        """,
        (purpose, _INFRA_LIKE),
    ).fetchone()
    n_infra = int(infra_row["n"] or 0) if infra_row is not None else 0
    if row is None or not row["n"]:
        return None, None, [], n_infra
    avg_score = float(row["avg_score"]) if row["avg_score"] is not None else None
    fail_rate = float(row["fails"] or 0) / float(row["n"])
    notes = [
        f"- [{r['failure_stage'] or 'fail'} score={float(r['score'] or 0):.2f}] "
        f"{str(r['judge_rationale'] or '').strip()[:340]}"
        for r in conn.execute(
            f"""
            SELECT ecr.score, ecr.failure_stage, ecr.judge_rationale
            FROM eval_case_results ecr
            JOIN eval_runs er ON er.id = ecr.eval_run_id
            WHERE er.purpose = ? AND ecr.passed = 0
              AND ecr.judge_rationale IS NOT NULL AND ecr.judge_rationale != ''
              AND {_QUALITY_ONLY}
            ORDER BY ecr.id DESC LIMIT ?
            """,
            (purpose, _INFRA_LIKE, MAX_FAILURES),
        )
    ]
    return avg_score, fail_rate, notes, n_infra


def _losing_rationales(conn: sqlite3.Connection, purpose: str) -> list[str]:
    """Judge prose from cases where this purpose's INCUMBENT lost the pair.

    Those rationales name what the incumbent's output lacked — which is exactly
    the deficit a prompt edit can attack.
    """
    if not _has_table(conn, "model_eval_verdicts"):
        return []
    out: list[str] = []
    for row in conn.execute(
        "SELECT incumbent, summary_json FROM model_eval_verdicts "
        "WHERE purpose = ? AND summary_json IS NOT NULL AND summary_json != '' "
        "AND recorded_at >= ? ORDER BY id DESC LIMIT 12",
        (purpose, _cutoff(WINDOW_DAYS)),
    ):
        try:
            parsed: object = json.loads(str(row["summary_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        cases = cast("dict[str, object]", parsed).get("cases")
        if not isinstance(cases, list):
            continue
        incumbent = str(row["incumbent"] or "")
        for entry in cast("list[object]", cases):
            if not isinstance(entry, dict):
                continue
            case = cast("dict[str, object]", entry)
            winner = case.get("winner_model")
            # The incumbent lost this case -> the rationales say why.
            if not isinstance(winner, str) or not winner or winner == incumbent:
                continue
            rationales = case.get("rationales")
            if not isinstance(rationales, list):
                continue
            for text_obj in cast("list[object]", rationales):
                if isinstance(text_obj, str) and text_obj.strip():
                    out.append(f"- {text_obj.strip()[:340]}")
                if len(out) >= MAX_RATIONALES:
                    return out
    return out


def _operational_rate(conn: sqlite3.Connection, purpose: str) -> float:
    """Error+fallback rate over production calls — the "format-retry" proxy."""
    if not _has_table(conn, "llm_calls"):
        return 0.0
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN (error IS NOT NULL AND error != '') OR fallback_used = 1
                        THEN 1 ELSE 0 END) AS bad
        FROM llm_calls WHERE purpose = ? AND called_at >= ?
        """,
        (purpose, _cutoff(WINDOW_DAYS)),
    ).fetchone()
    if row is None or not row["n"]:
        return 0.0
    return float(row["bad"] or 0) / float(row["n"])


def build_improvement_signal(db_path: Path, purpose: str) -> ImprovementSignal:
    """Assemble the §4.2 signal. Any DB problem degrades to the neutral prior
    with the failure stated in the rendered text — never to a confident zero."""
    if not Path(db_path).exists():
        return _prior_only(purpose, "DB not found — deficit is a prior, not a measurement")
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            avg_score, fail_rate, failures, n_infra = _eval_evidence(conn, purpose)
            rationales = _losing_rationales(conn, purpose)
            error_rate = _operational_rate(conn, purpose)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _prior_only(purpose, f"signal read failed ({exc}) — deficit is a prior")

    has_coverage = avg_score is not None
    if has_coverage:
        score_gap = max(0.0, 1.0 - float(avg_score or 0.0))
        deficit = _W_SCORE * score_gap + _W_FAIL * float(fail_rate or 0.0) + _W_OPS * error_rate
    else:
        deficit = NEUTRAL_PRIOR_DEFICIT + _W_OPS * error_rate
    deficit = round(min(1.0, max(0.0, deficit)), 4)

    lines: list[str] = []
    if has_coverage:
        lines.append(
            f"Eval coverage: avg score {float(avg_score or 0):.3f}, "
            f"fail rate {float(fail_rate or 0):.0%}."
        )
    else:
        lines.append(
            "Eval coverage: NONE for this purpose — the deficit below is a "
            f"neutral prior ({NEUTRAL_PRIOR_DEFICIT}), not a measurement. Treat the "
            "rationales as the only hard evidence."
        )
    if n_infra:
        lines.append(
            f"({n_infra} eval row(s) excluded as judge-call/infra failures — they say "
            "nothing about this prompt's quality.)"
        )
    lines.append(f"Operational error/fallback rate ({WINDOW_DAYS}d): {error_rate:.1%}.")
    if error_rate >= 0.25:
        lines.append(
            "  NOTE: an error rate this high is a TRANSPORT fault (CLI/quota), not a "
            "prompt defect. Do NOT propose edits aimed at 'reliability' or 'retries' — "
            "no prompt wording fixes a failing subprocess."
        )
    if failures:
        lines.append("\nRecent EVAL FAILURES (judge rationale):")
        lines.extend(failures)
    if rationales:
        lines.append("\nRecent cases where THIS prompt's output LOST to an alternative:")
        lines.extend(rationales)
    if not failures and not rationales:
        lines.append(
            "\nNo per-case judge evidence in the window. Improve on general "
            "grounds from the scaffold, and prefer a conservative edit."
        )

    return ImprovementSignal(
        purpose=purpose,
        text="\n".join(lines),
        deficit=deficit,
        has_eval_coverage=has_coverage,
        avg_eval_score=avg_score,
        fail_rate=fail_rate,
        error_rate=round(error_rate, 4),
        n_rationales=len(rationales),
        n_failures=len(failures),
        n_infra_excluded=n_infra,
    )


def _prior_only(purpose: str, why: str) -> ImprovementSignal:
    return ImprovementSignal(
        purpose=purpose,
        text=f"NO SIGNAL AVAILABLE: {why}.",
        deficit=NEUTRAL_PRIOR_DEFICIT,
        has_eval_coverage=False,
        avg_eval_score=None,
        fail_rate=None,
        error_rate=0.0,
        n_rationales=0,
        n_failures=0,
    )


def ab_leverage(cost_usd_30d: float, deficit: float) -> float:
    """Weight for the randomized purpose draw (§4.6 cadence, made concrete).

    NOT the model-downgrade headroom: ``PurposeWorkload.headroom_usd_30d`` is
    the saving from swapping to a cheaper MODEL, which is 0 for web-scoped and
    already-cheapest purposes — several of which (``recent_developments``) are
    explicitly A/B-eligible per §4.6. Prompt-improvement value scales instead
    with how much output the prompt governs (cost as the volume proxy) times how
    badly it is currently scoring.
    """
    return max(0.0, cost_usd_30d) * (1.0 + 2.0 * max(0.0, min(1.0, deficit)))
