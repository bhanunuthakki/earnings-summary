"""Migration contract for append-only transcript acquisition receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from models.documents import DocType, SourceType
from pipeline.transcript_acquisition import (
    COMBINED_SOURCE_REGIME_IDENTITY,
    persist_authorized_transcript_artifact,
    require_authorized_transcript_request,
    stage_authorized_payload,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from transcripts.acquisition_semantics import (
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptProvider,
)

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0017_add_owner_decision_checkpoints"
HEAD = "0018_add_transcript_acquisition_receipts"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0018_receipts_are_append_only_and_projection_bound(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    repo_root = tmp_path / "repo"
    config_path = repo_root / "micro_thesis" / "ir_config" / "ACME.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "ticker": "ACME",
                "platform": "mz",
                "results_center_url": "https://issuer.example.invalid/results",
                "spreadsheet_kpis": [],
            }
        ),
        encoding="utf-8",
    )
    database = migrated_db(repo_root / "data" / "portfolio.db", target=HEAD)
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    with connect_sqlite(
        database,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    ) as connection:
        connection.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','12-31')"
        )
        authorization = require_authorized_transcript_request(
            connection,
            TranscriptAcquisitionRequest(
                entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
                canonical_ticker="ACME",
                fiscal_year=2026,
                fiscal_quarter=2,
                as_of=date(2026, 8, 12),
                source_type=SourceType.IR_DOC,
                document_type=DocType.EARNINGS_CALL_TRANSCRIPT,
                provider=TranscriptProvider.ISSUER_IR,
                owner_requested=False,
                existing_artifact=False,
                existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
                source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
                source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
            ),
        )
        artifact = stage_authorized_payload(
            authorization,
            payload=b"Operator\nQuestion?",
            private_root=staging_root,
            source_url="https://issuer.example.invalid/transcript",
            canonical_document_path=Path("transcripts/raw/ACME_Q2_2026.txt"),
        )
        persist_authorized_transcript_artifact(
            connection,
            artifact,
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE transcript_acquisition_receipts SET artifact_size_bytes=13")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM transcript_acquisition_receipts")
        row = dict(connection.execute("SELECT * FROM transcript_acquisition_receipts").fetchone())
        columns = tuple(row)
        placeholders = ",".join("?" for _ in columns)
        forged = dict(row)
        forged["receipt_id"] = "d" * 64
        forged["recorded_at"] = "2026-02-30T99:99:99.000000Z"
        with pytest.raises(sqlite3.IntegrityError, match="invalid transcript acquisition receipt"):
            connection.execute(
                f"INSERT INTO transcript_acquisition_receipts ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(forged[column] for column in columns),
            )
        forged_artifact = json.loads(str(row["artifact_json"]))
        forged_artifact["source_url"] = "file:///C:/Windows/System32/config/SAM"
        forged_artifact["source_path"] = "C:/Windows/System32/config/SAM"
        forged_artifact["staged"]["source_path"] = "C:/Windows/System32/config/SAM"
        forged_artifact["staged"]["staging_root"] = "C:/Windows/System32/config"
        forged_artifact["staged"]["staged_path"] = "C:/Windows/System32/config/x.transcript"
        forged_json = json.dumps(forged_artifact, separators=(",", ":"), sort_keys=True)
        coordinated = dict(row)
        coordinated["artifact_json"] = forged_json
        coordinated["receipt_id"] = hashlib.sha256(forged_json.encode()).hexdigest()
        coordinated["source_url"] = forged_artifact["source_url"]
        with pytest.raises(sqlite3.IntegrityError, match="invalid transcript acquisition receipt"):
            connection.execute(
                f"INSERT INTO transcript_acquisition_receipts ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(coordinated[column] for column in columns),
            )

    with (
        sqlite3.connect(database) as unmanaged,
        pytest.raises(sqlite3.OperationalError, match="transcript_receipt_valid"),
    ):
        unmanaged.execute(
            f"INSERT INTO transcript_acquisition_receipts ({','.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )


def test_0018_downgrade_removes_only_transcript_receipts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "transcript-receipts-down.db", target=HEAD)
    command.downgrade(_config(database), PRIOR_HEAD)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PRIOR_HEAD,
        )
        names = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "transcript_acquisition_receipts" not in names
        assert "owner_decision_checkpoints" in names
