"""Typed, fail-closed completeness reads for source documents.

The issuer coverage ledger stores one row per expected fact.  This module
reassembles those immutable rows into the existing document coverage receipt
and exposes the only read contract used by extraction queues: a document is
complete only when every expected result is captured or explicitly rejected.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from pipeline.issuer_document_coverage import IssuerDocumentCoverageReceipt
from provenance.source_coverage import IssuerFactCoverageReceiptRecord


class DocumentCompletenessStatus(StrEnum):
    COMPLETE = "complete"
    PENDING = "pending"


class DocumentCompletenessReason(StrEnum):
    TERMINAL_RECEIPT = "terminal_receipt"
    NO_RECEIPT = "no_receipt"
    PARTIAL_RECEIPT = "partial_receipt"
    INVALID_RECEIPT = "invalid_receipt"
    RECEIPT_LEDGER_UNAVAILABLE = "receipt_ledger_unavailable"


class DocumentCompleteness(BaseModel):
    """The typed result of evaluating one source document's receipt ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: DocumentCompletenessStatus
    reason: DocumentCompletenessReason
    receipt_group_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _receipt_envelope(row: sqlite3.Row) -> dict[str, object]:
    payload_raw: object = json.loads(str(row["receipt_json"]))
    if not isinstance(payload_raw, dict) or not isinstance(payload_raw.get("receipt"), dict):
        raise ValueError("receipt payload envelope is missing")
    envelope = cast(dict[str, object], payload_raw["receipt"]).copy()
    # Rejection evidence is intentionally omitted from captured rows by the
    # append boundary, so it cannot make one document receipt look like two.
    envelope["rejection_frame_json"] = None
    envelope["rejection_frame_sha256"] = None
    return envelope


def _receipt_from_group(
    rows: list[sqlite3.Row], document_id: int, document_ticker: str
) -> IssuerDocumentCoverageReceipt:
    """Validate and reassemble one fact-level reconciliation group."""
    if not rows:
        raise ValueError("receipt group is empty")
    records = [IssuerFactCoverageReceiptRecord.model_validate(dict(row)) for row in rows]
    envelopes: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for record in records:
        payload_raw: object = json.loads(record.receipt_json)
        if not isinstance(payload_raw, dict):
            raise ValueError("receipt payload must be an object")
        payload = cast(dict[str, object], payload_raw)
        raw_envelope = payload.get("receipt")
        raw_result = payload.get("result")
        if not isinstance(raw_envelope, dict):
            raise ValueError("receipt payload envelope is missing")
        envelopes.append(cast(dict[str, object], raw_envelope))
        if record.fact_identity == "__zero_expected_population__":
            if raw_result is not None:
                raise ValueError("zero-population receipt has a fact result")
        elif not isinstance(raw_result, dict):
            raise ValueError("receipt payload result is missing")
        else:
            result = cast(dict[str, object], raw_result)
            expected = result.get("expected")
            if not isinstance(expected, dict):
                raise ValueError("receipt result expected fact is missing")
            results.append(result)
    merged_envelope = envelopes[0].copy()
    for envelope in envelopes[1:]:
        for field, value in envelope.items():
            prior = merged_envelope.get(field)
            if prior is not None and value is not None and prior != value:
                raise ValueError("receipt group envelopes disagree")
            if prior is None and value is not None:
                merged_envelope[field] = value
    if any(row["document_id"] != document_id for row in rows):
        raise ValueError("receipt group document mismatch")
    receipt = IssuerDocumentCoverageReceipt.model_validate(merged_envelope | {"results": results})
    if receipt.document_id != document_id or receipt.ticker.upper() != document_ticker.upper():
        raise ValueError("receipt document mismatch")
    if receipt.application_manifest_json is not None:
        # Fact-level persistence does not retain the manifest list position;
        # restore it before the existing complete-set validator compares the
        # typed identities.  The manifest parser also rechecks its hash.
        from pipeline.issuer_fact_manifest import IssuerFactManifest

        manifest = IssuerFactManifest.model_validate_json(receipt.application_manifest_json)
        order = {expected.identity_key: index for index, expected in enumerate(manifest.expected)}
        receipt = receipt.model_copy(
            update={
                "results": sorted(
                    receipt.results, key=lambda item: order[item.expected.identity_key]
                )
            }
        )
    receipt.validate_application_manifest_population()
    as_of = receipt.as_of.isoformat() if receipt.as_of is not None else "current"
    stale_before = receipt.stale_before.isoformat() if receipt.stale_before is not None else "none"
    expected_keys = {
        f"{document_id}|{result.expected.identity_key}|{as_of}|{stale_before}"
        for result in receipt.results
    }
    if not receipt.results:
        expected_keys = {f"{document_id}|__zero_expected_population__|{as_of}|{stale_before}"}
    if any(row["reconciliation_key"] not in expected_keys for row in rows):
        raise ValueError("receipt reconciliation key does not match typed receipt")
    if len(rows) != len(expected_keys):
        raise ValueError("receipt group does not contain exactly one row per expected fact")
    identities = {result.expected.identity_key for result in receipt.results}
    if len(identities) != len(receipt.results):
        raise ValueError("receipt group contains duplicate expected facts")
    if any(
        row["fact_identity"] != "__zero_expected_population__"
        and row["fact_identity"] not in identities
        for row in rows
    ):
        raise ValueError("receipt row fact identity is not in the typed receipt")
    if not receipt.results and rows[0]["fact_identity"] != "__zero_expected_population__":
        raise ValueError("zero-population receipt row has a fact identity")
    return receipt


