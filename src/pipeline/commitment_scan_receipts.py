"""Immutable, source-bound receipts for transcript commitment scans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from provenance.selection import selected_transcripts_relation

_TRANSCRIPT_NAME = re.compile(r"^(?P<ticker>[A-Z0-9.-]+)_Q(?P<quarter>[1-4])_(?P<year>[0-9]{4})$")


@dataclass(frozen=True)
class TranscriptScanBinding:
    transcript_id: int
    document_id: int
    transcript_acquisition_receipt_id: str
    transcript_sha256: str


@dataclass(frozen=True)
class CommitmentScanReceipt:
    receipt_id: str
    binding: TranscriptScanBinding
    prompt_version: str
    n_extracted: int
    output_manifest_json: str
    output_manifest_sha256: str
    recorded_at: str


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


def current_transcript_scan_binding(
    conn: sqlite3.Connection, transcript_id: int
) -> TranscriptScanBinding | None:
    """Resolve a selected transcript to its exact authorized acquisition receipt."""

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
    recorded_at: datetime | None = None,
) -> CommitmentScanReceipt:
    """Append or exactly replay one current transcript scan receipt."""

    if not scan_receipt_table_available(conn):
        raise RuntimeError("commitment_scan_receipts table is unavailable")
    version = prompt_version.strip()
    if not version:
        raise ValueError("commitment scan prompt_version is required")
    binding = current_transcript_scan_binding(conn, transcript_id)
    if binding is None:
        raise ValueError("current transcript lacks exact authorized acquisition evidence")
    output_json = _output_manifest(
        conn,
        transcript_id=transcript_id,
        commitment_ids=commitment_ids,
        management_indicator_ids=management_indicator_ids,
    )
    n_extracted = len(commitment_ids) + len(management_indicator_ids)
    output_sha = _sha256(output_json)
    receipt_id = _sha256(
        _receipt_payload(
            binding=binding,
            prompt_version=version,
            n_extracted=n_extracted,
            output_manifest_sha256=output_sha,
        )
    )
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
    )
    existing = conn.execute(
        "SELECT transcript_id,document_id,transcript_acquisition_receipt_id,transcript_sha256,"
        "prompt_version,n_extracted,output_manifest_json,output_manifest_sha256,recorded_at "
        "FROM commitment_scan_receipts WHERE receipt_id=?",
        (receipt_id,),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO commitment_scan_receipts "
            "(receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
        recorded_at=recorded,
    )


def _validate_receipt_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    binding: TranscriptScanBinding,
    prompt_version: str,
) -> CommitmentScanReceipt | None:
    try:
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
            item_id = int(str(item.get("id")))
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
        receipt_id = _sha256(
            _receipt_payload(
                binding=binding,
                prompt_version=prompt_version,
                n_extracted=n_extracted,
                output_manifest_sha256=output_sha,
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
            recorded_at=str(row["recorded_at"]),
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


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
    params: tuple[object, ...] = (transcript_id, prompt_version)
    if cutoff_at is None:
        row = conn.execute(
            "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256,recorded_at FROM commitment_scan_receipts "
            "WHERE transcript_id=? AND prompt_version=? "
            "ORDER BY datetime(recorded_at) DESC,receipt_id DESC LIMIT 1",
            params,
        ).fetchone()
    else:
        params += (cutoff_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),)
        row = conn.execute(
            "SELECT receipt_id,transcript_id,document_id,transcript_acquisition_receipt_id,"
            "transcript_sha256,prompt_version,n_extracted,output_manifest_json,"
            "output_manifest_sha256,recorded_at FROM commitment_scan_receipts "
            "WHERE transcript_id=? AND prompt_version=? "
            "AND datetime(recorded_at)<=datetime(?) "
            "ORDER BY datetime(recorded_at) DESC,receipt_id DESC LIMIT 1",
            params,
        ).fetchone()
    return (
        None
        if row is None
        else _validate_receipt_row(
            conn,
            row,
            binding=binding,
            prompt_version=prompt_version,
        )
    )


__all__ = [
    "CommitmentScanReceipt",
    "TranscriptScanBinding",
    "append_commitment_scan_receipt",
    "current_commitment_scan_receipt",
    "current_transcript_scan_binding",
    "scan_receipt_table_available",
]
