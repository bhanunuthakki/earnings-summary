"""Native, exact-source public-time bounds for labelled historical research."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from urllib.request import url2pathname
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from provenance.immutable_artifact import ImmutableArtifactConflictError, read_stable_artifact
from runtime.deterministic import as_utc as _utc
from runtime.deterministic import content_sha256_ascii as _sha


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _clock(value: object) -> datetime:
    """Retain fractional seconds; SQLite datetime() would discard them."""
    return _utc(datetime.fromisoformat(str(value)))


def _rows(
    conn: sqlite3.Connection, query: str, parameters: tuple[object, ...]
) -> list[dict[str, object]]:
    cursor = conn.execute(query, parameters)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, row, strict=True)) for row in cursor]


_ET = ZoneInfo("America/New_York")


_BRT = ZoneInfo("America/Sao_Paulo")


_POST_CEILING = datetime(2026, 8, 19, 23, 59, 59, tzinfo=_ET)


_PUBLIC_POLICY = "historical-public-witness/v1"


_EDGAR_CALENDAR = {
    "version": "edgar-calendar-reviewed-20261019/v1",
    "sources": [
        "https://www.sec.gov/submit-filings/filer-support-resources/edgar-calendar",
        "https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/",
    ],
    "checked_on": "2026-10-19",
    "coverage": ["2025-05-01/2025-08-31", "2026-01-01/2026-12-31"],
    "closed_days": [
        "2025-05-26",
        "2025-06-19",
        "2025-07-04",
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-05-25",
        "2026-06-19",
        "2026-07-03",
        "2026-09-07",
        "2026-10-12",
        "2026-11-11",
        "2026-11-26",
        "2026-12-25",
    ],
    "weekends": "Saturday/Sunday",
    "exceptions": "known exceptional closure requires hold; unsupported ranges hold",
}


class HistoricalSecAuthority(_Frozen):
    """Reference exact retained issuer or SEC metadata without copying its bytes."""

    source_observation_id: str = Field(min_length=1, max_length=128)
    expected_blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HistoricalPublicWitness(_Frozen):
    """A replayable public-time bound; never a claim of first dissemination."""

    schema_version: Literal["historical-public-witness/v1"] = _PUBLIC_POLICY
    document_version_id: str
    source_url: str
    witness_kind: Literal["sec_policy_zone_envelope", "issuer_declared_day", "retained_retrieval"]
    authorities: tuple[HistoricalSecAuthority, ...]
    raw_field: str
    raw_value: str
    raw_field_sha256: str
    lower_at: datetime
    upper_at: datetime
    precision: Literal["zone_ambiguous", "day", "observed_capture"]
    zone: str
    policy_sha256: str
    owner_approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_record_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    calendar_sha256: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def commitment_sha256(self) -> str:
        return _sha(self.model_dump(mode="json"))


class HistoricalIssuerDaySelection(_Frozen):
    document_version_id: str = Field(min_length=1, max_length=128)
    metadata: HistoricalSecAuthority
    record_id: str = Field(min_length=1, max_length=128)


def require_historical_public_window(cutoff: datetime, window: str | None) -> None:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("historical public cutoff requires an explicit time zone")
    if window == "nu-q2-2026-post/v1" and _utc(cutoff) > _utc(_POST_CEILING):
        raise ValueError("historical NU post cutoff exceeds the fixed August 19 ceiling")


def _next_edgar_day_end(day: date) -> datetime:
    def supported(value: date) -> bool:
        return date(2025, 5, 1) <= value <= date(2025, 8, 31) or value.year == 2026

    if not supported(day):
        raise ValueError("historical SEC business calendar range is unsupported")
    result = day + timedelta(days=1)
    while result.weekday() >= 5 or result.isoformat() in _EDGAR_CALENDAR["closed_days"]:
        result += timedelta(days=1)
    if not supported(result):
        raise ValueError("historical SEC next business day is outside calendar coverage")
    return _utc(datetime.combine(result, time(23, 59, 59), _ET))


def build_sec_public_witness(
    document_version_id: str,
    source_url: str,
    accepted: datetime,
    raw_value: str,
    authorities: tuple[HistoricalSecAuthority, ...],
    owner_approval_sha256: str,
    public_cutoff_at: datetime,
    public_window: Literal["nu-q2-2026-post/v1"] | None,
) -> HistoricalPublicWitness:
    """Construct a bound from acceptance fields replayed by the native SEC scope.

    Construction alone is not verification. ``verify_analysis_scope`` replays
    the admitted metadata, issuer/accession identity and this calculation.
    """
    if accepted.utcoffset() != timedelta(0):
        raise ValueError("historical SEC ambiguous-zone policy requires its declared UTC field")
    declared = _utc(accepted)
    eastern_wall = accepted.replace(tzinfo=_ET)
    upper = max(
        _next_edgar_day_end(value.astimezone(_ET).date()) for value in (declared, eastern_wall)
    )
    witness = HistoricalPublicWitness(
        document_version_id=document_version_id,
        source_url=source_url,
        witness_kind="sec_policy_zone_envelope",
        authorities=authorities,
        raw_field="filings.acceptanceDateTime",
        raw_value=raw_value,
        raw_field_sha256=_sha(raw_value),
        lower_at=min(declared, _utc(eastern_wall)),
        upper_at=upper,
        precision="zone_ambiguous",
        zone="declared_UTC|SEC_Eastern_wall_clock:envelope",
        policy_sha256=_sha(
            {"policy": _PUBLIC_POLICY, "rule": "next_EDGAR_business_day_23:59:59_ET_strict_after"}
        ),
        owner_approval_sha256=owner_approval_sha256,
        calendar_sha256=_sha(_EDGAR_CALENDAR),
    )
    require_historical_public_witness(witness, public_cutoff_at, public_window=public_window)
    return witness


def require_historical_public_witness(
    witness: HistoricalPublicWitness,
    public_cutoff_at: datetime,
    *,
    public_window: Literal["nu-q2-2026-post/v1"] | None = None,
) -> None:
    """Require strict ordering without moving the declared historical cutoff."""
    require_historical_public_window(public_cutoff_at, public_window)
    if _utc(public_cutoff_at) <= _utc(witness.upper_at):
        raise ValueError("historical source public bound does not precede the fixed cutoff")


def read_retained_observation_bytes(
    conn: sqlite3.Connection,
    observation_id: str,
    *,
    cutoff_at: datetime,
    observed_through: datetime,
    max_bytes: int,
    subject: str,
    expected_blob_sha256: str | None = None,
) -> tuple[dict[str, object], bytes]:
    """Read one retained observation's exact local bytes within its knowledge clocks."""
    rows = _rows(
        conn,
        "SELECT observation.*,blob.byte_size,blob.media_type,blob.storage_uri,"
        "blob.recorded_at AS blob_recorded_at FROM evidence_source_observations observation "
        "JOIN evidence_content_blobs blob ON blob.sha256=observation.blob_sha256 "
        "WHERE observation.observation_id=?",
        (observation_id,),
    )
    if len(rows) != 1:
        raise ValueError(f"{subject} is unavailable")
    row = rows[0]
    uri = urlsplit(str(row["storage_uri"]))
    path = Path(url2pathname(uri.path))
    if (
        uri.scheme != "file"
        or uri.netloc
        or uri.query
        or uri.fragment
        or not path.is_absolute()
        or path.as_uri() != str(row["storage_uri"])
    ):
        raise ValueError(f"{subject} storage is not exact")
    try:
        if path.stat().st_nlink != 1:
            raise ValueError(f"{subject} has a hard-link alias")
        _artifact, raw = read_stable_artifact(path, max_bytes=max_bytes, allowed_root=path.parent)
    except (ImmutableArtifactConflictError, OSError) as exc:
        raise ValueError(f"{subject} bytes are unavailable") from exc
    if (
        (expected_blob_sha256 is not None and expected_blob_sha256 != row["blob_sha256"])
        or hashlib.sha256(raw).hexdigest() != row["blob_sha256"]
        or len(raw) != int(str(row["byte_size"]))
        or any(_clock(row[field]) > _utc(cutoff_at) for field in ("observed_at", "retrieved_at"))
        or _clock(row["blob_recorded_at"]) > _utc(observed_through)
    ):
        raise ValueError(f"{subject} bytes or physical clocks differ")
    return row, raw


