"""Retire unlinked optional research and Telegram history.

Explicit decisions, current tenets, native document artifacts, stage receipts,
and their exact source lineage remain authoritative. Restore a reviewed
predecessor snapshot to recover retired optional history.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy.engine import Connection

from alembic import op

revision = "0065_retire_optional_research_history"
down_revision = "0064_retired_capture_coaching_stores"
branch_labels = None
depends_on = None

_KNOWN_ARTIFACT_TRIGGER_HASHES = {
    "trg_artifacts_dirty_on_commitment_insert": "ea18e04b7ddac05584652702727701e886aa8ddfc4274462f4eaaecdcc0a9e5d",
    "trg_artifacts_dirty_on_exec_comp_insert": "53e88186518483c9b209bd1fac51d0e11ae9630e7114a63e7d393cc61e85f527",
    "trg_artifacts_dirty_on_insider_insert": "4878defb5f2ed304b9f9cc2c1a42708935f6415cd8f34d3649ebbe083fac6dac",
    "trg_artifacts_dirty_on_segment_dimensions_insert": "77d1bbc3a903f46689debe814241bc975cb7005e5a14644d1655306b98954e7d",
    "trg_insights_dirty_on_prediction_grade": "cf7f3a630e2616b08f207862f114db21b758622541dbc9f38738e13a19417780",
    "trg_llm_artifact_document_immutable": "1bb5fab28d20d14196328e54ce683c00d7d8486ef5c511696547e097613b8c64",
    "trg_llm_artifact_document_shape_insert": "4033476c8a3b8a4df373c4bcf3001eeb48ca874ed3a1444058cd17732fd34392",
    "trg_llm_artifact_document_shape_update": "f11dcb33af91f3945f7cee210301ed48251f79639ed0e5ef98cb16f7e89022eb",
    "trg_llm_artifact_document_successor_restrict": "04ac3c13f86ac9989b60d003574adbbb3973f392428e752dd620e605f12b4a68",
    "trg_llm_artifact_document_supersession_exact": "68b51659e23134f192dbba6f13f6e292ca29188dce93cac73f07c7224407cbc8",
    "trg_pipeline_stage_document_receipt_immutable": "4573bcf0ad91051af346e45ec155043e9b7b8550940a453a7541311d9aa5b670",
    "trg_pipeline_stage_document_receipt_insert": "2f766bfb001b8e85f0ef58095aadfe8469b54e4146afd7549bdc306a42dae7e7",
    "trg_pipeline_stage_document_receipt_update": "44e9d66a7d3a43712e120f4c23fe5c569326239646b8201dc0bfc2e555f39d68",
}

_KNOWN_NOTE_TRIGGER_HASHES = {
    "analyst_notes_fts_ad": "76e47195a41523056df02db704685dd8bff7723c1438948d60508873d099bfc5",
    "analyst_notes_fts_ai": "ed2f027be8e246ca8299f83b840918496bde9587d398e82e4001908a81f04912",
    "analyst_notes_fts_au": "e26d309c3cdcbfec61051442b916d52dc17d8fd1c08744aeabb2d2c8f868e288",
}

_KNOWN_VIEW_HASHES = {
    "v_decision_journal": "b9ebbc6faaca5e184be295b6fa685dc4628e51b6f06849f8afa506c7abc02eb6",
}

_REQUIRED_COLUMNS = {
    "llm_artifacts": {
        "id",
        "scope",
        "purpose",
        "ticker",
        "generated_at",
        "superseded_by_id",
        "parent_artifact_ids",
        "document_version_id",
        "llm_call_id",
    },
    "pipeline_stage_transitions": {"artifact_id"},
    "decisions": {"source_artifact_id", "advice_artifact_id", "source_memo_id"},
    "predictions": {"source_kind", "source_artifact_id"},
    "prompt_calibration_scores": {"purpose", "artifact_id"},
    "llm_budgets": {"purpose"},
    "llm_budget_alerts": {"id", "purpose"},
    "model_eval_verdicts": {"id", "purpose"},
    "owner_profile_facts": {
        "id",
        "category",
        "provenance",
        "status",
        "is_latest",
        "superseded_by_id",
    },
    "decision_drafts": {
        "source_channel",
        "source_note_id",
        "decision_id",
        "draft_json",
    },
    "analyst_notes": {"id", "context_json", "body", "decision_id"},
    "raw_capture_sessions": {"id", "channel", "note_id"},
    "capture_audit_log": {"id", "channel", "session_id", "note_id"},
    "advisor_memos": {"id"},
    "stance_scores": {"memo_id"},
    "red_team_items": {"id"},
    "insight_notes": {"id", "kind", "status", "supersedes_id", "source_note_ids"},
}
_DELETED_ROW_TABLES = {
    "llm_artifacts",
    "prompt_calibration_scores",
    "llm_budgets",
    "llm_budget_alerts",
    "model_eval_verdicts",
    "owner_profile_facts",
    "predictions",
    "decision_drafts",
    "raw_capture_sessions",
    "capture_audit_log",
    "advisor_memos",
    "stance_scores",
    "red_team_items",
    "insight_notes",
}

_RETIRED_CONFIGURATION_PURPOSE_SQL = """
    'ledger_reply_intent','capture_triage','artifact_brief','capture_intent',
    'research_triage','triage_route_suggest','musing_decision_extract','behavior_distill'
