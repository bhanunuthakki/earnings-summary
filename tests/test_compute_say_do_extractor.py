"""Tests for src/compute/say_do_extractor.py — automated LLM extraction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Never

import pytest

from compute.management_indicators import ManagementIndicatorSchemaError
from compute.say_do import persist_manifest
from compute.say_do_extractor import (
    MAX_TRANSCRIPT_CHARS,
    CommitmentParseError,
    TranscriptContext,
    build_extraction_prompt,
    extract_for_transcript,
    fetch_kpi_catalog,
    fetch_transcript_text_and_segment,
    parse_llm_response,
    record_scan,
    transcripts_pending_extraction,
    transcripts_without_scan_receipt,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL DEFAULT 'transcript',
            UNIQUE(ticker, name)
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetch_status TEXT NOT NULL DEFAULT 'ok'
        );
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP,
            fiscal_period_type TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC NOT NULL,
            unit TEXT NOT NULL,
            narrative TEXT NOT NULL
        );
        CREATE TABLE commitment_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL UNIQUE,
            scanned_at TEXT NOT NULL,
            n_extracted INTEGER NOT NULL,
            prompt_version TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _seed_transcript(
    conn: sqlite3.Connection, ticker: str, text: str, period_end: str
) -> tuple[int, int]:
    """Insert a transcript + one segment. Returns (transcript_id, segment_id)."""
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256) "
        "VALUES (?, 'transcript_audio', 'earnings_call_transcript', ?, ?)",
        (ticker, f"{ticker}_transcript.txt", "fakehash"),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO transcripts (document_id, ticker, period_end) VALUES (?, ?, ?)",
        (doc_id, ticker, period_end),
    )
    transcript_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (?, 0, ?)",
        (transcript_id, text),
    )
    segment_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return (transcript_id, segment_id)


def _seed_kpi_def(conn: sqlite3.Connection, ticker: str, name: str, unit: str = "percent") -> None:
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source) VALUES (?, ?, ?, ?)",
        (ticker, name, unit, "transcript"),
    )
    conn.commit()


def _active_conn(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    *,
    ticker: str,
    text: str,
    period_end: str = "2025-12-31",
) -> tuple[sqlite3.Connection, int]:
    path = migrated_db(tmp_path / f"{ticker.lower()}-active.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    digest = hashlib.sha256(text.encode()).hexdigest()
    filename = f"{ticker}_Q4_2025.txt"
    conn.execute(
        "INSERT INTO documents "
        "(ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) "
        "VALUES (?,'ir_doc','ir_transcript',?,?,?,'ok',?)",
        (ticker, f"transcripts/processed/{filename}", digest, "2026-01-15", len(text.encode())),
    )
    document_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO transcripts "
        "(document_id,ticker,call_date,fiscal_period_type,period_end,source,is_active,is_current,"
        "recorded_at) VALUES (?,?,?,'Q4',?,'issuer_ir',1,1,?)",
        (document_id, ticker, "2026-01-15", period_end, "2026-01-15"),
    )
    transcript_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO transcript_segments "
        "(transcript_id,seq,speaker,time_code_start,time_code_end,text) "
        "VALUES (?,0,'CEO','00:00:00',NULL,?)",
        (transcript_id, text),
    )
    idempotency_key = "transcript:" + "1" * 64
    contract_sha = "2" * 64
    authorization = {
        "idempotency_key": idempotency_key,
        "request": {
            "canonical_ticker": ticker,
            "document_type": "earnings_call_transcript",
            "fiscal_quarter": 4,
            "fiscal_year": 2025,
            "provider": "issuer_ir",
            "source_regime_identity": {"contract_sha256": contract_sha, "regime": "combined"},
            "source_type": "ir_doc",
        },
        "schema_version": "transcript-acquisition-authorization@1",
        "status": "authorized",
    }
    artifact = {
        "authorization": authorization,
        "canonical_document_path": f"transcripts/raw/{filename}",
        "document_id": document_id,
        "schema_version": "authorized-transcript-artifact@1",
        "source_url": None,
        "staged": {"sha256": digest, "size_bytes": len(text.encode())},
    }
    artifact_json = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    receipt_id = hashlib.sha256(artifact_json.encode()).hexdigest()
    for trigger in (
        "trg_transcript_acquisition_receipts_validate",
        "trg_transcript_acquisition_receipts_stored_target_binding",
        "trg_transcript_acquisition_receipts_document_binding",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")  # nosec B608 -- test constant
    conn.execute(
        "INSERT INTO transcript_acquisition_receipts "
        "(receipt_id,idempotency_key,document_id,canonical_ticker,fiscal_year,fiscal_quarter,"
        "canonical_document_path,artifact_sha256,artifact_size_bytes,source_url,provider,"
        "source_type,document_type,source_regime,source_regime_contract_sha256,"
        "authorization_json,artifact_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            idempotency_key,
            document_id,
            ticker,
            2025,
            4,
            f"transcripts/raw/{filename}",
            digest,
            len(text.encode()),
            None,
            "issuer_ir",
            "ir_doc",
            "earnings_call_transcript",
            "combined",
            contract_sha,
            json.dumps(authorization, sort_keys=True, separators=(",", ":")),
            artifact_json,
            "2026-01-15T00:00:00Z",
        ),
    )
    conn.commit()
    return conn, transcript_id


# ---------------------------------------------------------------------------
# fetch_kpi_catalog
# ---------------------------------------------------------------------------


def test_fetch_kpi_catalog_returns_ticker_kpis(conn: sqlite3.Connection) -> None:
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    _seed_kpi_def(conn, "AMZN", "FCF Margin", "percent")
    _seed_kpi_def(conn, "GOOG", "GCP Revenue Growth", "percent")

    catalog = fetch_kpi_catalog(conn, "AMZN")
    assert sorted(catalog) == [
        ("AWS Revenue Growth", "percent"),
        ("FCF Margin", "percent"),
    ]


def test_fetch_kpi_catalog_case_insensitive_ticker(conn: sqlite3.Connection) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    assert fetch_kpi_catalog(conn, "amzn") == [("X", "percent")]


def test_fetch_kpi_catalog_empty_for_unknown_ticker(conn: sqlite3.Connection) -> None:
    assert fetch_kpi_catalog(conn, "ZZZ") == []


# ---------------------------------------------------------------------------
# transcripts_pending_extraction
# ---------------------------------------------------------------------------


def test_transcripts_pending_extraction_includes_unprocessed(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    pending = transcripts_pending_extraction(conn)
    assert [(p[0], p[1]) for p in pending] == [(tid, "AMZN")]


def test_transcripts_pending_extraction_excludes_already_processed(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    _, sid = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    conn.execute(
        "INSERT INTO management_commitments "
        "(ticker, period_made, transcript_segment_id, period_target, kpi_name, "
        " comparator, target_value, unit, narrative) "
        "VALUES ('AMZN', ?, ?, ?, 'X', 'ge', '5', 'percent', 'n')",
        (datetime(2025, 12, 31), sid, datetime(2026, 3, 31)),
    )
    conn.commit()
    assert transcripts_pending_extraction(conn) == []


def test_unreceipted_legacy_commitment_still_requires_exact_scan(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    tid, sid = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    conn.execute(
        "INSERT INTO management_commitments "
        "(ticker,period_made,transcript_segment_id,period_target,kpi_name,"
        "comparator,target_value,unit,narrative) "
        "VALUES ('AMZN',?,?,?,?, 'ge','5','percent','n')",
        (datetime(2025, 12, 31), sid, datetime(2026, 3, 31), "X"),
    )
    conn.commit()

    assert [item[0] for item in transcripts_without_scan_receipt(conn)] == [tid]


def test_transcripts_pending_extraction_filters_by_ticker(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    _seed_kpi_def(conn, "GOOG", "Y")
    _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    tid_g, _ = _seed_transcript(conn, "GOOG", "text", "2025-12-31")
    pending = transcripts_pending_extraction(conn, ticker="GOOG")
    assert [p[0] for p in pending] == [tid_g]


def test_transcripts_pending_extraction_excludes_scanned(
    conn: sqlite3.Connection,
) -> None:
    """A recorded scan — even one that found ZERO commitments — removes the
    transcript from the pending set (kills the daily re-scan loop)."""
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    conn.execute(
        "INSERT INTO commitment_scan_log "
        "(transcript_id,scanned_at,n_extracted,prompt_version) VALUES (?,datetime('now'),0,'v1')",
        (tid,),
    )
    conn.commit()
    assert transcripts_pending_extraction(conn) == []


def test_transcripts_pending_extraction_includes_no_catalog_tickers_for_novel_indicators(
    conn: sqlite3.Connection,
) -> None:
    """Novel management indicators remain valuable without a KPI catalog."""
    tid, _ = _seed_transcript(conn, "ZZZ", "text", "2025-12-31")
    assert [p[0] for p in transcripts_pending_extraction(conn)] == [tid]


def test_transcripts_pending_degrades_without_scan_log_table(
    conn: sqlite3.Connection,
) -> None:
    """Legacy selection can inspect the schema, while the writer fails closed."""
    conn.execute("DROP TABLE commitment_scan_log")
    conn.commit()
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    assert [p[0] for p in transcripts_pending_extraction(conn)] == [tid]
    with pytest.raises(RuntimeError, match="scan log schema"):
        record_scan(conn, tid, n_extracted=0)
    assert [p[0] for p in transcripts_pending_extraction(conn)] == [tid]


def test_record_scan_requires_exact_receipt_schema(conn: sqlite3.Connection) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    with pytest.raises(RuntimeError, match="segment manifest schema"):
        record_scan(conn, tid, n_extracted=0, prompt_version="v1")
    assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# build_extraction_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_kpi_catalog_lines() -> None:
    prompt = build_extraction_prompt(
        ticker="AMZN",
        transcript_text="some text",
        kpi_catalog=[("AWS Revenue Growth", "percent"), ("FCF Margin", "percent")],
        period_made=datetime(2025, 12, 31),
    )
    assert "AWS Revenue Growth  (unit: percent)" in prompt
    assert "FCF Margin  (unit: percent)" in prompt


def test_build_prompt_handles_empty_catalog() -> None:
    prompt = build_extraction_prompt(
        ticker="ZZZ",
        transcript_text="some text",
        kpi_catalog=[],
        period_made=datetime(2025, 12, 31),
    )
    assert "no KPIs defined for this ticker" in prompt


def test_build_prompt_truncates_long_transcript_and_notes_it() -> None:
    long_text = "x" * (MAX_TRANSCRIPT_CHARS + 1000)
    prompt = build_extraction_prompt(
        ticker="X",
        transcript_text=long_text,
        kpi_catalog=[("Revenue YoY Growth (USD)", "percent")],
        period_made=datetime(2025, 12, 31),
    )
    assert "transcript was truncated" in prompt


def test_build_prompt_includes_iso_period_made() -> None:
    prompt = build_extraction_prompt(
        ticker="X",
        transcript_text="t",
        kpi_catalog=[],
        period_made=datetime(2025, 9, 30),
    )
    assert "CALL DATE (period_made): 2025-09-30" in prompt


# ---------------------------------------------------------------------------
# parse_llm_response
# ---------------------------------------------------------------------------


_CTX = TranscriptContext(
    ticker="AMZN",
    period_made=datetime(2025, 12, 31),
    transcript_segment_id=42,
)


def test_parse_well_formed_response_produces_manifest() -> None:
    response = """{
      "commitments": [
        {
          "kpi_name": "AWS Revenue Growth",
          "comparator": "ge",
          "target_value": "20",
          "unit": "percent",
          "period_target": "2026-03-31",
          "narrative": "We expect AWS to grow at least 20%."
        }
      ],
      "novel_indicators": []
    }"""
    manifest = parse_llm_response(response, context=_CTX)
    assert len(manifest.commitments) == 1
    c = manifest.commitments[0]
    assert c.ticker == "AMZN"
    assert c.period_made == datetime(2025, 12, 31)
    assert c.transcript_segment_id == 42
    assert c.kpi_name == "AWS Revenue Growth"
    assert c.target_value == Decimal("20")


def test_parse_strips_markdown_fences() -> None:
    response = '```json\n{"commitments": [], "novel_indicators": []}\n```'
    manifest = parse_llm_response(response, context=_CTX)
    assert manifest.commitments == []


def test_markdown_wrapped_kpi_name_persists_plain(conn: sqlite3.Connection) -> None:
    """Persist boundary: kpi_name is a SCALAR — an LLM response wrapping it
    in `**bold**` (observed live 2026-08-02) must land plain in
    management_commitments. The strip lives on CommitmentInput so both the
    --auto parse path and an --apply manifest hit it."""
    _, segment_id = _seed_transcript(conn, "NU", "We expect risk-adj. NIM...", "2025-12-31")
    ctx = TranscriptContext(
        ticker="NU",
        period_made=datetime(2025, 12, 31),
        transcript_segment_id=segment_id,
    )
    response = """{
      "commitments": [
        {
          "kpi_name": "**Risk-adj. NIM**",
          "comparator": "ge",
          "target_value": "10",
          "unit": "percent",
          "period_target": "2026-03-31",
          "narrative": "We expect risk-adjusted NIM of at least 10%."
        }
      ],
      "novel_indicators": []
    }"""
    manifest = parse_llm_response(response, context=ctx)
    assert manifest.commitments[0].kpi_name == "Risk-adj. NIM"
    persist_manifest(conn, manifest)
    (kpi_name,) = conn.execute("SELECT kpi_name FROM management_commitments").fetchone()
    assert kpi_name == "Risk-adj. NIM"


def test_parse_invalid_json_raises() -> None:
    """Unusable response must NOT degrade to an empty manifest — an empty
    manifest reads as a legitimate zero-commitment scan and would be recorded
    in commitment_scan_log (the silent-empty pathology)."""
    with pytest.raises(CommitmentParseError, match="not valid JSON"):
        parse_llm_response("not json at all", context=_CTX)


def test_parse_invalid_shape_raises() -> None:
    """A response with a bad enum value fails top-level Pydantic → raises."""
    response = """{
      "commitments": [
        {
          "kpi_name": "Bad",
          "comparator": "BOGUS_COMPARATOR",
          "target_value": "20",
          "unit": "percent",
          "period_target": "2026-03-31",
          "narrative": "bad"
        }
      ]
    }"""
    with pytest.raises(CommitmentParseError, match="schema validation"):
        parse_llm_response(response, context=_CTX)


def test_parse_empty_commitments_array() -> None:
    manifest = parse_llm_response('{"commitments": [], "novel_indicators": []}', context=_CTX)
    assert manifest.commitments == []


# ---------------------------------------------------------------------------
# extract_for_transcript (orchestrator) — uses stub LLM
# ---------------------------------------------------------------------------


def test_extract_for_transcript_end_to_end(conn: sqlite3.Connection) -> None:
    """Full path: seed DB, stub LLM, verify manifest carries injected context."""
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    transcript_id, segment_id = _seed_transcript(
        conn, "AMZN", "We expect AWS to grow at least 20% next quarter.", "2025-12-31"
    )

    captured_prompt: list[str] = []

    def stub_llm(prompt: str) -> str:
        captured_prompt.append(prompt)
        return """{
          "commitments": [{
            "kpi_name": "AWS Revenue Growth",
            "comparator": "ge",
            "target_value": "20",
            "unit": "percent",
            "period_target": "2026-03-31",
            "narrative": "We expect AWS to grow at least 20% next quarter."
          }],
          "novel_indicators": []
        }"""

    manifest = extract_for_transcript(conn, transcript_id, llm_call=stub_llm)
    assert len(manifest.commitments) == 1
    c = manifest.commitments[0]
    assert c.ticker == "AMZN"
    assert c.transcript_segment_id == segment_id
    assert c.period_made == datetime(2025, 12, 31)
    assert c.target_value == Decimal("20")
    assert "AWS Revenue Growth" in captured_prompt[0]
    assert "TICKER: AMZN" in captured_prompt[0]


def test_extract_raises_for_unknown_transcript(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="not found"):
        extract_for_transcript(
            conn, 9999, llm_call=lambda p: '{"commitments": [], "novel_indicators": []}'
        )


def test_extract_scans_empty_catalog_for_novel_indicators(conn: sqlite3.Connection) -> None:
    """No catalog still merits a scan for a staged, unpromoted measurement."""
    transcript_id, _ = _seed_transcript(
        conn, "ZZZ", "We launched 42 new enterprise pilots.", "2025-12-31"
    )
    calls: list[str] = []

    def stub_llm(prompt: str) -> str:
        calls.append(prompt)
        return """{
          "commitments": [],
          "novel_indicators": [{
            "raw_label": "New enterprise pilots",
            "value": "42",
            "unit": "count",
            "scope": "product",
            "recurrence": "one_off",
            "source_excerpt": "We launched 42 new enterprise pilots."
          }]
        }"""

    manifest = extract_for_transcript(conn, transcript_id, llm_call=stub_llm)
    assert manifest.commitments == []
    assert len(manifest.indicators) == 1
    assert calls


def test_extract_retries_novel_indicator_with_unbound_source_excerpt(
    conn: sqlite3.Connection,
) -> None:
    transcript_id, _ = _seed_transcript(
        conn, "ZZZ", "We launched 42 new enterprise pilots.", "2025-12-31"
    )
    prompts: list[str] = []

    def stub_llm(prompt: str) -> str:
        prompts.append(prompt)
        source_excerpt = (
            "We started forty-two enterprise pilots."
            if len(prompts) == 1
            else "We launched 42 new enterprise pilots."
        )
        return f"""{{
          "commitments": [],
          "novel_indicators": [{{
            "raw_label": "New enterprise pilots",
            "value": "42",
            "unit": "count",
            "scope": "product",
            "recurrence": "one_off",
            "source_excerpt": "{source_excerpt}"
          }}]
        }}"""

    manifest = extract_for_transcript(conn, transcript_id, llm_call=stub_llm)

    assert len(prompts) == 2
    assert "failed exact segment binding" in prompts[1]
    assert "Copy each novel indicator source_excerpt exactly" in prompts[1]
    assert manifest.indicators[0].source_excerpt == "We launched 42 new enterprise pilots."


def test_extract_rejects_novel_indicator_after_two_unbound_source_excerpts(
    conn: sqlite3.Connection,
) -> None:
    transcript_id, _ = _seed_transcript(
        conn, "ZZZ", "We launched 42 new enterprise pilots.", "2025-12-31"
    )
    calls = 0

    def stub_llm(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return """{
          "commitments": [],
          "novel_indicators": [{
            "raw_label": "New enterprise pilots",
            "value": "42",
            "unit": "count",
            "scope": "product",
            "recurrence": "one_off",
            "source_excerpt": "We started forty-two enterprise pilots."
          }]
        }"""

    with pytest.raises(CommitmentParseError, match="failed exact segment binding"):
        extract_for_transcript(conn, transcript_id, llm_call=stub_llm)
    assert calls == 2


def test_extract_retries_once_with_feedback_then_succeeds(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    transcript_id, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    prompts: list[str] = []

    def flaky_llm(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return "Sure! Here are the commitments you asked for:"
        return '{"commitments": [], "novel_indicators": []}'

    manifest = extract_for_transcript(conn, transcript_id, llm_call=flaky_llm)
    assert manifest.commitments == []
    assert len(prompts) == 2
    assert prompts[1].startswith("IMPORTANT: your previous response was not the valid JSON")


def test_extract_raises_after_two_unusable_responses(
    conn: sqlite3.Connection,
) -> None:
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    transcript_id, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    with pytest.raises(CommitmentParseError):
        extract_for_transcript(conn, transcript_id, llm_call=lambda p: "still not json")


# ---------------------------------------------------------------------------
# fetch_transcript_text_and_segment
# ---------------------------------------------------------------------------


def test_fetch_picks_longest_segment(conn: sqlite3.Connection) -> None:
    transcript_id, _ = _seed_transcript(conn, "AMZN", "short", "2025-12-31")
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (?, 1, ?)",
        (transcript_id, "longer text by far"),
    )
    conn.commit()
    result = fetch_transcript_text_and_segment(conn, transcript_id)
    assert result is not None
    text, _segment_id, _period_end = result
    assert text == "longer text by far"


def test_second_segment_failure_returns_no_partial_manifest(conn: sqlite3.Connection) -> None:
    transcript_id, _ = _seed_transcript(conn, "AMZN", "first", "2025-12-31")
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (?, 1, ?)",
        (transcript_id, "second"),
    )
    conn.commit()
    calls = 0

    def llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls >= 2:
            return "not json"
        return '{"commitments": [], "novel_indicators": []}'

    with pytest.raises(CommitmentParseError):
        extract_for_transcript(conn, transcript_id, llm_call=llm)
    assert calls == 3  # first segment, second segment, retry


def test_fetch_returns_none_when_no_segments(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256) "
        "VALUES ('AMZN', 'transcript_audio', 'earnings_call_transcript', 'x', 'h')"
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO transcripts (document_id, ticker, period_end) VALUES (?, 'AMZN', ?)",
        (doc_id, "2025-12-31"),
    )
    transcript_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    assert fetch_transcript_text_and_segment(conn, transcript_id) is None


# ---------------------------------------------------------------------------
# execution/extract_commitments_from_transcript.py — LLM governance wiring
# ---------------------------------------------------------------------------


def _load_script() -> Any:
    import importlib.util
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "execution" / "extract_commitments_from_transcript.py"
    )
    spec = importlib.util.spec_from_file_location("extract_commitments_from_transcript", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["extract_commitments_from_transcript"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_run_auto_routes_through_governed_call_llm(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script must pass purpose= and ticker= on every LLM call (the
    _call_claude bypass made this the repo's largest anonymous cost line)."""
    mod = _load_script()
    conn, tid = _active_conn(
        tmp_path, migrated_db, ticker="AMZN", text="text", period_end="2025-12-31"
    )
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")

    seen: list[dict[str, object]] = []

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        seen.append(dict(kwargs))
        return '{"commitments": [], "novel_indicators": []}'

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    assert report["targets"] == 1
    assert report["failed_targets"] == 0
    assert seen and all(k["purpose"] == "saydo_commitment_extract" for k in seen)
    assert all(k["ticker"] == "AMZN" for k in seen)
    # zero-commitment scan must be recorded so tomorrow's run skips it
    row = conn.execute(
        "SELECT n_extracted FROM commitment_scan_log WHERE transcript_id = ?", (tid,)
    ).fetchone()
    assert row is not None and row["n_extracted"] == 0
    conn.close()


