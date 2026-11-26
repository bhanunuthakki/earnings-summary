"""Fail-closed external archive reader and writer for compacted evidence_nodes.

Leaf ``table_cell`` rows from qualified ``fulltext-evidence-backfill`` runs may be
externalized into deterministic, content-addressed gzipped JSONL archives under
``<state_root>/data/derived_evidence/<sha2>/<run_sha>.jsonl.gz`` and tracked by
``evidence_node_external_archives``.

Readers transparently merge live SQLite ``evidence_nodes`` with archived rows
when a run has an archive receipt, and fail closed if the state root is missing,
the archive escapes ``state_root`` or traverses a symlink, the archive SHA-256
mismatches, or the decoded row count disagrees with the persisted receipt.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import zlib
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from db_paths import default_product_state_root

ARCHIVE_DIR_RELPATH = Path("data") / "derived_evidence"

_LOAD_LIVE_RUN_NODES_SQL = (
    "SELECT node_id, evidence_key, revision, extraction_run_id, parent_node_id, "
    "supersedes_node_id, node_kind, text, locator_json, locator_sha256, recorded_at "
    "FROM evidence_nodes WHERE extraction_run_id = ? ORDER BY node_id"
)
_LOAD_LIVE_RUN_NODES_LIMIT_SQL = _LOAD_LIVE_RUN_NODES_SQL + " LIMIT ?"
_LOAD_LIVE_RUN_NODES_MINIMAL_SQL = (
    "SELECT node_id, extraction_run_id, supersedes_node_id, node_kind, text, "
    "locator_json, locator_sha256 "
    "FROM evidence_nodes WHERE extraction_run_id = ? ORDER BY node_id"
)
_LOAD_LIVE_RUN_NODES_MINIMAL_LIMIT_SQL = _LOAD_LIVE_RUN_NODES_MINIMAL_SQL + " LIMIT ?"

EVIDENCE_NODE_FK_QUERIES: dict[tuple[str, str], str] = {
    ("canonical_fact_projection_entries", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM canonical_fact_projection_entries "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("fact_observations_v2", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM fact_observations_v2 "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("fact_selection_decisions", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM fact_selection_decisions "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("filing_xbrl_raw_fact_commitments", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM filing_xbrl_raw_fact_commitments "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("image_ocr_results", "node_id"): (
        "SELECT DISTINCT node_id FROM image_ocr_results WHERE node_id IS NOT NULL"
    ),
    ("latest_governed_narrative_entries", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM latest_governed_narrative_entries "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("legacy_document_evidence_binding_revisions", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM legacy_document_evidence_binding_revisions "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("legacy_fact_evidence_match_revisions", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM legacy_fact_evidence_match_revisions "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("ocr_page_results", "node_id"): (
        "SELECT DISTINCT node_id FROM ocr_page_results WHERE node_id IS NOT NULL"
    ),
    ("reported_observations", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM reported_observations "
        "WHERE evidence_node_id IS NOT NULL"
    ),
    ("search_chunks", "evidence_node_id"): (
        "SELECT DISTINCT evidence_node_id FROM search_chunks WHERE evidence_node_id IS NOT NULL"
    ),
    ("kpi_definition_revisions", "source_evidence_node_id"): (
        "SELECT DISTINCT source_evidence_node_id FROM kpi_definition_revisions "
        "WHERE source_evidence_node_id IS NOT NULL"
    ),
    ("kpi_definition_comparability_revisions", "source_evidence_node_id"): (
        "SELECT DISTINCT source_evidence_node_id FROM kpi_definition_comparability_revisions "
        "WHERE source_evidence_node_id IS NOT NULL"
    ),
}
_LOAD_LIVE_RUN_NODES_ROWID_SQL = (
    "SELECT rowid AS native_rowid, node_id, evidence_key, revision, extraction_run_id, "
    "parent_node_id, supersedes_node_id, node_kind, text, locator_json, locator_sha256, "
    "recorded_at FROM evidence_nodes WHERE extraction_run_id = ? ORDER BY native_rowid"
)
_LOAD_LIVE_RUN_NODES_ROWID_LIMIT_SQL = _LOAD_LIVE_RUN_NODES_ROWID_SQL + " LIMIT ?"

_EVIDENCE_NODE_COLUMNS: tuple[str, ...] = (
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
)

_WINDOWS_REPARSE_POINT_FLAG = 0x0400


class EvidenceArchiveError(RuntimeError):
    """Fail-closed error raised when an external evidence archive cannot be verified."""


@dataclass(frozen=True, slots=True)
class ArchivedRunArtifact:
    """Verified on-disk external archive produced for one extraction run."""

    extraction_run_id: str
    storage_relpath: str
    archive_sha256: str
    archived_node_count: int
    archived_node_kinds: tuple[str, ...]
    archive_path: Path


@dataclass(frozen=True, slots=True)
class ExternalArchiveReceipt:
    """Persisted receipt row from ``evidence_node_external_archives``."""

    extraction_run_id: str
    storage_relpath: str
    archive_sha256: str
    archived_node_count: int
    archived_node_kinds: tuple[str, ...]
    original_output_sha256: str
    archived_at: str


def _is_lower_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _has_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    file_attrs = getattr(info, "st_file_attributes", 0)
    return bool(file_attrs & _WINDOWS_REPARSE_POINT_FLAG)


def validate_archive_relpath(storage_relpath: str) -> tuple[str, ...]:
    """Validate and split a relative archive path into safe POSIX segments."""
    if not storage_relpath or "\x00" in storage_relpath:
        raise EvidenceArchiveError("archive storage_relpath must be a non-empty relative path")
    if storage_relpath.startswith(("/", "\\")) or "\\" in storage_relpath or ":" in storage_relpath:
        raise EvidenceArchiveError(
            f"archive storage_relpath must be a POSIX relative path: {storage_relpath!r}"
        )
    parts = tuple(storage_relpath.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise EvidenceArchiveError(
            f"archive storage_relpath contains unsafe segments: {storage_relpath!r}"
        )
    return parts


def resolve_archive_path(
    state_root: Path, storage_relpath: str, *, require_exists: bool = True
) -> Path:
    """Resolve ``storage_relpath`` under ``state_root`` without allowing traversal or symlinks."""
    parts = validate_archive_relpath(storage_relpath)
    root_resolved = state_root.resolve()
    current = root_resolved
    for part in parts:
        current = current / part
        if _has_reparse_or_symlink(current):
            raise EvidenceArchiveError(
                f"archive path traverses symlink or reparse point: {storage_relpath}"
            )
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        raise EvidenceArchiveError(f"archive path escapes state_root: {storage_relpath}")
    if require_exists and not resolved.is_file():
        raise EvidenceArchiveError(f"external evidence archive file is missing: {storage_relpath}")
    return resolved


def archive_relpath_for_run(extraction_run_id: str) -> str:
    """Compute the canonical state-root-relative path for an extraction run's archive."""
    if not extraction_run_id:
        raise EvidenceArchiveError("extraction_run_id must be non-empty")
    run_sha = hashlib.sha256(extraction_run_id.encode("utf-8")).hexdigest()
    return (ARCHIVE_DIR_RELPATH / run_sha[:2] / f"{run_sha}.jsonl.gz").as_posix()


