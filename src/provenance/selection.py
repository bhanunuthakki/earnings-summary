"""One compatibility boundary for current transcript and filing evidence.

The 0214 lifecycle migration retains superseded evidence.  Current-state
consumers therefore obtain their SQL relation here instead of independently
probing for ``is_active``.  Pre-0214 databases and small test fixtures retain
their former table semantics.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

_TRANSCRIPTS_TABLE = "transcripts"
_FILING_SECTIONS_TABLE = "filing_sections"
_ACTIVE_TRANSCRIPTS_VIEW = "v_active_transcripts"
_ACTIVE_FILING_SECTIONS_VIEW = "v_active_filing_sections"

SelectionMode = Literal["active_view", "lifecycle_filter", "legacy_table"]


@dataclass(frozen=True)
class SelectedRelation:
    """A constant-safe SQL relation and the compatibility path that chose it."""

    sql: str
    selection_mode: SelectionMode

    def __str__(self) -> str:
        return self.sql


def _has_view(conn: sqlite3.Connection, view_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?", (view_name,)
    ).fetchone()
    return row is not None


def _has_lifecycle_column(conn: sqlite3.Connection, table_name: str) -> bool:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(column[1]) == "is_active" for column in columns)


def _selected_relation(
    conn: sqlite3.Connection, *, table_name: str, active_view_name: str
) -> SelectedRelation:
    if _has_view(conn, active_view_name):
        relation = SelectedRelation(active_view_name, "active_view")
    elif _has_lifecycle_column(conn, table_name):
        relation = SelectedRelation(
            f"(SELECT * FROM {table_name} WHERE is_active = 1)", "lifecycle_filter"
        )
    else:
        relation = SelectedRelation(table_name, "legacy_table")
    log.info(
        "evidence_selection_relation",
        extra={"relation": table_name, "selection_mode": relation.selection_mode},
    )
    return relation


def selected_transcripts_relation(conn: sqlite3.Connection) -> SelectedRelation:
    """Return the relation containing only current transcript evidence."""
    return _selected_relation(
        conn, table_name=_TRANSCRIPTS_TABLE, active_view_name=_ACTIVE_TRANSCRIPTS_VIEW
    )


def selected_filing_sections_relation(conn: sqlite3.Connection) -> SelectedRelation:
    """Return the relation containing only current filing-section evidence."""
    return _selected_relation(
        conn, table_name=_FILING_SECTIONS_TABLE, active_view_name=_ACTIVE_FILING_SECTIONS_VIEW
    )
