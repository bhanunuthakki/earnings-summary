"""Eval-coverage report: which LLM purposes have NO eval mode (plan PR 4).

The analogue of the unknown-purpose model warning: the model picker made
"purpose without a pin" observable; this makes "purpose without a quality
signal" observable. A purpose counts as covered when at least one mode can
score it:

  * **golden** — a mode-A golden set under evals/golden/ (graders in
    evals.viewspec_compile / evals.golden_classifiers);
  * **audit**  — a mode-B rubric spec (evals.rubric_judge.AUDIT_SPECS);
  * **outcome** — one of the mode-C graders run by
    execution/run_calibration_grading.py (constant below — update it when a
    grader is added there);
  * **capture_audit** — replay of an opt-in production prompt/response capture
    against a versioned purpose-specific quality bar;
  * **meta**   — the eval machinery's own purposes (judges); they grade
    others and are themselves audited by the spot-check script.

The universe = LLM_MODELS keys ∪ prompt_versions registry ∪ purposes
actually observed in llm_calls (so dynamic call sites surface too).
Dynamic ``lens:*`` purposes roll up into one synthetic ``lens:*`` row —
they share one generator (synthesis/lenses/_shared.py) and would otherwise
drown the table in per-scenario noise.

``eval_coverage`` remains an observability report: it shows every gap without
failing. ``eval_coverage_gate`` is the CI ratchet. The ratchet carries an
explicit snapshot of pre-existing registered gaps and blocks only new ones.
Adding a purpose to either the model picker or the prompt-version registry
therefore requires a real golden, rubric/audit, outcome, or meta eval in the
same change. Schema validation is an output contract, not a quality eval, and
is deliberately not a coverage mode.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from evals.capture_quality_specs import CAPTURE_QUALITY_SPECS
from evals.rubric_judge import AUDIT_SPECS
from evals.run_registry import GOLDEN_PURPOSES as REGISTERED_GOLDEN_PURPOSES
from llm.cli import LLM_MODELS
from llm.prompt_versions import registered_purposes
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

# Mode-A purposes with checked-in golden sets. viewspec is the pilot grader;
# the classifier trio landed in PR 4; ask_pack_router (evals.ask_router) in S4;
# ask_evidence_followup (evals.ask_loop) in S7; ask_claim_grounding
# (evals.ask_citations — citation accuracy) in S8; injection_canaries
# (evals.injection_canaries) in S9; news_structuring (Chip 2) in PR C.
GOLDEN_PURPOSES: frozenset[str] = frozenset(REGISTERED_GOLDEN_PURPOSES)

# Mode-C outcome graders (execution/run_calibration_grading.py rungs 1-3).
# decision_audit / management_prediction are score LABELS those graders
# write — they cover the decision_extraction / prediction-extraction chains.
OUTCOME_PURPOSES: frozenset[str] = frozenset(
    {"bear_case", "decision_audit", "management_prediction", "decision_extraction"}
)

# The eval machinery itself: judges, graders and steering calls that score or
# route OTHER purposes. Their own quality signal is the judge-agreement /
# classification spot check (execution/spot_check_eval_judge.py), not an eval
# mode. case_difficulty_classify is the sweep sampler's difficulty classifier
# (meta_eval_governance.md §2) — it stratifies the corpus others are graded on.
META_PURPOSES: frozenset[str] = frozenset(
    {
        "eval_judge",
        "backend_compare_judge",
        "bear_case_grading",
        "case_difficulty_classify",
        "optimizer_nominator",
        "model_frontier_research",
        "query_criteria_derive",
        "prompt_variant_propose",
        "prompt_reflect_rewrite",
        "readme_update_judge",
    }
)
_OBSERVED_META_PURPOSES: frozenset[str] = frozenset(
    {
        # Legacy ledger rows used this purpose before the canonical
        # backend_compare_judge name. It is the same governed judge workload,
        # but it is not a current registered purpose.
        "backend_judge",
    }
)

# The fallback budget row's synthetic purpose — never an LLM call's own.
_IGNORED: frozenset[str] = frozenset({"__default__"})

# The legacy snapshot was fully paid down by capture-quality replay audits.
# Keep the symbol for gate/test compatibility: adding any exemption is a new
# debt decision and must not be used to bypass the real quality-mode rule.
GRANDFATHERED_UNCOVERED_PURPOSES: frozenset[str] = frozenset()

_INSUFFICIENT_VERDICTS: frozenset[str] = frozenset({"INSUFFICIENT_DATA", "INSUFFICIENT_FRAME"})
_ERROR_VERDICTS: frozenset[str] = frozenset({"CANDIDATE_ERRORED", "JUDGE_DEGRADED"})
_GRADED_VERDICTS: frozenset[str] = frozenset({"SWITCH_DOWN", "KEEP_INCUMBENT", "HOLD"})


class EvalRunReceipt(BaseModel):
    """Durable evidence that an eval sweep actually graded usable cases.

    Coverage registration answers whether an eval *can* run. This receipt
    answers whether the latest sweep did run and produced gradeable evidence.
    Advisory insufficient-frame outcomes are kept separate from transport or
    judge errors so operators know whether to harvest data or repair infra.
    """

    schema_version: Literal["1"] = "1"
    run_id: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime
    attempted: int = Field(ge=0)
    graded: int = Field(ge=0)
    insufficient: int = Field(ge=0)
    errors: int = Field(ge=0)
    alert_count: int = Field(ge=0)
    alerts: tuple[str, ...] = ()
    status: Literal["passed", "alert"]


def build_eval_run_receipt(
    recommendations: Sequence[str],
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> EvalRunReceipt:
    """Classify a sweep without conflating thin evidence with broken infra."""
    insufficient = sum(item in _INSUFFICIENT_VERDICTS for item in recommendations)
    graded = sum(item in _GRADED_VERDICTS for item in recommendations)
    errors = len(recommendations) - insufficient - graded
    alerts: list[str] = []
    if graded == 0:
        alerts.append("no_graded_verdict")
    if errors:
        alerts.append("eval_errors_present")
    return EvalRunReceipt(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        attempted=len(recommendations),
        graded=graded,
        insufficient=insufficient,
        errors=errors,
        alert_count=len(alerts),
        alerts=tuple(alerts),
        status="alert" if alerts else "passed",
    )


def persist_eval_run_receipt(
    repo_root: Path, receipt: EvalRunReceipt, *, dry_run: bool = False
) -> Path:
    """Atomically persist an immutable run receipt plus the latest pointer."""
    base = repo_root / (".tmp" if dry_run else "data") / "model_eval_runs"
    base.mkdir(parents=True, exist_ok=True)
    payload = receipt.model_dump_json(indent=2) + "\n"
    run_path = base / f"{receipt.run_id}.json"
    for target in (run_path, base / "latest.json"):
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(target)
    return run_path


def load_latest_eval_run_receipt(repo_root: Path) -> EvalRunReceipt | None:
    """Read and validate the latest durable receipt; malformed means absent."""
    path = repo_root / "data" / "model_eval_runs" / "latest.json"
    try:
        return EvalRunReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One purpose's eval posture."""

    purpose: str
    modes: tuple[str, ...]  # golden/audit/capture_audit/outcome/meta
    model_pinned: bool
    observed_calls: int  # llm_calls rows ever (0 = registered but never called)

    @property
    def covered(self) -> bool:
        return bool(self.modes)


