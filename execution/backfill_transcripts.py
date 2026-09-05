"""Policy-bounded text-transcript backfill and commitment extraction.

Scheduled runs cover non-archived portfolio names. An evaluation name runs only
when explicitly selected with ``--ticker``; watchlist and index members do not
enter transcript collection.

  1. Compute the last N (default and maximum 5) fiscal-quarter end dates that have already
     passed, using `tracked_companies.fiscal_year_end` to map fiscal-quarter
     index → calendar quarter end.
  2. For each period with no exact DB/path/SHA evidence receipt, invoke
     `fetch_qa_transcript.fetch_qa()` to reacquire or replay an authorized Q&A
     artifact. Unreceipted local files never bypass source authorization.
  3. After acquisition, invoke `execution/ingest_transcripts.py` separately
     for each ticker with a new artifact. A quarantined peer ticker cannot
     block the rest of the portfolio batch (ingest remains idempotent on sha256).
  4. For each exact transcript in the configured recent-quarter window that
     lacks a durable scan receipt, invoke
     `execution/extract_commitments_from_transcript.py --auto --transcript-id X`.
     Out-of-window historical transcripts are not admitted to this job.

The script is idempotent at every layer:
  - Exact DB/path/SHA evidence skips a period already ingested
  - Aggregator misses are logged but tolerated. Audio/webcast extraction is
    excluded by source policy; this runner fetches text transcripts only.
  - `ingest_transcripts.py` is sha256-keyed
  - `extract_commitments --auto` skips transcripts that already have commitments

Designed to run unattended:
  - Hooked into `execution/onboard_ticker.py` (final stage; fire-and-forget)
  - Cron entry point at `cron/backfill_transcripts.task.xml` (daily 02:00,
    before the earnings-calendar fetcher at 05:45).
  - `--repo-root` is honored by both subprocess phases (ingest + extract):
    they invoke the resolved root's copy of the script with `cwd=<repo_root>`
    so worktree-based runs land on the main repo's DB and transcripts dir.

Usage:
    python execution/backfill_transcripts.py                       # automatic portfolio tickers
    python execution/backfill_transcripts.py --ticker NTDOY
    python execution/backfill_transcripts.py --lookback-quarters 5
    python execution/backfill_transcripts.py --skip-extract        # fetch + ingest only
    python execution/backfill_transcripts.py --dry-run             # plan only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.evidence_snapshot import snapshot_recorded_evidence  # noqa: E402
from llm.prompt_versions import prompt_version_for  # noqa: E402
from models.companies import ListType  # noqa: E402
from pipeline.commitment_scan_receipts import (  # noqa: E402
    current_commitment_scan_receipt,
)
from pipeline.data_coverage_dispositions import (  # noqa: E402
    COMMITMENT_SCAN_POLICY_NAME,
    COMMITMENT_SCAN_POLICY_PROVIDERS,
    COMMITMENT_SCAN_POLICY_VERSION,
    CoverageArtifactKind,
    CoverageAttempt,
    CoverageAttemptStatus,
    CoverageDispositionStatus,
    DataCoverageDispositionRequest,
    append_data_coverage_disposition,
    policy_config_sha256,
)
from pipeline.source_policy import (  # noqa: E402
    SOURCE_POLICY_CONFIG,
    ArtifactKind,
    CollectionSource,
    CollectionTarget,
    select_collection_targets,
)
from pipeline.transcript_acquisition import (  # noqa: E402
    TranscriptAcquisitionDeniedError,
)
from provenance.selection import selected_transcripts_relation  # noqa: E402
from runtime.python_process import managed_python_prefix  # noqa: E402
from transcripts.acquisition_semantics import (  # noqa: E402
    TRANSCRIPT_ACQUISITION_POLICY_VERSION,
)

# Sibling scripts in execution/ — needed when this module is imported (e.g.
# from tests) rather than run directly via `python execution/backfill_transcripts.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_qa_transcript as fetch_qa_transcript_module  # type: ignore[import-not-found]  # noqa: E402
from fetch_qa_transcript import (  # type: ignore[import-not-found]  # noqa: E402
    SOURCES as TRANSCRIPT_SOURCES,
)
from fetch_qa_transcript import (  # type: ignore[import-not-found]  # noqa: E402
    FetchQaAttemptStatus,
    FetchQaSpec,
    FetchQaStatus,
    fetch_qa,
)

import db  # noqa: E402

_RAW_DIR = PROJECT_ROOT / "transcripts" / "raw"
_PROCESSED_DIR = PROJECT_ROOT / "transcripts" / "processed"
_DEFAULT_LOOKBACK = SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters


def _retarget_paths(repo_root: Path) -> None:
    """Override db module paths AND this script's dir constants so all reads
    hit `repo_root` instead of this script's parent. Lets worktree-based runs
    target the main repo's data dir without copying the DB."""
    global _RAW_DIR, _PROCESSED_DIR
    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")
    _RAW_DIR = repo_root / "transcripts" / "raw"
    _PROCESSED_DIR = repo_root / "transcripts" / "processed"
    fetch_qa_transcript_module.RAW_DIR = _RAW_DIR
    fetch_qa_transcript_module.STAGING_DIR = repo_root / ".tmp" / "transcript-acquisition"


