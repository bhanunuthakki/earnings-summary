from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from quality.git_env import clean_local_git_env
from quality.roadmap_freeze_inputs import (
    FreezeInputError,
    assert_unchanged,
    load_json_input,
    strict_json,
)
from quality.roadmap_freeze_models import FreezeCoverage, FreezeReceipt, WorkIntent


def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(FreezeInputError, match="duplicate JSON key"):
        strict_json(b'{"status":"PASS","status":"HOLD"}')
    with pytest.raises(FreezeInputError, match="non-finite JSON number"):
        strict_json(b'{"seconds":NaN}')
    with pytest.raises(FreezeInputError, match="non-finite JSON number"):
        strict_json(b'{"seconds":Infinity}')


def test_strict_json_accepts_finite_numbers() -> None:
    value = strict_json(b'{"seconds":1.25}')
    assert value == {"seconds": 1.25}
    assert isinstance(value, dict)
    seconds = value["seconds"]
    assert isinstance(seconds, float)
    assert math.isfinite(seconds)


def test_coverage_model_rejects_forged_arithmetic() -> None:
    with pytest.raises(ValueError, match="required reduction"):
        FreezeCoverage(
            baseline_modules_over_1000=96,
            target_modules_over_1000=35,
            required_net_reduction=60,
            planned_crossing_paths=(),
            planned_net_reduction=0,
            observed_delivered_net_reduction=None,
            unplanned_net_reduction=60,
            protected_root_intents=(),
            distinct_pr_intents=0,
        )


def test_receipt_model_rejects_forged_pass_with_hold_reasons() -> None:
    coverage = FreezeCoverage(
        baseline_modules_over_1000=0,
        target_modules_over_1000=35,
        required_net_reduction=0,
        planned_crossing_paths=(),
        planned_net_reduction=0,
        observed_delivered_net_reduction=None,
        unplanned_net_reduction=0,
        protected_root_intents=(),
        distinct_pr_intents=0,
    )
    with pytest.raises(ValueError, match="PASS freeze"):
        FreezeReceipt(
            subject_commit="a" * 40,
            subject_tree="b" * 40,
            generator_sha256="c" * 64,
            evidence=(),
            plan_path=None,
            plan_sha256=None,
            plan=None,
            owner_snapshot_sha256=None,
            owner_snapshot=None,
            candidate_census=(),
            coverage=coverage,
            artifact_status="PASS",
            program_status="HOLD",
            hold_reasons=("missing evidence",),
        )


def test_same_bytes_replacement_fails_identity_recheck(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=clean_local_git_env())
    path = root / ".tmp/input.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    loaded = load_json_input(root, path)
    path.unlink()
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(FreezeInputError, match="changed during freeze"):
        assert_unchanged(loaded)


def test_set_like_intent_fields_reject_duplicates() -> None:
    raw = {
        "intent_id": "intent-one",
        "owner_issue": "BHA-108",
        "lane": "structure-request",
        "action": "split",
        "intended_outcome": "split one module",
        "depends_on": [],
        "resources": ["repo", "repo"],
        "parallel_group": None,
        "scope_sha256": "a" * 64,
        "evidence_refs": ["architecture"],
        "acceptance_tests": ["tests/test_contract.py"],
        "candidate_ids": ["large-module:src/a.py"],
    }
    with pytest.raises(ValueError, match="resources must be unique"):
        WorkIntent.model_validate_json(json.dumps(raw))
