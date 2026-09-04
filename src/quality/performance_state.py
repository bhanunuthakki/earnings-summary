"""Logical SQLite state fingerprints for deterministic performance fixtures."""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from typing import cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def database_state_sha256(database: Path) -> str:
    """Hash logical SQLite state, normalizing only route-event timestamps."""

    nonsemantic_timestamps: dict[str, frozenset[str]] = {
        "analyst_notes": frozenset({"created_at", "updated_at", "resolved_at"}),
        "ask_answer_groundings": frozenset({"recorded_at"}),
        "ask_exchange_artifacts": frozenset({"created_at"}),
        "ask_exchanges": frozenset({"created_at", "updated_at", "completed_at", "failed_at"}),
        "ask_grounding_traces": frozenset({"created_at"}),
        "ask_retrieval_trace_items": frozenset({"recorded_at"}),
        "ask_retrieval_traces": frozenset({"created_at"}),
        "ask_session_contexts": frozenset({"created_at", "updated_at"}),
        "ask_sessions": frozenset({"created_at", "updated_at", "distilled_at"}),
        "ask_turns": frozenset({"created_at"}),
        "capture_audit_log": frozenset({"created_at"}),
        "decisions": frozenset({"created_at", "user_acted_at", "outcome_at"}),
        "insight_notes": frozenset({"created_at", "updated_at", "as_of"}),
        "owner_profile_facts": frozenset({"created_at", "affirmed_at", "superseded_at"}),
        "raw_capture_sessions": frozenset({"created_at", "updated_at", "distilled_at"}),
        "research_proposal_decision_receipts": frozenset({"created_at"}),
        "research_proposals": frozenset(
            {"created_at", "updated_at", "actionable_at", "invalidated_at"}
        ),
        "research_tasks": frozenset({"created_at", "updated_at"}),
        "thesis_ledger_entries": frozenset({"created_at", "accepted_at"}),
    }
    nonsemantic_json_keys: dict[str, frozenset[str]] = {
        "analyst_notes": frozenset({"reconciled_at", "closed_at"}),
    }

    def normalize_json(value: object, keys: frozenset[str]) -> object:
        if isinstance(value, dict):
            mapping = cast("dict[object, object]", value)
            return {
                str(k): ("<route-event-timestamp>" if str(k) in keys else normalize_json(v, keys))
                for k, v in mapping.items()
            }
        if isinstance(value, list):
            items = cast("list[object]", value)
            return [normalize_json(item, keys) for item in items]
        return value

    with connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY) as connection:
        objects = connection.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        state: list[object] = []
        for name, kind, sql in objects:
            if kind != "table":
                state.append((kind, str(name), str(sql or "")))
                continue
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
            order_by = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY {order_by}').fetchall()
            normalized_rows: list[list[object]] = []
            for row in rows:
                values: list[object] = []
                for column, value in zip(columns, row, strict=True):
                    if column.endswith("_json") and isinstance(value, str):
                        with contextlib.suppress(TypeError, ValueError):
                            value = normalize_json(
                                json.loads(value),
                                nonsemantic_json_keys.get(str(name), frozenset()),
                            )
                    if column in nonsemantic_timestamps.get(str(name), frozenset()):
                        value = "<route-event-timestamp>"
                    values.append(value)
                normalized_rows.append(values)
            state.append((kind, str(name), tuple(columns), normalized_rows))
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
