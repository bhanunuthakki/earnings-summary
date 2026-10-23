"""Read verification retains exact global FTS coverage without per-chunk scans."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator

import pytest

from provenance import search_index_lineage

verify_representation: Callable[..., bool] = getattr(
    search_index_lineage, "_lexical_representation_complete"
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE evidence_nodes (node_id TEXT PRIMARY KEY, extraction_run_id TEXT NOT NULL);
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL, text TEXT NOT NULL
        );
        CREATE INDEX ix_chunks_manifest_node ON search_chunks(manifest_id, evidence_node_id);
        CREATE VIRTUAL TABLE search_lexical_chunks USING fts5(chunk_id UNINDEXED, text);
        INSERT INTO evidence_nodes VALUES ('node', 'extraction'), ('other', 'other-extraction');
        INSERT INTO search_chunks VALUES ('target', 'manifest', 'node', 'Exact text');
        INSERT INTO search_lexical_chunks VALUES ('target', 'Exact text');
        """
    )
    try:
        yield database
    finally:
        database.close()


def complete(conn: sqlite3.Connection, *, extraction: str = "extraction") -> bool:
    return verify_representation(conn, manifest_id="manifest", extraction_run_id=extraction)


@pytest.mark.parametrize("replacement", [None, "wrong", "exact text", "Exact text "])
def test_wrong_or_null_text_fails(conn: sqlite3.Connection, replacement: str | None) -> None:
    conn.execute("UPDATE search_lexical_chunks SET text=? WHERE chunk_id='target'", (replacement,))
    assert not complete(conn)


def test_missing_row_fails(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_lexical_chunks WHERE chunk_id='target'")
    assert not complete(conn)


@pytest.mark.parametrize("text", ["Exact text", "different", None])
def test_global_duplicate_fails(conn: sqlite3.Connection, text: str | None) -> None:
    # FTS rows have no manifest identity. An extra row anywhere for this ID is dissent.
    conn.execute("INSERT INTO search_lexical_chunks VALUES ('target', ?)", (text,))
    assert not complete(conn)


def test_other_extractions_and_unrelated_corruption_are_excluded(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO search_chunks VALUES ('other', 'manifest', 'other', 'missing')")
    conn.execute(
        "INSERT INTO search_chunks VALUES ('foreign', 'other-manifest', 'node', 'missing')"
    )
    conn.executemany(
        "INSERT INTO search_lexical_chunks VALUES ('unrelated', ?)", [(None,), ("bad",)]
    )
    assert complete(conn)
    assert not complete(conn, extraction="other-extraction")


def test_empty_requested_extraction_is_complete(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_lexical_chunks")
    assert complete(conn, extraction="absent")


def test_valid_exact_projection_and_single_global_scan(conn: sqlite3.Connection) -> None:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    assert complete(conn)
    conn.set_trace_callback(None)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN " + next(sql for sql in statements if not sql.startswith("--"))
    ).fetchall()
    details = [str(row[3]).upper() for row in plan]
    assert sum("SCAN" in item and "VIRTUAL TABLE" in item for item in details) == 1
    assert not any("CORRELATED" in item for item in details)


def test_unrelated_fts_growth_does_not_multiply_per_chunk_scans(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO search_chunks VALUES (?, 'manifest', 'node', ?)",
        [(f"target-{index}", f"text-{index}") for index in range(128)],
    )
    conn.executemany(
        "INSERT INTO search_lexical_chunks VALUES (?, ?)",
        [(f"target-{index}", f"text-{index}") for index in range(128)]
        + [(f"unrelated-{index}", "noise") for index in range(4096)],
    )
    # Bound SQLite work, not wall-clock speed. The correlated scan exhausts this
    # budget; one grouped pass retains enough headroom across supported runtimes.
    callbacks = 0

    def budget() -> int:
        nonlocal callbacks
        callbacks += 1
        return int(callbacks > 500)

    conn.set_progress_handler(budget, 1000)
    try:
        assert complete(conn)
    finally:
        conn.set_progress_handler(None, 0)


@pytest.mark.parametrize(
    ("canonical_id", "stored_ids", "expected"),
    [
        ("123", (123, "123"), True),
        ("1.5", (1.5, "1.5"), True),
        ("1.0", (1, 1.0), False),
        ("1", (1, 1.0, "1"), True),
        ("123", (123,), False),
        ("123", (b"123", "123"), True),
        ("123", (b"123",), False),
    ],
)
def test_global_id_grouping_preserves_original_storage_class_comparison(
    conn: sqlite3.Connection,
    canonical_id: str,
    stored_ids: tuple[object, ...],
    expected: bool,
) -> None:
    conn.execute("DELETE FROM search_lexical_chunks")
    conn.execute("DELETE FROM search_chunks")
    conn.execute(
        "INSERT INTO search_chunks VALUES (?, 'manifest', 'node', 'Exact text')",
        (canonical_id,),
    )
    conn.executemany(
        "INSERT INTO search_lexical_chunks VALUES (?, 'Exact text')",
        [(value,) for value in stored_ids],
    )
    original_missing = conn.execute(
        "SELECT 1 FROM search_chunks AS chunk "
        "JOIN evidence_nodes AS node ON node.node_id = chunk.evidence_node_id "
        "WHERE chunk.manifest_id = ? AND node.extraction_run_id = ? "
        "AND ((SELECT COUNT(*) FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id) <> 1 "
        "OR NOT EXISTS (SELECT 1 FROM search_lexical_chunks AS lexical "
        "WHERE lexical.chunk_id = chunk.chunk_id AND lexical.text = chunk.text)) LIMIT 1",
        ("manifest", "extraction"),
    ).fetchone()
    # FTS columns have BLOB affinity: numeric/BLOB IDs do not match TEXT IDs.
    # A cast would change the original comparison and can create false dissent.
    assert (original_missing is None) is expected
    assert complete(conn) is expected
