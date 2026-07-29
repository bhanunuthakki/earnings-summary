from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from filings.inline_xbrl_processor import ProcessorPackageMember
from provenance.sec_filing_xbrl_ingest import (
    _append_offline_artifacts,
    _original_recorded_at,
    file_uri_path,
    persist_exact,
)


def test_file_uri_rejects_authority_and_relative_path() -> None:
    with pytest.raises(ValueError, match="local file URI"):
        file_uri_path("file://server/share/filing.htm")
    with pytest.raises(ValueError, match="absolute"):
        file_uri_path("file:relative/filing.htm")


def test_exact_replay_preserves_original_recorded_at_and_rejects_conflict() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE ledger ("
        "row_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,"
        "payload TEXT NOT NULL,recorded_at TEXT NOT NULL)"
    )
    original = datetime(2026, 7, 28, tzinfo=UTC)
    replayed_at = original + timedelta(days=1)
    columns = ("row_id", "idempotency_key", "payload", "recorded_at")
    persist_exact(
        conn,
        table="ledger",
        columns=columns,
        values=("row-1", "key-1", "sealed", original),
    )
    persist_exact(
        conn,
        table="ledger",
        columns=columns,
        values=("row-1", "key-1", "sealed", replayed_at),
    )
    assert conn.execute("SELECT recorded_at FROM ledger").fetchone() == (
        str(original),
    )
    with pytest.raises(ValueError, match="conflicts"):
        persist_exact(
            conn,
            table="ledger",
            columns=columns,
            values=("row-1", "key-1", "changed", replayed_at),
        )


def test_offline_artifact_requires_source_evidence_available_at_cutoff(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "us-gaap.xsd"
    artifact_path.write_bytes(b"taxonomy")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE evidence_content_blobs ("
        "sha256 TEXT PRIMARY KEY,byte_size INTEGER,media_type TEXT,"
        "storage_uri TEXT,recorded_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE evidence_source_observations ("
        "observation_id TEXT PRIMARY KEY,source_url TEXT,blob_sha256 TEXT,"
        "retrieved_at TEXT)"
    )
    cutoff = datetime(2026, 7, 29, tzinfo=UTC)
    source_url = "https://xbrl.fasb.org/us-gaap/2026/us-gaap-2026.xsd"
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (
            digest,
            artifact_path.stat().st_size,
            "application/xml",
            artifact_path.as_uri(),
            cutoff - timedelta(days=2),
        ),
    )
    member = ProcessorPackageMember(
        member_ordinal=0,
        member_role="standard_taxonomy",
        source_url=source_url,
        local_path=artifact_path,
        blob_sha256=digest,
        byte_size=artifact_path.stat().st_size,
        media_type="application/xml",
    )
    with pytest.raises(ValueError, match="source observation"):
        _append_offline_artifacts(
            conn,
            captured=(),
            additional=(member,),
            issuer_id="issuer-1",
            accession_number="0000000001-26-000001",
            knowledge_cutoff=cutoff,
        )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?,?)",
        ("observation-1", source_url, digest, cutoff + timedelta(days=1)),
    )
    with pytest.raises(ValueError, match="source observation"):
        _append_offline_artifacts(
            conn,
            captured=(),
            additional=(member,),
            issuer_id="issuer-1",
            accession_number="0000000001-26-000001",
            knowledge_cutoff=cutoff,
        )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?,?)",
        ("observation-2", source_url, digest, cutoff - timedelta(days=1)),
    )
    admitted = _append_offline_artifacts(
        conn,
        captured=(),
        additional=(member,),
        issuer_id="issuer-1",
        accession_number="0000000001-26-000001",
        knowledge_cutoff=cutoff,
    )
    assert admitted[0].member_ordinal == 0


def test_replay_refuses_incomplete_or_non_atomic_durable_closure() -> None:
    conn = sqlite3.connect(":memory:")
    stamp = datetime(2026, 7, 29, tzinfo=UTC)
    conn.execute(
        "CREATE TABLE evidence_extraction_runs ("
        "extraction_run_id TEXT PRIMARY KEY,started_at TEXT,completed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?)",
        ("run-1", stamp, stamp),
    )
    for ddl in (
        "CREATE TABLE filing_xbrl_extraction_input_members "
        "(extraction_run_id TEXT,recorded_at TEXT)",
        "CREATE TABLE evidence_nodes (extraction_run_id TEXT,recorded_at TEXT)",
        "CREATE TABLE filing_xbrl_raw_fact_commitments "
        "(extraction_run_id TEXT,recorded_at TEXT)",
        "CREATE TABLE filing_xbrl_footnote_commitments "
        "(extraction_run_id TEXT,recorded_at TEXT)",
        "CREATE TABLE filing_xbrl_extraction_dispositions "
        "(extraction_run_id TEXT,recorded_at TEXT,knowledge_at TEXT)",
        "CREATE TABLE filing_xbrl_extraction_input_seals "
        "(extraction_run_id TEXT,member_count INTEGER,raw_fact_count INTEGER,"
        "footnote_count INTEGER,recorded_at TEXT)",
        "CREATE TABLE filing_xbrl_extraction_disposition_seals "
        "(extraction_run_id TEXT,entry_count INTEGER,recorded_at TEXT,knowledge_at TEXT)",
    ):
        conn.execute(ddl)
    with pytest.raises(ValueError, match="incomplete historical"):
        _original_recorded_at(conn, run_id="run-1", proposed=stamp + timedelta(days=1))
    conn.execute(
        "INSERT INTO filing_xbrl_extraction_input_seals VALUES (?,?,?,?,?)",
        ("run-1", 0, 0, 0, stamp),
    )
    conn.execute(
        "INSERT INTO filing_xbrl_extraction_disposition_seals VALUES (?,?,?,?)",
        ("run-1", 0, stamp, stamp + timedelta(seconds=1)),
    )
    with pytest.raises(ValueError, match="non-atomic durable clocks"):
        _original_recorded_at(conn, run_id="run-1", proposed=stamp + timedelta(days=1))
