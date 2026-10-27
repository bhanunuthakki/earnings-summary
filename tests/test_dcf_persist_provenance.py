"""Focused DCF provenance persistence and idempotency tests."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from dcf import artifact_promotion
from dcf.artifact_promotion import (
    DcfRecoveryError,
    StagedArtifactBundle,
    StagedFilePromotion,
    live_path_from_env,
)
from dcf.forecast_series import ForecastSeriesPoint
from dcf.persist import (
    DcfPromotionBlocked,
    DcfRunRow,
    matching_current_run_id,
    persist_run,
    read_reuse_context,
    upsert,
)
from dcf.provenance import (
    DcfInputProvenance,
    build_file_provenance,
    build_file_source_record,
)
from provenance.metric_ontology import (
    CanonicalMetric,
    CanonicalMetricDefinitionRevision,
    MetricOntology,
)
from sqlite_runtime import (
    SQLiteConnectionRole,
    connect_sqlite,
    register_sqlite_integrity_functions,
)

_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    segment_name TEXT,
    valuation_date TEXT,
    horizon_years INTEGER,
    wacc REAL,
    terminal_growth REAL,
    npv REAL,
    npv_per_share REAL,
    shares_outstanding REAL,
    currency TEXT,
    notes TEXT,
    run_id TEXT,
    live_price REAL,
    live_price_at TEXT,
    over_under_pct REAL,
    mos_bar_used REAL,
    assumption_snapshot_json TEXT,
    revenue_growths_json TEXT,
    fcf_margin REAL,
    assumptions_sync_status TEXT,
    assumptions_synced_at TEXT,
    sanity_flag TEXT,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER,
    input_sha256 TEXT,
    workbook_sha256 TEXT,
    engine_version TEXT,
    inputs_as_of TEXT,
    provenance_json TEXT
);
CREATE TABLE dcf_run_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dcf_run_id INTEGER NOT NULL REFERENCES dcf_runs(id) ON DELETE RESTRICT,
    role TEXT NOT NULL,
    locator TEXT NOT NULL,
    sha256 TEXT,
    byte_size INTEGER,
    observed_at TEXT,
    detail_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(dcf_run_id, role, locator)
);
"""


def _row() -> DcfRunRow:
    return DcfRunRow(
        ticker="META",
        valuation_date=date(2026, 7, 28),
        horizon_years=10,
        wacc=0.09,
        npv=1_000.0,
        npv_per_share=100.0,
        shares_outstanding=10_000_000.0,
        currency="USD",
        live_price=90.0,
        live_price_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        mos_bar_used=0.2,
        assumption_snapshot_json='{"wacc":0.09}',
        notes="workbook=META.xlsx (redesigned)",
        assumptions_sync_status="synced",
        assumptions_synced_at=datetime(2026, 7, 28, 12, 1),
        provenance=DcfInputProvenance(
            input_sha256="a" * 64,
            workbook_sha256="b" * 64,
            engine_version="redesign_fcff_v1",
            inputs_as_of=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
            detail={
                "equity_bridge_receipt": {"status": "verified"},
                "sources": [
                    {
                        "role": "income_statement",
                        "path": "data/META_income_statement.json",
                        "sha256": "c" * 64,
                        "bytes": 123,
                        "observed_at": "2026-07-28T11:59:00+00:00",
                    }
                ],
                "market_price": {
                    "price": 90.0,
                    "observed_at": "2026-07-28T12:00:00+00:00",
                    "source": "fmp_quote",
                },
            },
        ),
    )


def test_persistence_receipt_identifies_written_and_reused_revision() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(_SCHEMA)
        first = persist_run(conn, _row())
        repeated = persist_run(conn, _row())
        changed = persist_run(conn, dataclasses.replace(_row(), npv_per_share=110.0))

        assert first.written is True
        assert repeated.written is False
        assert repeated.revision == first.revision
        assert changed.revision.row_id != first.revision.row_id
        assert first.revision.input_sha256 == "a" * 64
        assert first.revision.workbook_sha256 == "b" * 64
        assert first.revision.engine_version == "redesign_fcff_v1"
        assert (
            first.revision.assumption_snapshot_sha256
            == hashlib.sha256(_row().assumption_snapshot_json.encode()).hexdigest()
        )
        provenance = _row().provenance
        assert provenance is not None
        assert (
            first.revision.provenance_sha256
            == hashlib.sha256(provenance.as_json().encode()).hexdigest()
        )


