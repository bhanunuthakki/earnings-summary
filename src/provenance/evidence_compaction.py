"""Guarded single-writer compaction logic for evidence_nodes externalization and coverage pruning.

Called by the thin CLI entrypoint ``execution/compact_evidence_and_coverage.py``.
All database queries use static SQL templates and explicit allowlists (zero
dynamic SQL string interpolation).
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

from filings.store import delete_sections_by_ids
from provenance.evidence_node_store import (
    has_external_archives_table,
    write_run_nodes_archive,
)
from provenance.selection import selected_filing_sections_relation
from run_lock import hold_run_lock
from runtime.job_runtime import inherited_lock_is_valid
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

DEFAULT_BATCH_SIZE = 20_000
DEFAULT_LOCK_TIMEOUT_S = 60.0
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

# Static SQL for every allowlisted external FK table referencing evidence_nodes(node_id).
# Any additional table discovered via PRAGMA foreign_key_list fails closed until registered here.
_EVIDENCE_NODE_FK_QUERIES: dict[tuple[str, str], str] = {
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

_CANDIDATE_LEAF_CELLS_SQL = (
    "SELECT n.extraction_run_id, n.node_id, "
    "(length(CAST(n.text AS BLOB)) + COALESCE(length(CAST(n.locator_json AS BLOB)), 0) "
    "+ length(CAST(n.node_id AS BLOB)) + length(CAST(n.evidence_key AS BLOB))) AS approx_bytes "
    "FROM evidence_nodes AS n "
    "JOIN evidence_extraction_runs AS r ON r.extraction_run_id = n.extraction_run_id "
    "WHERE r.extractor_name = ? AND n.node_kind = 'table_cell'"
)

_SELF_REFERENCED_NODE_IDS_SQL = (
    "SELECT parent_node_id, supersedes_node_id FROM evidence_nodes "
    "WHERE parent_node_id IS NOT NULL OR supersedes_node_id IS NOT NULL"
)

_FETCH_RUN_NODES_SQL = (
    "SELECT rowid AS native_rowid, node_id, evidence_key, revision, "
    "extraction_run_id, parent_node_id, supersedes_node_id, node_kind, "
    "text, locator_json, locator_sha256, recorded_at "
    "FROM evidence_nodes WHERE extraction_run_id = ? ORDER BY rowid"
)

_DROP_TRIGGER_SQL: dict[str, str] = {
    _EVIDENCE_NODE_TRIGGER: ("DROP TRIGGER IF EXISTS trg_evidence_nodes_append_only_delete"),
    _LEGACY_MATCH_TRIGGER: (
        "DROP TRIGGER IF EXISTS trg_legacy_fact_evidence_match_revisions_append_only_delete"
    ),
    "trg_source_coverage_assessments_append_only_delete": (
        "DROP TRIGGER IF EXISTS trg_source_coverage_assessments_append_only_delete"
    ),
    "trg_expected_document_obligation_bindings_append_only_delete": (
        "DROP TRIGGER IF EXISTS trg_expected_document_obligation_bindings_append_only_delete"
    ),
    "trg_expected_document_lifecycle_revisions_append_only_delete": (
        "DROP TRIGGER IF EXISTS trg_expected_document_lifecycle_revisions_append_only_delete"
    ),
    "trg_expected_documents_append_only_delete": (
        "DROP TRIGGER IF EXISTS trg_expected_documents_append_only_delete"
    ),
}

_FK_CHECK_SQL: dict[str, str] = {
    "evidence_nodes": "PRAGMA foreign_key_check(evidence_nodes)",
    "evidence_node_external_archives": "PRAGMA foreign_key_check(evidence_node_external_archives)",
    "source_coverage_assessments": "PRAGMA foreign_key_check(source_coverage_assessments)",
    "expected_document_obligation_bindings": (
        "PRAGMA foreign_key_check(expected_document_obligation_bindings)"
    ),
    "expected_document_lifecycle_revisions": (
        "PRAGMA foreign_key_check(expected_document_lifecycle_revisions)"
    ),
    "expected_documents": "PRAGMA foreign_key_check(expected_documents)",
    "filing_sections": "PRAGMA foreign_key_check(filing_sections)",
    "legacy_fact_evidence_match_revisions": (
        "PRAGMA foreign_key_check(legacy_fact_evidence_match_revisions)"
    ),
}


@dataclass(frozen=True, slots=True)
class _ChainTableSpec:
    table: str
    select_links_sql: str
    delete_by_pk_sql: str


_CHAIN_SPECS: dict[str, _ChainTableSpec] = {
    "source_coverage_assessments": _ChainTableSpec(
        table="source_coverage_assessments",
        select_links_sql=(
            "SELECT assessment_id, supersedes_assessment_id, expected_document_id "
            "FROM source_coverage_assessments"
        ),
        delete_by_pk_sql="DELETE FROM source_coverage_assessments WHERE assessment_id = ?",
    ),
    "expected_document_lifecycle_revisions": _ChainTableSpec(
        table="expected_document_lifecycle_revisions",
        select_links_sql=(
            "SELECT lifecycle_id, supersedes_lifecycle_id, expected_document_id "
            "FROM expected_document_lifecycle_revisions"
        ),
        delete_by_pk_sql="DELETE FROM expected_document_lifecycle_revisions WHERE lifecycle_id = ?",
    ),
    "legacy_fact_evidence_match_revisions": _ChainTableSpec(
        table="legacy_fact_evidence_match_revisions",
        select_links_sql=(
            "SELECT match_revision_id, supersedes_match_revision_id, NULL "
            "FROM legacy_fact_evidence_match_revisions"
        ),
        delete_by_pk_sql="DELETE FROM legacy_fact_evidence_match_revisions WHERE match_revision_id = ?",
    ),
}

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


def _existing_views(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'view'").fetchall()
    }


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    }


def _discover_incoming_fk_refs(
    conn: sqlite3.Connection,
    target_table: str,
) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for table_name in sorted(_existing_tables(conn)):
        if table_name == target_table:
            continue
        for fk in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(table_name)})").fetchall():
            if str(fk[2]) == target_table:
                refs.add((table_name, str(fk[3])))
    return refs


def _chunked_str(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    step = max(1, size)
    for offset in range(0, len(items), step):
        yield items[offset : offset + step]


def _chunked_int(items: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    step = max(1, size)
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
    """Drop allowlisted delete triggers, execute inside BEGIN IMMEDIATE, and restore verbatim."""
    if conn.in_transaction:
        raise RuntimeError("maintenance transaction requires an idle SQLite connection")
    captured: list[tuple[str, str]] = []
    for name in trigger_names:
        if name not in _DROP_TRIGGER_SQL:
            raise RuntimeError(f"refusing to manage unregistered trigger: {name}")
        sql = _capture_trigger_sql(conn, name)
        if sql is not None:
            captured.append((name, sql))

    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, _ in captured:
                conn.execute(_DROP_TRIGGER_SQL[name])
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
    sql = _FK_CHECK_SQL.get(table)
    if sql is None:
        raise RuntimeError(f"unregistered table for foreign-key check: {table}")
    violation = conn.execute(sql).fetchone()
    if violation is not None:
        raise RuntimeError(f"foreign-key verification failed on {table}: {tuple(violation)!r}")


def _collect_referenced_evidence_node_ids(conn: sqlite3.Connection) -> set[str]:
    tables = _existing_tables(conn)
    discovered = _discover_incoming_fk_refs(conn, "evidence_nodes")
    unregistered = discovered - set(_EVIDENCE_NODE_FK_QUERIES)
    if unregistered:
        formatted = ", ".join(f"{t}.{c}" for t, c in sorted(unregistered))
        raise RuntimeError(
            f"unregistered FK reference(s) to evidence_nodes cannot be compacted safely: {formatted}"
        )

    referenced: set[str] = set()
    # O(N) linear scan for self-referential parent/supersedes links (unindexed columns).
    for row in conn.execute(_SELF_REFERENCED_NODE_IDS_SQL).fetchall():
        if row[0] is not None:
            referenced.add(str(row[0]))
        if row[1] is not None:
            referenced.add(str(row[1]))

    for (table_name, col_name), query_sql in _EVIDENCE_NODE_FK_QUERIES.items():
        if table_name not in tables or col_name not in _existing_columns(conn, table_name):
            continue
        for row in conn.execute(query_sql).fetchall():
            if row[0] is not None:
                referenced.add(str(row[0]))
    return referenced


def _select_eligible_leaf_table_cells(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[str]], int, int]:
    """Return ({extraction_run_id: [node_id, ...]}, estimated_payload_bytes, already_archived_runs)."""
    tables = _existing_tables(conn)
    if not {"evidence_nodes", "evidence_extraction_runs"} <= tables:
        return {}, 0, 0

    referenced_node_ids = _collect_referenced_evidence_node_ids(conn)
    archived_runs: set[str] = set()
    if has_external_archives_table(conn):
        archived_runs = {
            str(row[0])
            for row in conn.execute(
                "SELECT extraction_run_id FROM evidence_node_external_archives"
            ).fetchall()
        }

    rows = conn.execute(_CANDIDATE_LEAF_CELLS_SQL, (_FULLTEXT_EXTRACTOR_NAME,)).fetchall()
    by_run: dict[str, list[str]] = {}
    estimated_bytes = 0
    for row in rows:
        run_id = str(row[0])
        node_id = str(row[1])
        if run_id in archived_runs or node_id in referenced_node_ids:
            continue
        estimated_bytes += int(row[2] or 0)
        by_run.setdefault(run_id, []).append(node_id)
    return by_run, estimated_bytes, len(archived_runs)


def _fetch_run_nodes_by_ids(
    conn: sqlite3.Connection, extraction_run_id: str, node_ids: Sequence[str]
) -> list[dict[str, object]]:
    target = set(node_ids)
    cursor = conn.execute(_FETCH_RUN_NODES_SQL, (extraction_run_id,))
    names = tuple(col[0] for col in cursor.description or ())
    fetched = [
        dict(zip(names, tuple(row), strict=True))
        for row in cursor.fetchall()
        if str(row[1]) in target
    ]
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
            for batch in _chunked_str(node_ids, options.batch_size):
                cur = conn.executemany(
                    "DELETE FROM evidence_nodes WHERE extraction_run_id = ? AND node_id = ?",
                    [(extraction_run_id, node_id) for node_id in batch],
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
    if "tracked_companies" not in _existing_tables(conn):
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

    known_expected_doc_refs = {
        ("source_coverage_assessments", "expected_document_id"),
        ("expected_document_obligation_bindings", "expected_document_id"),
        ("expected_document_lifecycle_revisions", "expected_document_id"),
        ("latest_governed_document_entries", "expected_document_id"),
    }
    unregistered = _discover_incoming_fk_refs(conn, "expected_documents") - known_expected_doc_refs
    if unregistered:
        formatted = ", ".join(f"{t}.{c}" for t, c in sorted(unregistered))
        raise RuntimeError(
            f"unregistered FK reference(s) to expected_documents cannot be pruned safely: {formatted}"
        )

    protected_ids: set[str] = set()
    if "latest_governed_document_entries" in tables and "expected_document_id" in _existing_columns(
        conn, "latest_governed_document_entries"
    ):
        protected_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT expected_document_id FROM latest_governed_document_entries "
                "WHERE expected_document_id IS NOT NULL"
            ).fetchall()
        }

    return [
        str(row[0])
        for row in conn.execute(
            "SELECT expected_document_id, issuer_id FROM expected_documents "
            "ORDER BY expected_document_id"
        ).fetchall()
        if str(row[1]) in eligible_issuers and str(row[0]) not in protected_ids
    ]


def _collect_chain_rows(
    conn: sqlite3.Connection,
    table: str,
    fk_ids: set[str],
) -> tuple[list[str], dict[str, str | None]]:
    """Return (ordered_target_ids, parent_by_id) for all rows referencing ``fk_ids`` + descendants."""
    if table not in _existing_tables(conn) or not fk_ids:
        return [], {}
    spec = _CHAIN_SPECS[table]
    parent_by_id: dict[str, str | None] = {}
    children_by_parent: dict[str, list[str]] = {}
    seed_ids: set[str] = set()
    for row in conn.execute(spec.select_links_sql).fetchall():
        pk = str(row[0])
        parent = None if row[1] is None else str(row[1])
        fk_val = None if row[2] is None else str(row[2])
        parent_by_id[pk] = parent
        if parent is not None:
            children_by_parent.setdefault(parent, []).append(pk)
        if fk_val is not None and fk_val in fk_ids:
            seed_ids.add(pk)

    collected = set(seed_ids)
    frontier = list(seed_ids)
    while frontier:
        current = frontier.pop()
        for child in children_by_parent.get(current, ()):
            if child not in collected:
                collected.add(child)
                frontier.append(child)
    return sorted(collected), parent_by_id


def _delete_chain_leaf_to_root(
    conn: sqlite3.Connection,
    table: str,
    target_ids: Sequence[str],
    parent_by_id: dict[str, str | None],
    batch_size: int,
) -> int:
    """Delete ``target_ids`` leaf-to-root using static SQL so self-referential FKs never fail."""
    if not target_ids:
        return 0
    spec = _CHAIN_SPECS[table]
    remaining = set(target_ids)
    # Also ensure no surviving row outside target_ids points at any row in remaining.
    external_parents = {
        parent for pk, parent in parent_by_id.items() if pk not in remaining and parent is not None
    }
    if remaining & external_parents:
        raise RuntimeError(f"cannot prune {table}: retained row(s) reference target parent(s)")

    # O(N) topological peel (leaf-to-root) using child-reference counts.
    child_count: dict[str, int] = dict.fromkeys(remaining, 0)
    for pk in remaining:
        parent = parent_by_id.get(pk)
        if parent is not None and parent in child_count:
            child_count[parent] += 1

    ready = sorted(pk for pk, count in child_count.items() if count == 0)
    total_deleted = 0
    while remaining:
        if not ready:
            raise RuntimeError(
                f"unable to make leaf-to-root progress deleting from {table} ({len(remaining)} remaining)"
            )
        next_ready: list[str] = []
        for batch in _chunked_str(ready, batch_size):
            cur = conn.executemany(spec.delete_by_pk_sql, [(pk,) for pk in batch])
            total_deleted += cur.rowcount
            for pk in batch:
                remaining.discard(pk)
                parent = parent_by_id.get(pk)
                if parent is not None and parent in child_count:
                    child_count[parent] -= 1
                    if child_count[parent] == 0:
                        next_ready.append(parent)
        ready = sorted(next_ready)
    return total_deleted


def _run_step_b_coverage(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    if not options.prune_index_member_coverage:
        return {"enabled": False}

    tables = _existing_tables(conn)
    expected_doc_ids = _select_prunable_expected_documents(conn)
    expected_doc_set = set(expected_doc_ids)

    assessment_ids, assessment_parents = _collect_chain_rows(
        conn, "source_coverage_assessments", expected_doc_set
    )
    lifecycle_ids, lifecycle_parents = _collect_chain_rows(
        conn, "expected_document_lifecycle_revisions", expected_doc_set
    )
    binding_ids: list[str] = []
    if "expected_document_obligation_bindings" in tables and expected_doc_set:
        binding_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT binding_id, expected_document_id "
                "FROM expected_document_obligation_bindings ORDER BY binding_id"
            ).fetchall()
            if str(row[1]) in expected_doc_set
        ]

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
            assessment_ids,
            assessment_parents,
            options.batch_size,
        )
        del_bindings = 0
        for batch in _chunked_str(binding_ids, options.batch_size):
            cur = conn.executemany(
                "DELETE FROM expected_document_obligation_bindings WHERE binding_id = ?",
                [(bid,) for bid in batch],
            )
            del_bindings += cur.rowcount
        del_lifecycle = _delete_chain_leaf_to_root(
            conn,
            "expected_document_lifecycle_revisions",
            lifecycle_ids,
            lifecycle_parents,
            options.batch_size,
        )
        del_expected = 0
        for batch in _chunked_str(expected_doc_ids, options.batch_size):
            cur = conn.executemany(
                "DELETE FROM expected_documents WHERE expected_document_id = ?",
                [(ed_id,) for ed_id in batch],
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
    tables = _existing_tables(conn)
    if "filing_sections" not in tables:
        return {
            "enabled": True,
            "eligible_rows": 0,
            "estimated_payload_bytes": 0,
            "deleted_rows": 0,
        }

    index_only_tickers, _ = _active_ticker_roles(conn)
    views = _existing_views(conn)
    eligible_bytes_by_id: dict[int, int] = {}

    # 1. Inactive rows via the migration-0072 v_inactive_filing_sections view.
    if "v_inactive_filing_sections" in views:
        for row in conn.execute(
            "SELECT id, length(CAST(text AS BLOB)) FROM v_inactive_filing_sections ORDER BY id"
        ).fetchall():
            eligible_bytes_by_id[int(row[0])] = int(row[1] or 0)
    elif options.apply:
        raise RuntimeError(
            "v_inactive_filing_sections view is missing; run `alembic upgrade head` first"
        )

    # 2. Active rows belonging strictly to index_member-only tickers via provenance.selection.
    if index_only_tickers:
        active_relation = selected_filing_sections_relation(conn)
        if active_relation.sql == "v_active_filing_sections":
            for row in conn.execute(
                "SELECT id, UPPER(TRIM(ticker)), length(CAST(text AS BLOB)) "
                "FROM v_active_filing_sections ORDER BY id"
            ).fetchall():
                if str(row[1] or "") in index_only_tickers:
                    eligible_bytes_by_id[int(row[0])] = int(row[2] or 0)

    eligible_ids = sorted(eligible_bytes_by_id)
    estimated_bytes = sum(eligible_bytes_by_id.values())

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
        for batch in _chunked_int(eligible_ids, options.batch_size):
            conn.execute("BEGIN IMMEDIATE")
            try:
                deleted += delete_sections_by_ids(conn, batch)
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


def _select_prunable_legacy_matches(
    conn: sqlite3.Connection,
) -> tuple[list[str], dict[str, str | None], int]:
    tables = _existing_tables(conn)
    if "legacy_fact_evidence_match_revisions" not in tables:
        return [], {}, 0

    known_legacy_match_refs = {
        ("fact_observation_match_proofs", "match_revision_id"),
        ("fact_observations_v2", "legacy_match_revision_id"),
    }
    unregistered = (
        _discover_incoming_fk_refs(conn, "legacy_fact_evidence_match_revisions")
        - known_legacy_match_refs
    )
    if unregistered:
        formatted = ", ".join(f"{t}.{c}" for t, c in sorted(unregistered))
        raise RuntimeError(
            f"unregistered FK reference(s) to legacy_fact_evidence_match_revisions: {formatted}"
        )

    retained_seed: set[str] = set()
    if "fact_observation_match_proofs" in tables:
        retained_seed.update(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT match_revision_id FROM fact_observation_match_proofs "
                "WHERE match_revision_id IS NOT NULL"
            ).fetchall()
        )
    if "fact_observations_v2" in tables:
        retained_seed.update(
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT legacy_match_revision_id FROM fact_observations_v2 "
                "WHERE legacy_match_revision_id IS NOT NULL"
            ).fetchall()
        )

    parent_by_id: dict[str, str | None] = {}
    bytes_by_id: dict[str, int] = {}
    for row in conn.execute(
        "SELECT match_revision_id, supersedes_match_revision_id, outcome, "
        "(length(CAST(fact_payload_json AS BLOB)) + length(CAST(candidate_manifest_json AS BLOB)) "
        "+ length(CAST(reason_details_json AS BLOB))) AS approx_bytes "
        "FROM legacy_fact_evidence_match_revisions ORDER BY match_revision_id"
    ).fetchall():
        mid = str(row[0])
        parent_by_id[mid] = None if row[1] is None else str(row[1])
        bytes_by_id[mid] = int(row[3] or 0)
        if str(row[2]) == "accepted":
            retained_seed.add(mid)

    # Compute transitive closure of retained rows along supersedes_match_revision_id.
    retained = set(retained_seed)
    frontier = list(retained_seed)
    while frontier:
        current = frontier.pop()
        parent = parent_by_id.get(current)
        if parent is not None and parent not in retained:
            retained.add(parent)
            frontier.append(parent)

    prunable = sorted(mid for mid in parent_by_id if mid not in retained)
    estimated_bytes = sum(bytes_by_id[mid] for mid in prunable)
    return prunable, parent_by_id, estimated_bytes


def _run_step_c_legacy_matches(
    conn: sqlite3.Connection, options: CompactionOptions, log_event: EventLogger
) -> dict[str, object]:
    if not options.prune_unreferenced_legacy_matches:
        return {"enabled": False}

    target_ids, parent_by_id, estimated_bytes = _select_prunable_legacy_matches(conn)
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
            target_ids,
            parent_by_id,
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
            conn.execute("PRAGMA cache_size = -262144")
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
