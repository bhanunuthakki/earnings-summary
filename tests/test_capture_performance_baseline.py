from __future__ import annotations

import sys
from pathlib import Path

from quality.performance import capture_performance_baseline

COMPANIONS = {"sql_statements": 2, "rows": 1, "elapsed_seconds": 0.01, "peak_rss_bytes": 100}


def test_baseline_receipt_has_revision_hashes_stats_and_environment() -> None:
    receipt = capture_performance_baseline(
        Path.cwd(),
        f"{sys.executable} -c \"print('ok')\"",
        samples=7,
        companion_measures=COMPANIONS,
        provenance="mac_guidance",
    )
    assert receipt.status == "PASS"
    assert receipt.hold is False
    assert receipt.revision
    assert receipt.source_sha256 and len(receipt.source_sha256) == 64
    assert receipt.config_sha256 and len(receipt.config_sha256) == 64
    assert receipt.timing.count == 7
    assert receipt.output_sha256
    assert receipt.environment["python"]


def test_failed_command_is_typed_failure_with_output_evidence() -> None:
    receipt = capture_performance_baseline(
        Path.cwd(),
        f"{sys.executable} -c \"import sys; print('bad'); sys.exit(3)\"",
        samples=7,
        companion_measures=COMPANIONS,
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
        companion_measures=COMPANIONS,
        provenance="mac_guidance",
    )
    assert len(receipt.timing_samples) == 7
    assert receipt.timing_samples[0].label == "cold"
    assert all(sample.label == "warm" for sample in receipt.timing_samples[1:])
    assert receipt.median_seconds is not None
    assert receipt.mad_seconds is not None
    assert receipt.bootstrap_ci_95 is not None
    assert receipt.stability_verdict in {"stable", "unstable"}