def test_persistence_receipt_survives_another_save_after_commit(tmp_path: Path) -> None:
    database = tmp_path / "revision.db"

    class InterleavedConnection(sqlite3.Connection):
        def commit(self) -> None:
            super().commit()
            with sqlite3.connect(database) as other:
                upsert(other, dataclasses.replace(_row(), npv_per_share=120.0))

    with sqlite3.connect(database, factory=InterleavedConnection) as conn:
        conn.executescript(_SCHEMA)
        saved = persist_run(conn, _row())
        latest_id = conn.execute("SELECT id FROM dcf_runs WHERE is_latest=1").fetchone()[0]
        assert saved.revision.row_id != latest_id
        persisted = conn.execute(
            "SELECT npv_per_share FROM dcf_runs WHERE id=?", (saved.revision.row_id,)
        ).fetchone()
        assert persisted == (100.0,)


def test_exact_provenance_retry_is_a_noop() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()

    assert upsert(conn, row) is True
    assert upsert(conn, row) is False

    count = conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0]
    assert count == 1
    current = conn.execute(
        "SELECT input_sha256, workbook_sha256, engine_version, provenance_json "
        "FROM dcf_runs WHERE is_latest=1"
    ).fetchone()
    assert current == (
        "a" * 64,
        "b" * 64,
        "redesign_fcff_v1",
        '{"equity_bridge_receipt":{"status":"verified"},'
        '"market_price":{"observed_at":"2026-07-28T12:00:00+00:00",'
        '"price":90.0,"source":"fmp_quote"},"sources":[{"bytes":123,'
        '"observed_at":"2026-07-28T11:59:00+00:00",'
        '"path":"data/META_income_statement.json","role":"income_statement",'
        '"sha256":"' + ("c" * 64) + '"}]}',
    )
    inputs = conn.execute(
        "SELECT role, locator, sha256, byte_size, observed_at FROM dcf_run_inputs ORDER BY role"
    ).fetchall()
    assert inputs == [
        (
            "income_statement",
            "data/META_income_statement.json",
            "c" * 64,
            123,
            "2026-07-28T11:59:00+00:00",
        ),
        (
            "market_price",
            "fmp_quote",
            None,
            None,
            "2026-07-28T12:00:00+00:00",
        ),
    ]


_DIMENSIONS_JSON = "[]"
_DIMENSIONS_DIGEST = hashlib.sha256(_DIMENSIONS_JSON.encode()).hexdigest()
_EVIDENCE_JSON = '{"basis":"reviewed test mapping"}'
_EVIDENCE_DIGEST = hashlib.sha256(_EVIDENCE_JSON.encode()).hexdigest()
_POLICY_DIGEST = "a" * 64
_REVIEWER = "test-reviewer"


