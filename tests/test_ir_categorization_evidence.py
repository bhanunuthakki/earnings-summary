"""Canonical IR placement retains source identity and atomic evidence admission."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from execution import categorize_ir_uploads as cli
from ir_uploads import CategorizationResult
from models.documents import DocType
from models.ir_uploads import Confidence
from provenance.evidence_backfill import ensure_legacy_document_evidence


def _classify(_path: Path, **_kwargs: object) -> CategorizationResult:
    return CategorizationResult(
        ticker="ZZ",
        doc_type=DocType.IR_SUPPLEMENT,
        period_end=date(2026, 6, 30),
        period_label="Q2 2026",
        confidence=Confidence.HIGH,
        ticker_evidence=["fixture"],
        doc_type_evidence=["fixture"],
        period_evidence=["fixture"],
    )


def _process(path: Path, conn: sqlite3.Connection, root: Path) -> dict[str, object]:
    return cli.process_ir_document(
        path,
        conn,
        False,
        None,
        root / "ir_documents",
        root,
        url_overrides={path.name: "https://issuer.example/q2-supplement.pdf"},
    )


@pytest.mark.parametrize("anchored", [False, True])
def test_registered_ir_relocation_preserves_identity_and_reindexes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    anchored: bool,
) -> None:
    root = tmp_path / "repo"
    source = root / "ir_documents" / "incoming.pdf"
    source.parent.mkdir(parents=True)
    raw = b"%PDF-1.4 fixture"
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(cli, "classify_ir_file", _classify)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size,source_url) "
            "VALUES ('ZZ','ir_doc','ir_supplement','2026-06-30',?,?,"
            "'2026-07-01','ok',?,'https://issuer.example/original.pdf')",
            ("ir_documents/incoming.pdf", digest, len(raw)),
        )
        document_id = int(conn.execute("SELECT id FROM documents").fetchone()[0])
        if anchored:
            ensure_legacy_document_evidence(conn, repo_root=root, document_id=document_id)
        conn.commit()
        before_nodes = [tuple(row) for row in conn.execute("SELECT * FROM evidence_nodes")]
        result = _process(source, conn, root)
        conn.commit()
        row = conn.execute("SELECT id,file_path,sha256,source_url FROM documents").fetchone()
        retained = root / row["file_path"]
        assert retained != source
        assert retained.read_bytes() == raw
        assert row["id"] == document_id
        assert row["sha256"] == digest
        assert row["source_url"] == "https://issuer.example/original.pdf"
        assert not result["documents_inserted"]
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1
        if anchored:
            assert source.read_bytes() == raw
            assert [
                tuple(row) for row in conn.execute("SELECT * FROM evidence_nodes")
            ] == before_nodes
        replay = _process(retained, conn, root)
        conn.commit()
        assert replay["status"] == "reindexed"
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_evidence_blob_locations_current WHERE storage_uri=?",
                (retained.resolve().as_uri(),),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("existing", [False, True])
def test_ir_capture_failure_preserves_original_and_rolls_back_row(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    root = tmp_path / "repo"
    source = root / "ir_documents/incoming.pdf"
    source.parent.mkdir(parents=True)
    raw = b"%PDF-1.4 fixture"
    source.write_bytes(raw)
    monkeypatch.setattr(cli, "classify_ir_file", _classify)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ValueError("evidence capture failed")

    monkeypatch.setattr(cli, "ensure_legacy_document_evidence", fail, raising=False)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        if existing:
            conn.execute(
                "INSERT INTO documents (ticker,source_type,doc_type,period_end,file_path,sha256,"
                "fetched_at,fetch_status,raw_bytes_size) VALUES "
                "('ZZ','ir_doc','ir_supplement','2026-06-30','ir_documents/incoming.pdf',?,"
                "'2026-07-01','ok',?)",
                (hashlib.sha256(raw).hexdigest(), len(raw)),
            )
            conn.commit()
        before = [tuple(row) for row in conn.execute("SELECT * FROM documents")]
        with pytest.raises(ValueError, match="evidence capture failed"):
            _process(source, conn, root)
        assert source.read_bytes() == raw
        assert [tuple(row) for row in conn.execute("SELECT * FROM documents")] == before
        assert not conn.in_transaction


def test_new_ir_document_gets_foundational_evidence(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    source = root / "ir_documents/incoming.pdf"
    source.parent.mkdir(parents=True)
    raw = b"%PDF-1.4 fixture"
    source.write_bytes(raw)
    monkeypatch.setattr(cli, "classify_ir_file", _classify)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        result = _process(source, conn, root)
        conn.commit()
        assert result["documents_inserted"]
        assert not source.exists()
        node = conn.execute("SELECT locator_json FROM evidence_nodes").fetchone()
        assert node is not None
        assert json.loads(node[0])["legacy_table"] == "documents"
        assert conn.execute("SELECT COUNT(*) FROM evidence_document_versions").fetchone()[0] == 1


def test_ir_installed_byte_change_is_rejected_without_deleting_input(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    source = root / "ir_documents/incoming.pdf"
    source.parent.mkdir(parents=True)
    raw = b"%PDF-1.4 fixture"
    source.write_bytes(raw)
    monkeypatch.setattr(cli, "classify_ir_file", _classify)
    publisher = cli.publish_bytes_no_clobber

    def corrupt(path: Path, payload: bytes) -> bool:
        published = publisher(path, payload)
        path.write_bytes(b"changed after install")
        return published

    monkeypatch.setattr(cli, "publish_bytes_no_clobber", corrupt)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="changed before registration"):
            _process(source, conn, root)
        assert source.read_bytes() == raw
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_ir_capture_supports_configured_junction_to_retained_data(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    state = root / "data" / "ir_sources"
    state.mkdir(parents=True)
    link = root / "ir_documents"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(state)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(state, target_is_directory=True)
    source = link / "incoming.pdf"
    raw = b"%PDF-1.4 fixture"
    source.write_bytes(raw)
    monkeypatch.setattr(cli, "classify_ir_file", _classify)
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        assert _process(source, conn, root)["documents_inserted"]
        retained = root / conn.execute("SELECT file_path FROM documents").fetchone()[0]
        assert retained.resolve().is_relative_to(state)
        assert retained.read_bytes() == raw
