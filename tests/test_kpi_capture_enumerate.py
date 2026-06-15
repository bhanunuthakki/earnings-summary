# These tests exercise internal capture-enumerate seams.
# pyright: reportPrivateUsage=false
"""Tests for the Stage-B capture-every-number enumerate mode in
compute.kpi_extract_summaries — the no-allowlist path that recovers the numbers
the tier_1 allowlist drops.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.kpi_extract_summaries as kes  # noqa: E402
from compute.kpi_extract_summaries import (  # noqa: E402
    _build_capture_manifest,
    _llm_extract_enumerate,
    capture_for_ticker,
)
from models.facts import FiscalPeriodType, Unit  # noqa: E402
from models.kpis import DefinitionOrigin  # noqa: E402


def _const_caller(response: str) -> Callable[[str, str | None], str]:
    """A typed stand-in for `_call_claude` that always returns `response`."""

    def _call(prompt: str, model: str | None = None) -> str:
        return response

    return _call


# ---------------------------------------------------------------------------
# _llm_extract_enumerate — the no-allowlist prompt + parser
# ---------------------------------------------------------------------------

_ENUMERATE_JSON = (
    "[\n"
    '  {"label": "Revenue", "value": 1200000000, "unit": "actual", "source_excerpt": "Revenue was $1.2 billion"},\n'
    '  {"label": "Net interest margin", "value": 17.8, "unit": "percent", "source_excerpt": "NIM of 17.8%"},\n'
    '  {"label": "Customers", "value": 118000000, "unit": "count", "source_excerpt": "118 million customers"}\n'
    "]"
)


def test_enumerate_prompt_has_no_allowlist_and_valid_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_call(prompt: str, model: str | None = None) -> str:
        captured["prompt"] = prompt
        return _ENUMERATE_JSON

    monkeypatch.setattr(kes, "_call_claude", fake_call)
    rows = _llm_extract_enumerate("NU", "Q1 2025 [ir]", "Revenue was $1.2 billion. NIM 17.8%.")

    prompt = captured["prompt"]
    # It explicitly tells the model there is NO target list.
    assert "no list of metrics" in prompt.lower() or "no allowlist" in prompt.lower()
    # Every offered unit token is a real Unit enum value.
    for token in ('"percent"', '"actual"', '"count"', '"ratio"', '"bps"'):
        assert token in prompt
        assert token.strip('"') in {u.value for u in Unit}
    assert len(rows) == 3
    assert rows[0]["label"] == "Revenue"


def test_enumerate_strips_fence_and_caps_max_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = "```json\n" + _ENUMERATE_JSON + "\n```"
    monkeypatch.setattr(kes, "_call_claude", _const_caller(fenced))
    rows = _llm_extract_enumerate("NU", "Q1 2025", "text", max_facts=2)
    assert len(rows) == 2  # capped


def test_enumerate_returns_empty_on_non_array_or_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kes, "_call_claude", _const_caller('{"not": "an array"}'))
    assert _llm_extract_enumerate("NU", "Q1 2025", "text") == []
    # Blank input short-circuits without even calling the model.
    calls: list[int] = []

    def _counting(prompt: str, model: str | None = None) -> str:
        calls.append(1)
        return "[]"

    monkeypatch.setattr(kes, "_call_claude", _counting)
    assert _llm_extract_enumerate("NU", "Q1 2025", "   ") == []
    assert calls == []


def test_enumerate_skips_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = (
        '[{"label": "Revenue", "value": 5, "unit": "actual"}, {"value": 9}, "junk", {"label": "X"}]'
    )
    monkeypatch.setattr(kes, "_call_claude", _const_caller(bad))
    rows = _llm_extract_enumerate("NU", "Q1 2025", "text")
    # Only the item with both label AND value survives.
    assert rows == [{"label": "Revenue", "value": 5, "unit": "actual"}]


# ---------------------------------------------------------------------------
# _build_capture_manifest
# ---------------------------------------------------------------------------


def test_build_capture_manifest_is_capture_origin_and_parses_rows() -> None:
    rows: list[dict[str, object]] = [
        {"label": "Revenue", "value": 1200000000, "unit": "actual", "source_excerpt": "x"},
        {"label": "Bad value", "value": "N/A", "unit": "actual"},  # unparseable -> skipped
        {"label": "", "value": 5, "unit": "actual"},  # blank label -> skipped
        {"label": "Take rate", "value": 1.5, "unit": "bogus"},  # bad unit -> ACTUAL fallback
    ]
    m = _build_capture_manifest("NU", datetime(2025, 3, 31), FiscalPeriodType.Q1, 42, rows)
    assert m.origin is DefinitionOrigin.CAPTURE
    assert m.source_doc_id == 42
    names = {v.name: v for v in m.values}
    assert set(names) == {"Revenue", "Take rate"}
    assert names["Revenue"].value == Decimal("1200000000")
    assert names["Take rate"].unit is Unit.ACTUAL  # bad token fell back


# ---------------------------------------------------------------------------
# capture_for_ticker — end-to-end against a file DB + a .tmp brief
# ---------------------------------------------------------------------------

_PROVENANCE_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_provenance "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)"
)


def _make_capture_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
            threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
            definition_origin TEXT NOT NULL DEFAULT 'analyst',
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0, extracted_by TEXT,
            locator TEXT, source_excerpt TEXT, supersedes_id INTEGER
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        CREATE TABLE ingestion_runs (
            run_id TEXT PRIMARY KEY, started_at TIMESTAMP, ended_at TIMESTAMP,
            directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, source_type TEXT, doc_type TEXT,
            period_end TIMESTAMP, file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP,
            fetch_status TEXT, raw_bytes_size INTEGER
        );
        """
    )
    conn.execute(_PROVENANCE_UNIQUE)
    conn.commit()
    conn.close()


def _write_brief(repo_root: Path) -> None:
    tmp = repo_root / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "NU_Q1_2025_press_release_summary.txt").write_text(
        "Revenue was $1.2 billion. NIM of 17.8%. We ended with 118 million customers.",
        encoding="utf-8",
    )


def test_capture_for_ticker_persists_capture_facts_and_canonicalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _make_capture_db(db)
    _write_brief(tmp_path)

    # Seed an analyst "Revenue" definition — the enumerated "Revenue" must
    # canonicalize onto it (persist origin=CAPTURE), not mint a duplicate.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('NU', 'Revenue', 'actual', 'llm_extracted', 'analyst')"
    )
    conn.commit()

    calls: list[int] = []

    def fake_call(prompt: str, model: str | None = None) -> str:
        calls.append(1)
        return _ENUMERATE_JSON

    monkeypatch.setattr(kes, "_call_claude", fake_call)

    log = capture_for_ticker("NU", tmp_path, conn, source_group="ir")
    assert log.kpis_inserted_total == 3
    assert len(calls) == 1

    defs = {
        str(r["name"]): str(r["definition_origin"])
        for r in conn.execute("SELECT name, definition_origin FROM kpi_definitions").fetchall()
    }
    # "Revenue" stayed analyst (captured value collapsed onto it); the two new
    # metrics minted as capture.
    assert defs == {
        "Revenue": "analyst",
        "Net interest margin": "capture",
        "Customers": "capture",
    }
    # Facts carry provenance back to the registered brief doc.
    fact_count = conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0]
    assert fact_count == 3

    # Idempotent: a second run skips the doc without another LLM call.
    log2 = capture_for_ticker("NU", tmp_path, conn, source_group="ir")
    assert log2.kpis_inserted_total == 0
    assert len(calls) == 1  # no new model call
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 3
    conn.close()
