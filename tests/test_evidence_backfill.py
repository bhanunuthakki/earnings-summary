"""Contracts for the bounded legacy-to-evidence-ledger backfill."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_backfill import (
    BackfillRequest,
    backfill_legacy_evidence,
    ensure_legacy_document_evidence,
)
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.integrity_audit import AuditOptions, audit_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config(db_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def _seed_legacy_schema(db_path: Path, repo_root: Path) -> None:
    raw_path = repo_root / "data" / "ACME_10q.html"
    raw_path.parent.mkdir(parents=True)
    raw_bytes = b"<html>official filing</html>"
    raw_path.write_bytes(raw_bytes)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, source_type TEXT NOT NULL,
                doc_type TEXT NOT NULL, period_start TIMESTAMP, period_end TIMESTAMP,
                file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
                fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL, source_url TEXT,
                accession_number TEXT
            );
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
                fiscal_period_type TEXT, period_end TIMESTAMP, is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE transcript_segments (
                id INTEGER PRIMARY KEY, transcript_id INTEGER NOT NULL, seq INTEGER NOT NULL,
                speaker TEXT, speaker_role TEXT, time_code_start TEXT, time_code_end TEXT,
                text TEXT NOT NULL
            );
            CREATE TABLE filing_sections (
                id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, source TEXT NOT NULL,
                source_ref TEXT NOT NULL, doc_id INTEGER, accession_number TEXT, form TEXT NOT NULL,
                fiscal_period TEXT NOT NULL, section_key_raw TEXT NOT NULL, ordinal INTEGER NOT NULL,
                text TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'ACME', 'sec_edgar', '10-Q', NULL, ?, ?, ?, ?, "
            "'ok', ?, 'https://sec.example/acme-10q', '0000000001-26-000001')",
            ("2026-06-30", "data/ACME_10q.html", raw_sha, "2026-07-20 12:00:00", len(raw_bytes)),
        )
        conn.execute("INSERT INTO transcripts VALUES (1, 1, 'ACME', 'Q2', '2026-06-30', 1)")
        conn.execute("INSERT INTO transcripts VALUES (2, 1, 'ACME', 'Q2', '2026-06-30', 0)")
        conn.execute(
            "INSERT INTO transcript_segments VALUES (1, 1, 0, 'CEO', 'executive', '00:00', "
            "'00:05', 'Revenue grew 20%.')"
        )
        conn.execute(
            "INSERT INTO transcript_segments VALUES (2, 2, 0, 'CEO', 'executive', '00:00', "
            "'00:05', 'Superseded transcript.')"
        )
        conn.execute(
            "INSERT INTO filing_sections VALUES (1, 'ACME', 'edgar_text', 'acc-1', 1, "
            "'0000000001-26-000001', '10-Q', 'Q2', 'Item 1A', 0, 'Risks changed.', 1)"
        )
        conn.execute(
            "INSERT INTO filing_sections VALUES (2, 'ACME', 'edgar_text', 'acc-1', 1, "
            "'0000000001-26-000001', '10-Q', 'Q2', 'Item 1A', 1, 'Old risks.', 0)"
        )
        conn.commit()
    finally:
        conn.close()


def _connection(tmp_path: Path) -> tuple[sqlite3.Connection, Path, Path]:
    db_path = tmp_path / "portfolio.db"
    repo_root = tmp_path / "repo"
    _seed_legacy_schema(db_path, repo_root)
    config = _config(db_path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0213_evidence_ledger_foundation")
    command.stamp(config, "0217_fact_selection_ledger")
    command.upgrade(config, "0218_evidence_replica_links")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, db_path, repo_root


def _request(
    repo_root: Path, *, apply: bool, batch_size: int = 100, task_id: str = "evidence-ledger-test"
) -> BackfillRequest:
    return BackfillRequest(
        repo_root=repo_root,
        apply=apply,
        batch_size=batch_size,
        task_id=task_id,
    )


def _install_binding_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE legacy_document_evidence_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE NOT NULL,
            legacy_document_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            document_version_id TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL,
            scope_locator_json TEXT NOT NULL,
            scope_locator_sha256 TEXT NOT NULL,
            scope_content_sha256 TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            knowledge_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            supersedes_binding_revision_id TEXT
        );
        CREATE VIEW v_legacy_document_evidence_bindings_current AS
        SELECT binding.* FROM legacy_document_evidence_binding_revisions AS binding
        WHERE NOT EXISTS (
            SELECT 1 FROM legacy_document_evidence_binding_revisions AS newer
            WHERE newer.legacy_document_id = binding.legacy_document_id
              AND newer.revision > binding.revision
        );
        """
    )


