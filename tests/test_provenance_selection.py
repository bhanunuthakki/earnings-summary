"""Contract tests for current-evidence SQL relation selection."""

from __future__ import annotations

import sqlite3

from provenance.selection import selected_filing_sections_relation, selected_transcripts_relation


def _conn(*, lifecycle: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    lifecycle_columns = ", is_active INTEGER NOT NULL DEFAULT 1" if lifecycle else ""
    conn.executescript(
        "CREATE TABLE transcripts (id INTEGER PRIMARY KEY" + lifecycle_columns + ");"
        "CREATE TABLE filing_sections (id INTEGER PRIMARY KEY" + lifecycle_columns + ");"
    )
    return conn


def test_selected_transcripts_relation_uses_legacy_table_before_0214() -> None:
    relation = selected_transcripts_relation(_conn(lifecycle=False))

    assert str(relation) == "transcripts"
    assert relation.selection_mode == "legacy_table"


def test_selected_filing_sections_relation_uses_legacy_table_before_0214() -> None:
    relation = selected_filing_sections_relation(_conn(lifecycle=False))

    assert str(relation) == "filing_sections"
    assert relation.selection_mode == "legacy_table"


def test_selected_transcripts_relation_filters_inactive_rows_after_0214() -> None:
    conn = _conn(lifecycle=True)
    conn.executemany("INSERT INTO transcripts (id, is_active) VALUES (?, ?)", [(1, 1), (2, 0)])

    relation = selected_transcripts_relation(conn)

    assert relation.selection_mode == "lifecycle_filter"
    assert [row[0] for row in conn.execute(f"SELECT id FROM {relation} ORDER BY id")] == [1]


def test_selected_filing_sections_relation_filters_inactive_rows_after_0214() -> None:
    conn = _conn(lifecycle=True)
    conn.executemany("INSERT INTO filing_sections (id, is_active) VALUES (?, ?)", [(1, 1), (2, 0)])

    relation = selected_filing_sections_relation(conn)

    assert relation.selection_mode == "lifecycle_filter"
    assert [row[0] for row in conn.execute(f"SELECT id FROM {relation} ORDER BY id")] == [1]


def test_selected_relation_prefers_provisioned_active_view() -> None:
    conn = _conn(lifecycle=True)
    conn.execute(
        "CREATE VIEW v_active_transcripts AS SELECT * FROM transcripts WHERE is_active = 1"
    )

    relation = selected_transcripts_relation(conn)

    assert str(relation) == "v_active_transcripts"
    assert relation.selection_mode == "active_view"


def test_selected_filing_sections_relation_prefers_provisioned_active_view() -> None:
    conn = _conn(lifecycle=True)
    conn.execute(
        "CREATE VIEW v_active_filing_sections AS SELECT * FROM filing_sections WHERE is_active = 1"
    )

    relation = selected_filing_sections_relation(conn)

    assert str(relation) == "v_active_filing_sections"
    assert relation.selection_mode == "active_view"
