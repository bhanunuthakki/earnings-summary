"""Immutable, source-bound receipts for transcript commitment scans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from provenance.selection import selected_transcripts_relation

_TRANSCRIPT_NAME = re.compile(r"^(?P<ticker>[A-Z0-9.-]+)_Q(?P<quarter>[1-4])_(?P<year>[0-9]{4})$")
_OBSERVATION_SCHEMA = "commitment-segment-observations@1"
_LEGACY_MANIFEST = '{"schema_version":"legacy-unobserved@0","segments":[]}'
_LEGACY_MANIFEST_SHA256 = "462e7c3d4eb4e810994e1692e3a50c32b4ddf230ee4c7c9690d81f053fa3d929"
_EMPTY_SOURCE_SHA256 = "6367e24ff1e38f3e3e590763053263ca9db7d36126e99e65caba76cb9fea37e4"


@dataclass(frozen=True)
class TranscriptScanBinding:
    transcript_id: int
    document_id: int
    transcript_acquisition_receipt_id: str
    transcript_sha256: str


@dataclass(frozen=True)
class TranscriptSegmentVersion:
    """Exact selected-segment bytes and context presented to extraction."""

    transcript_id: int
    segment_id: int
    sequence: int
    source_document_id: int
    period_end: str
    speaker: str | None
    time_code_start: str | None
    time_code_end: str | None
    text_sha256: str
    text_size_bytes: int

    def __post_init__(self) -> None:
        identity_fields = (self.transcript_id, self.segment_id, self.source_document_id)
        if any(type(value) is not int or value <= 0 for value in identity_fields):
            raise ValueError("segment observation identities must be positive integers")
        ordinal_fields = (self.sequence, self.text_size_bytes)
        if any(type(value) is not int or value < 0 for value in ordinal_fields):
            raise ValueError("segment observation ordinal fields must be non-negative integers")
        if not re.fullmatch(r"[0-9a-f]{64}", self.text_sha256):
            raise ValueError("segment observation text_sha256 must be lowercase SHA-256")
        try:
            datetime.fromisoformat(self.period_end)
        except ValueError as exc:
            raise ValueError("segment observation period_end must be ISO-8601") from exc

    def as_manifest_item(self) -> dict[str, object]:
        return {
            "period_end": self.period_end,
            "segment_id": self.segment_id,
            "sequence": self.sequence,
            "source_document_id": self.source_document_id,
            "speaker": self.speaker,
            "text_sha256": self.text_sha256,
            "text_size_bytes": self.text_size_bytes,
            "time_code_end": self.time_code_end,
            "time_code_start": self.time_code_start,
            "transcript_id": self.transcript_id,
        }


@dataclass(frozen=True)
class ObservedTranscriptSegment:
    """One fully parsed segment and its explicit extraction disposition."""

    source: TranscriptSegmentVersion
    disposition: str
    commitment_count: int
    indicator_count: int

    def __post_init__(self) -> None:
        counts = (self.commitment_count, self.indicator_count)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("segment scan output counts must be non-negative integers")
        expected = (
            "parsed_no_output"
            if self.commitment_count + self.indicator_count == 0
            else "parsed_with_output"
        )
        if self.disposition != expected:
            raise ValueError("segment scan disposition does not match its output counts")

    def as_manifest_item(self) -> dict[str, object]:
        return {
            "commitment_count": self.commitment_count,
            "disposition": self.disposition,
            "indicator_count": self.indicator_count,
            "source": self.source.as_manifest_item(),
        }


@dataclass(frozen=True)
class CommitmentScanReceipt:
    receipt_id: str
    binding: TranscriptScanBinding
    prompt_version: str
    n_extracted: int
    output_manifest_json: str
    output_manifest_sha256: str
    observed_segments: tuple[ObservedTranscriptSegment, ...]
    observed_segments_sha256: str
    observed_source_sha256: str
    recorded_at: str


class CommitmentScanCoverageState(StrEnum):
    COMPLETE = "complete"
    LEGACY_UNOBSERVED_REAUDIT_REQUIRED = "legacy_unobserved_reaudit_required"
    INVALID_REAUDIT_REQUIRED = "invalid_scan_evidence_reaudit_required"
    SOURCE_CHANGED_MISSING = "source_changed_missing"
    NEVER_SCANNED_MISSING = "never_scanned_missing"


@dataclass(frozen=True)
class CommitmentScanCoverage:
    state: CommitmentScanCoverageState
    receipt: CommitmentScanReceipt | None = None
    legacy_receipt_id: str | None = None
    legacy_n_extracted: int | None = None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scan_receipt_table_available(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='commitment_scan_receipts'"
        ).fetchone()
        is not None
    )


def _segment_manifest_columns_available(conn: sqlite3.Connection) -> bool:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(commitment_scan_receipts)")}
    return {
        "observed_segments_json",
        "observed_segments_sha256",
        "observed_source_sha256",
    } <= columns


_WRITABLE_RECEIPT_COLUMNS = {
    "receipt_id",
    "transcript_id",
    "document_id",
    "transcript_acquisition_receipt_id",
    "transcript_sha256",
    "prompt_version",
    "n_extracted",
    "output_manifest_json",
    "output_manifest_sha256",
    "observed_segments_json",
    "observed_segments_sha256",
    "observed_source_sha256",
    "recorded_at",
}


def scan_receipt_schema_available(conn: sqlite3.Connection) -> bool:
    """Return whether the exact immutable v2 receipt writer contract exists."""

    if not scan_receipt_table_available(conn):
        return False
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(commitment_scan_receipts)")}
    if not columns >= _WRITABLE_RECEIPT_COLUMNS:
        return False
    guards = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='commitment_scan_receipts'"
        )
    }
    if (
        not {
            "trg_commitment_scan_receipts_no_update",
            "trg_commitment_scan_receipts_no_delete",
        }
        <= guards
    ):
        return False
    indexes = {
        str(row[1]): (bool(row[2]), bool(row[4]))
        for row in conn.execute("PRAGMA index_list(commitment_scan_receipts)")
    }
    exact_index = indexes.get("ux_commitment_scan_receipts_exact_scan")
    if exact_index != (True, True):
        return False
    index_columns = tuple(
        str(row[2])
        for row in conn.execute("PRAGMA index_info(ux_commitment_scan_receipts_exact_scan)")
    )
    if index_columns != (
        "transcript_acquisition_receipt_id",
        "prompt_version",
        "observed_source_sha256",
    ):
        return False
    index_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='ux_commitment_scan_receipts_exact_scan'"
    ).fetchone()
    if index_sql_row is None or index_sql_row[0] is None:
        return False
    normalized_sql = "".join(str(index_sql_row[0]).casefold().split())
    expected_index_sql = (
        "createuniqueindexux_commitment_scan_receipts_exact_scan"
        "oncommitment_scan_receipts(transcript_acquisition_receipt_id,prompt_version,"
        "observed_source_sha256)"
        "wherejson_extract(observed_segments_json,'$.schema_version')="
        "'commitment-segment-observations@1'"
    )
    return normalized_sql == expected_index_sql


def _period_end(value: object) -> str:
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return text[:10]


def _optional_text(value: object) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def observe_transcript_segment(
    *,
    transcript_id: int,
    segment_id: int,
    sequence: int,
    source_document_id: int,
    period_end: object,
    speaker: object,
    time_code_start: object,
    time_code_end: object,
    text: str,
) -> TranscriptSegmentVersion:
    raw = text.encode("utf-8")
    return TranscriptSegmentVersion(
        transcript_id=transcript_id,
        segment_id=segment_id,
        sequence=sequence,
        source_document_id=source_document_id,
        period_end=_period_end(period_end),
        speaker=_optional_text(speaker),
        time_code_start=_optional_text(time_code_start),
        time_code_end=_optional_text(time_code_end),
        text_sha256=hashlib.sha256(raw).hexdigest(),
        text_size_bytes=len(raw),
    )


def selected_segment_versions(
    conn: sqlite3.Connection, transcript_id: int
) -> tuple[TranscriptSegmentVersion, ...]:
    """Read the current selected segment set with exact text and locator identities."""

    relation = selected_transcripts_relation(conn).sql
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(transcript_segments)")}
    speaker = "s.speaker" if "speaker" in columns else "NULL"
    time_start = "s.time_code_start" if "time_code_start" in columns else "NULL"
    time_end = "s.time_code_end" if "time_code_end" in columns else "NULL"
    rows = conn.execute(
        "SELECT t.id,t.document_id,t.period_end,s.id,s.seq,"
        + speaker
        + ","
        + time_start
        + ","
        + time_end
        + ",s.text "
        f"FROM {relation} AS t JOIN transcript_segments AS s ON s.transcript_id=t.id "  # nosec B608
        "WHERE t.id=? ORDER BY s.seq,s.id",
        (transcript_id,),
    ).fetchall()
    return tuple(
        observe_transcript_segment(
            transcript_id=int(row[0]),
            source_document_id=int(row[1]),
            period_end=row[2],
            segment_id=int(row[3]),
            sequence=int(row[4]),
            speaker=row[5],
            time_code_start=row[6],
            time_code_end=row[7],
            text=str(row[8]),
        )
        for row in rows
    )


def _strict_int(value: object, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("segment observation integer fields must be exact integers")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ValueError("segment observation integer field is outside its allowed range")
    return value


def _parse_segment_version(value: object) -> TranscriptSegmentVersion:
    if not isinstance(value, dict):
        raise ValueError("segment source version must be an object")
    item = cast(dict[object, object], value)
    expected_keys = {
        "period_end",
        "segment_id",
        "sequence",
        "source_document_id",
        "speaker",
        "text_sha256",
        "text_size_bytes",
        "time_code_end",
        "time_code_start",
        "transcript_id",
    }
    if set(item) != expected_keys or any(not isinstance(key, str) for key in item):
        raise ValueError("segment source version has an invalid shape")
    period_end = item["period_end"]
    text_sha256 = item["text_sha256"]
    optional_values = (item["speaker"], item["time_code_start"], item["time_code_end"])
    if not isinstance(period_end, str) or not isinstance(text_sha256, str):
        raise ValueError("segment source version text fields must be strings")
    if any(value is not None and not isinstance(value, str) for value in optional_values):
        raise ValueError("segment source version locator fields must be strings or null")
    return TranscriptSegmentVersion(
        transcript_id=_strict_int(item["transcript_id"], positive=True),
        segment_id=_strict_int(item["segment_id"], positive=True),
        sequence=_strict_int(item["sequence"], positive=False),
        source_document_id=_strict_int(item["source_document_id"], positive=True),
        period_end=period_end,
        speaker=cast(str | None, item["speaker"]),
        time_code_start=cast(str | None, item["time_code_start"]),
        time_code_end=cast(str | None, item["time_code_end"]),
        text_sha256=text_sha256,
        text_size_bytes=_strict_int(item["text_size_bytes"], positive=False),
    )


def _parse_observed_segments(value: object) -> tuple[ObservedTranscriptSegment, ...]:
    if not isinstance(value, dict):
        raise ValueError("observed segment manifest must be a versioned JSON object")
    manifest = cast(dict[object, object], value)
    if set(manifest) != {"schema_version", "segments"}:
        raise ValueError("observed segment manifest has an invalid shape")
    if manifest["schema_version"] != _OBSERVATION_SCHEMA:
        raise ValueError("observed segment manifest schema version is unsupported")
    raw_segments = manifest["segments"]
    if not isinstance(raw_segments, list):
        raise ValueError("observed segment manifest segments must be an explicit array")
    observations: list[ObservedTranscriptSegment] = []
    expected_keys = {"commitment_count", "disposition", "indicator_count", "source"}
    for raw in cast(list[object], raw_segments):
        if not isinstance(raw, dict):
            raise ValueError("observed segment manifest items must be objects")
        item = cast(dict[object, object], raw)
        if set(item) != expected_keys or any(not isinstance(key, str) for key in item):
            raise ValueError("observed segment manifest item has an invalid shape")
        disposition = item["disposition"]
        if not isinstance(disposition, str):
            raise ValueError("segment scan disposition must be a string")
        observations.append(
            ObservedTranscriptSegment(
                source=_parse_segment_version(item["source"]),
                disposition=disposition,
                commitment_count=_strict_int(item["commitment_count"], positive=False),
                indicator_count=_strict_int(item["indicator_count"], positive=False),
            )
        )
    ids = [item.source.segment_id for item in observations]
    if len(ids) != len(set(ids)):
        raise ValueError("observed segment identities must be unique")
    return tuple(observations)


def _observed_segments_json(observed: Sequence[ObservedTranscriptSegment]) -> str:
    return _canonical_json(
        {
            "schema_version": _OBSERVATION_SCHEMA,
            "segments": [item.as_manifest_item() for item in observed],
        }
    )


def _observed_source_sha256(observed: Sequence[ObservedTranscriptSegment]) -> str:
    source_json = _canonical_json(
        {
            "schema_version": "transcript-segment-source-set@1",
            "segments": [item.source.as_manifest_item() for item in observed],
        }
    )
    return _sha256(source_json)


def _outputs_match_observed(
    output_json: str, observed: Sequence[ObservedTranscriptSegment]
) -> bool:
    expected = {
        item.source.segment_id: (item.commitment_count, item.indicator_count) for item in observed
    }
    actual = {segment_id: [0, 0] for segment_id in expected}
    raw_outputs = cast(object, json.loads(output_json))
    if not isinstance(raw_outputs, list):
        return False
    for raw in cast(list[object], raw_outputs):
        if not isinstance(raw, dict):
            return False
        output = cast(dict[str, object], raw)
        segment_id = output.get("transcript_segment_id")
        if isinstance(segment_id, bool) or not isinstance(segment_id, int):
            return False
        counts = actual.get(segment_id)
        if counts is None:
            return False
        if output.get("kind") == "commitment":
            counts[0] += 1
        elif output.get("kind") == "management_indicator":
            counts[1] += 1
        else:
            return False
    return {key: tuple(value) for key, value in actual.items()} == expected


def current_transcript_scan_binding(
    conn: sqlite3.Connection, transcript_id: int
) -> TranscriptScanBinding | None:
    """Resolve a selected transcript to its exact authorized acquisition receipt."""

    required_tables = {
        "documents",
        "transcripts",
        "transcript_segments",
        "transcript_acquisition_receipts",
    }
    present_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?, ?, ?)",
            tuple(required_tables),
        )
    }
    if present_tables != required_tables:
        return None
    relation = selected_transcripts_relation(conn).sql
    rows = conn.execute(
        "SELECT t.id AS transcript_id,t.document_id,d.file_path,d.sha256,r.receipt_id,"
        "r.fiscal_year,r.fiscal_quarter,r.canonical_document_path,r.artifact_json "
        f"FROM {relation} AS t "  # nosec B608 -- repository-owned selection relation
        "JOIN documents AS d ON d.id=t.document_id "
        "JOIN transcript_acquisition_receipts AS r "
        "ON r.canonical_ticker=UPPER(t.ticker) AND r.artifact_sha256=d.sha256 "
        "AND (r.document_id IS NULL OR r.document_id=d.id) "
        "WHERE t.id=? AND t.is_current=1 "
        "AND r.provider='issuer_ir' AND r.source_type='ir_doc' "
        "AND r.document_type='earnings_call_transcript' "
        "AND EXISTS (SELECT 1 FROM transcript_segments AS s WHERE s.transcript_id=t.id) "
        "ORDER BY r.recorded_at DESC,r.receipt_id DESC",
        (transcript_id,),
    ).fetchall()
    for row in rows:
        file_path = PurePosixPath(str(row["file_path"]))
        if file_path.parent != PurePosixPath("transcripts/processed"):
            continue
        match = _TRANSCRIPT_NAME.fullmatch(file_path.stem)
        if match is None:
            continue
        if int(match.group("year")) != int(row["fiscal_year"]):
            continue
        if int(match.group("quarter")) != int(row["fiscal_quarter"]):
            continue
        expected_raw = PurePosixPath("transcripts/raw") / file_path.name
        if str(row["canonical_document_path"]) != expected_raw.as_posix():
            continue
        artifact_json = str(row["artifact_json"])
        if _sha256(artifact_json) != str(row["receipt_id"]):
            continue
        return TranscriptScanBinding(
            transcript_id=int(row["transcript_id"]),
            document_id=int(row["document_id"]),
            transcript_acquisition_receipt_id=str(row["receipt_id"]),
            transcript_sha256=str(row["sha256"]),
        )
    return None


def _commitment_output(conn: sqlite3.Connection, item_id: int) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT mc.id,mc.ticker,mc.period_made,mc.transcript_segment_id,mc.period_target,"
        "mc.kpi_name,mc.comparator,mc.target_value,mc.unit,mc.narrative,ts.transcript_id "
        "FROM management_commitments AS mc "
        "JOIN transcript_segments AS ts ON ts.id=mc.transcript_segment_id WHERE mc.id=?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": "commitment",
        "id": int(row["id"]),
        "ticker": str(row["ticker"]),
        "period_made": str(row["period_made"]),
        "transcript_segment_id": int(row["transcript_segment_id"]),
        "transcript_id": int(row["transcript_id"]),
        "period_target": str(row["period_target"]),
        "kpi_name": str(row["kpi_name"]),
        "comparator": str(row["comparator"]),
        "target_value": str(row["target_value"]),
        "unit": str(row["unit"]),
        "narrative": str(row["narrative"]),
    }


def _indicator_output(conn: sqlite3.Connection, item_id: int) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT mio.id,mio.idempotency_key,mio.ticker,mio.transcript_segment_id,"
        "mio.source_doc_id,mio.raw_label,mio.value,mio.unit,mio.scope,mio.speaker,"
        "mio.source_excerpt,mio.source_locator_json,mio.recurrence,ts.transcript_id "
        "FROM management_indicator_observations AS mio "
        "JOIN transcript_segments AS ts ON ts.id=mio.transcript_segment_id WHERE mio.id=?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": "management_indicator",
        "id": int(row["id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "ticker": str(row["ticker"]),
        "transcript_segment_id": int(row["transcript_segment_id"]),
        "transcript_id": int(row["transcript_id"]),
        "source_doc_id": int(row["source_doc_id"]),
        "raw_label": str(row["raw_label"]),
        "value": str(row["value"]),
        "unit": str(row["unit"]),
        "scope": str(row["scope"]),
        "speaker": None if row["speaker"] is None else str(row["speaker"]),
        "source_excerpt": str(row["source_excerpt"]),
        "source_locator_json": str(row["source_locator_json"]),
        "recurrence": str(row["recurrence"]),
    }


def _output_manifest(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    commitment_ids: Sequence[int],
    management_indicator_ids: Sequence[int],
) -> str:
    if len(set(commitment_ids)) != len(commitment_ids) or len(set(management_indicator_ids)) != len(
        management_indicator_ids
    ):
        raise ValueError("commitment scan output identities must be unique")
    items: list[dict[str, object]] = []
    for item_id in sorted(commitment_ids):
        item = _commitment_output(conn, item_id)
        if item is None or item["transcript_id"] != transcript_id:
            raise ValueError("commitment scan output is missing or belongs to another transcript")
        items.append(item)
    for item_id in sorted(management_indicator_ids):
        item = _indicator_output(conn, item_id)
        if item is None or item["transcript_id"] != transcript_id:
            raise ValueError(
                "management indicator output is missing or belongs to another transcript"
            )
        items.append(item)
    return _canonical_json(items)


def _receipt_payload(
    *,
    binding: TranscriptScanBinding,
    prompt_version: str,
    n_extracted: int,
    output_manifest_sha256: str,
    observed_segments_sha256: str,
    observed_source_sha256: str,
) -> str:
    return _canonical_json(
        {
            "schema_version": "commitment-scan-receipt@2",
            "transcript_id": binding.transcript_id,
            "document_id": binding.document_id,
            "transcript_acquisition_receipt_id": binding.transcript_acquisition_receipt_id,
            "transcript_sha256": binding.transcript_sha256,
            "prompt_version": prompt_version,
            "n_extracted": n_extracted,
            "output_manifest_sha256": output_manifest_sha256,
            "observed_segments_sha256": observed_segments_sha256,
            "observed_source_sha256": observed_source_sha256,
        }
    )


def _legacy_receipt_payload(
    *,
    binding: TranscriptScanBinding,
    prompt_version: str,
    n_extracted: int,
    output_manifest_sha256: str,
) -> str:
    return _canonical_json(
        {
            "schema_version": "commitment-scan-receipt@1",
            "transcript_id": binding.transcript_id,
            "document_id": binding.document_id,
            "transcript_acquisition_receipt_id": binding.transcript_acquisition_receipt_id,
            "transcript_sha256": binding.transcript_sha256,
            "prompt_version": prompt_version,
            "n_extracted": n_extracted,
            "output_manifest_sha256": output_manifest_sha256,
        }
    )


def append_commitment_scan_receipt(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    prompt_version: str,
    commitment_ids: Sequence[int] = (),
    management_indicator_ids: Sequence[int] = (),
    observed_segments: Sequence[ObservedTranscriptSegment] | None = None,
    expected_binding: TranscriptScanBinding | None = None,
    recorded_at: datetime | None = None,
) -> CommitmentScanReceipt:
    """Append or exactly replay one current transcript scan receipt."""

    if not scan_receipt_table_available(conn):
        raise RuntimeError("commitment_scan_receipts table is unavailable")
    if not _segment_manifest_columns_available(conn):
        raise RuntimeError("commitment scan segment manifest schema is unavailable")
    version = prompt_version.strip()
    if not version:
        raise ValueError("commitment scan prompt_version is required")
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    binding = current_transcript_scan_binding(conn, transcript_id)
    if binding is None:
        raise ValueError("current transcript lacks exact authorized acquisition evidence")
    if expected_binding is not None and binding != expected_binding:
        raise ValueError("transcript source binding changed before receipt issuance")
    output_json = _output_manifest(
        conn,
        transcript_id=transcript_id,
        commitment_ids=commitment_ids,
        management_indicator_ids=management_indicator_ids,
    )
    n_extracted = len(commitment_ids) + len(management_indicator_ids)
    output_sha = _sha256(output_json)
    if observed_segments is None:
        raise ValueError("typed observed segment coverage is required")
    observed = tuple(observed_segments)
    current_segments = selected_segment_versions(conn, transcript_id)
    if not current_segments or tuple(item.source for item in observed) != current_segments:
        raise ValueError("observed segment coverage does not match current selected segment bytes")
    if sum(item.commitment_count + item.indicator_count for item in observed) != n_extracted:
        raise ValueError("observed segment dispositions do not match extracted output count")
    if not _outputs_match_observed(output_json, observed):
        raise ValueError("observed per-segment dispositions do not match exact scan outputs")
    observed_json = _observed_segments_json(observed)
    observed_sha = _sha256(observed_json)
    observed_source_sha = _observed_source_sha256(observed)
    receipt_id = _sha256(
        _receipt_payload(
            binding=binding,
            prompt_version=version,
            n_extracted=n_extracted,
            output_manifest_sha256=output_sha,
            observed_segments_sha256=observed_sha,
            observed_source_sha256=observed_source_sha,
        )
    )
    sealed = conn.execute(
        "SELECT receipt_id FROM commitment_scan_receipts "
        "WHERE transcript_acquisition_receipt_id=? AND prompt_version=? "
        "AND observed_source_sha256=? "
        "AND json_extract(observed_segments_json,'$.schema_version')=? "
        "ORDER BY receipt_id LIMIT 1",
        (
            binding.transcript_acquisition_receipt_id,
            version,
            observed_source_sha,
            _OBSERVATION_SCHEMA,
        ),
    ).fetchone()
    if sealed is not None and str(sealed[0]) != receipt_id:
        raise ValueError("commitment scan is already sealed with a different output manifest")
    timestamp = (
        (recorded_at or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    values = (
        binding.transcript_id,
        binding.document_id,
        binding.transcript_acquisition_receipt_id,
        binding.transcript_sha256,
        version,
        n_extracted,
        output_json,
        output_sha,
        observed_json,
        observed_sha,
        observed_source_sha,
    )
    existing = conn.execute(
        "SELECT transcript_id,document_id,transcript_acquisition_receipt_id,transcript_sha256,"
        "prompt_version,n_extracted,output_manifest_json,output_manifest_sha256,"
        "observed_segments_json,observed_segments_sha256,observed_source_sha256,recorded_at "
        "FROM commitment_scan_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO commitment_scan_receipts "
            "(receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256,observed_segments_json,observed_segments_sha256,"
            "observed_source_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, *values, timestamp),
        )
        recorded = timestamp
    else:
        if tuple(existing)[:-1] != values:
            raise ValueError("commitment scan receipt identity collision")
        recorded = str(existing["recorded_at"])
    return CommitmentScanReceipt(
        receipt_id=receipt_id,
        binding=binding,
        prompt_version=version,
        n_extracted=n_extracted,
        output_manifest_json=output_json,
        output_manifest_sha256=output_sha,
        observed_segments=observed,
        observed_segments_sha256=observed_sha,
        observed_source_sha256=observed_source_sha,
        recorded_at=recorded,
    )


def _sealed_receipt_binding(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> TranscriptScanBinding | None:
    """Validate a receipt row's own immutable acquisition binding."""

    try:
        binding = TranscriptScanBinding(
            transcript_id=_strict_int(row["transcript_id"], positive=True),
            document_id=_strict_int(row["document_id"], positive=True),
            transcript_acquisition_receipt_id=str(row["transcript_acquisition_receipt_id"]),
            transcript_sha256=str(row["transcript_sha256"]),
        )
        if not re.fullmatch(r"[0-9a-f]{64}", binding.transcript_sha256):
            return None
        acquisition = conn.execute(
            "SELECT receipt_id,document_id,artifact_sha256,artifact_json,provider,source_type,"
            "document_type FROM transcript_acquisition_receipts WHERE receipt_id=?",
            (binding.transcript_acquisition_receipt_id,),
        ).fetchone()
        if acquisition is None:
            return None
        artifact_json = str(acquisition["artifact_json"])
        if (
            str(acquisition["receipt_id"]) != _sha256(artifact_json)
            or acquisition["document_id"] != binding.document_id
            or str(acquisition["artifact_sha256"]) != binding.transcript_sha256
            or str(acquisition["provider"]) != "issuer_ir"
            or str(acquisition["source_type"]) != "ir_doc"
            or str(acquisition["document_type"]) != "earnings_call_transcript"
        ):
            return None
        return binding
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def _validate_receipt_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    binding: TranscriptScanBinding,
    prompt_version: str,
    require_current_segments: bool = True,
) -> CommitmentScanReceipt | None:
    try:
        if not _segment_manifest_columns_available(conn):
            return None
        observed_json = str(row["observed_segments_json"])
        row_observed_segments = _parse_observed_segments(cast(object, json.loads(observed_json)))
        if observed_json != _observed_segments_json(row_observed_segments):
            return None
        observed_sha = _sha256(observed_json)
        if observed_sha != str(row["observed_segments_sha256"]):
            return None
        observed_source_sha = _observed_source_sha256(row_observed_segments)
        if observed_source_sha != str(row["observed_source_sha256"]):
            return None
        observed_sources = tuple(item.source for item in row_observed_segments)
        if not observed_sources:
            return None
        if require_current_segments:
            if observed_sources != selected_segment_versions(conn, binding.transcript_id):
                return None
        elif any(
            source.transcript_id != binding.transcript_id
            or source.source_document_id != binding.document_id
            for source in observed_sources
        ):
            return None
        if (
            int(row["transcript_id"]) != binding.transcript_id
            or int(row["document_id"]) != binding.document_id
            or str(row["transcript_acquisition_receipt_id"])
            != binding.transcript_acquisition_receipt_id
            or str(row["transcript_sha256"]) != binding.transcript_sha256
            or str(row["prompt_version"]) != prompt_version
        ):
            return None
        raw_items = cast(object, json.loads(str(row["output_manifest_json"])))
        if not isinstance(raw_items, list):
            return None
        commitment_ids: list[int] = []
        indicator_ids: list[int] = []
        for raw_item in cast(list[object], raw_items):
            if not isinstance(raw_item, dict):
                return None
            item = cast(dict[str, object], raw_item)
            raw_id = item.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
                return None
            item_id = raw_id
            if item.get("kind") == "commitment":
                commitment_ids.append(item_id)
            elif item.get("kind") == "management_indicator":
                indicator_ids.append(item_id)
            else:
                return None
        output_json = _output_manifest(
            conn,
            transcript_id=binding.transcript_id,
            commitment_ids=commitment_ids,
            management_indicator_ids=indicator_ids,
        )
        output_sha = _sha256(output_json)
        n_extracted = len(commitment_ids) + len(indicator_ids)
        if not _outputs_match_observed(output_json, row_observed_segments):
            return None
        receipt_id = _sha256(
            _receipt_payload(
                binding=binding,
                prompt_version=prompt_version,
                n_extracted=n_extracted,
                output_manifest_sha256=output_sha,
                observed_segments_sha256=observed_sha,
                observed_source_sha256=observed_source_sha,
            )
        )
        if (
            output_json != str(row["output_manifest_json"])
            or output_sha != str(row["output_manifest_sha256"])
            or n_extracted != int(row["n_extracted"])
            or receipt_id != str(row["receipt_id"])
        ):
            return None
        return CommitmentScanReceipt(
            receipt_id=receipt_id,
            binding=binding,
            prompt_version=prompt_version,
            n_extracted=n_extracted,
            output_manifest_json=output_json,
            output_manifest_sha256=output_sha,
            observed_segments=row_observed_segments,
            observed_segments_sha256=observed_sha,
            observed_source_sha256=observed_source_sha,
            recorded_at=str(row["recorded_at"]),
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def _validated_historical_v2_receipt(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    prompt_version: str,
) -> CommitmentScanReceipt | None:
    binding = _sealed_receipt_binding(conn, row)
    if binding is None:
        return None
    return _validate_receipt_row(
        conn,
        row,
        binding=binding,
        prompt_version=prompt_version,
        require_current_segments=False,
    )


def current_commitment_scan_receipt(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    prompt_version: str,
    cutoff_at: datetime | None = None,
) -> CommitmentScanReceipt | None:
    """Return a valid latest receipt for the selected transcript/current prompt."""

    if not scan_receipt_table_available(conn):
        return None
    binding = current_transcript_scan_binding(conn, transcript_id)
    if binding is None:
        return None
    coverage_columns = (
        ",observed_segments_json,observed_segments_sha256,observed_source_sha256"
        if _segment_manifest_columns_available(conn)
        else ""
    )
    params: tuple[object, ...] = (transcript_id, prompt_version)
    if cutoff_at is None:
        rows = conn.execute(
            "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256"
            + coverage_columns
            + ",recorded_at FROM commitment_scan_receipts "
            "WHERE transcript_id=? AND prompt_version=? "
            "ORDER BY datetime(recorded_at) DESC,receipt_id DESC",
            params,
        ).fetchall()
    else:
        params += (cutoff_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),)
        rows = conn.execute(
            "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256"
            + coverage_columns
            + ",recorded_at FROM commitment_scan_receipts "
            "WHERE transcript_id=? AND prompt_version=? "
            "AND datetime(recorded_at)<=datetime(?) "
            "ORDER BY datetime(recorded_at) DESC,receipt_id DESC",
            params,
        ).fetchall()
    for row in rows:
        receipt = _validate_receipt_row(
            conn,
            row,
            binding=binding,
            prompt_version=prompt_version,
        )
        if receipt is not None:
            return receipt
    return None


