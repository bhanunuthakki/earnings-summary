from __future__ import annotations

import sqlite3
from pathlib import Path

from provenance.data_infrastructure_benchmark import (
    BenchmarkBudgets,
    ProductionBenchmarkConfig,
    run_production_contract_benchmark,
    verify_report_sha256,
)


def _budgets() -> BenchmarkBudgets:
    return BenchmarkBudgets(
        max_total_seconds=120,
        max_peak_python_memory_bytes=512 * 1024 * 1024,
        max_database_bytes=512 * 1024 * 1024,
        min_stream_rows_per_second=0.01,
        min_projection_rows_per_second=0.01,
        max_point_p95_milliseconds=10_000,
        max_page_p95_milliseconds=10_000,
        max_full_audit_seconds=60,
        max_bucket_audit_seconds=60,
    )


def test_production_contract_mode_uses_real_public_apis(
    tmp_path: Path,
) -> None:
    database = tmp_path / "production-contract.db"
    report = run_production_contract_benchmark(
        config=ProductionBenchmarkConfig(
            fact_count=3,
            publication_chunk_size=2,
            page_size=2,
            read_samples=2,
        ),
        budgets=_budgets(),
        database_path=database,
    )

    assert report.benchmark_mode == "production_contract"
    assert report.scale_interpretation == "measured_only_no_extrapolation_proof"
    assert report.row_counts.source_stream_events == 3
    assert report.row_counts.checkpoint_entries == 3
    assert report.row_counts.delta_entries == 0
    assert report.correctness.publication_exact_replay
    assert report.correctness.stream_page_replay
    assert report.correctness.strict_checkpoint_audit
    assert report.correctness.strict_delta_audit
    assert report.correctness.bounded_search_reads
    assert report.bounded_reads.maximum_rows_fetched_per_query <= 2
    assert report.measurements.full_audit.maximum_rows_fetched == 1_000
    assert (
        "_write_entries_and_batches buffers at most 1000 facts or "
        "16 MiB for one configured projection batch" in report.build_time_materialization
    )
    assert (
        "_checkpoint_bucket_commitments and "
        "_effective_bucket_commitment materialize one canonical bucket "
        "payload at a time, capped at 250000 entries and 16 MiB"
        in report.build_time_materialization
    )
    assert (
        "_seal_payloads materializes the fixed 4096-bucket logical "
        "commitment vector and a batch vector capped at 250000 batches "
        "and 64 MiB" in report.build_time_materialization
    )
    assert all(
        "strict audit materializes" not in limitation
        for limitation in report.production_limitations
    )
    serialized_report = report.model_dump_json()
    assert "full_audit_materialized_rows" not in serialized_report
    assert "strict audit materializes" not in serialized_report
    assert report.production_apis == (
        "provenance.source_fact_stream.read_publication_page",
        "search.canonical_fact_projection.build_canonical_projection_generation",
        "search.canonical_fact_projection.search_canonical_facts",
        "search.canonical_fact_projection.verify_canonical_projection_generation",
    )
    assert report.overall_pass
    assert verify_report_sha256(report)

    conn = sqlite3.connect(database)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_fact_projection_entries "
            "WHERE generation_id='production-checkpoint-v1'"
        ).fetchone() == (3,)
        assert conn.execute("SELECT COUNT(*) FROM source_fact_publication_stream").fetchone() == (
            3,
        )
    finally:
        conn.close()
