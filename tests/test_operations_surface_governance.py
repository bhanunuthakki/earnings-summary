from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repo_instructions_route_operational_changes_to_the_surface_directive() -> None:
    instructions = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "directives/operations_governance_surface.md" in instructions
    assert "adds, removes, renames, or materially changes" in instructions
    assert "explicit tested no-surface-change disposition" in instructions


def test_targeted_fact_resolution_is_deliberately_internal_only() -> None:
    roadmap = (
        PROJECT_ROOT / "docs" / "design" / "investment_grade_grounded_data_roadmap.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(roadmap.split())

    assert "backfill_financial_fact_resolutions.py --fact-table --fact-row-id --apply" in normalized
    assert "internal, receipt-bound repair primitive" in normalized
    assert (
        "canonical `OperationsRegistry`, `OperationsSnapshot`, and "
        "`build_operations_panel_view`" in normalized
    )
    assert "adds no Operations card, health claim, or operator action" in normalized


def test_surface_directive_states_the_complete_decision_contract() -> None:
    directive = (PROJECT_ROOT / "directives" / "operations_governance_surface.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "Target sources",
        "Output schema",
        "Refresh cadence",
        "Logical Idempotency Key",
        "Rate-limit budget",
        "Failure-mode policy",
        "Trigger matrix",
        "Removal contract",
        "Operator-action boundary",
        "Required evidence",
    ):
        assert required in directive
    assert "Missing, Stale, Invalid, or Unavailable" in directive
    assert "Do not copy current task names" in directive


def test_pull_request_template_requires_one_operations_impact_disposition() -> None:
    template = (PROJECT_ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")

    assert "## Operations & Governance impact" in template
    assert "None — reason" in template
    assert "Existing dynamic projection remains truthful" in template
    assert "Surface/registry/freshness/action contract updated" in template
    assert "Select exactly one" in template