def _seed_legacy_file_observation(
    conn: sqlite3.Connection,
    repo_root: Path,
    source_url: str,
    *,
    blob_sha256: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT source_type, sha256, fetched_at, raw_bytes_size FROM documents WHERE id = 1"
    ).fetchone()
    assert row is not None
    observed_at = datetime.fromisoformat(str(row[2]))
    raw_path = repo_root / "data" / "ACME_10q.html"
    stored_blob_sha256 = blob_sha256 or str(row[1])
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=stored_blob_sha256,
            byte_size=int(row[3]),
            media_type="text/html",
            storage_uri=raw_path.as_uri(),
            recorded_at=observed_at,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="legacy-obs-1",
            idempotency_key="legacy-document:1:observation",
            source_kind=str(row[0]),
            source_url=source_url,
            blob_sha256=stored_blob_sha256,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=observed_at,
            retrieved_at=observed_at,
            retrieval_config_sha256=hashlib.sha256(
                b"legacy-evidence-backfill-config-v1"
            ).hexdigest(),
            collector_code_version="evidence-backfill@1",
        )
    )
    conn.commit()


def test_dry_run_plans_complete_chain_without_ledger_writes(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        result = backfill_legacy_evidence(conn, _request(repo_root, apply=False))
        assert result.dry_run is True
        assert result.documents_backfilled == 1
        assert result.filing_sections_backfilled == 1
        assert result.transcript_segments_backfilled == 1
        assert conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0] == 0
    finally:
        conn.close()


def test_apply_is_idempotent_and_uses_only_active_evidence(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        first = backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        assert first.records_created == 9
        assert conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0] == 3
        texts = {row[0] for row in conn.execute("SELECT text FROM evidence_nodes")}
        assert "Superseded transcript." not in texts
        assert "Old risks." not in texts
        document_version = conn.execute(
            "SELECT issuer_id, language FROM evidence_document_versions"
        ).fetchone()
        assert tuple(document_version) == ("legacy-ticker:ACME", "und")
        assert (
            conn.execute(
                "SELECT availability_state FROM v_evidence_blob_locations_current"
            ).fetchone()[0]
            == "present"
        )
        assert (
            conn.execute("SELECT link_kind FROM evidence_document_observation_links").fetchone()[0]
            == "primary"
        )
        audit = audit_connection(conn, AuditOptions())
        assert not any(
            finding.code.startswith("EVIDENCE_BLOB_")
            or finding.code.startswith("EVIDENCE_DOCUMENT_PRIMARY")
            for finding in audit.findings
        )
        locators = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT node_kind, locator_json FROM evidence_nodes WHERE locator_json IS NOT NULL"
            )
        }
        assert locators["section"] == {
            "filing_ordinal": 0,
            "filing_section_key_raw": "Item 1A",
            "legacy_row_id": 1,
            "legacy_table": "filing_sections",
            "source_ref": "acc-1",
        }
        assert locators["transcript_turn"] == {
            "legacy_row_id": 1,
            "legacy_table": "transcript_segments",
            "transcript_speaker": "CEO",
            "transcript_time_code_end": "00:05",
            "transcript_time_code_start": "00:00",
            "transcript_turn_sequence": 0,
        }

        second = backfill_legacy_evidence(
            conn, _request(repo_root, apply=True, task_id="evidence-ledger-replay")
        )
        assert second.records_created == 0
        assert second.records_replayed == 9
    finally:
        conn.close()


def test_targeted_legacy_document_capture_creates_idempotent_root_binding(
    tmp_path: Path,
) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        _install_binding_schema(conn)

        first = ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=1)
        second = ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=1)

        binding = conn.execute(
            "SELECT legacy_document_id,revision,document_version_id,evidence_node_id,"
            "scope_content_sha256 FROM v_legacy_document_evidence_bindings_current"
        ).fetchone()
        assert tuple(binding) == (
            1,
            1,
            "legacy-doc-1",
            "legacy-node-doc-1",
            conn.execute("SELECT sha256 FROM documents WHERE id=1").fetchone()[0],
        )
        assert first.records_created == 10
        assert second.records_created == 0
        assert second.records_replayed == 10
    finally:
        conn.close()


