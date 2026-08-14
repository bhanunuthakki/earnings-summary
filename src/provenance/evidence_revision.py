"""Cheap canonical revision coordinates for Ask evidence caches.

The token deliberately follows governed fact-observation capture and the
current transcript selection view. It does not read legacy fact tables and it
does not include chat, trace, or LLM ledger tables, so unrelated interaction
writes cannot evict a valid retrieval.
"""

from __future__ import annotations

import sqlite3


def legacy_fact_append_revision(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return O(1)-sized compatibility coordinates for legacy fact appends."""

    row = conn.execute(
        "SELECT "
        "COALESCE(MAX(CASE WHEN name='financial_facts' THEN seq END),0),"
        "COALESCE(MAX(CASE WHEN name='kpi_facts' THEN seq END),0) "
        "FROM sqlite_sequence"
    ).fetchone()
    if row is None:
        raise RuntimeError("legacy fact append revision query returned no row")
    return int(row[0]), int(row[1])


def current_evidence_revision(conn: sqlite3.Connection) -> tuple[object, ...]:
    """Return bounded coordinates for every database-backed Ask evidence lane.

    The fact-link rowid and legacy compatibility sequences are intentionally
    process-cache coordinates, not persisted identities: append, delete,
    restore, or VACUUM changes the token and conservatively invalidates the
    memo without scanning either fact table.
    """

    row = conn.execute(
        "SELECT "
        "(SELECT MAX(rowid) FROM fact_observation_revisions),"
        "(SELECT MAX(id) FROM documents),"
        "(SELECT MAX(id) FROM v_active_transcripts),"
        "(SELECT MAX(id) FROM position_sizing_intent),"
        "(SELECT MAX(updated_at) FROM position_sizing_intent),"
        "(SELECT MAX(id) FROM decisions),"
        "(SELECT MAX(COALESCE(outcome_at,user_acted_at,made_at,created_at)) FROM decisions),"
        "(SELECT MAX(id) FROM analyst_notes),"
        "(SELECT MAX(updated_at) FROM analyst_notes),"
        "(SELECT MAX(id) FROM disclosure_events),"
        "(SELECT MAX(id) FROM macro_series),"
        "(SELECT MAX(id) FROM macro_sensitivities),"
        "(SELECT MAX(id) FROM insight_notes),"
        "(SELECT MAX(id) FROM dcf_runs),"
        "(SELECT MAX(id) FROM llm_artifacts),"
        "(SELECT MAX(id) FROM tracked_companies),"
        "(SELECT MAX(id) FROM fact_overrides),"
        "(SELECT MAX(id) FROM validation_issues),"
        "(SELECT MAX(id) FROM kpi_definitions)"
    ).fetchone()
    if row is None:
        raise RuntimeError("current evidence revision query returned no row")
    return (*legacy_fact_append_revision(conn), *tuple(row))


__all__ = ["current_evidence_revision", "legacy_fact_append_revision"]
