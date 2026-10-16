"""Immutable, non-admitting projections of exact legacy KPI disposition targets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from pipeline.kpi_semantics import KpiSemanticStatus, current_kpi_semantic_context
from provenance.financial_fact_resolution import canonical_fact_relation


class LegacyKpiDispositionCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: dict[str, JsonValue]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _commitment(self) -> LegacyKpiDispositionCapture:
        if projection_sha256(self.payload) != self.payload_sha256:
            raise ValueError("legacy disposition projection commitment mismatch")
        return self


def projection_json(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def projection_sha256(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(projection_json(payload).encode("utf-8")).hexdigest()


def _row_payload(cursor: sqlite3.Cursor) -> dict[str, JsonValue] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = cursor.description
    if columns is None:
        raise ValueError("legacy projection column inventory is absent")
    # Keep SQLite types and every persisted column. Binary columns retain exact bytes.
    return {
        str(column[0]): {"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value
        for column, value in zip(columns, row, strict=True)
    }


def read_legacy_kpi_disposition_capture(
    conn: sqlite3.Connection, *, fact_id: int, require_head: bool = True
) -> LegacyKpiDispositionCapture:
    """Read a full raw projection without adding an observation or admission."""
    fact = _row_payload(conn.execute("SELECT * FROM kpi_facts WHERE id=?", (fact_id,)))
    if fact is None:
        raise ValueError("legacy disposition fact is absent")
    if (
        require_head
        and conn.execute(
            "SELECT 1 FROM kpi_facts WHERE supersedes_id=? LIMIT 1", (fact_id,)
        ).fetchone()
        is not None
    ):
        raise ValueError("legacy disposition target is no longer the fact-chain head")
    definition = _row_payload(
        conn.execute("SELECT * FROM kpi_definitions WHERE id=?", (fact["kpi_definition_id"],))
    )
    if definition is None:
        raise ValueError("legacy disposition definition is absent")
    document = _row_payload(
        conn.execute("SELECT * FROM documents WHERE id=?", (fact["source_doc_id"],))
    )
    if fact["source_doc_id"] is not None and document is None:
        raise ValueError("legacy disposition source identity is absent")
    payload: dict[str, JsonValue] = {
        "capture_kind": "unadmitted_legacy_kpi_projection.v1",
        "fact": fact,
        "definition": definition,
        "document": document,
    }
    return LegacyKpiDispositionCapture(payload=payload, payload_sha256=projection_sha256(payload))


def captured_legacy_quarantine_matches(
    conn: sqlite3.Connection, *, fact_id: int, user_id: str, require_head: bool = True
) -> bool:
    """Permit correction only while the sealed non-admitting quarantine is current."""
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kpi_legacy_disposition_captures'"
        ).fetchone()
        is None
    ):
        return False
    current = current_kpi_semantic_context(conn, kpi_fact_id=fact_id)
    if current is None or current.context.status is not KpiSemanticStatus.QUARANTINED:
        return False
    if current.kpi_definition_revision_id is not None:
        return False
    relation = canonical_fact_relation(conn, "kpi_facts")
    if (
        relation.selection_mode != "resolved_view"
        or conn.execute(
            f"SELECT 1 FROM {relation.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
            (fact_id,),
        ).fetchone()
        is not None
    ):
        return False
    row = conn.execute(
        "SELECT payload_json,payload_sha256 FROM kpi_legacy_disposition_captures "
        "WHERE kpi_fact_id=? AND quarantine_context_id=? AND user_id=?",
        (fact_id, current.id, user_id),
    ).fetchone()
    if row is None:
        return False
    captured = LegacyKpiDispositionCapture(
        payload=cast(dict[str, JsonValue], json.loads(str(row[0]))), payload_sha256=str(row[1])
    )
    try:
        actual = read_legacy_kpi_disposition_capture(
            conn, fact_id=fact_id, require_head=require_head
        )
    except ValueError:
        return False
    return actual.payload_sha256 == captured.payload_sha256
