from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PARENT = "0062_explicit_decision_journal"
REVISION = "0063_document_product_completion"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _schema_objects(connection: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type=?", (object_type,))
    }


def test_0063_adds_document_columns_and_receipts_without_rebuilding_existing_rows(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = migrated_db(tmp_path / "document-product-completion.db", target=PARENT)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO llm_artifacts "
        "(ticker,scope,purpose,fiscal_period,content_md,input_sha256,output_sha256,"
        "model,prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "SYNTH",
            "ticker",
            "company_description",
            None,
            "Retained generic artifact",
            "1" * 64,
            "2" * 64,
            "test-model",
            "legacy-v1",
            "2026-10-26T12:00:00+00:00",
        ),
    )
    artifact_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO pipeline_runs (pipeline_key,directive,ticker_scope,first_started_at) "
        "VALUES ('document-test-pipeline','test','SYNTH','2026-10-26T12:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO pipeline_attempts (attempt_id,pipeline_key,started_at,status) "
        "VALUES ('document-test-attempt','document-test-pipeline',"
        "'2026-10-26T12:00:00+00:00','in_progress')"
    )
    connection.execute(
        "INSERT INTO pipeline_stage_transitions "
        "(attempt_id,ticker,stage,status,started_at) "
        "VALUES ('document-test-attempt','SYNTH','FETCH','ok','2026-10-26T12:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO prompt_calibration_scores "
        "(purpose,prompt_version,ticker,score,reason,artifact_id) "
        "VALUES ('company_description','legacy-v1','SYNTH',0.8,'retained reference',?)",
        (artifact_id,),
    )
    connection.commit()
    before_indexes = _schema_objects(connection, "index")
    before_triggers = _schema_objects(connection, "trigger")
    before_artifact = connection.execute(
        "SELECT id,ticker,scope,purpose,fiscal_period,content_md,input_sha256,"
        "output_sha256,model,prompt_version,generated_at FROM llm_artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    before_stage = connection.execute(
        "SELECT attempt_id,ticker,stage,status,started_at FROM pipeline_stage_transitions"
    ).fetchone()
    connection.close()

    command.upgrade(_config(path), REVISION)

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        artifact_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(llm_artifacts)")
        }
        stage_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(pipeline_stage_transitions)")
        }
        assert {
            "issuer_id",
            "document_version_id",
            "document_period_start",
            "document_period_end",
            "document_period_type",
            "document_fiscal_year",
            "document_fiscal_quarter",
        } <= artifact_columns
        assert {"artifact_id", "receipt_json", "receipt_sha256"} <= stage_columns
        assert connection.execute(
            "SELECT monthly_cap_usd,warn_threshold_pct,hard_block,on_exceed "
            "FROM llm_budgets WHERE purpose='transcript_summary_claim_audit'"
        ).fetchone() == (5, 0.8, 1, "block")
        assert (
            connection.execute(
                "SELECT id,ticker,scope,purpose,fiscal_period,content_md,input_sha256,"
                "output_sha256,model,prompt_version,generated_at FROM llm_artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
            == before_artifact
        )
        assert (
            connection.execute(
                "SELECT attempt_id,ticker,stage,status,started_at FROM pipeline_stage_transitions"
            ).fetchone()
            == before_stage
        )
        assert connection.execute(
            "SELECT artifact_id,receipt_json,receipt_sha256 FROM pipeline_stage_transitions"
        ).fetchone() == (None, None, None)
        assert connection.execute(
            "SELECT artifact_id FROM prompt_calibration_scores"
        ).fetchone() == (artifact_id,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert before_indexes <= _schema_objects(connection, "index")
        assert before_triggers <= _schema_objects(connection, "trigger")
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE '%document%product%'"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="document receipt"):
            connection.execute(
                "UPDATE pipeline_stage_transitions SET artifact_id=?, receipt_json=NULL, "
                "receipt_sha256=NULL",
                (artifact_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO llm_artifacts (ticker,scope,purpose,input_sha256,"
                "prompt_version,generated_at,issuer_id) VALUES (?,?,?,?,?,?,?)",
                (
                    "SYNTH",
                    "ticker",
                    "company_description",
                    "1" * 64,
                    "v1",
                    "2026-10-26T12:00:00+00:00",
                    "issuer-synthetic",
                ),
            )

        valid_key = {
            "issuer_id": "missing-issuer",
            "document_version_id": "missing-document",
            "period_start": None,
            "period_end": "2026-03-31",
            "period_type": "Q1",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "product": "presentation_brief",
        }

        def insert_document_candidate(
            *, content_json: str, fiscal_year: int | None = 2026, input_sha: str = "1" * 64
        ) -> None:
            connection.execute(
                "INSERT INTO llm_artifacts "
                "(ticker,scope,purpose,fiscal_period,content_json,input_sha256,output_sha256,"
                "model,prompt_version,generated_at,issuer_id,document_version_id,"
                "document_period_start,document_period_end,document_period_type,"
                "document_fiscal_year,document_fiscal_quarter) "
                "VALUES ('SYNTH','document','presentation_brief','FY2026Q1',?,?,?,'test',"
                "'v1','2026-10-26T12:00:00+00:00','missing-issuer','missing-document',"
                "NULL,'2026-03-31','Q1',?,1)",
                (content_json, input_sha, "2" * 64, fiscal_year),
            )

        absent_key = dict(valid_key)
        del absent_key["period_type"]
        with pytest.raises(sqlite3.IntegrityError, match="invalid native document"):
            insert_document_candidate(
                content_json=(
                    '{"schema_version":"document-product-envelope@1","key":'
                    + json.dumps(absent_key)
                    + "}"
                )
            )

        null_fiscal_key = dict(valid_key)
        null_fiscal_key["fiscal_year"] = None
        with pytest.raises(sqlite3.IntegrityError, match="invalid native document"):
            insert_document_candidate(
                content_json=(
                    '{"schema_version":"document-product-envelope@1","key":'
                    + json.dumps(null_fiscal_key)
                    + "}"
                ),
                fiscal_year=None,
            )

        valid_document_json = (
            '{"schema_version":"document-product-envelope@1","key":' + json.dumps(valid_key) + "}"
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid native document"):
            insert_document_candidate(
                content_json=valid_document_json,
                input_sha="z" * 64,
            )
    finally:
        connection.close()
