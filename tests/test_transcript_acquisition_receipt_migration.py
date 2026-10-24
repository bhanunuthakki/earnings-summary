"""Migration contract for append-only transcript acquisition receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from models.documents import DocType, SourceType
from pipeline.source_policy import ReportedPeriodStatus, ReportedPeriodWindow
from pipeline.transcript_acquisition import (
    COMBINED_SOURCE_REGIME_IDENTITY,
    TranscriptAcquisitionDeniedError,
    load_authorized_transcript_receipt,
    load_authorized_transcript_replay,
    persist_authorized_transcript_artifact,
    read_authorized_transcript,
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
from transcripts.immutable_staging import StagedTranscriptArtifact

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0017_add_owner_decision_checkpoints"
HEAD = "0018_add_transcript_acquisition_receipts"
REPORTED_PERIOD_PRIOR_HEAD = "0062_reviewed_pdf_pages"
REPORTED_PERIOD_HEAD = "0063_transcript_reported_period_receipts"


@pytest.fixture
def darwin_staging_double(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use typed staged provenance while migration assertions run on Darwin."""

    if sys.platform != "darwin":
        return
    from pipeline import transcript_acquisition as acquisition

    payloads: dict[str, bytes] = {}

    def stage(
        source_path: Path,
        private_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
        max_bytes: int,
    ) -> StagedTranscriptArtifact:
        del max_bytes
        payload = source_path.read_bytes()
        assert len(payload) == expected_size_bytes
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        source = source_path.resolve()
        root = private_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        source_metadata = source.stat()
        root_metadata = root.stat()
        payloads[expected_sha256] = payload
        return StagedTranscriptArtifact(
            source_path=source,
            source_device=int(source_metadata.st_dev),
            source_inode=int(source_metadata.st_ino),
            staging_root=root,
            staging_root_device=int(root_metadata.st_dev),
            staging_root_inode=int(root_metadata.st_ino),
            staged_path=root / f"{expected_sha256}.transcript",
            sha256=expected_sha256,
            size_bytes=expected_size_bytes,
        )

    def read(
        artifact: StagedTranscriptArtifact,
        *,
        trusted_staging_root: Path,
        trusted_staging_root_device: int,
        trusted_staging_root_inode: int,
        expected_source_path: Path,
        expected_source_device: int,
        expected_source_inode: int,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> bytes:
        del (
            trusted_staging_root,
            trusted_staging_root_device,
            trusted_staging_root_inode,
            expected_source_path,
            expected_source_device,
            expected_source_inode,
        )
        assert artifact.sha256 == expected_sha256
        assert artifact.size_bytes == expected_size_bytes
        return payloads[artifact.sha256]

    monkeypatch.setattr(acquisition, "stage_transcript_artifact", stage)
    monkeypatch.setattr(acquisition, "read_staged_transcript", read)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0018_receipts_are_append_only_and_projection_bound(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    darwin_staging_double: None,
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
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','equity','12-31')"
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


def test_0063_preserves_historical_receipt_and_admits_current_receipt(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    darwin_staging_double: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import transcript_acquisition as acquisition

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
    database = migrated_db(repo_root / "data" / "portfolio.db", target=REPORTED_PERIOD_PRIOR_HEAD)
    staging_root = repo_root / ".tmp" / "transcript-acquisition"
    staging_root.mkdir(parents=True)
    request = TranscriptAcquisitionRequest(
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
    )
    with connect_sqlite(database, role=SQLiteConnectionRole.WRITER, schema_preflight=False) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,instrument_type,fiscal_year_end) "
            "VALUES ('ACME','Acme','portfolio','equity','12-31')"
        )
        historical = require_authorized_transcript_request(conn, request)
        historical_artifact = stage_authorized_payload(
            historical,
            payload=b"Operator\nHistorical question?",
            private_root=staging_root,
            source_url="https://issuer.example.invalid/transcript",
            canonical_document_path=Path("transcripts/raw/ACME_Q2_2026.txt"),
        )
        persist_authorized_transcript_artifact(
            conn,
            historical_artifact,
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        conn.commit()
        historical_row = dict(
            conn.execute("SELECT * FROM transcript_acquisition_receipts").fetchone()
        )
    command.upgrade(_config(database), REPORTED_PERIOD_HEAD)
    with connect_sqlite(database, role=SQLiteConnectionRole.WRITER, schema_preflight=False) as conn:
        assert (
            dict(
                conn.execute(
                    "SELECT * FROM transcript_acquisition_receipts WHERE receipt_id=?",
                    (historical_row["receipt_id"],),
                ).fetchone()
            )
            == historical_row
        )

        def qualified_window(*_args: object, **_kwargs: object) -> ReportedPeriodWindow:
            return ReportedPeriodWindow(
                status=ReportedPeriodStatus.COMPLETE,
                period_ends=(
                    date(2026, 6, 30),
                    date(2026, 3, 31),
                    date(2025, 12, 31),
                    date(2025, 9, 30),
                    date(2025, 6, 30),
                ),
                snapshot_ids=("synthetic-sec", "synthetic-ir"),
                fiscal_year_end="12-31",
                proof_sha256="a" * 64,
                reason="migration_fixture_qualified_source_window",
            )

        monkeypatch.setattr(acquisition, "reported_period_window_from_inventory", qualified_window)
        current_request = request.model_copy(
            update={"schema_version": "transcript-acquisition-request@2"}
        )
        current = require_authorized_transcript_request(conn, current_request)
        current_artifact = stage_authorized_payload(
            current,
            payload=b"Operator\nCurrent question?",
            private_root=staging_root,
            source_url="https://issuer.example.invalid/transcript",
            canonical_document_path=Path("transcripts/raw/ACME_Q2_2026.txt"),
        )
        current_id = persist_authorized_transcript_artifact(
            conn,
            current_artifact,
            project_root=repo_root,
            trusted_staging_root=staging_root,
        )
        conn.commit()
        assert (
            load_authorized_transcript_replay(
                conn,
                request=current_request,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
            == current_artifact
        )
        changed_window = qualified_window().model_copy(update={"proof_sha256": "b" * 64})

        def revised_window(*_args: object, **_kwargs: object) -> ReportedPeriodWindow:
            return changed_window

        monkeypatch.setattr(
            acquisition,
            "reported_period_window_from_inventory",
            revised_window,
        )
        assert (
            load_authorized_transcript_receipt(
                conn,
                receipt_id=current_id,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
            == current_artifact
        )
        with pytest.raises(TranscriptAcquisitionDeniedError):
            read_authorized_transcript(
                conn,
                current_artifact,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
        with pytest.raises(TranscriptAcquisitionDeniedError):
            load_authorized_transcript_replay(
                conn,
                request=current_request,
                project_root=repo_root,
                trusted_staging_root=staging_root,
            )
        current_row = dict(
            conn.execute(
                "SELECT * FROM transcript_acquisition_receipts WHERE receipt_id=?",
                (current_id,),
            ).fetchone()
        )
        conn.execute("SAVEPOINT transcript_schema_shape")
        try:
            triggers = conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' "
                "AND tbl_name='transcript_acquisition_receipts'"
            ).fetchall()
            for trigger in triggers:
                conn.execute(f'DROP TRIGGER "{trigger[0]}"')
            columns = tuple(current_row)
            for missing_path in (("schema_version",), ("request", "schema_version")):
                invalid_auth = json.loads(str(current_row["authorization_json"]))
                if len(missing_path) == 1:
                    invalid_auth.pop("schema_version")
                else:
                    invalid_auth["request"].pop("schema_version")
                invalid_artifact = json.loads(str(current_row["artifact_json"]))
                invalid_artifact["authorization"] = invalid_auth
                forged_shape = dict(current_row)
                forged_shape["authorization_json"] = json.dumps(
                    invalid_auth, sort_keys=True, separators=(",", ":")
                )
                forged_shape["artifact_json"] = json.dumps(
                    invalid_artifact, sort_keys=True, separators=(",", ":")
                )
                forged_shape["receipt_id"] = hashlib.sha256(
                    str(forged_shape["artifact_json"]).encode()
                ).hexdigest()
                with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                    conn.execute(
                        f"INSERT INTO transcript_acquisition_receipts ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})",
                        tuple(forged_shape[column] for column in columns),
                    )
        finally:
            conn.execute("ROLLBACK TO SAVEPOINT transcript_schema_shape")
            conn.execute("RELEASE SAVEPOINT transcript_schema_shape")
        forged = dict(current_row)
        forged["receipt_id"] = "d" * 64
        invalid_auth = json.loads(str(forged["authorization_json"]))
        invalid_auth["request"]["schema_version"] = "transcript-acquisition-request@1"
        forged["authorization_json"] = json.dumps(invalid_auth, sort_keys=True)
        columns = tuple(forged)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO transcript_acquisition_receipts ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(forged[column] for column in columns),
            )
    with pytest.raises(RuntimeError, match="cannot downgrade while current transcript receipts"):
        command.downgrade(_config(database), REPORTED_PERIOD_PRIOR_HEAD)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            REPORTED_PERIOD_HEAD,
        )
        assert conn.execute("SELECT COUNT(*) FROM transcript_acquisition_receipts").fetchone() == (
            2,
        )