@dataclass(frozen=True, slots=True)
class CoverageGateResult:
    """Result of the no-new-gap registered-purpose ratchet."""

    new_uncovered: tuple[str, ...]
    stale_grandfathered: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.new_uncovered and not self.stale_grandfathered


def _observed_call_purposes(db_path: Path) -> dict[str, int]:
    """purpose -> all-time call count from llm_calls. Empty on missing DB/table."""
    if not db_path.exists():
        return {}
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return {}
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_calls'"
        ).fetchone()
        if present is None:
            return {}
        rows = conn.execute(
            "SELECT purpose, COUNT(*) FROM llm_calls WHERE purpose IS NOT NULL GROUP BY purpose"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(p): int(n) for p, n in rows}


def eval_coverage(db_path: Path) -> list[CoverageRow]:
    """Every known purpose with its eval modes, uncovered first (the gap is
    the headline), then by observed call volume so the busiest gaps lead."""
    observed = _observed_call_purposes(db_path)
    universe = (set(LLM_MODELS) | set(registered_purposes()) | set(observed)) - _IGNORED

    lens_calls = sum(n for p, n in observed.items() if p.startswith("lens:"))
    universe = {p for p in universe if not p.startswith("lens:")}

    rows: list[CoverageRow] = []
    for purpose in universe:
        modes: list[str] = []
        if purpose in GOLDEN_PURPOSES:
            modes.append("golden")
        if purpose in AUDIT_SPECS:
            modes.append("audit")
        if purpose in CAPTURE_QUALITY_SPECS:
            modes.append("capture_audit")
        if purpose in OUTCOME_PURPOSES:
            modes.append("outcome")
        if purpose in META_PURPOSES or purpose in _OBSERVED_META_PURPOSES:
            modes.append("meta")
        rows.append(
            CoverageRow(
                purpose=purpose,
                modes=tuple(modes),
                model_pinned=purpose in LLM_MODELS,
                observed_calls=observed.get(purpose, 0),
            )
        )
    if lens_calls:
        lens_modes = ("capture_audit",) if "lens:*" in CAPTURE_QUALITY_SPECS else ()
        rows.append(
            CoverageRow(
                purpose="lens:*",
                modes=lens_modes,
                model_pinned=False,  # per-lens models live on the Lens object by design
                observed_calls=lens_calls,
            )
        )
    rows.sort(key=lambda r: (r.covered, -r.observed_calls, r.purpose))
    return rows


