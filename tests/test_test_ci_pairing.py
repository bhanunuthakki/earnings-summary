from __future__ import annotations

import hashlib
import json
from typing import cast

import pytest

from src.quality.test_ci_pairing import aggregate_test_ci_pairs, evaluate_test_ci_pair
from src.quality.test_ci_performance import (
    ArtifactIdentity,
    FrozenTestCohort,
    PhaseTimings,
    RuntimeIdentity,
    WorkerEvidence,
    cohort_identity,
    config_identity,
    node_identity,
)
from src.quality.test_ci_performance import TestCIPerformanceReceipt as _TestCIPerformanceReceipt
from src.quality.test_ci_performance import TestCounts as _TestCounts

_REV_A = "1" * 40
_REV_B = "2" * 40


def _receipt(
    *, source: str = "a" * 64, attempt: str = "attempt-a", revision: str | None = None
) -> dict[str, object]:
    cohort = FrozenTestCohort(kind="full-suite", test_files=("tests/test_a.py",))
    nodes = ("tests/test_a.py::test_one",)
    worker = WorkerEvidence(
        worker_id="gw0",
        node_ids=nodes,
        node_id_sha256=node_identity(nodes),
        counts=_TestCounts(passed=1, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0),
        timings=PhaseTimings(
            collection_seconds=0.1,
            setup_seconds=0.1,
            call_seconds=0.1,
            teardown_seconds=0.1,
        ),
        elapsed_seconds=0.4,
        cache_state="cold",
    )
    configuration = (
        ArtifactIdentity(path="pyproject.toml", sha256="b" * 64),
        ArtifactIdentity(path="requirements.lock", sha256="c" * 64),
        ArtifactIdentity(path="tests/conftest.py", sha256="d" * 64),
        ArtifactIdentity(path=".github/workflows/ci.yml", sha256="e" * 64),
    )
    runtime = RuntimeIdentity(
        python_version="3.11.0",
        python_implementation="CPython",
        platform="test-platform",
        machine="test-machine",
        cpu_count=4,
        pytest_version="8.0",
        xdist_version="3.0",
        worker_count=1,
    )
    payload: dict[str, object] = _TestCIPerformanceReceipt(
        attempt_id=attempt,
        revision=revision,
        cohort=cohort,
        source_sha256=source,
        config_sha256=config_identity(configuration),
        configuration=configuration,
        runtime=runtime,
        cohort_sha256=cohort_identity(cohort),
        execution_outcome="passed",
        evidence_status="hold",
        hold_reasons=("single raw run is unpaired; paired evidence is required",),
        workers=(worker,),
        cache_state="cold",
    ).model_dump(mode="json")
    return payload


def test_single_samples_hold_without_median_or_delta() -> None:
    result = evaluate_test_ci_pair(
        _receipt(revision=_REV_A),
        _receipt(source="f" * 64, attempt="attempt-b", revision=_REV_B),
    )
    assert result.status == "HOLD"
    assert result.comparable is False
    assert result.baseline_median_seconds is None
    assert result.current_median_seconds is None
    assert result.delta_seconds is None
    assert "repeated samples" in " ".join(result.hold_reasons)


def test_forged_pairing_claim_is_rejected_structurally() -> None:
    baseline = _receipt(revision=_REV_A)
    current = _receipt(source="f" * 64, attempt="attempt-b", revision=_REV_B)
    baseline["paired"] = True
    result = evaluate_test_ci_pair(baseline, current)
    assert result.status == "INVALID"
    assert "structural validation" in " ".join(result.invalid_reasons)


def test_same_source_and_revision_is_held() -> None:
    result = evaluate_test_ci_pair(
        _receipt(revision=_REV_A), _receipt(attempt="attempt-b", revision=_REV_A)
    )
    assert result.status == "HOLD"
    assert "identical" in " ".join(result.hold_reasons)


