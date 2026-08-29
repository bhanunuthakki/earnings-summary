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

    expected_verdicts = {
        "L0": {
            "idea-evaluator": "PASS",
            "product-feature": "ADVISORY",
            "architecture-reviewer": "ADVISORY",
            "legal-compliance": "ADVISORY",
            "finops-pricing": "ADVISORY",
        },
        "L1": {
            "product-feature": "PASS",
            "architecture-reviewer": "PASS",
            "data-foundation": "PASS",
            "qa-test-strategy": "PASS",
            "ux-design": "HOLD",
            "frontend-web": "HOLD",
            "llm-evals-orchestrator": "HOLD",
            "sec-appsec": "HOLD",
            "sec-authz": "HOLD",
            "sec-llm": "PASS",
            "api-surface-designer": "ADVISORY",
            "legal-compliance": "ADVISORY",
            "operations-readiness": "BLOCK",
            "docs-support-readiness": "PASS",
            "finops-pricing": "ADVISORY",
        },
    }
    assert {
        rung: {name: gate["verdict"] for name, gate in gates.items()}
        for rung, gates in data["gates"].items()
    } == expected_verdicts

    assert len(data["capabilities"]) == 1
    capability = data["capabilities"][0]
    receipt_id = "codex-gpt-5-6-sol-high-2026-08-28-v1"
    assert capability["receipt_id"] == receipt_id
    assert capability["status"] == "AVAILABLE"
    assert capability["role"] == "blocking-specialist"
    assert capability["receipt_hash"] == (
        "sha256:f33b18c36076d466f391bacd8a348fb960a4673ac6452da0fbb2dd4049aee64a"
    )
    qualified_rubrics = set(capability["qualified_rubrics"])

    for rung in data["gates"].values():
        for expert, gate in rung.items():
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
                assert gate["capability_receipt"] == receipt_id
                assert expert in qualified_rubrics
                assert gate["open_findings"] == []
            elif gate["verdict"] == "HOLD":
                assert gate["verdict_basis"] == "none"
                assert gate["capability_receipt"] == ""
                assert gate["open_findings"] == []
            elif gate["verdict"] == "BLOCK":
                assert gate["verdict_basis"] == "model_receipt"
                assert gate["capability_receipt"] == receipt_id
                assert expert in qualified_rubrics
                assert gate["open_findings"]
            else:
                raise AssertionError(f"unexpected verdict: {gate['verdict']}")


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
