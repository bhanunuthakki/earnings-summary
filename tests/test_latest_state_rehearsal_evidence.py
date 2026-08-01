from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from provenance.latest_governed_population import LatestGovernedPopulationReceipt
from provenance.latest_state_rehearsal import (
    ArtifactCommitment,
    CandidateScopePerformance,
    DatabaseFileState,
    RestoreRoundtripEvidence,
)
from provenance.latest_state_rehearsal_evidence import (
    CandidatePerformanceRequest,
    RestoreRoundtripRequest,
    generate_candidate_performance_evidence,
    generate_restore_roundtrip_evidence,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id))"
        )
        conn.execute("INSERT INTO parent VALUES (1)")
        conn.execute("INSERT INTO child VALUES (1,1)")
        conn.commit()
    finally:
        conn.close()


def test_restore_roundtrip_uses_only_disposable_clone_and_restores_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.db"
    _database(source)
    original = source.read_bytes()
    audit = tmp_path / "audit.json"
    coverage = tmp_path / "coverage.json"
    audit.write_text("{}", encoding="utf-8")
    coverage.write_text("{}", encoding="utf-8")
    calls: list[Path] = []

    def clone(request: object) -> SimpleNamespace:
        source_database = Path(getattr(request, "source_database"))
        destination_database = Path(getattr(request, "destination_database"))
        calls.append(destination_database)
        destination_database.parent.mkdir(parents=False, exist_ok=False)
        destination_database.write_bytes(source_database.read_bytes())
        return SimpleNamespace(destination_database_sha256=_sha256(destination_database))

    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence.prepare_compressed_clone",
        clone,
    )
    request = RestoreRoundtripRequest(
        repo_root=tmp_path,
        source_database=source,
        candidate_audit_receipt=audit,
        candidate_coverage_receipt=coverage,
        work_directory=tmp_path / "restore-work",
        operation_recorded_at=NOW,
        minimum_free_bytes=5 * 1024 * 1024 * 1024,
    )

    evidence = generate_restore_roundtrip_evidence(request)

    assert isinstance(evidence, RestoreRoundtripEvidence)
    assert evidence.rollback_database_sha256 == _sha256(source)
    assert evidence.restored_database_sha256 == evidence.rollback_database_sha256
    assert evidence.mutated_database_sha256 != evidence.rollback_database_sha256
    assert evidence.quick_check == "ok"
    assert evidence.integrity_check == "ok"
    assert evidence.foreign_key_violation_count == 0
    assert source.read_bytes() == original
    assert len(calls) == 2
    assert not request.work_directory.exists()


def test_restore_roundtrip_cleans_owned_clone_when_mutation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.db"
    _database(source)
    audit = tmp_path / "audit.json"
    coverage = tmp_path / "coverage.json"
    audit.write_text("{}", encoding="utf-8")
    coverage.write_text("{}", encoding="utf-8")

    def clone(request: object) -> SimpleNamespace:
        source_database = Path(getattr(request, "source_database"))
        destination_database = Path(getattr(request, "destination_database"))
        destination_database.parent.mkdir(parents=False, exist_ok=False)
        destination_database.write_bytes(source_database.read_bytes())
        return SimpleNamespace(destination_database_sha256=_sha256(destination_database))

    def fail_mutation(_path: Path) -> None:
        raise OSError("injected mutation failure")

    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence.prepare_compressed_clone",
        clone,
    )
    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence._mutate_disposable_clone",
        fail_mutation,
    )
    request = RestoreRoundtripRequest(
        repo_root=tmp_path,
        source_database=source,
        candidate_audit_receipt=audit,
        candidate_coverage_receipt=coverage,
        work_directory=tmp_path / "restore-work",
        operation_recorded_at=NOW,
        minimum_free_bytes=5 * 1024 * 1024 * 1024,
    )

    with pytest.raises(OSError, match="injected"):
        generate_restore_roundtrip_evidence(request)

    assert not request.work_directory.exists()


def test_restore_roundtrip_refuses_work_directory_outside_source_parent(tmp_path: Path) -> None:
    source = tmp_path / "candidate.db"
    source.write_bytes(b"db")

    with pytest.raises(ValueError, match="share the source database parent"):
        RestoreRoundtripRequest(
            repo_root=tmp_path,
            source_database=source,
            candidate_audit_receipt=tmp_path / "audit.json",
            candidate_coverage_receipt=tmp_path / "coverage.json",
            work_directory=tmp_path / "nested" / "restore-work",
            operation_recorded_at=NOW,
            minimum_free_bytes=5 * 1024 * 1024 * 1024,
        )


