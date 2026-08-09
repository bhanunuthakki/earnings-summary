"""Safely restore missing document-parent chains from a recovery SQLite DB.

This is a recovery tool for the case where a historical ``documents`` parent
was deleted while one or more derived documents still reference its id.  It
does not infer a replacement parent: a changed transcript is not evidence for
the text an older LLM summary actually read.

For every dangling ``documents.parent_document_id`` in ``--db``, the tool
restores the identically-id'd document from ``--recovery-db`` only when:

* the recovery document's file path resolves inside ``--repo-root``;
* that file still exists and its SHA-256 equals the recovery ledger value; and
* the target and recovery schemas/any pre-existing dependency rows match.

Transcript rows and their speaker-attributed segments are copied verbatim with
their original primary keys.  The default is a read-only dry run.  ``--apply``
uses one immediate transaction, enables foreign keys, and rolls back unless
``PRAGMA foreign_key_check`` is empty after the complete restore.

The CLI deliberately requires explicit database paths.  Do not point it at a
live DB; run it first against an isolated restored copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.secrets import load_project_env  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _int_list() -> list[int]:
    return []


def _repair_item_list() -> list[RepairItem]:
    return []


class RepairItem(BaseModel):
    """One missing parent root and the exact recovery rows it needs."""

    parent_document_id: int
    affected_child_document_ids: list[int] = Field(default_factory=_int_list)
    restored_document_ids: list[int] = Field(default_factory=_int_list)
    restored_transcript_ids: list[int] = Field(default_factory=_int_list)
    restored_segment_ids: list[int] = Field(default_factory=_int_list)
    replacement_parent_document_id: int | None = None
    status: Literal[
        "planned", "planned_relink", "restored", "relinked", "blocked", "already_present"
    ]
    reason: str | None = None


class RepairResult(BaseModel):
    """Schema-validated stdout contract for a repair/dry-run invocation."""

    mode: Literal["dry_run", "apply"]
    dangling_children: int
    missing_parent_roots: int
    restored_documents: int = 0
    restored_transcripts: int = 0
    restored_segments: int = 0
    blocked_parent_roots: int = 0
    foreign_key_violations_after: int | None = None
    items: list[RepairItem] = Field(default_factory=_repair_item_list)


class RepairBlockedError(RuntimeError):
    """Raised internally when exact restoration cannot be proven safe."""


_TABLES = ("documents", "transcripts", "transcript_segments")


def _log(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, default=str) + "\n")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _open_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    conn = connect_sqlite(resolved, role=SQLiteConnectionRole.READ_ONLY)
    conn.row_factory = sqlite3.Row
    return conn


def _open_target(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return _open_readonly(path)
    conn = connect_sqlite(
        str(path.resolve(strict=True)),
        role=SQLiteConnectionRole.WRITER,
        # Explicit recovery tooling must be able to repair a database at the
        # historical revision selected by the operator.
        schema_preflight=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    if not rows:
        raise RepairBlockedError(f"required table missing: {table}")
    return tuple(str(row[1]) for row in rows)


def _alembic_revision(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RepairBlockedError("alembic_version table is missing") from exc
    if row is None or not row[0]:
        raise RepairBlockedError("alembic_version is empty")
    return str(row[0])


def _table_schema(conn: sqlite3.Connection, table: str) -> tuple[object, ...]:
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if sql_row is None or not sql_row[0]:
        raise RepairBlockedError(f"required table missing: {table}")
    columns = tuple(
        tuple(row) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")
    )
    foreign_keys = tuple(
        tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})")
    )
    return (str(sql_row[0]), columns, foreign_keys)


def _require_matching_schemas(
    target: sqlite3.Connection,
    recovery: sqlite3.Connection,
    *,
    allowed_recovery_revision: str | None,
) -> dict[str, tuple[str, ...]]:
    target_revision = _alembic_revision(target)
    recovery_revision = _alembic_revision(recovery)
    revision_override = target_revision != recovery_revision
    if revision_override and recovery_revision != allowed_recovery_revision:
        raise RepairBlockedError(
            f"alembic revision mismatch: target={target_revision}, recovery={recovery_revision}"
        )
    columns: dict[str, tuple[str, ...]] = {}
    for table in _TABLES:
        target_columns = _table_columns(target, table)
        recovery_columns = _table_columns(recovery, table)
        if not revision_override:
            if _table_schema(target, table) != _table_schema(recovery, table):
                raise RepairBlockedError(f"schema DDL mismatch for {table}")
            if target_columns != recovery_columns:
                raise RepairBlockedError(
                    f"schema mismatch for {table}: "
                    f"target={target_columns}, recovery={recovery_columns}"
                )
            columns[table] = target_columns
            continue

        target_info = {
            str(row[1]): tuple(row[2:6])
            for row in target.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        }
        recovery_info = {
            str(row[1]): tuple(row[2:6])
            for row in recovery.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        }
        if not set(recovery_columns).issubset(target_columns):
            raise RepairBlockedError(
                f"recovery schema has columns absent from target for {table}: "
                f"target={target_columns}, recovery={recovery_columns}"
            )
        for column in recovery_columns:
            if target_info[column] != recovery_info[column]:
                raise RepairBlockedError(
                    f"column contract mismatch for {table}.{column}: "
                    f"target={target_info[column]}, recovery={recovery_info[column]}"
                )
        for column in set(target_columns).difference(recovery_columns):
            if int(target_info[column][1]) != 0:
                raise RepairBlockedError(f"target-only column {table}.{column} is not nullable")
        target_foreign_keys = target.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        ).fetchall()
        recovery_foreign_keys = recovery.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        ).fetchall()
        if [tuple(row) for row in target_foreign_keys] != [
            tuple(row) for row in recovery_foreign_keys
        ]:
            raise RepairBlockedError(f"foreign-key contract mismatch for {table}")
        columns[table] = recovery_columns
    return columns


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_recovery_file(repo_root: Path, stored_path: object) -> Path:
    if not isinstance(stored_path, str) or not stored_path.strip():
        raise RepairBlockedError("recovery document has no file_path")
    candidate = Path(stored_path)
    resolved = (candidate if candidate.is_absolute() else repo_root / candidate).resolve(
        strict=True
    )
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise RepairBlockedError(f"recovery file escapes repo root: {stored_path}") from exc
    return resolved


def _row_by_id(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {_quote_identifier(table)} WHERE id = ?",  # nosec B608
        (row_id,),
    ).fetchone()


def _documents_by_sha(conn: sqlite3.Connection, sha256: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE sha256 = ? ORDER BY id", (sha256,)
    ).fetchall()


def _rows_for_document(conn: sqlite3.Connection, table: str, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {_quote_identifier(table)} WHERE document_id = ? ORDER BY id",  # nosec B608
        (document_id,),
    ).fetchall()


def _transcripts_by_natural_key(
    conn: sqlite3.Connection, transcript: sqlite3.Row
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM transcripts
        WHERE ticker = ?
          AND fiscal_period_type IS ?
          AND period_end IS ?
        ORDER BY id
        """,
        (
            transcript["ticker"],
            transcript["fiscal_period_type"],
            transcript["period_end"],
        ),
    ).fetchall()