def quarter_end_date(fiscal_year: int, fiscal_quarter: int, fye_month: int) -> date:
    """Calendar date when fiscal Q<q> of fiscal year <fy> ends.

    Convention: `fiscal_year` is the calendar year in which the fiscal year
    ENDS (FY2026 for a March-FYE company ends 2026-03-31; its Q1 ends 9
    months earlier — 2025-06-30).
    """
    months_before_fye = (4 - fiscal_quarter) * 3
    year = fiscal_year
    month = fye_month - months_before_fye
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    last_day = monthrange(year, month)[1]
    return date(year, month, last_day)


def recent_fiscal_quarters(fye_month: int, today: date, n: int) -> list[tuple[int, int]]:
    """Return up to `n` (fiscal_year, fiscal_quarter) pairs whose period_end
    is <= today, most recent first."""
    out: list[tuple[int, int]] = []
    # Walk fiscal years from a year ahead (Apple's FYE 9 means Q1 of next
    # fiscal year can end in the current calendar year) backwards.
    for y in range(today.year + 1, today.year - _DEFAULT_LOOKBACK, -1):
        for q in (4, 3, 2, 1):
            end = quarter_end_date(y, q, fye_month)
            if end <= today:
                out.append((y, q))
                if len(out) == n:
                    return out
    return out


@dataclass(frozen=True)
class FetchedTranscriptIdentity:
    label: str
    receipt_id: str
    canonical_document_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class TranscriptArtifactConflict:
    label: str
    receipt_id: str
    reason_code: str


@dataclass(frozen=True)
class CommitmentScanTarget:
    ticker: str
    fye_month: int
    fiscal_year: int
    fiscal_quarter: int
    transcript_id: int


@dataclass
class TickerBackfillResult:
    ticker: str
    fye_month: int
    fetched: list[str] = field(default_factory=list[str])
    skipped_existing: list[str] = field(default_factory=list[str])
    aggregator_misses: list[str] = field(default_factory=list[str])
    errors: list[str] = field(default_factory=list[str])
    coverage_dispositions: list[str] = field(default_factory=list[str])
    commitment_scan_dispositions: list[str] = field(default_factory=list[str])
    fetched_artifacts: list[FetchedTranscriptIdentity] = field(
        default_factory=list[FetchedTranscriptIdentity]
    )
    artifact_conflicts: list[TranscriptArtifactConflict] = field(
        default_factory=list[TranscriptArtifactConflict]
    )


@dataclass(frozen=True)
class TranscriptEvidence:
    reference: str
    sha256: str


def _qlabel(year: int, quarter: int) -> str:
    return f"Q{quarter}_{year}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_processed_path_conflicts(identity: FetchedTranscriptIdentity) -> bool:
    processed_relative = f"transcripts/processed/{Path(identity.canonical_document_path).name}"
    conn = db.get_connection()
    try:
        recorded = {
            str(row["sha256"])
            for row in conn.execute(
                "SELECT sha256 FROM documents WHERE file_path=?", (processed_relative,)
            ).fetchall()
        }
    finally:
        conn.close()
    if recorded and recorded != {identity.sha256}:
        return True
    processed_path = Path(db.PROJECT_ROOT).resolve() / processed_relative
    return processed_path.exists() and _sha256(processed_path) != identity.sha256


def _recorded_evidence_path(root: Path, recorded: str) -> Path | None:
    candidate = Path(recorded)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "transcripts" or parts[1] not in {"raw", "processed"}:
        return None
    intended = (root / parts[0] / parts[1]).resolve()
    lexical = root / candidate
    current = lexical
    while True:
        try:
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        if current.is_symlink() or (isinstance(attributes, int) and bool(attributes & 0x400)):
            return None
        if current == root:
            break
        if root not in current.parents:
            return None
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(intended)
        return resolved
    except (OSError, ValueError):
        return None


def _has_ingested_evidence(ticker: str, year: int, quarter: int, fye_month: int) -> bool:
    """Require current segments, processed bytes, and exact authorized receipt."""

    return _ingested_evidence(ticker, year, quarter, fye_month) is not None


