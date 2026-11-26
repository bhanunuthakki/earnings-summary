"""Guarded single-writer compaction logic for evidence_nodes externalization and coverage pruning.

Called by the thin CLI entrypoint ``execution/compact_evidence_and_coverage.py``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from provenance.evidence_node_store import (
    has_external_archives_table,
    write_run_nodes_archive,
)
from run_lock import hold_run_lock
from runtime.job_runtime import inherited_lock_is_valid
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

DEFAULT_BATCH_SIZE = 20_000
DEFAULT_LOCK_TIMEOUT_S = 60.0
_MAX_SQLITE_BIND_VARS = 500
_FULLTEXT_EXTRACTOR_NAME = "fulltext-evidence-backfill"

_EVIDENCE_NODE_TRIGGER = "trg_evidence_nodes_append_only_delete"
_LEGACY_MATCH_TRIGGER = "trg_legacy_fact_evidence_match_revisions_append_only_delete"
_COVERAGE_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("source_coverage_assessments", "trg_source_coverage_assessments_append_only_delete"),
    (
        "expected_document_obligation_bindings",
        "trg_expected_document_obligation_bindings_append_only_delete",
    ),
    (
        "expected_document_lifecycle_revisions",
        "trg_expected_document_lifecycle_revisions_append_only_delete",
    ),
    ("expected_documents", "trg_expected_documents_append_only_delete"),
)

# Explicit external FK tables that may reference evidence_nodes(node_id).
# Additional FK tables discovered via PRAGMA foreign_key_list are unioned dynamically.
_NAMED_EVIDENCE_NODE_FK_REFS: tuple[tuple[str, str], ...] = (
    ("canonical_fact_projection_entries", "evidence_node_id"),
    ("fact_observations_v2", "evidence_node_id"),
    ("fact_selection_decisions", "evidence_node_id"),
    ("filing_xbrl_raw_fact_commitments", "evidence_node_id"),
    ("image_ocr_results", "node_id"),
    ("latest_governed_narrative_entries", "evidence_node_id"),
    ("legacy_document_evidence_binding_revisions", "evidence_node_id"),
    ("legacy_fact_evidence_match_revisions", "evidence_node_id"),
    ("ocr_page_results", "node_id"),
    ("reported_observations", "evidence_node_id"),
    ("search_chunks", "evidence_node_id"),
    ("kpi_definition_revisions", "source_evidence_node_id"),
    ("kpi_definition_comparability_revisions", "source_evidence_node_id"),
)

EventLogger = Callable[..., None]


def _noop_log_event(event: str, **fields: object) -> None:
    del event, fields


@dataclass(frozen=True, slots=True)
class CompactionOptions:
    db_path: Path
    state_root: Path
    project_root: Path
    apply: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE
    prune_index_member_coverage: bool = False
    prune_inactive_filing_sections: bool = False
    prune_unreferenced_legacy_matches: bool = False
    vacuum: bool = False
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
    }


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    }


def _discover_external_fk_refs(
    conn: sqlite3.Connection,
    target_table: str,
    *,
    extra_named: Sequence[tuple[str, str]] = (),
    exclude_tables: set[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    tables = _existing_tables(conn)
    excluded = exclude_tables or set()
    refs: set[tuple[str, str]] = set()
    for table_name, col_name in extra_named:
        if (
            table_name in tables
            and table_name not in excluded
            and col_name in _existing_columns(conn, table_name)
        ):
            refs.add((table_name, col_name))
    for table_name in sorted(tables):
        if table_name == target_table or table_name in excluded:
            continue
        for fk in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(table_name)})").fetchall():
            if str(fk[2]) == target_table:
                refs.add((table_name, str(fk[3])))
    return tuple(sorted(refs))


def _chunked(items: Sequence[str] | Sequence[int], size: int) -> Iterator[Sequence[object]]:
    step = max(1, min(size, _MAX_SQLITE_BIND_VARS))
    for offset in range(0, len(items), step):
        yield items[offset : offset + step]


def _capture_trigger_sql(conn: sqlite3.Connection, trigger_name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = ?",
        (trigger_name,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _verify_triggers_present(conn: sqlite3.Connection, expected: Sequence[tuple[str, str]]) -> None:
    for trigger_name, expected_sql in expected:
        actual = _capture_trigger_sql(conn, trigger_name)
        if actual != expected_sql:
            raise RuntimeError(
                f"append-only trigger {trigger_name} was not restored verbatim after maintenance"
            )


@contextmanager
def _maintenance_transaction(
    conn: sqlite3.Connection, trigger_names: Sequence[str]
) -> Generator[None]:
    """Drop named delete triggers, execute maintenance inside BEGIN IMMEDIATE, and restore verbatim."""
    if conn.in_transaction:
        raise RuntimeError("maintenance transaction requires an idle SQLite connection")
    captured: list[tuple[str, str]] = []
    for name in trigger_names:
        sql = _capture_trigger_sql(conn, name)
        if sql is not None:
            captured.append((name, sql))

    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, _ in captured:
                conn.execute(f"DROP TRIGGER IF EXISTS {_quote_ident(name)}")
            yield
            for _, sql in captured:
                conn.execute(sql)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prev_isolation
        if captured:
            _verify_triggers_present(conn, captured)


def _check_table_foreign_keys(conn: sqlite3.Connection, table: str) -> None:
    violation = conn.execute(f"PRAGMA foreign_key_check({_quote_ident(table)})").fetchone()
    if violation is not None:
        raise RuntimeError(f"foreign-key verification failed on {table}: {tuple(violation)!r}")


def _select_eligible_leaf_table_cells(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[str]], int, int]:
    """Return ({extraction_run_id: [node_id, ...]}, estimated_payload_bytes, already_archived_runs)."""
    tables = _existing_tables(conn)
    if not {"evidence_nodes", "evidence_extraction_runs"} <= tables:
        return {}, 0, 0

    fk_refs = _discover_external_fk_refs(
        conn, "evidence_nodes", extra_named=_NAMED_EVIDENCE_NODE_FK_REFS
    )
    not_exists_clauses = [
        "NOT EXISTS (SELECT 1 FROM evidence_nodes AS child WHERE child.parent_node_id = n.node_id)",
        "NOT EXISTS (SELECT 1 FROM evidence_nodes AS newer WHERE newer.supersedes_node_id = n.node_id)",
    ]
    for table_name, col_name in fk_refs:
        qt = _quote_ident(table_name)
        qc = _quote_ident(col_name)
        not_exists_clauses.append(
            f"NOT EXISTS (SELECT 1 FROM {qt} AS ref WHERE ref.{qc} = n.node_id)"
        )

    already_archived_runs = 0
    if has_external_archives_table(conn):
        already_archived_runs = int(
            conn.execute("SELECT COUNT(*) FROM evidence_node_external_archives").fetchone()[0]
        )
        not_exists_clauses.append(
            "NOT EXISTS (SELECT 1 FROM evidence_node_external_archives AS arch "
            "WHERE arch.extraction_run_id = n.extraction_run_id)"
        )

    predicate = " AND ".join(not_exists_clauses)
    sql = (
        "SELECT n.extraction_run_id, n.node_id, "
        "(length(CAST(n.text AS BLOB)) + COALESCE(length(CAST(n.locator_json AS BLOB)), 0) "
        "+ length(CAST(n.node_id AS BLOB)) + length(CAST(n.evidence_key AS BLOB))) AS approx_bytes "
        "FROM evidence_nodes AS n "
        "JOIN evidence_extraction_runs AS r ON r.extraction_run_id = n.extraction_run_id "
        "WHERE r.extractor_name = ? AND n.node_kind = 'table_cell' AND "
        + predicate
        + " ORDER BY n.extraction_run_id, n.rowid"
    )
    rows = conn.execute(sql, (_FULLTEXT_EXTRACTOR_NAME,)).fetchall()  # nosec B608 -- validated sqlite_schema identifiers; bound params
    by_run: dict[str, list[str]] = {}
    estimated_bytes = 0
    for row in rows:
        run_id = str(row[0])
        node_id = str(row[1])
        estimated_bytes += int(row[2] or 0)
        by_run.setdefault(run_id, []).append(node_id)
    return by_run, estimated_bytes, already_archived_runs


def _fetch_run_nodes_by_ids(
    conn: sqlite3.Connection, extraction_run_id: str, node_ids: Sequence[str]
) -> list[dict[str, object]]:
    fetched: list[dict[str, object]] = []
    for batch in _chunked(node_ids, _MAX_SQLITE_BIND_VARS):
        placeholders = ",".join("?" for _ in batch)
        cursor = conn.execute(
            "SELECT rowid AS native_rowid, node_id, evidence_key, revision, "  # nosec B608 -- placeholder count only; all values bound
            "extraction_run_id, parent_node_id, supersedes_node_id, node_kind, "
            "text, locator_json, locator_sha256, recorded_at "
            f"FROM evidence_nodes WHERE extraction_run_id = ? AND node_id IN ({placeholders}) "
            "ORDER BY rowid",
            (extraction_run_id, *batch),
        )
        names = tuple(col[0] for col in cursor.description or ())
        fetched.extend(dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall())
    fetched.sort(key=lambda item: (cast(int, item["native_rowid"]), str(item["node_id"])))
    return fetched


def _run_step_a(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    by_run, estimated_bytes, already_archived_runs = _select_eligible_leaf_table_cells(conn)
    result: dict[str, object] = {
        "eligible_runs": len(by_run),
        "eligible_nodes": sum(len(ids) for ids in by_run.values()),
        "already_archived_runs": already_archived_runs,
        "estimated_payload_bytes": estimated_bytes,
        "archived_runs": 0,
        "archived_nodes": 0,
        "deleted_nodes": 0,
        "archive_bytes_written": 0,
    }
    if not options.apply or not by_run:
        return result

    if not has_external_archives_table(conn):
        raise RuntimeError(
            "evidence_node_external_archives table is missing; run `alembic upgrade head` first"
        )

    archived_runs = archived_nodes = deleted_nodes = archive_bytes_written = 0
    for extraction_run_id in sorted(by_run):
        node_ids = by_run[extraction_run_id]
        run_row = conn.execute(
            "SELECT output_sha256 FROM evidence_extraction_runs WHERE extraction_run_id = ?",
            (extraction_run_id,),
        ).fetchone()
        if run_row is None or not run_row[0]:
            raise RuntimeError(
                f"extraction run {extraction_run_id} is missing output_sha256 commitment"
            )
        original_output_sha256 = str(run_row[0])

        rows_to_archive = _fetch_run_nodes_by_ids(conn, extraction_run_id, node_ids)
        if len(rows_to_archive) != len(node_ids):
            raise RuntimeError(
                f"eligible node count drift for run {extraction_run_id}: "
                f"expected {len(node_ids)}, fetched {len(rows_to_archive)}"
            )

        artifact = write_run_nodes_archive(options.state_root, extraction_run_id, rows_to_archive)
        archived_at = datetime.now(UTC).isoformat()
        kinds_json = json.dumps(list(artifact.archived_node_kinds), separators=(",", ":"))
        archive_bytes_written += artifact.archive_path.stat().st_size

        with _maintenance_transaction(conn, (_EVIDENCE_NODE_TRIGGER,)):
            conn.execute(
                "INSERT INTO evidence_node_external_archives ("
                "extraction_run_id, storage_relpath, archive_sha256, archived_node_count, "
                "archived_node_kinds_json, original_output_sha256, archived_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    extraction_run_id,
                    artifact.storage_relpath,
                    artifact.archive_sha256,
                    artifact.archived_node_count,
                    kinds_json,
                    original_output_sha256,
                    archived_at,
                ),
            )
            run_deleted = 0
            for batch in _chunked(node_ids, options.batch_size):
                placeholders = ",".join("?" for _ in batch)
                cur = conn.execute(
                    f"DELETE FROM evidence_nodes WHERE extraction_run_id = ? "  # nosec B608 -- placeholder count only; all values bound
                    f"AND node_id IN ({placeholders})",
                    (extraction_run_id, *batch),
                )
                run_deleted += cur.rowcount
            if run_deleted != len(node_ids):
                raise RuntimeError(
                    f"delete count mismatch for run {extraction_run_id}: "
                    f"expected {len(node_ids)}, deleted {run_deleted}"
                )

        archived_runs += 1
        archived_nodes += artifact.archived_node_count
        deleted_nodes += run_deleted
        log_event(
            "compact_evidence_run_archived",
            extraction_run_id=extraction_run_id,
            archived_nodes=artifact.archived_node_count,
            storage_relpath=artifact.storage_relpath,
            archive_sha256=artifact.archive_sha256,
        )

    _check_table_foreign_keys(conn, "evidence_nodes")
    _check_table_foreign_keys(conn, "evidence_node_external_archives")

    result["archived_runs"] = archived_runs
    result["archived_nodes"] = archived_nodes
    result["deleted_nodes"] = deleted_nodes
    result["archive_bytes_written"] = archive_bytes_written
    return result


def _active_ticker_roles(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    tables = _existing_tables(conn)
    if "tracked_companies" not in tables:
        return set(), set()
    rows = conn.execute(
        "SELECT UPPER(TRIM(ticker)), list_type FROM tracked_companies WHERE archived_at IS NULL"
    ).fetchall()
    roles_by_ticker: dict[str, set[str]] = {}
    for row in rows:
        ticker = str(row[0] or "").strip().upper()
        list_type = str(row[1] or "").strip()
        if ticker:
            roles_by_ticker.setdefault(ticker, set()).add(list_type)
    index_only = {t for t, roles in roles_by_ticker.items() if roles == {"index_member"}}
    non_index = {t for t, roles in roles_by_ticker.items() if roles - {"index_member"}}
    return index_only, non_index


def _index_member_only_issuers(conn: sqlite3.Connection) -> set[str]:
    tables = _existing_tables(conn)
    if "expected_documents" not in tables or "tracked_companies" not in tables:
        return set()
    index_only_tickers, non_index_tickers = _active_ticker_roles(conn)
    if not index_only_tickers:
        return set()

    tickers_by_issuer: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT issuer_id, UPPER(TRIM(ticker)) FROM expected_documents "
        "WHERE ticker IS NOT NULL AND TRIM(ticker) <> ''"
    ).fetchall():
        tickers_by_issuer.setdefault(str(row[0]), set()).add(str(row[1]))

    if {"securities", "security_listing_assertions"} <= tables:
        for row in conn.execute(
            "SELECT DISTINCT s.issuer_id, UPPER(TRIM(a.normalized_ticker)) "
            "FROM securities AS s "
            "JOIN security_listing_assertions AS a ON a.security_id = s.security_id "
            "WHERE a.normalized_ticker IS NOT NULL AND TRIM(a.normalized_ticker) <> ''"
        ).fetchall():
            tickers_by_issuer.setdefault(str(row[0]), set()).add(str(row[1]))

    return {
        issuer_id
        for issuer_id, issuer_tickers in tickers_by_issuer.items()
        if not (issuer_tickers & non_index_tickers) and bool(issuer_tickers & index_only_tickers)
    }


def _select_prunable_expected_documents(conn: sqlite3.Connection) -> list[str]:
    tables = _existing_tables(conn)
    if "expected_documents" not in tables:
        return []
    eligible_issuers = _index_member_only_issuers(conn)
    if not eligible_issuers:
        return []

    managed_children = {
        "source_coverage_assessments",
        "expected_document_obligation_bindings",
        "expected_document_lifecycle_revisions",
    }
    extra_protected: list[tuple[str, str]] = []
    if "latest_governed_document_entries" in tables and "expected_document_id" in _existing_columns(
        conn, "latest_governed_document_entries"
    ):
        extra_protected.append(("latest_governed_document_entries", "expected_document_id"))
    protected_refs = _discover_external_fk_refs(
        conn,
        "expected_documents",
        extra_named=extra_protected,
        exclude_tables=managed_children,
    )
    protected_clauses = [
        f"NOT EXISTS (SELECT 1 FROM {_quote_ident(t)} AS ref WHERE ref.{_quote_ident(c)} = ed.expected_document_id)"
        for t, c in protected_refs
    ]
    extra_where = (" AND " + " AND ".join(protected_clauses)) if protected_clauses else ""

    eligible_ids: list[str] = []
    for issuer_batch in _chunked(sorted(eligible_issuers), _MAX_SQLITE_BIND_VARS):
        placeholders = ",".join("?" for _ in issuer_batch)
        rows = conn.execute(
            f"SELECT ed.expected_document_id FROM expected_documents AS ed "  # nosec B608 -- validated sqlite_schema identifiers; bound params
            f"WHERE ed.issuer_id IN ({placeholders}){extra_where} "
            "ORDER BY ed.expected_document_id",
            tuple(issuer_batch),
        ).fetchall()
        eligible_ids.extend(str(row[0]) for row in rows)
    return eligible_ids


def _collect_dependent_chain_ids(
    conn: sqlite3.Connection,
    table: str,
    pk_col: str,
    parent_col: str,
    fk_col: str,
    fk_ids: Sequence[str],
) -> list[str]:
    if table not in _existing_tables(conn) or not fk_ids:
        return []
    qt, qpk, qparent, qfk = (
        _quote_ident(table),
        _quote_ident(pk_col),
        _quote_ident(parent_col),
        _quote_ident(fk_col),
    )
    seed_ids: set[str] = set()
    for batch in _chunked(fk_ids, _MAX_SQLITE_BIND_VARS):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT {qpk} FROM {qt} WHERE {qfk} IN ({placeholders})",  # nosec B608 -- trusted internal constants; bound params
            tuple(batch),
        ).fetchall()
        seed_ids.update(str(row[0]) for row in rows)
    if not seed_ids:
        return []

    collected = set(seed_ids)
    frontier = sorted(seed_ids)
    while frontier:
        next_frontier: list[str] = []
        for batch in _chunked(frontier, _MAX_SQLITE_BIND_VARS):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT {qpk} FROM {qt} WHERE {qparent} IN ({placeholders})",  # nosec B608 -- trusted internal constants; bound params
                tuple(batch),
            ).fetchall()
            for row in rows:
                child_id = str(row[0])
                if child_id not in collected:
                    collected.add(child_id)
                    next_frontier.append(child_id)
        frontier = next_frontier
    return sorted(collected)


def _delete_chain_leaf_to_root(
    conn: sqlite3.Connection,
    table: str,
    pk_col: str,
    parent_col: str,
    target_ids: Sequence[str],
    batch_size: int,
) -> int:
    if not target_ids:
        return 0
    qt, qpk, qparent = _quote_ident(table), _quote_ident(pk_col), _quote_ident(parent_col)
    remaining = set(target_ids)
    total_deleted = 0
    while remaining:
        leaves: list[str] = []
        for batch in _chunked(sorted(remaining), _MAX_SQLITE_BIND_VARS):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT t.{qpk} FROM {qt} AS t WHERE t.{qpk} IN ({placeholders}) "  # nosec B608 -- trusted internal constants; bound params
                f"AND NOT EXISTS (SELECT 1 FROM {qt} AS c WHERE c.{qparent} = t.{qpk})",
                tuple(batch),
            ).fetchall()
            leaves.extend(str(row[0]) for row in rows)
        if not leaves:
            raise RuntimeError(
                f"unable to make leaf-to-root progress deleting from {table} ({len(remaining)} remaining)"
            )
        for batch in _chunked(leaves, batch_size):
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                f"DELETE FROM {qt} WHERE {qpk} IN ({placeholders})",  # nosec B608 -- trusted internal constants; bound params
                tuple(batch),
            )
            total_deleted += cur.rowcount
        remaining.difference_update(leaves)
    return total_deleted


def _run_step_b_coverage(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    if not options.prune_index_member_coverage:
        return {"enabled": False}

    tables = _existing_tables(conn)
    expected_doc_ids = _select_prunable_expected_documents(conn)
    assessment_ids = _collect_dependent_chain_ids(
        conn,
        "source_coverage_assessments",
        "assessment_id",
        "supersedes_assessment_id",
        "expected_document_id",
        expected_doc_ids,
    )
    lifecycle_ids = _collect_dependent_chain_ids(
        conn,
        "expected_document_lifecycle_revisions",
        "lifecycle_id",
        "supersedes_lifecycle_id",
        "expected_document_id",
        expected_doc_ids,
    )
    binding_ids: list[str] = []
    if "expected_document_obligation_bindings" in tables and expected_doc_ids:
        for batch in _chunked(expected_doc_ids, _MAX_SQLITE_BIND_VARS):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                "SELECT binding_id FROM expected_document_obligation_bindings "  # nosec B608 -- placeholder count only; bound params
                f"WHERE expected_document_id IN ({placeholders}) ORDER BY binding_id",
                tuple(batch),
            ).fetchall()
            binding_ids.extend(str(row[0]) for row in rows)

    result: dict[str, object] = {
        "enabled": True,
        "eligible_expected_documents": len(expected_doc_ids),
        "eligible_source_coverage_assessments": len(assessment_ids),
        "eligible_obligation_bindings": len(binding_ids),
        "eligible_lifecycle_revisions": len(lifecycle_ids),
        "deleted_expected_documents": 0,
        "deleted_source_coverage_assessments": 0,
        "deleted_obligation_bindings": 0,
        "deleted_lifecycle_revisions": 0,
    }
    if not options.apply or not expected_doc_ids:
        return result

    trigger_names = [trg for tbl, trg in _COVERAGE_TRIGGERS if tbl in tables]
    with _maintenance_transaction(conn, trigger_names):
        del_assessments = _delete_chain_leaf_to_root(
            conn,
            "source_coverage_assessments",
            "assessment_id",
            "supersedes_assessment_id",
            assessment_ids,
            options.batch_size,
        )
        del_bindings = 0
        for batch in _chunked(binding_ids, options.batch_size):
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                "DELETE FROM expected_document_obligation_bindings "  # nosec B608 -- placeholder count only; bound params
                f"WHERE binding_id IN ({placeholders})",
                tuple(batch),
            )
            del_bindings += cur.rowcount
        del_lifecycle = _delete_chain_leaf_to_root(
            conn,
            "expected_document_lifecycle_revisions",
            "lifecycle_id",
            "supersedes_lifecycle_id",
            lifecycle_ids,
            options.batch_size,
        )
        del_expected = 0
        for batch in _chunked(expected_doc_ids, options.batch_size):
            placeholders = ",".join("?" for _ in batch)
            cur = conn.execute(
                "DELETE FROM expected_documents "  # nosec B608 -- placeholder count only; bound params
                f"WHERE expected_document_id IN ({placeholders})",
                tuple(batch),
            )
            del_expected += cur.rowcount

    for tbl, _ in _COVERAGE_TRIGGERS:
        if tbl in tables:
            _check_table_foreign_keys(conn, tbl)

    result["deleted_source_coverage_assessments"] = del_assessments
    result["deleted_obligation_bindings"] = del_bindings
    result["deleted_lifecycle_revisions"] = del_lifecycle
    result["deleted_expected_documents"] = del_expected
    log_event(
        "compact_index_member_coverage_pruned",
        expected_documents=del_expected,
        source_coverage_assessments=del_assessments,
        obligation_bindings=del_bindings,
        lifecycle_revisions=del_lifecycle,
    )
    return result


def _run_step_b_filing_sections(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    if not options.prune_inactive_filing_sections:
        return {"enabled": False}
    if "filing_sections" not in _existing_tables(conn):
        return {
            "enabled": True,
            "eligible_rows": 0,
            "estimated_payload_bytes": 0,
            "deleted_rows": 0,
        }

    index_only_tickers, _ = _active_ticker_roles(conn)
    eligible_ids: list[int] = []
    estimated_bytes = 0
    for row in conn.execute(
        "SELECT id, UPPER(TRIM(ticker)), is_active, length(CAST(text AS BLOB)) "
        "FROM filing_sections ORDER BY id"
    ).fetchall():
        if int(row[2]) == 0 or str(row[1] or "") in index_only_tickers:
            eligible_ids.append(int(row[0]))
            estimated_bytes += int(row[3] or 0)

    result: dict[str, object] = {
        "enabled": True,
        "eligible_rows": len(eligible_ids),
        "estimated_payload_bytes": estimated_bytes,
        "deleted_rows": 0,
    }
    if not options.apply or not eligible_ids:
        return result

    deleted = 0
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        for batch in _chunked(eligible_ids, options.batch_size):
            placeholders = ",".join("?" for _ in batch)
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    f"DELETE FROM filing_sections WHERE id IN ({placeholders})",  # nosec B608 -- placeholder count only; bound params
                    tuple(batch),
                )
                deleted += cur.rowcount
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.isolation_level = prev_isolation

    _check_table_foreign_keys(conn, "filing_sections")
    result["deleted_rows"] = deleted
    log_event("compact_filing_sections_pruned", deleted_rows=deleted)
    return result


def _select_prunable_legacy_matches(conn: sqlite3.Connection) -> tuple[list[str], int]:
    tables = _existing_tables(conn)
    if "legacy_fact_evidence_match_revisions" not in tables:
        return [], 0

    base_clauses = ["m.outcome = 'accepted'"]
    if "fact_observation_match_proofs" in tables:
        base_clauses.append(
            "EXISTS (SELECT 1 FROM fact_observation_match_proofs AS p "
            "WHERE p.match_revision_id = m.match_revision_id)"
        )
    if "fact_observations_v2" in tables:
        base_clauses.append(
            "EXISTS (SELECT 1 FROM fact_observations_v2 AS f "
            "WHERE f.legacy_match_revision_id = m.match_revision_id)"
        )
    for table_name, col_name in _discover_external_fk_refs(
        conn,
        "legacy_fact_evidence_match_revisions",
        exclude_tables={"fact_observation_match_proofs", "fact_observations_v2"},
    ):
        base_clauses.append(
            f"EXISTS (SELECT 1 FROM {_quote_ident(table_name)} AS ref "
            f"WHERE ref.{_quote_ident(col_name)} = m.match_revision_id)"
        )

    sql = (
        "WITH RECURSIVE retained(match_revision_id) AS ("
        "  SELECT m.match_revision_id FROM legacy_fact_evidence_match_revisions AS m "
        f"  WHERE {' OR '.join(base_clauses)} "
        "  UNION "
        "  SELECT parent.supersedes_match_revision_id "
        "  FROM legacy_fact_evidence_match_revisions AS parent "
        "  JOIN retained ON retained.match_revision_id = parent.match_revision_id "
        "  WHERE parent.supersedes_match_revision_id IS NOT NULL"
        ") "
        "SELECT m.match_revision_id, "
        "(length(CAST(m.fact_payload_json AS BLOB)) + length(CAST(m.candidate_manifest_json AS BLOB)) "
        "+ length(CAST(m.reason_details_json AS BLOB))) AS approx_bytes "
        "FROM legacy_fact_evidence_match_revisions AS m "
        "WHERE m.match_revision_id NOT IN (SELECT match_revision_id FROM retained) "
        "ORDER BY m.match_revision_id"
    )
    rows = conn.execute(sql).fetchall()  # nosec B608 -- validated sqlite_schema identifiers only
    return [str(row[0]) for row in rows], sum(int(row[1] or 0) for row in rows)


def _run_step_c_legacy_matches(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    if not options.prune_unreferenced_legacy_matches:
        return {"enabled": False}

    target_ids, estimated_bytes = _select_prunable_legacy_matches(conn)
    result: dict[str, object] = {
        "enabled": True,
        "eligible_rows": len(target_ids),
        "estimated_payload_bytes": estimated_bytes,
        "deleted_rows": 0,
    }
    if not options.apply or not target_ids:
        return result

    with _maintenance_transaction(conn, (_LEGACY_MATCH_TRIGGER,)):
        deleted = _delete_chain_leaf_to_root(
            conn,
            "legacy_fact_evidence_match_revisions",
            "match_revision_id",
            "supersedes_match_revision_id",
            target_ids,
            options.batch_size,
        )

    _check_table_foreign_keys(conn, "legacy_fact_evidence_match_revisions")
    result["deleted_rows"] = deleted
    log_event("compact_legacy_matches_pruned", deleted_rows=deleted)
    return result


def _run_step_d_vacuum(
    conn: sqlite3.Connection,
    options: CompactionOptions,
    *,
    db_bytes_before: int,
    log_event: EventLogger,
) -> dict[str, object]:
    if not options.vacuum or not options.apply:
        return {
            "requested": options.vacuum,
            "executed": False,
            "db_bytes_before": db_bytes_before,
            "db_bytes_after": options.db_path.stat().st_size,
        }

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        raise RuntimeError(
            f"post-compaction PRAGMA foreign_key_check failed with {len(fk_violations)} violation(s)"
        )
    conn.execute("PRAGMA cache_size = -262144")
    integrity_results = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity_results != ["ok"]:
        raise RuntimeError(f"post-compaction PRAGMA integrity_check failed: {integrity_results!r}")

    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.isolation_level = prev_isolation

    db_bytes_after = options.db_path.stat().st_size
    log_event(
        "compact_vacuum_completed",
        db_bytes_before=db_bytes_before,
        db_bytes_after=db_bytes_after,
        db_bytes_reclaimed=db_bytes_before - db_bytes_after,
    )
    return {
        "requested": True,
        "executed": True,
        "foreign_key_check": "ok",
        "integrity_check": "ok",
        "db_bytes_before": db_bytes_before,
        "db_bytes_after": db_bytes_after,
        "db_bytes_reclaimed": db_bytes_before - db_bytes_after,
    }


@contextmanager
def _maybe_acquire_write_lock(options: CompactionOptions) -> Generator[None]:
    if not options.apply or inherited_lock_is_valid(options.project_root, "portfolio-db"):
        yield
        return
    with hold_run_lock(
        options.db_path,
        owner="compact-evidence-and-coverage",
        timeout_s=options.lock_timeout_s,
    ):
        yield


def run_compaction(
    options: CompactionOptions, *, log_event: EventLogger = _noop_log_event
) -> dict[str, object]:
    """Execute dry-run measurement or guarded single-writer compaction."""
    if options.batch_size < 1:
        raise ValueError("--batch-size must be >= 1")
    db_bytes_before = options.db_path.stat().st_size
    with _maybe_acquire_write_lock(options):
        role = SQLiteConnectionRole.WRITER if options.apply else SQLiteConnectionRole.READ_ONLY
        conn = connect_sqlite(options.db_path, role=role, schema_preflight=options.apply)
        try:
            step_a = _run_step_a(conn, options, log_event)
            step_b_coverage = _run_step_b_coverage(conn, options, log_event)
            step_b_sections = _run_step_b_filing_sections(conn, options, log_event)
            step_c = _run_step_c_legacy_matches(conn, options, log_event)
            step_d = _run_step_d_vacuum(
                conn, options, db_bytes_before=db_bytes_before, log_event=log_event
            )
        finally:
            conn.close()

    return {
        "schema_version": "compact-evidence-and-coverage/v1",
        "mode": "apply" if options.apply else "dry_run",
        "db_path": str(options.db_path),
        "state_root": str(options.state_root),
        "batch_size": options.batch_size,
        "step_a_evidence_nodes": step_a,
        "step_b_index_member_coverage": step_b_coverage,
        "step_b_filing_sections": step_b_sections,
        "step_c_legacy_matches": step_c,
        "step_d_vacuum": step_d,
    }


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_LOCK_TIMEOUT_S",
    "CompactionOptions",
    "run_compaction",
]