def _public_source_bytes(
    conn: sqlite3.Connection,
    authority: HistoricalSecAuthority,
    cutoff_at: datetime,
    observed_through: datetime,
    *,
    max_bytes: int = 2_000_000,
) -> tuple[dict[str, object], bytes]:
    return read_retained_observation_bytes(
        conn,
        authority.source_observation_id,
        cutoff_at=cutoff_at,
        observed_through=observed_through,
        max_bytes=max_bytes,
        subject="historical public witness authority",
        expected_blob_sha256=authority.expected_blob_sha256,
    )


def _public_document(
    conn: sqlite3.Connection,
    document_version_id: str,
    issuer_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
) -> tuple[dict[str, object], bytes]:
    if _utc(observed_through) < _utc(cutoff_at):
        raise ValueError("historical public witness physical clocks are reversed")
    rows = _rows(
        conn,
        "SELECT document.*,observation.source_url,observation.retrieved_at,observation.observed_at "
        "FROM v_evidence_document_versions_canonical document "
        "JOIN evidence_source_observations observation "
        "ON observation.observation_id=document.observation_id "
        "WHERE document.document_version_id=?",
        (document_version_id,),
    )
    if len(rows) != 1 or rows[0]["issuer_id"] != issuer_id:
        raise ValueError("historical public witness document subject differs")
    row = rows[0]
    _source, raw = _public_source_bytes(
        conn,
        HistoricalSecAuthority(
            source_observation_id=str(row["observation_id"]),
            expected_blob_sha256=str(row["blob_sha256"]),
        ),
        cutoff_at,
        observed_through,
        max_bytes=32_000_000,
    )
    if _clock(row["recorded_at"]) > _utc(observed_through):
        raise ValueError("historical public witness document exceeds physical clock")
    return row, raw


