"""Fail-closed comparison of raw pytest/CI performance receipts.

``test_ci_performance`` intentionally records one raw run.  This module is the
boundary for comparing two such runs: it validates the receipts again, checks
that they describe the same experiment, and refuses to turn one observation
per side into a performance conclusion.
"""

from __future__ import annotations

import json
import random
import statistics
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .test_ci_performance import (
    TestCIPerformanceReceipt,
    cohort_identity,
    config_identity,
)

PairStatus = Literal["HOLD", "INVALID", "PASS"]

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
    baseline_mad_seconds: float | None = None
    current_mad_seconds: float | None = None
    bootstrap_ci_95: tuple[float, float] | None = None
    regression_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_admission(self) -> PairedTestCIPerformanceReceipt:
        if self.comparable and self.status not in {"HOLD", "PASS"}:
            raise ValueError("comparable pairing must be HOLD or PASS")
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


def aggregate_test_ci_pairs(
    baseline_raw: list[object], current_raw: list[object], *, max_runs: int = 22
) -> PairedTestCIPerformanceReceipt:
    """Aggregate repeated raw receipts after one warmup, fail-closed."""
    if len(baseline_raw) < 8 or len(current_raw) < 8:
        return PairedTestCIPerformanceReceipt(
            status="HOLD",
            baseline_attempt_id=None,
            current_attempt_id=None,
            baseline_source_sha256=None,
            current_source_sha256=None,
            baseline_revision=None,
            current_revision=None,
            cohort_sha256=None,
            config_sha256=None,
            cache_state=None,
            sample_count_per_side=0,
            hold_reasons=(
                "at least 8 raw runs per side are required (one warmup plus 7 measured)",
            ),
            invalid_reasons=(),
        )
    baseline = [_parse_receipt(item, "baseline") for item in baseline_raw[:max_runs]]
    current = [_parse_receipt(item, "current") for item in current_raw[:max_runs]]
    if len(baseline) != len(current):
        return PairedTestCIPerformanceReceipt(
            status="HOLD",
            baseline_attempt_id=None,
            current_attempt_id=None,
            baseline_source_sha256=None,
            current_source_sha256=None,
            baseline_revision=None,
            current_revision=None,
            cohort_sha256=None,
            config_sha256=None,
            cache_state=None,
            sample_count_per_side=0,
            hold_reasons=("baseline/current raw run lengths differ",),
            invalid_reasons=(),
        )
    errors = tuple(error for parsed in (*baseline, *current) for error in parsed.errors)
    if errors or any(parsed.receipt is None for parsed in (*baseline, *current)):
        return PairedTestCIPerformanceReceipt(
            status="INVALID",
            baseline_attempt_id=None,
            current_attempt_id=None,
            baseline_source_sha256=None,
            current_source_sha256=None,
            baseline_revision=None,
            current_revision=None,
            cohort_sha256=None,
            config_sha256=None,
            cache_state=None,
            sample_count_per_side=0,
            hold_reasons=(),
            invalid_reasons=errors or ("receipt parsing failed",),
        )
    b = [parsed.receipt for parsed in baseline[1:] if parsed.receipt]
    c = [parsed.receipt for parsed in current[1:] if parsed.receipt]
    assert b and c
    reasons: list[str] = []
    for side, runs in (("baseline", b), ("current", c)):
        identities = {(run.source_sha256, run.revision) for run in runs}
        attempts = [run.attempt_id for run in runs]
        if len(identities) != 1:
            reasons.append(f"{side} source/revision identity changes across repeats")
        if len(attempts) != len(set(attempts)):
            reasons.append(f"{side} attempt identifiers are not unique")
        # A digest supplied inside a receipt is not an attestation: a caller
        # can rewrite both the observation and its digest.  The approved
        # runner must provide an independently retained OS/network/cache
        # trace before this evaluator may admit PASS.
        reasons.append(f"{side} runner network/cache attestations are unavailable")
        for run in runs:
            reasons.extend(_raw_holds_are_permitted(run, side))
            reasons.extend(_receipt_nodes(run, side))
            if run.execution_outcome != "passed":
                reasons.append(f"{side} execution outcome is {run.execution_outcome}")
    for left, right in zip(b, c, strict=True):
        reasons.extend(_compatibility_reasons(left, right))
    if len(b) < 7 or len(c) < 7:
        reasons.append("at least 7 measured repeats per side are required")
    if any(
        (run.source_sha256, run.revision) == (other.source_sha256, other.revision)
        for run in b
        for other in c
    ):
        reasons.append("baseline and current identify the same source revision")

    def elapsed(run: TestCIPerformanceReceipt) -> float:
        return run.process_wall_seconds or max(worker.elapsed_seconds for worker in run.workers)

    bv, cv = [elapsed(run) for run in b], [elapsed(run) for run in c]
    bm, cm = statistics.median(bv), statistics.median(cv)
    bmad = statistics.median(abs(v - bm) for v in bv)
    cmad = statistics.median(abs(v - cm) for v in cv)
    if bmad > bm * 0.05 or cmad > cm * 0.05:
        reasons.append("timing samples remain unstable after warmup")
    if any(cval > bval * 1.10 for bval, cval in zip(bv, cv, strict=True)):
        reasons.append("current run exceeds baseline by more than 10%")
    for left, right in zip(b, c, strict=True):
        if left.runtime.worker_count != right.runtime.worker_count:
            reasons.append("worker-count companion differs")
        left_workers = {worker.worker_id: worker for worker in left.workers}
        right_workers = {worker.worker_id: worker for worker in right.workers}
        for worker_id in left_workers.keys() & right_workers.keys():
            old, new = left_workers[worker_id], right_workers[worker_id]
            old_metrics = (
                old.peak_rss_bytes or 0,
                old.timings.collection_seconds,
                old.timings.setup_seconds,
                old.timings.call_seconds,
                old.timings.teardown_seconds,
                old.timings.migrated_db_template_build_seconds,
                old.timings.migrated_db_template_copy_seconds,
            )
            new_metrics = (
                new.peak_rss_bytes or 0,
                new.timings.collection_seconds,
                new.timings.setup_seconds,
                new.timings.call_seconds,
                new.timings.teardown_seconds,
                new.timings.migrated_db_template_build_seconds,
                new.timings.migrated_db_template_copy_seconds,
            )
            if any(n > o * 1.10 for o, n in zip(old_metrics, new_metrics, strict=True) if o > 0):
                reasons.append(f"worker companion regression >10% ({worker_id})")
    deltas = [cval - bval for bval, cval in zip(bv, cv, strict=True)]
    rng = random.Random(0)
    estimates = [statistics.median(rng.choices(deltas, k=len(deltas))) for _ in range(1000)]
    estimates.sort()
    ci = (estimates[25], estimates[974])
    if not all(run.cache_state in {"cold", "warm"} for run in (*b, *c)):
        reasons.append("cache state is not proven")
    if any(run.network_isolation != "proven" for run in (*b, *c)):
        reasons.append("network isolation is not proven")
    if ci[1] >= 0:
        reasons.append("bootstrap confidence interval includes zero; improvement is unproven")
    status: PairStatus = "PASS" if not reasons and ci[1] < 0 else "HOLD"
    return PairedTestCIPerformanceReceipt(
        status=status,
        comparable=True,
        baseline_attempt_id=b[0].attempt_id,
        current_attempt_id=c[0].attempt_id,
        baseline_source_sha256=b[0].source_sha256,
        current_source_sha256=c[0].source_sha256,
        baseline_revision=b[0].revision,
        current_revision=c[0].revision,
        cohort_sha256=b[0].cohort_sha256,
        config_sha256=b[0].config_sha256,
        cache_state=b[0].cache_state,
        sample_count_per_side=min(len(b), len(c)),
        baseline_median_seconds=bm,
        current_median_seconds=cm,
        delta_seconds=cm - bm,
        hold_reasons=tuple(dict.fromkeys(reasons)),
        invalid_reasons=(),
        baseline_mad_seconds=bmad,
        current_mad_seconds=cmad,
        bootstrap_ci_95=ci,
    )


evaluate_pair = evaluate_test_ci_pair


def write_pairing_receipt(receipt: PairedTestCIPerformanceReceipt, destination: str | Path) -> None:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")