def _rows_for_transcript(conn: sqlite3.Connection, transcript_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id = ? ORDER BY id", (transcript_id,)
    ).fetchall()


def _same_row(left: sqlite3.Row, right: sqlite3.Row, columns: Iterable[str]) -> bool:
    return all(left[column] == right[column] for column in columns)


def _insert_exact(conn: sqlite3.Connection, table: str, row: sqlite3.Row) -> None:
    _insert_with_overrides(conn, table, row, {})


def _insert_with_overrides(
    conn: sqlite3.Connection,
    table: str,
    row: sqlite3.Row,
    overrides: Mapping[str, object],
) -> None:
    columns = _table_columns(conn, table)
    available_columns = set(row.keys())
    col_sql = ", ".join(_quote_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {_quote_identifier(table)} ({col_sql}) VALUES ({placeholders})",  # nosec B608
        tuple(
            overrides[column]
            if column in overrides
            else row[column]
            if column in available_columns
            else None
            for column in columns
        ),
    )


def _dangling_parents(target: sqlite3.Connection) -> dict[int, list[int]]:
    rows = target.execute(
        """
        SELECT child.id AS child_id, child.parent_document_id AS parent_id
        FROM documents AS child
        LEFT JOIN documents AS parent ON parent.id = child.parent_document_id
        WHERE child.parent_document_id IS NOT NULL AND parent.id IS NULL
        ORDER BY child.parent_document_id, child.id
        """
    ).fetchall()
    out: dict[int, list[int]] = {}
    for row in rows:
        parent_id = int(row["parent_id"])
        out.setdefault(parent_id, []).append(int(row["child_id"]))
    return out


