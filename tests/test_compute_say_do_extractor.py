"""Tests for src/compute/say_do_extractor.py — automated LLM extraction."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

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
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, ?)",
        (ticker, name, unit),
    )
    conn.commit()


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
    record_scan(conn, tid, n_extracted=0, prompt_version="v1")
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
    """Pre-0129 DB (no commitment_scan_log): selection still works, scans
    are silently unfiltered, record_scan is a warning no-op."""
    conn.execute("DROP TABLE commitment_scan_log")
    conn.commit()
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    assert [p[0] for p in transcripts_pending_extraction(conn)] == [tid]
    record_scan(conn, tid, n_extracted=0)  # must not raise
    assert [p[0] for p in transcripts_pending_extraction(conn)] == [tid]


def test_record_scan_upserts_on_repeat(conn: sqlite3.Connection) -> None:
    _seed_kpi_def(conn, "AMZN", "X")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")
    record_scan(conn, tid, n_extracted=0, prompt_version="v1")
    record_scan(conn, tid, n_extracted=2, prompt_version="v2")
    row = conn.execute(
        "SELECT n_extracted, prompt_version FROM commitment_scan_log WHERE transcript_id = ?",
        (tid,),
    ).fetchone()
    assert (row["n_extracted"], row["prompt_version"]) == (2, "v2")
    n = conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0]
    assert n == 1


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
      ]
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
    response = '```json\n{"commitments": []}\n```'
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
      ]
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
    manifest = parse_llm_response('{"commitments": []}', context=_CTX)
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
          }]
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
        extract_for_transcript(conn, 9999, llm_call=lambda p: '{"commitments": []}')


def test_extract_scans_empty_catalog_for_novel_indicators(conn: sqlite3.Connection) -> None:
    """No catalog still merits a scan for a staged, unpromoted measurement."""
    transcript_id, _ = _seed_transcript(conn, "ZZZ", "some transcript text", "2025-12-31")
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
        return '{"commitments": []}'

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
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script must pass purpose= and ticker= on every LLM call (the
    _call_claude bypass made this the repo's largest anonymous cost line)."""
    mod = _load_script()
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    tid, _ = _seed_transcript(conn, "AMZN", "text", "2025-12-31")

    seen: list[dict[str, object]] = []

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        seen.append(dict(kwargs))
        return '{"commitments": []}'

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    assert report["targets"] == 1
    assert seen and all(k["purpose"] == "saydo_commitment_extract" for k in seen)
    assert all(k["ticker"] == "AMZN" for k in seen)
    # zero-commitment scan must be recorded so tomorrow's run skips it
    row = conn.execute(
        "SELECT n_extracted FROM commitment_scan_log WHERE transcript_id = ?", (tid,)
    ).fetchone()
    assert row is not None and row["n_extracted"] == 0


def test_run_auto_dry_run_records_no_scan(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_script()
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    _seed_transcript(conn, "AMZN", "text", "2025-12-31")

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return '{"commitments": []}'

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=True)
    n = conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0]
    assert n == 0


def test_run_auto_parse_failure_records_no_scan(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript whose extraction failed must stay pending (retryable),
    and the failure must be visible in the run report."""
    mod = _load_script()
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    _seed_transcript(conn, "AMZN", "text", "2025-12-31")

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return "never json"

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)
    results = report["results"]
    assert len(results) == 1 and "CommitmentParseError" in str(results[0]["error"])
    n = conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0]
    assert n == 0


def test_run_auto_scan_failure_rolls_back_extracted_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan receipt and extracted observations are one atomic write set."""
    mod = _load_script()
    _seed_kpi_def(conn, "AMZN", "AWS Revenue Growth", "percent")
    _seed_transcript(conn, "AMZN", "text", "2025-12-31")

    def stub_call_llm(prompt: str, **kwargs: object) -> str:
        return """{
          "commitments": [{
            "kpi_name": "AWS Revenue Growth",
            "comparator": "ge",
            "target_value": "20",
            "unit": "percent",
            "period_target": "2026-03-31",
            "narrative": "We expect AWS to grow at least 20%."
          }]
        }"""

    def fail_scan(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("scan receipt unavailable")

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    monkeypatch.setattr(mod, "record_scan", fail_scan)

    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    results = report["results"]
    assert len(results) == 1 and "OperationalError" in str(results[0]["error"])
    assert conn.execute("SELECT COUNT(*) FROM management_commitments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM commitment_scan_log").fetchone()[0] == 0


def test_run_auto_indicator_persistence_failure_records_no_scan(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing staging migration is visible and leaves the transcript pending."""
    mod = _load_script()
    tid, _ = _seed_transcript(conn, "ZZZ", "text", "2025-12-31")

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

    monkeypatch.setattr(mod, "call_llm", stub_call_llm)
    report = mod._run_auto(conn, ticker=None, transcript_id=None, max_n=0, dry_run=False)

    results = report["results"]
    assert len(results) == 1
    assert "ManagementIndicatorSchemaError" in str(results[0]["error"])
    n = conn.execute(
        "SELECT COUNT(*) FROM commitment_scan_log WHERE transcript_id=?", (tid,)
    ).fetchone()[0]
    assert n == 0
