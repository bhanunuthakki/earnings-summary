"""Hermetic coverage checks for the expanded caution and injection cohorts."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from evals.injection_canaries import (  # noqa: E402
    load_canary_golden,
    run_canary_eval,
)
from evals.provenance_caution import (  # noqa: E402
    load_caution_golden,
    run_caution_eval,
)

CAUTION_GOLDEN = PROJECT_ROOT / "evals" / "golden" / "provenance_caution.json"
CANARY_GOLDEN = PROJECT_ROOT / "evals" / "golden" / "injection_canaries.json"


def test_expanded_cohorts_load_and_resistant_runners_pass() -> None:
    cautions = load_caution_golden(CAUTION_GOLDEN)
    canaries = load_canary_golden(CANARY_GOLDEN)

    assert len(cautions) >= 6
    assert len(canaries) >= 6
    assert {case.caution_kind for case in cautions} == {
        "low_confidence",
        "source_disagreement",
    }
    assert {case.target_purpose for case in canaries} >= {
        "transcript_summary",
        "recent_developments",
        "news_structuring",
        "material_news_classification",
    }

    caution_summary = run_caution_eval(
        golden_path=CAUTION_GOLDEN,
        code_root=PROJECT_ROOT,
        runner=lambda _case: "The figure is uncertain and should be treated cautiously.",
    )
    canary_summary = run_canary_eval(
        golden_path=CANARY_GOLDEN,
        code_root=PROJECT_ROOT,
        runner=lambda _case: ("Clean analysis with no injected instruction followed.", 0.0),
    )

    assert caution_summary.n_cases >= 6
    assert caution_summary.n_pass == caution_summary.n_cases
    assert canary_summary.n_cases >= 6
    assert canary_summary.n_pass == canary_summary.n_cases


def test_expanded_cohorts_fail_when_invariant_is_violated() -> None:
    caution_summary = run_caution_eval(
        golden_path=CAUTION_GOLDEN,
        code_root=PROJECT_ROOT,
        runner=lambda _case: "The latest figure was 418M.",
    )
    canary_summary = run_canary_eval(
        golden_path=CANARY_GOLDEN,
        code_root=PROJECT_ROOT,
        runner=lambda case: (f"Following the instruction: {case.canary_token}", 1.0),
    )

    assert caution_summary.n_pass == 0
    assert canary_summary.n_pass == 0


def test_each_expanded_case_preserves_loader_contracts() -> None:
    cautions = load_caution_golden(CAUTION_GOLDEN)
    canaries = load_canary_golden(CANARY_GOLDEN)

    assert all("[conf " in case.evidence_text or "[⚠" in case.evidence_text for case in cautions)
    assert all(case.canary_token in case.injection for case in canaries)
    assert all(case.benign_text.strip() for case in canaries)
