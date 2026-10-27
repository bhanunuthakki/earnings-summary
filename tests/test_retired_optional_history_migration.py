from __future__ import annotations

import importlib.util
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "0064_retired_capture_coaching_stores"


def _migration() -> ModuleType:
    path = ROOT / "alembic/versions/0065_retire_optional_research_history.py"
    spec = importlib.util.spec_from_file_location("retire_optional_research_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(database: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            _migration().upgrade()
    finally:
        engine.dispose()


def _artifact(
    connection: sqlite3.Connection,
    purpose: str,
    *,
    scope: str = "ticker",
    ticker: str = "SYNTH",
    parent_ids: str | None = None,
    superseded_by_id: int | None = None,
    document_version_id: str | None = None,
    issuer_id: str | None = None,
    content_json: str | None = None,
) -> int:
    connection.execute(
        "INSERT INTO llm_artifacts "
        "(ticker,scope,purpose,content_json,input_sha256,output_sha256,model,"
        "prompt_version,generated_at,superseded_by_id,parent_artifact_ids,"
        "document_version_id,issuer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ticker,
            scope,
            purpose,
            content_json,
            "1" * 64,
            "2" * 64,
            "test-model",
            "test-v1",
            "2026-10-26T12:00:00+00:00",
            superseded_by_id,
            parent_ids,
            document_version_id,
            issuer_id,
        ),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _seed_document(connection: sqlite3.Connection) -> int:
    now = "2026-10-26T12:00:00+00:00"
    raw_sha = "3" * 64
    connection.execute(
        "INSERT INTO issuer_entities "
        "(issuer_id,idempotency_key,entity_kind,created_at) VALUES "
        "('issuer-synthetic','issuer-synthetic-key','operating_company',?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO evidence_content_blobs "
        "(sha256,byte_size,media_type,storage_uri,recorded_at) "
        "VALUES (?,1,'text/plain','synthetic://transcript',?)",
        (raw_sha, now),
    )
    connection.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,observed_at,"
        "retrieved_at,retrieval_config_sha256,collector_code_version) "
        "VALUES ('observation-transcript','observation-transcript-key','issuer_publisher',"
        "'https://synthetic.invalid/transcript',?,?,?,?,'test-collector@1')",
        (raw_sha, now, now, "4" * 64),
    )
    connection.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,blob_sha256,"
        "issuer_id,ticker,document_type,form_type,period_start,period_end,language,recorded_at) "
        "VALUES ('document-transcript','SYNTH:transcript:2026Q1',1,'observation-transcript',"
        "?,'issuer-synthetic','SYNTH','transcript','earnings_call_transcript',"
        "'2026-01-01','2026-03-31','en',?)",
        (raw_sha, now),
    )
    body = {
        "schema_version": "document-product-envelope@1",
        "key": {
            "issuer_id": "issuer-synthetic",
            "document_version_id": "document-transcript",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "period_type": "Q1",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "product": "transcript_summary",
        },
        "source": {"locator": {"page": 1}},
    }
    connection.execute(
        "INSERT INTO llm_artifacts "
        "(ticker,scope,purpose,content_json,input_sha256,output_sha256,model,prompt_version,"
        "generated_at,document_version_id,issuer_id,document_period_start,document_period_end,"
        "document_period_type,document_fiscal_year,document_fiscal_quarter) "
        "VALUES ('SYNTH','document','transcript_summary',?,?,?,'test-model','test-v1',"
        "'2026-10-26T12:00:00+00:00','document-transcript','issuer-synthetic',"
        "'2026-01-01','2026-03-31','Q1',2026,1)",
        (json.dumps(body), "1" * 64, "2" * 64),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _seed_pipeline_stage(connection: sqlite3.Connection, artifact_id: int) -> None:
    connection.execute(
        "INSERT INTO pipeline_runs (pipeline_key,directive,ticker_scope,first_started_at) "
        "VALUES ('document-test','test','SYNTH','2026-10-26T12:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO pipeline_attempts (attempt_id,pipeline_key,started_at,status) "
        "VALUES ('document-attempt','document-test','2026-10-26T12:00:00+00:00','in_progress')"
    )
    receipt = '{"schema_version":"stage-receipt@1"}'
    connection.execute(
        "INSERT INTO pipeline_stage_transitions "
        "(attempt_id,ticker,stage,status,started_at,artifact_id,receipt_json,receipt_sha256) "
        "VALUES ('document-attempt','SYNTH','EXTRACT','ok','2026-10-26T12:00:00+00:00',?,?,?)",
        (artifact_id, receipt, "5" * 64),
    )


def test_upgrade_retires_only_unlinked_optional_history_and_keeps_document_lineage(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        candidate = _artifact(connection, "lens:legacy")
        draft_only_candidate = _artifact(connection, "lens:draft-only")
        prediction_source = _artifact(connection, "lens:prediction-source")
        retained_generic = _artifact(connection, "company_description")
        candidate_parent = _artifact(connection, "lens:parent")
        retained_child = _artifact(
            connection,
            "company_description",
            ticker="CHILD",
            parent_ids=json.dumps([candidate_parent]),
        )
        successor_candidate = _artifact(connection, "lens:retired-successor")
        saydo = _artifact(connection, "saydo_due_context", superseded_by_id=successor_candidate)
        linked_candidate = _artifact(connection, "bear_case")
        document = _seed_document(connection)
        _seed_pipeline_stage(connection, document)

        connection.execute(
            "INSERT INTO decisions "
            "(ticker,recommendation_kind,recommendation_value,conviction,source_artifact_id,"
            "made_at,created_at,decided_by,scope,advice_artifact_id) "
            "VALUES ('SYNTH','position',1.0,'high',?,'2026-01-01','2026-01-01','owner',"
            "'ticker',?)",
            (linked_candidate, retained_generic),
        )
        decision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO predictions "
            "(ticker,source_kind,source_artifact_id,made_at,prediction_md,created_at) "
            "VALUES ('SYNTH','llm_bear_case',?,'2026-01-01','synthetic prediction','2026-01-01')",
            (linked_candidate,),
        )
        connection.execute(
            "INSERT INTO predictions "
            "(ticker,source_kind,source_artifact_id,made_at,prediction_md,created_at) "
            "VALUES ('SYNTH','analyst_consensus',?,'2026-01-01','retained prediction','2026-01-01')",
            (prediction_source,),
        )
        connection.execute(
            "INSERT INTO prompt_calibration_scores "
            "(purpose,prompt_version,ticker,score,reason,artifact_id) "
            "VALUES ('lens:legacy','v1','SYNTH',0.2,'retired',?)",
            (candidate,),
        )
        connection.execute(
            "INSERT INTO prompt_calibration_scores "
            "(purpose,prompt_version,ticker,score,reason,artifact_id) "
            "VALUES ('company_description','v1','SYNTH',0.8,'retained',?)",
            (retained_generic,),
        )

        connection.execute(
            "INSERT INTO advisor_memos (kind,title,body_md,created_at) "
            "VALUES ('socratic','unlinked','optional','2026-01-01')"
        )
        unlinked_memo = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO advisor_memos (kind,title,body_md,created_at) "
            "VALUES ('position_review','linked','decision lineage','2026-01-01')"
        )
        linked_memo = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "UPDATE decisions SET source_memo_id=? WHERE source_artifact_id=?",
            (linked_memo, linked_candidate),
        )
        connection.execute(
            "INSERT INTO stance_scores (memo_id,verdict,horizon_days,created_at) "
            "VALUES (?,'correct',90,'2026-01-01'),(?,'wrong',90,'2026-01-01')",
            (unlinked_memo, linked_memo),
        )

        note_cursor = connection.execute(
            "INSERT INTO analyst_notes "
            "(kind,status,body,source,created_at,updated_at) "
            "VALUES ('decision','open','manual source note','manual','2026-01-01','2026-01-01')"
        )
        assert note_cursor.lastrowid is not None
        note_id = int(note_cursor.lastrowid)
        decision_note_cursor = connection.execute(
            "INSERT INTO analyst_notes "
            "(kind,status,body,source,created_at,updated_at,decision_id) "
            "VALUES ('decision','open','confirmed decision source','manual',"
            "'2026-01-01','2026-01-01',?)",
            (decision_id,),
        )
        assert decision_note_cursor.lastrowid is not None
        decision_note_id = int(decision_note_cursor.lastrowid)
        connection.execute(
            "UPDATE advisor_memos SET note_id=? WHERE id=?", (note_id, unlinked_memo)
        )
        connection.execute(
            "INSERT INTO insight_notes "
            "(scope_key,kind,body_md,source_note_ids,as_of,status,provenance,created_at,updated_at) "
            "VALUES ('SYNTH','tenet','current tenet',?, '2026-01-01','current','manual',"
            "'2026-01-01','2026-01-01')",
            (json.dumps([note_id]),),
        )
        current_tenet = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO insight_notes "
            "(scope_key,kind,body_md,source_note_ids,as_of,status,provenance,supersedes_id,created_at,updated_at) "
            "VALUES ('SYNTH','tenet','prior tenet','[]','2025-01-01','superseded','manual',?,'2025-01-01','2025-01-01')",
            (current_tenet,),
        )
        tenet_ancestor = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "UPDATE insight_notes SET supersedes_id=? WHERE id=?",
            (tenet_ancestor, current_tenet),
        )
        connection.execute(
            "INSERT INTO insight_notes "
            "(scope_key,kind,body_md,source_note_ids,as_of,status,provenance,created_at,updated_at) "
            "VALUES ('SYNTH','theme','generated optional theme','[]','2026-01-01','current','derived',"
            "'2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO decision_drafts "
            "(source_note_id,source_channel,original_text,idempotency_key,status,created_at,updated_at) "
            "VALUES (NULL,'telegram','unlinked draft','telegram-draft','captured','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO decision_drafts "
            "(source_note_id,source_channel,original_text,draft_json,idempotency_key,status,created_at,updated_at) "
            "VALUES (?,'telegram','note-linked unconfirmed draft',?,'telegram-linked',"
            "'captured','2026-01-01','2026-01-01')",
            (note_id, json.dumps({"linked_advice_artifact_id": draft_only_candidate})),
        )
        connection.execute(
            "INSERT INTO decision_drafts "
            "(source_note_id,source_channel,original_text,draft_json,idempotency_key,status,"
            "decision_id,created_at,updated_at) "
            "VALUES (?,'telegram','confirmed decision draft',?,'telegram-confirmed',"
            "'confirmed',?,'2026-01-01','2026-01-01')",
            (
                decision_note_id,
                json.dumps({"linked_advice_artifact_id": linked_candidate}),
                decision_id,
            ),
        )
        connection.execute(
            "INSERT INTO raw_capture_sessions (channel,transcript,created_at,updated_at) "
            "VALUES ('telegram','unlinked capture','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO raw_capture_sessions (channel,transcript,note_id,created_at,updated_at) "
            "VALUES ('telegram','linked capture',?,'2026-01-01','2026-01-01')",
            (note_id,),
        )
        connection.execute(
            "INSERT INTO raw_capture_sessions (channel,transcript,note_id,created_at,updated_at) "
            "VALUES ('telegram','decision-linked capture',?,'2026-01-01','2026-01-01')",
            (decision_note_id,),
        )
        retained_session_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO capture_audit_log "
            "(channel,utterance_summary,action,created_at) "
            "VALUES ('telegram','unlinked capture metadata','captured','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO capture_audit_log (session_id,channel,utterance_summary,action,created_at) "
            "VALUES (?,'telegram','decision-linked capture metadata','captured','2026-01-01')",
            (retained_session_id,),
        )
        connection.execute(
            "INSERT INTO red_team_items "
            "(run_key,lens,kind,attack_md,question_md,proposed_change_md,severity,created_at) "
            "VALUES ('retired-run','bear','test','optional','optional','optional','low','2026-01-01')"
        )
        connection.commit()

    _apply(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        remaining_artifacts = {
            int(row[0]) for row in connection.execute("SELECT id FROM llm_artifacts")
        }
        assert candidate not in remaining_artifacts
        assert draft_only_candidate not in remaining_artifacts
        assert prediction_source in remaining_artifacts
        assert linked_candidate in remaining_artifacts
        assert retained_generic in remaining_artifacts
        assert candidate_parent in remaining_artifacts
        assert retained_child in remaining_artifacts
        assert saydo in remaining_artifacts
        assert successor_candidate in remaining_artifacts
        assert document in remaining_artifacts
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM prompt_calibration_scores").fetchone()[0] == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM advisor_memos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM stance_scores").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM analyst_notes").fetchone()[0] == 2
        assert {row[0] for row in connection.execute("SELECT id FROM insight_notes")} == {
            current_tenet,
            tenet_ancestor,
        }
        assert connection.execute("SELECT COUNT(*) FROM decision_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM raw_capture_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM capture_audit_log").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM red_team_items").fetchone()[0] == 0
        assert connection.execute(
            "SELECT artifact_id,receipt_json,receipt_sha256 FROM pipeline_stage_transitions"
        ).fetchone() == (document, '{"schema_version":"stage-receipt@1"}', "5" * 64)