def test_binding_aware_dry_run_accounts_for_the_planned_root_binding(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        _install_binding_schema(conn)

        result = backfill_legacy_evidence(conn, _request(repo_root, apply=False))

        assert result.records_planned == 10
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_exact_replay_does_not_reinsert_into_a_sealed_extraction_run(
    tmp_path: Path,
) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        first = backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        assert first.records_created == 9
        conn.execute(
            "CREATE TRIGGER reject_sealed_extraction_run_reinsert "
            "BEFORE INSERT ON evidence_nodes "
            "WHEN NEW.extraction_run_id = 'legacy-run-doc-1' "
            "BEGIN SELECT RAISE(ABORT, 'fact extraction run is sealed'); END"
        )

        replay = backfill_legacy_evidence(
            conn,
            _request(repo_root, apply=True, task_id="evidence-ledger-sealed-replay"),
        )

        assert replay.records_created == 0
        assert replay.records_replayed == 9
    finally:
        conn.close()


def test_apply_reuses_verified_legacy_file_observation_from_a_clone_root(
    tmp_path: Path,
) -> None:
    conn, _, source_root = _connection(tmp_path)
    try:
        conn.execute("UPDATE documents SET source_url = NULL WHERE id = 1")
        conn.commit()
        source_uri = (source_root / "data" / "ACME_10q.html").as_uri()
        _seed_legacy_file_observation(conn, source_root, source_uri)

        clone_root = tmp_path / "clone"
        clone_path = clone_root / "data" / "ACME_10q.html"
        clone_path.parent.mkdir(parents=True)
        clone_path.write_bytes((source_root / "data" / "ACME_10q.html").read_bytes())

        result = backfill_legacy_evidence(
            conn, _request(clone_root, apply=True, task_id="clone-replay")
        )

        assert result.records_created == 7
        assert result.records_replayed == 2
        assert (
            conn.execute(
                "SELECT source_url FROM evidence_source_observations "
                "WHERE idempotency_key = 'legacy-document:1:observation'"
            ).fetchone()[0]
            == source_uri
        )
        assert tuple(
            conn.execute(
                "SELECT storage_uri, verified_sha256, availability_state "
                "FROM v_evidence_blob_locations_current "
                "WHERE blob_sha256 = (SELECT sha256 FROM documents WHERE id = 1) "
                "AND storage_uri = ?",
                (clone_path.as_uri(),),
            ).fetchone()
        ) == (
            clone_path.as_uri(),
            conn.execute("SELECT sha256 FROM documents WHERE id = 1").fetchone()[0],
            "present",
        )
    finally:
        conn.close()


def test_apply_uses_a_root_independent_uri_for_new_legacy_local_documents(
    tmp_path: Path,
) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        conn.execute("UPDATE documents SET source_url = NULL WHERE id = 1")
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = 1", ("data/tmp/../ACME_10q.html",)
        )
        conn.commit()

        backfill_legacy_evidence(conn, _request(repo_root, apply=True))

        assert (
            conn.execute(
                "SELECT source_url FROM evidence_source_observations "
                "WHERE idempotency_key = 'legacy-document:1:observation'"
            ).fetchone()[0]
            == "legacy-corpus:///data/ACME_10q.html"
        )
    finally:
        conn.close()


def test_apply_replaces_new_local_file_uri_with_a_logical_corpus_uri(
    tmp_path: Path,
) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        conn.execute(
            "UPDATE documents SET source_url = ? WHERE id = 1",
            ((repo_root / "data" / "ACME_10q.html").as_uri(),),
        )
        conn.commit()

        backfill_legacy_evidence(conn, _request(repo_root, apply=True))

        assert (
            conn.execute(
                "SELECT source_url FROM evidence_source_observations "
                "WHERE idempotency_key = 'legacy-document:1:observation'"
            ).fetchone()[0]
            == "legacy-corpus:///data/ACME_10q.html"
        )
    finally:
        conn.close()


