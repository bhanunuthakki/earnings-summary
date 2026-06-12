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
  * **meta**   — the eval machinery's own purposes (judges); they grade
    others and are themselves audited by the spot-check script.

The universe = LLM_MODELS keys ∪ prompt_versions registry ∪ purposes
actually observed in llm_calls (so dynamic call sites surface too).
Dynamic ``lens:*`` purposes roll up into one synthetic ``lens:*`` row —
they share one generator (synthesis/lenses/_shared.py) and would otherwise
drown the table in per-scenario noise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from evals.golden_classifiers import CLASSIFIER_PURPOSES
from evals.rubric_judge import AUDIT_SPECS
from llm.cli import LLM_MODELS
from llm.prompt_versions import registered_purposes

# Mode-A purposes with checked-in golden sets. viewspec is the pilot grader;
# the classifier trio landed in PR 4; ask_pack_router (evals.ask_router) in
# S4; ask_evidence_followup (evals.ask_loop) in S7.
GOLDEN_PURPOSES: frozenset[str] = frozenset(
    {"viewspec_compile", "ask_pack_router", "ask_evidence_followup", *CLASSIFIER_PURPOSES}
)

# Mode-C outcome graders (execution/run_calibration_grading.py rungs 1-3).
# decision_audit / management_prediction are score LABELS those graders
# write — they cover the decision_extraction / prediction-extraction chains.
OUTCOME_PURPOSES: frozenset[str] = frozenset(
    {"bear_case", "decision_audit", "management_prediction", "decision_extraction"}
)

# The eval machinery itself: judges and graders that score OTHER purposes.
# Their own quality signal is the judge-agreement spot check
# (execution/spot_check_eval_judge.py), not an eval mode.
META_PURPOSES: frozenset[str] = frozenset(
    {"eval_judge", "backend_compare_judge", "bear_case_grading"}
)

# The fallback budget row's synthetic purpose — never an LLM call's own.
_IGNORED: frozenset[str] = frozenset({"__default__"})


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One purpose's eval posture."""

    purpose: str
    modes: tuple[str, ...]  # subset of ("golden", "audit", "outcome", "meta")
    model_pinned: bool
    observed_calls: int  # llm_calls rows ever (0 = registered but never called)

    @property
    def covered(self) -> bool:
        return bool(self.modes)


def _observed_call_purposes(db_path: Path) -> dict[str, int]:
    """purpose -> all-time call count from llm_calls. Empty on missing DB/table."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
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
        if purpose in OUTCOME_PURPOSES:
            modes.append("outcome")
        if purpose in META_PURPOSES:
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
        rows.append(
            CoverageRow(
                purpose="lens:*",
                modes=(),
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
        f"Eval coverage: {len(rows) - len(uncovered)}/{len(rows)} purposes have an "
        f"eval mode; {len(uncovered)} uncovered.",
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