def render_coverage_text(rows: list[CoverageRow]) -> str:
    """The CLI table. Uncovered purposes first; the summary line carries the
    countable fact (n uncovered / n total)."""
    uncovered = [r for r in rows if not r.covered]
    lines = [
        f"Eval mode availability: {len(rows) - len(uncovered)}/{len(rows)} purposes have an "
        f"eval mode; {len(uncovered)} uncovered.",
        "Capture-audit availability is not a passing score or corpus-readiness claim; "
        "a missing current versioned cohort exits 2 when that audit runs.",
        "",
        f"{'purpose':<36} {'modes':<24} {'pinned':<7} calls",
    ]
    lines.append("-" * 78)
    for r in rows:
        modes = ",".join(r.modes) if r.modes else "NONE"
        lines.append(
            f"{r.purpose:<36} {modes:<24} {'yes' if r.model_pinned else 'no':<7} {r.observed_calls}"
        )
    return "\n".join(lines)


def eval_coverage_gate(rows: list[CoverageRow]) -> CoverageGateResult:
    """Apply the no-new-gap ratchet to registered model/prompt purposes.

    Observed-only purposes remain visible in the report but do not make CI
    nondeterministic: the gate's input universe is the two checked-in
    registries. Conversely, a purpose registered *only* in prompt_versions is
    still gated, so adding a prompt cannot evade the model-picker side.

    A stale exemption is also a failure. That makes coverage monotonic: after a
    grandfathered purpose gains a real mode, its exemption must be deleted and
    cannot later hide a regression.
    """
    registered = (set(LLM_MODELS) | set(registered_purposes())) - _IGNORED
    uncovered_registered = {
        row.purpose for row in rows if row.purpose in registered and not row.covered
    }
    return CoverageGateResult(
        new_uncovered=tuple(sorted(uncovered_registered - GRANDFATHERED_UNCOVERED_PURPOSES)),
        stale_grandfathered=tuple(sorted(GRANDFATHERED_UNCOVERED_PURPOSES - uncovered_registered)),
    )


def render_coverage_gate_text(result: CoverageGateResult) -> str:
    """Human-readable CI result with an actionable failure explanation."""
    if result.passed:
        return (
            "Eval coverage gate: PASS - no registered purpose lacks a configured "
            "executable eval mode. Capture corpus readiness is checked when the mode runs."
        )

    lines = ["Eval coverage gate: FAIL."]
    if result.new_uncovered:
        lines.append(
            "New registered purposes without a golden/audit/capture_audit/outcome/meta eval:"
        )
        lines.extend(f"  - {purpose}" for purpose in result.new_uncovered)
    if result.stale_grandfathered:
        lines.append(
            "Stale grandfathered gaps (covered or no longer registered; remove their exemptions):"
        )
        lines.extend(f"  - {purpose}" for purpose in result.stale_grandfathered)
    lines.append(
        "Schema validation or an empty capture declaration alone is not a quality eval. "
        "Add an executable eval mode, or remove an obsolete registration."
    )
    return "\n".join(lines)
