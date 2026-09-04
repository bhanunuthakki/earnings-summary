"""Fail-closed comparison of raw pytest/CI performance receipts.

``test_ci_performance`` intentionally records one raw run.  This module is the
boundary for comparing two such runs: it validates the receipts again, checks
that they describe the same experiment, and refuses to turn one observation
per side into a performance conclusion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .test_ci_performance import (
    TestCIPerformanceReceipt,
    cohort_identity,
    config_identity,
)

PairStatus = Literal["HOLD", "INVALID"]

_UNPAIRED_REASON = "single raw run is unpaired; paired evidence is required"
_ALLOWED_RAW_HOLDS = frozenset(
    {
        _UNPAIRED_REASON,
        "cache_state is unknown",
        "network isolation is requested-not-proven",
        "cache evidence is declared-only",
    }
)


class PairedTestCIPerformanceReceipt(BaseModel):
    """The typed result of attempting to compare two raw receipts.

    Median and delta values remain ``None`` for the current one-run input
    contract.  They are fields rather than an omitted ad-hoc value so callers
    cannot mistake a held comparison for a zero delta.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "test-ci-pairing/v1"
    status: PairStatus
    comparable: bool = False
    baseline_attempt_id: str | None
    current_attempt_id: str | None
    baseline_source_sha256: str | None
    current_source_sha256: str | None
    baseline_revision: str | None
    current_revision: str | None
    cohort_sha256: str | None
    config_sha256: str | None
    cache_state: str | None
    sample_count_per_side: int = Field(ge=0)
    baseline_median_seconds: float | None = Field(default=None, ge=0)
    current_median_seconds: float | None = Field(default=None, ge=0)
    delta_seconds: float | None = None
    hold_reasons: tuple[str, ...]
    invalid_reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_admission(self) -> PairedTestCIPerformanceReceipt:
        if self.comparable and self.status != "HOLD":
            raise ValueError("one-run pairing cannot be admitted as comparable")
        if self.comparable and self.sample_count_per_side < 2:
            raise ValueError("comparable evidence requires repeated samples")
        if not self.comparable and any(
            value is not None
            for value in (
                self.baseline_median_seconds,
                self.current_median_seconds,
                self.delta_seconds,
            )
        ):
            raise ValueError("held or invalid comparisons cannot emit timing values")
        return self


# Short aliases keep the boundary discoverable for callers that use the
# generic ``PairingEvaluation`` terminology.
PairingEvaluation = PairedTestCIPerformanceReceipt


class _ParsedReceipt:
    def __init__(
        self,
        receipt: TestCIPerformanceReceipt | None,
        revision: str | None,
        errors: tuple[str, ...],
    ) -> None:
        self.receipt = receipt
        self.revision = revision
        self.errors = errors


