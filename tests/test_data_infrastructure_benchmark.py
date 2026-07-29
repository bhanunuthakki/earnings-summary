from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from provenance.data_infrastructure_benchmark import (
    BenchmarkBudgets,
    BenchmarkConfig,
    BenchmarkIntegrityError,
    RefusedBenchmarkPathError,
    run_benchmark,
    verify_report_sha256,
    verify_stream_chain,
    write_report_atomic,
)

ROOT = Path(__file__).resolve().parents[1]


def _config(*, fact_count: int = 1_000) -> BenchmarkConfig:
    return BenchmarkConfig(
        fact_count=fact_count,
        delta_count=25,
        chunk_size=200,
        page_size=50,
        read_samples=20,
    )


def _budgets(**overrides: float | int) -> BenchmarkBudgets:
    values: dict[str, float | int] = {
        "max_total_seconds": 60.0,
        "max_peak_python_memory_bytes": 128 * 1024 * 1024,
        "max_database_bytes": 128 * 1024 * 1024,
        "min_stream_rows_per_second": 1.0,
        "min_projection_rows_per_second": 1.0,
        "max_point_p95_milliseconds": 1_000.0,
        "max_page_p95_milliseconds": 1_000.0,
        "max_full_audit_seconds": 30.0,
        "max_bucket_audit_seconds": 30.0,
    }
    values.update(overrides)
    return BenchmarkBudgets.model_validate(values)


def _run(root: Path, *, budgets: BenchmarkBudgets | None = None):
    return run_benchmark(
        config=_config(),
        budgets=budgets or _budgets(),
        database_path=root / "synthetic.db",
    )


def test_benchmark_is_deterministic_outside_measurements(tmp_path: Path) -> None:
    first = _run(tmp_path / "first")
    second = _run(tmp_path / "second")

    assert first.config_sha256 == second.config_sha256
    assert first.correctness == second.correctness
    assert first.deterministic_commitments == second.deterministic_commitments
    assert first.row_counts == second.row_counts
    assert first.bounded_reads.maximum_rows_fetched_per_query <= first.config.page_size
    assert first.overall_pass


def test_stream_verifier_detects_tampering(tmp_path: Path) -> None:
    report = _run(tmp_path)
    conn = sqlite3.connect(tmp_path / "synthetic.db")
    conn.execute("UPDATE source_stream_events SET payload_json='{}' WHERE event_sequence=7")
    conn.commit()

    with pytest.raises(BenchmarkIntegrityError, match="stream_event_payload_tampered"):
        verify_stream_chain(conn, fetch_size=report.config.chunk_size)
    conn.close()


def test_budget_failure_is_explicit(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        budgets=_budgets(max_total_seconds=0.000_001),
    )

    assert not report.overall_pass
    failed = {result.budget_name for result in report.budget_results if not result.passed}
    assert "max_total_seconds" in failed


def test_atomic_report_write_uses_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _run(tmp_path / "run")
    output = tmp_path / "report.json"
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    write_report_atomic(report, output)

    assert replacements and replacements[-1][1] == output
    assert json.loads(output.read_text(encoding="utf-8"))["report_sha256"] == (report.report_sha256)
    assert verify_report_sha256(report)
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))


def test_live_database_path_is_refused_before_open() -> None:
    live = ROOT / "data" / "portfolio.db"
    before = live.stat() if live.exists() else None

    with pytest.raises(RefusedBenchmarkPathError, match="live portfolio database"):
        run_benchmark(
            config=_config(),
            budgets=_budgets(),
            database_path=live,
        )

    if before is None:
        assert not live.exists()
    else:
        after = live.stat()
        assert (before.st_size, before.st_mtime_ns) == (
            after.st_size,
            after.st_mtime_ns,
        )


def test_config_caps_fact_and_fetch_sizes() -> None:
    with pytest.raises(ValueError):
        BenchmarkConfig(
            fact_count=1_000_001,
            delta_count=1,
            chunk_size=1_000,
            page_size=1_000,
            read_samples=1,
        )
    with pytest.raises(ValueError):
        BenchmarkConfig(
            fact_count=1_000,
            delta_count=1,
            chunk_size=1_001,
            page_size=1_000,
            read_samples=1,
        )