def verify_historical_issuer_day_witness(
    conn: sqlite3.Connection,
    selection: HistoricalIssuerDaySelection,
    *,
    issuer_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
    owner_approval_sha256: str,
    public_cutoff_at: datetime,
    public_window: Literal["nu-q2-2026-post/v1"] | None = None,
) -> HistoricalPublicWitness:
    """Replay an issuer-declared publication day for an exact admitted PDF."""
    document, pdf = _public_document(
        conn, selection.document_version_id, issuer_id, cutoff_at, observed_through
    )
    metadata, raw = _public_source_bytes(conn, selection.metadata, cutoff_at, observed_through)
    metadata_url = urlsplit(str(metadata["source_url"]))
    if not pdf.startswith(b"%PDF-") or (
        metadata_url.scheme != "https"
        or metadata_url.netloc != "api.mziq.com"
        or metadata_url.fragment
    ):
        raise ValueError("issuer publication requires retained PDF and issuer metadata authority")
    decoded: object = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("issuer publication metadata is not an object")
    payload = cast(dict[str, object], decoded)
    data = payload.get("data")
    if not isinstance(data, dict) or payload.get("success") is not True:
        raise ValueError("issuer publication metadata has no successful data object")
    data_object = cast(dict[str, object], data)
    records = data_object.get("document_metas")
    if not isinstance(records, list):
        raise ValueError("issuer publication records are unavailable")
    record_values = cast(list[object], records)
    matches: list[dict[str, object]] = []
    for value in record_values:
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            if record.get("id") == selection.record_id:
                matches.append(record)
    if len(matches) != 1:
        raise ValueError("issuer publication record is absent or ambiguous")
    record = matches[0]
    url = str(document["source_url"])
    parts = urlsplit(url)
    match = re.fullmatch(r"/mzfilemanager/v2/d/([^/]+)/([^/]+)", parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc != "api.mziq.com"
        or parts.fragment
        or match is None
        or record.get("file_url") != url
        or record.get("company_id") != match[1]
        or record.get("download_link_id") != match[2]
        or record.get("is_published") is not True
        or record.get("language_code") != "en_US"
        or str(record.get("file_size")) != str(len(pdf))
    ):
        raise ValueError("issuer publication exact file URL or identity binding differs")
    value = record.get("file_published_date")
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T00:00:00(?:\.000)?Z", value
    ):
        raise ValueError("issuer publication requires its raw date-precision field")
    day = date.fromisoformat(value[:10])
    lower = min(_utc(datetime.combine(day, time.min, zone)) for zone in (_ET, _BRT))
    upper = max(_utc(datetime.combine(day, time(23, 59, 59), zone)) for zone in (_ET, _BRT))
    witness = HistoricalPublicWitness(
        document_version_id=selection.document_version_id,
        source_url=url,
        witness_kind="issuer_declared_day",
        authorities=(selection.metadata,),
        raw_field="data.document_metas[].file_published_date",
        raw_value=value,
        raw_field_sha256=_sha(
            {"record_id": selection.record_id, "file_url": url, "file_published_date": value}
        ),
        lower_at=lower,
        upper_at=upper,
        precision="day",
        zone="America/New_York|America/Sao_Paulo:declared_day_envelope",
        policy_sha256=_sha({"policy": _PUBLIC_POLICY, "rule": "issuer_declared_day_envelope"}),
        owner_approval_sha256=owner_approval_sha256,
        issuer_record_id=selection.record_id,
    )
    require_historical_public_witness(witness, public_cutoff_at, public_window=public_window)
    return witness


def verify_historical_retrieval_witness(
    conn: sqlite3.Connection,
    document_version_id: str,
    *,
    issuer_id: str,
    cutoff_at: datetime,
    observed_through: datetime,
    owner_approval_sha256: str,
    public_cutoff_at: datetime,
    public_window: Literal["nu-q2-2026-post/v1"] | None = None,
) -> HistoricalPublicWitness:
    """A retained capture is an upper bound only at its real retrieval clock."""
    row, _raw = _public_document(conn, document_version_id, issuer_id, cutoff_at, observed_through)
    captured = _clock(row["retrieved_at"])
    witness = HistoricalPublicWitness(
        document_version_id=document_version_id,
        source_url=str(row["source_url"]),
        witness_kind="retained_retrieval",
        authorities=(
            HistoricalSecAuthority(
                source_observation_id=str(row["observation_id"]),
                expected_blob_sha256=str(row["blob_sha256"]),
            ),
        ),
        raw_field="evidence_source_observations.retrieved_at",
        raw_value=str(row["retrieved_at"]),
        raw_field_sha256=_sha(str(row["retrieved_at"])),
        lower_at=captured,
        upper_at=captured,
        precision="observed_capture",
        zone="UTC",
        policy_sha256=_sha({"policy": _PUBLIC_POLICY, "rule": "retained_retrieval_upper_bound"}),
        owner_approval_sha256=owner_approval_sha256,
    )
    require_historical_public_witness(witness, public_cutoff_at, public_window=public_window)
    return witness