def _validated_legacy_receipt(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    prompt_version: str,
) -> tuple[TranscriptScanBinding, str, int] | None:
    try:
        if (
            str(row["observed_segments_json"]) != _LEGACY_MANIFEST
            or str(row["observed_segments_sha256"]) != _LEGACY_MANIFEST_SHA256
            or str(row["observed_source_sha256"]) != _EMPTY_SOURCE_SHA256
            or str(row["prompt_version"]) != prompt_version
        ):
            return None
        binding = _sealed_receipt_binding(conn, row)
        if binding is None:
            return None
        raw_items = cast(object, json.loads(str(row["output_manifest_json"])))
        if not isinstance(raw_items, list):
            return None
        commitment_ids: list[int] = []
        indicator_ids: list[int] = []
        for raw_item in cast(list[object], raw_items):
            if not isinstance(raw_item, dict):
                return None
            item = cast(dict[str, object], raw_item)
            raw_id = item.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
                return None
            if item.get("kind") == "commitment":
                commitment_ids.append(raw_id)
            elif item.get("kind") == "management_indicator":
                indicator_ids.append(raw_id)
            else:
                return None
        output_json = _output_manifest(
            conn,
            transcript_id=binding.transcript_id,
            commitment_ids=commitment_ids,
            management_indicator_ids=indicator_ids,
        )
        output_sha = _sha256(output_json)
        n_extracted = len(commitment_ids) + len(indicator_ids)
        receipt_id = _sha256(
            _legacy_receipt_payload(
                binding=binding,
                prompt_version=prompt_version,
                n_extracted=n_extracted,
                output_manifest_sha256=output_sha,
            )
        )
        if (
            output_json != str(row["output_manifest_json"])
            or output_sha != str(row["output_manifest_sha256"])
            or n_extracted != _strict_int(row["n_extracted"], positive=False)
            or receipt_id != str(row["receipt_id"])
        ):
            return None
        return binding, receipt_id, n_extracted
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def commitment_scan_coverage(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    prompt_version: str,
    cutoff_at: datetime | None = None,
) -> CommitmentScanCoverage:
    """Classify exact, historical-incomplete, invalid, and genuinely missing scans."""

    current = current_commitment_scan_receipt(
        conn,
        transcript_id=transcript_id,
        prompt_version=prompt_version,
        cutoff_at=cutoff_at,
    )
    if current is not None:
        return CommitmentScanCoverage(CommitmentScanCoverageState.COMPLETE, receipt=current)
    if not scan_receipt_schema_available(conn):
        return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
    binding = current_transcript_scan_binding(conn, transcript_id)
    if binding is None:
        return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
    params: tuple[object, ...] = (transcript_id, prompt_version)
    cutoff_clause = ""
    if cutoff_at is not None:
        cutoff_clause = " AND datetime(recorded_at)<=datetime(?)"
        params += (cutoff_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),)
    rows = conn.execute(
        "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
        "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
        "output_manifest_sha256,observed_segments_json,observed_segments_sha256,"
        "observed_source_sha256,recorded_at FROM commitment_scan_receipts "
        "WHERE transcript_id=? AND prompt_version=?"
        + cutoff_clause
        + " ORDER BY datetime(recorded_at) DESC,receipt_id DESC",
        params,
    ).fetchall()
    valid_other_source = False
    for row in rows:
        marker = str(row["observed_segments_json"])
        if marker != _LEGACY_MANIFEST:
            historical = _validated_historical_v2_receipt(
                conn,
                row,
                prompt_version=prompt_version,
            )
            if historical is None:
                return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
            if historical.binding == binding:
                # The binding is current, so failure of the current reader means
                # its selected segment bytes/context changed without a new source.
                return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
            valid_other_source = True
            continue
        legacy = _validated_legacy_receipt(conn, row, prompt_version=prompt_version)
        if legacy is None:
            return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
        legacy_binding, receipt_id, n_extracted = legacy
        if legacy_binding == binding:
            return CommitmentScanCoverage(
                CommitmentScanCoverageState.LEGACY_UNOBSERVED_REAUDIT_REQUIRED,
                legacy_receipt_id=receipt_id,
                legacy_n_extracted=n_extracted,
            )
        valid_other_source = True
    if valid_other_source:
        return CommitmentScanCoverage(CommitmentScanCoverageState.SOURCE_CHANGED_MISSING)
    log_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(commitment_scan_log)")}
    if {"transcript_id", "prompt_version", "scanned_at"} <= log_columns:
        log_params: tuple[object, ...] = (transcript_id, prompt_version)
        log_cutoff = ""
        if cutoff_at is not None:
            log_cutoff = " AND datetime(scanned_at)<=datetime(?)"
            log_params += (cutoff_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),)
        legacy_log = conn.execute(
            "SELECT 1 FROM commitment_scan_log WHERE transcript_id=? "
            "AND (prompt_version=? OR prompt_version IS NULL)" + log_cutoff + " LIMIT 1",
            log_params,
        ).fetchone()
        if legacy_log is not None:
            return CommitmentScanCoverage(CommitmentScanCoverageState.INVALID_REAUDIT_REQUIRED)
    return CommitmentScanCoverage(CommitmentScanCoverageState.NEVER_SCANNED_MISSING)


__all__ = [
    "CommitmentScanCoverage",
    "CommitmentScanCoverageState",
    "CommitmentScanReceipt",
    "ObservedTranscriptSegment",
    "TranscriptScanBinding",
    "TranscriptSegmentVersion",
    "append_commitment_scan_receipt",
    "commitment_scan_coverage",
    "current_commitment_scan_receipt",
    "current_transcript_scan_binding",
    "observe_transcript_segment",
    "scan_receipt_schema_available",
    "scan_receipt_table_available",
    "selected_segment_versions",
]
