"""Structural checks for the living frontend-instruction authority graph."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_frontend_instruction_routes_are_truthful_and_local_first() -> None:
    agents = _read("AGENTS.md")
    design = _read("directives/design_language.md")

    assert "procedures/frontend-quality.md" in agents
    assert "solo, local-first product" in agents
    assert "/harden --full" in agents
    assert "material browser-grounded UX evidence" in agents
    assert "The shared `frontend-quality` procedure owns the generic rubric" in design
    assert "`directives/interaction_contract.md`" in design
    assert "draft evidence only" in design


def test_ui_authority_statuses_do_not_promote_draft_or_history() -> None:
    index = _read("directives/README.md")
    navigation = _read("directives/navigation_ia.md")
    historical = _read("directives/interaction_paradigm_2026_06.md")
    interaction = _read("directives/interaction_contract.md")
    comments = _read("directives/report_comments_and_chat.md")

    assert "draft evidence only; non-governing" in index
    assert "Status: DRAFT" in navigation
    assert "record-only" in historical
    assert "canonical interaction boundary" in interaction
    assert "living contract" in comments
    assert "history/report_comments_and_chat_2026_06_2026_08.md" in comments


def test_comments_history_retains_the_superseded_design_source() -> None:
    history = _read("directives/history/report_comments_and_chat_2026_06_2026_08.md")
    assert "Status:** record-only" in history
    assert "# Report Comments + Unified Work OS Copilot — design scope" in history
    assert "## Feature 1: Inline comments" in history
    assert "## Implementation cadence" in history
    assert "## Open questions for the user" in history


def test_design_audit_owns_only_registered_or_explicit_execution() -> None:
    audit = _read("directives/design_conformance_audit.md")
    for concern in (
        "container economy",
        "competing layout grammars",
        "redundant hierarchy",
        "decorative formatting",
    ):
        assert concern in audit
    assert "explicit" in audit
    assert "already registered monthly schedule" in audit
    assert "never creates, changes, or implies a schedule" in audit
