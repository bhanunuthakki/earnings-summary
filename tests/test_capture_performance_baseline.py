from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from execution.capture_performance_baseline import main as capture_baseline_cli
from quality.performance import (
    CausalRunEnvelope,
    CompanionMeasures,
    RouteCausalCompanion,
    capture_performance_baseline,
)


def test_arbitrary_timing_only_command_is_held_without_causal_envelope() -> None:
    receipt = capture_performance_baseline(
        Path.cwd(),
        f"{sys.executable} -c \"print('ok')\"",
        samples=7,
        provenance="mac_guidance",
    )
    assert receipt.status == "HOLD"
    assert receipt.hold is True
    assert "validated causal evidence envelope" in " ".join(receipt.hold_reasons)
    assert receipt.revision
    assert receipt.source_sha256 and len(receipt.source_sha256) == 64
    assert receipt.config_sha256 and len(receipt.config_sha256) == 64
    assert receipt.timing.count >= 7
    assert receipt.output_sha256
    assert receipt.environment["python"]


def test_failed_command_is_typed_failure_with_output_evidence() -> None:
    receipt = capture_performance_baseline(
        Path.cwd(),
        f"{sys.executable} -c \"import sys; print('bad'); sys.exit(3)\"",
        samples=7,
        provenance="mac_guidance",
    )
    assert receipt.status == "FAIL"
    assert receipt.hold is True
    assert receipt.exit_codes == [3]
    assert receipt.output_bytes > 0
    assert "bad" in receipt.output


def test_empty_command_is_hold_without_execution() -> None:
    receipt = capture_performance_baseline(Path.cwd(), "", samples=1)
    assert receipt.status == "HOLD"
    assert receipt.hold is True
    assert receipt.timing.count == 0
    assert "empty" in " ".join(receipt.hold_reasons)


def test_adaptive_statistics_and_labels_are_present() -> None:
    receipt = capture_performance_baseline(
        Path.cwd(),
        f"{sys.executable} -c \"print('ok')\"",
        samples=7,
        provenance="mac_guidance",
    )
    # Adaptive collection may add warm repeats when the initial requested
    # cohort is noisy; the requested value is the minimum measured cohort.
    assert 7 <= len(receipt.timing_samples) <= 21
    assert receipt.timing_samples[0].label == "cold"
    assert all(sample.label == "warm" for sample in receipt.timing_samples[1:])
    assert receipt.median_seconds is not None
    assert receipt.mad_seconds is not None
    assert receipt.bootstrap_ci_95 is not None
    assert receipt.stability_verdict in {"stable", "unstable"}


def test_baseline_receipt_does_not_accept_caller_companions() -> None:
    parameters = inspect.signature(capture_performance_baseline).parameters
    assert "companion_measures" not in parameters
    assert "require_causal_envelope" not in parameters


def test_baseline_cli_rejects_forged_companion_sidecar() -> None:
    with pytest.raises(SystemExit):
        capture_baseline_cli(
            [
                "--command",
                f'{sys.executable} -c "print(\\"ok\\")"',
                "--output",
                ".tmp/quality/forged-baseline.json",
                "--samples",
                "7",
                "--provenance",
                "mac_guidance",
                "--companion-json",
                ".tmp/quality/forged-companions.json",
            ]
        )


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (CompanionMeasures, "sql_statements", -1),
        (CompanionMeasures, "rows", -1),
        (CompanionMeasures, "elapsed_seconds", 0),
        (CompanionMeasures, "peak_rss_bytes", -1),
        (CausalRunEnvelope, "sql_statements", -1),
        (CausalRunEnvelope, "rows", -1),
        (CausalRunEnvelope, "elapsed_seconds", 0),
        (CausalRunEnvelope, "peak_rss_bytes", -1),
    ],
)
def test_negative_or_nonpositive_causal_metrics_are_rejected(
    model: type[CausalRunEnvelope] | type[CompanionMeasures], field: str, value: int
) -> None:
    payload: dict[str, object] = {
        "sql_statements": 1,
        "rows": 1,
        "elapsed_seconds": 1.0,
        "peak_rss_bytes": 1,
        "alembic_revision": None,
        "query_plan_sha256": None,
        "connection_role": "none",
        "stage": "test",
        "revision": "test",
    }
    if model is CompanionMeasures:
        payload = {
            key: payload[key]
            for key in ("sql_statements", "rows", "elapsed_seconds", "peak_rss_bytes")
        }
    payload[field] = value
    with pytest.raises(ValueError):
        model.model_validate(payload)


def test_nested_route_and_stage_metrics_are_constrained() -> None:
    route = {
        "route_name": "/healthz",
        "phase": "cold",
        "method": "GET",
        "status_code": 200,
        "allowed_success_statuses": (200,),
        "elapsed_seconds": 1.0,
        "sql_statements": 1,
        "connection_count": 1,
        "response_sha256": "a" * 64,
        "auth_fixture_identity": "test",
        "fixture_sha256": "b" * 64,
        "external_call_hold_seconds": 0.0,
        "network_disabled": True,
    }
    with pytest.raises(ValueError):
        RouteCausalCompanion.model_validate({**route, "sql_statements": -1})
    with pytest.raises(ValueError):
        CausalRunEnvelope.model_validate(
            {
                "sql_statements": 1,
                "rows": 1,
                "elapsed_seconds": 1.0,
                "peak_rss_bytes": 1,
                "alembic_revision": None,
                "query_plan_sha256": None,
                "connection_role": "none",
                "stage": "test",
                "revision": "test",
                "route_companions": (route,),
                "stage_timings": {"builder": 0},
            }
        )
    with pytest.raises(ValueError):
        CausalRunEnvelope.model_validate(
            {
                "sql_statements": 1,
                "rows": 1,
                "elapsed_seconds": 1.0,
                "peak_rss_bytes": 1,
                "alembic_revision": None,
                "query_plan_sha256": None,
                "connection_role": "none",
                "stage": "test",
                "revision": "test",
                "stage_peak_rss_bytes": {"builder": -1},
            }
        )
