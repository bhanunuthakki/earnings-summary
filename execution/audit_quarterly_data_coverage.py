"""Read-only, deterministic closure audit for an explicit quarterly cohort.

The command never infers a cohort: callers must pass exactly eleven
``--ticker`` values and they must equal the active portfolio roster in the
audited database. A fixed ``--cutoff-at`` selects the latest disposition
revision, while canonical evidence is always recomputed independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from aggregator_sources import SOURCES as TRANSCRIPT_SOURCES  # noqa: E402
from compute.evidence_snapshot import snapshot_recorded_evidence  # noqa: E402
from earnings_surprise_store import verify_persisted_observation_row  # noqa: E402
from llm.prompt_versions import prompt_version_for  # noqa: E402
from pipeline.commitment_scan_receipts import (  # noqa: E402
    current_commitment_scan_receipt,
)
from pipeline.data_coverage_dispositions import (  # noqa: E402
    COMMITMENT_SCAN_POLICY_NAME,
    COMMITMENT_SCAN_POLICY_PROVIDERS,
    COMMITMENT_SCAN_POLICY_VERSION,
    EARNINGS_SURPRISE_POLICY_NAME,
    EARNINGS_SURPRISE_POLICY_PROVIDERS,
    EARNINGS_SURPRISE_POLICY_VERSION,
    CoverageArtifactKind,
    CoverageAttempt,
    CoverageAttemptStatus,
    CoverageDispositionStatus,
    DataCoverageDispositionRequest,
    fiscal_quarter_period_end,
    policy_config_sha256,
)
from pipeline.transcript_acquisition import COMBINED_SOURCE_REGIME_IDENTITY  # noqa: E402
from provenance.selection import selected_transcripts_relation  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    transcript_authorization_idempotency_key,
)

_ARTIFACT_ORDER = (
    CoverageArtifactKind.TEXT_TRANSCRIPT,
    CoverageArtifactKind.COMMITMENT_SCAN,
    CoverageArtifactKind.EARNINGS_SURPRISE,
)
_ACCEPTED_GAP_STATUSES = frozenset(
    {
        "source_unavailable",
        "policy_blocked",
        "provider_coverage_gap",
        "repair_evidence_missing",
    }
)
_RETRYABLE_STATUSES = frozenset({"source_unavailable", "provider_coverage_gap"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--cutoff-at must include a timezone")
    return parsed.astimezone(UTC)


def _season_target(
    *, reporting_year: int, reporting_quarter: int, fye_month: int
) -> tuple[int, int, date]:
    """Map a calendar reporting season to the issuer fiscal quarter ending in it."""

    start_month = 1 + (reporting_quarter - 1) * 3
    end_month = reporting_quarter * 3
    season_start = date(reporting_year, start_month, 1)
    season_end = date(reporting_year, end_month, monthrange(reporting_year, end_month)[1])
    candidates = [
        (fiscal_year, fiscal_quarter, period_end)
        for fiscal_year in range(reporting_year, reporting_year + 2)
        for fiscal_quarter in range(1, 5)
        if season_start
        <= (period_end := fiscal_quarter_period_end(fiscal_year, fiscal_quarter, fye_month))
        <= season_end
    ]
    if len(candidates) != 1:
        raise ValueError("reporting season does not map to exactly one issuer fiscal quarter")
    return candidates[0]


def _current_policy_identity(
    artifact_kind: CoverageArtifactKind,
) -> tuple[str, str, tuple[str, ...]]:
    if artifact_kind is CoverageArtifactKind.TEXT_TRANSCRIPT:
        return (
            "transcript_acquisition",
            TRANSCRIPT_ACQUISITION_POLICY_VERSION,
            tuple(source.name for source in TRANSCRIPT_SOURCES),
        )
    if artifact_kind is CoverageArtifactKind.COMMITMENT_SCAN:
        return (
            COMMITMENT_SCAN_POLICY_NAME,
            COMMITMENT_SCAN_POLICY_VERSION,
            COMMITMENT_SCAN_POLICY_PROVIDERS,
        )
    return (
        EARNINGS_SURPRISE_POLICY_NAME,
        EARNINGS_SURPRISE_POLICY_VERSION,
        EARNINGS_SURPRISE_POLICY_PROVIDERS,
    )


def _attempts_are_sufficient(
    *,
    artifact_kind: CoverageArtifactKind,
    status: CoverageDispositionStatus,
    reason_code: str,
    attempts: tuple[CoverageAttempt, ...],
) -> bool:
    if not attempts:
        return False
    statuses = {attempt.status for attempt in attempts}
    providers = {attempt.provider for attempt in attempts}
    if status is CoverageDispositionStatus.SATISFIED:
        return bool(
            statuses
            & {
                CoverageAttemptStatus.EVIDENCE_PRESENT,
                CoverageAttemptStatus.ACQUIRED,
                CoverageAttemptStatus.IDEMPOTENT_REPLAY,
                CoverageAttemptStatus.SOURCE_HIT,
            }
        )
    if status is CoverageDispositionStatus.SOURCE_UNAVAILABLE:
        if artifact_kind is CoverageArtifactKind.TEXT_TRANSCRIPT:
            expected = {source.name for source in TRANSCRIPT_SOURCES}
            return (
                providers == expected
                and statuses
                <= {
                    CoverageAttemptStatus.AUTHORIZED_MISS,
                    CoverageAttemptStatus.POLICY_DENIED,
                }
                and CoverageAttemptStatus.AUTHORIZED_MISS in statuses
            )
        if artifact_kind is CoverageArtifactKind.COMMITMENT_SCAN:
            return providers == {"transcript_prerequisite"} and statuses == {
                CoverageAttemptStatus.AUTHORIZED_MISS
            }
        return False
    if status is CoverageDispositionStatus.POLICY_BLOCKED:
        return (
            artifact_kind is CoverageArtifactKind.TEXT_TRANSCRIPT
            and providers == {source.name for source in TRANSCRIPT_SOURCES}
            and statuses == {CoverageAttemptStatus.POLICY_DENIED}
        )
    if status is CoverageDispositionStatus.PROVIDER_COVERAGE_GAP:
        return (
            artifact_kind is CoverageArtifactKind.EARNINGS_SURPRISE
            and providers == set(EARNINGS_SURPRISE_POLICY_PROVIDERS)
            and statuses <= {CoverageAttemptStatus.SOURCE_MISS}
        )
    if status is CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING:
        return (
            artifact_kind is CoverageArtifactKind.TEXT_TRANSCRIPT
            and providers == {source.name for source in TRANSCRIPT_SOURCES}
            and statuses
            <= {
                CoverageAttemptStatus.AUTHORIZED_MISS,
                CoverageAttemptStatus.POLICY_DENIED,
            }
            and bool(
                statuses
                & {
                    CoverageAttemptStatus.AUTHORIZED_MISS,
                    CoverageAttemptStatus.POLICY_DENIED,
                }
            )
        ) or (
            artifact_kind is CoverageArtifactKind.COMMITMENT_SCAN
            and providers == {"transcript_prerequisite"}
            and statuses == {CoverageAttemptStatus.FAILED}
        )
    if reason_code == "reacquired_transcript_conflicts_with_canonical_bytes":
        by_provider = {attempt.provider: attempt.status for attempt in attempts}
        return by_provider == {
            "issuer_ir": (
                CoverageAttemptStatus.IDEMPOTENT_REPLAY
                if CoverageAttemptStatus.IDEMPOTENT_REPLAY in statuses
                else CoverageAttemptStatus.ACQUIRED
            ),
            "canonical_processed_path": CoverageAttemptStatus.FAILED,
        }
    return CoverageAttemptStatus.FAILED in statuses


def _reason_matches_status(
    *,
    artifact_kind: CoverageArtifactKind,
    status: CoverageDispositionStatus,
    reason_code: str,
) -> bool:
    expected: dict[tuple[CoverageArtifactKind, CoverageDispositionStatus], frozenset[str]] = {
        (CoverageArtifactKind.TEXT_TRANSCRIPT, CoverageDispositionStatus.SATISFIED): frozenset(
            {"authorized_processed_transcript_with_segments", "exact_db_path_sha_evidence"}
        ),
        (
            CoverageArtifactKind.TEXT_TRANSCRIPT,
            CoverageDispositionStatus.SOURCE_UNAVAILABLE,
        ): frozenset({"authorized_text_transcript_unavailable"}),
        (CoverageArtifactKind.TEXT_TRANSCRIPT, CoverageDispositionStatus.POLICY_BLOCKED): frozenset(
            {"transcript_source_policy_denied"}
        ),
        (
            CoverageArtifactKind.TEXT_TRANSCRIPT,
            CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING,
        ): frozenset({"canonical_transcript_evidence_missing"}),
        (
            CoverageArtifactKind.TEXT_TRANSCRIPT,
            CoverageDispositionStatus.OPERATIONAL_ERROR,
        ): frozenset(
            {
                "reacquired_transcript_conflicts_with_canonical_bytes",
                "transcript_acquisition_exception",
                "transcript_ingest_postcondition_failed",
            }
        ),
        (CoverageArtifactKind.COMMITMENT_SCAN, CoverageDispositionStatus.SATISFIED): frozenset(
            {"commitment_scan_evidence_present"}
        ),
        (
            CoverageArtifactKind.COMMITMENT_SCAN,
            CoverageDispositionStatus.SOURCE_UNAVAILABLE,
        ): frozenset({"transcript_prerequisite_unavailable"}),
        (
            CoverageArtifactKind.COMMITMENT_SCAN,
            CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING,
        ): frozenset({"transcript_evidence_prerequisite_missing"}),
        (
            CoverageArtifactKind.COMMITMENT_SCAN,
            CoverageDispositionStatus.OPERATIONAL_ERROR,
        ): frozenset(
            {"commitment_extraction_missing_evidence", "commitment_extraction_not_attempted"}
        ),
        (CoverageArtifactKind.EARNINGS_SURPRISE, CoverageDispositionStatus.SATISFIED): frozenset(
            {"persisted_surprise_observation_and_projection"}
        ),
        (
            CoverageArtifactKind.EARNINGS_SURPRISE,
            CoverageDispositionStatus.PROVIDER_COVERAGE_GAP,
        ): frozenset({"no_admitted_surprise_observation"}),
        (
            CoverageArtifactKind.EARNINGS_SURPRISE,
            CoverageDispositionStatus.OPERATIONAL_ERROR,
        ): frozenset({"surprise_source_refresh_failed"}),
    }
    return reason_code in expected.get((artifact_kind, status), frozenset())


def _transcript_authorization_keys(
    *, ticker: str, fiscal_year: int, fiscal_quarter: int, period_end: date
) -> dict[str, str]:
    keys: dict[str, str] = {}
    for source in TRANSCRIPT_SOURCES:
        request = TranscriptAcquisitionRequest(
            entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
            canonical_ticker=ticker,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=period_end,
            source_type=source.source_type,
            document_type=source.document_type,
            provider=source.provider,
            owner_requested=False,
            existing_artifact=False,
            existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
            source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
            source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
        )
        keys[source.name] = transcript_authorization_idempotency_key(request)
    return keys


def _coverage_request_from_row(
    row: sqlite3.Row,
    *,
    artifact_kind: CoverageArtifactKind,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
) -> DataCoverageDispositionRequest:
    parsed_attempts = cast(object, json.loads(str(row["attempts_json"])))
    if not isinstance(parsed_attempts, list):
        raise ValueError("attempts are not an array")
    attempts: list[CoverageAttempt] = []
    for item in cast(list[object], parsed_attempts):
        if not isinstance(item, dict):
            raise ValueError("attempt is not an object")
        attempt = cast(dict[str, object], item)
        attempts.append(
            CoverageAttempt(
                provider=str(attempt.get("provider", "")),
                status=CoverageAttemptStatus(str(attempt.get("status", ""))),
                authorization_key=(
                    None
                    if attempt.get("authorization_key") is None
                    else str(attempt["authorization_key"])
                ),
            )
        )
    retry_after = None if row["retry_after"] is None else _parse_cutoff(str(row["retry_after"]))
    return DataCoverageDispositionRequest(
        artifact_kind=artifact_kind,
        ticker=ticker,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        period_end=date.fromisoformat(str(row["period_end"])),
        status=CoverageDispositionStatus(str(row["status"])),
        reason_code=str(row["reason_code"]),
        attempts=tuple(attempts),
        policy_name=str(row["policy_name"]),
        policy_version=str(row["policy_version"]),
        policy_config_sha256=str(row["policy_config_sha256"]),
        evidence_reference=(
            None if row["evidence_reference"] is None else str(row["evidence_reference"])
        ),
        evidence_sha256=(None if row["evidence_sha256"] is None else str(row["evidence_sha256"])),
        operation_id=None if row["operation_id"] is None else str(row["operation_id"]),
        observed_at=_parse_cutoff(str(row["observed_at"])),
        retry_after=retry_after,
    )


def _latest_disposition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
    period_end: date,
    artifact_kind: CoverageArtifactKind,
    cutoff_at: datetime,
) -> dict[str, object] | None:
    rows = conn.execute(
        "SELECT disposition_id,idempotency_key,revision,supersedes_disposition_id,period_end,status,"
        "reason_code,attempts_json,attempts_sha256,policy_name,policy_version,"
        "policy_config_sha256,evidence_reference,evidence_sha256,operation_id,observed_at,retry_after,"
        "recorded_at FROM data_coverage_dispositions "
        "WHERE ticker=? AND fiscal_year=? AND fiscal_quarter=? AND artifact_kind=? "
        "AND datetime(recorded_at)<=datetime(?) ORDER BY revision",
        (
            ticker,
            fiscal_year,
            fiscal_quarter,
            artifact_kind.value,
            _iso_utc(cutoff_at),
        ),
    ).fetchall()
    if not rows:
        return None
    reasons: list[str] = []
    prior_id: str | None = None
    latest = rows[-1]
    latest_request: DataCoverageDispositionRequest | None = None
    for expected_revision, row in enumerate(rows, start=1):
        if str(row["period_end"]) != period_end.isoformat():
            reasons.append("period_end_mismatch")
        attempts_json = str(row["attempts_json"])
        try:
            parsed_attempts = cast(object, json.loads(attempts_json))
        except json.JSONDecodeError:
            parsed_attempts = None
        if not isinstance(parsed_attempts, list):
            reasons.append("attempts_json_invalid")
        if _sha256(attempts_json) != str(row["attempts_sha256"]):
            reasons.append("attempts_sha256_mismatch")
        if int(row["revision"]) != expected_revision:
            reasons.append("revision_sequence_invalid")
        predecessor = (
            None
            if row["supersedes_disposition_id"] is None
            else str(row["supersedes_disposition_id"])
        )
        if predecessor != prior_id:
            reasons.append("revision_predecessor_invalid")
        expected_disposition_id = _sha256(
            _canonical_json(
                {
                    "idempotency_key": str(row["idempotency_key"]),
                    "revision": int(row["revision"]),
                    "supersedes_disposition_id": predecessor,
                }
            )
        )
        if str(row["disposition_id"]) != expected_disposition_id:
            reasons.append("disposition_id_mismatch")
        try:
            request = _coverage_request_from_row(
                row,
                artifact_kind=artifact_kind,
                ticker=ticker,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
            )
            if str(row["idempotency_key"]) != _sha256(
                _canonical_json(request.model_dump(mode="json"))
            ):
                reasons.append("idempotency_key_mismatch")
            if row is latest:
                latest_request = request
        except (TypeError, ValueError):
            reasons.append("disposition_request_invalid")
        prior_id = str(row["disposition_id"])
    status = str(latest["status"])
    retry_after = None if latest["retry_after"] is None else str(latest["retry_after"])
    if latest_request is not None and not _attempts_are_sufficient(
        artifact_kind=artifact_kind,
        status=latest_request.status,
        reason_code=latest_request.reason_code,
        attempts=latest_request.attempts,
    ):
        reasons.append("attempts_semantically_insufficient")
    if latest_request is not None and not _reason_matches_status(
        artifact_kind=artifact_kind,
        status=latest_request.status,
        reason_code=latest_request.reason_code,
    ):
        reasons.append("reason_code_status_mismatch")
    if (
        latest_request is not None
        and artifact_kind is CoverageArtifactKind.TEXT_TRANSCRIPT
        and latest_request.status
        in {
            CoverageDispositionStatus.SOURCE_UNAVAILABLE,
            CoverageDispositionStatus.POLICY_BLOCKED,
            CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING,
            CoverageDispositionStatus.OPERATIONAL_ERROR,
        }
    ):
        expected_keys = _transcript_authorization_keys(
            ticker=ticker,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_end=period_end,
        )
        if any(
            attempt.provider in expected_keys
            and attempt.authorization_key != expected_keys[attempt.provider]
            for attempt in latest_request.attempts
        ):
            reasons.append("attempt_authorization_identity_mismatch")
    expected_policy_name, expected_policy_version, expected_providers = _current_policy_identity(
        artifact_kind
    )
    if (
        str(latest["policy_name"]) != expected_policy_name
        or str(latest["policy_version"]) != expected_policy_version
    ):
        reasons.append("policy_identity_stale")
    if str(latest["policy_config_sha256"]) != policy_config_sha256(
        policy_name=expected_policy_name,
        policy_version=expected_policy_version,
        providers=expected_providers,
    ):
        reasons.append("policy_config_sha256_mismatch")
    if status in _RETRYABLE_STATUSES:
        if retry_after is None:
            reasons.append("retry_after_missing")
        else:
            try:
                if _parse_cutoff(retry_after) <= cutoff_at:
                    reasons.append("disposition_retry_due")
            except ValueError:
                reasons.append("retry_after_invalid")
    return {
        "disposition_id": str(latest["disposition_id"]),
        "revision": int(latest["revision"]),
        "status": status,
        "reason_code": str(latest["reason_code"]),
        "attempts_sha256": str(latest["attempts_sha256"]),
        "policy_name": str(latest["policy_name"]),
        "policy_version": str(latest["policy_version"]),
        "policy_config_sha256": str(latest["policy_config_sha256"]),
        "evidence_reference": (
            None if latest["evidence_reference"] is None else str(latest["evidence_reference"])
        ),
        "evidence_sha256": (
            None if latest["evidence_sha256"] is None else str(latest["evidence_sha256"])
        ),
        "observed_at": str(latest["observed_at"]),
        "retry_after": retry_after,
        "recorded_at": str(latest["recorded_at"]),
        "valid": not reasons,
        "verification_reason_codes": sorted(set(reasons)),
    }


def _transcript_evidence(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    ticker: str,
    fiscal_year: int,
    fiscal_quarter: int,
    period_end: date,
    cutoff_at: datetime,
) -> dict[str, object]:
    reasons: list[str] = []
    relation = selected_transcripts_relation(conn).sql
    transcripts = conn.execute(
        "SELECT t.id AS transcript_id,t.document_id,d.file_path,d.sha256 "
        f"FROM {relation} AS t JOIN documents AS d ON d.id=t.document_id "  # nosec B608
        "WHERE UPPER(t.ticker)=? AND t.is_current=1 AND t.fiscal_period_type=? "
        "AND date(t.period_end)=date(?) ORDER BY t.id",
        (ticker, f"Q{fiscal_quarter}", period_end.isoformat()),
    ).fetchall()
    if len(transcripts) != 1:
        reasons.append(
            "current_transcript_missing" if not transcripts else "current_transcript_ambiguous"
        )
        return {
            "complete": False,
            "verification_reason_codes": reasons,
            "transcript_id": None,
            "document_id": None,
            "receipt_id": None,
            "processed_path": None,
            "sha256": None,
            "segment_count": 0,
            "evidence_reference": None,
            "evidence_sha256": None,
        }
    transcript = transcripts[0]
    transcript_id = int(transcript["transcript_id"])
    document_id = int(transcript["document_id"])
    expected_processed = f"transcripts/processed/{ticker}_Q{fiscal_quarter}_{fiscal_year}.txt"
    expected_raw = f"transcripts/raw/{ticker}_Q{fiscal_quarter}_{fiscal_year}.txt"
    recorded_path = str(transcript["file_path"])
    recorded_sha = str(transcript["sha256"])
    if recorded_path != expected_processed:
        reasons.append("processed_path_mismatch")
    snapshot = snapshot_recorded_evidence(repo_root, recorded_path)
    if snapshot is None:
        reasons.append("processed_bytes_missing_or_unsafe")
    elif snapshot.sha256 != recorded_sha:
        reasons.append("processed_sha256_mismatch")
    segment_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id=?", (transcript_id,)
        ).fetchone()[0]
    )
    if segment_count < 1:
        reasons.append("transcript_segments_missing")
    receipts = conn.execute(
        "SELECT receipt_id,artifact_sha256,canonical_document_path,artifact_json "
        "FROM transcript_acquisition_receipts WHERE canonical_ticker=? "
        "AND fiscal_year=? AND fiscal_quarter=? AND artifact_sha256=? "
        "AND (document_id IS NULL OR document_id=?) "
        "AND provider='issuer_ir' AND source_type='ir_doc' "
        "AND document_type='earnings_call_transcript' "
        "AND canonical_document_path=? AND datetime(recorded_at)<=datetime(?) "
        "ORDER BY datetime(recorded_at) DESC,receipt_id DESC",
        (
            ticker,
            fiscal_year,
            fiscal_quarter,
            recorded_sha,
            document_id,
            expected_raw,
            _iso_utc(cutoff_at),
        ),
    ).fetchall()
    receipt_id: str | None = None
    for receipt in receipts:
        candidate_id = str(receipt["receipt_id"])
        if _sha256(str(receipt["artifact_json"])) == candidate_id:
            receipt_id = candidate_id
            break
    if receipt_id is None:
        reasons.append("authorized_transcript_receipt_missing_or_invalid")
    return {
        "complete": not reasons,
        "verification_reason_codes": sorted(set(reasons)),
        "transcript_id": transcript_id,
        "document_id": document_id,
        "receipt_id": receipt_id,
        "processed_path": recorded_path,
        "sha256": recorded_sha,
        "segment_count": segment_count,
        "evidence_reference": None if receipt_id is None else f"transcript-receipt:{receipt_id}",
        "evidence_sha256": None if receipt_id is None else recorded_sha,
    }


def _commitment_scan_evidence(
    conn: sqlite3.Connection,
    *,
    transcript: dict[str, object],
    cutoff_at: datetime,
) -> dict[str, object]:
    transcript_id = transcript.get("transcript_id")
    if not transcript.get("complete") or not isinstance(transcript_id, int):
        return {
            "complete": False,
            "verification_reason_codes": ["exact_transcript_prerequisite_missing"],
            "transcript_id": transcript_id,
            "receipt_id": None,
            "prompt_version": prompt_version_for("saydo_commitment_extract"),
            "n_extracted": None,
            "output_manifest_sha256": None,
            "evidence_reference": None,
            "evidence_sha256": None,
        }
    current_prompt = prompt_version_for("saydo_commitment_extract")
    receipt = current_commitment_scan_receipt(
        conn,
        transcript_id=transcript_id,
        prompt_version=current_prompt,
        cutoff_at=cutoff_at,
    )
    if receipt is None:
        return {
            "complete": False,
            "verification_reason_codes": ["current_prompt_scan_receipt_missing_or_invalid"],
            "transcript_id": transcript_id,
            "receipt_id": None,
            "prompt_version": current_prompt,
            "n_extracted": None,
            "output_manifest_sha256": None,
            "evidence_reference": None,
            "evidence_sha256": None,
        }
    return {
        "complete": True,
        "verification_reason_codes": [],
        "transcript_id": transcript_id,
        "receipt_id": receipt.receipt_id,
        "prompt_version": current_prompt,
        "n_extracted": receipt.n_extracted,
        "output_manifest_sha256": receipt.output_manifest_sha256,
        "evidence_reference": f"commitment-scan-receipt:{receipt.receipt_id}",
        "evidence_sha256": receipt.receipt_id,
    }


def _surprise_evidence(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: date,
    cutoff_at: datetime,
) -> dict[str, object]:
    max_release = period_end + timedelta(days=110)
    rows = conn.execute(
        "SELECT o.*,e.source_observation_id,"
        "CASE WHEN e.ticker IS o.ticker AND e.release_date IS o.release_date "
        "AND e.eps_estimate IS o.eps_estimate AND e.eps_actual IS o.eps_actual "
        "AND e.revenue_estimate IS o.revenue_estimate "
        "AND e.revenue_actual IS o.revenue_actual "
        "AND e.eps_surprise_pct IS o.eps_surprise_pct "
        "AND e.revenue_surprise_pct IS o.revenue_surprise_pct "
        "AND e.num_analysts_eps IS o.num_analysts_eps "
        "AND e.num_analysts_revenue IS o.num_analysts_revenue "
        "AND e.source_name IS o.source_name AND e.source_url IS o.source_url "
        "AND e.fetched_at IS o.fetched_at "
        "THEN 1 ELSE 0 END AS projection_matches "
        "FROM earnings_surprise_observations AS o "
        "JOIN earnings_surprises AS e ON e.source_observation_id=o.observation_id "
        "WHERE UPPER(o.ticker)=? AND date(o.release_date)>date(?) "
        "AND date(o.release_date)<=date(?) AND datetime(o.recorded_at)<=datetime(?) "
        "ORDER BY date(o.release_date),datetime(o.fetched_at),o.observation_id",
        (ticker, period_end.isoformat(), max_release.isoformat(), _iso_utc(cutoff_at)),
    ).fetchall()
    reasons: list[str] = []
    invalid_candidate_reasons: list[str] = []
    valid: list[sqlite3.Row] = []
    for row in rows:
        candidate_reasons: list[str] = []
        _, observation_reasons = verify_persisted_observation_row(row)
        candidate_reasons.extend(f"surprise_{reason}" for reason in observation_reasons)
        if str(row["provenance_status"]) != "source_observed":
            candidate_reasons.append("surprise_provenance_not_source_observed")
        if str(row["source_name"]) not in EARNINGS_SURPRISE_POLICY_PROVIDERS:
            candidate_reasons.append("surprise_source_not_admitted")
        if int(row["projection_matches"]) != 1:
            candidate_reasons.append("surprise_projection_mismatch")
        if candidate_reasons:
            invalid_candidate_reasons.extend(candidate_reasons)
            continue
        valid.append(row)
    if len(valid) != 1:
        reasons.extend(invalid_candidate_reasons)
        reasons.append(
            "surprise_observation_projection_missing" if not valid else "surprise_quarter_ambiguous"
        )
        row = None
    else:
        row = valid[0]
    observation_id = None if row is None else str(row["observation_id"])
    observation_sha = None if row is None else str(row["canonical_payload_sha256"])
    return {
        "complete": not reasons,
        "verification_reason_codes": reasons,
        "observation_id": observation_id,
        "canonical_payload_sha256": observation_sha,
        "release_date": None if row is None else str(row["release_date"]),
        "source_name": None if row is None else str(row["source_name"]),
        "evidence_reference": (
            None if observation_id is None else f"earnings-surprise-observation:{observation_id}"
        ),
        "evidence_sha256": observation_sha,
    }


def _classify_artifact(
    evidence: dict[str, object], disposition: dict[str, object] | None
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if disposition is not None and not disposition["valid"]:
        reasons.extend(cast(list[str], disposition["verification_reason_codes"]))
    if evidence["complete"]:
        if disposition is not None and disposition["status"] == "satisfied":
            if disposition["evidence_reference"] != evidence["evidence_reference"]:
                reasons.append("satisfied_disposition_reference_mismatch")
            if disposition["evidence_sha256"] != evidence["evidence_sha256"]:
                reasons.append("satisfied_disposition_sha256_mismatch")
        return ("evidence_complete" if not reasons else "invalid", sorted(set(reasons)))
    if disposition is None:
        return "missing", ["disposition_missing"]
    status = str(disposition["status"])
    if status == "satisfied":
        reasons.append("satisfied_disposition_without_valid_evidence")
        return "invalid", sorted(set(reasons))
    if status in _ACCEPTED_GAP_STATUSES and not reasons:
        return "accepted_truthful_disposition", []
    reasons.append("disposition_not_accepted_for_closure")
    return "actionable", sorted(set(reasons))


def audit_cohort(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    fiscal_year: int,
    fiscal_quarter: int,
    expected_tickers: list[str],
    cutoff_at: datetime,
) -> tuple[dict[str, object], int]:
    expected = sorted({ticker.strip().upper() for ticker in expected_tickers if ticker.strip()})
    roster = [
        str(row["ticker"]).upper()
        for row in conn.execute(
            "SELECT ticker FROM tracked_companies WHERE list_type='portfolio' "
            "AND archived_at IS NULL ORDER BY ticker"
        ).fetchall()
    ]
    roster_matches = len(expected) == 11 and expected == roster
    ticker_rows: list[dict[str, object]] = []
    counts = {
        "evidence_complete": 0,
        "accepted_truthful_disposition": 0,
        "actionable": 0,
        "missing": 0,
        "invalid": 0,
    }
    for ticker in expected:
        company = conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies WHERE UPPER(ticker)=? "
            "AND list_type='portfolio' AND archived_at IS NULL",
            (ticker,),
        ).fetchone()
        if company is None or not isinstance(company["fiscal_year_end"], str):
            artifacts = [
                {
                    "artifact_kind": artifact_kind.value,
                    "closure_state": "invalid",
                    "evidence": None,
                    "latest_disposition": None,
                    "verification_reason_codes": ["active_portfolio_target_missing_or_fye_invalid"],
                }
                for artifact_kind in _ARTIFACT_ORDER
            ]
            ticker_rows.append(
                {
                    "ticker": ticker,
                    "fiscal_year_end_month": None,
                    "target_fiscal_year": None,
                    "target_fiscal_quarter": None,
                    "period_end": None,
                    "outcome": "invalid",
                    "artifacts": artifacts,
                    "verification_reason_codes": ["active_portfolio_target_missing_or_fye_invalid"],
                }
            )
            counts["invalid"] += len(_ARTIFACT_ORDER)
            continue
        fye_month = int(str(company["fiscal_year_end"])[:2])
        target_fiscal_year, target_fiscal_quarter, period_end = _season_target(
            reporting_year=fiscal_year,
            reporting_quarter=fiscal_quarter,
            fye_month=fye_month,
        )
        transcript = _transcript_evidence(
            conn,
            repo_root=repo_root,
            ticker=ticker,
            fiscal_year=target_fiscal_year,
            fiscal_quarter=target_fiscal_quarter,
            period_end=period_end,
            cutoff_at=cutoff_at,
        )
        evidence_by_kind = {
            CoverageArtifactKind.TEXT_TRANSCRIPT: transcript,
            CoverageArtifactKind.COMMITMENT_SCAN: _commitment_scan_evidence(
                conn, transcript=transcript, cutoff_at=cutoff_at
            ),
            CoverageArtifactKind.EARNINGS_SURPRISE: _surprise_evidence(
                conn,
                ticker=ticker,
                period_end=period_end,
                cutoff_at=cutoff_at,
            ),
        }
        artifacts: list[dict[str, object]] = []
        for artifact_kind in _ARTIFACT_ORDER:
            evidence = evidence_by_kind[artifact_kind]
            disposition = _latest_disposition(
                conn,
                ticker=ticker,
                fiscal_year=target_fiscal_year,
                fiscal_quarter=target_fiscal_quarter,
                artifact_kind=artifact_kind,
                period_end=period_end,
                cutoff_at=cutoff_at,
            )
            closure_state, verification_reasons = _classify_artifact(evidence, disposition)
            artifacts.append(
                {
                    "artifact_kind": artifact_kind.value,
                    "closure_state": closure_state,
                    "evidence": evidence,
                    "latest_disposition": disposition,
                    "verification_reason_codes": verification_reasons,
                }
            )
            counts[closure_state] += 1
        scan = artifacts[1]
        if (
            scan["closure_state"] == "accepted_truthful_disposition"
            and cast(dict[str, Any], scan["latest_disposition"])["status"] == "source_unavailable"
        ):
            transcript_disposition = cast(dict[str, Any] | None, artifacts[0]["latest_disposition"])
            if transcript_disposition is None or transcript_disposition["status"] not in {
                "source_unavailable",
                "policy_blocked",
            }:
                counts["accepted_truthful_disposition"] -= 1
                counts["invalid"] += 1
                scan["closure_state"] = "invalid"
                scan["verification_reason_codes"] = ["scan_gap_contradicts_transcript_state"]
        ticker_outcome = (
            "incomplete_or_invalid"
            if any(
                item["closure_state"] in {"invalid", "missing", "actionable"} for item in artifacts
            )
            else (
                "closed_with_accounted_dispositions"
                if any(
                    item["closure_state"] == "accepted_truthful_disposition" for item in artifacts
                )
                else "complete"
            )
        )
        ticker_rows.append(
            {
                "ticker": ticker,
                "fiscal_year_end_month": fye_month,
                "target_fiscal_year": target_fiscal_year,
                "target_fiscal_quarter": target_fiscal_quarter,
                "period_end": period_end.isoformat(),
                "outcome": ticker_outcome,
                "artifacts": artifacts,
                "verification_reason_codes": [],
            }
        )
    if not roster_matches or counts["invalid"] or counts["missing"] or counts["actionable"]:
        outcome = "incomplete_or_invalid"
        rc = 2
    elif counts["accepted_truthful_disposition"]:
        outcome = "closed_with_accounted_dispositions"
        rc = 1
    else:
        outcome = "complete"
        rc = 0
    body: dict[str, object] = {
        "schema_version": "quarterly-data-coverage-audit@1",
        "outcome": outcome,
        "cutoff_at": _iso_utc(cutoff_at),
        "reporting_season_year": fiscal_year,
        "reporting_season_quarter": fiscal_quarter,
        "expected_tickers": expected,
        "active_portfolio_tickers": roster,
        "roster_matches": roster_matches,
        "ticker_set_sha256": _sha256(_canonical_json(expected)),
        "target_count": len(expected) * len(_ARTIFACT_ORDER),
        "counts": counts,
        "tickers": ticker_rows,
    }
    body["target_set_sha256"] = _sha256(
        _canonical_json(
            [
                [
                    ticker_row["ticker"],
                    ticker_row["target_fiscal_year"],
                    ticker_row["target_fiscal_quarter"],
                    ticker_row["period_end"],
                    artifact_kind.value,
                ]
                for ticker_row in ticker_rows
                for artifact_kind in _ARTIFACT_ORDER
            ]
        )
    )
    body["report_sha256"] = _sha256(_canonical_json(body))
    return body, rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--fiscal-year",
        type=int,
        required=True,
        help="Calendar reporting-season year; each issuer fiscal target is derived from FYE",
    )
    parser.add_argument(
        "--fiscal-quarter",
        type=int,
        choices=(1, 2, 3, 4),
        required=True,
        help="Calendar reporting-season quarter; each issuer fiscal target is derived from FYE",
    )
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument("--cutoff-at", required=True)
    args = parser.parse_args(argv)
    try:
        cutoff_at = _parse_cutoff(args.cutoff_at)
        with connect_sqlite(
            args.db.resolve(),
            role=SQLiteConnectionRole.READ_ONLY,
            schema_preflight=False,
        ) as conn:
            conn.execute("BEGIN")
            report, rc = audit_cohort(
                conn,
                repo_root=args.repo_root.resolve(),
                fiscal_year=args.fiscal_year,
                fiscal_quarter=args.fiscal_quarter,
                expected_tickers=args.ticker,
                cutoff_at=cutoff_at,
            )
            conn.rollback()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        report = {
            "schema_version": "quarterly-data-coverage-audit@1",
            "outcome": "incomplete_or_invalid",
            "error": f"{type(exc).__name__}: {exc}",
        }
        report["report_sha256"] = _sha256(_canonical_json(report))
        rc = 2
    print(_canonical_json(report))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
