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
from pipeline.decision_journal_panel import render_decision_journal_panel


def _build_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    db_dir = tmp_path / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return migrated_db(db_dir / "portfolio.db")


def test_empty_state_renders_owner_journal(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    html = render_decision_journal_section(_build_db(tmp_path, migrated_db))
    assert "Owner Decision journal" in html
    assert "No Owner Decisions recorded yet." in html


def test_panel_renders_confirmed_owner_decision_only(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _build_db(tmp_path, migrated_db)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, created_at, "
            "user_action_kind, user_acted_at, outcome_label, outcome_pct, outcome_at, "
            "process_quality) VALUES ('NU','hold','owner','2026-06-10T00:00:00',"
            "'2026-06-10T00:00:00','followed','2026-06-10T00:01:00','correct',5.5,"
            "'2026-09-10T00:00:00','sound')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, made_at, created_at) "
            "VALUES ('WIX','avoid','advisor','2026-06-11T00:00:00','2026-06-11T00:00:00')"
        )
        conn.commit()

    html = render_decision_journal_section(db_path)
    assert "NU" in html
    assert "owner: hold" in html
    assert "followed" in html
    assert "correct" in html
    assert "+5.5%" in html
    assert "sound" in html
    assert "WIX" not in html
    assert "advisor history" not in html
    assert "attested" not in html
    assert "ping" not in html
    assert "nudge" not in html


def test_missing_view_degrades_to_empty_state(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    sqlite3.connect(db_path).close()
    html = render_decision_journal_section(db_path)
    assert "No Owner Decisions recorded yet." in html


def test_filter_chips_only_offer_owner_and_process_lessons(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    panel = render_decision_journal_panel(_build_db(tmp_path, migrated_db))
    assert 'data-dj-filter="owner"' in panel
    assert 'data-dj-filter="lessons"' in panel
    assert "adopted" not in panel
    assert "coaching" not in panel
    assert "All advice artifacts" not in panel


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