def test_cohort_config_runtime_and_cache_mismatch_is_held() -> None:
    baseline = _receipt(revision=_REV_A)
    current = _receipt(source="f" * 64, attempt="attempt-b", revision=_REV_B)
    current["cohort"] = {"kind": "full-suite", "test_files": ["tests/test_other.py"]}
    current["cohort_sha256"] = hashlib.sha256(
        json.dumps(current["cohort"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    result = evaluate_test_ci_pair(baseline, current)
    assert result.status == "HOLD"
    reasons = " ".join(result.hold_reasons)
    assert "cohort" in reasons
    assert "coverage" in reasons


def test_config_runtime_and_cache_mismatches_are_not_paired() -> None:
    baseline = _receipt(revision=_REV_A)
    current = _receipt(source="f" * 64, attempt="attempt-b", revision=_REV_B)
    current["config_sha256"] = "f" * 64
    runtime = current["runtime"]
    assert isinstance(runtime, dict)
    runtime["python_version"] = "3.12.0"
    current["cache_state"] = "warm"
    result = evaluate_test_ci_pair(baseline, current)
    assert result.status == "HOLD"
    reasons = " ".join(result.hold_reasons)
    assert "configurations are not exactly compatible" in reasons
    assert "runtime identities are not exactly compatible" in reasons
    assert "cache states are not exactly compatible" in reasons


def test_invalid_raw_receipt_is_invalid() -> None:
    result = evaluate_test_ci_pair({"schema_version": "forged"}, _receipt(revision=_REV_B))
    assert result.status == "INVALID"
    assert result.invalid_reasons


def test_repeated_aggregate_discards_warmup_and_computes_bootstrap() -> None:
    baseline: list[object] = []
    current: list[object] = []
    for index in range(8):
        left = _receipt(attempt=f"b-{index}", revision=_REV_A)
        right = _receipt(source="f" * 64, attempt=f"c-{index}", revision=_REV_B)
        left["network_isolation"] = right["network_isolation"] = "proven"
        left["cache_evidence"] = right["cache_evidence"] = "measured"
        left["output_sha256"] = right["output_sha256"] = f"{index + 1:064x}"
        left["network_isolation_proof_sha256"] = right["network_isolation_proof_sha256"] = (
            hashlib.sha256(f"network-isolation/v1:{left['output_sha256']}".encode()).hexdigest()
        )
        left["cache_evidence_proof_sha256"] = right["cache_evidence_proof_sha256"] = hashlib.sha256(
            f"cache-observation/v1:{left['output_sha256']}".encode()
        ).hexdigest()
        left["process_wall_seconds"] = 1.0
        right["process_wall_seconds"] = 0.9
        baseline.append(left)
        current.append(right)
    result = aggregate_test_ci_pairs(baseline, current)
    assert result.status == "HOLD"
    assert "attestations are unavailable" in " ".join(result.hold_reasons)
    assert result.sample_count_per_side == 7
    assert result.bootstrap_ci_95 is not None
    assert result.delta_seconds == pytest.approx(-0.1)


def test_aggregate_accepts_warmup_plus_21_raw_receipts() -> None:
    baseline: list[object] = []
    current: list[object] = []
    for index in range(22):
        left = _receipt(attempt=f"b22-{index}", revision=_REV_A)
        right = _receipt(source="f" * 64, attempt=f"c22-{index}", revision=_REV_B)
        left["process_wall_seconds"] = right["process_wall_seconds"] = 1.0
        baseline.append(left)
        current.append(right)
    result = aggregate_test_ci_pairs(baseline, current)
    assert result.sample_count_per_side == 21
    assert result.status == "HOLD"


def test_aggregate_holds_when_experiment_declarations_change_mid_series() -> None:
    baseline: list[object] = []
    current: list[object] = []
    for index in range(8):
        left = _receipt(attempt=f"frozen-b-{index}", revision=_REV_A)
        right = _receipt(source="f" * 64, attempt=f"frozen-c-{index}", revision=_REV_B)
        left["network_isolation"] = right["network_isolation"] = "proven"
        left["cache_evidence"] = right["cache_evidence"] = "measured"
        left["output_sha256"] = right["output_sha256"] = f"{index + 1:064x}"
        left["network_isolation_proof_sha256"] = right["network_isolation_proof_sha256"] = (
            hashlib.sha256(f"network-isolation/v1:{left['output_sha256']}".encode()).hexdigest()
        )
        left["cache_evidence_proof_sha256"] = right["cache_evidence_proof_sha256"] = hashlib.sha256(
            f"cache-observation/v1:{left['output_sha256']}".encode()
        ).hexdigest()
        left["process_wall_seconds"] = right["process_wall_seconds"] = 1.0
        baseline.append(left)
        current.append(right)
    changed = cast(dict[str, object], baseline[3])
    changed_runtime = cast(dict[str, object], changed["runtime"])
    changed_runtime["worker_count"] = 2
    result = aggregate_test_ci_pairs(baseline, current)
    assert result.status == "HOLD"
    assert "baseline experiment declarations change" in " ".join(result.hold_reasons)


def test_missing_revision_is_an_explicit_hold() -> None:
    result = evaluate_test_ci_pair(_receipt(), _receipt(source="f" * 64, attempt="attempt-b"))
    assert result.status == "HOLD"
    assert "revision identity is missing" in " ".join(result.hold_reasons)