def _plan_document_tree(
    target: sqlite3.Connection,
    recovery: sqlite3.Connection,
    repo_root: Path,
    document_id: int,
    columns: dict[str, tuple[str, ...]],
    planned_documents: list[sqlite3.Row],
    planned_transcripts: list[sqlite3.Row],
    planned_segments: list[sqlite3.Row],
    visiting: set[int],
    planned_ids: set[int],
    document_aliases: dict[int, int],
    alias_documents: list[sqlite3.Row],
) -> None:
    """Plan parent-first exact copies, including a rare recursive parent chain."""
    if document_id in planned_ids:
        return
    if document_id in visiting:
        raise RepairBlockedError(f"cycle in recovery documents parent chain at id={document_id}")
    recovery_doc = _row_by_id(recovery, "documents", document_id)
    if recovery_doc is None:
        raise RepairBlockedError(f"recovery DB lacks document id={document_id}")

    # Validate the recovery evidence even when an ancestor document happens to
    # be present in the target already.  A parent chain is only auditable when
    # every retained source still resolves under the approved repo root.
    source_file = _safe_recovery_file(repo_root, recovery_doc["file_path"])
    expected_sha = str(recovery_doc["sha256"] or "").lower()
    observed_sha = _sha256(source_file)
    if len(expected_sha) != 64 or observed_sha != expected_sha:
        raise RepairBlockedError(
            f"SHA-256 mismatch for document id={document_id}: "
            f"expected={expected_sha or '<missing>'} observed={observed_sha}"
        )

    target_doc = _row_by_id(target, "documents", document_id)
    if target_doc is not None:
        if not _same_row(target_doc, recovery_doc, columns["documents"]):
            raise RepairBlockedError(f"target document id={document_id} differs from recovery")
        planned_ids.add(document_id)
        return

    visiting.add(document_id)
    raw_parent = recovery_doc["parent_document_id"]
    if raw_parent is not None:
        _plan_document_tree(
            target,
            recovery,
            repo_root,
            int(raw_parent),
            columns,
            planned_documents,
            planned_transcripts,
            planned_segments,
            visiting,
            planned_ids,
            document_aliases,
            alias_documents,
        )

    canonical_matches = _documents_by_sha(target, expected_sha)
    canonical_document_id: int | None = None
    if canonical_matches:
        if len(canonical_matches) != 1:
            raise RepairBlockedError(
                f"target has {len(canonical_matches)} documents with SHA-256 {expected_sha}"
            )
        canonical = canonical_matches[0]
        comparable_columns = tuple(
            column for column in columns["documents"] if column not in {"id", "fetched_at"}
        )
        if not _same_row(canonical, recovery_doc, comparable_columns):
            raise RepairBlockedError(
                f"same-hash target document id={int(canonical['id'])} "
                f"differs from recovery id={document_id}"
            )
        canonical_document_id = int(canonical["id"])
        document_aliases[document_id] = canonical_document_id
        alias_documents.append(recovery_doc)
    else:
        planned_documents.append(recovery_doc)

    for recovery_transcript in _rows_for_document(recovery, "transcripts", document_id):
        transcript_id = int(recovery_transcript["id"])
        existing_transcript = _row_by_id(target, "transcripts", transcript_id)
        transcript_is_planned = False
        if existing_transcript is not None:
            transcript_matches = all(
                existing_transcript[column]
                == (
                    canonical_document_id
                    if column == "document_id" and canonical_document_id is not None
                    else recovery_transcript[column]
                )
                for column in columns["transcripts"]
            )
            if not transcript_matches:
                raise RepairBlockedError(
                    f"target transcript id={transcript_id} differs from recovery"
                )
        else:
            natural_matches = _transcripts_by_natural_key(target, recovery_transcript)
            if natural_matches:
                if len(natural_matches) != 1:
                    raise RepairBlockedError(
                        f"target has {len(natural_matches)} transcripts for "
                        f"{recovery_transcript['ticker']} "
                        f"{recovery_transcript['fiscal_period_type']} "
                        f"{recovery_transcript['period_end']}"
                    )
                canonical_transcript = natural_matches[0]
                comparable_transcript_columns = tuple(
                    column
                    for column in columns["transcripts"]
                    if column not in {"id", "document_id"}
                )
                if not _same_row(
                    canonical_transcript,
                    recovery_transcript,
                    comparable_transcript_columns,
                ):
                    raise RepairBlockedError(
                        f"natural-key transcript id={int(canonical_transcript['id'])} "
                        f"differs from recovery id={transcript_id}"
                    )
            else:
                planned_transcripts.append(recovery_transcript)
                transcript_is_planned = True
        if existing_transcript is None and not transcript_is_planned:
            continue
        for recovery_segment in _rows_for_transcript(recovery, transcript_id):
            segment_id = int(recovery_segment["id"])
            existing_segment = _row_by_id(target, "transcript_segments", segment_id)
            if existing_segment is not None:
                if not _same_row(
                    existing_segment, recovery_segment, columns["transcript_segments"]
                ):
                    raise RepairBlockedError(
                        f"target transcript segment id={segment_id} differs from recovery"
                    )
            else:
                planned_segments.append(recovery_segment)
    visiting.remove(document_id)
    planned_ids.add(document_id)


