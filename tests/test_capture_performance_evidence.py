from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from execution.capture_performance_evidence import main as capture_cli
from quality.performance import (
    COHORT_REGISTRY,
    CausalEvidence,
    FrozenPerformanceCohort,
    capture_performance_evidence,
)


def _evidence() -> CausalEvidence:
    return CausalEvidence(
        sql_statements=20,
        rows=20,
        elapsed_seconds=0.02,
        peak_rss_bytes=1024,
        alembic_revision="head",
        query_plan_sha256="a" * 64,
        connection_role="request_scoped_read",
        stage="route-render",
    )


def _envelope_command() -> str:
    payload = {
        "sql_statements": 20,
        "rows": 20,
        "elapsed_seconds": 0.02,
        "peak_rss_bytes": 1024,
        "alembic_revision": "head",
        "query_plan_sha256": "a" * 64,
        "connection_role": "request_scoped_read",
        "stage": "route-render",
        "revision": "current",
    }
    return shlex.join([sys.executable, "-c", f"import json;print(json.dumps({payload!r}))"])


def test_route_cohort_is_frozen_and_has_causal_contract() -> None:
    cohort = FrozenPerformanceCohort(
        cohort="route_cold_warm",
        declared_command=_envelope_command(),
        route_count=20,
        route_names=tuple(f"route-{i}" for i in range(20)),
    )
    receipt = capture_performance_evidence(
        Path.cwd(),
        cohort,
        evidence=_evidence(),
        provenance="mac_guidance",
        baseline_revision="base",
        current_revision="current",
    )
    assert receipt.baseline.status == "HOLD"
    assert receipt.baseline.timing.count == 7
    assert receipt.causal_evidence.query_plan_sha256 == "a" * 64
    assert receipt.paired_identity is False
    assert FrozenPerformanceCohort.model_config.get("frozen") is True


def test_missing_cohort_companions_hold() -> None:
    cohort = FrozenPerformanceCohort(cohort="migrations", declared_command="")
    receipt = capture_performance_evidence(
        Path.cwd(),
        cohort,
        evidence=CausalEvidence(
            sql_statements=None,
            rows=None,
            elapsed_seconds=None,
            peak_rss_bytes=None,
            alembic_revision=None,
            query_plan_sha256=None,
            connection_role="none",
            stage=None,
        ),
        provenance="mac_guidance",
    )
    assert receipt.baseline.status == "HOLD"
    assert receipt.baseline.adaptive_verdict == "hold"
    assert any("alembic_revision" in reason for reason in receipt.baseline.hold_reasons)


def test_registry_is_canonical_and_invalid_envelope_holds() -> None:
    assert set(COHORT_REGISTRY) == {
        "integrity",
        "migrations",
        "route_cold_warm",
        "dcf",
        "source_analysis",
        "ci",
    }
    cohort = COHORT_REGISTRY["dcf"]
    bad = capture_performance_evidence(
        Path.cwd(),
        cohort,
        samples=7,
        provenance="mac_guidance",
        baseline_revision="base",
        current_revision="current",
    )
    assert bad.baseline.status == "HOLD"
    assert "causal evidence envelope" in " ".join(bad.baseline.hold_reasons)


def test_forged_sidecar_and_revision_labels_can_never_pass() -> None:
    """Self-reported metrics are not proof of paired revision execution."""
    receipt = capture_performance_evidence(
        Path.cwd(),
        FrozenPerformanceCohort(
            cohort="ci",
            declared_command=_envelope_command(),
        ),
        evidence=_evidence(),
        provenance="approved_windows_production_shaped",
        baseline_revision="base",
        current_revision="current",
    )
    assert receipt.baseline.status == "HOLD"
    assert receipt.paired_identity is False
    assert any("revision-aware harness" in reason for reason in receipt.baseline.hold_reasons)


def test_cli_has_no_caller_selected_workload_or_sidecar() -> None:
    """The CLI must select only the frozen registry workload."""
    with pytest.raises(SystemExit):
        capture_cli(
            [
                "--cohort",
                "ci",
                "--output",
                ".tmp/quality/forged.json",
                "--provenance",
                "mac_guidance",
                "--command",
                "python -c 'print(\"fake\")'",
            ]
        )


def test_source_analysis_runs_real_paired_revisions() -> None:
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD~1"], text=True).strip()
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    receipt = capture_performance_evidence(
        Path.cwd(),
        COHORT_REGISTRY["source_analysis"],
        samples=7,
        provenance="mac_guidance",
        baseline_revision=baseline,
        current_revision=current,
        timeout_seconds=30,
    )
    assert receipt.baseline.status == "PASS"
    assert receipt.paired_identity is True
    assert len(receipt.causal_runs) == 14
    assert {run.revision for run in receipt.causal_runs} == {baseline, current}
    assert all(run.stage == "source-analysis" for run in receipt.causal_runs)
    assert all(sample.label == "cold" for sample in receipt.baseline.timing_samples)
