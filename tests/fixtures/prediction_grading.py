"""Synthetic, revision-bound prediction evidence; no live state or acquisition."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from pipeline.kpi_definition_revisions import persist_kpi_definition_revision
from pipeline.kpi_semantics import persist_kpi_semantic_context
from tests.fixtures.kpi_revision_setup import (
    NOW,
    definition_fixture,
    revision_database,
    semantic_fixture,
)

PREDICTIONS_SCHEMA = """
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_kind TEXT NOT NULL,
    source_doc_id INTEGER, source_artifact_id INTEGER, source_excerpt TEXT,
    made_at TEXT NOT NULL, target_period TEXT, prediction_md TEXT NOT NULL,
    kpi_name TEXT, kpi_concept_id INTEGER, comparator TEXT,
    target_value REAL, target_unit TEXT,
    realized_value REAL, realized_doc_id INTEGER,
    outcome TEXT NOT NULL DEFAULT 'pending', outcome_confidence REAL,
    evaluated_at TEXT, evaluator_run_id TEXT, notes TEXT, created_at TEXT
);
"""


def prediction_database(path: Path) -> sqlite3.Connection:
    conn = revision_database(path)
    conn.executescript(PREDICTIONS_SCHEMA)
    conn.execute("DELETE FROM kpi_definitions")
    conn.execute(
        "UPDATE reporting_entities SET reporting_entity_id='entity-amat', issuer_id='issuer-amat'"
    )
    conn.execute("UPDATE evidence_document_versions SET issuer_id='issuer-amat'")
    return conn


def admit_prediction_facts(conn: sqlite3.Connection) -> None:
    """Bind existing synthetic percentage facts through actual definition/context writers."""
    conn.row_factory = sqlite3.Row
    clock = NOW.isoformat()
    for row in conn.execute("SELECT id,name FROM kpi_definitions").fetchall():
        definition_id = int(row["id"])
        name = str(row["name"])
        definition = persist_kpi_definition_revision(
            conn,
            definition_fixture(
                kpi_definition_id=definition_id,
                kpi_definition_revision_id=f"definition-{definition_id}",
                idempotency_key=f"definition-{definition_id}",
                reporting_entity_id="entity-amat",
                reported_label=name,
                unit_family="percentage",
                unit_key="percent",
                currency_disposition="not_applicable",
                currency=None,
            ),
        )
        for fact in conn.execute(
            "SELECT * FROM kpi_facts WHERE kpi_definition_id=?", (definition_id,)
        ).fetchall():
            fact_id = int(fact["id"])
            conn.execute(
                "INSERT OR IGNORE INTO evidence_document_versions VALUES (?, 'observation-1', 'issuer-amat', ?, 1, ?)",
                (f"fact-document-{fact_id}", fact["source_doc_id"], clock),
            )
            period = str(fact["period_end"])
            observation = f"observation-{fact_id}"
            resolution = f"resolution-{fact_id}"
            key = f"logical-{fact_id}"
            conn.execute("UPDATE kpi_facts SET locator=? WHERE id=?", ('{"pdf_page":7}', fact_id))
            conn.execute(
                "INSERT INTO reported_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    observation,
                    "AMAT",
                    f"kpi_definition:{definition_id}",
                    period,
                    "quarter",
                    str(fact["value"]),
                    None,
                    "percent",
                    clock,
                    clock,
                ),
            )
            conn.execute(
                "INSERT INTO fact_observation_revisions VALUES ('kpi_facts',?,1,?,?,?,?,?)",
                (fact_id, observation, key, fact["source_doc_id"], '{"pdf_page":7}', clock),
            )
            conn.execute(
                "INSERT INTO observation_resolution_revisions VALUES (?,?,1,?,?,?,?)",
                (resolution, key, observation, clock, clock, clock),
            )
            conn.execute(
                "INSERT INTO fact_resolution_outcomes VALUES (?,'resolved',?)", (resolution, clock)
            )
            context = semantic_fixture(name).model_copy(
                update={"reported_period_end": date.fromisoformat(period)}
            )
            persist_kpi_semantic_context(
                conn,
                kpi_fact_id=fact_id,
                context=context,
                reviewed_by="owner",
                knowledge_at=NOW,
                kpi_definition_revision_id=definition.kpi_definition_revision_id,
            )