def _canonical_row_sort_key(row: dict[str, object]) -> tuple[int, int, str]:
    native_rowid = row.get("native_rowid")
    node_id = str(row.get("node_id", ""))
    if isinstance(native_rowid, int) and not isinstance(native_rowid, bool):
        return (0, native_rowid, node_id)
    return (1, 0, node_id)


def _serialize_rows_canonical_jsonl(
    extraction_run_id: str, rows: Sequence[Mapping[str, object]]
) -> tuple[bytes, tuple[str, ...], list[str]]:
    if not rows:
        raise EvidenceArchiveError("cannot archive an empty node list")
    normalized_rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    kinds: set[str] = set()
    for index, raw_row in enumerate(rows):
        row = dict(raw_row)
        node_id = row.get("node_id")
        node_kind = row.get("node_kind")
        if not isinstance(node_id, str) or not node_id:
            raise EvidenceArchiveError(f"archived row at index {index} lacks a valid node_id")
        if node_id in seen_ids:
            raise EvidenceArchiveError(f"duplicate node_id in archive payload: {node_id}")
        seen_ids.add(node_id)
        if not isinstance(node_kind, str) or not node_kind:
            raise EvidenceArchiveError(f"archived row {node_id} lacks a valid node_kind")
        row_run_id = row.get("extraction_run_id")
        if row_run_id is not None and str(row_run_id) != extraction_run_id:
            raise EvidenceArchiveError(
                f"archived row {node_id} belongs to run {row_run_id!r}, expected {extraction_run_id!r}"
            )
        kinds.add(node_kind)
        normalized_rows.append(row)

    ordered = sorted(normalized_rows, key=_canonical_row_sort_key)
    canonical_lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for row in ordered
    ]
    payload_bytes = ("\n".join(canonical_lines) + "\n").encode("utf-8")
    return payload_bytes, tuple(sorted(kinds)), canonical_lines


