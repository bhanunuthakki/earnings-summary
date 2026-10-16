"""Evidence-native extraction candidate and local replica contracts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from provenance.evidence_native_candidates import (
    LocalEvidenceReadError,
    has_evidence_native_after,
    read_verified_local_evidence_bytes,
    resolve_local_storage_uri,
    select_evidence_native_candidates,
    select_evidence_native_candidates_by_id,
)
from provenance.immutable_artifact import ImmutableArtifactConflictError, read_stable_artifact


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


def test_exact_legacy_selection_requires_explicit_opt_in(tmp_path: Path) -> None:
    conn = _connection(tmp_path)
    try:
        with pytest.raises(ValueError, match="evidence-native document versions not found"):
            select_evidence_native_candidates_by_id(conn, document_version_ids=("legacy-version",))
        selected = select_evidence_native_candidates_by_id(
            conn, document_version_ids=("legacy-version",), include_legacy=True
        )
        assert [candidate.document_version_id for candidate in selected] == ["legacy-version"]
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_no_follow_resolution_preserves_lexical_path_for_reader_refusal(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "source.bin"
    target.write_bytes(b"exact")
    link = root / "link.bin"
    link.symlink_to(target)
    assert resolve_local_storage_uri(link.as_uri(), allowed_roots=(root,)) == target
    assert (
        resolve_local_storage_uri(link.as_uri(), allowed_roots=(root,), follow_links=False) == link
    )
    with pytest.raises(ImmutableArtifactConflictError, match="reparse point"):
        read_stable_artifact(link, max_bytes=5, allowed_root=root)


def test_verified_replica_reader_refuses_tampered_and_wrong_size_replicas(tmp_path: Path) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest
        primary.write_bytes(b"third")
        stale = tmp_path / "blobs" / "stale"
        stale.write_bytes(b"stale")
        wrong_size = tmp_path / "blobs" / "wrong-size"
        wrong_size.write_bytes(expected)
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('stale', ?, ?, 'local', 'present', ?, ?, '2026-07-26')",
            (digest, stale.as_uri(), digest, len(expected)),
        )
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('wrong-size', ?, ?, 'local', 'present', ?, ?, '2026-07-26')",
            (digest, wrong_size.as_uri(), digest, len(expected) + 1),
        )
        conn.commit()

        with pytest.raises(LocalEvidenceReadError, match="sha256_mismatch"):
            read_verified_local_evidence_bytes(
                conn,
                storage_uri=primary.as_uri(),
                expected_sha256=digest,
                expected_byte_size=len(expected),
                allowed_roots=(tmp_path / "blobs",),
                document_version_id="version-1",
            )
    finally:
        conn.close()


def test_verified_replica_reader_rejects_outside_root_and_unregistered_files(
    tmp_path: Path,
) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest
        primary.write_bytes(b"third")
        unregistered = tmp_path / "blobs" / "unregistered-exact-copy"
        unregistered.write_bytes(expected)
        outside = tmp_path / "outside" / "exact-copy"
        outside.parent.mkdir()
        outside.write_bytes(expected)
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('outside', ?, ?, 'local', 'present', ?, ?, '2026-07-26')",
            (digest, outside.as_uri(), digest, len(expected)),
        )
        conn.commit()

        with pytest.raises(LocalEvidenceReadError, match="sha256_mismatch"):
            read_verified_local_evidence_bytes(
                conn,
                storage_uri=primary.as_uri(),
                expected_sha256=digest,
                expected_byte_size=len(expected),
                allowed_roots=(tmp_path / "blobs",),
                document_version_id="version-1",
            )
    finally:
        conn.close()


def test_verified_replica_reader_wraps_filesystem_errors_as_typed_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest

        def fail_lstat(_path: Path) -> os.stat_result:
            raise OSError("simulated inaccessible filesystem")

        monkeypatch.setattr(Path, "lstat", fail_lstat)
        with pytest.raises(LocalEvidenceReadError, match="content_unreadable"):
            read_verified_local_evidence_bytes(
                conn,
                storage_uri=primary.as_uri(),
                expected_sha256=digest,
                expected_byte_size=len(expected),
                allowed_roots=(tmp_path / "blobs",),
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "identity", [{"document_version_id": "version-1"}, {"legacy_document_id": 42}]
)
@pytest.mark.parametrize("primary_state", ["missing", "tampered"])
def test_verified_reader_recovers_only_registered_exact_bytes(
    tmp_path: Path, identity: dict[str, str | int], primary_state: str
) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest
        if primary_state == "missing":
            primary.unlink()
        else:
            primary.write_bytes(b"third")
        replica = tmp_path / "blobs" / "exact-copy"
        replica.write_bytes(expected)
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('exact-copy', ?, ?, 'local', 'present', ?, ?, '2026-07-26')",
            (digest, replica.as_uri(), digest, len(expected)),
        )
        conn.commit()
        result = read_verified_local_evidence_bytes(
            conn,
            storage_uri=primary.as_uri(),
            expected_sha256=digest,
            expected_byte_size=len(expected),
            allowed_roots=(tmp_path / "blobs",),
            legacy_document_id=42 if "legacy_document_id" in identity else None,
            document_version_id="version-1" if "document_version_id" in identity else None,
        )
        assert result.raw_bytes == expected
        assert result.path == replica
        assert result.storage_uri == replica.as_uri()
        assert result.location_observation_id == "exact-copy"
        assert result.used_replica
        assert (
            conn.execute("SELECT COUNT(*) FROM evidence_blob_location_observations").fetchone()[0]
            == 3
        )
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["missing", "unverified", "remote", "different-document"])
def test_verified_reader_refuses_nonadmitted_locations(tmp_path: Path, state: str) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest
        primary.unlink()
        copy = tmp_path / "blobs" / "copy"
        copy.write_bytes(expected)
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('copy', ?, ?, ?, ?, ?, ?, '2026-07-26')",
            (
                digest,
                copy.as_uri(),
                "remote" if state == "remote" else "local",
                "missing" if state == "missing" else "present",
                None if state == "unverified" else digest,
                len(expected),
            ),
        )
        conn.commit()
        with pytest.raises(LocalEvidenceReadError, match="content_missing"):
            read_verified_local_evidence_bytes(
                conn,
                storage_uri=primary.as_uri(),
                expected_sha256=digest,
                expected_byte_size=len(expected),
                allowed_roots=(tmp_path / "blobs",),
                document_version_id="version-2" if state == "different-document" else "version-1",
            )
    finally:
        conn.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_verified_reader_refuses_registered_symlink_replica(tmp_path: Path) -> None:
    conn = _connection(tmp_path)
    try:
        expected = b"first"
        digest = hashlib.sha256(expected).hexdigest()
        primary = tmp_path / "blobs" / digest[:2] / digest
        primary.unlink()
        target = tmp_path / "blobs" / "target"
        target.write_bytes(expected)
        link = tmp_path / "blobs" / "link"
        link.symlink_to(target)
        conn.execute(
            "INSERT INTO evidence_blob_location_observations VALUES "
            "('link', ?, ?, 'local', 'present', ?, ?, '2026-07-26')",
            (digest, link.as_uri(), digest, len(expected)),
        )
        conn.commit()
        with pytest.raises(LocalEvidenceReadError, match="content_missing"):
            read_verified_local_evidence_bytes(
                conn,
                storage_uri=primary.as_uri(),
                expected_sha256=digest,
                expected_byte_size=len(expected),
                allowed_roots=(tmp_path / "blobs",),
                document_version_id="version-1",
            )
    finally:
        conn.close()


def test_verified_reader_handles_empty_bytes_without_a_zero_byte_limit(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    with sqlite3.connect(":memory:") as conn:
        result = read_verified_local_evidence_bytes(
            conn,
            storage_uri=path.as_uri(),
            expected_sha256=hashlib.sha256(b"").hexdigest(),
            expected_byte_size=0,
            allowed_roots=(tmp_path,),
        )
        assert result.raw_bytes == b""
        assert not result.used_replica


@pytest.mark.parametrize("content", [b"four", b"sixsix"])
def test_verified_reader_rejects_wrong_physical_size(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "content"
    path.write_bytes(content)
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(LocalEvidenceReadError, match="byte_size_mismatch"),
    ):
        read_verified_local_evidence_bytes(
            conn,
            storage_uri=path.as_uri(),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            expected_byte_size=5,
            allowed_roots=(tmp_path,),
        )