def test_run_auto_dry_run_records_no_scan(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    _seed_transcript(conn, "AMZN", "text", "2025-12-31")

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return '{"commitments": [], "novel_indicators": []}'

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=True)
    n = conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0]
    assert n == 0


def test_run_auto_unbound_indicator_after_retry_records_no_scan(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript whose extraction failed must stay pending (retryable),
    and the failure must be visible in the run report."""
    mod = _load_script()
    conn, _ = _active_conn(
        tmp_path,
        migrated_db,
        ticker="ZZZ",
        text="We launched 42 new enterprise pilots.",
    )

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return """{
          "commitments": [],
          "novel_indicators": [{
            "raw_label": "New enterprise pilots",
            "value": "42",
            "unit": "count",
            "scope": "product",
            "recurrence": "one_off",
            "source_excerpt": "We started forty-two enterprise pilots."
          }]
        }"""

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)
    results = report["results"]
    assert len(results) == 1 and "CommitmentParseError" in str(results[0]["error"])
    assert report["failed_targets"] == 1
    n = conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0]
    assert n == 0
    conn.close()


def test_run_auto_scan_failure_rolls_back_extracted_rows(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan receipt and extracted observations are one atomic write set."""
    mod = _load_script()
    conn, _ = _active_conn(tmp_path, migrated_db, ticker="AMZN", text="text")
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return """{
          "commitments": [{
            "kpi_name": "AWS Revenue Growth",
            "comparator": "ge",
            "target_value": "20",
            "unit": "percent",
            "period_target": "2026-03-31",
            "narrative": "We expect AWS to grow at least 20%."
          }],
          "novel_indicators": []
        }"""

    def fail_scan(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("scan receipt unavailable")

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    monkeypatch.setattr(mod, "record_scan", fail_scan)

    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    results = report["results"]
    assert len(results) == 1 and "OperationalError" in str(results[0]["error"])
    assert report["failed_targets"] == 1
    assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0
    conn.close()


def test_run_auto_indicator_persistence_failure_records_no_scan(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staging failure is visible and leaves the transcript pending."""
    mod = _load_script()
    conn, tid = _active_conn(
        tmp_path,
        migrated_db,
        ticker="ZZZ",
        text="We launched 42 new enterprise pilots.",
    )

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return """{
          "commitments": [],
          "novel_indicators": [{
            "raw_label": "New enterprise pilots",
            "value": "42",
            "unit": "count",
            "scope": "product",
            "recurrence": "one_off",
            "source_excerpt": "We launched 42 new enterprise pilots."
          }]
        }"""

    def fail_indicator_persistence(*_args: object, **_kwargs: object) -> Never:
        raise ManagementIndicatorSchemaError("forced staging failure")

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    monkeypatch.setattr(mod, "persist_indicators", fail_indicator_persistence)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    results = report["results"]
    assert len(results) == 1
    assert "ManagementIndicatorSchemaError" in str(results[0]["error"])
    assert report["failed_targets"] == 1
    n = conn.execute(
        "SELECT COUNT(*) FROM commitment_scan_log WHERE transcript_id=?", (tid,)
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_main_auto_returns_nonzero_when_any_target_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mod = _load_script()

    def fake_open_db(_path: str) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    def fake_set_db_path(_path: str) -> None:
        return None

    def fake_run_auto(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "targets": 1,
            "total_inserted": 0,
            "failed_targets": 1,
            "dry_run": False,
            "results": [{"ticker": "BN", "error": "ValueError: source excerpt mismatch"}],
        }

    monkeypatch.setattr(mod, "open_db", fake_open_db)
    monkeypatch.setattr(mod.db, "set_db_path", fake_set_db_path)
    monkeypatch.setattr(mod, "_run_auto", fake_run_auto)

    assert mod.main(["--auto", "--db", str(tmp_path / "portfolio.db")]) == 1