"""
_RETIRED_PROFILE_RULE_SQL = (
    "category='behavioral' AND provenance='derived' AND status IN ('proposed','rejected')"
)
_RETIRED_NOTE_CONTEXT_KEYS = (
    "ledger_answer",
    "ledger_answer_pending",
    "engage_brief",
    "ladder",
    "engage_intent",
    "research_route",
    "research_route_why",
    "route_suggestion",
    "auto_routed",
    "route_parked",
)

_RETIRED_PURPOSE_SQL = f"""
    purpose GLOB 'lens:*'
    OR purpose IN (
      'investment_decision_card', 'post_earnings_readout', 'pre_earnings_brief',
      'filing_intelligence', 'earnings_tone_diff', 'qa_topics',
      'transcript_qa_judgment', 'valuation_basis', 'incremental_dollar_recommendation',
      'senior_partner_brief', 'thesis_collision', 'etf_role_synthesis',
      'kpi_inflection_context', 'saydo_pair', 'saydo_filter',
      'pairwise_analysis', 'saydo_importance',
      {_RETIRED_CONFIGURATION_PURPOSE_SQL}
    )
"""

_ARTIFACT_SETS_SQL = f"""
WITH RECURSIVE
artifact_candidates(id) AS (
  SELECT a.id FROM llm_artifacts AS a
  WHERE a.scope <> 'document' AND a.document_version_id IS NULL AND (
    a.superseded_by_id IS NOT NULL
    OR a.purpose IN ('saydo_pair','saydo_filter',{_RETIRED_CONFIGURATION_PURPOSE_SQL})
    OR (a.scope='etf' AND a.purpose='etf_role_synthesis')
    OR (a.scope IN ('ticker','portfolio') AND a.purpose GLOB 'lens:*')
    OR (a.scope='ticker' AND a.purpose IN (
      'investment_decision_card','post_earnings_readout','pre_earnings_brief',
      'filing_intelligence','earnings_tone_diff','qa_topics','transcript_qa_judgment',
      'valuation_basis','bear_case','kpi_inflection_context'
    ))
    OR (a.scope='portfolio' AND a.purpose IN (
      'incremental_dollar_recommendation','senior_partner_brief','thesis_collision'
    ))
  )
),
artifact_seed(id) AS (
  SELECT a.id FROM llm_artifacts AS a
  WHERE NOT EXISTS (SELECT 1 FROM artifact_candidates c WHERE c.id=a.id)
  UNION SELECT source_artifact_id FROM decisions WHERE source_artifact_id IS NOT NULL
  UNION SELECT advice_artifact_id FROM decisions WHERE advice_artifact_id IS NOT NULL
  UNION SELECT source_artifact_id FROM predictions
    WHERE source_artifact_id IS NOT NULL
      AND source_kind IN ('mgmt_commitment','risk_factor_10k','sell_side','analyst_consensus')
  UNION SELECT score.artifact_id FROM prompt_calibration_scores AS score
    WHERE score.artifact_id IS NOT NULL AND NOT ({_RETIRED_PURPOSE_SQL.replace("purpose", "score.purpose")})
  UNION SELECT CASE
    WHEN json_valid(d.draft_json)=0 THEN NULL
    WHEN json_type(d.draft_json, '$.linked_advice_artifact_id')='integer'
      THEN CAST(json_extract(d.draft_json, '$.linked_advice_artifact_id') AS INTEGER)
    ELSE NULL END
    FROM decision_drafts AS d
    WHERE d.decision_id IS NOT NULL OR d.source_channel<>'telegram' OR EXISTS (
      SELECT 1 FROM analyst_notes n JOIN decisions decision ON decision.id=n.decision_id
      WHERE n.id=d.source_note_id
    )
  UNION SELECT id FROM llm_artifacts
    WHERE purpose='saydo_due_context'
  UNION SELECT id FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY generated_at DESC,id DESC) AS rn
    FROM llm_artifacts
    WHERE purpose='bear_case' AND scope='ticker' AND superseded_by_id IS NULL
  ) WHERE rn=1
  UNION SELECT artifact_id FROM pipeline_stage_transitions WHERE artifact_id IS NOT NULL
),
artifact_dependency_edges(from_id,to_id) AS (
  SELECT id, superseded_by_id FROM llm_artifacts WHERE superseded_by_id IS NOT NULL
  UNION ALL
  SELECT child.id, CAST(parent.value AS INTEGER)
  FROM llm_artifacts AS child
  JOIN json_each(CASE
    WHEN child.parent_artifact_ids IS NULL THEN '[]'
    WHEN json_valid(child.parent_artifact_ids)=0 THEN '[]'
    WHEN json_type(child.parent_artifact_ids)='array' THEN child.parent_artifact_ids
    ELSE '[]' END) AS parent
  WHERE parent.type='integer'
),
protected_artifacts(id) AS (
  SELECT id FROM artifact_seed WHERE id IS NOT NULL
  UNION
  SELECT edge.to_id FROM protected_artifacts AS keep
  JOIN artifact_dependency_edges AS edge ON edge.from_id=keep.id
)
SELECT c.id FROM artifact_candidates AS c
WHERE NOT EXISTS (SELECT 1 FROM protected_artifacts AS p WHERE p.id=c.id)
ORDER BY c.id
"""


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(connection: Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.exec_driver_sql(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    }


def _assert_schema_and_dependencies(connection: Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise RuntimeError(
            f"optional-history migration is missing expected tables: {missing_tables}"
        )
    missing_columns = {
        table: sorted(columns - _columns(connection, table))
        for table, columns in _REQUIRED_COLUMNS.items()
        if columns - _columns(connection, table)
    }
    if missing_columns:
        raise RuntimeError(
            f"optional-history migration is missing expected columns: {missing_columns}"
        )

    known_fks = {
        ("llm_artifacts", "superseded_by_id"): ("llm_artifacts", "id", "NO ACTION", "SET NULL"),
        ("prompt_calibration_scores", "artifact_id"): (
            "llm_artifacts",
            "id",
            "NO ACTION",
            "NO ACTION",
        ),
        ("pipeline_stage_transitions", "artifact_id"): (
            "llm_artifacts",
            "id",
            "NO ACTION",
            "RESTRICT",
        ),
    }
    for source_table in sorted(tables):
        for row in connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote_identifier(source_table)})"
        ).fetchall():
            target_table, source_column = str(row[2]), str(row[3])
            target_column = str(row[4])
            on_update, on_delete = str(row[5]).upper(), str(row[6]).upper()
            expected = known_fks.get((source_table, source_column))
            if expected is not None and expected != (
                target_table,
                target_column,
                on_update,
                on_delete,
            ):
                raise RuntimeError(
                    "known foreign-key edge changed in optional-history migration: "
                    f"{source_table}.{source_column} references {target_table}.{target_column} "
                    f"({on_update}, {on_delete}); expected {expected}"
                )
            if target_table in _DELETED_ROW_TABLES and expected is None:
                raise RuntimeError(
                    "unexpected foreign-key dependency on optional history: "
                    f"{source_table}.{source_column} references {target_table}.{target_column} "
                    f"({on_update}, {on_delete})"
                )

    for row in connection.exec_driver_sql(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
    ).fetchall():
        name, sql = str(row[0]), str(row[2])
        has_history_reference = any(
            re.search(rf"\b{re.escape(table)}\b", sql, re.I)
            for table in _DELETED_ROW_TABLES | {"pipeline_stage_transitions"}
        )
        if has_history_reference:
            digest = hashlib.sha256(sql.encode()).hexdigest()
            expected_digest = _KNOWN_ARTIFACT_TRIGGER_HASHES.get(name)
            if expected_digest is not None and digest == expected_digest:
                continue
            raise RuntimeError(f"unreviewed trigger dependency on artifact history: {name!r}")

    for name, sql in connection.exec_driver_sql(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='analyst_notes'"
    ).fetchall():
        if (
            not isinstance(sql, str)
            or _KNOWN_NOTE_TRIGGER_HASHES.get(str(name)) != hashlib.sha256(sql.encode()).hexdigest()
        ):
            raise RuntimeError(f"unreviewed trigger dependency on note context: {name!r}")

    for row in connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"
    ).fetchall():
        name, sql = str(row[0]), str(row[1])
        has_history_reference = any(
            re.search(rf"\b{re.escape(table)}\b", sql, re.I)
            for table in _DELETED_ROW_TABLES | {"pipeline_stage_transitions"}
        )
        has_note_context_reference = (
            re.search(r"\banalyst_notes\b", sql, re.I) is not None
            and re.search(r"\bcontext_json\b", sql, re.I) is not None
        )
        if has_history_reference or has_note_context_reference:
            digest = hashlib.sha256(sql.encode()).hexdigest()
            if _KNOWN_VIEW_HASHES.get(name) != digest:
                raise RuntimeError(f"unreviewed view dependency on optional history: {name!r}")


def _reference_blockers(connection: Connection) -> dict[str, int]:
    checks = {
        "owner_profile_facts.retained_successor_is_retired": f"""
          WITH retired_rules(id) AS (
            SELECT id FROM owner_profile_facts WHERE {_RETIRED_PROFILE_RULE_SQL}
          )
          SELECT COUNT(*) FROM owner_profile_facts
          WHERE superseded_by_id IN (SELECT id FROM retired_rules)
            AND id NOT IN (SELECT id FROM retired_rules)
        """,
        "llm_artifacts.parent_artifact_ids_invalid_json_or_shape": """
          SELECT COUNT(*) FROM llm_artifacts WHERE parent_artifact_ids IS NOT NULL
          AND CASE WHEN json_valid(parent_artifact_ids)=0 THEN 1
            WHEN json_type(parent_artifact_ids)<>'array' THEN 1 ELSE 0 END=1
        """,
        "llm_artifacts.parent_artifact_ids_noninteger_or_unresolved": """
          SELECT COUNT(*) FROM llm_artifacts a
          JOIN json_each(CASE WHEN json_valid(a.parent_artifact_ids)
            AND json_type(a.parent_artifact_ids)='array' THEN a.parent_artifact_ids ELSE '[]' END) p
          WHERE a.parent_artifact_ids IS NOT NULL AND (p.type<>'integer' OR NOT EXISTS
            (SELECT 1 FROM llm_artifacts parent WHERE parent.id=CAST(p.value AS INTEGER)))
        """,
        "llm_artifacts.superseded_by_id_unresolved_or_self": """
          SELECT COUNT(*) FROM llm_artifacts a WHERE a.superseded_by_id IS NOT NULL
          AND (a.superseded_by_id=a.id OR NOT EXISTS
            (SELECT 1 FROM llm_artifacts successor WHERE successor.id=a.superseded_by_id))
        """,
        "decision_drafts.draft_json_invalid": """
          SELECT COUNT(*) FROM decision_drafts
          WHERE draft_json IS NOT NULL AND NOT json_valid(draft_json)
        """,
        "decision_drafts.linked_advice_artifact_id_invalid_or_unresolved": """
          SELECT COUNT(*) FROM decision_drafts d
          WHERE (d.decision_id IS NOT NULL OR d.source_channel<>'telegram' OR EXISTS (
              SELECT 1 FROM analyst_notes n JOIN decisions decision ON decision.id=n.decision_id
              WHERE n.id=d.source_note_id)) AND CASE WHEN json_valid(d.draft_json)=0 THEN 0
            WHEN json_type(d.draft_json, '$.linked_advice_artifact_id') IS NULL THEN 0
            WHEN json_type(d.draft_json, '$.linked_advice_artifact_id')='null' THEN 0
            WHEN json_type(d.draft_json, '$.linked_advice_artifact_id')<>'integer' THEN 1
            WHEN NOT EXISTS (SELECT 1 FROM llm_artifacts a
              WHERE a.id=CAST(json_extract(d.draft_json, '$.linked_advice_artifact_id') AS INTEGER)) THEN 1
            ELSE 0 END=1
        """,
        "insight_notes.source_note_ids_invalid_json_or_shape": """
          SELECT COUNT(*) FROM insight_notes WHERE source_note_ids IS NOT NULL
          AND CASE WHEN json_valid(source_note_ids)=0 THEN 1
            WHEN json_type(source_note_ids)<>'array' THEN 1 ELSE 0 END=1
        """,
        "insight_notes.source_note_ids_noninteger_or_unresolved": """
          SELECT COUNT(*) FROM insight_notes i
          JOIN json_each(CASE WHEN json_valid(i.source_note_ids)
            AND json_type(i.source_note_ids)='array' THEN i.source_note_ids ELSE '[]' END) n
          WHERE i.source_note_ids IS NOT NULL AND (n.type<>'integer' OR NOT EXISTS
            (SELECT 1 FROM analyst_notes a WHERE a.id=CAST(n.value AS INTEGER)))
        """,
        "insight_notes.supersedes_id_unresolved_or_self": """
          SELECT COUNT(*) FROM insight_notes i WHERE i.supersedes_id IS NOT NULL
          AND (i.supersedes_id=i.id OR NOT EXISTS
            (SELECT 1 FROM insight_notes prior WHERE prior.id=i.supersedes_id))
        """,
        "decisions.artifact_or_memo_reference_unresolved": """
          SELECT COUNT(*) FROM decisions d WHERE
            (d.source_artifact_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM llm_artifacts a WHERE a.id=d.source_artifact_id)) OR
            (d.advice_artifact_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM llm_artifacts a WHERE a.id=d.advice_artifact_id)) OR
            (d.source_memo_id IS NOT NULL AND NOT EXISTS
              (SELECT 1 FROM advisor_memos m WHERE m.id=d.source_memo_id))
        """,
        "predictions.source_artifact_id_unresolved": """
          SELECT COUNT(*) FROM predictions p WHERE p.source_artifact_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM llm_artifacts a WHERE a.id=p.source_artifact_id)
        """,
        "predictions.source_kind_unknown": """
          SELECT COUNT(*) FROM predictions WHERE source_kind NOT IN
            ('mgmt_commitment','llm_bear_case','risk_factor_10k','sell_side','analyst_consensus')
        """,
        "prompt_calibration_scores.artifact_id_unresolved": """
          SELECT COUNT(*) FROM prompt_calibration_scores p WHERE p.artifact_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM llm_artifacts a WHERE a.id=p.artifact_id)
        """,
    }
    return {name: int(connection.exec_driver_sql(sql).scalar_one()) for name, sql in checks.items()}


def _retire_rows(connection: Connection) -> None:
    connection.exec_driver_sql(
        "DELETE FROM decision_drafts WHERE source_channel='telegram' "
        "AND decision_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM analyst_notes n JOIN decisions decision ON decision.id=n.decision_id "
        "WHERE n.id=decision_drafts.source_note_id)"
    )
    connection.exec_driver_sql(
        "DELETE FROM capture_audit_log WHERE channel='telegram' "
        "AND NOT EXISTS (SELECT 1 FROM raw_capture_sessions s "
        "JOIN analyst_notes n ON n.id=s.note_id JOIN decisions decision ON decision.id=n.decision_id "
        "WHERE s.id=capture_audit_log.session_id) "
        "AND NOT EXISTS (SELECT 1 FROM analyst_notes n "
        "JOIN decisions decision ON decision.id=n.decision_id "
        "WHERE n.id=capture_audit_log.note_id)"
    )
    connection.exec_driver_sql(
        "DELETE FROM raw_capture_sessions WHERE channel='telegram' "
        "AND NOT EXISTS (SELECT 1 FROM analyst_notes n "
        "JOIN decisions decision ON decision.id=n.decision_id "
        "WHERE n.id=raw_capture_sessions.note_id)"
    )
    connection.exec_driver_sql(
        "DELETE FROM prompt_calibration_scores WHERE " + _RETIRED_PURPOSE_SQL
    )
    connection.exec_driver_sql(
        "DELETE FROM predictions WHERE source_kind='llm_bear_case' "
        "AND (source_artifact_id IS NULL OR source_artifact_id IN (" + _ARTIFACT_SETS_SQL + "))"
    )
    connection.exec_driver_sql(
        "DELETE FROM stance_scores WHERE memo_id NOT IN ("
        "SELECT source_memo_id FROM decisions WHERE source_memo_id IS NOT NULL)"
    )
    connection.exec_driver_sql(
        "DELETE FROM advisor_memos WHERE id NOT IN ("
        "SELECT source_memo_id FROM decisions WHERE source_memo_id IS NOT NULL)"
    )
    connection.exec_driver_sql("DELETE FROM red_team_items")
    connection.exec_driver_sql(
        "DELETE FROM insight_notes WHERE id NOT IN ("
        "WITH RECURSIVE protected_insights(id) AS ("
        "SELECT id FROM insight_notes WHERE status='current' AND kind='tenet' "
        "UNION SELECT parent.id FROM protected_insights p "
        "JOIN insight_notes child ON child.id=p.id "
        "JOIN insight_notes parent ON parent.id=child.supersedes_id) "
        "SELECT id FROM protected_insights)"
    )

    connection.exec_driver_sql("DELETE FROM llm_artifacts WHERE id IN (" + _ARTIFACT_SETS_SQL + ")")

    # Remove only unconfirmed machine coaching. Never rewind a retained version
    # or change the current/affirmed facts used by owner-context readers.
    connection.exec_driver_sql("DELETE FROM owner_profile_facts WHERE " + _RETIRED_PROFILE_RULE_SQL)
    for table in ("llm_budget_alerts", "llm_budgets", "model_eval_verdicts"):
        connection.exec_driver_sql(
            f"DELETE FROM {table} WHERE purpose IN ({_RETIRED_CONFIGURATION_PURPOSE_SQL})"
        )
    paths = ",".join(f"'$.{key}'" for key in _RETIRED_NOTE_CONTEXT_KEYS)
    present = " OR ".join(
        f"json_type(context_json,'$.{key}') IS NOT NULL" for key in _RETIRED_NOTE_CONTEXT_KEYS
    )
    connection.exec_driver_sql(
        f"UPDATE analyst_notes SET context_json=json_remove(context_json,{paths}) "
        f"WHERE CASE WHEN json_valid(context_json) THEN {present} ELSE 0 END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_schema_and_dependencies(bind)
    blockers = {name: count for name, count in _reference_blockers(bind).items() if count}
    if blockers:
        raise RuntimeError(f"optional-history migration reference blockers: {blockers}")
    _retire_rows(bind)


def downgrade() -> None:
    raise RuntimeError(
        "Restore a reviewed predecessor database snapshot to recover retired optional research history"
    )