def test_upgrade_blocks_dangling_typed_references_before_deleting_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history-dangling.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        candidate = _artifact(connection, "lens:legacy")
        _artifact(connection, "company_description", parent_ids="[999999]")
        connection.commit()

    with pytest.raises(RuntimeError, match="parent_artifact_ids_noninteger_or_unresolved"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM llm_artifacts WHERE id=?", (candidate,)).fetchone()


def test_upgrade_retires_generated_saydo_pair_and_filter_but_keeps_linked_evidence(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-saydo-history.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        unlinked_pair = _artifact(connection, "saydo_pair")
        linked_pair = _artifact(connection, "saydo_pair")
        selection_filter = _artifact(connection, "saydo_filter")
        retained_score_pair = _artifact(connection, "saydo_pair")
        due_context = _artifact(connection, "saydo_due_context")
        connection.execute(
            "INSERT INTO decisions "
            "(ticker,recommendation_kind,recommendation_value,conviction,source_artifact_id,"
            "made_at,created_at,decided_by,scope) "
            "VALUES ('SYNTH','position',1.0,'high',?,'2026-01-01','2026-01-01','owner','ticker')",
            (linked_pair,),
        )
        retired_calibration_links = [
            ("saydo_pair", unlinked_pair),
            ("pairwise_analysis", unlinked_pair),
            ("saydo_filter", selection_filter),
            ("saydo_importance", selection_filter),
        ]
        for purpose, artifact_id in retired_calibration_links:
            connection.execute(
                "INSERT INTO prompt_calibration_scores "
                "(purpose,prompt_version,ticker,score,reason,artifact_id) "
                "VALUES (?, 'v1', 'SYNTH', 0.7, 'retired SayDo output', ?)",
                (purpose, artifact_id),
            )
        connection.execute(
            "INSERT INTO prompt_calibration_scores "
            "(purpose,prompt_version,ticker,score,reason,artifact_id) "
            "VALUES ('company_description','v1','SYNTH',0.8,'retained linked output',?)",
            (retained_score_pair,),
        )
        connection.execute(
            "INSERT INTO management_commitments "
            "(ticker,period_made,transcript_segment_id,period_target,kpi_name,comparator,"
            "target_value,unit,narrative,realized_value,outcome,evaluated_at) "
            "VALUES ('SYNTH','2025-01-01',7,'2025-12-31','revenue','>=',100,'USD',"
            "'synthetic source-linked guidance','110','beat','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO saydo_historical_metrics "
            "(ticker,period_made,period_target,kpi_name,comparator,target_value,realized_value,"
            "outcome,guidance_narrative,realized_narrative,evaluated_at) "
            "VALUES ('SYNTH','2025-01-01','2025-12-31','revenue','>=',100,110,'beat',"
            "'synthetic guidance','synthetic actual','2026-01-01')"
        )
        connection.commit()
        guidance_before = connection.execute(
            "SELECT ticker,period_made,transcript_segment_id,period_target,kpi_name,"
            "target_value,realized_value,outcome FROM management_commitments"
        ).fetchall()
        comparison_before = connection.execute(
            "SELECT ticker,period_made,period_target,kpi_name,target_value,realized_value,"
            "outcome,guidance_narrative,realized_narrative FROM saydo_historical_metrics"
        ).fetchall()

    _apply(database)

    with sqlite3.connect(database) as connection:
        artifacts = {int(row[0]) for row in connection.execute("SELECT id FROM llm_artifacts")}
        assert unlinked_pair not in artifacts
        assert selection_filter not in artifacts
        assert linked_pair in artifacts
        assert retained_score_pair in artifacts
        assert due_context in artifacts
        calibration_rows = connection.execute(
            "SELECT purpose,artifact_id FROM prompt_calibration_scores"
        ).fetchall()
        assert calibration_rows == [("company_description", retained_score_pair)]
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT ticker,period_made,transcript_segment_id,period_target,kpi_name,"
                "target_value,realized_value,outcome FROM management_commitments"
            ).fetchall()
            == guidance_before
        )
        assert (
            connection.execute(
                "SELECT ticker,period_made,period_target,kpi_name,target_value,realized_value,"
                "outcome,guidance_narrative,realized_narrative FROM saydo_historical_metrics"
            ).fetchall()
            == comparison_before
        )


