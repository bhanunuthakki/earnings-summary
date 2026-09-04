from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.quality.test_ci_performance import (
    FrozenTestCohort,
    PhaseTimings,
    WorkerEvidence,
    cohort_identity,
    node_identity,
    receipt_from_fragments,
    source_identity,
)
from src.quality.test_ci_performance import (
    TestCounts as PerformanceCounts,
)


def _worker(
    node_ids: tuple[str, ...] = ("tests/test_a.py::test_one",),
    *,
    worker_id: str = "gw0",
    cache_state: str = "cold",
) -> WorkerEvidence:
    return WorkerEvidence(
        worker_id=worker_id,
        node_ids=node_ids,
        node_id_sha256=node_identity(node_ids),
        counts=PerformanceCounts(
            passed=len(node_ids), failed=0, errors=0, skipped=0, xfailed=0, xpassed=0
        ),
        timings=PhaseTimings(
            collection_seconds=0.2,
            setup_seconds=0.1,
            call_seconds=0.8,
            teardown_seconds=0.1,
        ),
        elapsed_seconds=1.2,
        peak_rss_bytes=100,
        cache_state=cache_state if cache_state in {"cold", "warm", "unknown"} else "unknown",
    )


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in (
        "pyproject.toml",
        "requirements.lock",
        "tests/conftest.py",
        ".github/workflows/ci.yml",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return tmp_path


def _cohort() -> FrozenTestCohort:
    return FrozenTestCohort(kind="full-suite", test_files=("tests/test_a.py",))


def test_cohort_is_frozen_and_identity_is_exact() -> None:
    cohort = FrozenTestCohort(
        kind="ci-shard",
        source_shard=1,
        source_shards=8,
        split_count=1,
        split_part=0,
        test_files=("tests/test_a.py",),
    )
    assert cohort_identity(cohort) == cohort_identity(cohort.model_copy())


@pytest.mark.parametrize(
    "values",
    [
        {"kind": "full-suite", "source_shard": 1},
        {
            "kind": "ci-shard",
            "source_shard": 0,
            "source_shards": 8,
            "split_count": 1,
            "split_part": 0,
        },
        {
            "kind": "ci-shard",
            "source_shard": 1,
            "source_shards": 8,
            "split_count": 1,
            "split_part": 1,
        },
        {"kind": "full-suite", "test_files": ("tests/z.py", "tests/a.py")},
    ],
)
def test_invalid_cohort_shapes_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FrozenTestCohort.model_validate(values)


def test_worker_rejects_tampered_node_hash_and_count() -> None:
    payload = _worker().model_dump()
    payload["node_id_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="node_id_sha256"):
        WorkerEvidence.model_validate(payload)
    payload = _worker().model_dump()
    payload["counts"]["passed"] = 2
    with pytest.raises(ValidationError, match="outcome counts"):
        WorkerEvidence.model_validate(payload)


def test_single_successful_run_is_hold_and_cannot_be_paired(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    receipt = receipt_from_fragments(
        root,
        _cohort(),
        [_worker()],
        attempt_id="attempt-1",
        execution_outcome="passed",
        cache_state="cold",
        worker_count=1,
    )
    assert receipt.execution_outcome == "passed"
    assert receipt.evidence_status == "hold"
    assert receipt.paired is False
    assert "unpaired" in " ".join(receipt.hold_reasons)


def test_duplicate_worker_or_node_ownership_is_invalid(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    receipt = receipt_from_fragments(
        root,
        _cohort(),
        [_worker(), _worker()],
        attempt_id="attempt-2",
        execution_outcome="passed",
        cache_state="cold",
        worker_count=2,
    )
    assert receipt.evidence_status == "invalid"
    assert "identifiers are not unique" in " ".join(receipt.hold_reasons)
    assert "ownership overlaps" in " ".join(receipt.hold_reasons)


def test_node_outside_cohort_and_cache_mismatch_are_invalid(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    receipt = receipt_from_fragments(
        root,
        _cohort(),
        [_worker(("tests/test_other.py::test_one",), cache_state="warm")],
        attempt_id="attempt-3",
        execution_outcome="passed",
        cache_state="cold",
        worker_count=1,
    )
    assert receipt.evidence_status == "invalid"
    reasons = " ".join(receipt.hold_reasons)
    assert "outside the frozen cohort" in reasons
    assert "disagrees" in reasons


def test_unknown_cache_state_is_not_inferred_from_run_shape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    receipt = receipt_from_fragments(
        root,
        _cohort(),
        [_worker(cache_state="unknown")],
        attempt_id="attempt-4",
        execution_outcome="passed",
        cache_state="unknown",
        worker_count=1,
    )
    assert receipt.cache_state == "unknown"
    assert "cache_state is unknown" in " ".join(receipt.hold_reasons)


def test_source_identity_includes_untracked_inputs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before = source_identity(root)
    untracked = root / "execution" / "new_runner.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("VALUE = 2\n")
    after = source_identity(root)
    assert before is not None
    assert after is not None
    assert after != before


def test_missing_fragment_or_configuration_is_invalid(tmp_path: Path) -> None:
    receipt = receipt_from_fragments(
        tmp_path,
        _cohort(),
        [],
        attempt_id="attempt-5",
        execution_outcome="not_run",
        cache_state="cold",
        fragment_errors=("invalid worker fragment worker-gw0.json: ValidationError",),
    )
    assert receipt.evidence_status == "invalid"
    reasons = " ".join(receipt.hold_reasons)
    assert "no valid worker evidence" in reasons
    assert "configuration identities are missing" in reasons
    assert "source identity is unavailable" in reasons
