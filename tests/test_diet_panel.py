"""Tests for src/pipeline/diet_panel.py — the information-diet PULL surface.

Renders through the S1 control kit; verifies the two lenses (ingest stream +
forward agenda), the consensus_rating pill, the disclosed scaffold note,
empty-state disclosure on a pre-0094 DB, and HTML-escaping of untrusted fields.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from pipeline.diet_panel import render_diet_panel
from signals.store import record_investor_day, record_media_appearance

from ._signals_fixtures import make_news_then_signals, signals_only

_NEWS = [
    (
        "META",
        "MS upgrades META to Buy",
        "http://x/2",
        "2026-06-11 09:00:00",
        None,
        "Morgan Stanley",
        "yf_grades",
        "t",
    ),
    (
        "NU",
        "Nu launches product",
        "http://x/3",
        "2026-06-12 09:00:00",
        "snip",
        "Bloomberg",
        "fmp_stock_news",
        "t",
    ),
]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    d = tmp_path / "diet.db"
    make_news_then_signals(d, _NEWS)
    conn = sqlite3.connect(str(d))
    try:
        record_investor_day(conn, "META", date(2099, 9, 18), "Analyst Day 2099", firm="Meta IR")
    finally:
        conn.close()
    return d


def test_renders_both_lenses_through_the_kit(db: Path) -> None:
    html = render_diet_panel(db)
    # ingest stream: the rating + the news, with the consensus_rating pill.
    assert "Ingest stream" in html
    assert "MS upgrades META to Buy" in html
    assert "Nu launches product" in html
    # Category pills stay QUIET (neutral .k-pill, no accent): accent is reserved
    # for interactive/selected/unread, not a decorative category tint (§2).
    assert 'k-pill">Rating' in html
    # forward agenda: the investor day as a dated row.
    assert "Forward agenda" in html
    assert "Analyst Day 2099" in html
    assert "2099-09-18" in html
    # kit classes only — no bespoke table/pill systems.
    for cls in ("p-table", "k-pill", "k-tick", "panel"):
        assert cls in html


def test_media_appearance_renders_in_the_stream(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        record_media_appearance(
            conn,
            "NU",
            "David Vélez on Invest Like the Best",
            url="http://pod/ep1",
            firm="Invest Like the Best",
        )
    finally:
        conn.close()
    html = render_diet_panel(db)
    assert "David Vélez on Invest Like the Best" in html
    assert 'k-pill">Podcast' in html  # neutral category pill (§2 accent discipline)
    assert "Invest Like the Best" in html  # the show name in the source column


def test_signal_links_to_its_source(db: Path) -> None:
    html = render_diet_panel(db)
    assert 'href="http://x/2"' in html  # the rating story is a doorway to its url


def test_disclosed_scaffolds_are_named_not_promised(db: Path) -> None:
    html = render_diet_panel(db)
    assert "fast-follows" in html
    assert "buy-side ratings" in html.lower()
    assert "estimate" in html.lower()


def test_empty_state_discloses_on_pre_migration_db(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    signals_only(empty)  # signals table exists but no rows
    html = render_diet_panel(empty)
    assert "No diet signals yet" in html
    assert "No investor days on the calendar" in html


def test_no_raw_hex_in_output(db: Path) -> None:
    html = render_diet_panel(db)
    # the S1 guard scans the source; this pins the RENDERED output too.
    assert not re.findall(r"(?<![\w&])#[0-9a-fA-F]{3,8}\b", html)


def test_book_names_float_to_top_and_are_marked(tmp_path: Path) -> None:
    """Owner feedback 2026-07-14: the diet must prioritise portfolio + evaluation
    names. A held name with an OLDER story still leads a watchlist name with a
    NEWER one, and carries the 'core' marker."""
    d = tmp_path / "prio.db"
    make_news_then_signals(
        d,
        [
            # book name, older
            (
                "NU",
                "Nu held story",
                "http://x/nu",
                "2026-06-10 09:00:00",
                None,
                "Reuters",
                "fmp_stock_news",
                "t",
            ),
            # non-book name, newer (would lead in pure recency)
            (
                "ZZZ",
                "Zzz watch story",
                "http://x/zz",
                "2026-06-14 09:00:00",
                None,
                "Reuters",
                "fmp_stock_news",
                "t",
            ),
        ],
    )
    conn = sqlite3.connect(str(d))
    try:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)",
            [("NU", "portfolio"), ("ZZZ", "watchlist")],
        )
        conn.commit()
    finally:
        conn.close()
    html = render_diet_panel(d)
    # Held name leads despite its older timestamp; watchlist name follows.
    assert html.index("Nu held story") < html.index("Zzz watch story")
    # The book row is marked; the watchlist row is not.
    assert "core" in html


def test_low_quality_sources_are_filtered_from_the_stream(tmp_path: Path) -> None:
    """Owner feedback 2026-07-14: 'shit sources'. Content-farm publishers are
    dropped from the reading lane; reputable ones stay."""
    d = tmp_path / "sources.db"
    make_news_then_signals(
        d,
        [
            (
                "NU",
                "Nu real report",
                "http://x/1",
                "2026-06-12 09:00:00",
                None,
                "Reuters",
                "fmp_stock_news",
                "t",
            ),
            (
                "NU",
                "Nu farm recap",
                "http://x/2",
                "2026-06-13 09:00:00",
                None,
                "Zacks Investment Research",
                "fmp_stock_news",
                "t",
            ),
        ],
    )
    html = render_diet_panel(d)
    assert "Nu real report" in html
    assert "Nu farm recap" not in html


def test_escapes_untrusted_headline(tmp_path: Path) -> None:
    d = tmp_path / "xss.db"
    make_news_then_signals(
        d,
        [
            (
                "NU",
                "<script>alert(1)</script>",
                "http://x/1",
                "2026-06-12 09:00:00",
                None,
                "X",
                "fmp_stock_news",
                "t",
            )
        ],
    )
    html = render_diet_panel(d)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
