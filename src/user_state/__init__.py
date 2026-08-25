"""
src/user_state/ — row-CRUD for the user-owned Personal CIO substrate tables.

Layout:
    registry — user_kpi_registry: which KPIs load-bear the thesis per ticker.
    sizing   — position_sizing_intent: append-only history of target/max/note
               sizing declarations.
    ledger   — thesis_ledger_entries: append-only durable history of every
               accepted thesis update / bear append.
    notes    — analyst_notes: lifecycle-aware open questions and watch items,
               including alert-derived earnings follow-ups.

All three modules share ``_db.open_conn`` for connection setup; that helper
fails loudly when the DB or schema is missing rather than silently no-op'ing
(the user-state writes are primary, not telemetry).
"""

from __future__ import annotations

from user_state.ledger import ThesisLedgerEntryRow, append_entry, list_entries
from user_state.registry import (
    UserKpiRegistryRow,
    delete_kpi,
    get_thesis_breakers,
    list_kpis,
    upsert_kpi,
)
from user_state.sizing import (
    PositionSizingIntentRow,
    append_intent,
    latest_intent,
    list_intents,
)

__all__ = [
    "PositionSizingIntentRow",
    "ThesisLedgerEntryRow",
    "UserKpiRegistryRow",
    "append_entry",
    "append_intent",
    "delete_kpi",
    "get_thesis_breakers",
    "latest_intent",
    "list_entries",
    "list_intents",
    "list_kpis",
    "upsert_kpi",
]