def test_clone_replay_rejects_existing_legacy_observation_with_a_different_blob(
    tmp_path: Path,
) -> None:
    conn, _, source_root = _connection(tmp_path)
    try:
        conn.execute("UPDATE documents SET source_url = NULL WHERE id = 1")
        conn.commit()
        _seed_legacy_file_observation(
            conn,
            source_root,
            "file:///C:/unrelated/data/ACME_10q.html?alternate-origin",
            blob_sha256="f" * 64,
        )

        clone_root = tmp_path / "clone"
        clone_path = clone_root / "data" / "ACME_10q.html"
        clone_path.parent.mkdir(parents=True)
        clone_path.write_bytes((source_root / "data" / "ACME_10q.html").read_bytes())

        with pytest.raises(ValueError, match="different blob hash"):
            backfill_legacy_evidence(
                conn, _request(clone_root, apply=True, task_id="clone-replay-reject")
            )
    finally:
        conn.close()


def test_apply_records_a_new_present_revision_after_location_was_missing(
    tmp_path: Path,
) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        current = conn.execute(
            "SELECT location_observation_id, blob_sha256, storage_uri, recorded_at "
            "FROM v_evidence_blob_locations_current"
        ).fetchone()
        missing_at = datetime(2026, 7, 25, 12, 0, 0)
        EvidenceLinkLedger(conn).persist_location(
            BlobLocationObservation(
                location_observation_id="location-missing-r2",
                idempotency_key="location-missing-r2",
                blob_sha256=str(current[1]),
                storage_uri=str(current[2]),
                location_kind="local",
                availability_state="missing",
                location_sequence=2,
                verified_at=missing_at,
                supersedes_location_observation_id=str(current[0]),
                recorded_at=missing_at,
            )
        )
        conn.commit()

        result = backfill_legacy_evidence(
            conn, _request(repo_root, apply=True, task_id="evidence-location-reappeared")
        )

        assert result.records_created == 1
        assert tuple(
            conn.execute(
                "SELECT availability_state, location_sequence "
                "FROM v_evidence_blob_locations_current"
            ).fetchone()
        ) == ("present", 3)
    finally:
        conn.close()


