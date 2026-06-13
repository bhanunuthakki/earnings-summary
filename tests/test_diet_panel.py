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
from signals.store import record_investor_day

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
    assert 'k-pill-accent">Rating' in html
    # forward agenda: the investor day as a dated row.
    assert "Forward agenda" in html
    assert "Analyst Day 2099" in html
    assert "2099-09-18" in html
    # kit classes only — no bespoke table/pill systems.
    for cls in ("p-table", "k-pill", "k-tick", "panel"):
        assert cls in html


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
