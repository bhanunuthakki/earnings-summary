"""Unit Tests: Hardening State Integrity and Audit Invariants (BHA-58)."""

from __future__ import annotations

import json
from pathlib import Path


def test_harden_state_json_structure() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    state_file = repo_root / ".harden" / "state.json"
    assert state_file.exists(), ".harden/state.json must exist"

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data.get("current_rung") == "L1"
    assert "controlling_commit_sha" in data
    assert "last_audit_timestamp" in data

    gates = data.get("gates", {}).get("L1", {})
    required_reviewers = [
        "architecture-reviewer",
        "data-engineer",
        "llm-evals-orchestrator",
        "qa-test-strategy",
    ]
    for rev in required_reviewers:
        assert rev in gates, f"Missing reviewer {rev} in L1 gates"
        assert gates[rev].get("verdict") == "PASS"
        assert gates[rev].get("open_findings") == 0


def test_harden_state_has_operational_caveats() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    state_file = repo_root / ".harden" / "state.json"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    arch = data["gates"]["L1"]["architecture-reviewer"]
    assert "operational_dependencies" in arch
    assert any("BHA-19" in dep for dep in arch["operational_dependencies"])