def test_active_only_falls_back_when_lifecycle_views_are_absent(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        result = backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        assert result.selection_modes == {
            "filing_sections": "lifecycle_filter",
            "transcripts": "lifecycle_filter",
        }
    finally:
        conn.close()


def test_hash_mismatch_is_quarantined_without_evidence_writes(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        conn.execute("UPDATE documents SET sha256 = ? WHERE id = 1", ("0" * 64,))
        conn.commit()
        result = backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_quarantined == 1
        assert result.records_created == 0
        assert result.finding_counts == {"sha256_mismatch": 1}
        assert all(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in (
                "evidence_content_blobs",
                "evidence_source_observations",
                "evidence_document_versions",
                "evidence_extraction_runs",
                "evidence_nodes",
                "evidence_blob_location_observations",
                "evidence_document_observation_links",
            )
        )
    finally:
        conn.close()


def test_apply_checkpoints_and_resumes_bounded_documents(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        raw_path = repo_root / "data" / "ACME_10q.html"
        raw = raw_path.read_bytes()
        beta_path = repo_root / "data" / "BETA_deck.html"
        beta_path.write_bytes(raw)
        conn.execute(
            "INSERT INTO documents VALUES (2, 'BETA', 'ir_doc', 'investor_presentation', NULL, NULL, "
            "?, ?, ?, 'ok', ?, NULL, NULL)",
            ("data/BETA_deck.html", hashlib.sha256(raw).hexdigest(), "2026-07-21", len(raw)),
        )
        conn.commit()
        first = backfill_legacy_evidence(conn, _request(repo_root, apply=True, batch_size=1))
        assert first.has_more is True
        state = repo_root / ".tmp" / "evidence-ledger-test" / "state.json"
        assert json.loads(state.read_text(encoding="utf-8"))["last_document_id"] == 1
        second = backfill_legacy_evidence(conn, _request(repo_root, apply=True, batch_size=1))
        assert second.documents_backfilled == 1
        assert second.has_more is False
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 2
    finally:
        conn.close()


def test_explicit_document_id_is_bounded_and_checkpoint_free(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        raw = b"<html>second official filing</html>"
        path = repo_root / "data" / "BETA_10q.html"
        path.write_bytes(raw)
        conn.execute(
            "INSERT INTO documents VALUES (2, 'BETA', 'sec_edgar', '10-Q', NULL, NULL, "
            "?, ?, '2026-07-21', 'ok', ?, NULL, NULL)",
            ("data/BETA_10q.html", hashlib.sha256(raw).hexdigest(), len(raw)),
        )
        conn.commit()

        request = BackfillRequest(
            repo_root=repo_root,
            apply=True,
            document_id=2,
            task_id="target-document-two",
        )
        result = backfill_legacy_evidence(conn, request)

        assert result.documents_considered == 1
        assert result.last_document_id_before == 1
        assert result.last_document_id_after == 2
        assert result.has_more is False
        assert not (repo_root / ".tmp" / request.task_id / "state.json").exists()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_document_versions WHERE legacy_document_id=1"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_document_versions WHERE legacy_document_id=2"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_summary_is_json_serializable_for_cli_stdout(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        result = backfill_legacy_evidence(conn, _request(repo_root, apply=False))
        payload = json.loads(result.model_dump_json())
        assert payload["task_id"] == "evidence-ledger-test"
        assert datetime.fromisoformat(payload["run_at"].replace("Z", "+00:00"))
    finally:
        conn.close()


def test_output_hash_changes_when_emitted_active_evidence_changes(tmp_path: Path) -> None:
    first_conn, _, first_root = _connection(tmp_path)
    changed_dir = tmp_path / "changed"
    changed_dir.mkdir()
    second_conn, _, second_root = _connection(changed_dir)
    try:
        backfill_legacy_evidence(first_conn, _request(first_root, apply=True))
        first_hash = first_conn.execute(
            "SELECT output_sha256 FROM evidence_extraction_runs"
        ).fetchone()[0]
        second_conn.execute(
            "UPDATE filing_sections SET text = 'Risks materially changed.' WHERE id = 1"
        )
        second_conn.commit()
        backfill_legacy_evidence(second_conn, _request(second_root, apply=True))
        second_hash = second_conn.execute(
            "SELECT output_sha256 FROM evidence_extraction_runs"
        ).fetchone()[0]
        assert first_hash != second_hash
    finally:
        first_conn.close()
        second_conn.close()


def test_sec_fragment_is_retained_but_not_treated_as_a_filename(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        conn.execute(
            "UPDATE documents SET file_path = ? WHERE id = 1", ("data/ACME_10q.html#accn=7",)
        )
        conn.commit()
        result = backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        assert result.documents_backfilled == 1
        locator = json.loads(
            conn.execute(
                "SELECT locator_json FROM evidence_nodes WHERE node_id = 'legacy-node-doc-1'"
            ).fetchone()[0]
        )
        assert locator["source_ref"] == "data/ACME_10q.html#accn=7"
    finally:
        conn.close()


def test_section_uses_legacy_created_clock_when_available(tmp_path: Path) -> None:
    conn, _, repo_root = _connection(tmp_path)
    try:
        conn.execute("ALTER TABLE filing_sections ADD COLUMN created_at TIMESTAMP")
        conn.execute("UPDATE filing_sections SET created_at = '2026-07-19 10:30:00' WHERE id = 1")
        conn.commit()
        backfill_legacy_evidence(conn, _request(repo_root, apply=True))
        recorded_at = conn.execute(
            "SELECT recorded_at FROM evidence_nodes WHERE node_id = 'legacy-node-filing-section-1'"
        ).fetchone()[0]
        assert str(recorded_at).startswith("2026-07-19 10:30:00")
    finally:
        conn.close()


def test_cli_writes_only_summary_json_to_stdout(tmp_path: Path) -> None:
    conn, db_path, repo_root = _connection(tmp_path)
    conn.close()
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution" / "backfill_evidence_ledger.py"),
            "--db",
            str(db_path),
            "--repo-root",
            str(repo_root),
            "--task-id",
            "evidence-ledger-cli",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout)["dry_run"] is True
    assert all(json.loads(line)["event"] for line in completed.stderr.splitlines() if line)
