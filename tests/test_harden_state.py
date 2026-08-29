"""Unit Tests: Hardening State Integrity and Audit Invariants (BHA-58)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _load_state() -> tuple[Path, dict[str, Any]]:
    repo_root = Path(__file__).resolve().parent.parent
    state_file = repo_root / ".harden" / "state.json"
    assert state_file.exists(), ".harden/state.json must exist"
    return repo_root, json.loads(state_file.read_text(encoding="utf-8"))


def test_harden_state_json_structure() -> None:
    repo_root, data = _load_state()
    assert data["$schema"] == "internal://harden-state/v2"
    assert data["target_rung"] == "L1"
    assert set(data) == {
        "$schema",
        "target_rung",
        "profile",
        "fingerprints",
        "capabilities",
        "gates",
    }
    assert set(data["gates"]) == {"L0", "L1"}

    l1 = data["gates"]["L1"]
    required_passes = {
        "product-feature",
        "architecture-reviewer",
        "data-foundation",
        "qa-test-strategy",
        "sec-llm",
        "docs-support-readiness",
    }
    required_holds = {
        "ux-design",
        "frontend-web",
        "llm-evals-orchestrator",
        "sec-appsec",
        "sec-authz",
    }
    assert {name for name, gate in l1.items() if gate["verdict"] == "PASS"} == required_passes
    assert {name for name, gate in l1.items() if gate["verdict"] == "HOLD"} == required_holds

    for rung in data["gates"].values():
        for gate in rung.values():
            assert isinstance(gate["open_findings"], list)
            assert set(gate["evidence"]) == set(gate["evidence_hashes"])
            for relative_path, expected_hash in gate["evidence_hashes"].items():
                evidence_path = (repo_root / relative_path).resolve()
                assert evidence_path.is_relative_to(repo_root.resolve())
                assert evidence_path.is_file()
                actual_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                assert expected_hash == f"sha256:{actual_hash}"
            if gate["verdict"] in {"PASS", "ADVISORY"}:
                assert gate["verdict_basis"] == "model_receipt"
                assert gate["capability_receipt"]
                assert gate["open_findings"] == []
            elif gate["verdict"] == "HOLD":
                assert gate["verdict_basis"] == "none"
                assert gate["capability_receipt"] == ""
                assert gate["open_findings"] == []


def test_harden_state_has_operational_caveats() -> None:
    repo_root, data = _load_state()
    operations = data["gates"]["L1"]["operations-readiness"]
    expected_findings = {
        "OPS-BHA20-TRACKER-LIVE",
        "OPS-BHA20-SCHEDULER-ACL",
    }
    assert operations["verdict"] == "BLOCK"
    assert operations["verdict_basis"] == "model_receipt"
    assert set(operations["open_findings"]) == expected_findings

    live_path = repo_root / "docs/hardening/v2/evidence/windows-live-gap-2026-08-29.json"
    assert live_path.relative_to(repo_root).as_posix() in operations["evidence"]
    live = json.loads(live_path.read_text(encoding="utf-8"))
    assert live["overall"] == "BLOCK"
    assert live["portfolio"]["tracker_state"] == "unavailable"
    assert live["company_desk"]["position_state"] == "unavailable"
    assert {finding["id"] for finding in live["findings"]} == expected_findings
    assert {finding["owner"] for finding in live["findings"]} == {"BHA-20"}
