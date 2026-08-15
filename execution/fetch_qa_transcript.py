"""
execution/fetch_qa_transcript.py
--------------------------------
Fetch ONLY the Q&A segment of an earnings call from free, no-auth aggregator
sites (roic.ai → stockanalysis.com → tickertrends.io). First hit wins.

Why Q&A only:
  - Prepared remarks are reproducible from the press release + investor deck,
    which `execution/process_ir_documents.py` already summarizes via
    `generate_press_release_summary` / `generate_presentation_brief`.
  - The Q&A segment is the unique audio-only content — analysts probing the
    edges of management's prepared message — and is exactly what say-do
    consistency analysis needs.
  - Aggregators publish Q&A pre-segmented and speaker-tagged, which is
    structurally cleaner than diarizing audio.

Output:
  transcripts/raw/<TICKER>_Q<N>_<YEAR>.txt — synthesizer-banner header + raw
  Q&A text. Registered with source=aggregator_<name>; QA validator routes to
  the synthesized-flavor checks.

Use as the PRIMARY pull path; `fetch_audio_transcripts.py` becomes the audio
fallback for quarters not yet indexed by any aggregator.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.append(str(SRC_DIR))

import db  # noqa: E402
import index_manager  # noqa: E402
from aggregator_sources import (  # noqa: E402
    SOURCES,
    AggregatorHit,
    AggregatorSource,
)
from alias_manager import resolve_ticker  # noqa: E402
from pipeline.transcript_acquisition import (  # noqa: E402
    COMBINED_SOURCE_REGIME_IDENTITY,
    AuthorizedTranscriptArtifact,
    TranscriptAcquisitionDeniedError,
    authorize_transcript_request,
    load_authorized_transcript_replay,
    persist_authorized_transcript_artifact,
    project_root_for_database,
    read_authorized_transcript,
    stage_authorized_payload,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402
from transcript_qa import (  # noqa: E402
    validate_synthesized_transcript,
)
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    ExistingArtifactBehavior,
    TranscriptAcquisitionAuthorization,
    TranscriptAcquisitionEntrypoint,
    TranscriptAcquisitionRequest,
    TranscriptAuthorizationStatus,
)

RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
STAGING_DIR = PROJECT_ROOT / ".tmp" / "transcript-acquisition"


def _policy_today() -> date:
    return date.today()


class FetchQaSpec(BaseModel):
    ticker: str
    year: int = Field(ge=2000, le=2100)
    quarter: int = Field(ge=1, le=4)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


@dataclass(frozen=True)
class FetchQaResult:
    ticker: str
    year: int
    quarter: int
    output_path: Path
    source_name: str
    page_url: str
    authorization: TranscriptAcquisitionAuthorization
    acquired_artifact: AuthorizedTranscriptArtifact


class FetchQaAttemptStatus(StrEnum):
    DENIED = "denied"
    PROVIDER_MISS = "provider_miss"
    ACQUIRED = "acquired"


class FetchQaStatus(StrEnum):
    DENIED = "denied"
    PROVIDER_MISS = "provider_miss"
    ACQUIRED = "acquired"
    IDEMPOTENT_REPLAY = "idempotent_replay"


@dataclass(frozen=True)
class FetchQaAttempt:
    provider: str
    status: FetchQaAttemptStatus
    idempotency_key: str


@dataclass(frozen=True)
class FetchQaOutcome:
    status: FetchQaStatus
    idempotency_key: str
    attempts: tuple[FetchQaAttempt, ...]
    result: FetchQaResult | None = None


class TranscriptCollectionPolicyError(TranscriptAcquisitionDeniedError):
    """The stored role or reported-quarter window denied a network fetch."""


def _request_for_source(
    spec: FetchQaSpec,
    *,
    source: object,
    owner_requested: bool,
    as_of: date,
) -> TranscriptAcquisitionRequest:
    from aggregator_sources import AggregatorSource

    if not isinstance(source, AggregatorSource):
        raise TypeError("source must be an AggregatorSource")
    canonical = resolve_ticker(spec.ticker)
    return TranscriptAcquisitionRequest(
        entrypoint=TranscriptAcquisitionEntrypoint.FETCH_QA_TRANSCRIPT,
        canonical_ticker=canonical,
        fiscal_year=spec.year,
        fiscal_quarter=spec.quarter,
        as_of=as_of,
        source_type=source.source_type,
        document_type=source.document_type,
        provider=source.provider,
        owner_requested=owner_requested,
        existing_artifact=False,
        existing_artifact_behavior=ExistingArtifactBehavior.REFRESH,
        source_policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
        source_regime_identity=COMBINED_SOURCE_REGIME_IDENTITY,
    )


def _log_denial(authorization: TranscriptAcquisitionAuthorization) -> None:
    sys.stderr.write(
        json.dumps(
            {
                "event": "source_collection_policy_denied",
                "ticker": authorization.request.canonical_ticker,
                "entrypoint": authorization.request.entrypoint.value,
                "provider": authorization.request.provider.value,
                "reason": authorization.reason.value,
                "idempotency_key": authorization.idempotency_key,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _build_header(spec: FetchQaSpec, hit: AggregatorHit) -> str:
    """Build the file's banner header.

    Deliberately excludes a wall-clock fetch timestamp: this header is
    hashed (`sha256_of`) to decide whether a re-fetch is byte-identical to
    what's already ingested. A `Built at: {datetime.now()}` line used to
    live here and made every re-fetch hash differently even when the
    underlying Q&A text hadn't changed, defeating that idempotency check
    (root cause of the 2026-07-25 transcript-duplication incident — see
    `execution/dedupe_transcripts.py`). Fetch time is already tracked in
    `.tmp/transcript_index.json`'s `indexed_at`; it doesn't need to also be
    inside the hashed file content.
    """
    canonical = resolve_ticker(spec.ticker)
    source_label = (
        hit.source_name if hit.source_name == "issuer_ir" else f"aggregator_{hit.source_name}"
    )
    return (
        f"=== SYNTHESIZED QUARTERLY UPDATE — Q&A SEGMENT ONLY ===\n"
        f"Generated by execution/fetch_qa_transcript.py from an authorized transcript source.\n"
        f"This file contains the QUESTION-AND-ANSWER segment only — prepared\n"
        f"remarks are reproducible from the press release + investor deck and\n"
        f"are excluded here to keep the input focused for say-do analysis.\n"
        f"\n"
        f"Ticker:    {canonical}\n"
        f"Period:    Q{spec.quarter} {spec.year}\n"
        f"Source:    {source_label} ({hit.page_url})\n"
        f"\n"
        f"=== Q&A SEGMENT ===\n"
    )


def _replay_artifact(
    conn: sqlite3.Connection,
    *,
    request: TranscriptAcquisitionRequest,
    project_root: Path,
) -> tuple[AuthorizedTranscriptArtifact, bytes] | None:
    try:
        artifact = load_authorized_transcript_replay(
            conn,
            request=request,
            project_root=project_root,
            trusted_staging_root=STAGING_DIR,
        )
        if artifact is None:
            return None
        staged_bytes = read_authorized_transcript(
            conn,
            artifact,
            project_root=project_root,
            trusted_staging_root=STAGING_DIR,
        )
    except (OSError, ValueError, TranscriptAcquisitionDeniedError):
        return None
    return artifact, staged_bytes


def _restore_replay(
    spec: FetchQaSpec,
    *,
    output_path: Path,
    artifact: AuthorizedTranscriptArtifact,
    staged_bytes: bytes,
) -> None:
    if not output_path.is_file() or output_path.read_bytes() != staged_bytes:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(staged_bytes)
    qa_result = validate_synthesized_transcript(output_path)
    index_manager.register_transcript(
        resolve_ticker(spec.ticker),
        spec.year,
        f"Q{spec.quarter}",
        source="issuer_ir",
        filepath=output_path.name,
        has_qa=True,
        qa_status=qa_result.status.value,
        qa_details=qa_result.model_dump(mode="json"),
        acquisition_receipt=artifact,
    )


def fetch_qa(
    spec: FetchQaSpec,
    force: bool = False,
    *,
    db_path: Path,
    owner_requested: bool,
    as_of: date | None = None,
) -> FetchQaOutcome:
    canonical = resolve_ticker(spec.ticker)
    qlabel = f"Q{spec.quarter}"
    output_path = RAW_DIR / f"{canonical}_{qlabel}_{spec.year}.txt"
    effective_as_of = _policy_today() if as_of is None else as_of
    attempts: list[FetchQaAttempt] = []
    last_key = "transcript:" + "0" * 64
    project_root = project_root_for_database(db_path)
    authorized_sources: list[
        tuple[AggregatorSource, TranscriptAcquisitionRequest, TranscriptAcquisitionAuthorization]
    ] = []
    with connect_sqlite(
        db_path,
        role=SQLiteConnectionRole.READ_ONLY,
        schema_preflight=False,
    ) as conn:
        for source in SOURCES:
            request = _request_for_source(
                spec,
                source=source,
                owner_requested=owner_requested,
                as_of=effective_as_of,
            )
            authorization = authorize_transcript_request(conn, request)
            last_key = authorization.idempotency_key
            if authorization.status is TranscriptAuthorizationStatus.DENIED:
                _log_denial(authorization)
                attempts.append(FetchQaAttempt(source.name, FetchQaAttemptStatus.DENIED, last_key))
                continue
            replay = _replay_artifact(conn, request=request, project_root=project_root)
            if replay is not None and not force:
                artifact, staged_bytes = replay
                _restore_replay(
                    spec,
                    output_path=output_path,
                    artifact=artifact,
                    staged_bytes=staged_bytes,
                )
                return FetchQaOutcome(
                    FetchQaStatus.IDEMPOTENT_REPLAY,
                    last_key,
                    tuple(attempts),
                )
            authorized_sources.append((source, request, authorization))

    for source, request, authorization in authorized_sources:
        last_key = authorization.idempotency_key
        hit = source.fetch_qa(canonical, spec.year, spec.quarter)
        if hit is None:
            attempts.append(
                FetchQaAttempt(source.name, FetchQaAttemptStatus.PROVIDER_MISS, last_key)
            )
            continue
        payload = (_build_header(spec, hit) + hit.qa_text).encode("utf-8")
        acquired_artifact = stage_authorized_payload(
            authorization,
            payload=payload,
            private_root=STAGING_DIR,
            source_url=hit.page_url,
            canonical_document_path=Path("transcripts") / "raw" / output_path.name,
        )
        with connect_sqlite(
            db_path,
            role=SQLiteConnectionRole.WRITER,
            schema_preflight=False,
        ) as conn:
            if authorize_transcript_request(conn, request) != authorization:
                raise TranscriptAcquisitionDeniedError(
                    "transcript authorization changed before durable persistence"
                )
            staged_bytes = read_authorized_transcript(
                conn,
                acquired_artifact,
                project_root=project_root,
                trusted_staging_root=STAGING_DIR,
            )
            persist_authorized_transcript_artifact(
                conn,
                acquired_artifact,
                project_root=project_root,
                trusted_staging_root=STAGING_DIR,
            )
            conn.commit()
        _restore_replay(
            spec,
            output_path=output_path,
            artifact=acquired_artifact,
            staged_bytes=staged_bytes,
        )
        attempts.append(FetchQaAttempt(source.name, FetchQaAttemptStatus.ACQUIRED, last_key))
        result = FetchQaResult(
            ticker=canonical,
            year=spec.year,
            quarter=spec.quarter,
            output_path=output_path,
            source_name=hit.source_name,
            page_url=hit.page_url,
            authorization=authorization,
            acquired_artifact=acquired_artifact,
        )
        return FetchQaOutcome(FetchQaStatus.ACQUIRED, last_key, tuple(attempts), result)

    if attempts and all(item.status is FetchQaAttemptStatus.DENIED for item in attempts):
        return FetchQaOutcome(FetchQaStatus.DENIED, last_key, tuple(attempts))
    tried = [attempt.provider for attempt in attempts]
    if attempts:
        print(
            f"[miss] {canonical} {qlabel} {spec.year}: no aggregator hit. "
            f"Tried: {', '.join(tried)}. "
            "No policy-approved audio/webcast fallback is available."
        )
    return FetchQaOutcome(FetchQaStatus.PROVIDER_MISS, last_key, tuple(attempts))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch the Q&A segment of an earnings call from authorized sources."
    )
    parser.add_argument("--ticker", required=True, help="Stock ticker (e.g. NOW)")
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--quarter", required=True, type=int, choices=[1, 2, 3, 4])
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(db.DB_PATH),
        help="portfolio.db containing the active stored company role",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing transcript file.",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print the configured source chain and exit.",
    )
    args = parser.parse_args()

    if args.list_sources:
        print("Transcript source chain (policy checked in priority order):")
        for s in SOURCES:
            print(f"  - {s.name}")
        return

    try:
        spec = FetchQaSpec(ticker=args.ticker, year=args.year, quarter=args.quarter)
    except ValidationError as e:
        parser.error(str(e))

    fetch_qa(spec, force=args.force, db_path=args.db, owner_requested=True)


if __name__ == "__main__":
    main()