def _seeded_reuse_conn() -> sqlite3.Connection:
    """A legacy minimal-schema connection (no forecast plane)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    persist_run(conn, _row())
    return conn


def _latest_id_of(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT id FROM dcf_runs WHERE is_latest=1").fetchone()[0])


_RUN_COLUMNS = (
    "ticker",
    "segment_name",
    "valuation_date",
    "horizon_years",
    "wacc",
    "terminal_growth",
    "npv",
    "npv_per_share",
    "shares_outstanding",
    "currency",
    "notes",
    "run_id",
    "live_price",
    "live_price_at",
    "over_under_pct",
    "mos_bar_used",
    "assumption_snapshot_json",
    "revenue_growths_json",
    "fcf_margin",
    "assumptions_sync_status",
    "assumptions_synced_at",
    "sanity_flag",
    "is_latest",
    "superseded_at",
    "superseded_by_id",
    "input_sha256",
    "workbook_sha256",
    "engine_version",
    "inputs_as_of",
    "provenance_json",
)


def _clone_current_run(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    ticker: str | None = None,
    segment_name: str | None = None,
) -> int:
    """Clone every compared column of ``source_id`` into a new row, changing
    only the isolated identity field (ticker OR segment_name), and copy the
    source's input-ledger population — so the clone differs from a matching
    baseline by exactly that one identity field."""
    cols = ", ".join(_RUN_COLUMNS)
    conn.execute(
        f"INSERT INTO dcf_runs ({cols}) SELECT {cols} FROM dcf_runs WHERE id = ?",
        (source_id,),
    )
    clone_id = int(conn.execute("SELECT id FROM dcf_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
    if ticker is not None:
        conn.execute("UPDATE dcf_runs SET ticker=? WHERE id=?", (ticker, clone_id))
    if segment_name is not None:
        conn.execute("UPDATE dcf_runs SET segment_name=? WHERE id=?", (segment_name, clone_id))
    conn.execute(
        """
        INSERT INTO dcf_run_inputs
            (dcf_run_id, role, locator, sha256, byte_size, observed_at, detail_json)
        SELECT ?, role, locator, sha256, byte_size, observed_at, detail_json
        FROM dcf_run_inputs WHERE dcf_run_id = ?
        """,
        (clone_id, source_id),
    )
    return clone_id


def _point(
    *, mapping_revision_id: int = 1, series_key: str = "revenue", value: float = 10.0
) -> ForecastSeriesPoint:
    return ForecastSeriesPoint(
        mapping_revision_id=mapping_revision_id,
        series_key=series_key,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),
        value=value,
    )


def _migrated_reuse_conn(db_path: Path) -> sqlite3.Connection:
    """A connection to the real migrated schema with its integrity functions."""
    conn = sqlite3.connect(db_path)
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _insert_admitted_mapping(
    conn: sqlite3.Connection,
    *,
    revision: int = 1,
    supersedes: int | None = None,
) -> int:
    """Seed the canonical definition + one admitted mapping revision (rev 1)."""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    ontology = MetricOntology(conn)
    ontology.persist_metric(
        CanonicalMetric(
            metric_id="test-revenue",
            idempotency_key="test-revenue",
            canonical_name="Test revenue",
            effective_at=at,
            knowledge_at=at,
            recorded_at=at,
        )
    )
    ontology.persist_metric_definition(
        CanonicalMetricDefinitionRevision(
            metric_definition_revision_id="test-definition",
            idempotency_key="test-definition",
            metric_id="test-revenue",
            revision=1,
            lifecycle="active",
            definition_text="Test annual revenue",
            value_kind="numeric",
            period_kind="duration",
            unit_family="currency",
            accounting_basis="us_gaap",
            scope_constraints={},
            effective_at=at,
            knowledge_at=at,
            recorded_at=at,
        )
    )
    cursor = conn.execute(
        """
        INSERT INTO dcf_forecast_metric_mapping_revisions (
            ticker, engine_family, engine_version, series_key, viewspec_metric_token,
            canonical_metric_definition_revision_id, period_kind, unit_family, value_scale,
            currency, accounting_basis, consolidation_scope, dimensions_sha256, dimensions_json,
            admission_status, confidence, policy_name, policy_version, policy_config_sha256,
            evidence_json, evidence_sha256, reviewer_identity, revision,
            supersedes_mapping_revision_id, effective_at, knowledge_at, recorded_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "META",
            "redesign",
            "redesign_fcff_v1",
            "revenue",
            "fin:revenue",
            "test-definition",
            "duration",
            "currency",
            "millions",
            "USD",
            "us_gaap",
            "consolidated",
            _DIMENSIONS_DIGEST,
            _DIMENSIONS_JSON,
            "admitted",
            "high",
            "test",
            "v1",
            _POLICY_DIGEST,
            _EVIDENCE_JSON,
            _EVIDENCE_DIGEST,
            _REVIEWER,
            revision,
            supersedes,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    return int(cursor.lastrowid or 0)


def test_matching_current_run_id_reuses_exact_ledger_and_empty_forecast() -> None:
    conn = _seeded_reuse_conn()
    latest = _latest_id_of(conn)
    assert matching_current_run_id(conn, _row()) == latest


def test_matching_current_run_id_rejects_missing_input_ledger_table() -> None:
    conn = _seeded_reuse_conn()
    conn.execute("DROP TABLE dcf_run_inputs")
    assert matching_current_run_id(conn, _row()) is None


def test_matching_current_run_id_rejects_deleted_input_ledger_rows() -> None:
    conn = _seeded_reuse_conn()
    conn.execute("DELETE FROM dcf_run_inputs")
    assert matching_current_run_id(conn, _row()) is None


def test_matching_current_run_id_rejects_corrupted_input_ledger_row() -> None:
    conn = _seeded_reuse_conn()
    conn.execute(
        "UPDATE dcf_run_inputs SET sha256=? WHERE role='income_statement'",
        ("f" * 64,),
    )
    assert matching_current_run_id(conn, _row()) is None


def test_matching_current_run_id_run_id_without_snapshot_fails_closed() -> None:
    """A passed ``run_id`` without the caller's active read transaction
    declines reuse (the identity could not have been read consistently)."""
    conn = _seeded_reuse_conn()
    latest = _latest_id_of(conn)
    assert matching_current_run_id(conn, _row()) == latest
    assert not conn.in_transaction
    assert matching_current_run_id(conn, _row(), run_id=latest) is None


def test_matching_current_run_id_reuses_exact_migrated_forecast_population(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    """On the real migrated plane the stored population — not a pretend
    minimal table — decides exact reuse, including the input ledger."""
    db_path = migrated_db(tmp_path / "reuse-forecast.db")
    conn = _migrated_reuse_conn(db_path)
    mapping_id = _insert_admitted_mapping(conn)
    conn.commit()
    row = dataclasses.replace(_row(), forecast_points=(_point(mapping_revision_id=mapping_id),))
    persist_run(conn, row)
    latest = _latest_id_of(conn)
    assert matching_current_run_id(conn, row) == latest

    # Expected-empty vs stored-nonempty declines: the committed population
    # is the truth an empty probe must never ride.
    assert matching_current_run_id(conn, dataclasses.replace(_row(), forecast_points=())) is None
    # A diverging expected population declines (the stored points are
    # append-only, so the mismatch is probed, not fabricated by mutation).
    diverging = dataclasses.replace(
        _row(), forecast_points=(_point(mapping_revision_id=mapping_id, value=999.0),)
    )
    assert matching_current_run_id(conn, diverging) is None
    conn.close()


def test_matching_current_run_id_declines_when_mapping_admission_revoked(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    """A producer rebuilding the population after the mapping was superseded
    selects the new revision, so the stored population no longer matches and
    reuse declines."""
    db_path = migrated_db(tmp_path / "reuse-admission.db")
    conn = _migrated_reuse_conn(db_path)
    rev1 = _insert_admitted_mapping(conn)
    conn.commit()
    persist_run(
        conn, dataclasses.replace(_row(), forecast_points=(_point(mapping_revision_id=rev1),))
    )
    conn.commit()
    rev2 = _insert_admitted_mapping(conn, revision=2, supersedes=rev1)
    conn.commit()

    rebuilt = dataclasses.replace(_row(), forecast_points=(_point(mapping_revision_id=rev2),))
    assert matching_current_run_id(conn, rebuilt) is None
    conn.close()


def test_matching_current_run_id_wal_reader_binds_one_snapshot(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    """Real WAL interleaving: a writer commits a NEW current run between the
    reader's context row and its dependent population reads. The commit must
    actually succeed during the read, and the reader's caller-held snapshot
    must bind the single old exact row — never a mix of old context and new
    population."""
    db_path = migrated_db(tmp_path / "reuse-wal.db")
    setup = sqlite3.connect(db_path)
    setup.execute("PRAGMA journal_mode=WAL")
    base = _row()
    assert base.provenance is not None
    detail = dict(base.provenance.detail or {})
    detail["ticker"] = "META"
    row_a = dataclasses.replace(
        base,
        notes="run A",
        provenance=dataclasses.replace(base.provenance, detail=detail),
    )
    persist_run(setup, row_a)
    run_a = _latest_id_of(setup)
    setup.commit()
    setup.close()

    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as reader:
        reader.execute("BEGIN")
        context = read_reuse_context(reader, "META")
        assert context is not None
        assert context.run_id == run_a

        # The writer commits while the reader holds its snapshot open.
        with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as writer:
            upsert(writer, dataclasses.replace(_row(), notes="run B", npv=2_000.0))
        with sqlite3.connect(db_path) as witness:
            new_latest = int(
                witness.execute("SELECT id FROM dcf_runs WHERE is_latest=1").fetchone()[0]
            )
        assert new_latest != run_a

        # Keyed to the context identity, the reader binds the old exact row.
        assert matching_current_run_id(reader, row_a, run_id=context.run_id) == run_a
        # Selecting latest again inside the stale snapshot would still see
        # run A — but a FRESH guarded read sees the new current row and
        # conservatively declines the old expectation.
    with connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY) as fresh:
        assert matching_current_run_id(fresh, row_a) is None


def test_matching_current_run_id_keys_to_passed_run_id_not_latest(tmp_path: Path) -> None:
    database = tmp_path / "reuse-snapshot.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        persist_run(conn, dataclasses.replace(_row(), notes="run A"))
        run_a = _latest_id_of(conn)
        conn.commit()

        with sqlite3.connect(database) as other:
            upsert(other, dataclasses.replace(_row(), notes="run B", npv=2_000.0))
            run_b = _latest_id_of(other)
            assert run_b != run_a

        row_a = dataclasses.replace(_row(), notes="run A")
        # Fresh snapshot: run B is the current META row, so the stale
        # expectation declines both by latest-selection and by exact id.
        assert matching_current_run_id(conn, row_a) is None
        conn.execute("BEGIN")
        try:
            assert matching_current_run_id(conn, row_a, run_id=run_a) is None
            # The genuine current identity still matches exactly by id.
            row_b = dataclasses.replace(_row(), notes="run B", npv=2_000.0)
            assert matching_current_run_id(conn, row_b, run_id=run_b) == run_b
        finally:
            conn.execute("ROLLBACK")


def test_matching_current_run_id_run_id_rejects_other_issuer_and_segmented_row(
    tmp_path: Path,
) -> None:
    """The explicit run_id path binds ONE current unsegmented identity for the
    expected issuer. Each negative clone is identical to the matching current
    META baseline in every compared field (notes, financials, provenance,
    input/workbook/engine/snapshot digests) and carries an equal input-ledger
    population, differing ONLY by ticker or segment_name — so each rejection
    is attributable solely to the corresponding identity predicate."""
    with sqlite3.connect(tmp_path / "reuse-identity.db") as conn:
        conn.executescript(_SCHEMA)
        row_a = dataclasses.replace(_row(), notes="run A")
        persist_run(conn, row_a)
        run_a = _latest_id_of(conn)
        other_issuer = _clone_current_run(conn, run_a, ticker="AAPL")
        segmented = _clone_current_run(conn, run_a, segment_name="HoldCo")
        assert other_issuer != run_a
        assert segmented not in (run_a, other_issuer)
        conn.commit()

        conn.execute("BEGIN")
        try:
            # Positive control: the exact current baseline still matches.
            assert matching_current_run_id(conn, row_a, run_id=run_a) == run_a
            # Identical except ticker: only the ticker predicate rejects it.
            assert matching_current_run_id(conn, row_a, run_id=other_issuer) is None
            # Identical except segment_name: only the segment predicate rejects it.
            assert matching_current_run_id(conn, row_a, run_id=segmented) is None
        finally:
            conn.execute("ROLLBACK")


def test_dcf_row_and_staged_workbook_promote_together(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    live = tmp_path / "META.xlsx"
    staged = tmp_path / "META.rebuild.xlsx"
    live.write_bytes(b"old workbook")
    staged.write_bytes(b"new workbook")

    assert upsert(conn, _row(), artifact_promotion=StagedFilePromotion(staged, live)) is True

    assert live.read_bytes() == b"new workbook"
    assert not staged.exists()
    assert not list(tmp_path.glob("*.rollback.*"))
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1


def test_commit_failure_restores_workbook_and_dcf_row(tmp_path: Path) -> None:
    class FailingCommitConnection(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("simulated commit failure")

    conn = sqlite3.connect(":memory:", factory=FailingCommitConnection)
    conn.executescript(_SCHEMA)
    live = tmp_path / "META.xlsx"
    staged = tmp_path / "META.rebuild.xlsx"
    live.write_bytes(b"old workbook")
    staged.write_bytes(b"new workbook")

    with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
        upsert(conn, _row(), artifact_promotion=StagedFilePromotion(staged, live))

    assert live.read_bytes() == b"old workbook"
    assert staged.read_bytes() == b"new workbook"
    assert not list(tmp_path.glob("*.rollback.*"))
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_commit_failure_restores_workbook_and_assumptions_bundle(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    class FailingCommitConnection(sqlite3.Connection):
        def commit(self) -> None:
            raise sqlite3.OperationalError("synthetic commit failure")

    db = migrated_db(tmp_path / "isolated.sqlite")
    conn = sqlite3.connect(db, factory=FailingCommitConnection)
    workbook, assumptions = tmp_path / "META.xlsx", tmp_path / "META.json"
    workbook.write_bytes(b"accepted workbook")
    assumptions.write_bytes(b"accepted assumptions")
    candidate, candidate_json = tmp_path / "candidate.xlsx", tmp_path / "candidate.json"
    candidate.write_bytes(b"candidate workbook")
    candidate_json.write_bytes(b"candidate assumptions")
    try:
        with (
            pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"),
            StagedArtifactBundle([(candidate, workbook), (candidate_json, assumptions)]),
        ):
            upsert(conn, _row())
        assert workbook.read_bytes() == b"accepted workbook"
        assert assumptions.read_bytes() == b"accepted assumptions"
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
        assert not list(tmp_path.glob("*.rollback.*"))
    finally:
        conn.close()


def test_bundle_failed_recovery_retains_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workbook, assumptions = tmp_path / "META.xlsx", tmp_path / "META.json"
    workbook.write_bytes(b"accepted workbook")
    assumptions.write_bytes(b"accepted assumptions")
    candidate, candidate_json = tmp_path / "candidate.xlsx", tmp_path / "candidate.json"
    candidate.write_bytes(b"candidate workbook")
    candidate_json.write_bytes(b"candidate assumptions")
    original_replace = os.replace

    def fail_workbook_restore(source: Path, target: Path) -> None:
        if ".rollback." in source.name and target == workbook:
            raise OSError("synthetic recovery failure")
        original_replace(source, target)

    monkeypatch.setattr(artifact_promotion.os, "replace", fail_workbook_restore)
    with (
        pytest.raises(DcfRecoveryError, match="dcf_recovery_failed"),
        StagedArtifactBundle([(candidate, workbook), (candidate_json, assumptions)]),
    ):
        raise sqlite3.OperationalError("synthetic commit failure")
    assert assumptions.read_bytes() == b"accepted assumptions"
    backups = list(tmp_path.glob("META.rollback.*.xlsx"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"accepted workbook"


def _bridge_row(row: DcfRunRow, status: str) -> DcfRunRow:
    assert row.provenance is not None
    detail = dict(row.provenance.detail or {})
    detail["equity_bridge_receipt"] = {"status": status}
    return dataclasses.replace(row, provenance=dataclasses.replace(row.provenance, detail=detail))


def test_weaker_bridge_candidate_cannot_replace_verified_current() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    current = _bridge_row(_row(), "verified")
    assert upsert(conn, current) is True
    candidate = _bridge_row(dataclasses.replace(_row(), npv_per_share=101.0), "unverified")

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, candidate)

    assert blocked.value.decision.reason == "candidate_equity_bridge_unverified"
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 1
    assert (
        conn.execute("SELECT npv_per_share FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 100.0
    )


@pytest.mark.parametrize("status", ["missing", "unverified"])
def test_unverified_bridge_candidate_cannot_be_first_promotion(status: str) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    candidate = _row()
    assert candidate.provenance is not None
    detail = dict(candidate.provenance.detail or {})
    if status == "missing":
        detail.pop("equity_bridge_receipt", None)
    else:
        detail["equity_bridge_receipt"] = {"status": "unverified"}
    candidate = dataclasses.replace(
        candidate,
        provenance=dataclasses.replace(candidate.provenance, detail=detail),
    )

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, candidate)

    assert blocked.value.decision.reason == "candidate_equity_bridge_unverified"
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_equity_direct_archetype_uses_explicit_not_applicable_receipt(tmp_path: Path) -> None:
    workbook = tmp_path / "bank.xlsx"
    workbook.write_bytes(b"equity-direct model")
    provenance = build_file_provenance(
        ticker="META",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="bank_excess_return_v1",
        effective_inputs={},
        assumption_snapshot={},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=(),
        equity_direct_archetype="bank_excess_return",
    )
    assert provenance.detail is not None
    assert provenance.detail["equity_bridge_receipt"] == {
        "schema_version": "dcf_equity_bridge_receipt.v3",
        "status": "not_applicable",
        "reason_code": "equity_direct_valuation",
        "valuation_scope": "equity",
        "valuation_archetype": "bank_excess_return",
    }

    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    assert upsert(conn, dataclasses.replace(_row(), provenance=provenance)) is True


def test_not_applicable_status_without_typed_archetype_contract_is_blocked() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    candidate = _row()
    assert candidate.provenance is not None
    malformed = dataclasses.replace(
        candidate.provenance,
        detail={"equity_bridge_receipt": {"status": "not_applicable"}},
    )

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, dataclasses.replace(candidate, provenance=malformed))

    assert blocked.value.decision.reason == "candidate_equity_bridge_unverified"
    assert blocked.value.decision.candidate_bridge_status == "unverified"


@pytest.mark.parametrize(
    ("receipt_key", "malformed_value", "expected_status"),
    [("status", [], "missing"), ("valuation_archetype", [], "unverified")],
)
def test_structurally_malformed_receipt_returns_typed_hold(
    receipt_key: str, malformed_value: object, expected_status: str
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    candidate = _row()
    assert candidate.provenance is not None
    receipt: dict[str, object] = {
        "schema_version": "dcf_equity_bridge_receipt.v3",
        "status": "not_applicable",
        "reason_code": "equity_direct_valuation",
        "valuation_scope": "equity",
        "valuation_archetype": "bank_excess_return",
    }
    receipt[receipt_key] = malformed_value
    malformed = dataclasses.replace(
        candidate.provenance,
        detail={"equity_bridge_receipt": receipt},
    )

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, dataclasses.replace(candidate, provenance=malformed))

    assert blocked.value.decision.reason == "candidate_equity_bridge_unverified"
    assert blocked.value.decision.candidate_bridge_status == expected_status
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_outlier_candidate_is_blocked_with_deterministic_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    candidate = dataclasses.replace(_row(), live_price=200.0)

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, candidate)

    decision = blocked.value.decision
    assert decision.reason == "outlier_requires_explicit_owner_review"
    assert decision.candidate_evidence == {
        "ticker": "META",
        "engine_version": "redesign_fcff_v1",
        "input_sha256": "a" * 64,
        "workbook_sha256": "b" * 64,
        "inputs_as_of": "2026-07-28T12:00:00+00:00",
        "equity_bridge_status": "verified",
        "sanity_flag": "outlier",
    }
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0


def test_reused_hash_cannot_hide_a_changed_calculation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert upsert(conn, row) is True

    changed = dataclasses.replace(row, npv=1_200.0, npv_per_share=120.0)
    assert upsert(conn, changed) is True

    versions = conn.execute(
        "SELECT npv_per_share, is_latest, superseded_by_id FROM dcf_runs ORDER BY id"
    ).fetchall()
    assert versions[0][0:2] == (100.0, 0)
    assert versions[0][2] == 2
    assert versions[1] == (120.0, 1, None)
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 4


def test_pacific_information_date_accepts_next_utc_day_before_midnight() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 14, 2, 0, tzinfo=UTC),
    )
    boundary = dataclasses.replace(
        row,
        valuation_date=date(2026, 8, 13),
        provenance=provenance,
    )

    assert upsert(conn, boundary) is True


def test_pacific_information_date_rejects_later_local_day() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
    )
    boundary = dataclasses.replace(
        row,
        valuation_date=date(2026, 8, 13),
        provenance=provenance,
    )

    with pytest.raises(ValueError, match="later than the Pacific valuation date"):
        upsert(conn, boundary)


def test_pacific_information_date_rejects_naive_cutoff() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    row = _row()
    assert row.provenance is not None
    provenance = dataclasses.replace(
        row.provenance,
        inputs_as_of=datetime(2026, 8, 13, 17, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        upsert(conn, dataclasses.replace(row, provenance=provenance))


def test_provenance_none_cannot_publish_an_unverified_first_run() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)

    with pytest.raises(DcfPromotionBlocked) as blocked:
        upsert(conn, dataclasses.replace(_row(), provenance=None))

    assert blocked.value.decision.reason == "candidate_equity_bridge_unverified"
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0


def test_invalid_source_rolls_back_with_the_parent_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    provenance = _row().provenance
    assert provenance is not None
    invalid = dataclasses.replace(
        _row(),
        provenance=dataclasses.replace(
            provenance,
            detail={
                "sources": [
                    {
                        "role": "income_statement",
                        "path": "data/source.json",
                        "sha256": "not-a-sha",
                    }
                ]
            },
        ),
    )

    with pytest.raises(ValueError, match="invalid SHA-256"):
        upsert(conn, invalid)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 0


def test_input_ledger_constraint_failure_preserves_the_current_run() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    current = _row()
    assert upsert(conn, current) is True
    provenance = current.provenance
    assert provenance is not None
    source = {
        "role": "income_statement",
        "path": "data/duplicate.json",
        "sha256": "d" * 64,
    }
    duplicate_inputs = dataclasses.replace(
        current,
        npv=1_100.0,
        provenance=dataclasses.replace(
            provenance,
            input_sha256="e" * 64,
            detail={
                "equity_bridge_receipt": {"status": "verified"},
                "sources": [source, source],
            },
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        upsert(conn, duplicate_inputs)

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1
    assert conn.execute("SELECT npv FROM dcf_runs WHERE is_latest=1").fetchone()[0] == 1_000.0
    assert conn.execute("SELECT COUNT(*) FROM dcf_run_inputs").fetchone()[0] == 2


def test_generated_workbook_does_not_fake_the_input_cutoff(tmp_path: Path) -> None:
    source = tmp_path / "assumptions.json"
    workbook = tmp_path / "model.xlsx"
    source.write_text('{"growth":0.1}', encoding="utf-8")
    workbook.write_bytes(b"generated workbook")
    source_time = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    workbook_time = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    os.utime(source, (source_time.timestamp(), source_time.timestamp()))
    os.utime(workbook, (workbook_time.timestamp(), workbook_time.timestamp()))

    provenance = build_file_provenance(
        ticker="META",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="test@1",
        effective_inputs={"growth": 0.1},
        assumption_snapshot={"growth": 0.1},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=((source, "owner_assumptions"),),
    )

    assert provenance.inputs_as_of == source_time
    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    typed_sources = cast("list[dict[str, object]]", sources)
    by_role = {item["role"]: item for item in typed_sources}
    assert by_role["owner_assumptions"]["influences_calculation"] is True
    assert by_role["calculation_workbook"]["influences_calculation"] is False


def test_staged_workbook_hash_uses_the_promoted_live_locator(tmp_path: Path) -> None:
    staged = tmp_path / "dcf" / "META.rebuild.xlsx"
    live = tmp_path / "dcf" / "META.xlsx"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"candidate workbook")

    provenance = build_file_provenance(
        ticker="META",
        repo_root=tmp_path,
        workbook_path=staged,
        workbook_locator_path=live,
        engine_version="test@1",
        effective_inputs={},
        assumption_snapshot={},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=(),
    )

    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    calculation = next(
        cast("dict[str, object]", source)
        for source in cast("list[object]", sources)
        if isinstance(source, dict) and source.get("role") == "calculation_workbook"
    )
    assert calculation["path"] == "dcf/META.xlsx"
    assert provenance.workbook_sha256 == hashlib.sha256(b"candidate workbook").hexdigest()


def test_live_workbook_locator_uses_promotion_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = tmp_path / "dcf" / "META.rebuild.xlsx"
    live = tmp_path / "dcf" / "META.xlsx"

    monkeypatch.delenv("DCF_PROMOTE_DEST", raising=False)
    assert live_path_from_env(staged) == staged

    monkeypatch.setenv("DCF_PROMOTE_DEST", str(live))
    assert live_path_from_env(staged) == live


def test_pre_overwrite_workbook_input_receipt_retains_the_exact_old_bytes(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "BN.xlsx"
    workbook.write_bytes(b"owner-edited yellow cells")
    source_time = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    os.utime(workbook, (source_time.timestamp(), source_time.timestamp()))
    receipt = build_file_source_record(workbook, role="owner_workbook_inputs", repo_root=tmp_path)
    assert receipt is not None

    workbook.write_bytes(b"new generated workbook")
    provenance = build_file_provenance(
        ticker="BN",
        repo_root=tmp_path,
        workbook_path=workbook,
        engine_version="holdco_sotp_v1",
        effective_inputs={"mark": 12.0},
        assumption_snapshot={"mark": 12.0},
        live_price=None,
        live_price_at=None,
        live_price_source=None,
        source_files=(),
        source_records=(receipt,),
    )

    assert provenance.inputs_as_of == source_time
    assert provenance.workbook_sha256 != receipt["sha256"]
    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    typed_sources = cast("list[dict[str, object]]", sources)
    owner_input = next(item for item in typed_sources if item["role"] == "owner_workbook_inputs")
    assert owner_input["sha256"] == receipt["sha256"]