def document_completeness(conn: sqlite3.Connection, document_id: int) -> DocumentCompleteness:
    """Assess one document using only a valid terminal coverage receipt.

    Missing receipt tables, malformed rows, and unknown result statuses all
    fail closed as pending.  Existing ``kpi_facts`` rows are deliberately not
    consulted for completion; they are evidence only after the receipt has
    declared the expected population.
    """
    try:
        document = conn.execute(
            "SELECT ticker FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        if document is None:
            return DocumentCompleteness(
                status=DocumentCompletenessStatus.PENDING,
                reason=DocumentCompletenessReason.INVALID_RECEIPT,
            )
        rows = conn.execute(
            "SELECT record_id,idempotency_key,reconciliation_key,document_id,ticker,"
            "fact_identity,receipt_json,receipt_sha256,recorded_at "
            "FROM issuer_fact_coverage_receipts WHERE document_id=? "
            "ORDER BY reconciliation_key, record_id",
            (document_id,),
        ).fetchall()
    except sqlite3.Error:
        return DocumentCompleteness(
            status=DocumentCompletenessStatus.PENDING,
            reason=DocumentCompletenessReason.RECEIPT_LEDGER_UNAVAILABLE,
        )
    if not rows:
        return DocumentCompleteness(
            status=DocumentCompletenessStatus.PENDING,
            reason=DocumentCompletenessReason.NO_RECEIPT,
        )
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        # The persistence boundary intentionally gives each fact row its own
        # reconciliation key.  The repeated, hash-bound receipt envelope is
        # the document-level group key that joins those rows back together.
        try:
            envelope = _receipt_envelope(row)
            group_key = _canonical_json(envelope)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            group_key = str(row["record_id"])
        groups.setdefault(group_key, []).append(row)
    saw_partial = False
    for key, group in groups.items():
        try:
            receipt = _receipt_from_group(group, document_id, str(document["ticker"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if receipt.missing_count:
            saw_partial = True
            continue
        if receipt.application_manifest_json is None:
            # A fact-level receipt proves only what was returned. The immutable
            # application manifest is the authority for the full expected
            # population, so a terminal claim without it is invalid.
            continue
        return DocumentCompleteness(
            status=DocumentCompletenessStatus.COMPLETE,
            reason=DocumentCompletenessReason.TERMINAL_RECEIPT,
            receipt_group_key=hashlib.sha256(key.encode("utf-8")).hexdigest(),
        )
    return DocumentCompleteness(
        status=DocumentCompletenessStatus.PENDING,
        reason=(
            DocumentCompletenessReason.PARTIAL_RECEIPT
            if saw_partial
            else DocumentCompletenessReason.INVALID_RECEIPT
        ),
    )
