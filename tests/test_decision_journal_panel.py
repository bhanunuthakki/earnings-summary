"""Tests for the owner decision journal panel."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from integrations.portfolio_tracker_client import LivePortfolio
from pipeline.allocation_decisions_panel import (
    compose_decisions_page,
    render_decision_journal_section,
)


def _build_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return migrated_db(db_dir / "portfolio.db")


def test_missing_view_degrades_to_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    sqlite3.connect(db_path).close()
    html = render_decision_journal_section(db_path)
    assert "No Owner Decisions recorded yet." in html


def test_compose_page_wires_journal_section_when_provided() -> None:
    offline = LivePortfolio(available=False, api_url="http://x", error="down")
    page_with = compose_decisions_page(
        [],
        [],
        offline,
        None,
        decision_journal_html='<section class="panel">JOURNAL_MARKER</section>',
    )
    assert "JOURNAL_MARKER" in page_with

    page_without = compose_decisions_page([], [], offline, None)
    assert "JOURNAL_MARKER" not in page_without