def repair(
    db_path: Path,
    recovery_db_path: Path,
    repo_root: Path,
    *,
    apply: bool = False,
    allow_live: bool = False,
    allowed_recovery_revision: str | None = None,
) -> RepairResult:
    """Plan or atomically apply an exact parent-document recovery.

    Any unsafe root blocks the whole repair.  This all-or-nothing behavior
    avoids claiming a successful provenance repair while leaving other known
    dangling references behind.
    """
    target_path = db_path.resolve(strict=True)
    recovery_path = recovery_db_path.resolve(strict=True)
    root = repo_root.resolve(strict=True)
    if target_path == recovery_path:
        raise ValueError("--db and --recovery-db must be distinct files")
    load_project_env(root)
    from db_paths import configured_db_path

    configured_live = configured_db_path(root)
    if apply and target_path == configured_live and not allow_live:
        raise RepairBlockedError(
            "refusing live DB repair without explicit --allow-live after writers are stopped"
        )

    target = _open_target(target_path, readonly=not apply)
    recovery = _open_readonly(recovery_path)
    transaction_open = False
    try:
        # Pin all recovery reads to one SQLite snapshot. The caller should still
        # supply an isolated restored backup rather than a live writer.
        recovery.execute("BEGIN")
        # Plan under the same writer lock that will perform the copy.  A
        # concurrent ingestion must not change a dangling child or reuse an id
        # after validation but before the insert transaction begins.
        if apply:
            target.execute("BEGIN IMMEDIATE")
            transaction_open = True
        columns = _require_matching_schemas(
            target,
            recovery,
            allowed_recovery_revision=allowed_recovery_revision,
        )
        dangling = _dangling_parents(target)
        items: list[RepairItem] = []
        planned_documents: list[sqlite3.Row] = []
        planned_transcripts: list[sqlite3.Row] = []
        planned_segments: list[sqlite3.Row] = []
        planned_ids: set[int] = set()
        document_aliases: dict[int, int] = {}
        alias_documents: list[sqlite3.Row] = []
        blocked = False

        for parent_id, child_ids in dangling.items():
            before_docs = len(planned_documents)
            before_transcripts = len(planned_transcripts)
            before_segments = len(planned_segments)
            before_aliases = len(document_aliases)
            try:
                _plan_document_tree(
                    target,
                    recovery,
                    root,
                    parent_id,
                    columns,
                    planned_documents,
                    planned_transcripts,
                    planned_segments,
                    set(),
                    planned_ids,
                    document_aliases,
                    alias_documents,
                )
            except RepairBlockedError as exc:
                blocked = True
                items.append(
                    RepairItem(
                        parent_document_id=parent_id,
                        affected_child_document_ids=child_ids,
                        status="blocked",
                        reason=str(exc),
                    )
                )
                continue
            replacement_parent_id = document_aliases.get(parent_id)
            item_status: Literal["planned", "planned_relink", "already_present"]
            if replacement_parent_id is not None and len(document_aliases) > before_aliases:
                item_status = "planned_relink"
            elif len(planned_documents) > before_docs:
                item_status = "planned"
            else:
                item_status = "already_present"
            items.append(
                RepairItem(
                    parent_document_id=parent_id,
                    affected_child_document_ids=child_ids,
                    replacement_parent_document_id=replacement_parent_id,
                    restored_document_ids=[
                        int(row["id"]) for row in planned_documents[before_docs:]
                    ],
                    restored_transcript_ids=[
                        int(row["id"]) for row in planned_transcripts[before_transcripts:]
                    ],
                    restored_segment_ids=[
                        int(row["id"]) for row in planned_segments[before_segments:]
                    ],
                    status=item_status,
                )
            )

        result = RepairResult(
            mode="apply" if apply else "dry_run",
            dangling_children=sum(len(child_ids) for child_ids in dangling.values()),
            missing_parent_roots=len(dangling),
            blocked_parent_roots=sum(item.status == "blocked" for item in items),
            items=items,
        )
        if blocked:
            if transaction_open:
                target.rollback()
                transaction_open = False
            _log("repair_blocked", blocked_parent_roots=result.blocked_parent_roots)
            return result
        if not apply:
            return result

        try:
            # Revalidate the evidence bytes at the transaction boundary.
            for row in [*planned_documents, *alias_documents]:
                source_file = _safe_recovery_file(root, row["file_path"])
                if _sha256(source_file) != str(row["sha256"] or "").lower():
                    raise RepairBlockedError(
                        f"source changed before apply for document id={int(row['id'])}"
                    )
            for row in planned_documents:
                raw_parent_id = row["parent_document_id"]
                overrides = (
                    {"parent_document_id": document_aliases[int(raw_parent_id)]}
                    if raw_parent_id is not None and int(raw_parent_id) in document_aliases
                    else {}
                )
                _insert_with_overrides(target, "documents", row, overrides)
            for old_parent_id, replacement_parent_id in document_aliases.items():
                cursor = target.execute(
                    """
                    UPDATE documents
                    SET parent_document_id = ?
                    WHERE parent_document_id = ?
                    """,
                    (replacement_parent_id, old_parent_id),
                )
                expected_children = len(dangling.get(old_parent_id, ()))
                if cursor.rowcount != expected_children:
                    raise RepairBlockedError(
                        f"relink row-count mismatch for parent id={old_parent_id}: "
                        f"expected={expected_children} updated={cursor.rowcount}"
                    )
            for row in planned_transcripts:
                source_document_id = int(row["document_id"])
                overrides = (
                    {"document_id": document_aliases[source_document_id]}
                    if source_document_id in document_aliases
                    else {}
                )
                _insert_with_overrides(target, "transcripts", row, overrides)
            for row in planned_segments:
                _insert_exact(target, "transcript_segments", row)
            violations = target.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RepairBlockedError(f"foreign_key_check found {len(violations)} violation(s)")
            target.commit()
            transaction_open = False
        except Exception:
            target.rollback()
            transaction_open = False
            raise

        for item in result.items:
            if item.status == "planned":
                item.status = "restored"
            elif item.status == "planned_relink":
                item.status = "relinked"
        result.restored_documents = len(planned_documents)
        result.restored_transcripts = len(planned_transcripts)
        result.restored_segments = len(planned_segments)
        result.foreign_key_violations_after = 0
        _log(
            "repair_applied",
            documents=result.restored_documents,
            relinked_documents=len(document_aliases),
            transcripts=result.restored_transcripts,
            segments=result.restored_segments,
        )
        return result
    except Exception:
        if transaction_open:
            target.rollback()
        raise
    finally:
        recovery.close()
        target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, required=True, help="Isolated target SQLite DB to repair"
    )
    parser.add_argument(
        "--recovery-db", type=Path, required=True, help="Read-only recovery SQLite DB"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="Repository root containing the recovery document paths",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply the validated recovery transaction"
    )
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Explicitly allow the canonical live DB after all writers are stopped",
    )
    parser.add_argument(
        "--allow-recovery-revision",
        help="Explicitly permit this recovery revision when relevant table DDL matches exactly",
    )
    args = parser.parse_args()
    try:
        result = repair(
            args.db,
            args.recovery_db,
            args.repo_root,
            apply=args.apply,
            allow_live=args.allow_live,
            allowed_recovery_revision=args.allow_recovery_revision,
        )
    except (OSError, sqlite3.Error, RepairBlockedError, ValueError) as exc:
        _log("repair_failed", error=str(exc))
        return 2
    print(result.model_dump_json(indent=2))
    return 2 if result.blocked_parent_roots else 0


if __name__ == "__main__":
    raise SystemExit(main())