def _payload(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    if isinstance(value, (str, bytes, bytearray)):
        decoded: object = json.loads(value)
        if isinstance(decoded, dict):
            return cast(Mapping[str, object], decoded)
    raise ValueError("receipt payload must be a JSON object or mapping")


def _parse_receipt(value: object, side: str) -> _ParsedReceipt:
    try:
        payload = dict(_payload(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _ParsedReceipt(None, None, (f"{side} receipt is not a JSON object",))

    try:
        receipt = TestCIPerformanceReceipt.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        detail = ", ".join(fields[:4]) or "schema"
        return _ParsedReceipt(
            None, None, (f"{side} receipt failed structural validation ({detail})",)
        )
    return _ParsedReceipt(receipt, receipt.revision, ())


def _raw_holds_are_permitted(receipt: TestCIPerformanceReceipt, side: str) -> tuple[str, ...]:
    reasons = tuple(receipt.hold_reasons)
    unexpected = tuple(reason for reason in reasons if reason not in _ALLOWED_RAW_HOLDS)
    if receipt.evidence_status != "hold":
        return (f"{side} evidence status is {receipt.evidence_status}, not hold",)
    if receipt.paired:
        return (f"{side} receipt claims paired evidence",)
    if _UNPAIRED_REASON not in reasons:
        return (f"{side} receipt is missing the required raw/unpaired hold",)
    if unexpected:
        return (f"{side} hold contains non-declarative failure reasons: {', '.join(unexpected)}",)
    return ()


def _receipt_nodes(receipt: TestCIPerformanceReceipt, side: str) -> tuple[str, ...]:
    errors: list[str] = []
    worker_ids = [worker.worker_id for worker in receipt.workers]
    if len(worker_ids) != len(set(worker_ids)):
        errors.append(f"{side} worker identifiers are not unique")
    seen: set[str] = set()
    overlap: set[str] = set()
    for worker in receipt.workers:
        worker_nodes = set(worker.node_ids)
        overlap.update(seen.intersection(worker_nodes))
        seen.update(worker_nodes)
        if worker.cache_state != receipt.cache_state:
            errors.append(f"{side} worker cache state disagrees with receipt cache state")
    if overlap:
        errors.append(f"{side} worker node ownership overlaps")
    if len(receipt.workers) != receipt.runtime.worker_count:
        errors.append(f"{side} worker count does not match runtime declaration")
    expected_files = set(receipt.cohort.test_files)
    actual_files = {node.split("::", 1)[0] for node in seen}
    if not expected_files:
        errors.append(f"{side} cohort contains no test files")
    if actual_files != expected_files:
        errors.append(f"{side} node coverage is not complete for the frozen cohort")
    return tuple(errors)


def _compatibility_reasons(
    baseline: TestCIPerformanceReceipt,
    current: TestCIPerformanceReceipt,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if baseline.cohort_sha256 != cohort_identity(baseline.cohort):
        reasons.append("baseline cohort identity is internally inconsistent")
    if current.cohort_sha256 != cohort_identity(current.cohort):
        reasons.append("current cohort identity is internally inconsistent")
    if baseline.cohort_sha256 != current.cohort_sha256 or baseline.cohort != current.cohort:
        reasons.append("cohorts are not exactly compatible")
    if baseline.config_sha256 != config_identity(baseline.configuration):
        reasons.append("baseline configuration identity is internally inconsistent")
    if current.config_sha256 != config_identity(current.configuration):
        reasons.append("current configuration identity is internally inconsistent")
    if (
        baseline.config_sha256 != current.config_sha256
        or baseline.configuration != current.configuration
    ):
        reasons.append("configurations are not exactly compatible")
    if baseline.runtime != current.runtime:
        reasons.append("runtime identities are not exactly compatible")
    if baseline.cache_state != current.cache_state:
        reasons.append("cache states are not exactly compatible")
    if baseline.network_isolation != current.network_isolation:
        reasons.append("network isolation declarations are not exactly compatible")
    if baseline.cache_evidence != current.cache_evidence:
        reasons.append("cache evidence declarations are not exactly compatible")
    return tuple(reasons)


def evaluate_test_ci_pair(
    baseline_raw: object,
    current_raw: object,
) -> PairedTestCIPerformanceReceipt:
    """Evaluate two raw receipt payloads without inventing a performance result."""

    baseline = _parse_receipt(baseline_raw, "baseline")
    current = _parse_receipt(current_raw, "current")
    structural_errors = baseline.errors + current.errors
    common = {
        "baseline_attempt_id": baseline.receipt.attempt_id if baseline.receipt else None,
        "current_attempt_id": current.receipt.attempt_id if current.receipt else None,
        "baseline_source_sha256": baseline.receipt.source_sha256 if baseline.receipt else None,
        "current_source_sha256": current.receipt.source_sha256 if current.receipt else None,
        "baseline_revision": baseline.revision,
        "current_revision": current.revision,
        "cohort_sha256": baseline.receipt.cohort_sha256 if baseline.receipt else None,
        "config_sha256": baseline.receipt.config_sha256 if baseline.receipt else None,
        "cache_state": baseline.receipt.cache_state if baseline.receipt else None,
        "sample_count_per_side": 1 if baseline.receipt and current.receipt else 0,
        "baseline_median_seconds": None,
        "current_median_seconds": None,
        "delta_seconds": None,
    }
    if structural_errors:
        return PairedTestCIPerformanceReceipt.model_validate(
            {
                **common,
                "status": "INVALID",
                "hold_reasons": (),
                "invalid_reasons": structural_errors,
            }
        )
    assert baseline.receipt is not None
    assert current.receipt is not None
    errors = list(_raw_holds_are_permitted(baseline.receipt, "baseline"))
    errors.extend(_raw_holds_are_permitted(current.receipt, "current"))
    if baseline.receipt.execution_outcome != "passed":
        errors.append(f"baseline execution outcome is {baseline.receipt.execution_outcome}")
    if current.receipt.execution_outcome != "passed":
        errors.append(f"current execution outcome is {current.receipt.execution_outcome}")
    errors.extend(_receipt_nodes(baseline.receipt, "baseline"))
    errors.extend(_receipt_nodes(current.receipt, "current"))
    baseline_nodes = {node for worker in baseline.receipt.workers for node in worker.node_ids}
    current_nodes = {node for worker in current.receipt.workers for node in worker.node_ids}
    if baseline_nodes != current_nodes:
        errors.append("baseline and current node coverage differ")
    errors.extend(_compatibility_reasons(baseline.receipt, current.receipt))
    if baseline.receipt.source_sha256 is None or current.receipt.source_sha256 is None:
        errors.append("source identity is missing")
    if baseline.receipt.config_sha256 is None or current.receipt.config_sha256 is None:
        errors.append("configuration identity is missing")
    if baseline.revision is None or current.revision is None:
        errors.append("revision identity is missing; paired comparison is held")
    elif (baseline.receipt.source_sha256, baseline.revision) == (
        current.receipt.source_sha256,
        current.revision,
    ):
        errors.append("baseline and current source+revision identities are identical")
    if baseline.receipt.cache_state == "unknown":
        errors.append(
            "cache state is unknown; declared-only cache evidence cannot support comparison"
        )
    if baseline.receipt.cache_evidence == "declared-only":
        errors.append("cache evidence is declared-only")
    if baseline.receipt.network_isolation == "requested-not-proven":
        errors.append("network isolation is requested-not-proven")
    errors.append("one raw sample per side is insufficient; repeated samples are required")
    return PairedTestCIPerformanceReceipt.model_validate(
        {
            **common,
            "status": "HOLD",
            "hold_reasons": tuple(dict.fromkeys(errors)),
            "invalid_reasons": (),
        }
    )


evaluate_pair = evaluate_test_ci_pair


def write_pairing_receipt(receipt: PairedTestCIPerformanceReceipt, destination: str | Path) -> None:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