def _compress_deterministic_gzip(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=6, mtime=0) as handle:
        handle.write(payload)
    return buffer.getvalue()


def _parse_archive_bytes(
    data: bytes,
    *,
    expected_sha256: str,
    expected_count: int,
    extraction_run_id: str,
) -> tuple[dict[str, object], ...]:
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise EvidenceArchiveError(
            f"external archive SHA-256 mismatch for run {extraction_run_id}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        decompressed = gzip.decompress(data)
    except (OSError, EOFError, zlib.error) as exc:
        raise EvidenceArchiveError(
            f"external archive decompression failed for run {extraction_run_id}: {exc}"
        ) from exc
    try:
        decoded_text = decompressed.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceArchiveError(
            f"external archive is not valid UTF-8 for run {extraction_run_id}: {exc}"
        ) from exc

    parsed_rows: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(decoded_text.splitlines(), start=1):
        if not line:
            continue
        try:
            item: object = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceArchiveError(
                f"external archive invalid JSON at line {line_number} for run {extraction_run_id}"
            ) from exc
        if not isinstance(item, dict):
            raise EvidenceArchiveError(
                f"external archive line {line_number} is not a JSON object for run {extraction_run_id}"
            )
        row = cast(dict[str, object], item)
        node_id = row.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise EvidenceArchiveError(
                f"external archive line {line_number} lacks node_id for run {extraction_run_id}"
            )
        if node_id in seen_ids:
            raise EvidenceArchiveError(
                f"external archive contains duplicate node_id {node_id} for run {extraction_run_id}"
            )
        seen_ids.add(node_id)
        row_run_id = row.get("extraction_run_id")
        if row_run_id is not None and str(row_run_id) != extraction_run_id:
            raise EvidenceArchiveError(
                f"external archive row {node_id} extraction_run_id mismatch: "
                f"expected {extraction_run_id}, got {row_run_id!r}"
            )
        parsed_rows.append(row)

    if len(parsed_rows) != expected_count:
        raise EvidenceArchiveError(
            f"external archive row count mismatch for run {extraction_run_id}: "
            f"expected {expected_count}, got {len(parsed_rows)}"
        )
    return tuple(parsed_rows)


def write_run_nodes_archive(
    state_root: Path,
    extraction_run_id: str,
    rows: Sequence[Mapping[str, object]],
) -> ArchivedRunArtifact:
    """Write a deterministic gzipped JSONL archive atomically and verify round-trip readback."""
    if not extraction_run_id:
        raise EvidenceArchiveError("extraction_run_id must be non-empty")
    storage_relpath = archive_relpath_for_run(extraction_run_id)
    dest_path = resolve_archive_path(state_root, storage_relpath, require_exists=False)
    payload_bytes, archived_kinds, expected_lines = _serialize_rows_canonical_jsonl(
        extraction_run_id, rows
    )
    compressed_bytes = _compress_deterministic_gzip(payload_bytes)
    archive_sha256 = hashlib.sha256(compressed_bytes).hexdigest()

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-verify parent directories did not introduce symlinks before writing.
    dest_path = resolve_archive_path(state_root, storage_relpath, require_exists=False)

    fd, tmp_name = tempfile.mkstemp(
        dir=dest_path.parent,
        prefix=".tmp-evidence-archive-",
        suffix=".jsonl.gz",
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(compressed_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # os.fdopen closes fd once entered; if fdopen itself failed, close safely.
            raise
        os.replace(tmp_path, dest_path)
    except BaseException:
        if tmp_path.exists():
            with suppress(OSError):
                tmp_path.unlink()
        raise

    # Fail-closed round-trip readback verification before returning.
    disk_bytes = dest_path.read_bytes()
    verified_rows = _parse_archive_bytes(
        disk_bytes,
        expected_sha256=archive_sha256,
        expected_count=len(expected_lines),
        extraction_run_id=extraction_run_id,
    )
    readback_lines = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        for row in verified_rows
    ]
    if readback_lines != expected_lines:
        raise EvidenceArchiveError(
            f"external archive round-trip payload mismatch for run {extraction_run_id}"
        )

    return ArchivedRunArtifact(
        extraction_run_id=extraction_run_id,
        storage_relpath=storage_relpath,
        archive_sha256=archive_sha256,
        archived_node_count=len(verified_rows),
        archived_node_kinds=archived_kinds,
        archive_path=dest_path,
    )


def has_external_archives_table(conn: sqlite3.Connection) -> bool:
    """Return True when ``evidence_node_external_archives`` exists in the connected schema."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence_node_external_archives'"
    ).fetchone()
    return row is not None


def _row_to_receipt(row: Sequence[object]) -> ExternalArchiveReceipt:
    extraction_run_id = str(row[0])
    storage_relpath = str(row[1])
    archive_sha256 = str(row[2])
    archived_node_count = int(str(row[3]))
    kinds_raw = str(row[4])
    original_output_sha256 = str(row[5])
    archived_at = str(row[6])
    try:
        parsed_kinds: object = json.loads(kinds_raw)
    except json.JSONDecodeError as exc:
        raise EvidenceArchiveError(
            f"invalid archived_node_kinds_json for run {extraction_run_id}"
        ) from exc
    if not isinstance(parsed_kinds, list) or not all(
        isinstance(item, str) and item for item in cast(list[object], parsed_kinds)
    ):
        raise EvidenceArchiveError(
            f"archived_node_kinds_json must be a non-empty string array for run {extraction_run_id}"
        )
    kinds = tuple(cast(list[str], parsed_kinds))
    if not _is_lower_sha256(archive_sha256):
        raise EvidenceArchiveError(f"invalid archive_sha256 for run {extraction_run_id}")
    if not _is_lower_sha256(original_output_sha256):
        raise EvidenceArchiveError(f"invalid original_output_sha256 for run {extraction_run_id}")
    if archived_node_count <= 0:
        raise EvidenceArchiveError(
            f"invalid archived_node_count {archived_node_count} for run {extraction_run_id}"
        )
    return ExternalArchiveReceipt(
        extraction_run_id=extraction_run_id,
        storage_relpath=storage_relpath,
        archive_sha256=archive_sha256,
        archived_node_count=archived_node_count,
        archived_node_kinds=kinds,
        original_output_sha256=original_output_sha256,
        archived_at=archived_at,
    )


def archive_receipt_for_run(
    conn: sqlite3.Connection, extraction_run_id: str
) -> ExternalArchiveReceipt | None:
    """Return the persisted archive receipt for ``extraction_run_id``, or ``None``."""
    if not has_external_archives_table(conn):
        return None
    row = conn.execute(
        "SELECT extraction_run_id, storage_relpath, archive_sha256, archived_node_count, "
        "archived_node_kinds_json, original_output_sha256, archived_at "
        "FROM evidence_node_external_archives WHERE extraction_run_id = ?",
        (extraction_run_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_receipt(tuple(row))


def archive_receipts_for_document_version(
    conn: sqlite3.Connection, document_version_id: str
) -> tuple[ExternalArchiveReceipt, ...]:
    """Return all persisted archive receipts for extraction runs of a document version."""
    if not has_external_archives_table(conn):
        return ()
    rows = conn.execute(
        "SELECT a.extraction_run_id, a.storage_relpath, a.archive_sha256, a.archived_node_count, "
        "a.archived_node_kinds_json, a.original_output_sha256, a.archived_at "
        "FROM evidence_node_external_archives AS a "
        "JOIN evidence_extraction_runs AS r ON r.extraction_run_id = a.extraction_run_id "
        "WHERE r.document_version_id = ? "
        "ORDER BY a.extraction_run_id",
        (document_version_id,),
    ).fetchall()
    return tuple(_row_to_receipt(tuple(row)) for row in rows)


def load_document_version_external_archives(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    state_root: Path | None = None,
) -> tuple[tuple[ExternalArchiveReceipt, ...], list[dict[str, object]]]:
    """Load archive receipts for a document version and optionally hydrate archived nodes."""
    receipts = archive_receipts_for_document_version(conn, document_version_id)
    archived_nodes: list[dict[str, object]] = []
    if state_root is not None and receipts:
        for receipt in receipts:
            archived_nodes.extend(verify_archive_receipt(state_root, receipt))
    return receipts, archived_nodes


def verify_archive_receipt(
    state_root: Path, receipt: ExternalArchiveReceipt
) -> tuple[dict[str, object], ...]:
    """Read and strictly verify one on-disk external archive against its DB receipt."""
    archive_path = resolve_archive_path(state_root, receipt.storage_relpath, require_exists=True)
    try:
        raw_bytes = archive_path.read_bytes()
    except OSError as exc:
        raise EvidenceArchiveError(
            f"unable to read external archive {receipt.storage_relpath}: {exc}"
        ) from exc
    rows = _parse_archive_bytes(
        raw_bytes,
        expected_sha256=receipt.archive_sha256,
        expected_count=receipt.archived_node_count,
        extraction_run_id=receipt.extraction_run_id,
    )
    actual_kinds = tuple(sorted({str(row.get("node_kind", "")) for row in rows}))
    expected_kinds = tuple(sorted(set(receipt.archived_node_kinds)))
    if actual_kinds != expected_kinds:
        raise EvidenceArchiveError(
            f"external archive node kinds mismatch for run {receipt.extraction_run_id}: "
            f"expected {expected_kinds}, got {actual_kinds}"
        )
    return rows


def load_archived_run_nodes(
    conn: sqlite3.Connection,
    extraction_run_id: str,
    *,
    state_root: Path | None,
) -> tuple[dict[str, object], ...]:
    """Load and verify archived nodes for ``extraction_run_id``, or ``()`` if unarchived."""
    receipt = archive_receipt_for_run(conn, extraction_run_id)
    if receipt is None:
        return ()
    if state_root is None:
        raise EvidenceArchiveError(
            f"state_root is required to read external archive for extraction run {extraction_run_id}"
        )
    return verify_archive_receipt(state_root, receipt)


def archive_receipt_has_substantive_kinds(receipt: ExternalArchiveReceipt) -> bool:
    """Return True when the archive receipt records at least one non-``document`` kind."""
    return any(kind != "document" for kind in receipt.archived_node_kinds)


def resolve_state_root_for_connection(
    conn: sqlite3.Connection,
    *,
    state_root: Path | None = None,
    repo_root: Path | None = None,
) -> Path | None:
    """Resolve an explicit or DB-derived product state root for external archives."""
    if state_root is not None:
        return state_root
    db_file = ""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            db_file = str(row[2] or "")
            break
    if db_file and db_file != ":memory:":
        db_path = Path(db_file)
        if db_path.parent.name.casefold() == "data":
            candidate = db_path.parent.parent.resolve()
            if (candidate / ARCHIVE_DIR_RELPATH).is_dir() or (
                db_path.name.casefold() == "portfolio.db"
            ):
                return candidate
        if (db_path.parent / ARCHIVE_DIR_RELPATH).is_dir():
            return db_path.parent.resolve()
        if repo_root is not None:
            return default_product_state_root(repo_root, db_path=db_path)
    if repo_root is not None:
        return default_product_state_root(repo_root)
    return None


def resolve_state_root_from_roots(
    state_root: Path | None,
    candidate_roots: Sequence[Path],
) -> Path | None:
    """Resolve an explicit state root, falling back to a single/matching approved root."""
    if state_root is not None:
        return state_root
    if len(candidate_roots) == 1:
        return candidate_roots[0]
    for candidate in candidate_roots:
        if (candidate / ARCHIVE_DIR_RELPATH).is_dir():
            return candidate
    return None


def _fetch_live_run_nodes(
    conn: sqlite3.Connection,
    extraction_run_id: str,
    *,
    include_rowid: bool,
    limit: int | None,
) -> list[dict[str, object]]:
    try:
        if include_rowid:
            if limit is not None:
                cursor = conn.execute(
                    _LOAD_LIVE_RUN_NODES_ROWID_LIMIT_SQL, (extraction_run_id, limit)
                )
            else:
                cursor = conn.execute(_LOAD_LIVE_RUN_NODES_ROWID_SQL, (extraction_run_id,))
        elif limit is not None:
            cursor = conn.execute(_LOAD_LIVE_RUN_NODES_LIMIT_SQL, (extraction_run_id, limit))
        else:
            cursor = conn.execute(_LOAD_LIVE_RUN_NODES_SQL, (extraction_run_id,))
    except sqlite3.OperationalError as exc:
        if include_rowid or "no such column" not in str(exc).lower():
            raise
        if limit is not None:
            cursor = conn.execute(
                _LOAD_LIVE_RUN_NODES_MINIMAL_LIMIT_SQL, (extraction_run_id, limit)
            )
        else:
            cursor = conn.execute(_LOAD_LIVE_RUN_NODES_MINIMAL_SQL, (extraction_run_id,))
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def load_run_nodes(
    conn: sqlite3.Connection,
    extraction_run_id: str,
    *,
    state_root: Path | None = None,
    include_external: bool = True,
    include_rowid: bool = False,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """Load live SQLite ``evidence_nodes`` merged with any external archive for a run.

    Results are returned in deterministic ``node_id`` order by default, or in
    original insertion order (``native_rowid``) when ``include_rowid=True`` and
    every merged row carries an integer ``native_rowid``. When a node exists in
    both SQLite (e.g. rehydrated for indexing) and the external archive, it is
    deduplicated by ``node_id`` preferring the live SQLite row while preserving
    the original pre-compaction ``native_rowid`` when ``include_rowid=True``.
    """
    receipt = archive_receipt_for_run(conn, extraction_run_id) if include_external else None
    if receipt is None:
        return _fetch_live_run_nodes(
            conn,
            extraction_run_id,
            include_rowid=include_rowid,
            limit=limit,
        )

    if state_root is None:
        raise EvidenceArchiveError(
            f"state_root is required to read external archive for extraction run {extraction_run_id}"
        )

    live_rows = _fetch_live_run_nodes(
        conn,
        extraction_run_id,
        include_rowid=include_rowid,
        limit=None,
    )
    archived_rows = verify_archive_receipt(state_root, receipt)

    merged_by_id: dict[str, dict[str, object]] = {}
    archived_rowids: dict[str, int] = {}
    for row in archived_rows:
        row_copy = dict(row)
        node_id = str(row_copy["node_id"])
        raw_rowid = row_copy.get("native_rowid")
        if isinstance(raw_rowid, int) and not isinstance(raw_rowid, bool):
            archived_rowids[node_id] = raw_rowid
        if not include_rowid:
            row_copy.pop("native_rowid", None)
        merged_by_id[node_id] = row_copy
    for row in live_rows:
        node_id = str(row["node_id"])
        merged_row = dict(row)
        if include_rowid and node_id in archived_rowids:
            merged_row["native_rowid"] = archived_rowids[node_id]
        merged_by_id[node_id] = merged_row

    merged = list(merged_by_id.values())
    if include_rowid and all(
        isinstance(item.get("native_rowid"), int) and not isinstance(item.get("native_rowid"), bool)
        for item in merged
    ):
        merged.sort(key=lambda item: (cast(int, item["native_rowid"]), str(item["node_id"])))
    else:
        merged.sort(key=lambda item: str(item["node_id"]))

    if limit is not None and len(merged) > limit:
        return merged[:limit]
    return merged


def merge_run_nodes_archive(
    conn: sqlite3.Connection,
    *,
    state_root: Path,
    extraction_run_id: str,
    new_rows: Sequence[Mapping[str, object]],
    original_output_sha256: str,
) -> tuple[ArchivedRunArtifact, bool]:
    """Write a new archive or merge ``new_rows`` into an existing verified run archive.

    Returns ``(artifact, is_update)``. Fails closed if an existing archive receipt
    disagrees on ``original_output_sha256`` or if any ``node_id`` duplicates an
    already-archived row.
    """
    existing_receipt = archive_receipt_for_run(conn, extraction_run_id)
    if existing_receipt is None:
        artifact = write_run_nodes_archive(state_root, extraction_run_id, new_rows)
        return artifact, False
    if existing_receipt.original_output_sha256 != original_output_sha256:
        raise EvidenceArchiveError(
            f"existing archive receipt original_output_sha256 mismatch for run {extraction_run_id}"
        )
    existing_rows = load_archived_run_nodes(conn, extraction_run_id, state_root=state_root)
    combined_rows: list[Mapping[str, object]] = [dict(row) for row in existing_rows]
    combined_rows.extend(new_rows)
    artifact = write_run_nodes_archive(state_root, extraction_run_id, combined_rows)
    return artifact, True


def order_compaction_run_groups(
    run_ids: set[str],
    cross_run_edges: set[tuple[str, str]],
) -> list[list[str]]:
    """Order runs so child/successor runs commit deletes before parent/predecessor runs."""
    in_degree: dict[str, int] = dict.fromkeys(run_ids, 0)
    dependents: dict[str, list[str]] = {}
    for src_run, dst_run in sorted(cross_run_edges):
        if src_run == dst_run or src_run not in in_degree or dst_run not in in_degree:
            continue
        in_degree[dst_run] += 1
        dependents.setdefault(src_run, []).append(dst_run)
    queue: deque[str] = deque(sorted(rid for rid, deg in in_degree.items() if deg == 0))
    groups: list[list[str]] = []
    emitted: set[str] = set()
    while queue:
        rid = queue.popleft()
        groups.append([rid])
        emitted.add(rid)
        newly_ready: list[str] = []
        for dst in dependents.get(rid, ()):
            in_degree[dst] -= 1
            if in_degree[dst] == 0:
                newly_ready.append(dst)
        for dst in sorted(newly_ready):
            queue.append(dst)
    remaining = sorted(run_ids - emitted)
    if remaining:
        groups.append(remaining)
    return groups


def peel_leaf_evidence_nodes(
    candidates: Mapping[str, tuple[str, int]],
    self_link_rows: Iterable[Sequence[object]],
) -> tuple[dict[str, list[str]], list[list[str]], int]:
    """O(N) bottom-up Kahn leaf peel over ``parent_node_id`` and ``supersedes_node_id``."""
    live_child_count: dict[str, int] = dict.fromkeys(candidates, 0)
    cand_targets: dict[str, list[str]] = {}
    for row in self_link_rows:
        src_id = str(row[0])
        parent_raw, supersedes_raw = row[1], row[2]
        if parent_raw is not None and supersedes_raw is not None:
            p_str, s_str = str(parent_raw), str(supersedes_raw)
            targets: tuple[str, ...] = (p_str,) if p_str == s_str else (p_str, s_str)
        elif parent_raw is not None:
            targets = (str(parent_raw),)
        elif supersedes_raw is not None:
            targets = (str(supersedes_raw),)
        else:
            continue
        src_is_candidate = src_id in candidates
        for target in targets:
            if target in candidates:
                live_child_count[target] += 1
                if src_is_candidate and src_id != target:
                    cand_targets.setdefault(src_id, []).append(target)

    queue: deque[str] = deque(sorted(nid for nid, count in live_child_count.items() if count == 0))
    peeled_set: set[str] = set()
    by_run: dict[str, list[str]] = {}
    estimated_bytes = 0
    while queue:
        nid = queue.popleft()
        peeled_set.add(nid)
        run_id, approx_bytes = candidates[nid]
        by_run.setdefault(run_id, []).append(nid)
        estimated_bytes += approx_bytes
        for target in cand_targets.get(nid, ()):
            live_child_count[target] -= 1
            if live_child_count[target] == 0:
                queue.append(target)
    if not by_run:
        return {}, [], 0

    cross_run_edges: set[tuple[str, str]] = {
        (candidates[src_id][0], candidates[target][0])
        for src_id in peeled_set
        for target in cand_targets.get(src_id, ())
        if target in peeled_set and candidates[src_id][0] != candidates[target][0]
    }
    return by_run, order_compaction_run_groups(set(by_run), cross_run_edges), estimated_bytes


def load_substantive_run_nodes_for_review(
    conn: sqlite3.Connection,
    extraction_run_id: str,
    *,
    document_version_id: str,
    substantive_kinds: frozenset[str],
    max_text_chars_per_node: int,
    max_nodes: int,
    state_root: Path | None = None,
) -> list[dict[str, object]]:
    """Load bounded non-superseded substantive nodes for KPI review from live or archived storage."""
    if archive_receipt_for_run(conn, extraction_run_id) is not None:
        run_nodes = load_run_nodes(conn, extraction_run_id, state_root=state_root)
        superseded_ids = {
            str(node["supersedes_node_id"])
            for node in run_nodes
            if node.get("supersedes_node_id") is not None
        }
        return [
            {
                "node_id": str(node["node_id"]),
                "node_kind": str(node["node_kind"]),
                "locator_json": node.get("locator_json"),
                "locator_sha256": node.get("locator_sha256"),
                "bounded_text": str(node.get("text") or "")[: max_text_chars_per_node + 1],
                "text_length": len(str(node.get("text") or "")),
                "extraction_run_id": str(node["extraction_run_id"]),
                "document_version_id": document_version_id,
            }
            for node in run_nodes
            if str(node["node_kind"]) in substantive_kinds
            and str(node["node_id"]) not in superseded_ids
        ][: max_nodes + 1]

    sql_rows = conn.execute(
        "SELECT node.node_id,node.node_kind,node.locator_json,node.locator_sha256,"
        "substr(node.text,1,?) AS bounded_text,length(node.text) AS text_length,"
        "node.extraction_run_id,run.document_version_id FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
        "WHERE node.extraction_run_id=? "
        "AND node.node_kind IN (SELECT value FROM json_each(?)) "
        "AND NOT EXISTS (SELECT 1 FROM evidence_nodes newer "
        "WHERE newer.supersedes_node_id=node.node_id "
        "AND newer.extraction_run_id=node.extraction_run_id) ORDER BY node.node_id LIMIT ?",
        (
            max_text_chars_per_node + 1,
            extraction_run_id,
            json.dumps(sorted(substantive_kinds)),
            max_nodes + 1,
        ),
    ).fetchall()
    return [
        {
            "node_id": row[0],
            "node_kind": row[1],
            "locator_json": row[2],
            "locator_sha256": row[3],
            "bounded_text": row[4],
            "text_length": row[5],
            "extraction_run_id": row[6],
            "document_version_id": row[7],
        }
        for row in sql_rows
    ]


__all__ = [
    "ARCHIVE_DIR_RELPATH",
    "EVIDENCE_NODE_FK_QUERIES",
    "ArchivedRunArtifact",
    "EvidenceArchiveError",
    "ExternalArchiveReceipt",
    "archive_receipt_for_run",
    "archive_receipt_has_substantive_kinds",
    "archive_receipts_for_document_version",
    "archive_relpath_for_run",
    "has_external_archives_table",
    "load_archived_run_nodes",
    "load_document_version_external_archives",
    "load_run_nodes",
    "load_substantive_run_nodes_for_review",
    "merge_run_nodes_archive",
    "order_compaction_run_groups",
    "peel_leaf_evidence_nodes",
    "resolve_archive_path",
    "resolve_state_root_for_connection",
    "resolve_state_root_from_roots",
    "validate_archive_relpath",
    "verify_archive_receipt",
    "write_run_nodes_archive",
]
