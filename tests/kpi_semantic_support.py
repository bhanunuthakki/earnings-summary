"""Test-only helpers for source-qualified KPI fixtures."""

from __future__ import annotations

import json
import sqlite3


def admit_all_kpi_facts(conn: sqlite3.Connection) -> None:
    """Attach a minimal valid semantic head to every unclassified KPI fixture row.

    Production readers deliberately fail closed. Tests whose subject is not
    semantic admission must therefore state that their synthetic facts are
    source-qualified instead of relying on the pre-0030 implicit contract.
    """
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "kpi_facts" not in tables or "kpi_definitions" not in tables:
        return
    if "kpi_fact_semantic_contexts" not in tables:
        conn.execute(
            "CREATE TABLE kpi_fact_semantic_contexts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,kpi_fact_id INTEGER NOT NULL,"
            "revision INTEGER NOT NULL DEFAULT 1,supersedes_context_id INTEGER,"
            "metric_name_as_reported TEXT NOT NULL,reported_period_end TEXT,"
            "period_role TEXT NOT NULL DEFAULT 'current',"
            "publication_lane TEXT NOT NULL,accounting_basis TEXT NOT NULL,"
            "consolidation_scope TEXT NOT NULL,dimensions_json TEXT NOT NULL,"
            "unit_scale TEXT NOT NULL,source_row_label TEXT,source_column_header TEXT,"
            "source_value_text TEXT,status TEXT NOT NULL,reason_code TEXT,"
            "reviewed_by TEXT NOT NULL DEFAULT 'test',knowledge_at TEXT NOT NULL "
            "DEFAULT '2026-08-27T00:00:00Z')"
        )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")}
    rows = conn.execute(
        "SELECT kf.id,kf.period_end,kf.unit,kf.value,kd.name "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id=kf.kpi_definition_id "
        "WHERE NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts ksc "
        "WHERE ksc.kpi_fact_id=kf.id) ORDER BY kf.id"
    ).fetchall()
    for fact_id, period_end, unit, value, name in rows:
        unit_text = str(unit or "actual").lower()
        unit_scale = unit_text if unit_text in {"thousands", "millions", "billions"} else "none"
        candidates: dict[str, object] = {
            "kpi_fact_id": fact_id,
            "revision": 1,
            "supersedes_context_id": None,
            "metric_name_as_reported": str(name),
            "reported_period_end": str(period_end)[:10],
            "period_role": "current",
            "publication_lane": "current_actual",
            "accounting_basis": "management",
            "consolidation_scope": "consolidated",
            "dimensions_json": json.dumps({}, separators=(",", ":")),
            "unit_scale": unit_scale,
            "source_row_label": str(name),
            "source_column_header": str(period_end)[:10],
            "source_value_text": str(value),
            "status": "admitted",
            "reason_code": None,
            "reviewed_by": "test-fixture",
            "knowledge_at": "2026-08-27T00:00:00Z",
        }
        fields = [field for field in candidates if field in columns]
        conn.execute(
            f"INSERT INTO kpi_fact_semantic_contexts ({','.join(fields)}) "
            f"VALUES ({','.join('?' for _ in fields)})",
            tuple(candidates[field] for field in fields),
        )
