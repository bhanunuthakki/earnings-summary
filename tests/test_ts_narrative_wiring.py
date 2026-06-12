"""Tests for the time-series narrative wiring across §2, §4, §5, §9.

Asserts that pre-computed signals from the timeseries_signals table land
in each section's downstream surface:

  - §2 thesis  → ThesisSection.ts_context_md (renderable field)
  - §4 segments → SegmentsSection.ts_context_md (renderable field)
  - §5 earnings → ts_signals_md kwarg on extract_qa_vs_prepared_themes
  - §9 bear case → ts_signals_md kwarg on generate_bear_case

For sections that own an LLM call (bear case, earnings) we monkeypatch the
extractor / generator at its bound name in the section module and capture
the kwargs. For sections without an LLM call (thesis, segments) we read
the populated field on the returned Pydantic model. Empty-signals path is
covered by a dedicated test per section so an empty heading never leaks
into the prompt or output.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from report.sections import bear_case as bear_case_module
from report.sections import earnings as earnings_module
from report.sections import segments as segments_module
from report.sections import thesis as thesis_module

# ---------------------------------------------------------------------------
# DB / fixture helpers
# ---------------------------------------------------------------------------


def _create_minimal_schema(db_path: Path) -> None:
    """Build the slim schema each section under test reads from.

    Mirrors the production DDL for the tables touched (financial_facts,
    segment_periods + segment_dimensions, transcripts, earnings_surprises,
    thesis_evaluations, timeseries_signals, kpi_definitions, kpi_facts).
    Kept inline so the test runs without alembic.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE timeseries_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR(16) NOT NULL,
                metric_name VARCHAR(128) NOT NULL,
                metric_kind VARCHAR(16) NOT NULL,
                signal_type VARCHAR(32) NOT NULL,
                value_json TEXT NOT NULL,
                severity VARCHAR(8) NOT NULL,
                narrative TEXT,
                computed_at DATETIME NOT NULL,
                run_id VARCHAR(64),
                CONSTRAINT uq_timeseries_signals_logical
                    UNIQUE (ticker, metric_name, metric_kind, signal_type)
            );
            CREATE TABLE segment_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                period_end TEXT NOT NULL,
                fiscal_period_type TEXT NOT NULL
            );
            CREATE TABLE segment_dimensions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                dim_type TEXT NOT NULL,
                dim_name TEXT NOT NULL,
                metric TEXT NOT NULL,
                value NUMERIC NOT NULL
            );
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER,
                ticker TEXT NOT NULL,
                call_date TEXT,
                fiscal_period_type TEXT NOT NULL,
                period_end TEXT NOT NULL,
                source_url TEXT,
                has_qa_section INTEGER
            );
            CREATE TABLE earnings_surprises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                release_date TEXT NOT NULL,
                eps_surprise_pct NUMERIC,
                revenue_surprise_pct NUMERIC,
                source_name TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE kpi_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'actual',
                primary_source TEXT NOT NULL,
                fallback_source TEXT,
                ir_url TEXT,
                threshold_tier TEXT,
                threshold_low REAL,
                threshold_high REAL,
                notes TEXT,
                UNIQUE(ticker, name)
            );
            CREATE TABLE kpi_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                period_end TEXT NOT NULL,
                fiscal_period_type TEXT NOT NULL,
                kpi_definition_id INTEGER NOT NULL,
                value NUMERIC NOT NULL,
                unit TEXT NOT NULL,
                source_doc_id INTEGER NOT NULL,
                source_excerpt TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_signal(
    db_path: Path,
    *,
    ticker: str,
    metric_name: str,
    metric_kind: str,
    signal_type: str,
    severity: str,
    narrative: str,
    value: dict[str, Any] | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO timeseries_signals (
                ticker, metric_name, metric_kind, signal_type,
                value_json, severity, narrative, computed_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker.upper(),
                metric_name,
                metric_kind,
                signal_type,
                json.dumps(value or {}),
                severity,
                narrative,
                datetime.now().isoformat(sep=" ", timespec="seconds"),
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_holdings(repo: Path, ticker: str, *, tier_1_kpis: list[dict[str, str]]) -> None:
    """Minimal holdings JSON sufficient for thesis.build + earnings._ts_signals_md."""
    d = repo / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker.upper(),
        "thesis": f"Test thesis for {ticker}",
        "break_conditions": ["revenue growth < 10% for 2 consecutive quarters"],
        "tier_1_kpis": tier_1_kpis,
    }
    (d / f"{ticker.upper()}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def repo_with_signals(tmp_path: Path) -> Path:
    """Repo root with a portfolio.db carrying signals for ticker 'TST'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir = repo / "data"
    data_dir.mkdir()
    db = data_dir / "portfolio.db"
    _create_minimal_schema(db)

    _insert_signal(
        db,
        ticker="TST",
        metric_name="revenue",
        metric_kind="financial",
        signal_type="trend",
        severity="yellow",
        narrative="Revenue: decelerating trend (slope -2.3%/q, significant).",
    )
    _insert_signal(
        db,
        ticker="TST",
        metric_name="operating_income",
        metric_kind="financial",
        signal_type="anomaly",
        severity="red",
        narrative="Operating Income: 1 anomalous quarter(s); most recent 2026-03-31 z=+2.80.",
    )
    _insert_signal(
        db,
        ticker="TST",
        metric_name="Subscription ARR",
        metric_kind="kpi",
        signal_type="yoy_acceleration",
        severity="yellow",
        narrative="Subscription ARR: YoY +18.0% (Δ -3.50% QoQ, decelerating).",
    )
    _insert_signal(
        db,
        ticker="TST",
        metric_name="Cloud:revenue",
        metric_kind="segment",
        signal_type="inflection",
        severity="red",
        narrative="Cloud:Revenue: inflection detected at 2025-09-30 (magnitude 0.85σ, 1Q ago).",
    )
    return repo


# ---------------------------------------------------------------------------
# Bear case — TS block reaches generate_bear_case
# ---------------------------------------------------------------------------


def _fake_bear_response() -> str:
    return json.dumps(
        {
            "failure_modes": [],
            "most_underweighted": "",
            "out_of_scope_flags": [],
        }
    )


def test_bear_case_injects_all_signals_into_prompt(
    monkeypatch: pytest.MonkeyPatch, repo_with_signals: Path
) -> None:
    """Every signal — financial + KPI + segment — must land in the bear-case
    prompt under the Disconfirmation Candidates heading."""
    from report.models import (
        EarningsSection,
        FinancialsSection,
        SectionStatus,
        SegmentsSection,
        ThesisSection,
    )

    captured: dict[str, Any] = {}

    def fake_generate_bear_case(**kwargs: Any) -> str:
        captured.update(kwargs)
        return _fake_bear_response()

    monkeypatch.setattr(bear_case_module, "generate_bear_case", fake_generate_bear_case)

    thesis = ThesisSection(
        status=SectionStatus.OK,
        thesis_full="some thesis",
        break_conditions=["x"],
    )
    financials = FinancialsSection(status=SectionStatus.MISSING_DATA)
    segments = SegmentsSection(status=SectionStatus.MISSING_DATA)
    earnings = EarningsSection(status=SectionStatus.MISSING_DATA)

    bear_case_module.build(
        ticker="TST",
        repo_root=repo_with_signals,
        enable_llm=True,
        thesis=thesis,
        financials=financials,
        segments=segments,
        earnings=earnings,
    )

    assert "ts_signals_md" in captured
    block = captured["ts_signals_md"]
    assert "## Time-Series Disconfirmation Candidates" in block
    # All four seeded signals appear in the block
    assert "Revenue: decelerating trend" in block
    assert "Operating Income: 1 anomalous quarter" in block
    assert "Subscription ARR: YoY +18.0%" in block
    assert "Cloud:Revenue: inflection detected" in block


def test_bear_case_empty_signals_produces_no_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ticker with no signals must produce ts_signals_md='' — no dangling heading."""
    from report.models import (
        EarningsSection,
        FinancialsSection,
        SectionStatus,
        SegmentsSection,
        ThesisSection,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    _create_minimal_schema(repo / "data" / "portfolio.db")

    captured: dict[str, Any] = {}

    def fake_generate_bear_case(**kwargs: Any) -> str:
        captured.update(kwargs)
        return _fake_bear_response()

    monkeypatch.setattr(bear_case_module, "generate_bear_case", fake_generate_bear_case)

    bear_case_module.build(
        ticker="EMPTY",
        repo_root=repo,
        enable_llm=True,
        thesis=ThesisSection(status=SectionStatus.OK, thesis_full="t", break_conditions=["x"]),
        financials=FinancialsSection(status=SectionStatus.MISSING_DATA),
        segments=SegmentsSection(status=SectionStatus.MISSING_DATA),
        earnings=EarningsSection(status=SectionStatus.MISSING_DATA),
    )

    assert captured["ts_signals_md"] == ""


# ---------------------------------------------------------------------------
# Earnings — TS block reaches extract_qa_vs_prepared_themes
# ---------------------------------------------------------------------------


def _seed_minimal_transcript(repo: Path, ticker: str) -> None:
    """One CallStreet-shaped transcript so _build_themes actually fires."""
    d = repo / "transcripts" / "processed"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_Q1_2026.txt").write_text(
        "MANAGEMENT DISCUSSION SECTION\n\nCEO: revenue grew 20%.\n\n"
        "QUESTION AND ANSWER SECTION\n\nAnalyst: margin trajectory?\n",
        encoding="utf-8",
    )
    # transcripts row so the has_qa_flag lookup succeeds
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.execute(
        "INSERT INTO transcripts (ticker, fiscal_period_type, period_end, has_qa_section) "
        "VALUES (?, ?, ?, ?)",
        (ticker, "Q1", "2026-03-30", 1),
    )
    conn.commit()
    conn.close()


def test_earnings_themes_injects_ts_block(
    monkeypatch: pytest.MonkeyPatch, repo_with_signals: Path
) -> None:
    """The themes extractor must receive a ts_signals_md kwarg carrying the
    headline P&L + tier-1 KPI signals."""
    ticker = "TST"
    _seed_holdings(
        repo_with_signals,
        ticker,
        tier_1_kpis=[{"name": "Subscription ARR", "break": "growth < 15%"}],
    )
    _seed_minimal_transcript(repo_with_signals, ticker)

    captured: dict[str, Any] = {}

    def fake_extractor(
        ticker_arg: str,
        transcripts_arg: list[dict[str, Any]],
        ts_signals_md: str = "",
    ) -> str:
        captured["ticker"] = ticker_arg
        captured["ts_signals_md"] = ts_signals_md
        return json.dumps({"prepared_themes": [], "qa_themes": []})

    import llm_client

    monkeypatch.setattr(llm_client, "extract_qa_vs_prepared_themes", fake_extractor)

    earnings_module.build(ticker, repo_with_signals, enable_llm=True)

    block = captured.get("ts_signals_md", "")
    assert "## Time-Series Context for Quarterly Interpretation" in block
    # Universal headline metrics
    assert "Revenue: decelerating trend" in block
    assert "Operating Income: 1 anomalous quarter" in block
    # Tier-1 KPI from holdings JSON
    assert "Subscription ARR: YoY +18.0%" in block
    # Segment signals do NOT belong on the earnings block (those are for §4/§9)
    assert "Cloud:Revenue: inflection" not in block


def test_earnings_themes_empty_signals_produces_no_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    _create_minimal_schema(repo / "data" / "portfolio.db")
    _seed_holdings(repo, "EMPTY", tier_1_kpis=[])
    _seed_minimal_transcript(repo, "EMPTY")

    captured: dict[str, Any] = {}

    def fake_extractor(
        ticker_arg: str,
        transcripts_arg: list[dict[str, Any]],
        ts_signals_md: str = "",
    ) -> str:
        captured["ts_signals_md"] = ts_signals_md
        return json.dumps({"prepared_themes": [], "qa_themes": []})

    import llm_client

    monkeypatch.setattr(llm_client, "extract_qa_vs_prepared_themes", fake_extractor)

    earnings_module.build("EMPTY", repo, enable_llm=True)
    assert captured.get("ts_signals_md") == ""


def test_earnings_themes_cache_invalidates_on_ts_signal_change(
    monkeypatch: pytest.MonkeyPatch, repo_with_signals: Path
) -> None:
    """A change in the timeseries_signals table must invalidate the cached
    themes payload — otherwise stale themes ride alongside a new TS block."""
    ticker = "TST"
    _seed_holdings(repo_with_signals, ticker, tier_1_kpis=[])
    _seed_minimal_transcript(repo_with_signals, ticker)

    call_count = {"n": 0}

    def fake_extractor(*args: Any, **kwargs: Any) -> str:
        call_count["n"] += 1
        return json.dumps({"prepared_themes": [], "qa_themes": []})

    import llm_client

    monkeypatch.setattr(llm_client, "extract_qa_vs_prepared_themes", fake_extractor)

    earnings_module.build(ticker, repo_with_signals, enable_llm=True)
    assert call_count["n"] == 1

    # Same inputs → cache hit, no second LLM call
    earnings_module.build(ticker, repo_with_signals, enable_llm=True)
    assert call_count["n"] == 1

    # Refresh a signal → cache key flips → LLM re-fires
    _insert_signal(
        repo_with_signals / "data" / "portfolio.db",
        ticker="TST",
        metric_name="revenue",
        metric_kind="financial",
        signal_type="inflection",
        severity="red",
        narrative="Revenue: inflection detected at 2026-03-31 (magnitude 1.20σ, 0Q ago).",
    )
    earnings_module.build(ticker, repo_with_signals, enable_llm=True)
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Thesis — ts_context_md populated on the section payload
# ---------------------------------------------------------------------------


def test_thesis_section_populates_ts_context(repo_with_signals: Path) -> None:
    _seed_holdings(
        repo_with_signals,
        "TST",
        tier_1_kpis=[{"name": "Subscription ARR", "break": "growth < 15%"}],
    )
    section = thesis_module.build("TST", repo_with_signals)

    assert "## Time-Series Context (last computed)" in section.ts_context_md
    assert "Revenue: decelerating trend" in section.ts_context_md
    assert "Operating Income: 1 anomalous quarter" in section.ts_context_md
    # Tier-1 KPI signal also surfaced
    assert "Subscription ARR: YoY +18.0%" in section.ts_context_md
    # Segment signals belong on §4, not §2
    assert "Cloud:Revenue" not in section.ts_context_md


def test_thesis_section_empty_signals_yields_empty_string(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    _create_minimal_schema(repo / "data" / "portfolio.db")
    _seed_holdings(repo, "EMPTY", tier_1_kpis=[])

    section = thesis_module.build("EMPTY", repo)
    assert section.ts_context_md == ""


# ---------------------------------------------------------------------------
# Segments — ts_context_md carries only metric_kind='segment' rows
# ---------------------------------------------------------------------------


def test_segments_section_populates_ts_context_with_segment_signals_only(
    repo_with_signals: Path,
) -> None:
    # Seed one segment_periods row so the segments builder doesn't bail with
    # MISSING_DATA — the TS field is populated even when grids are empty
    # because the load_segment_signals path is independent of segment_periods.
    conn = sqlite3.connect(str(repo_with_signals / "data" / "portfolio.db"))
    conn.execute(
        "INSERT INTO segment_periods (ticker, period_end, fiscal_period_type) VALUES (?, ?, ?)",
        ("TST", "2026-03-31", "Q1"),
    )
    period_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, metric, value) "
        "VALUES (?, ?, ?, ?, ?)",
        (period_id, "product", "Cloud", "revenue", 1000000.0),
    )
    conn.commit()
    conn.close()

    section = segments_module.build("TST", repo_with_signals)

    assert "## Segment Time-Series Context" in section.ts_context_md
    assert "Cloud:Revenue: inflection detected" in section.ts_context_md
    # Financial / KPI signals do NOT belong on the segments block
    assert "Revenue: decelerating trend" not in section.ts_context_md
    assert "Subscription ARR" not in section.ts_context_md


def test_segments_section_empty_signals_yields_empty_string(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "data").mkdir()
    _create_minimal_schema(repo / "data" / "portfolio.db")
    # Need at least one segment row so the builder doesn't return MISSING_DATA
    # and we can read the (empty) ts_context_md field.
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.execute(
        "INSERT INTO segment_periods (ticker, period_end, fiscal_period_type) VALUES (?, ?, ?)",
        ("EMPTY", "2026-03-31", "Q1"),
    )
    period_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO segment_dimensions (period_id, dim_type, dim_name, metric, value) "
        "VALUES (?, ?, ?, ?, ?)",
        (period_id, "product", "Cloud", "revenue", 1000000.0),
    )
    conn.commit()
    conn.close()

    section = segments_module.build("EMPTY", repo)
    assert section.ts_context_md == ""


