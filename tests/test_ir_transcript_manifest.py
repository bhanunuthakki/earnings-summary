"""Explicit KPI manifests must bind transcript values to stored source segments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from execution import extract_kpis_from_ir as extractor
from models.documents import SourceType
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue
from pipeline.locators import transcript_span_locator


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    database = migrated_db(tmp_path / "transcript-manifest.sqlite")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _manifest(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    ticker: str = "NU",
    excerpt: str = "Quarterly revenue was $120 million.",
) -> KpiExtractionManifest:
    period_end = datetime(2025, 3, 31)
    segment_text = "Quarterly revenue was $120 million. This is the reported current quarter."
    source_path = repo_root / "ir_documents" / "NU" / "transcript.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = segment_text.encode("utf-8")
    source_path.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    document = conn.execute(
        "INSERT INTO documents "
        "(ticker,source_type,doc_type,period_end,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
        "VALUES ('NU','ir_doc','ir_transcript',?,?,?,?, 'ok',?)",
        (
            period_end.isoformat(),
            str(source_path),
            source_sha256,
            "2025-04-01T00:00:00Z",
            len(source_bytes),
        ),
    )
    assert document.lastrowid is not None
    document_id = int(document.lastrowid)
    transcript = conn.execute(
        "INSERT INTO transcripts (document_id,ticker,fiscal_period_type,period_end) "
        "VALUES (?, 'NU','Q1',?)",
        (document_id, period_end.isoformat()),
    )
    assert transcript.lastrowid is not None
    transcript_id = int(transcript.lastrowid)
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id,seq,speaker,text) VALUES (?,0,'CFO',?)",
        (transcript_id, segment_text),
    )
    conn.commit()
    from provenance.evidence_backfill import ensure_legacy_document_evidence

    ensure_legacy_document_evidence(conn, repo_root=repo_root, document_id=document_id)
    conn.commit()
    start = segment_text.index(excerpt) if excerpt in segment_text else 0
    value = KpiValue(
        name="Quarterly revenue",
        value=Decimal("120000000"),
        unit=Unit.ACTUAL,
        currency=Currency.USD,
        source_excerpt=excerpt,
        locator=transcript_span_locator(
            transcript_line=0,
            speaker_turn_index=0,
            char_start=start,
            char_end=start + len(excerpt),
            verbatim_snippet=excerpt,
        ),
    )
    return KpiExtractionManifest(
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=document_id,
        primary_source=SourceType.IR_DOC,
        values=[value],
    )


def test_apply_manifest_persists_original_transcript_segment_locator(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest(conn, tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifests": [manifest.model_dump(mode="json")]}), encoding="utf-8")

    def open_test_db(_path: str | Path) -> sqlite3.Connection:
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(extractor, "open_db", open_test_db)
    monkeypatch.setattr(
        sys, "argv", ["extract_kpis_from_ir", "--apply", str(path), "--db", "unused"]
    )

    assert extractor.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["failed"] == 0
    assert result["inserted"] == 1
    row = conn.execute(
        "SELECT source_doc_id,source_excerpt,locator FROM kpi_facts WHERE ticker='NU'"
    ).fetchone()
    assert row is not None
    assert row["source_doc_id"] == manifest.source_doc_id
    assert row["source_excerpt"] == manifest.values[0].source_excerpt
    locator = FactLocator.model_validate_json(row["locator"])
    assert locator.transcript_line == 0
    assert locator.transcript_span is not None
    assert locator.transcript_span.speaker_turn_index == 0


@pytest.mark.parametrize(
    ("ticker", "excerpt"),
    [("OTHER", "Quarterly revenue was $120 million."), ("NU", "Revenue was $120 million.")],
)
def test_apply_manifest_rejects_transcript_identity_or_segment_quote_mismatch(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ticker: str,
    excerpt: str,
) -> None:
    manifest = _manifest(conn, tmp_path, ticker=ticker, excerpt=excerpt)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifests": [manifest.model_dump(mode="json")]}), encoding="utf-8")

    def open_test_db(_path: str | Path) -> sqlite3.Connection:
        db_path = str(conn.execute("PRAGMA database_list").fetchone()[2])
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(extractor, "open_db", open_test_db)
    monkeypatch.setattr(
        sys, "argv", ["extract_kpis_from_ir", "--apply", str(path), "--db", "unused"]
    )

    assert extractor.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["failed"] == 1
    assert result["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 0