def test_restore_roundtrip_refuses_configured_live_database(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "portfolio.db"
    source.write_bytes(b"db")

    with pytest.raises(ValueError, match="configured live database"):
        RestoreRoundtripRequest(
            repo_root=tmp_path,
            source_database=source,
            candidate_audit_receipt=tmp_path / "audit.json",
            candidate_coverage_receipt=tmp_path / "coverage.json",
            work_directory=data / "restore-work",
            operation_recorded_at=NOW,
            minimum_free_bytes=5 * 1024 * 1024 * 1024,
        )


def test_restore_roundtrip_refuses_live_sidecar_as_work_directory(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "candidate.db"
    source.write_bytes(b"db")

    with pytest.raises(ValueError, match="alias"):
        RestoreRoundtripRequest(
            repo_root=tmp_path,
            source_database=source,
            candidate_audit_receipt=tmp_path / "audit.json",
            candidate_coverage_receipt=tmp_path / "coverage.json",
            work_directory=data / "portfolio.db-wal",
            operation_recorded_at=NOW,
            minimum_free_bytes=5 * 1024 * 1024 * 1024,
        )


def test_candidate_performance_binds_noop_receipt_benchmark_and_stable_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text("{}", encoding="utf-8")
    noop = tmp_path / "noop.json"
    noop.write_text("{}", encoding="utf-8")
    noop_artifact = ArtifactCommitment.from_path(noop)
    dry = SimpleNamespace(
        outcome="no_op",
        current_write_count=0,
        fact_change_count=0,
        document_change_count=0,
        narrative_change_count=0,
    )
    receipt = LatestGovernedPopulationReceipt.model_construct(
        result=SimpleNamespace(
            mode="dry_run",
            outcome="planned",
            processed_scope_ids=("scope-a", "scope-b"),
            remaining_scope_ids=(),
            scope_results=(
                SimpleNamespace(scope_id="scope-a", dry_run=dry),
                SimpleNamespace(scope_id="scope-b", dry_run=dry),
            ),
            heads_before={"scope-a": ("r1", "a"), "scope-b": ("r2", "b")},
            heads_after={"scope-a": ("r1", "a"), "scope-b": ("r2", "b")},
        )
    )
    benchmark_artifact = ArtifactCommitment.from_path(benchmark)

    def load_benchmark(_path: Path) -> tuple[ArtifactCommitment, SimpleNamespace]:
        return (
            benchmark_artifact,
            SimpleNamespace(
                overall_pass=True,
                config=SimpleNamespace(profile="production"),
                history_independence=SimpleNamespace(latency_ratio=1.1),
                implementation_provenance=SimpleNamespace(source_set_sha256="current"),
            ),
        )

    def verify_production(_report: object) -> bool:
        return True

    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence.verify_production_benchmark_report",
        verify_production,
    )

    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence._load_benchmark",
        load_benchmark,
    )
    before = DatabaseFileState.from_path(database)

    def measure_candidate_reads(
        *,
        database_path: Path,
        scope_ids: tuple[str, ...],
        read_samples: int,
        read_limit: int,
    ) -> tuple[CandidateScopePerformance, ...]:
        del database_path, scope_ids, read_samples, read_limit
        return (
            CandidateScopePerformance(
                scope_id="scope-a",
                sample_count=3,
                fact_read_p95_milliseconds=2.0,
                narrative_read_p95_milliseconds=3.0,
                fact_query_uses_production_index=True,
                narrative_query_uses_fts_index=True,
                fact_query_plan=("SEARCH fact USING INDEX ix_latest_governed_fact_search",),
                narrative_query_plan=("SCAN latest_governed_narrative_fts VIRTUAL TABLE INDEX",),
            ),
            CandidateScopePerformance(
                scope_id="scope-b",
                sample_count=3,
                fact_read_p95_milliseconds=2.5,
                narrative_read_p95_milliseconds=3.5,
                fact_query_uses_production_index=True,
                narrative_query_uses_fts_index=True,
                fact_query_plan=("SEARCH fact USING INDEX ix_latest_governed_fact_search",),
                narrative_query_plan=("SCAN latest_governed_narrative_fts VIRTUAL TABLE INDEX",),
            ),
        )

    monkeypatch.setattr(
        "provenance.latest_state_rehearsal_evidence._measure_candidate_reads",
        measure_candidate_reads,
    )
    request = CandidatePerformanceRequest(
        synthetic_benchmark_report=benchmark,
        read_samples=3,
        read_limit=10,
        max_fact_read_p95_milliseconds=100,
        max_narrative_read_p95_milliseconds=100,
        max_history_scale_ratio=1.5,
    )
    request_path = tmp_path / "performance-request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")
    request_artifact = ArtifactCommitment.from_path(request_path)

    evidence = generate_candidate_performance_evidence(
        database_path=database,
        request=request,
        request_artifact=request_artifact,
        no_op_receipt_artifact=noop_artifact,
        no_op_receipt=receipt,
        production_scope_ids=("scope-a", "scope-b"),
    )

    assert evidence.database_sha256 == before.file_sha256
    assert evidence.performance_request == request_artifact
    assert evidence.synthetic_benchmark_report == benchmark_artifact
    assert evidence.candidate_no_op_receipt == noop_artifact
    assert evidence.no_op_current_write_count == 0
    assert evidence.measured_scope_ids == ("scope-a", "scope-b")
    assert evidence.fact_read_p95_milliseconds == 2.5
    assert evidence.narrative_read_p95_milliseconds == 3.5
    assert len(evidence.scope_measurements) == 2
    assert evidence.fact_query_uses_production_index
    assert evidence.narrative_query_uses_fts_index


def test_candidate_performance_refuses_changed_or_partial_noop_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate.db"
    _database(database)
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text("{}", encoding="utf-8")
    noop = tmp_path / "noop.json"
    noop.write_text("{}", encoding="utf-8")
    receipt = LatestGovernedPopulationReceipt.model_construct(
        result=SimpleNamespace(
            mode="dry_run",
            outcome="planned",
            processed_scope_ids=("scope-a",),
            remaining_scope_ids=("scope-b",),
            scope_results=(),
            heads_before={},
            heads_after={},
        )
    )
    request = CandidatePerformanceRequest(
        synthetic_benchmark_report=benchmark,
        read_samples=3,
        read_limit=10,
        max_fact_read_p95_milliseconds=100,
        max_narrative_read_p95_milliseconds=100,
        max_history_scale_ratio=1.5,
    )
    request_path = tmp_path / "performance-request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")

    with pytest.raises(ValueError, match="exact production cohort"):
        generate_candidate_performance_evidence(
            database_path=database,
            request=request,
            request_artifact=ArtifactCommitment.from_path(request_path),
            no_op_receipt_artifact=ArtifactCommitment.from_path(noop),
            no_op_receipt=receipt,
            production_scope_ids=("scope-a", "scope-b"),
        )
