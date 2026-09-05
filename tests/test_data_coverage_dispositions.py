from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.data_coverage_dispositions import (
    CoverageArtifactKind,
    CoverageAttempt,
    CoverageAttemptStatus,
    CoverageDispositionStatus,
    DataCoverageDispositionRequest,
    append_data_coverage_disposition,
    current_data_coverage_disposition,
    policy_config_sha256,
)


def _request(
    *,
    status: CoverageDispositionStatus,
    observed_at: datetime,
    evidence: bool = False,
) -> DataCoverageDispositionRequest:
    return DataCoverageDispositionRequest(
        artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
        ticker="NVO",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 6, 30),
        status=status,
        reason_code=(
            "exact_evidence_present" if evidence else "issuer_text_transcript_unavailable"
        ),
        attempts=(
            CoverageAttempt(
                provider="issuer_ir",
                status=(
                    CoverageAttemptStatus.EVIDENCE_PRESENT
                    if evidence
                    else CoverageAttemptStatus.AUTHORIZED_MISS
                ),
            ),
        ),
        policy_name="transcript_acquisition",
        policy_version="2026-08-12.2",
        policy_config_sha256=policy_config_sha256(
            policy_name="transcript_acquisition",
            policy_version="2026-08-12.2",
            providers=("issuer_ir",),
        ),
        evidence_reference="document:42" if evidence else None,
        evidence_sha256="a" * 64 if evidence else None,
        observed_at=observed_at,
        retry_after=(None if evidence else observed_at + timedelta(days=7)),
    )


def test_append_only_revisions_and_current_head(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "coverage.db")
    first_time = datetime(2026, 9, 5, 10, tzinfo=UTC)
    with sqlite3.connect(db_path) as conn:
        first = append_data_coverage_disposition(
            conn,
            _request(status=CoverageDispositionStatus.SOURCE_UNAVAILABLE, observed_at=first_time),
            recorded_at=first_time,
        )
        replay = append_data_coverage_disposition(
            conn,
            _request(status=CoverageDispositionStatus.SOURCE_UNAVAILABLE, observed_at=first_time),
            recorded_at=first_time,
        )
        second_time = first_time + timedelta(days=1)
        second = append_data_coverage_disposition(
            conn,
            _request(
                status=CoverageDispositionStatus.SATISFIED,
                observed_at=second_time,
                evidence=True,
            ),
            recorded_at=second_time,
        )
        conn.commit()

        assert replay == first
        assert first.revision == 1
        assert second.revision == 2
        assert second.supersedes_disposition_id == first.disposition_id
        current = current_data_coverage_disposition(
            conn,
            artifact_kind=CoverageArtifactKind.TEXT_TRANSCRIPT,
            ticker="NVO",
            fiscal_year=2026,
            fiscal_quarter=2,
        )
        assert current == second
        assert conn.execute(
            "SELECT COUNT(*) FROM v_data_coverage_dispositions_current"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE data_coverage_dispositions SET reason_code='changed' "
                "WHERE disposition_id=?",
                (first.disposition_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM data_coverage_dispositions WHERE disposition_id=?",
                (first.disposition_id,),
            )


def test_disposition_never_allows_missing_evidence_to_claim_satisfied() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="requires exact evidence"):
        DataCoverageDispositionRequest(
            artifact_kind=CoverageArtifactKind.EARNINGS_SURPRISE,
            ticker="BN",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=date(2026, 6, 30),
            status=CoverageDispositionStatus.SATISFIED,
            reason_code="source_hit",
            attempts=(),
            policy_name="earnings_surprise_sources",
            policy_version="1",
            policy_config_sha256="b" * 64,
            observed_at=now,
        )


def test_commitment_scan_receipts_are_append_only(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    db_path = migrated_db(tmp_path / "scan-receipt.db")
    empty_manifest_sha = hashlib.sha256(b"[]").hexdigest()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO commitment_scan_receipts "
            "(receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "c" * 64,
                1,
                1,
                "d" * 64,
                "e" * 64,
                "v1",
                0,
                "[]",
                empty_manifest_sha,
                "2026-09-05T00:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE commitment_scan_receipts SET prompt_version='v2' WHERE receipt_id=?",
                ("c" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM commitment_scan_receipts WHERE receipt_id=?", ("c" * 64,))
