"""Tests for the §6 surprise-scorecard header — builder + markdown renderer.

Covers the new `surprise_scorecard` field on `EarningsSection` and its
rendering. The post-FMP-lapse case (revenue side all-NULL) gets explicit
coverage to verify the "source coverage absent" fallback row instead of a
misleading 0% beat rate.
"""

from __future__ import annotations

import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from report.models import EarningsSection, SectionStatus, SurpriseScorecardCard
from report.renderers.markdown import _surprise_scorecard_block as _md_block
from report.sections.earnings import _build_surprise_card


def _seed_db(tmp_path: Path, rows: list[tuple[str, str, str | None, str | None]]) -> Path:
    """Create a minimal portfolio.db with the earnings_surprises table seeded.

    Returns the repo_root path the builder consumes (data/portfolio.db lives
    underneath it).
    """
    repo_root = tmp_path / "repo"
    db_dir = repo_root / "data"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE earnings_surprises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            release_date TEXT NOT NULL,
            eps_estimate NUMERIC,
            eps_actual NUMERIC,
            revenue_estimate NUMERIC,
            revenue_actual NUMERIC,
            eps_surprise_pct NUMERIC,
            revenue_surprise_pct NUMERIC,
            num_analysts_eps INTEGER,
            num_analysts_revenue INTEGER,
            source_name TEXT NOT NULL,
            source_url TEXT,
            fetched_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    for ticker, release_date, eps_pct, rev_pct in rows:
        conn.execute(
            "INSERT INTO earnings_surprises "
            "(ticker, release_date, eps_surprise_pct, revenue_surprise_pct, "
            " source_name, fetched_at) VALUES (?, ?, ?, ?, 'fmp_calendar', '2026-05-13T12:00:00')",
            (ticker, release_date, eps_pct, rev_pct),
        )
    conn.commit()
    conn.close()
    return repo_root


# --- _build_surprise_card (builder) -----------------------------------------


def test_builder_returns_none_when_db_missing(tmp_path: Path) -> None:
    """No DB file -> None (renderers will skip the header). Avoids crashing
    on a fresh checkout that hasn't yet run any migrations or backfills."""
    card = _build_surprise_card("WIX", tmp_path)
    assert card is None


def test_builder_returns_none_when_no_rows_for_ticker(tmp_path: Path) -> None:
    """DB present but no records for this ticker -> None."""
    repo = _seed_db(tmp_path, [("OTHER", "2025-08-06", "10.0", "5.0")])
    assert _build_surprise_card("WIX", repo) is None


def test_builder_populates_full_surprise_card(tmp_path: Path) -> None:
    """4 quarters with both sides -> populated card; Decimal -> float at boundary."""
    repo = _seed_db(
        tmp_path,
        [
            ("WIX", "2024-08-06", "10.0", "2.0"),
            ("WIX", "2024-11-19", "15.0", "-1.0"),
            ("WIX", "2025-02-19", "-5.0", "3.0"),
            ("WIX", "2025-05-21", "8.0", "1.0"),
        ],
    )
    card = _build_surprise_card("WIX", repo)
    assert card is not None
    assert card.total_quarters == 4
    assert card.eps_beats == 3
    assert card.eps_misses == 1
    assert card.eps_beat_rate_pct == pytest.approx(75.0)
    # avg = (10 + 15 + -5 + 8) / 4 = 7.0
    assert card.eps_avg_surprise_pct == pytest.approx(7.0)
    # Latest = ORDER BY release_date DESC -> 2025-05-21 row
    assert card.eps_latest_surprise_pct == pytest.approx(8.0)
    assert card.revenue_no_data == 0
    assert card.revenue_beat_rate_pct == pytest.approx(75.0)


def test_builder_handles_post_fmp_lapse_revenue(tmp_path: Path) -> None:
    """Revenue all-NULL (yfinance-only world) -> revenue side reports no_data."""
    repo = _seed_db(
        tmp_path,
        [
            ("WIX", "2024-08-06", "10.0", None),
            ("WIX", "2024-11-19", "15.0", None),
        ],
    )
    card = _build_surprise_card("WIX", repo)
    assert card is not None
    assert card.eps_beat_rate_pct == pytest.approx(100.0)
    assert card.revenue_no_data == 2
    assert card.revenue_beat_rate_pct is None


# --- Markdown renderer ------------------------------------------------------


def test_md_renderer_skips_when_none() -> None:
    out = StringIO()
    _md_block(out, None)
    assert out.getvalue() == ""


def test_md_renderer_skips_when_zero_quarters() -> None:
    """Empty scorecard sentinel -> no output. Defensive: in practice the
    builder returns None for this case, but the renderer should be robust."""
    out = StringIO()
    empty = SurpriseScorecardCard(
        total_quarters=0,
        eps_beats=0,
        eps_misses=0,
        eps_no_data=0,
        revenue_beats=0,
        revenue_misses=0,
        revenue_no_data=0,
    )
    _md_block(out, empty)
    assert out.getvalue() == ""