# ---------------------------------------------------------------------------
# Formatter behavior — defensive direct checks
# ---------------------------------------------------------------------------


def test_format_signals_block_empty_list_returns_empty_string() -> None:
    from report.sections._ts_signals import format_signals_as_prompt_block

    assert format_signals_as_prompt_block([], heading="X") == ""


def test_format_signals_block_skips_rows_without_narrative() -> None:
    from report.sections._ts_signals import SignalRow, format_signals_as_prompt_block

    rows = [
        SignalRow(
            metric_name="x",
            metric_kind="financial",
            signal_type="correlation",
            value_json="{}",
            severity="green",
            narrative=None,
        ),
    ]
    # Only narrative-less rows → no body lines → empty string (no heading).
    assert format_signals_as_prompt_block(rows, heading="X") == ""


def test_format_signals_block_severity_chips() -> None:
    from report.sections._ts_signals import SignalRow, format_signals_as_prompt_block

    rows = [
        SignalRow(
            metric_name="a",
            metric_kind="financial",
            signal_type="trend",
            value_json="{}",
            severity="red",
            narrative="A: bad.",
        ),
        SignalRow(
            metric_name="b",
            metric_kind="financial",
            signal_type="trend",
            value_json="{}",
            severity="yellow",
            narrative="B: watch.",
        ),
        SignalRow(
            metric_name="c",
            metric_kind="financial",
            signal_type="trend",
            value_json="{}",
            severity="green",
            narrative="C: fine.",
        ),
    ]
    out = format_signals_as_prompt_block(rows, heading="Header")
    assert "## Header" in out
    assert "- A: bad. [RED]" in out
    assert "- B: watch. [yellow]" in out
    # Green carries no chip
    assert "- C: fine." in out
    assert "C: fine. [" not in out
