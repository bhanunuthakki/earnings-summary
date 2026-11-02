"""Retire optional-input automation, personal coaching, and derived profile rows.

Explicit decisions, owner-confirmed process facts, financial sources, and exact
retained references remain authoritative. Restore a reviewed predecessor
snapshot to recover retired history.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy.engine import Connection

from alembic import op

revision = "0069_retire_obsolete_input_history"
down_revision = "0068_retire_personal_coaching"
branch_labels = None
depends_on = None

# These producers are retired. Company research, normal briefs, transcripts,
# source extraction, generic lens:* calls, and retained portfolio research are
# intentionally outside this set.
_RETIRED_CALL_PURPOSES = (
    "advisor_socratic_questions",
    "advisor_socratic_memo",
    "annual_letter",
    "artifact_brief",
    "coach_reply_intent",
    "behavior_distill",
    "capture_intent",
    "capture_triage",
    "etf_role_synthesis",
    "exit_postmortem_draft",
    "ledger_reply_intent",
    "musing_decision_extract",
    "pairwise_analysis",
    "position_review",
    "research_triage",
    "saydo_filter",
    "saydo_importance",
    "saydo_pair",
    "tenet_accountability",
    "tenet_distill",
    "tenet_semantic_tension",
    "theme_seed_cluster",
    "theme_synthesis",
    "triage_route_suggest",
    "weekly_packet_predraft",
)

_REQUIRED_COLUMNS = {
    "llm_calls": {"id", "purpose", "artifact_id"},
    "owner_profile_facts": {
        "id",
        "category",
        "provenance",
        "status",
        "superseded_by_id",
    },
    "ask_answer_audit_records": {"llm_call_id", "claim_audit_llm_call_id"},
    "ask_answer_groundings": {"llm_call_id"},
    "decision_drafts": {"llm_call_id", "draft_json"},
    "insights": {"llm_call_id"},
    "llm_artifacts": {"id", "llm_call_id", "content_json"},
    "analyst_notes": {"context_json"},
    "research_proposals": {"evidence_json", "artifact_json", "canonical_content_json"},
    "evidence_nodes": {"node_id", "text"},
}

_SCALAR_CALL_REFERENCES = {
    ("ask_answer_audit_records", "llm_call_id"),
    ("ask_answer_audit_records", "claim_audit_llm_call_id"),
    ("ask_answer_groundings", "llm_call_id"),
    ("decision_drafts", "llm_call_id"),
    ("insights", "llm_call_id"),
    ("llm_artifacts", "llm_call_id"),
}

_JSON_CALL_REFERENCES = {
    ("analyst_notes", "context_json"),
    ("decision_drafts", "draft_json"),
    ("research_proposals", "evidence_json"),
    ("research_proposals", "artifact_json"),
    ("research_proposals", "canonical_content_json"),
    ("llm_artifacts", "content_json"),
}
_JSON_CALL_KEYS = ("llm_call_id", "claim_audit_llm_call_id", "call_id")

_KNOWN_CALL_TRIGGERS = {
    "trg_ask_answer_audit_record_claim_llm": "167fd1732770340e5bf3140912321c93973e892c5f208a2ea80bcbd0658d8047",  # pragma: allowlist secret -- schema SQL digest, not a credential
    "trg_ask_answer_audit_record_llm": "9b8005bf6daa3eb52168d7e070d9b698266ea9aa9274c8d450db5cd671b5c3cc",  # pragma: allowlist secret -- schema SQL digest, not a credential
}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables(connection: Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.exec_driver_sql(f"PRAGMA table_info({_quote(table)})").fetchall()
    }


def _assert_schema(connection: Connection) -> None:
    tables = _tables(connection)
    missing = sorted(set(_REQUIRED_COLUMNS) - tables)
    if missing:
        raise RuntimeError(f"optional-input retirement is missing expected tables: {missing}")
    missing_columns = {
        table: sorted(columns - _columns(connection, table))
        for table, columns in _REQUIRED_COLUMNS.items()
        if columns - _columns(connection, table)
    }
    if missing_columns:
        raise RuntimeError(
            f"optional-input retirement is missing expected columns: {missing_columns}"
        )


def _candidate_call_ids(connection: Connection) -> list[int]:
    placeholders = ",".join("?" for _ in _RETIRED_CALL_PURPOSES)
    return [
        int(row[0])
        for row in connection.exec_driver_sql(
            f"SELECT id FROM llm_calls WHERE purpose IN ({placeholders}) ORDER BY id",
            _RETIRED_CALL_PURPOSES,
        ).fetchall()
    ]


def _assert_registered_json(connection: Connection) -> None:
    for table, column in sorted(_JSON_CALL_REFERENCES):
        invalid = connection.exec_driver_sql(
            f"SELECT 1 FROM {_quote(table)} WHERE {_quote(column)} IS NOT NULL "
            f"AND NOT json_valid({_quote(column)}) LIMIT 1"
        ).first()
        if invalid:
            raise RuntimeError(
                f"optional-input retirement found invalid registered JSON: {table}.{column}"
            )

    invalid_ir = connection.exec_driver_sql(
        "SELECT 1 FROM evidence_nodes WHERE node_id GLOB 'ir-pdf-response:*' "
        "AND NOT json_valid(text) LIMIT 1"
    ).first()
    if invalid_ir:
        raise RuntimeError("optional-input retirement found invalid governed IR response JSON")
    unknown_ir = connection.exec_driver_sql(
        "SELECT 1 FROM evidence_nodes WHERE node_id GLOB 'ir-pdf-response:*' "
        "AND json_valid(text) AND json_extract(text,'$.schema_version') "
        "IS NOT 'ir-pdf-governed-response.v2' LIMIT 1"
    ).first()
    if unknown_ir:
        raise RuntimeError("optional-input retirement found an unknown governed IR response shape")
    invalid_ir_call = connection.exec_driver_sql(
        "SELECT 1 FROM evidence_nodes WHERE node_id GLOB 'ir-pdf-response:*' "
        "AND json_type(text,'$.response.llm_call_id') IS NOT NULL "
        "AND json_type(text,'$.response.llm_call_id') NOT IN ('integer','text','null') LIMIT 1"
    ).first()
    if invalid_ir_call:
        raise RuntimeError("optional-input retirement found an invalid governed IR call reference")


def _call_reference_ids(connection: Connection, candidate_ids: list[int]) -> set[int]:
    if not candidate_ids:
        return set()
    candidate_slots = ",".join("?" for _ in candidate_ids)
    referenced: set[int] = set()

    # Exact scalar references are known independently of whether they have a
    # declared SQLite FK. Unknown populated FKs fail closed below.
    for table, column in sorted(_SCALAR_CALL_REFERENCES):
        rows = connection.exec_driver_sql(
            f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} IN ({candidate_slots})",
            tuple(candidate_ids),
        ).fetchall()
        referenced.update(int(row[0]) for row in rows)

    # These exact JSON owners are registered call-reference containers. Numeric
    # text and integral JSON reals can identify the same call. Use NUMERIC rather
    # than INTEGER so a fractional value cannot become an unrelated call ID.
    # Ambiguous numeric prefixes conservatively retain history.
    for table, column in sorted(_JSON_CALL_REFERENCES):
        placeholders = ",".join("?" for _ in _JSON_CALL_KEYS)
        rows = connection.exec_driver_sql(
            f"SELECT DISTINCT CAST(tree.atom AS NUMERIC) FROM {_quote(table)} AS owner, "
            f"json_tree(owner.{_quote(column)}) AS tree "
            f"WHERE owner.{_quote(column)} IS NOT NULL "
            f"AND tree.key IN ({placeholders}) AND tree.type IN ('integer','real','text') "
            f"AND CAST(tree.atom AS NUMERIC) IN ({candidate_slots})",
            (*_JSON_CALL_KEYS, *candidate_ids),
        ).fetchall()
        referenced.update(int(row[0]) for row in rows)

    ir_rows = connection.exec_driver_sql(
        "SELECT DISTINCT CAST(json_extract(text,'$.response.llm_call_id') AS NUMERIC) "
        "FROM evidence_nodes WHERE node_id GLOB 'ir-pdf-response:*' "
        "AND CAST(json_extract(text,'$.response.llm_call_id') AS NUMERIC) IN ("
        + candidate_slots
        + ")",
        tuple(candidate_ids),
    ).fetchall()
    referenced.update(int(row[0]) for row in ir_rows)

    # Scan every FK edge into llm_calls. The admitted edges are retained exact
    # references; any populated unknown edge stops the migration before writes.
    for source_table in sorted(_tables(connection)):
        for column in sorted(_columns(connection, source_table)):
            normalized = column.lower()
            if not ("llm_call_id" in normalized or normalized == "call_id"):
                continue
            if (source_table, column) in _SCALAR_CALL_REFERENCES:
                continue
            rows = connection.exec_driver_sql(
                f"SELECT DISTINCT {_quote(column)} FROM {_quote(source_table)} "
                f"WHERE {_quote(column)} IN ({candidate_slots})",
                tuple(candidate_ids),
            ).fetchall()
            if rows:
                raise RuntimeError(
                    "optional-input retirement found an unsupported populated call reference: "
                    f"{source_table}.{column}"
                )
        for edge in connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote(source_table)})"
        ).fetchall():
            target_table, source_column, target_column = str(edge[2]), str(edge[3]), str(edge[4])
            if target_table != "llm_calls":
                continue
            rows = connection.exec_driver_sql(
                f"SELECT DISTINCT {_quote(source_column)} FROM {_quote(source_table)} "
                f"WHERE {_quote(source_column)} IN ({candidate_slots})",
                tuple(candidate_ids),
            ).fetchall()
            values = {int(row[0]) for row in rows}
            if (source_table, source_column) in _SCALAR_CALL_REFERENCES and target_column == "id":
                referenced.update(values)
            elif values:
                raise RuntimeError(
                    "optional-input retirement found an unsupported populated call foreign key: "
                    f"{source_table}.{source_column}"
                )

    # llm_calls.artifact_id is a legacy scalar, not an FK. A call attached to
    # an extant artifact is linked history. A dangling scalar is not a dependency.
    rows = connection.exec_driver_sql(
        f"SELECT call.id FROM llm_calls call JOIN llm_artifacts artifact "
        f"ON artifact.id=call.artifact_id WHERE call.id IN ({candidate_slots})",
        tuple(candidate_ids),
    ).fetchall()
    referenced.update(int(row[0]) for row in rows)
    return referenced


def _assert_deletion_triggers(connection: Connection) -> None:
    for name, sql in connection.exec_driver_sql(
        "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND sql IS NOT NULL"
    ).fetchall():
        trigger_name, trigger_sql = str(name), str(sql)
        if not re.search(r"\b(?:llm_calls|owner_profile_facts)\b", trigger_sql, re.I):
            continue
        digest = hashlib.sha256(trigger_sql.encode()).hexdigest()
        if _KNOWN_CALL_TRIGGERS.get(trigger_name) == digest:
            continue
        raise RuntimeError(
            f"optional-input retirement found an unsupported trigger dependency: {trigger_name}"
        )


def _profile_candidates(connection: Connection) -> list[int]:
    rows = connection.exec_driver_sql(
        "SELECT id FROM owner_profile_facts WHERE category='capacity' OR "
        "(category IN ('appetite','behavioral') AND provenance='derived' "
        "AND status IN ('proposed','rejected')) "
        "ORDER BY id"
    ).fetchall()
    return [int(row[0]) for row in rows]


def _assert_profile_references(connection: Connection, candidate_ids: list[int]) -> None:
    if not candidate_ids:
        return
    placeholders = ",".join("?" for _ in candidate_ids)
    successor = connection.exec_driver_sql(
        "SELECT 1 FROM owner_profile_facts WHERE id NOT IN ("
        "SELECT id FROM owner_profile_facts WHERE category='capacity' OR "
        "(category IN ('appetite','behavioral') AND provenance='derived' "
        "AND status IN ('proposed','rejected'))) "
        f"AND superseded_by_id IN ({placeholders}) LIMIT 1",
        tuple(candidate_ids),
    ).first()
    if successor:
        raise RuntimeError(
            "optional-input retirement found a retained owner fact pointing to a retired fact"
        )

    for source_table in sorted(_tables(connection)):
        for column in sorted(_columns(connection, source_table)):
            normalized = column.lower()
            if "profile_fact_id" not in normalized and "owner_profile_fact_id" not in normalized:
                continue
            rows = connection.exec_driver_sql(
                f"SELECT 1 FROM {_quote(source_table)} WHERE {_quote(column)} "
                f"IN ({placeholders}) LIMIT 1",
                tuple(candidate_ids),
            ).first()
            if rows and source_table != "owner_profile_facts":
                raise RuntimeError(
                    "optional-input retirement found an unsupported owner-fact reference: "
                    f"{source_table}.{column}"
                )
        for edge in connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote(source_table)})"
        ).fetchall():
            if str(edge[2]) != "owner_profile_facts":
                continue
            source_column, target_column = str(edge[3]), str(edge[4])
            rows = connection.exec_driver_sql(
                f"SELECT 1 FROM {_quote(source_table)} WHERE {_quote(source_column)} "
                f"IN ({placeholders}) LIMIT 1",
                tuple(candidate_ids),
            ).first()
            if rows:
                raise RuntimeError(
                    "optional-input retirement found an external owner-fact dependency: "
                    f"{source_table}.{source_column}->{target_column}"
                )


def upgrade() -> None:
    connection = op.get_bind()
    _assert_schema(connection)
    _assert_registered_json(connection)
    _assert_deletion_triggers(connection)

    call_ids = _candidate_call_ids(connection)
    retained_call_ids = _call_reference_ids(connection, call_ids)
    profile_ids = _profile_candidates(connection)
    _assert_profile_references(connection, profile_ids)

    deletable_calls = sorted(set(call_ids) - retained_call_ids)
    if deletable_calls:
        placeholders = ",".join("?" for _ in deletable_calls)
        connection.exec_driver_sql(
            f"DELETE FROM llm_calls WHERE id IN ({placeholders})", tuple(deletable_calls)
        )
    if profile_ids:
        placeholders = ",".join("?" for _ in profile_ids)
        connection.exec_driver_sql(
            f"DELETE FROM owner_profile_facts WHERE id IN ({placeholders})", tuple(profile_ids)
        )


def downgrade() -> None:
    raise RuntimeError(
        "Restore a reviewed predecessor database snapshot to recover optional input and coaching history"
    )