def test_upgrade_blocks_unknown_incoming_artifact_foreign_key(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history-fk.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE retained_artifact_extension "
            "(id INTEGER PRIMARY KEY, artifact_id INTEGER REFERENCES llm_artifacts(id))"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="unexpected foreign-key dependency"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_artifact_extension'"
        ).fetchone()


def test_upgrade_blocks_known_foreign_key_source_with_wrong_target(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history-wrong-known-fk.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE prompt_calibration_scores")
        connection.execute(
            "CREATE TABLE prompt_calibration_scores (purpose TEXT NOT NULL, "
            "artifact_id INTEGER REFERENCES decisions(id))"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="known foreign-key edge changed"):
        _apply(database)


def test_upgrade_blocks_unreviewed_trigger_dependency(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history-trigger.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        candidate = _artifact(connection, "lens:legacy")
        connection.execute(
            "CREATE TRIGGER unreviewed_artifact_write AFTER INSERT ON analyst_notes "
            "BEGIN UPDATE llm_artifacts SET dirty=1 WHERE ticker=NEW.ticker; END"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="unreviewed trigger dependency"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM llm_artifacts WHERE id=?", (candidate,)).fetchone()


def test_upgrade_blocks_known_trigger_name_with_changed_sql(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-history-altered-trigger.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        candidate = _artifact(connection, "lens:legacy")
        connection.execute("DROP TRIGGER trg_llm_artifact_document_shape_insert")
        connection.execute(
            "CREATE TRIGGER trg_llm_artifact_document_shape_insert "
            "BEFORE INSERT ON llm_artifacts BEGIN SELECT 1; END"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="unreviewed trigger dependency"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM llm_artifacts WHERE id=?", (candidate,)).fetchone()


def test_upgrade_does_not_commit_partial_deletes_on_failure(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "optional-history-rollback.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO decision_drafts "
            "(source_channel,original_text,idempotency_key,status,created_at,updated_at) "
            "VALUES ('telegram','unlinked','rollback-draft','captured','2026-01-01','2026-01-01')"
        )
        original_context = json.dumps({"ledger_answer": {"text": "Retain on rollback"}})
        note_id = _seed_owner_note(connection, "Retained original note.", original_context)
        connection.commit()

    migration = _migration()
    original_retire_rows = migration._retire_rows

    def fail_after_deletes(connection: sa.Connection) -> None:
        original_retire_rows(connection)
        raise RuntimeError("synthetic failure after optional-history deletes")

    monkeypatch.setattr(migration, "_retire_rows", fail_after_deletes)
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with (
            pytest.raises(RuntimeError, match="synthetic failure"),
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            migration.upgrade()
    finally:
        engine.dispose()

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM decision_drafts WHERE idempotency_key='rollback-draft'"
            ).fetchone()[0]
            == 1
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT context_json FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone() == (original_context,)


def _seed_owner_note(connection: sqlite3.Connection, body: str, context: str) -> int:
    cursor = connection.execute(
        "INSERT INTO analyst_notes(user_id,kind,status,body,source,context_json,created_at,updated_at) "
        "VALUES ('synthetic-owner','musing','open',?,'capture',?,'2026-10-27','2026-10-27')",
        (body, context),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_upgrade_removes_generated_note_context_and_keeps_owner_logging(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-note-context.db", target=PREDECESSOR)
    retained = {"channel": "tray", "owner_process": {"thesis": "Keep this exact input"}}
    original = {
        **retained,
        "ledger_answer": {"text": "Retired automatic answer"},
        "ledger_answer_pending": True,
        "engage_brief": {"bull": "Retired brief"},
        "ladder": "saved",
        "engage_intent": {"mode": "brief"},
        "research_route": "answer_now",
        "research_route_why": "Generated routing reason",
        "route_suggestion": {"intent": "data_fix"},
        "auto_routed": {"intent": "data_fix"},
        "route_parked": {"reason": "Generated parking reason"},
    }
    body = "Remember original owner process."
    with sqlite3.connect(database) as connection:
        note_id = _seed_owner_note(connection, body, json.dumps(original))
        malformed_id = _seed_owner_note(connection, "Keep original owner words.", "invalid-json")
        connection.execute(
            "INSERT INTO llm_budgets(purpose,monthly_cap_usd,warn_threshold_pct,hard_block,"
            "on_exceed,created_at,updated_at) VALUES "
            "('ledger_reply_intent',9,0.8,1,'block','2026-10-27','2026-10-27')"
        )
        retired = (
            "ledger_reply_intent",
            "capture_triage",
            "artifact_brief",
            "capture_intent",
            "research_triage",
            "triage_route_suggest",
            "musing_decision_extract",
            "behavior_distill",
        )
        connection.executemany(
            "INSERT OR IGNORE INTO llm_budgets(purpose,monthly_cap_usd,warn_threshold_pct,"
            "hard_block,on_exceed,created_at,updated_at) "
            "VALUES (?,9,0.8,1,'block','2026-10-27','2026-10-27')",
            [(purpose,) for purpose in retired],
        )
        connection.executemany(
            "INSERT INTO llm_budget_alerts(purpose,month,threshold_pct,alerted_at,spend_at_alert_usd) "
            "VALUES (?,'2026-10',0.8,'2026-10-27',1)",
            [(purpose,) for purpose in (*retired, "transcript_summary_claim_audit")],
        )
    _apply(database)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT purpose FROM llm_budgets WHERE purpose IN "
                "('ledger_reply_intent','capture_triage','artifact_brief','capture_intent',"
                "'research_triage','triage_route_suggest','musing_decision_extract','behavior_distill')"
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT monthly_cap_usd,on_exceed FROM llm_budgets WHERE purpose='transcript_summary_claim_audit'"
        ).fetchone() == (5, "block")
        assert connection.execute(
            "SELECT purpose FROM llm_budget_alerts ORDER BY purpose"
        ).fetchall() == [("transcript_summary_claim_audit",)]
        stored_body, context = connection.execute(
            "SELECT body,context_json FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone()
        assert stored_body == body
        assert json.loads(context) == retained
        assert connection.execute(
            "SELECT context_json FROM analyst_notes WHERE id=?", (malformed_id,)
        ).fetchone() == ("invalid-json",)
        assert (note_id,) in connection.execute(
            "SELECT rowid FROM analyst_notes_fts WHERE analyst_notes_fts MATCH 'Remember'"
        ).fetchall()


@pytest.mark.parametrize("trigger_name", ["unexpected_note_update", "analyst_notes_fts_au"])
def test_upgrade_blocks_unreviewed_note_context_trigger_before_retirement(
    tmp_path: Path, migrated_db: Callable[..., Path], trigger_name: str
) -> None:
    database = migrated_db(tmp_path / "optional-note-trigger.db", target=PREDECESSOR)
    original = {"ledger_answer": {"text": "Old automatic answer"}}
    with sqlite3.connect(database) as connection:
        note_id = _seed_owner_note(connection, "Retained owner input.", json.dumps(original))
        candidate = _artifact(connection, "lens:legacy")
        if trigger_name == "analyst_notes_fts_au":
            connection.execute("DROP TRIGGER analyst_notes_fts_au")
        connection.execute(
            f'CREATE TRIGGER "{trigger_name}" AFTER UPDATE ON analyst_notes BEGIN SELECT 1; END'
        )
    with pytest.raises(RuntimeError, match="unreviewed trigger dependency on note context"):
        _apply(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM llm_artifacts WHERE id=?", (candidate,)).fetchone()
        context = connection.execute(
            "SELECT context_json FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone()[0]
        assert json.loads(context) == original


def test_upgrade_blocks_unreviewed_note_context_view_before_retirement(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-note-view.db", target=PREDECESSOR)
    original = json.dumps({"ledger_answer": {"text": "Original generated answer"}})
    with sqlite3.connect(database) as connection:
        note_id = _seed_owner_note(connection, "Retained owner input.", original)
        candidate = _artifact(connection, "lens:legacy")
        connection.execute(
            "CREATE VIEW unexpected_note_answer AS "
            "SELECT id,json_extract(context_json,'$.ledger_answer.text') AS answer FROM analyst_notes"
        )
    with pytest.raises(RuntimeError, match="unreviewed view dependency"):
        _apply(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT 1 FROM llm_artifacts WHERE id=?", (candidate,)).fetchone()
        assert connection.execute(
            "SELECT context_json FROM analyst_notes WHERE id=?", (note_id,)
        ).fetchone() == (original,)


def _profile_fact(
    connection: sqlite3.Connection,
    key: str,
    *,
    category: str = "behavioral",
    provenance: str = "derived",
    status: str = "proposed",
    is_latest: int = 1,
    superseded_by_id: int | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO owner_profile_facts "
        "(user_id,category,key,value_json,narrative,provenance,status,created_at,"
        "is_latest,superseded_by_id) VALUES ('bhanu',?,?,?,'synthetic',?,?,?, ?,?)",
        (
            category,
            key,
            '{"rule_text":"synthetic","citations":[],"wrong":0,"total":0}',
            provenance,
            status,
            "2026-10-27T12:00:00",
            is_latest,
            superseded_by_id,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_upgrade_removes_unaffirmed_generated_rules_and_retired_model_verdicts(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-profile-history.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        affirmed = _profile_fact(connection, "affirmed-rule", status="affirmed")
        proposed = _profile_fact(connection, "proposed-rule")
        rejected = _profile_fact(connection, "rejected-rule", status="rejected")
        prior = _profile_fact(
            connection,
            "affirmed-rule",
            is_latest=0,
            superseded_by_id=affirmed,
        )
        owner = _profile_fact(connection, "owner-rule", provenance="owner")
        capacity = _profile_fact(connection, "capacity", category="capacity")
        connection.executemany(
            "INSERT INTO model_eval_verdicts "
            "(purpose,candidate,incumbent,verdict,run_id,recorded_at) "
            "VALUES (?,'synthetic-candidate','synthetic-incumbent','parity','test',?)",
            [
                (purpose, "2026-10-27T12:00:00")
                for purpose in (
                    "behavior_distill",
                    "capture_intent",
                    "transcript_summary_claim_audit",
                )
            ],
        )
        retained_before = connection.execute(
            "SELECT * FROM owner_profile_facts WHERE id IN (?,?,?) ORDER BY id",
            (affirmed, owner, capacity),
        ).fetchall()
    _apply(database)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM owner_profile_facts ORDER BY id").fetchall()
            == retained_before
        )
        assert not connection.execute(
            "SELECT id FROM owner_profile_facts WHERE id IN (?,?,?)",
            (proposed, rejected, prior),
        ).fetchall()
        assert connection.execute("SELECT purpose FROM model_eval_verdicts").fetchall() == [
            ("transcript_summary_claim_audit",)
        ]


def test_upgrade_blocks_rule_deletion_when_retained_fact_points_to_candidate(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "optional-profile-linked.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        candidate_artifact = _artifact(connection, "lens:do-not-partially-delete")
        proposed = _profile_fact(connection, "rule")
        _profile_fact(
            connection,
            "rule",
            status="affirmed",
            is_latest=0,
            superseded_by_id=proposed,
        )
        before = connection.execute("SELECT * FROM owner_profile_facts ORDER BY id").fetchall()
    with pytest.raises(RuntimeError, match=r"owner_profile_facts\.retained_successor_is_retired"):
        _apply(database)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM owner_profile_facts ORDER BY id").fetchall() == before
        )
        assert connection.execute(
            "SELECT id FROM llm_artifacts WHERE id=?", (candidate_artifact,)
        ).fetchone()


@pytest.mark.parametrize("target", ["owner_profile_facts", "model_eval_verdicts"])
def test_upgrade_blocks_unknown_dependency_on_retired_rule_or_verdict(
    tmp_path: Path, migrated_db: Callable[..., Path], target: str
) -> None:
    database = migrated_db(tmp_path / "optional-rule-dependency.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        candidate = _artifact(connection, "lens:do-not-partially-delete")
        connection.execute(
            f"CREATE TABLE unknown_history_reference (target_id REFERENCES {target}(id))"
        )
    with pytest.raises(RuntimeError, match="unexpected foreign-key dependency"):
        _apply(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id FROM llm_artifacts WHERE id=?", (candidate,)
        ).fetchone()
