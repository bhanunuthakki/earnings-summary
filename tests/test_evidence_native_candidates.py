"""Evidence-native extraction candidate and local replica contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from provenance.evidence_native_candidates import (
    has_evidence_native_after,
    resolve_local_storage_uri,
    select_evidence_native_candidates,
    select_evidence_native_candidates_by_id,
)


def _connection(tmp_path: Path) -> sqlite3.Connection:
    content_root = tmp_path / "blobs"
    content_root.mkdir()
    first = b"first"
    second = b"second"
    first_sha = hashlib.sha256(first).hexdigest()
    second_sha = hashlib.sha256(second).hexdigest()
    first_path = content_root / first_sha[:2] / first_sha
    second_path = content_root / second_sha[:2] / second_sha
    first_path.parent.mkdir()
    second_path.parent.mkdir()
    first_path.write_bytes(first)
    second_path.write_bytes(second)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE evidence_content_blobs (
          sha256 TEXT PRIMARY KEY, byte_size INTEGER NOT NULL,
          media_type TEXT NOT NULL, storage_uri TEXT NOT NULL
        );
        CREATE TABLE evidence_source_observations (
          observation_id TEXT PRIMARY KEY, source_url TEXT NOT NULL
        );
        CREATE TABLE evidence_document_versions (
          document_version_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
          blob_sha256 TEXT NOT NULL, legacy_document_id INTEGER, recorded_at TEXT NOT NULL
        );
        CREATE TABLE evidence_blob_location_observations (
          location_observation_id TEXT PRIMARY KEY, blob_sha256 TEXT NOT NULL,
          storage_uri TEXT NOT NULL, location_kind TEXT NOT NULL,
          availability_state TEXT NOT NULL, verified_sha256 TEXT,
          verified_byte_size INTEGER, verified_at TEXT NOT NULL
        );
        CREATE VIEW v_evidence_blob_locations_current AS
        SELECT * FROM evidence_blob_location_observations;
        """
    )
    for ordinal, (digest, body, path) in enumerate(
        ((first_sha, first, first_path), (second_sha, second, second_path)), start=1
    ):
        conn.execute(
            "INSERT INTO evidence_content_blobs VALUES (?, ?, 'text/plain', ?)",
            (digest, len(body), path.as_uri()),
        )
        conn.execute(
            "INSERT INTO evidence_source_observations VALUES (?, ?)",
            (f"obs-{ordinal}", f"https://issuer.test/report-{ordinal}.txt"),
        )
        conn.execute(
            "INSERT INTO evidence_document_versions VALUES (?, ?, ?, NULL, ?)",
            (f"version-{ordinal}", f"obs-{ordinal}", digest, f"2026-07-2{ordinal}"),
        )
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES (?, ?, ?, 'local', "
            "'present', ?, ?, '2026-07-25')",
            (f"location-{ordinal}", digest, path.as_uri(), digest, len(body)),
        )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES "
        "('legacy-version', 'obs-1', ?, 42, '2026-07-23')",
        (first_sha,),
    )
    conn.commit()
    return conn


def test_selects_legacy_free_versions_by_append_order_with_ledger_metadata(
    tmp_path: Path,
) -> None:
    conn = _connection(tmp_path)
    try:
        first = select_evidence_native_candidates(conn, after_rowid=0, batch_size=1)
        assert len(first) == 1
        assert first[0].document_version_id == "version-1"
        assert first[0].media_type == "text/plain"
        assert first[0].source_ref == "https://issuer.test/report-1.txt"
        assert has_evidence_native_after(conn, first[0].evidence_rowid)

        second = select_evidence_native_candidates(
            conn, after_rowid=first[0].evidence_rowid, batch_size=5
        )
        assert [candidate.document_version_id for candidate in second] == ["version-2"]
        assert not has_evidence_native_after(conn, second[0].evidence_rowid)
    finally:
        conn.close()


def test_local_uri_resolution_is_explicitly_root_bounded(tmp_path: Path) -> None:
    inside = tmp_path / "allowed" / "blob"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    outside = tmp_path / "outside" / "blob"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    assert (
        resolve_local_storage_uri(inside.as_uri(), allowed_roots=(inside.parent,))
        == inside.resolve()
    )
    assert (
        resolve_local_storage_uri(str(inside.resolve()), allowed_roots=(inside.parent,))
        == inside.resolve()
    )
    assert resolve_local_storage_uri(outside.as_uri(), allowed_roots=(inside.parent,)) is None
    assert (
        resolve_local_storage_uri("https://issuer.test/report", allowed_roots=(tmp_path,)) is None
    )


def test_explicit_selection_is_exact_and_returns_append_order(tmp_path: Path) -> None:
    conn = _connection(tmp_path)
    try:
        candidates = select_evidence_native_candidates_by_id(
            conn,
            document_version_ids=("version-2", "version-1"),
        )
        assert [candidate.document_version_id for candidate in candidates] == [
            "version-1",
            "version-2",
        ]
    finally:
        conn.close()


def test_pdf_filter_uses_source_url_when_server_media_type_is_generic(tmp_path: Path) -> None:
    conn = _connection(tmp_path)
    try:
        conn.execute(
            "UPDATE evidence_content_blobs SET media_type = 'application/octet-stream' "
            "WHERE sha256 = (SELECT blob_sha256 FROM evidence_document_versions "
            "WHERE document_version_id = 'version-2')"
        )
        conn.execute(
            "UPDATE evidence_source_observations "
            "SET source_url = 'https://issuer.test/report.pdf?download=1' "
            "WHERE observation_id = 'obs-2'"
        )
        conn.commit()
        candidates = select_evidence_native_candidates(
            conn, after_rowid=0, batch_size=10, pdf_only=True
        )
        assert [candidate.document_version_id for candidate in candidates] == ["version-2"]
    finally:
        conn.close()
