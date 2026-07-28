"""Expectation inventories may shrink only through an explicit authority revision."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.source_coverage_reconcile import (
    ExpectedDocumentImport,
    ExpectedDocumentWithdrawalImport,
    ExplicitAbsence,
    InventoryComponentImport,
    SourceCoverageImport,
    reconcile_source_coverage,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 4, 0, 0)
A, B = "a" * 64, "b" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "expectation-lifecycle.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0224_expected_document_lifecycle")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=A,
            byte_size=10,
            media_type="application/json",
            storage_uri="file:///sec-submissions.json",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="authority-observation",
            idempotency_key="authority-observation",
            source_kind="sec",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
            blob_sha256=A,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=B,
            collector_code_version="sec-submissions@1",
        )
    )
    conn.commit()
    return conn


def _expected() -> ExpectedDocumentImport:
    return ExpectedDocumentImport(
        expected_document_key="ACME:2026Q1:10-Q",
        source_kind="sec_filing",
        document_type="filing",
        form_type="10-Q",
        accession_number="0000000001-26-000001",
        expectation_basis="authoritative",
        absence=ExplicitAbsence(
            coverage_status="not_discovered",
            reason_code="package_fetch_pending",
            reason_details=(("accession", "0000000001-26-000001"),),
        ),
    )


def _request(
    revision: int,
    *,
    expected_documents: tuple[ExpectedDocumentImport, ...] = (),
    withdrawals: tuple[ExpectedDocumentWithdrawalImport, ...] = (),
) -> SourceCoverageImport:
    stamp = STAMP + timedelta(hours=revision - 1)
    return SourceCoverageImport(
        inventory_key="ACME:sec-submissions",
        revision=revision,
        issuer_id="issuer-acme",
        ticker="ACME",
        source_kind="sec_submissions",
        source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        source_observation_id="authority-observation",
        outcome="succeeded",
        authoritative=True,
        retrieval_config_sha256=B,
        collector_code_version="sec-submissions@1",
        started_at=stamp,
        completed_at=stamp,
        recorded_at=stamp,
        reconciled_at=stamp,
        components=(
            InventoryComponentImport(
                component_key="sec-submissions-primary",
                component_kind="primary",
                source_url="https://data.sec.gov/submissions/CIK0000000001.json",
                source_observation_id="authority-observation",
                outcome="succeeded",
                required=True,
                ordinal=0,
            ),
        ),
        expected_documents=expected_documents,
        withdrawals=withdrawals,
        apply=True,
    )


def test_exact_replay_does_not_invent_an_expectation_revision(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        request = _request(1, expected_documents=(_expected(),))
        first = reconcile_source_coverage(conn, request)
        replay = reconcile_source_coverage(conn, request)

        assert first.records_created == 6
        assert replay.records_created == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM expected_document_lifecycle_revisions").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_lifecycle_uses_canonical_append_only_trigger_contract(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        trigger_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND tbl_name = 'expected_document_lifecycle_revisions'"
            )
        }
        assert "trg_expected_document_lifecycle_revisions_append_only" in trigger_names
        assert "trg_expected_document_lifecycle_revisions_append_only_delete" in trigger_names
    finally:
        conn.close()


def test_disappearing_expectation_requires_exact_withdrawal_and_is_not_deleted(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    try:
        reconcile_source_coverage(conn, _request(1, expected_documents=(_expected(),)))

        with pytest.raises(ValueError, match="missing withdrawals"):
            reconcile_source_coverage(conn, _request(2))
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 1

        withdrawal = ExpectedDocumentWithdrawalImport(
            expected_document_key="ACME:2026Q1:10-Q",
            status="withdrawn_by_authority",
            reason_code="authority_removed_accession",
            reason_details=(("authority", "SEC submissions revision 2"),),
        )
        result = reconcile_source_coverage(conn, _request(2, withdrawals=(withdrawal,)))

        assert result.records_created == 4
        assert conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == 1
        current = conn.execute(
            "SELECT status, revision, source_inventory_snapshot_id "
            "FROM v_expected_document_lifecycle_current"
        ).fetchone()
        assert current is not None
        assert (current[0], current[1]) == ("withdrawn_by_authority", 2)
        assert str(current[2]).startswith("source-snapshot:")
    finally:
        conn.close()
