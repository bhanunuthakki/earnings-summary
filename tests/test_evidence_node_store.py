"""Unit and integration tests for migration 0072, evidence_node_store, source_viewers, and integrity_audit."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from pipeline.source_viewers import load_document_version, render_document_version_page
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    EvidenceLocator,
    EvidenceNode,
    ExtractionRun,
    SourceObservation,
)
from provenance.evidence_node_store import (
    EvidenceArchiveError,
    archive_receipt_for_run,
    load_archived_run_nodes,
    load_run_nodes,
    write_run_nodes_archive,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_run_with_nodes(
    conn: sqlite3.Connection, *, run_id: str = "run-arch-1"
) -> tuple[str, str]:
    """Seed one valid extraction run with a document, table_row, and two table_cell nodes."""
    blob_bytes = b"<html><body><table><tr><td>Revenue</td><td>100</td></tr></table></body></html>"
    blob_sha = hashlib.sha256(blob_bytes).hexdigest()
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=blob_sha,
            byte_size=len(blob_bytes),
            media_type="text/html",
            storage_uri="file:///synthetic/source.html",
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs-arch-1",
            idempotency_key="obs-arch-1",
            source_kind="sec_filing_document",
            source_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm",
            blob_sha256=blob_sha,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="test.v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="doc-arch-1",
            document_key="doc-arch-key-1",
            version_sequence=1,
            observation_id="obs-arch-1",
            blob_sha256=blob_sha,
            issuer_id="issuer-arch-1",
            ticker="TEST",
            document_type="filing",
            form_type="10-Q",
            accession_number="0000000001-26-000001",
            period_start=datetime(2026, 4, 1, tzinfo=UTC),
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            language="en",
            recorded_at=STAMP,
        )
    )
    output_sha = "b" * 64
    ledger.persist(
        ExtractionRun(
            extraction_run_id=run_id,
            idempotency_key=run_id,
            document_version_id="doc-arch-1",
            input_sha256=blob_sha,
            extractor_name="fulltext-evidence-backfill",
            extractor_config_sha256="c" * 64,
            extractor_code_version="fulltext-evidence-backfill@4-replica-aware-web-archive",
            output_sha256=output_sha,
            started_at=STAMP,
            completed_at=STAMP,
            outcome="succeeded",
        )
    )
    doc_node = EvidenceNode(
        node_id="node-01-doc",
        evidence_key="node-01-doc",
        revision=1,
        extraction_run_id=run_id,
        node_kind="document",
        text="Revenue 100",
        locator=EvidenceLocator(
            source_ref="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm"
        ),
        recorded_at=STAMP,
    )
    row_node = EvidenceNode(
        node_id="node-02-row",
        evidence_key="node-02-row",
        revision=1,
        extraction_run_id=run_id,
        parent_node_id="node-01-doc",
        node_kind="table_row",
        text="Revenue | 100",
        locator=EvidenceLocator(
            source_ref="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm",
            table_name="table-1",
            table_row_index=1,
        ),
        recorded_at=STAMP,
    )
    cell_a = EvidenceNode(
        node_id="node-03-cell-a",
        evidence_key="node-03-cell-a",
        revision=1,
        extraction_run_id=run_id,
        parent_node_id="node-02-row",
        node_kind="table_cell",
        text="Revenue",
        locator=EvidenceLocator(
            source_ref="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm",
            table_name="table-1",
            table_row_index=1,
            table_column_index=0,
        ),
        recorded_at=STAMP,
    )
    cell_b = EvidenceNode(
        node_id="node-04-cell-b",
        evidence_key="node-04-cell-b",
        revision=1,
        extraction_run_id=run_id,
        parent_node_id="node-02-row",
        node_kind="table_cell",
        text="100",
        locator=EvidenceLocator(
            source_ref="https://www.sec.gov/Archives/edgar/data/1/000000000126000001/doc.htm",
            table_name="table-1",
            table_row_index=1,
            table_column_index=1,
        ),
        recorded_at=STAMP,
    )
    for node in (doc_node, row_node, cell_a, cell_b):
        ledger.persist(node)
    conn.commit()
    return run_id, output_sha


def test_migration_0072_table_constraints_downgrade_and_reupgrade(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "mig0072.db")
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert row is not None and tuple(row) == ("0072_evidence_node_external_archives",)
        run_id, output_sha = _seed_run_with_nodes(conn)

        # Reject path traversal, absolute path, bad sha, count <= 0, non-array kinds.
        bad_cases: list[tuple[str, str, int, str, str]] = [
            ("../escape.jsonl.gz", "a" * 64, 1, '["table_cell"]', output_sha),
            ("/abs/path.jsonl.gz", "a" * 64, 1, '["table_cell"]', output_sha),
            ("data/derived_evidence/aa/ok.jsonl.gz", "NOT_HEX", 1, '["table_cell"]', output_sha),
            ("data/derived_evidence/aa/ok.jsonl.gz", "a" * 64, 0, '["table_cell"]', output_sha),
            (
                "data/derived_evidence/aa/ok.jsonl.gz",
                "a" * 64,
                1,
                '{"kind":"table_cell"}',
                output_sha,
            ),
        ]
        for relpath, sha, count, kinds_json, orig_sha in bad_cases:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO evidence_node_external_archives VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, relpath, sha, count, kinds_json, orig_sha, STAMP.isoformat()),
                )

        # Valid insert succeeds and passes table-scoped FK check.
        conn.execute(
            "INSERT INTO evidence_node_external_archives VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                "data/derived_evidence/ab/" + "a" * 64 + ".jsonl.gz",
                "d" * 64,
                2,
                '["table_cell"]',
                output_sha,
                STAMP.isoformat(),
            ),
        )
        conn.commit()
        assert (
            conn.execute("PRAGMA foreign_key_check(evidence_node_external_archives)").fetchall()
            == []
        )

    # Downgrade to 0071 removes the table cleanly.
    cfg = _alembic_config(db_path)
    command.downgrade(cfg, "0071_admit_aggregator_transcript_providers")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0071_admit_aggregator_transcript_providers",
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_node_external_archives'"
            ).fetchone()
            is None
        )

    # Verify migration 0072 uses table-scoped FK check only (never bare PRAGMA foreign_key_check).
    mod_source = (
        PROJECT_ROOT / "alembic" / "versions" / "0072_evidence_node_external_archives.py"
    ).read_text(encoding="utf-8")
    assert "PRAGMA foreign_key_check(evidence_node_external_archives)" in mod_source
    assert 'PRAGMA foreign_key_check"' not in mod_source
    assert "PRAGMA foreign_key_check'" not in mod_source


def test_archive_write_deterministic_roundtrip_and_fail_closed_checks(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "store.db")
    state_root = tmp_path / "state"
    state_root.mkdir()

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        run_id, output_sha = _seed_run_with_nodes(conn)
        cell_rows: list[dict[str, object]] = [
            dict(
                zip(
                    (
                        "native_rowid",
                        "node_id",
                        "evidence_key",
                        "revision",
                        "extraction_run_id",
                        "parent_node_id",
                        "supersedes_node_id",
                        "node_kind",
                        "text",
                        "locator_json",
                        "locator_sha256",
                        "recorded_at",
                    ),
                    tuple(row),
                    strict=True,
                )
            )
            for row in conn.execute(
                "SELECT rowid, node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
                "supersedes_node_id, node_kind, text, locator_json, locator_sha256, recorded_at "
                "FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell' "
                "ORDER BY rowid",
                (run_id,),
            ).fetchall()
        ]

        # Deterministic across reversed input order.
        art1 = write_run_nodes_archive(state_root, run_id, cell_rows)
        bytes1 = art1.archive_path.read_bytes()
        art2 = write_run_nodes_archive(state_root, run_id, list(reversed(cell_rows)))
        bytes2 = art2.archive_path.read_bytes()
        assert bytes1 == bytes2
        assert art1.archive_sha256 == art2.archive_sha256
        assert art1.archived_node_count == 2
        assert art1.archived_node_kinds == ("table_cell",)

        # Persist receipt and remove live cell rows to simulate post-compaction state.
        conn.execute("DROP TRIGGER trg_evidence_nodes_append_only_delete")
        conn.execute(
            "DELETE FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
            (run_id,),
        )
        conn.execute(
            "CREATE TRIGGER trg_evidence_nodes_append_only_delete BEFORE DELETE ON evidence_nodes "
            "BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END"
        )
        conn.execute(
            "INSERT INTO evidence_node_external_archives VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                art1.storage_relpath,
                art1.archive_sha256,
                art1.archived_node_count,
                json.dumps(list(art1.archived_node_kinds)),
                output_sha,
                STAMP.isoformat(),
            ),
        )
        conn.commit()

        # Round-trip load_archived_run_nodes and merged load_run_nodes.
        receipt = archive_receipt_for_run(conn, run_id)
        assert receipt is not None
        archived = load_archived_run_nodes(conn, run_id, state_root=state_root)
        assert [row["node_id"] for row in archived] == ["node-03-cell-a", "node-04-cell-b"]

        merged = load_run_nodes(conn, run_id, state_root=state_root)
        assert [row["node_id"] for row in merged] == [
            "node-01-doc",
            "node-02-row",
            "node-03-cell-a",
            "node-04-cell-b",
        ]
        merged_rowid = load_run_nodes(conn, run_id, state_root=state_root, include_rowid=True)
        assert [row["node_id"] for row in merged_rowid] == [
            "node-01-doc",
            "node-02-row",
            "node-03-cell-a",
            "node-04-cell-b",
        ]

        # Fail closed when state_root is missing.
        with pytest.raises(EvidenceArchiveError, match="state_root is required"):
            load_archived_run_nodes(conn, run_id, state_root=None)

        # Audit clean with valid state_root.
        clean_audit = audit_connection(conn, AuditOptions(state_root=state_root))
        assert not any(f.code.startswith("EVIDENCE_ARCHIVE_") for f in clean_audit.findings)

        # Corrupt archive bytes -> fail closed on load AND audit blocker.
        art1.archive_path.write_bytes(gzip.compress(b'{"node_id":"tampered"}\n'))
        with pytest.raises(EvidenceArchiveError, match="SHA-256 mismatch"):
            load_archived_run_nodes(conn, run_id, state_root=state_root)
        corrupt_audit = audit_connection(conn, AuditOptions(state_root=state_root))
        assert any(f.code == "EVIDENCE_ARCHIVE_FILE_INVALID" for f in corrupt_audit.findings)

        # Missing file -> fail closed.
        art1.archive_path.unlink()
        with pytest.raises(EvidenceArchiveError, match="missing"):
            load_archived_run_nodes(conn, run_id, state_root=state_root)

        # Symlink archive -> fail closed.
        outside_file = tmp_path / "outside.jsonl.gz"
        outside_file.write_bytes(bytes1)
        art1.archive_path.symlink_to(outside_file)
        with pytest.raises(EvidenceArchiveError, match="symlink"):
            load_archived_run_nodes(conn, run_id, state_root=state_root)
        art1.archive_path.unlink()

        # Restore valid bytes, then tamper archived_node_count in DB receipt -> fail closed.
        art1.archive_path.write_bytes(bytes1)
        conn.execute(
            "UPDATE evidence_node_external_archives SET archived_node_count = 1 WHERE extraction_run_id = ?",
            (run_id,),
        )
        conn.commit()
        with pytest.raises(EvidenceArchiveError, match="count mismatch"):
            load_archived_run_nodes(conn, run_id, state_root=state_root)

        # Mismatched original_output_sha256 -> integrity audit blocker.
        conn.execute(
            "UPDATE evidence_node_external_archives SET archived_node_count = 2, "
            "original_output_sha256 = ? WHERE extraction_run_id = ?",
            ("f" * 64, run_id),
        )
        conn.commit()
        mismatch_audit = audit_connection(conn, AuditOptions(state_root=state_root))
        assert any(f.code == "EVIDENCE_ARCHIVE_RUN_MISMATCH" for f in mismatch_audit.findings)


def test_source_viewer_handles_external_archives_safely(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "viewer.db")
    state_root = tmp_path / "state"
    state_root.mkdir()

    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER) as conn:
        run_id, output_sha = _seed_run_with_nodes(conn)
        cell_rows: list[dict[str, object]] = [
            {
                "node_id": str(row[0]),
                "evidence_key": str(row[1]),
                "revision": int(row[2]),
                "extraction_run_id": str(row[3]),
                "parent_node_id": row[4],
                "supersedes_node_id": row[5],
                "node_kind": str(row[6]),
                "text": str(row[7]),
                "locator_json": row[8],
                "locator_sha256": row[9],
                "recorded_at": str(row[10]),
            }
            for row in conn.execute(
                "SELECT node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
                "supersedes_node_id, node_kind, text, locator_json, locator_sha256, recorded_at "
                "FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
                (run_id,),
            ).fetchall()
        ]
        artifact = write_run_nodes_archive(state_root, run_id, cell_rows)
        conn.execute("DROP TRIGGER trg_evidence_nodes_append_only_delete")
        conn.execute(
            "DELETE FROM evidence_nodes WHERE extraction_run_id = ? AND node_kind = 'table_cell'",
            (run_id,),
        )
        conn.execute(
            "CREATE TRIGGER trg_evidence_nodes_append_only_delete BEFORE DELETE ON evidence_nodes "
            "BEGIN SELECT RAISE(ABORT, 'evidence ledger is append-only'); END"
        )
        conn.execute(
            "INSERT INTO evidence_node_external_archives VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                artifact.storage_relpath,
                artifact.archive_sha256,
                artifact.archived_node_count,
                json.dumps(list(artifact.archived_node_kinds)),
                output_sha,
                STAMP.isoformat(),
            ),
        )
        conn.commit()

    # Without state_root: does not crash, exposes external archive metadata and live nodes.
    view_no_root = load_document_version(db_path, "doc-arch-1")
    assert view_no_root is not None
    assert [n.node_id for n in view_no_root.nodes] == ["node-01-doc", "node-02-row"]
    assert len(view_no_root.external_archives) == 1
    assert view_no_root.external_archives[0].archived_node_count == 2
    html_no_root = render_document_version_page(db_path, "doc-arch-1")
    assert html_no_root is not None
    assert "external archive" in html_no_root

    # With state_root: hydrates archived table_cell nodes safely.
    view_hydrated = load_document_version(db_path, "doc-arch-1", state_root=state_root)
    assert view_hydrated is not None
    assert [n.node_id for n in view_hydrated.nodes] == [
        "node-01-doc",
        "node-02-row",
        "node-03-cell-a",
        "node-04-cell-b",
    ]

    # With corrupted archive + explicit state_root: fails closed cleanly (returns None).
    artifact.archive_path.write_bytes(b"not-gzip")
    assert load_document_version(db_path, "doc-arch-1", state_root=state_root) is None
