"""Contracts for immutable evidence replica and retrieval links (0218)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.evidence_links import (
    BlobLocationObservation,
    DocumentObservationLink,
    EvidenceLinkLedger,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0217_fact_selection_ledger"
LEDGER_HEAD = "0213_evidence_ledger_foundation"
HEAD = "0218_evidence_replica_links"
SHA_A = "a" * 64
SHA_B = "b" * 64
STAMP = datetime(2026, 7, 26, 12, 0, 0)


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "links.db"
    config = _config(db_path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, LEDGER_HEAD)
    command.stamp(config, PRIOR_HEAD)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_ledger(conn: sqlite3.Connection) -> None:
    ledger = EvidenceLedger(conn)
    assert ledger.persist(
        ContentBlob(
            sha256=SHA_A,
            byte_size=42,
            media_type="text/html",
            storage_uri="file:///evidence/acme/10q.html",
            recorded_at=STAMP,
        )
    ).created
    assert ledger.persist(
        SourceObservation(
            observation_id="observation-primary",
            idempotency_key="source:acme:primary",
            source_kind="sec_filing",
            source_url="https://www.sec.gov/Archives/acme-10q.htm",
            blob_sha256=SHA_A,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=SHA_B,
            collector_code_version="collector@1",
        )
    ).created
    assert ledger.persist(
        DocumentVersion(
            document_version_id="document-v1",
            document_key="ACME:10-Q:2026-06-30",
            version_sequence=1,
            observation_id="observation-primary",
            blob_sha256=SHA_A,
            issuer_id="0000123456",
            ticker="ACME",
            document_type="10-Q",
            form_type="10-Q",
            accession_number="0000123456-26-000042",
            exhibit_id=None,
            period_start=None,
            period_end=datetime(2026, 6, 30),
            as_of_at=datetime(2026, 6, 30),
            language="en",
            replaces_document_version_id=None,
            legacy_document_id=None,
            recorded_at=STAMP,
        )
    ).created


def _location(
    *,
    location_observation_id: str = "location-primary",
    idempotency_key: str = "location:primary",
    storage_uri: str = "file:///evidence/acme/10q.html",
    location_kind: Literal["local", "object", "archive", "mirror"] = "local",
    availability_state: Literal["present", "missing", "quarantined"] = "present",
    location_sequence: int = 1,
    supersedes_location_observation_id: str | None = None,
    verified_at: datetime = STAMP,
) -> BlobLocationObservation:
    return BlobLocationObservation(
        location_observation_id=location_observation_id,
        idempotency_key=idempotency_key,
        blob_sha256=SHA_A,
        storage_uri=storage_uri,
        location_kind=location_kind,
        availability_state=availability_state,
        location_sequence=location_sequence,
        verified_at=verified_at,
        verified_byte_size=42,
        verified_sha256=SHA_A,
        supersedes_location_observation_id=supersedes_location_observation_id,
        recorded_at=verified_at,
    )


def test_one_blob_can_have_multiple_immutable_replica_locations(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_ledger(conn)
        ledger = EvidenceLinkLedger(conn)
        assert ledger.persist_location(_location()).created
        assert ledger.persist_location(
            _location(
                location_observation_id="location-archive",
                idempotency_key="location:archive",
                storage_uri="s3://archive/acme/10q.html",
                location_kind="archive",
            )
        ).created
        current = conn.execute(
            "SELECT storage_uri, availability_state FROM v_evidence_blob_locations_current "
            "WHERE blob_sha256 = ? ORDER BY storage_uri",
            (SHA_A,),
        ).fetchall()
        assert current == [
            ("file:///evidence/acme/10q.html", "present"),
            ("s3://archive/acme/10q.html", "present"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE evidence_blob_location_observations SET availability_state = 'missing' "
                "WHERE location_observation_id = 'location-primary'"
            )
    finally:
        conn.close()


def test_location_availability_is_a_chained_revision_not_an_overwrite(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_ledger(conn)
        ledger = EvidenceLinkLedger(conn)
        assert ledger.persist_location(_location()).created
        missing = _location(
            location_observation_id="location-primary-missing",
            idempotency_key="location:primary:missing",
            availability_state="missing",
            location_sequence=2,
            supersedes_location_observation_id="location-primary",
            verified_at=STAMP + timedelta(hours=1),
        )
        assert ledger.persist_location(missing).created
        assert conn.execute(
            "SELECT availability_state FROM v_evidence_blob_locations_current "
            "WHERE location_observation_id = 'location-primary-missing'"
        ).fetchone() == ("missing",)
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_blob_location_observations"
        ).fetchone() == (2,)
        invalid = _location(
            location_observation_id="location-invalid",
            idempotency_key="location:invalid",
            availability_state="present",
            location_sequence=3,
            supersedes_location_observation_id="location-primary",
            verified_at=STAMP + timedelta(hours=2),
        )
        with pytest.raises(sqlite3.IntegrityError, match="prior location revision"):
            ledger.persist_location(invalid)
    finally:
        conn.close()


def test_same_bytes_can_be_retrieved_again_and_linked_to_one_document(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_ledger(conn)
        core = EvidenceLedger(conn)
        assert core.persist(
            SourceObservation(
                observation_id="observation-mirror",
                idempotency_key="source:acme:mirror",
                source_kind="issuer_site",
                source_url="https://investors.example/acme-10q.html",
                blob_sha256=SHA_A,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP + timedelta(hours=1),
                retrieved_at=STAMP + timedelta(hours=1),
                retrieval_config_sha256=SHA_B,
                collector_code_version="collector@1",
            )
        ).created
        ledger = EvidenceLinkLedger(conn)
        assert ledger.persist_link(
            DocumentObservationLink(
                link_id="link-primary",
                document_version_id="document-v1",
                observation_id="observation-primary",
                link_kind="primary",
                linked_at=STAMP,
            )
        ).created
        assert ledger.persist_link(
            DocumentObservationLink(
                link_id="link-mirror",
                document_version_id="document-v1",
                observation_id="observation-mirror",
                link_kind="mirror",
                linked_at=STAMP + timedelta(hours=1),
            )
        ).created
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_document_observation_links "
            "WHERE document_version_id = 'document-v1'"
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT document_version_id FROM v_evidence_document_current "
            "WHERE document_key = 'ACME:10-Q:2026-06-30'"
        ).fetchone() == ("document-v1",)
    finally:
        conn.close()


def test_link_mismatch_and_replay_conflicts_fail_loudly(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    try:
        _seed_ledger(conn)
        core = EvidenceLedger(conn)
        assert core.persist(
            ContentBlob(
                sha256=SHA_B,
                byte_size=9,
                media_type="text/plain",
                storage_uri="file:///other",
                recorded_at=STAMP,
            )
        ).created
        assert core.persist(
            SourceObservation(
                observation_id="observation-other-bytes",
                idempotency_key="source:other",
                source_kind="other",
                source_url="https://example.test/other",
                blob_sha256=SHA_B,
                source_published_at=None,
                filing_at=None,
                accepted_at=None,
                observed_at=STAMP,
                retrieved_at=STAMP,
                retrieval_config_sha256=SHA_A,
                collector_code_version="collector@1",
            )
        ).created
        ledger = EvidenceLinkLedger(conn)
        mismatch = DocumentObservationLink(
            link_id="link-mismatch",
            document_version_id="document-v1",
            observation_id="observation-other-bytes",
            link_kind="retrieval",
            linked_at=STAMP,
        )
        with pytest.raises(sqlite3.IntegrityError, match="same blob"):
            ledger.persist_link(mismatch)
        assert ledger.persist_location(_location()).created
        changed = _location(availability_state="missing")
        with pytest.raises(ValueError, match="conflicts"):
            ledger.persist_location(changed)
        wrong_hash = _location(
            location_observation_id="location-wrong-hash",
            idempotency_key="location:wrong-hash",
        ).model_copy(update={"verified_sha256": SHA_B})
        with pytest.raises(ValueError, match="verified_sha256"):
            ledger.persist_location(wrong_hash)
    finally:
        conn.close()


def test_migration_seeds_primary_links_locations_and_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "seed.db"
    config = _config(db_path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, LEDGER_HEAD)
    conn = sqlite3.connect(db_path)
    try:
        _seed_ledger(conn)
        conn.commit()
    finally:
        conn.close()
    command.stamp(config, PRIOR_HEAD)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_blob_location_observations"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT link_kind FROM evidence_document_observation_links"
        ).fetchone() == ("primary",)
    finally:
        conn.close()
    command.downgrade(config, PRIOR_HEAD)
    conn = sqlite3.connect(db_path)
    try:
        objects = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        assert "evidence_blob_location_observations" not in objects
        assert "v_evidence_document_current" not in objects
    finally:
        conn.close()


def test_migration_handles_minimal_stamped_schema_without_seed_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "minimal.db"
    config = _config(db_path)
    command.stamp(config, PRIOR_HEAD)
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_blob_location_observations"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM evidence_document_observation_links"
        ).fetchone() == (0,)
    finally:
        conn.close()
