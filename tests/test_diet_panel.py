"""Tests for src/pipeline/diet_panel.py — the information-diet PULL surface.

Renders through the S1 control kit; verifies the lenses (earnings readouts +
ingest stream + forward agenda), the parsed sell-side table, the disclosed
scaffold note, empty-state disclosure on a pre-0094 DB, HTML-escaping of
untrusted fields, and — owner ruling 2026-07-30 — that general-news headlines
NEVER render (the news reading list was removed entirely; only EDGAR-fed
``general_news`` rows survive, as filings).
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
    # ingest stream, regrouped (Wave 3, D3): the rating lands in the parsed
    # Sell-side actions table (full title preserved on hover); the general-news
    # headline is GONE — the reading list was removed entirely (2026-07-30).
    assert "Ingest stream" in html
    assert "Sell-side actions" in html
    assert "upgrades <strong>Buy</strong>" in html
    assert 'title="MS upgrades META to Buy"' in html
    assert "Nu launches product" not in html
    # forward agenda: the investor day as a dated row.
    assert "Forward agenda" in html
    assert "Analyst Day 2099" in html
    assert "2099-09-18" in html
    # kit classes only — no bespoke table/pill systems.
    for cls in ("p-table", "k-tick", "panel"):
        assert cls in html


def test_headline_news_never_renders(db: Path) -> None:
    """Owner ruling 2026-07-30: 'remove news section entirely'. No news group
    header, no headline row, regardless of source quality."""
    html = render_diet_panel(db)
    assert "News &amp; podcasts" not in html
    assert "Nu launches product" not in html


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
    assert "Podcasts" in html  # its own group header now (news list removed)
    assert "David Vélez on Invest Like the Best" in html
    assert 'k-pill">Podcast' in html  # neutral category pill (§2 accent discipline)
    assert "Invest Like the Best" in html  # the show name in the source column


def test_signal_links_to_its_source(db: Path) -> None:
    html = render_diet_panel(db)
    # The parsed rating row is still a doorway to its story (D3 groups
    # summarize; they never orphan the underlying source).
    assert 'href="http://x/2"' in html


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
    # EDGAR-fed rows (the filings block) — headline news no longer renders.
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
                "SEC",
                "edgar_8k",
                "t",
            ),
            # non-book name, newer (would lead in pure recency)
            (
                "ZZZ",
                "Zzz watch story",
                "http://x/zz",
                "2026-06-14 09:00:00",
                None,
                "SEC",
                "edgar_8k",
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


def test_headline_news_dropped_regardless_of_source_quality(tmp_path: Path) -> None:
    """Supersedes the 2026-07-14 'shit sources' quality filter at this surface:
    since 2026-07-30 NO headline news renders — reputable or content-farm."""
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
    assert "Nu real report" not in html
    assert "Nu farm recap" not in html


def test_stale_stream_carries_freshness_warn_chip(db: Path) -> None:
    """Wave B (B3): the fixture's newest signal is weeks old — the stream
    header must say so with a warn chip instead of silently rendering a
    stale feed as current."""
    html = render_diet_panel(db)
    assert "newest signal" in html
    assert 'class="k-chip k-chip-warn"' in html
    assert "may have stalled" in html


def test_fresh_stream_has_no_warn_chip(tmp_path: Path) -> None:
    """A stream whose newest signal is under 48h old gets the quiet muted
    stamp, never the warn chip."""
    from datetime import UTC, datetime

    d = tmp_path / "fresh.db"
    now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    # A rating row (yf_grades) — freshness is computed from the rows that
    # actually render, and headline news no longer does.
    make_news_then_signals(
        d,
        [("NU", "JPM maintains Buy on NU", "http://x/1", now, None, "JPM", "yf_grades", "t")],
    )
    html = render_diet_panel(d)
    assert "newest signal 0d ago" in html
    assert "k-chip-warn" not in html


def test_escapes_untrusted_headline(tmp_path: Path) -> None:
    # An EDGAR-fed row (renders in the filings block) — the escaping contract
    # must hold on the rows that still reach the page.
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
                "SEC",
                "edgar_8k",
                "t",
            )
        ],
    )
    html = render_diet_panel(d)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# Wave 3 (surface_density_jit_redesign.md D3, walkthrough #8): the stream is
# grouped by kind — parsed sell-side table, filings block, news reading list —
# never one undifferentiated chronological mix.
# --------------------------------------------------------------------------- #


def test_stream_groups_ratings_filings_and_news(tmp_path: Path) -> None:
    db = tmp_path / "signals.db"
    make_news_then_signals(
        db,
        [
            (
                "NOW",
                "Macquarie maintains Neutral on NOW; PT $109 -> $110",
                "http://s/1",
                "2026-07-23 10:00:00",
                None,
                "Macquarie",
                "yf_grades",
                "t",
            ),
            (
                "BN",
                "SC 13D/A: activist stake (>5%) amended - BROOKFIELD Corp",
                "http://s/2",
                "2026-07-24 00:31:19",
                None,
                "SEC",
                "edgar_13d",
                "t",
            ),
            (
                "NU",
                "Nu wins banking license in Mexico",
                "http://s/3",
                "2026-07-22 09:00:00",
                "s",
                "Reuters",
                "fmp_stock_news",
                "t",
            ),
        ],
    )
    html = render_diet_panel(db)

    # Ratings + filings group headers with deterministic summaries; the news
    # reading list is gone (2026-07-30) and its headline never renders.
    assert "Sell-side actions" in html
    assert "1 action(s) on 1 name(s)" in html
    assert "1 PT raise(s) / 0 cut(s)" in html
    assert "Filings" in html
    assert "News &amp; podcasts" not in html
    assert "Nu wins banking license in Mexico" not in html
    # The rating parsed into firm/action/PT columns with the delta anchored.
    assert "maintains <strong>Neutral</strong>" in html
    assert "$109 →" in html and "$110" in html and "(+1%)" in html
    # The filing's kind lifted into a chip, headline de-prefixed.
    assert 'k-chip k-chip-mono">SC 13D/A</span>' in html
    # Groups order: ratings then filings.
    assert html.index("Sell-side actions") < html.index("Filings")


def test_stream_empty_when_only_headline_news_exists(tmp_path: Path) -> None:
    """A DB holding nothing but headline news renders the honest empty state —
    the panel behaves as if the news lane never existed (hide-don't-stub)."""
    db = tmp_path / "signals.db"
    make_news_then_signals(
        db,
        [
            (
                "NU",
                "Nu launches product",
                "http://x/1",
                "2026-06-01 12:00:00",
                "s",
                "Reuters",
                "fmp_stock_news",
                "t",
            )
        ],
    )
    html = render_diet_panel(db)
    assert "Sell-side actions" not in html
    assert ">Filings" not in html
    assert "News &amp; podcasts" not in html
    assert "Nu launches product" not in html
    assert "No diet signals yet" in html
