"""Actual sourced-reader comparisons retain immutable identity and cutover HOLD."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import timeseries.loaders as loader_module
from execution.audit_kpi_revision_reader import main as reader_cli
from pipeline.kpi_definition_revisions import (
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionLifecycle,
    persist_kpi_definition_comparability_revision,
    persist_kpi_definition_revision,
)
from pipeline.kpi_semantics import KpiSemanticStatus, persist_kpi_semantic_context
from sqlite_snapshot import SnapshotRequest, create_snapshot
from tests.fixtures.kpi_revision_setup import (
    NOW,
)
from tests.fixtures.kpi_revision_setup import (
    comparability_fixture as _relation,
)
from tests.fixtures.kpi_revision_setup import (
    definition_fixture as _definition,
)
from tests.fixtures.kpi_revision_setup import (
    fact_fixture as _fact,
)
from tests.fixtures.kpi_revision_setup import (
    revision_database as _database,
)
from tests.fixtures.kpi_revision_setup import (
    semantic_fixture as _context,
)
from timeseries.kpi_revision_shadow import (
    KpiReaderShadowComparison,
    KpiRevisionReadRequest,
    read_revision_kpi_points,
)
from timeseries.loaders import load_kpi_series_with_provenance, rehearse_kpi_series_reader


def _request(**changes: object) -> KpiRevisionReadRequest:
    return KpiRevisionReadRequest.model_validate(
        {"ticker": "NU", "kpi_definition_id": 1, "effective_at": NOW, "known_at": NOW} | changes
    )


def _bound_fact(conn: sqlite3.Connection, **changes: object) -> tuple[int, int]:
    definition = persist_kpi_definition_revision(conn, _definition(**changes))
    fact_id = _fact(conn, definition_id=definition.kpi_definition_id)
    context_id = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    assert context_id is not None
    return fact_id, context_id


def _snapshot(tmp_path: Path, *, later_quarantine: bool = False) -> tuple[Path, Path]:
    source = tmp_path / "source.db"
    conn = _database(source)
    fact_id, _ = _bound_fact(conn)
    if later_quarantine:
        persist_kpi_semantic_context(
            conn,
            kpi_fact_id=fact_id,
            context=_context().model_copy(
                update={"status": KpiSemanticStatus.QUARANTINED, "reason_code": "fixture_review"}
            ),
            reviewed_by="owner",
            knowledge_at=NOW + timedelta(days=1),
            kpi_definition_revision_id=None,
        )
    conn.executescript(
        "CREATE TABLE alembic_version(version_num TEXT NOT NULL);"
        "INSERT INTO alembic_version VALUES ('synthetic-reader-schema');"
        "CREATE TABLE documents(id INTEGER PRIMARY KEY,fetched_at TEXT,source_url TEXT,"
        "doc_type TEXT,source_type TEXT,source_quality_tier TEXT,accession_number TEXT,filing_date TEXT);"
        "INSERT INTO documents VALUES(10,'2026-09-06','https://issuer.example/report',"
        "'earnings_release','ir_doc','sec_official',NULL,'2026-09-06');"
    )
    conn.commit()
    conn.close()
    snapshot = tmp_path / "reader.db"
    result = create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    return snapshot, result.manifest_path


def test_actual_sourced_reader_comparison_has_exact_trace_and_deterministic_hold(
    tmp_path: Path,
) -> None:
    snapshot, manifest = _snapshot(tmp_path)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    legacy = load_kpi_series_with_provenance(
        "NU", "Monthly ARPAC", db_path=snapshot, snapshot_manifest=manifest
    )
    assert len(legacy) == 1
    result = rehearse_kpi_series_reader(
        db_path=snapshot, snapshot_manifest=manifest, request=_request()
    )
    replay = rehearse_kpi_series_reader(
        db_path=snapshot, snapshot_manifest=manifest, request=_request()
    )
    assert result == replay
    assert result.scoped_value_source_parity is True
    assert result.authorizes_reader_activation is False
    point = result.revision.points[0]
    assert point.fact_id == legacy[0].provenance["fact_id"]
    assert point.source_document_id == legacy[0].provenance["source_doc_id"] == 10
    assert point.locator_json == '{"pdf_page":7}'
    assert point.observation_id == "kpi-observation-1"
    assert point.resolution_revision == point.fact_revision == point.semantic_context_revision == 1
    assert point.definition_revision_id == "definition-r1"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
    assert not snapshot.with_name(snapshot.name + "-wal").exists()
    assert not snapshot.with_name(snapshot.name + "-shm").exists()
    forged = result.model_dump(mode="json")
    forged["scoped_value_source_parity"] = False
    with pytest.raises(ValidationError, match="hash mismatch"):
        KpiReaderShadowComparison.model_validate(forged)


def test_historical_reader_uses_historical_semantic_head_and_resolution() -> None:
    conn = _database()
    fact_id, old_context = _bound_fact(conn)
    later = NOW + timedelta(days=1)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=_context(),
        reviewed_by="owner",
        knowledge_at=later,
        kpi_definition_revision_id=None,
    )
    historical = read_revision_kpi_points(conn, request=_request())
    current = read_revision_kpi_points(conn, request=_request(effective_at=later, known_at=later))
    assert historical.points[0].semantic_context_id == old_context
    assert historical.points[0].resolution_id == "kpi-resolution-kpi-logical-1-1"
    assert current.points == ()
    assert "empty_admitted_series" in current.blocking_reasons
    assert conn.in_transaction  # caller state was not committed or rolled back
    conn.close()


def test_discontinued_current_definition_never_falls_back_to_old_series() -> None:
    conn = _database()
    _bound_fact(conn)
    later = NOW + timedelta(days=1)
    persist_kpi_definition_revision(
        conn,
        _definition(
            kpi_definition_revision_id="definition-r2",
            idempotency_key="definition-key-r2",
            revision=2,
            supersedes_definition_revision_id="definition-r1",
            lifecycle=KpiDefinitionLifecycle.DISCONTINUED,
            effective_at=later,
            knowledge_at=later,
            recorded_at=later,
        ),
    )
    result = read_revision_kpi_points(conn, request=_request(effective_at=later, known_at=later))
    assert result.points == () and result.blocking_reasons == ("discontinued",)
    assert read_revision_kpi_points(conn, request=_request()).points
    conn.close()


def test_wrong_issuer_and_same_label_unbound_definition_never_merge() -> None:
    conn = _database()
    _bound_fact(conn)
    conn.execute("INSERT INTO kpi_definitions VALUES (2,'NU','Monthly ARPAC','actual')")
    with pytest.raises(ValueError, match="requested issuer"):
        read_revision_kpi_points(conn, request=_request(ticker="OTHER"))
    result = read_revision_kpi_points(conn, request=_request(kpi_definition_id=2))
    assert result.points == () and result.blocking_reasons == ("legacy_unbound",)
    conn.close()


def test_break_and_duplicate_period_are_retained_without_false_continuity() -> None:
    conn = _database()
    _bound_fact(conn)
    conn.execute("INSERT INTO kpi_definitions VALUES (2,'NU','Renamed ARPAC','actual')")
    _bound_fact(
        conn,
        kpi_definition_id=2,
        kpi_definition_revision_id="renamed-r1",
        idempotency_key="renamed-key",
    )
    persist_kpi_definition_comparability_revision(
        conn,
        _relation(
            "definition-r1",
            "renamed-r1",
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
        ),
    )
    result = read_revision_kpi_points(conn, request=_request())
    assert len(result.points) == 2
    assert result.resolution.breaks
    assert result.blocking_reasons == (
        "ambiguous_period_requires_explicit_selection",
        "comparability_break_requires_segmented_consumer",
    )
    conn.close()


def test_comparison_rejects_unverified_file_before_reading(tmp_path: Path) -> None:
    snapshot, manifest = _snapshot(tmp_path)
    snapshot.write_bytes(snapshot.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="manifest-matched"):
        rehearse_kpi_series_reader(db_path=snapshot, snapshot_manifest=manifest, request=_request())


def test_request_requires_ordered_aware_cutoffs() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _request(known_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError, match="effective cutoff"):
        _request(effective_at=NOW + timedelta(days=1))


def test_actual_current_reader_difference_is_explained_without_historical_fallback(
    tmp_path: Path,
) -> None:
    snapshot, manifest = _snapshot(tmp_path, later_quarantine=True)
    result = rehearse_kpi_series_reader(
        db_path=snapshot, snapshot_manifest=manifest, request=_request()
    )
    assert result.legacy_points == ()
    assert len(result.revision.points) == 1
    assert result.differences == ("selected_fact_membership_differs",)
    assert result.scoped_value_source_parity is False
    assert result.authorizes_reader_activation is False


def test_actual_reader_comparison_checks_snapshot_after_both_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, manifest = _snapshot(tmp_path)
    actual = loader_module.load_kpi_series_with_provenance

    def mutate_after_read(
        ticker: str,
        kpi_name: str,
        *,
        db_path: Path,
        period_types: tuple[str, ...],
        snapshot_manifest: Path,
    ) -> list[loader_module.SourcedObservation]:
        points = actual(
            ticker,
            kpi_name,
            db_path=db_path,
            period_types=period_types,
            snapshot_manifest=snapshot_manifest,
        )
        db_path.write_bytes(db_path.read_bytes() + b"changed")
        return points

    monkeypatch.setattr(loader_module, "load_kpi_series_with_provenance", mutate_after_read)
    with pytest.raises(RuntimeError, match="identity changed"):
        rehearse_kpi_series_reader(db_path=snapshot, snapshot_manifest=manifest, request=_request())


def test_cli_retains_a_non_authorizing_receipt_and_bootstrap_help(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, manifest = _snapshot(tmp_path)
    assert (
        reader_cli(
            [
                "--db-path",
                str(snapshot),
                "--snapshot-manifest",
                str(manifest),
                "--ticker",
                "NU",
                "--definition-id",
                "1",
                "--effective-at",
                NOW.isoformat(),
                "--known-at",
                NOW.isoformat(),
            ]
        )
        == 2
    )
    receipt = KpiReaderShadowComparison.model_validate(json.loads(capsys.readouterr().out))
    assert receipt.scoped_value_source_parity and not receipt.authorizes_reader_activation
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "execution/sqlite_bootstrap.py"),
            str(root / "execution/audit_kpi_revision_reader.py"),
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    assert result.returncode == 0, result.stderr
    assert "--snapshot-manifest" in result.stdout
