# pyright: reportPrivateUsage=false
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline.fmp_doc_index import index_fmp_files_for_ticker
from provenance.evidence_backfill import _resolve_legacy_path


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

    assert _resolve_legacy_path(runtime_root.resolve(), "data/source.json") == target.resolve()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    assert _resolve_legacy_path(runtime_root.resolve(), str(outside)) is None
