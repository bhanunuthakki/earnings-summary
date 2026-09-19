from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

import pipeline.fmp_doc_index as fmp_index
from pipeline.fmp_doc_index import index_fmp_files_for_ticker
from provenance import evidence_backfill
from provenance.evidence_backfill import ensure_legacy_document_evidence


def test_existing_fmp_document_is_anchored_before_refresh(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    db_path = migrated_db(repo_root / "data" / "portfolio.db")
    fmp_path = repo_root / "data" / "historical" / "fmp" / "ZZ_income_statement_quarterly.json"
    fmp_path.parent.mkdir(parents=True)
    payload = [{"date": "2026-06-30", "revenue": 123.0}]
    raw = json.dumps(payload).encode()
    fmp_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO documents "
            "(ticker, source_type, doc_type, period_end, file_path, sha256, fetched_at, "
            "fetch_status, raw_bytes_size) VALUES "
            "('ZZ', 'fmp', 'fmp_income_statement', '2026-06-30', ?, ?, "
            "'2026-07-01T00:00:00+00:00', 'ok', ?)",
            (
                "data/historical/fmp/ZZ_income_statement_quarterly.json",
                digest,
                len(raw),
            ),
        )
        conn.commit()

        # Older deployments already anchored this mutable cache location. A
        # same-byte relocation must preserve the original immutable identities.
        document_id = int(conn.execute("SELECT id FROM documents").fetchone()[0])
        conn.execute(
            "UPDATE documents SET source_url=? WHERE id=?",
            ("https://financialmodelingprep.com/stable/income-statement", document_id),
        )
        ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=document_id)
        conn.commit()
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "evidence_nodes",
                "evidence_extraction_runs",
                "evidence_source_observations",
                "legacy_document_evidence_binding_revisions",
            )
        }

        assert index_fmp_files_for_ticker(conn, "ZZ", repo_root) == 0
        row = conn.execute(
            "SELECT document_version_id FROM evidence_document_versions "
            "WHERE legacy_document_id = (SELECT id FROM documents WHERE sha256 = ?)",
            (digest,),
        ).fetchone()
        assert row is not None

        assert index_fmp_files_for_ticker(conn, "ZZ", repo_root) == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM evidence_document_versions "
                "WHERE legacy_document_id = (SELECT id FROM documents WHERE sha256 = ?)",
                (digest,),
            ).fetchone()[0]
            == 1
        )
        for table, expected in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == expected
        retained = repo_root / conn.execute("SELECT file_path FROM documents").fetchone()[0]
        assert retained != fmp_path
        assert retained.read_bytes() == raw
        assert conn.execute("SELECT source_url FROM documents").fetchone()[0] == (
            "https://financialmodelingprep.com/stable/income-statement"
        )
        fmp_path.write_bytes(b"changed cache")
        ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=document_id)
        assert (
            conn.execute(
                "SELECT verified_sha256 FROM v_evidence_blob_locations_current "
                "WHERE storage_uri=? AND availability_state='present'",
                (retained.resolve().as_uri(),),
            ).fetchone()[0]
            == digest
        )


def test_legacy_path_allows_explicit_linked_data_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    live_data = tmp_path / "live-data"
    runtime_root.mkdir()
    live_data.mkdir()
    target = live_data / "source.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (runtime_root / "data").symlink_to(live_data, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    assert (
        cast(
            Callable[[Path, str], Path | None], getattr(evidence_backfill, "_resolve_legacy_path")
        )(runtime_root.resolve(), "data/source.json")
        == target.resolve()
    )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    assert (
        cast(
            Callable[[Path, str], Path | None], getattr(evidence_backfill, "_resolve_legacy_path")
        )(runtime_root.resolve(), str(outside))
        is None
    )


def test_refresh_preserves_each_registered_document_and_ledger_blob(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    root = tmp_path / "repo"
    db = migrated_db(tmp_path / "runtime.db")
    cache = root / "data/historical/fmp/ZZ_profile.json"
    cache.parent.mkdir(parents=True)
    versions = (b'[{"symbol":"ZZ","price":10}]', b'[{"symbol":"ZZ","price":11}]\n')
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        for raw in versions:
            cache.write_bytes(raw)
            assert index_fmp_files_for_ticker(conn, "ZZ", root) == 1
        assert index_fmp_files_for_ticker(conn, "ZZ", root) == 0
        rows = conn.execute(
            "SELECT file_path, sha256, source_url FROM documents ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        for row, expected in zip(rows, versions, strict=True):
            retained = root / row["file_path"]
            assert retained != cache
            assert retained.read_bytes() == expected
            assert row["sha256"] == hashlib.sha256(expected).hexdigest()
            assert row["source_url"] is None
            uri = conn.execute(
                "SELECT storage_uri FROM evidence_content_blobs WHERE sha256=?", (row["sha256"],)
            ).fetchone()[0]
            assert str(uri) == retained.resolve().as_uri()
        assert cache.read_bytes() == versions[-1]


def test_failed_evidence_capture_rolls_back_document_registration(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    db = migrated_db(tmp_path / "runtime.db")
    cache = root / "data/historical/fmp/ZZ_profile.json"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'[{"symbol":"ZZ","price":10}]')

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ValueError("capture failed")

    monkeypatch.setattr(fmp_index, "ensure_legacy_document_evidence", fail)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(ValueError, match="capture failed"):
            index_fmp_files_for_ticker(conn, "ZZ", root)
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert not conn.in_transaction


def test_fmp_snapshot_publication_supports_configured_data_directory_link(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    root = tmp_path / "repo"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "data"), str(state)],
            check=True,
            capture_output=True,
        )
    else:
        (root / "data").symlink_to(state, target_is_directory=True)
    cache = root / "data/historical/fmp/ZZ_profile.json"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'[{"symbol":"ZZ"}]')
    with sqlite3.connect(migrated_db(tmp_path / "runtime.db")) as conn:
        conn.row_factory = sqlite3.Row
        assert index_fmp_files_for_ticker(conn, "ZZ", root) == 1
        retained = root / conn.execute("SELECT file_path FROM documents").fetchone()[0]
        assert retained.resolve().is_relative_to(state)
        assert retained.read_bytes() == cache.read_bytes()