def test_md_renderer_full_card() -> None:
    out = StringIO()
    card = SurpriseScorecardCard(
        total_quarters=8,
        eps_beats=7,
        eps_misses=1,
        eps_no_data=0,
        eps_beat_rate_pct=87.5,
        eps_avg_surprise_pct=14.07,
        eps_latest_surprise_pct=9.1,
        revenue_beats=8,
        revenue_misses=0,
        revenue_no_data=0,
        revenue_beat_rate_pct=100.0,
        revenue_avg_surprise_pct=2.74,
        revenue_latest_surprise_pct=5.6,
    )
    _md_block(out, card)
    s = out.getvalue()
    assert "Analyst surprise — last 8 reported quarters" in s
    # Header row + EPS + Revenue
    assert "| EPS | 7 | 1 | +87.5% | +14.07% | +9.10% |" in s
    assert "| Revenue | 8 | 0 | +100.0% | +2.74% | +5.60% |" in s


def test_md_renderer_negative_values_signed() -> None:
    """Negative beat-rate average gets no '+' prefix; positive does."""
    out = StringIO()
    card = SurpriseScorecardCard(
        total_quarters=8,
        eps_beats=3,
        eps_misses=5,
        eps_no_data=0,
        eps_beat_rate_pct=37.5,
        eps_avg_surprise_pct=-3.04,
        eps_latest_surprise_pct=-7.05,
        revenue_beats=4,
        revenue_misses=4,
        revenue_no_data=0,
        revenue_beat_rate_pct=50.0,
        revenue_avg_surprise_pct=0.11,
        revenue_latest_surprise_pct=-0.5,
    )
    _md_block(out, card)
    s = out.getvalue()
    # Negative values rendered without leading '+'
    assert "-3.04%" in s
    assert "-7.05%" in s
    # Positive small values still get '+'
    assert "+0.11%" in s


def test_md_renderer_revenue_no_data_row() -> None:
    """When revenue source coverage is absent, render '—' with explanatory note,
    not 0% beat rate."""
    out = StringIO()
    card = SurpriseScorecardCard(
        total_quarters=4,
        eps_beats=3,
        eps_misses=1,
        eps_no_data=0,
        eps_beat_rate_pct=75.0,
        eps_avg_surprise_pct=7.0,
        eps_latest_surprise_pct=8.0,
        revenue_beats=0,
        revenue_misses=0,
        revenue_no_data=4,
        revenue_beat_rate_pct=None,
        revenue_avg_surprise_pct=None,
        revenue_latest_surprise_pct=None,
    )
    _md_block(out, card)
    s = out.getvalue()
    assert "| Revenue | — | — | — | — | — _(source coverage absent)_ |" in s
    # Must NOT render 0% anywhere in the revenue row
    assert "Revenue | 0 | 0 | 0.0%" not in s


# --- EarningsSection model — surprise_scorecard field defaults --------------


def test_earnings_section_default_surprise_is_none() -> None:
    """Backward compatibility: the new field has a None default, so existing
    callers that don't set it (every pre-Phase-E test) still work."""
    s = EarningsSection(status=SectionStatus.OK)
    assert s.surprise_scorecard is None


# --- Renderer ordering: scorecard precedes missing-data callout -------------


def test_md_renders_scorecard_even_when_section_is_missing_data() -> None:
    """The scorecard is sourced from earnings_surprises — a separate pipeline
    from the LLM summaries that drive MISSING_DATA. A ticker that has surprise
    data but no per-quarter summaries should still show the beat-rate header
    above the 'pending stage' callout."""
    from report.models import MissingReason
    from report.renderers.markdown import _earnings as md_earnings

    out = StringIO()
    section = EarningsSection(
        status=SectionStatus.MISSING_DATA,
        missing=MissingReason(stage="SYNTHESIZE", fix_command="...", detail="..."),
        surprise_scorecard=SurpriseScorecardCard(
            total_quarters=4,
            eps_beats=3,
            eps_misses=1,
            eps_no_data=0,
            eps_beat_rate_pct=75.0,
            revenue_beats=4,
            revenue_misses=0,
            revenue_no_data=0,
            revenue_beat_rate_pct=100.0,
        ),
    )
    md_earnings(out, section)
    s = out.getvalue()
    # Both blocks appear, in the right order
    scorecard_pos = s.find("Analyst surprise — last 4")
    missing_pos = s.find("Pending stage")
    assert scorecard_pos > -1, "scorecard should render even under MISSING_DATA"
    assert missing_pos > -1, "missing-data callout should still render"
    assert scorecard_pos < missing_pos, "scorecard must come before the missing callout"