def _transcript_rows_exist(ticker: str, year: int, quarter: int, fye_month: int) -> bool:
    period_end = quarter_end_date(year, quarter, fye_month).isoformat()
    conn = db.get_connection()
    try:
        relation = selected_transcripts_relation(conn).sql
        return (
            conn.execute(
                f"SELECT 1 FROM {relation} WHERE UPPER(ticker)=? "  # nosec B608
                "AND is_current=1 AND fiscal_period_type=? "
                "AND date(period_end)=date(?) LIMIT 1",
                (ticker.upper(), f"Q{quarter}", period_end),
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def _ingested_evidence(
    ticker: str, year: int, quarter: int, fye_month: int
) -> TranscriptEvidence | None:
    period_end = quarter_end_date(year, quarter, fye_month).isoformat()
    conn = db.get_connection()
    try:
        try:
            rows = conn.execute(
                "SELECT d.id, d.file_path, d.sha256, r.receipt_id FROM documents AS d "
                "JOIN transcripts AS t ON t.document_id=d.id "
                "JOIN transcript_acquisition_receipts AS r "
                "ON r.canonical_ticker=UPPER(t.ticker) "
                "AND r.fiscal_year=? AND r.fiscal_quarter=? "
                "AND r.artifact_sha256=d.sha256 "
                "AND (r.document_id IS NULL OR r.document_id=d.id) "
                "WHERE UPPER(d.ticker)=? AND UPPER(t.ticker)=? "
                "AND t.fiscal_period_type=? AND date(t.period_end)=date(?) "
                "AND t.is_current=1 AND d.file_path=? "
                "AND r.provider='issuer_ir' AND r.source_type='ir_doc' "
                "AND r.document_type='earnings_call_transcript' "
                "AND r.canonical_document_path=? "
                "AND EXISTS ("
                "SELECT 1 FROM transcript_segments AS s WHERE s.transcript_id=t.id"
                ") ORDER BY d.id DESC, r.recorded_at DESC, r.receipt_id DESC",
                (
                    year,
                    quarter,
                    ticker.upper(),
                    ticker.upper(),
                    f"Q{quarter}",
                    period_end,
                    f"transcripts/processed/{ticker.upper()}_Q{quarter}_{year}.txt",
                    f"transcripts/raw/{ticker.upper()}_Q{quarter}_{year}.txt",
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()
    root = Path(db.PROJECT_ROOT).resolve()
    for row in rows:
        snapshot = snapshot_recorded_evidence(root, str(row["file_path"]))
        if snapshot is not None and snapshot.sha256 == str(row["sha256"]):
            return TranscriptEvidence(
                reference=f"transcript-receipt:{row['receipt_id']}",
                sha256=snapshot.sha256,
            )
    return None


_TRANSCRIPT_POLICY_SHA256 = policy_config_sha256(
    policy_name="transcript_acquisition",
    policy_version=TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    providers=tuple(source.name for source in TRANSCRIPT_SOURCES),
)


def _coverage_attempts(hit: object) -> tuple[CoverageAttempt, ...]:
    attempts = getattr(hit, "attempts", ())
    mapped: list[CoverageAttempt] = []
    statuses = {
        FetchQaAttemptStatus.DENIED: CoverageAttemptStatus.POLICY_DENIED,
        FetchQaAttemptStatus.PROVIDER_MISS: CoverageAttemptStatus.AUTHORIZED_MISS,
        FetchQaAttemptStatus.ACQUIRED: CoverageAttemptStatus.ACQUIRED,
        FetchQaAttemptStatus.IDEMPOTENT_REPLAY: CoverageAttemptStatus.IDEMPOTENT_REPLAY,
    }
    for attempt in attempts:
        mapped.append(
            CoverageAttempt(
                provider=str(attempt.provider),
                status=statuses[attempt.status],
                authorization_key=str(attempt.idempotency_key),
            )
        )
    return tuple(mapped)


def _persist_coverage_disposition(
    *,
    ticker: str,
    year: int,
    quarter: int,
    fye_month: int,
    status: CoverageDispositionStatus,
    reason_code: str,
    attempts: tuple[CoverageAttempt, ...],
    observed_at: datetime,
    evidence: TranscriptEvidence | None = None,
    retry_after: datetime | None = None,
    artifact_kind: CoverageArtifactKind = CoverageArtifactKind.TEXT_TRANSCRIPT,
    policy_name: str = "transcript_acquisition",
    policy_version: str = TRANSCRIPT_ACQUISITION_POLICY_VERSION,
    policy_config_sha256_override: str | None = None,
) -> str:
    conn = db.get_connection()
    try:
        disposition = append_data_coverage_disposition(
            conn,
            DataCoverageDispositionRequest(
                artifact_kind=artifact_kind,
                ticker=ticker,
                fiscal_year=year,
                fiscal_quarter=quarter,
                period_end=quarter_end_date(year, quarter, fye_month),
                status=status,
                reason_code=reason_code,
                attempts=attempts,
                policy_name=policy_name,
                policy_version=policy_version,
                policy_config_sha256=(
                    _TRANSCRIPT_POLICY_SHA256
                    if policy_config_sha256_override is None
                    else policy_config_sha256_override
                ),
                evidence_reference=None if evidence is None else evidence.reference,
                evidence_sha256=None if evidence is None else evidence.sha256,
                observed_at=observed_at,
                retry_after=retry_after,
            ),
        )
        conn.commit()
        return disposition.request.status.value
    finally:
        conn.close()


def _backfill_one(
    ticker: str,
    fye_month: int,
    lookback: int,
    today: date,
    dry_run: bool,
    db_path: Path,
    owner_requested: bool,
) -> TickerBackfillResult:
    if lookback < 1 or lookback > _DEFAULT_LOOKBACK:
        raise ValueError(f"lookback must be between 1 and {_DEFAULT_LOOKBACK}")
    result = TickerBackfillResult(ticker=ticker, fye_month=fye_month)
    quarters = recent_fiscal_quarters(fye_month, today, lookback)
    for y, q in quarters:
        label = _qlabel(y, q)
        observed_at = datetime.now(UTC)
        if _has_ingested_evidence(ticker, y, q, fye_month):
            result.skipped_existing.append(label)
            if dry_run:
                continue
            try:
                evidence = _ingested_evidence(ticker, y, q, fye_month)
                if evidence is None:
                    raise RuntimeError("exact ingested evidence disappeared before disposition")
                result.coverage_dispositions.append(
                    f"{label}:"
                    + _persist_coverage_disposition(
                        ticker=ticker,
                        year=y,
                        quarter=q,
                        fye_month=fye_month,
                        status=CoverageDispositionStatus.SATISFIED,
                        reason_code="exact_db_path_sha_evidence",
                        attempts=(
                            CoverageAttempt(
                                provider="canonical_store",
                                status=CoverageAttemptStatus.EVIDENCE_PRESENT,
                            ),
                        ),
                        observed_at=observed_at,
                        evidence=evidence,
                    )
                )
            except Exception as exc:
                result.errors.append(
                    f"{label}: coverage disposition: {type(exc).__name__}: {exc}"[:200]
                )
            continue
        if dry_run:
            result.aggregator_misses.append(f"{label} [dry-run]")
            continue
        try:
            hit = fetch_qa(
                FetchQaSpec(ticker=ticker, year=y, quarter=q),
                force=False,
                db_path=db_path,
                owner_requested=owner_requested,
            )
        except TranscriptAcquisitionDeniedError as e:
            result.errors.append(f"{label}: {type(e).__name__}: {e}"[:200])
            try:
                result.coverage_dispositions.append(
                    f"{label}:"
                    + _persist_coverage_disposition(
                        ticker=ticker,
                        year=y,
                        quarter=q,
                        fye_month=fye_month,
                        status=CoverageDispositionStatus.POLICY_BLOCKED,
                        reason_code="transcript_source_policy_denied",
                        attempts=(
                            CoverageAttempt(
                                provider="transcript_chain",
                                status=CoverageAttemptStatus.POLICY_DENIED,
                            ),
                        ),
                        observed_at=observed_at,
                    )
                )
            except Exception as disposition_exc:
                result.errors.append(
                    f"{label}: coverage disposition: {type(disposition_exc).__name__}: "
                    f"{disposition_exc}"[:200]
                )
            continue
        except Exception as e:
            result.errors.append(f"{label}: {type(e).__name__}: {e}"[:200])
            try:
                result.coverage_dispositions.append(
                    f"{label}:"
                    + _persist_coverage_disposition(
                        ticker=ticker,
                        year=y,
                        quarter=q,
                        fye_month=fye_month,
                        status=CoverageDispositionStatus.OPERATIONAL_ERROR,
                        reason_code="transcript_acquisition_exception",
                        attempts=(
                            CoverageAttempt(
                                provider="transcript_chain",
                                status=CoverageAttemptStatus.FAILED,
                            ),
                        ),
                        observed_at=observed_at,
                        retry_after=observed_at + timedelta(days=1),
                    )
                )
            except Exception as disposition_exc:
                result.errors.append(
                    f"{label}: coverage disposition: {type(disposition_exc).__name__}: "
                    f"{disposition_exc}"[:200]
                )
            continue
        if hit.status in {FetchQaStatus.ACQUIRED, FetchQaStatus.IDEMPOTENT_REPLAY}:
            if hit.result is None:
                result.errors.append(f"{label}: acquired transcript omitted exact receipt identity")
                continue
            identity = FetchedTranscriptIdentity(
                label=label,
                receipt_id=hit.result.receipt_id,
                canonical_document_path=hit.result.acquired_artifact.canonical_document_path.as_posix(),
                sha256=hit.result.acquired_artifact.sha256,
                size_bytes=hit.result.acquired_artifact.size_bytes,
            )
            if _canonical_processed_path_conflicts(identity):
                reason = "reacquired_transcript_conflicts_with_canonical_bytes"
                attempts = (
                    *_coverage_attempts(hit),
                    CoverageAttempt(
                        provider="canonical_processed_path",
                        status=CoverageAttemptStatus.FAILED,
                    ),
                )
                try:
                    result.coverage_dispositions.append(
                        f"{label}:"
                        + _persist_coverage_disposition(
                            ticker=ticker,
                            year=y,
                            quarter=q,
                            fye_month=fye_month,
                            status=CoverageDispositionStatus.OPERATIONAL_ERROR,
                            reason_code=reason,
                            attempts=attempts,
                            observed_at=observed_at,
                            retry_after=observed_at + timedelta(days=1),
                        )
                    )
                    result.artifact_conflicts.append(
                        TranscriptArtifactConflict(
                            label=label,
                            receipt_id=identity.receipt_id,
                            reason_code=reason,
                        )
                    )
                except Exception as exc:
                    result.errors.append(
                        f"{label}: coverage disposition: {type(exc).__name__}: {exc}"[:200]
                    )
                continue
            result.fetched.append(label)
            result.fetched_artifacts.append(identity)
            # Acquisition is not completeness. A final disposition is written
            # only after ingest proves current segments, the exact processed
            # bytes, and their authorized acquisition receipt.
            continue
        if _transcript_rows_exist(ticker, y, q, fye_month):
            evidence = None
            status = CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING
            reason = "canonical_transcript_evidence_missing"
        elif hit.status == FetchQaStatus.DENIED:
            result.errors.append(f"{label}: transcript acquisition denied")
            evidence = None
            status = CoverageDispositionStatus.POLICY_BLOCKED
            reason = "transcript_source_policy_denied"
        else:
            result.aggregator_misses.append(label)
            evidence = None
            status = CoverageDispositionStatus.SOURCE_UNAVAILABLE
            reason = "authorized_text_transcript_unavailable"
        try:
            result.coverage_dispositions.append(
                f"{label}:"
                + _persist_coverage_disposition(
                    ticker=ticker,
                    year=y,
                    quarter=q,
                    fye_month=fye_month,
                    status=status,
                    reason_code=reason,
                    attempts=_coverage_attempts(hit),
                    observed_at=observed_at,
                    evidence=evidence,
                    retry_after=(
                        observed_at + timedelta(days=7)
                        if status is CoverageDispositionStatus.SOURCE_UNAVAILABLE
                        else None
                    ),
                )
            )
        except Exception as exc:
            result.errors.append(
                f"{label}: coverage disposition: {type(exc).__name__}: {exc}"[:200]
            )
    return result


def _resolve_tickers(arg_ticker: str | None) -> list[tuple[str, int]]:
    """Return policy-authorized transcript work in company-priority order."""
    conn = db.get_connection()
    try:
        if arg_ticker:
            cur = conn.execute(
                "SELECT ticker, fiscal_year_end, list_type FROM tracked_companies "
                "WHERE ticker = ? AND archived_at IS NULL",
                (arg_ticker.upper(),),
            )
        else:
            cur = conn.execute(
                "SELECT ticker, fiscal_year_end, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL ORDER BY ticker"
            )
        rows = cur.fetchall()
    finally:
        conn.close()
    months_by_ticker: dict[str, int] = {}
    targets: list[CollectionTarget] = []
    for r in rows:
        fye_raw = r["fiscal_year_end"]
        if not isinstance(fye_raw, str) or len(fye_raw) < 2:
            sys.stderr.write(
                f"[skip] {r['ticker']}: fiscal_year_end is missing/malformed ({fye_raw!r})\n"
            )
            continue
        try:
            month = int(fye_raw[:2])
        except ValueError:
            sys.stderr.write(f"[skip] {r['ticker']}: fiscal_year_end={fye_raw!r} not parseable\n")
            continue
        if not 1 <= month <= 12:
            sys.stderr.write(f"[skip] {r['ticker']}: fiscal_year_end month {month} out of range\n")
            continue
        ticker = str(r["ticker"]).upper()
        try:
            role = ListType(str(r["list_type"]))
        except ValueError:
            continue
        months_by_ticker[ticker] = month
        targets.append(
            CollectionTarget(
                ticker=ticker,
                coverage_role=role,
                requested=arg_ticker is not None,
            )
        )
    selection = select_collection_targets(
        tuple(targets),
        source=CollectionSource.TRANSCRIPT,
        artifact_kind=ArtifactKind.TEXT_TRANSCRIPT,
    )
    for item in selection.denied:
        sys.stderr.write(
            json.dumps(
                {
                    "event": "source_collection_policy_denied",
                    "ticker": item.target.ticker,
                    "coverage_role": item.target.coverage_role.value,
                    "source": CollectionSource.TRANSCRIPT.value,
                    "artifact_kind": ArtifactKind.TEXT_TRANSCRIPT.value,
                    "reason": item.decision.reason.value,
                },
                sort_keys=True,
            )
            + "\n"
        )
    return [
        (item.target.ticker, months_by_ticker[item.target.ticker]) for item in selection.allowed
    ]


def _run_ingest(
    repo_root: Path,
    ticker: str,
    receipt_ids: list[str],
    dry_run: bool,
    *,
    owner_requested: bool,
) -> int:
    """Ingest newly fetched files for one ticker.

    Runs the current code checkout's state adapter while keeping mutable
    transcript files and the database under ``repo_root``. Per-ticker
    isolation prevents an unrelated quarantined artifact from blocking the
    rest of the scheduled portfolio batch.
    """
    if dry_run:
        print(
            f"  [dry-run] would invoke ingest_transcripts.py --ticker {ticker} "
            f"for {len(receipt_ids)} exact receipts",
            file=sys.stderr,
        )
        return 0
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "ingest_transcripts_state.py"),
        "--repo-root",
        str(repo_root),
        "--ticker",
        ticker,
    ]
    if owner_requested:
        cmd.append("--owner-requested")
    for receipt_id in receipt_ids:
        cmd.extend(["--receipt-id", receipt_id])
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode


def _run_extract(
    repo_root: Path,
    ticker: str,
    transcript_id: int,
    dry_run: bool,
) -> int:
    """Run commitment extraction for one exact in-window transcript.

    Runs current code with an explicit state-root database.
    """
    if dry_run:
        print(
            f"  [dry-run] would invoke extract_commitments --auto "
            f"--transcript-id {transcript_id} ({ticker})",
            file=sys.stderr,
        )
        return 0
    cmd = [
        *managed_python_prefix(PROJECT_ROOT),
        str(PROJECT_ROOT / "execution" / "extract_commitments_from_transcript.py"),
        "--auto",
        "--transcript-id",
        str(transcript_id),
        "--db",
        str(repo_root / "data" / "portfolio.db"),
    ]
    proc = subprocess.run(cmd, cwd=str(repo_root))
    return proc.returncode


def _commitment_scan_evidence(
    ticker: str, year: int, quarter: int, fye_month: int
) -> TranscriptEvidence | None:
    """Return exact durable evidence that the period's transcript was scanned."""

    period_end = quarter_end_date(year, quarter, fye_month).isoformat()
    transcript_evidence = _ingested_evidence(ticker, year, quarter, fye_month)
    if transcript_evidence is None:
        return None
    conn = db.get_connection()
    try:
        relation = selected_transcripts_relation(conn).sql
        transcripts = conn.execute(
            f"SELECT id FROM {relation} WHERE UPPER(ticker)=? "  # nosec B608
            "AND is_current=1 AND fiscal_period_type=? AND date(period_end)=date(?) "
            "ORDER BY id",
            (ticker.upper(), f"Q{quarter}", period_end),
        ).fetchall()
        if len(transcripts) != 1:
            return None
        receipt = current_commitment_scan_receipt(
            conn,
            transcript_id=int(transcripts[0]["id"]),
            prompt_version=prompt_version_for("saydo_commitment_extract"),
        )
        if receipt is None:
            return None
        expected_transcript_receipt = transcript_evidence.reference.removeprefix(
            "transcript-receipt:"
        )
        if (
            receipt.binding.transcript_acquisition_receipt_id != expected_transcript_receipt
            or receipt.binding.transcript_sha256 != transcript_evidence.sha256
        ):
            return None
        return TranscriptEvidence(
            reference=f"commitment-scan-receipt:{receipt.receipt_id}",
            sha256=receipt.receipt_id,
        )
    finally:
        conn.close()


def _transcript_id_for_period(
    ticker: str,
    year: int,
    quarter: int,
    fye_month: int,
) -> int | None:
    """Return the one selected transcript for an exact fiscal period."""

    period_end = quarter_end_date(year, quarter, fye_month).isoformat()
    conn = db.get_connection()
    try:
        relation = selected_transcripts_relation(conn).sql
        rows = conn.execute(
            f"SELECT id FROM {relation} WHERE UPPER(ticker)=? "  # nosec B608
            "AND is_current=1 AND fiscal_period_type=? AND date(period_end)=date(?) "
            "ORDER BY id",
            (ticker.upper(), f"Q{quarter}", period_end),
        ).fetchall()
        return int(rows[0]["id"]) if len(rows) == 1 else None
    finally:
        conn.close()


def _commitment_scan_targets(
    results: list[TickerBackfillResult], today: date, lookback: int
) -> list[CommitmentScanTarget]:
    """Select exact in-window transcripts that lack a durable scan outcome."""

    pending: list[CommitmentScanTarget] = []
    for result in results:
        for year, quarter in recent_fiscal_quarters(result.fye_month, today, lookback):
            transcript_id = _transcript_id_for_period(
                result.ticker,
                year,
                quarter,
                result.fye_month,
            )
            if (
                transcript_id is not None
                and (_ingested_evidence(result.ticker, year, quarter, result.fye_month) is not None)
                and (
                    _commitment_scan_evidence(result.ticker, year, quarter, result.fye_month)
                    is None
                )
            ):
                pending.append(
                    CommitmentScanTarget(
                        ticker=result.ticker,
                        fye_month=result.fye_month,
                        fiscal_year=year,
                        fiscal_quarter=quarter,
                        transcript_id=transcript_id,
                    )
                )
    return pending


def _run_commitment_scan_targets(
    repo_root: Path,
    targets: list[CommitmentScanTarget],
    *,
    dry_run: bool,
) -> list[dict[str, object]]:
    """Run and verify each exact target without aborting its peers."""

    results: list[dict[str, object]] = []
    for target in targets:
        print(
            f"[backfill_transcripts] extracting commitments for {target.ticker} "
            f"transcript_id={target.transcript_id}",
            file=sys.stderr,
        )
        rc = _run_extract(
            repo_root,
            target.ticker,
            target.transcript_id,
            dry_run,
        )
        if (
            rc == 0
            and not dry_run
            and _commitment_scan_evidence(
                target.ticker,
                target.fiscal_year,
                target.fiscal_quarter,
                target.fye_month,
            )
            is None
        ):
            print(
                f"[backfill_transcripts] {target.ticker} transcript_id="
                f"{target.transcript_id}: extractor returned 0 without exact scan evidence",
                file=sys.stderr,
            )
            rc = 1
        results.append(
            {
                "ticker": target.ticker,
                "fiscal_year": target.fiscal_year,
                "fiscal_quarter": target.fiscal_quarter,
                "transcript_id": target.transcript_id,
                "rc": rc,
            }
        )
    return results


def _persist_commitment_scan_coverage(
    result: TickerBackfillResult,
    *,
    today: date,
    lookback: int,
    extraction_attempted: bool,
) -> bool:
    """Persist exact scan/evidence or an explicit non-complete prerequisite outcome."""

    observed_at = datetime.now(UTC)
    all_actionable_closed = True
    policy_sha = policy_config_sha256(
        policy_name=COMMITMENT_SCAN_POLICY_NAME,
        policy_version=COMMITMENT_SCAN_POLICY_VERSION,
        providers=COMMITMENT_SCAN_POLICY_PROVIDERS,
    )
    for year, quarter in recent_fiscal_quarters(result.fye_month, today, lookback):
        label = _qlabel(year, quarter)
        evidence = _commitment_scan_evidence(result.ticker, year, quarter, result.fye_month)
        transcript_exists = _transcript_rows_exist(result.ticker, year, quarter, result.fye_month)
        transcript_evidence = _ingested_evidence(result.ticker, year, quarter, result.fye_month)
        attempt_provider = "governed_llm"
        if evidence is not None:
            status = CoverageDispositionStatus.SATISFIED
            reason = "commitment_scan_evidence_present"
            attempt_status = CoverageAttemptStatus.EVIDENCE_PRESENT
            retry_after = None
        elif not transcript_exists:
            status = CoverageDispositionStatus.SOURCE_UNAVAILABLE
            reason = "transcript_prerequisite_unavailable"
            attempt_status = CoverageAttemptStatus.AUTHORIZED_MISS
            attempt_provider = "transcript_prerequisite"
            retry_after = observed_at + timedelta(days=7)
        elif transcript_evidence is None:
            status = CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING
            reason = "transcript_evidence_prerequisite_missing"
            attempt_status = CoverageAttemptStatus.FAILED
            attempt_provider = "transcript_prerequisite"
            retry_after = None
        else:
            status = CoverageDispositionStatus.OPERATIONAL_ERROR
            reason = (
                "commitment_extraction_missing_evidence"
                if extraction_attempted
                else "commitment_extraction_not_attempted"
            )
            attempt_status = CoverageAttemptStatus.FAILED
            attempt_provider = "governed_llm"
            retry_after = observed_at + timedelta(days=1)
            all_actionable_closed = False
        persisted = _persist_coverage_disposition(
            ticker=result.ticker,
            year=year,
            quarter=quarter,
            fye_month=result.fye_month,
            status=status,
            reason_code=reason,
            attempts=(
                CoverageAttempt(
                    provider=attempt_provider,
                    status=attempt_status,
                ),
            ),
            observed_at=observed_at,
            evidence=evidence,
            retry_after=retry_after,
            artifact_kind=CoverageArtifactKind.COMMITMENT_SCAN,
            policy_name=COMMITMENT_SCAN_POLICY_NAME,
            policy_version=COMMITMENT_SCAN_POLICY_VERSION,
            policy_config_sha256_override=policy_sha,
        )
        result.commitment_scan_dispositions.append(f"{label}:{persisted}")
    return all_actionable_closed


def _newly_ingested_tickers(
    results: list[TickerBackfillResult], ingest_results: list[dict[str, object]]
) -> list[str]:
    """Return tickers whose newly fetched transcripts were ingested successfully.

    The daily backfill is an acquisition job, not an all-universe commitment
    rebuild.  Restricting the LLM phase to this run's new inputs keeps the job
    bounded and prevents overlap with the 02:15 scan and 03:00 protected window.
    """
    successful = {str(item["ticker"]) for item in ingest_results if item.get("rc") == 0}
    return [result.ticker for result in results if result.fetched and result.ticker in successful]


def _fetched_evidence_complete(result: TickerBackfillResult) -> bool:
    """Require every acquired period to have its exact DB/path/SHA receipt."""
    if not result.fetched:
        return False
    for label in result.fetched:
        try:
            quarter_token, year_token = label.split("_", 1)
            quarter = int(quarter_token.removeprefix("Q"))
            year = int(year_token)
        except ValueError:
            return False
        if not _has_ingested_evidence(result.ticker, year, quarter, result.fye_month):
            return False
    return True


def _persist_fetched_transcript_coverage(result: TickerBackfillResult) -> bool:
    """Close fetched periods only after their full canonical ingest postcondition."""

    complete = True
    for label in result.fetched:
        quarter_token, year_token = label.split("_", 1)
        quarter = int(quarter_token.removeprefix("Q"))
        year = int(year_token)
        observed_at = datetime.now(UTC)
        evidence = _ingested_evidence(result.ticker, year, quarter, result.fye_month)
        if evidence is not None:
            status = CoverageDispositionStatus.SATISFIED
            reason = "authorized_processed_transcript_with_segments"
            retry_after = None
            attempt_status = CoverageAttemptStatus.EVIDENCE_PRESENT
        elif _transcript_rows_exist(result.ticker, year, quarter, result.fye_month):
            status = CoverageDispositionStatus.REPAIR_EVIDENCE_MISSING
            reason = "canonical_transcript_evidence_missing"
            retry_after = None
            attempt_status = CoverageAttemptStatus.FAILED
            complete = False
        else:
            status = CoverageDispositionStatus.OPERATIONAL_ERROR
            reason = "transcript_ingest_postcondition_failed"
            retry_after = observed_at + timedelta(days=1)
            attempt_status = CoverageAttemptStatus.FAILED
            complete = False
        persisted = _persist_coverage_disposition(
            ticker=result.ticker,
            year=year,
            quarter=quarter,
            fye_month=result.fye_month,
            status=status,
            reason_code=reason,
            attempts=(CoverageAttempt(provider="canonical_store", status=attempt_status),),
            observed_at=observed_at,
            evidence=evidence,
            retry_after=retry_after,
        )
        result.coverage_dispositions.append(f"{label}:{persisted}")
    return complete


def _first_nonzero_ingest_rc(ingest_results: list[dict[str, object]]) -> int:
    """Return the first typed nonzero ingest result, or zero."""
    for item in ingest_results:
        rc = item.get("rc")
        if isinstance(rc, int) and rc != 0:
            return rc
    return 0


def _terminal_exit_code(
    ingest_rc: int | None,
    extract_results: list[dict[str, object]],
    acquisition_errors: int = 0,
) -> int:
    """Preserve a child-ingest failure for Scheduler and human operators."""
    if ingest_rc not in (None, 0):
        return ingest_rc
    if acquisition_errors or any(item.get("rc") != 0 for item in extract_results):
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ticker",
        help="Owner-requested stored portfolio/evaluation ticker",
    )
    p.add_argument(
        "--lookback-quarters",
        type=int,
        default=_DEFAULT_LOOKBACK,
        help=f"How many recent fiscal quarters to attempt per ticker (default {_DEFAULT_LOOKBACK})",
    )
    p.add_argument(
        "--skip-ingest", action="store_true", help="Skip the post-fetch ingest_transcripts.py call"
    )
    p.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip the post-ingest commitment-extraction calls",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — print what WOULD be fetched/ingested/extracted",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, transcripts/. Default: this repo. "
        "Worktree-based runs should pass the main repo path.",
    )
    args = p.parse_args()
    if args.lookback_quarters < 1 or args.lookback_quarters > _DEFAULT_LOOKBACK:
        p.error(f"--lookback-quarters must be between 1 and {_DEFAULT_LOOKBACK}")
    repo_root = args.repo_root.resolve()
    if repo_root != PROJECT_ROOT:
        _retarget_paths(repo_root)

    today = date.today()
    selected_db_path = repo_root / "data" / "portfolio.db"
    tickers = _resolve_tickers(args.ticker)
    if not tickers:
        print(json.dumps({"event": "no_tickers"}))
        return 0

    per_ticker: list[TickerBackfillResult] = []
    print(
        f"[backfill_transcripts] scope={len(tickers)} tickers  "
        f"lookback={args.lookback_quarters}q  today={today.isoformat()}",
        file=sys.stderr,
    )
    for ticker, fye_month in tickers:
        r = _backfill_one(
            ticker,
            fye_month,
            args.lookback_quarters,
            today,
            args.dry_run,
            selected_db_path,
            args.ticker is not None,
        )
        per_ticker.append(r)
        print(
            f"  {ticker:6s} fye={fye_month:02d}  "
            f"fetched={len(r.fetched)}  "
            f"skipped_existing={len(r.skipped_existing)}  "
            f"misses={len(r.aggregator_misses)}  "
            f"errors={len(r.errors)}",
            file=sys.stderr,
        )

    any_fetched = any(r.fetched_artifacts for r in per_ticker)
    ingest_rc: int | None = None
    ingest_results: list[dict[str, object]] = []
    if any_fetched and not args.skip_ingest:
        print("[backfill_transcripts] running per-ticker transcript ingest", file=sys.stderr)
        for result in per_ticker:
            if not result.fetched_artifacts:
                continue
            rc = _run_ingest(
                repo_root,
                result.ticker,
                [artifact.receipt_id for artifact in result.fetched_artifacts],
                args.dry_run,
                owner_requested=args.ticker is not None,
            )
            if rc == 0 and not args.dry_run and not _fetched_evidence_complete(result):
                print(
                    f"[backfill_transcripts] {result.ticker}: ingest returned 0 without "
                    "exact DB/path/SHA evidence",
                    file=sys.stderr,
                )
                rc = 1
            if not args.dry_run:
                try:
                    if not _persist_fetched_transcript_coverage(result):
                        rc = 1
                except Exception as exc:
                    result.errors.append(
                        f"transcript coverage postcondition: {type(exc).__name__}: {exc}"[:200]
                    )
                    rc = 1
            ingest_results.append({"ticker": result.ticker, "rc": rc})
        ingest_rc = _first_nonzero_ingest_rc(ingest_results)
    elif args.skip_ingest:
        print("[backfill_transcripts] --skip-ingest set; skipping ingest", file=sys.stderr)
    else:
        print("[backfill_transcripts] no new fetches; skipping ingest", file=sys.stderr)

    # Select every recent exact-period transcript that still lacks a durable
    # scan outcome. This catches transcripts that predated the current run as
    # well as artifacts ingested above, while the scan log keeps reruns bounded.
    extract_results: list[dict[str, object]] = []
    commitment_scan_targets: list[CommitmentScanTarget] = []
    commitment_scan_tickers: set[str] = set()
    if not args.skip_extract and not args.dry_run:
        commitment_scan_targets = _commitment_scan_targets(
            per_ticker,
            today,
            args.lookback_quarters,
        )
        commitment_scan_tickers = {target.ticker for target in commitment_scan_targets}
        extract_results = _run_commitment_scan_targets(
            repo_root,
            commitment_scan_targets,
            dry_run=args.dry_run,
        )
        for result in per_ticker:
            try:
                closed = _persist_commitment_scan_coverage(
                    result,
                    today=today,
                    lookback=args.lookback_quarters,
                    extraction_attempted=result.ticker in commitment_scan_tickers,
                )
            except Exception as exc:
                result.errors.append(f"commitment scan coverage: {type(exc).__name__}: {exc}"[:200])
                closed = False
            if (
                result.ticker in commitment_scan_tickers
                and not closed
                and not any(
                    item["ticker"] == result.ticker and item["rc"] != 0 for item in extract_results
                )
            ):
                extract_results.append(
                    {
                        "ticker": result.ticker,
                        "phase": "coverage_postcondition",
                        "rc": 1,
                    }
                )
    elif args.skip_extract:
        print("[backfill_transcripts] --skip-extract set; skipping commitments", file=sys.stderr)

    summary = {
        "today": today.isoformat(),
        "tickers_scanned": len(tickers),
        "lookback_quarters": args.lookback_quarters,
        "dry_run": args.dry_run,
        "per_ticker": [asdict(r) for r in per_ticker],
        "ingest_rc": ingest_rc,
        "ingest_results": ingest_results,
        "extract_results": extract_results,
        "totals": {
            "fetched": sum(len(r.fetched) for r in per_ticker),
            "artifact_conflicts": sum(len(r.artifact_conflicts) for r in per_ticker),
            "skipped_existing": sum(len(r.skipped_existing) for r in per_ticker),
            "aggregator_misses": sum(len(r.aggregator_misses) for r in per_ticker),
            "errors": sum(len(r.errors) for r in per_ticker),
        },
    }
    print(json.dumps(summary, indent=2))
    return _terminal_exit_code(
        ingest_rc,
        extract_results,
        acquisition_errors=sum(len(result.errors) for result in per_ticker),
    )


if __name__ == "__main__":
    sys.exit(main())
