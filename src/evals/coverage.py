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
from dataclasses import dataclass
from pathlib import Path

from evals.golden_classifiers import CLASSIFIER_PURPOSES
from evals.rubric_judge import AUDIT_SPECS
from llm.cli import LLM_MODELS
from llm.prompt_versions import registered_purposes

# Mode-A purposes with checked-in golden sets. viewspec is the pilot grader;
# the classifier trio landed in PR 4; ask_pack_router (evals.ask_router) in S4;
# ask_evidence_followup (evals.ask_loop) in S7; ask_claim_grounding
# (evals.ask_citations — citation accuracy) in S8; injection_canaries
# (evals.injection_canaries) in S9; news_structuring (Chip 2) in PR C.
GOLDEN_PURPOSES: frozenset[str] = frozenset(
    {
        "viewspec_compile",
        "ask_pack_router",
        "ask_evidence_followup",
        "ask_claim_grounding",
        "injection_canaries",
        "provenance_caution",
        "news_structuring",
        "peer_selection",
        "podcast_takeaway_summary",
        "key_metrics",
        "scenario_prior",
        # Sector-benchmark-ETF proposal (comparable_sets_bottoms_up.md §4, Phase 3).
        "sector_benchmark_proposal",
        # segment_10q_period_disambiguate rides in via CLASSIFIER_PURPOSES below.
        *CLASSIFIER_PURPOSES,
    }
)

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
    }
)

# The fallback budget row's synthetic purpose — never an LLM call's own.
_IGNORED: frozenset[str] = frozenset({"__default__"})

# Explicit debt snapshot at the introduction of the no-new-gap ratchet
# (2026-07-26). A purpose belongs here only when it was already registered and
# uncovered at that point. This is intentionally verbose and checked in:
#
# * a new model-picker OR prompt-version purpose cannot silently join it;
# * when a real eval lands, CI requires removing the now-stale exemption;
# * adding Pydantic/schema validation alone never qualifies for removal.
#
# Pay this set down; never append to it merely to make CI green.
GRANDFATHERED_UNCOVERED_PURPOSES: frozenset[str] = frozenset(
    {
        "advisor_socratic_memo",
        "advisor_socratic_questions",
        "advisor_swap_check",
        "annual_letter",
        "artifact_brief",
        "ask_answer",
        "business_factor_taxonomy",
        "canonicalize_segments",
        "coach_reply_intent",
        "company_description",
        "customer_concentration_extraction",
        "dcf_assumption_extract",
        "dcf_assumptions",
        "diet_source_quality",
        "drift_narrate",
        "earnings_tone_diff",
        "etf_role_synthesis",
        "event_brief",
        "exec_comp_alignment",
        "exec_comp_extraction",
        "exit_postmortem_draft",
        "extract_8k_overrides",
        "footnote_extraction",
        "guidance_lifecycle_triage",
        "investor_deck_extraction",
        "ir_sheet_kpi_map",
        "kpi_inflection_context",
        "kpi_registry_auto_proposal",
        "kpi_summary_enumerate",
        "kpi_summary_extract",
        "market_signals",
        "material_news_classification",
        "musing_decision_extract",
        "pairwise_analysis",
        "patent_timeline",
        "platform_diagram",
        "positioning_coach_turn",
        "positioning_encode",
        "presentation_brief",
        "press_release_summary",
        "pressure_test_thesis",
        "qualitative_conditions_extract",
        "recent_developments",
        "red_team_attack",
        "red_team_cross_book",
        "research_adversarial_assess",
        "research_code_spec",
        "research_fetch",
        "research_narrate",
        "research_triage",
        "risk_factor_classify",
        "risk_factor_diff",
        "saydo_commitment_extract",
        "saydo_due_context",
        "saydo_filter",
        "saydo_importance",
        "segment_6k_breakdown_extract",
        "segment_crosstab_extract",
        "segment_definition_extract",
        "session_distill",
        "strategic_analysis",
        "tenet_accountability",
        "tenet_distill",
        "tenet_semantic_tension",
        "theme_seed_cluster",
        "theme_synthesis",
        "thesis_collision",
        "thesis_entry_draft",
        "thesis_pass_a",
        "thesis_pass_b",
        "transcript_qa_judgment",
        "transcript_topic_triage",
        "valuation_basis",
        "weekly_packet_predraft",
    }
)


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
        return "Eval coverage gate: PASS - no new registered purpose lacks a real quality eval."

    lines = ["Eval coverage gate: FAIL."]
    if result.new_uncovered:
        lines.append("New registered purposes without a golden/audit/outcome/meta eval:")
        lines.extend(f"  - {purpose}" for purpose in result.new_uncovered)
    if result.stale_grandfathered:
        lines.append(
            "Stale grandfathered gaps (covered or no longer registered; remove their exemptions):"
        )
        lines.extend(f"  - {purpose}" for purpose in result.stale_grandfathered)
    lines.append(
        "Schema validation alone is not a quality eval. Add a real eval mode, "
        "or remove an obsolete registration."
    )
    return "\n".join(lines)
