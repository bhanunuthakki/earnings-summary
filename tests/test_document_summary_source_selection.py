"""Hermetic tests for exact registered source-period selection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.issuer_document_inventory import (
    IssuerDocumentInventoryError,
    resolve_registered_document_period,
)
from provenance.evidence_backfill import ensure_legacy_document_evidence
from provenance.issuer_registry_bootstrap import (
    SecSubmissionsIssuerRequest,
    bootstrap_sec_submissions_issuer,
)

_CIK = "0001691493"
_SEC_URL = f"https://data.sec.gov/submissions/CIK{_CIK}.json"
_SOURCE_URL = "https://issuer.example.test/nu/release.pdf"
_OBSERVED = datetime(2026, 10, 18, 4, 39, 34, tzinfo=UTC)
_RETRIEVED = datetime(2026, 10, 18, 4, 39, 35, tzinfo=UTC)
_RECORDED = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _source_body() -> bytes:
    return json.dumps(
        {
            "cik": int(_CIK),
            "name": "Nu Holdings Ltd.",
            "entityType": "operating",
            "tickers": ["NU"],
            "exchanges": ["NYSE"],
            "fiscalYearEnd": "0131",
        },
        separators=(",", ":"),
    ).encode()


def _seed_registered_document(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    period_end: str | None = "2026-07-31",
) -> tuple[sqlite3.Connection, str]:
    database = migrated_db(tmp_path / "document-period.db")
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA foreign_keys=ON")
    body = _source_body()
    bootstrap_sec_submissions_issuer(
        conn,
        request=SecSubmissionsIssuerRequest(
            ticker="NU",
            normalized_cik=_CIK,
            source_url=_SEC_URL,
            raw_body=body,
            blob_root=tmp_path / "sec-blobs",
            observed_at=_OBSERVED,
            retrieved_at=_RETRIEVED,
            recorded_at=_RECORDED,
            apply=True,
        ),
    )
    conn.row_factory = sqlite3.Row
    source_bytes = b"Synthetic reported quarterly release"
    relative = "ir_documents/NU/release.pdf"
    source_path = tmp_path / relative
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source_bytes)
    conn.execute(
        "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
        "fetched_at,fetch_status,raw_bytes_size,source_url) VALUES (1,?,?,?,?,?,?,?,?,?,?)",
        (
            "NU",
            "ir_doc",
            "ir_press_release",
            period_end,
            relative,
            hashlib.sha256(source_bytes).hexdigest(),
            _RECORDED.isoformat(),
            "ok",
            len(source_bytes),
            _SOURCE_URL,
        ),
    )
    ensure_legacy_document_evidence(conn, repo_root=tmp_path, document_id=1)
    row = conn.execute(
        "SELECT document_version_id FROM evidence_document_versions WHERE legacy_document_id=1"
    ).fetchone()
    assert row is not None
    conn.commit()
    return conn, str(row[0])


def test_resolves_current_source_period_with_repeatable_sec_witness_and_borrowed_connection(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, version_id = _seed_registered_document(tmp_path, migrated_db)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    try:
        first = resolve_registered_document_period(conn, version_id)
        second = resolve_registered_document_period(conn, version_id)

        assert first.request.ticker == "NU"
        assert (first.request.fiscal_year, first.request.fiscal_quarter) == (2027, 2)
        assert first.request.period_end.isoformat() == "2026-07-31"
        assert first.period_start is None
        assert first.request.expected_documents[0].source_url == _SOURCE_URL
        assert first.request.fiscal_period_witness is not None
        assert first.request.fiscal_period_witness.document_version_id == version_id
        assert first.request.fiscal_period_witness.fiscal_year_end == "01-31"
        assert first.request.canonical_json == second.request.canonical_json
        assert first.recorded_issuer_id == "legacy-ticker:NU"
        assert first.binding_revision_id
        assert first.binding_kind == "recorded_subject"
        assert conn.row_factory is sqlite3.Row
        assert conn.in_transaction
    finally:
        conn.rollback()
        conn.close()


def test_rejects_incomplete_or_wrong_fiscal_coordinates_before_generation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, version_id = _seed_registered_document(tmp_path, migrated_db)
    try:
        with pytest.raises(IssuerDocumentInventoryError, match="coordinates_incomplete"):
            resolve_registered_document_period(conn, version_id, fiscal_year=2027)
        with pytest.raises(IssuerDocumentInventoryError, match="period_mismatch"):
            resolve_registered_document_period(conn, version_id, fiscal_year=2027, fiscal_quarter=1)
    finally:
        conn.close()


def test_rejects_unknown_source_period_before_generation(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    conn, version_id = _seed_registered_document(tmp_path, migrated_db, period_end=None)
    try:
        with pytest.raises(IssuerDocumentInventoryError, match="period_unknown"):
            resolve_registered_document_period(conn, version_id)
    finally:
        conn.close()
