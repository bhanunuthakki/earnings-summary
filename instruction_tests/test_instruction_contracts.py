"""Standalone semantic checks for the project instruction authority graph.

These tests deliberately use only the standard library and repository text. They
must remain runnable without importing ``tests/conftest.py`` or opening the app DB.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

from execution import validate_directive_manifest

ROOT = Path(__file__).resolve().parents[1]
DIRECTIVES = ROOT / "directives"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _manifest() -> dict[str, object]:
    payload: object = json.loads(_read("directives/directive_manifest.json"))
    assert isinstance(payload, dict)
    payload_dict = cast(dict[object, object], payload)
    assert all(isinstance(key, str) for key in payload_dict)
    return cast(dict[str, object], payload_dict)


def _entries() -> dict[str, dict[str, object]]:
    entries_value = _manifest()["directives"]
    assert isinstance(entries_value, dict)
    entries_dict = cast(dict[object, object], entries_value)
    assert all(isinstance(path, str) for path in entries_dict)
    entries = cast(dict[str, object], entries_dict)
    typed: dict[str, dict[str, object]] = {}
    for path, entry_value in entries.items():
        assert isinstance(entry_value, dict)
        entry_dict = cast(dict[object, object], entry_value)
        assert all(isinstance(key, str) for key in entry_dict)
        typed[path] = cast(dict[str, object], entry_dict)
    return typed


def test_directive_manifest_is_complete_and_uses_the_closed_class_vocabulary() -> None:
    entries = _entries()

    tracked = {path.relative_to(DIRECTIVES).as_posix() for path in DIRECTIVES.rglob("*.md")}
    assert set(entries) == tracked
    assert {entry["class"] for entry in entries.values() if isinstance(entry, dict)} <= {
        "canonical",
        "runbook",
        "draft",
        "history",
    }
    assert all(
        isinstance(entry, dict)
        and entry.get("class") in {"canonical", "runbook", "draft", "history"}
        for entry in entries.values()
    )


def test_manifest_is_the_complete_non_overlapping_authority_graph() -> None:
    manifest = _manifest()
    entries = _entries()

    assert manifest["schema_version"] == 2
    owners_by_domain: dict[str, str] = {}
    for path, entry in entries.items():
        classification = entry["class"]
        if classification == "canonical":
            domains = entry.get("authority_domains")
            assert isinstance(domains, list) and domains, path
            assert "governed_by" not in entry, path
            for domain in domains:
                assert isinstance(domain, str) and re.fullmatch(
                    r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", domain
                ), (path, domain)
                assert domain not in owners_by_domain, (
                    domain,
                    owners_by_domain.get(domain),
                    path,
                )
                owners_by_domain[domain] = path
        elif classification == "runbook":
            governed_by = entry.get("governed_by")
            assert isinstance(governed_by, list) and governed_by, path
            assert "authority_domains" not in entry, path
            for owner in governed_by:
                assert isinstance(owner, str), (path, owner)
                assert entries[owner]["class"] == "canonical", (path, owner)
        else:
            assert set(entry) == {"class", "summary"}, path


def test_readme_routes_through_manifest_instead_of_a_partial_authority_table() -> None:
    readme = _read("directives/README.md")

    assert "authority_domains" in readme
    assert "governed_by" in readme
    assert "## Current authority map" not in readme


def test_manifest_validator_rejects_duplicate_canonical_ownership() -> None:
    payload = copy.deepcopy(_manifest())
    entries = cast(dict[str, dict[str, object]], payload["directives"])
    entries["annual_kpi_cadence_design.md"]["authority_domains"] = ["directive_authority"]

    with patch.object(validate_directive_manifest, "_load_manifest", return_value=payload):
        errors = validate_directive_manifest.validate()

    assert any("multiple canonical owners" in error for error in errors)


def test_manifest_validator_rejects_runbook_links_to_noncanonical_files() -> None:
    payload = copy.deepcopy(_manifest())
    entries = cast(dict[str, dict[str, object]], payload["directives"])
    entries["backfill_transcripts.md"]["governed_by"] = ["capture_every_number_program.md"]

    with patch.object(validate_directive_manifest, "_load_manifest", return_value=payload):
        errors = validate_directive_manifest.validate()

    assert any("governed_by target must be a canonical directive" in error for error in errors)


def test_retention_decisions_live_with_canonical_owners_and_existing_writers() -> None:
    entries = _entries()
    dag = _read("directives/data_pipeline_dag.md")
    calls = _read("directives/llm_calls.md")
    db_gc = _read("execution/db_gc.py")
    weekly_cleanup = _read("execution/run_weekly_cleanup.py")

    assert entries["db_garbage_collection.md"]["governed_by"] == [
        "data_pipeline_dag.md",
        "data_provenance.md",
        "llm_calls.md",
        "operations_governance_surface.md",
    ]
    assert entries["weekly_cleanup.md"]["governed_by"] == [
        "data_pipeline_dag.md",
        "operations_governance_surface.md",
    ]
    assert "## Intermediate and telemetry lifecycle" in dag
    assert "## LLM artifact lifecycle" in calls
    assert "DEFAULT_TELEMETRY_RETENTION_DAYS = 90" in db_gc
    assert "DEFAULT_ARTIFACT_RETENTION_DAYS = 180" in db_gc
    assert "DEFAULT_DISPOSABLE_RETENTION_DAYS = 30" in weekly_cleanup
    assert "DEFAULT_CACHE_RETENTION_DAYS = 7" in weekly_cleanup
    agents = _read("AGENTS.md")
    assert "Safe to wipe." not in agents
    assert "no active run or recovery path depends on them" in " ".join(agents.split())


def test_semantic_manifest_edges_match_current_directive_status_and_state_owner() -> None:
    entries = _entries()

    assert entries["dcf_gsheets_setup.md"]["governed_by"] == ["holdings_json_schema.md"]
    assert entries["ir_events_ingestion.md"]["class"] == "draft"
    assert "governed_by" not in entries["ir_events_ingestion.md"]
    assert entries["monthly_red_team.md"]["class"] == "canonical"
    assert entries["monthly_red_team.md"]["authority_domains"] == ["portfolio_red_team"]


def test_manifest_validator_accepts_the_checked_in_authority_graph() -> None:
    result = subprocess.run(
        [sys.executable, "execution/validate_directive_manifest.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_folder_contract_validator_accepts_the_actual_repository_topology() -> None:
    result = subprocess.run(
        [sys.executable, "execution/validate_folder_contract.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    contract = _read("directives/folder_structure.md")
    validator = _read("execution/validate_folder_contract.py")
    assert '"tooling_directories"' in contract
    assert "tooling_directories" in validator
    assert "TOOLING_ROOTS" not in validator


def test_instruction_tooling_resolves_only_an_explicit_or_project_python() -> None:
    makefile = _read("Makefile")
    hook = _read(".githooks/pre-push")

    assert "PY ?= python3" not in makefile
    assert "No project Python found; set PYTHON_BIN or create .venv" in makefile
    assert "command -v python3" not in hook
    assert "no project Python interpreter; set PYTHON_BIN or create .venv" in hook


def test_frontend_continuity_has_one_project_owner_and_browser_proof() -> None:
    agents = _read("AGENTS.md")
    design = _read("directives/design_language.md")

    assert "nearest shipped sibling" in design
    assert "new visual family" in design
    assert "browser" in agents.lower()
    assert "src/ui/design_registry.py" in agents
    assert "four visible roles" in design
    assert "Decorative left rails" in design


def test_draft_and_history_are_non_governing_but_lineage_is_retained() -> None:
    entries = _entries()
    readme = _read("directives/README.md")
    current_comments = _read("directives/report_comments_and_chat.md")
    comment_history = _read("directives/history/report_comments_and_chat_2026_06_2026_08.md")

    assert entries["navigation_ia.md"]["class"] == "draft"
    assert entries["interaction_paradigm_2026_06.md"]["class"] == "history"
    assert entries["report_comments_and_chat.md"]["class"] == "canonical"
    assert entries["history/report_comments_and_chat_2026_06_2026_08.md"]["class"] == "history"
    assert "non-governing until explicitly promoted" in readme
    assert "cannot override current authority" in readme
    assert "history/report_comments_and_chat_2026_06_2026_08.md" in current_comments
    assert "The living contract is `directives/report_comments_and_chat.md`" in comment_history


def test_project_rulebook_uses_manifest_classes_as_the_directive_authority() -> None:
    agents = _read("AGENTS.md")
    assert "Only `canonical` entries own active policy or task contracts" in agents
    assert "A `runbook` executes a named canonical contract but does not redefine it" in agents
    assert "`draft` and `history` entries never govern current behavior" in agents
    assert "Executable canonical directives and runbooks" in agents
    assert "Each directive must specify" not in agents
    assert "[[root:Delegation & Subagent Calibration]]" not in agents
    assert "[[root:Evidence governance]]" not in agents
    assert "[[root:Evidence and delegation]]" in agents


def test_project_rulebook_names_windows_db_authority_and_investment_grade_gate() -> None:
    agents = _read("AGENTS.md")
    folder_contract = _read("directives/folder_structure.md")

    assert (
        r"C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\portfolio.db" in agents
    )
    assert "/Applications/earnings-summary/data/portfolio.db" in agents
    assert "is never a live, fallback, replica, or roster authority" in agents
    assert "Investment-grade financial-data invariant" in agents
    assert (
        "Acquisition completeness and extraction completeness are separate typed receipts" in agents
    )
    assert "Every consumer" in agents and "provenance-aware fact resolver" in agents
    assert "a Mac checkout-local `data/portfolio.db` is an invalid artifact" in folder_contract


def test_design_audit_is_a_runbook_and_never_creates_its_own_schedule() -> None:
    entries = _entries()
    audit = _read("directives/design_conformance_audit.md")

    assert entries["design_conformance_audit.md"]["class"] == "runbook"
    assert "already registered monthly schedule" in audit
    assert "never creates, changes, or implies a schedule" in audit


def test_llm_documents_have_non_overlapping_live_owners() -> None:
    manifest = _entries()
    assert manifest["llm_calls.md"]["class"] == "canonical"
    assert manifest["llm_evals.md"]["class"] == "canonical"
    assert manifest["model_eval_loop.md"]["class"] == "canonical"
    assert manifest["cheapest_model_routing.md"]["class"] == "canonical"
    assert manifest["gemini_backend.md"]["class"] == "runbook"
    assert manifest["openrouter_backend.md"]["class"] == "runbook"
    assert manifest["llm_evals_plan.md"]["class"] == "history"
    assert manifest["meta_eval_governance.md"]["class"] == "history"

    calls = _read("directives/llm_calls.md")
    evals = _read("directives/llm_evals.md")
    model_loop = _read("directives/model_eval_loop.md")
    routing = _read("directives/cheapest_model_routing.md")
    assert "For normal purpose-resolved Claude-family pins" in calls
    assert "production default is `codex`" in calls
    assert "operational failure falls back to the\nClaude subscription transport" in calls
    assert "explicit provider-family model IDs route to that provider" in calls
    assert "A forced backend must fail rather than silently\nswitch" in calls

    assert "quality and failure evidence" in evals
    assert "judges never switch production routing directly" in evals
    assert "the only automatic promotion/demotion writer" in model_loop
    assert "HOLD never\nswitches or reverts production" in model_loop
    assert "owns economic eligibility and ordering" in routing
    assert "qualification and promotion" in routing

    canonical = {
        "llm_calls.md": calls,
        "llm_evals.md": evals,
        "model_eval_loop.md": model_loop,
        "cheapest_model_routing.md": routing,
    }
    forbidden_history = (
        re.compile(r"\bPR\s*#?\d+\b", re.IGNORECASE),
        re.compile(r"#[0-9]{3,}"),
        re.compile(r"[A-Z]:\\\\"),
        re.compile(r"\b(?:two|both) backends\b", re.IGNORECASE),
        re.compile(r"\$\d+(?:\.\d+)?\s*/\s*M(?:Tok|tokens?)", re.IGNORECASE),
    )
    for name, text in canonical.items():
        assert not any(pattern.search(text) for pattern in forbidden_history), name

    # Provider/model brands are execution facts in the call contract, not a way
    # to qualify candidates in the quality, promotion, or economic owners.
    qualification_docs = evals + model_loop + routing
    assert not re.search(
        r"\b(?:Claude|Gemini|OpenRouter|Codex|Luna|Terra|Sol|Fable)\b",
        qualification_docs,
    )

    for runbook_name in ("gemini_backend.md", "openrouter_backend.md"):
        runbook = _read(f"directives/{runbook_name}")
        normalized = " ".join(runbook.split())
        assert "Nothing in this runbook authorizes a production route" in normalized or (
            "Setup success does not authorize production routing" in normalized
        )
        assert "forced" in normalized.lower() and "silently switching" in normalized


def test_identity_vocabulary_separates_repeat_safety_from_attempts() -> None:
    definitions = _read("DEFINITIONS.md")
    dag = _read("directives/data_pipeline_dag.md")
    provenance = _read("directives/data_provenance.md")
    documents = _read("src/models/documents.py")

    for term in (
        "Logical Idempotency Key",
        "Content Identity",
        "Observation Version",
        "Attempt Identity",
    ):
        assert f"## {term}" in definitions
    assert "Attempt Identity" in dag
    assert "Logical Idempotency Key" in dag
    assert "Content Identity" in provenance
    assert "content identity" in documents.lower()

    assert "New domain terms must be added here before being used" not in definitions
    assert "before it crosses one of those durable boundaries" in definitions

    manifest = _entries()
    identity_signals = re.compile(
        r"Logical Idempotency Key|Attempt Identity|\brun_id\b|"
        r"\bidempotency_key\b|sha256-keyed",
        re.IGNORECASE,
    )
    identity_terms = (
        "Logical Idempotency Key",
        "Content Identity",
        "Observation Version",
        "Attempt Identity",
    )
    for name, entry in manifest.items():
        if entry["class"] not in {"canonical", "runbook"}:
            continue
        text = _read(f"directives/{name}")
        if identity_signals.search(text):
            assert all(term in text for term in identity_terms), name

    active_text = "\n".join(
        _read(f"directives/{name}")
        for name, entry in manifest.items()
        if entry["class"] in {"canonical", "runbook"}
    )
    assert not re.search(r"(?im)^#{1,6}\s+Idempotency key\s*$", active_text)
    assert "sha256-keyed unique constraint" not in active_text.lower()


def test_design_behavior_uses_the_live_interaction_contract_only() -> None:
    design = _read("directives/design_language.md")
    assert "all active interaction, doorway, overlay, and dismissal behavior" in design
    assert "`directives/interaction_contract.md`" in design
    assert (
        "`directives/interaction_paradigm_2026_06.md` is record-only history and never an input"
        in design
    )


def test_project_rulebook_states_current_subscription_transport_truth() -> None:
    agents = _read("AGENTS.md")
    assert "Codex membership transport" in agents
    assert "Claude subscription fallback" in agents
    assert "src/llm/cli.py` → subscription `claude` CLI" not in agents
