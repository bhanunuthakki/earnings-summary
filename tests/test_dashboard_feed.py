"""Tests for src/dashboard/feed.py — the persistent chronological alert log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from alerts import (
    approve_alert,
    dismiss_alert,
    fire_alert,
)
from dashboard import render_alert_feed

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "dashboard_feed.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _evidence(memo: str = "memo") -> str:
    return json.dumps({"summary": "ok", "memo": memo})


# ----------------------------------------------------------------------------
# Empty
# ----------------------------------------------------------------------------


def test_empty_db_renders_empty_list(db_path: Path) -> None:
    html = render_alert_feed(db_path=db_path)
    assert "No alerts match" in html
    # The filter strip is still rendered so the analyst sees what they filtered by
    assert "ticker:" in html
    assert "trigger:" in html
    # Complete HTML document
    assert "<!doctype html>" in html


# ----------------------------------------------------------------------------
# Populated DB with mixed statuses
# ----------------------------------------------------------------------------


def _seed_mixed_statuses(db_path: Path) -> list[int]:
    """Seed 5 alerts of mixed status. Returns alert IDs in seed order.

    Order: 2 pending (GOOG kpi_inflection + META material_news),
           2 approved (GOOG saydo_due + NU thesis_drift),
           1 dismissed (META earnings_tone).
    """
    fired = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)
    seed: list[tuple[str, str, str]] = [
        ("GOOG", "kpi_inflection", "sig-1"),
        ("META", "material_news", "sig-2"),
        ("GOOG", "saydo_due", "sig-3"),
        ("NU", "thesis_drift", "sig-4"),
        ("META", "earnings_tone", "sig-5"),
    ]
    ids: list[int] = []
    for ticker, kind, sig in seed:
        row = fire_alert(
            ticker=ticker,
            trigger_kind=kind,
            fired_at=fired,
            evidence_json=_evidence(),
            signature_sha=sig,
            db_path=db_path,
        )
        ids.append(row.id)
    # Transition the appropriate ones
    approve_alert(ids[2], db_path=db_path)  # GOOG saydo_due → approved
    approve_alert(ids[3], db_path=db_path)  # NU thesis_drift → approved
    dismiss_alert(ids[4], db_path=db_path)  # META earnings_tone → dismissed
    return ids


def test_five_mixed_status_alerts_all_render_with_correct_badge(
    db_path: Path,
) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    # Sanity: the count line should reflect 5 alerts
    assert "5 shown" in html
    # The status badges should reflect the actual states
    assert html.count(">pending<") >= 2
    assert html.count(">approved<") >= 2
    assert html.count(">dismissed<") >= 1
    # All 5 trigger kinds appear
    for kind in (
        "kpi_inflection",
        "material_news",
        "saydo_due",
        "thesis_drift",
        "earnings_tone",
    ):
        assert kind in html


# ----------------------------------------------------------------------------
# Filters
# ----------------------------------------------------------------------------


def test_ticker_filter_narrows_to_one_ticker(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, ticker="GOOG")
    # Two GOOG alerts seeded; both should be in the result
    assert "2 shown" in html
    # META + NU should not appear in the alert cards (but ticker label "ALL"
    # also gets replaced with GOOG in the filter strip — so we'd see it once
    # at most, in the filter chip)
    assert html.count('class="ticker-badge">META') == 0
    assert html.count('class="ticker-badge">NU') == 0
    # The filter chip echoes the filter value
    assert 'ticker:</span> <span class="filter-value">GOOG' in html


def test_trigger_kind_filter_narrows_to_one_kind(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, trigger_kind="saydo_due")
    assert "1 shown" in html
    # The filter chip shows the requested trigger kind
    assert 'trigger:</span> <span class="filter-value">saydo_due' in html


def test_status_filter_narrows_to_pending(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, status="pending")
    assert "2 shown" in html
    # Only pending status badges in the alert list
    assert html.count(">approved<") == 0
    assert html.count(">dismissed<") == 0


def test_status_filter_narrows_to_dismissed(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, status="dismissed")
    assert "1 shown" in html
    # The dismissed status appears at least once (the badge for the lone alert)
    assert ">dismissed<" in html


def test_combined_ticker_and_status_filter(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, ticker="GOOG", status="approved")
    # GOOG saydo_due is the only GOOG that's approved
    assert "1 shown" in html
    assert "saydo_due" in html


# ----------------------------------------------------------------------------
# Document validity
# ----------------------------------------------------------------------------


def test_feed_html_is_a_complete_document(db_path: Path) -> None:
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    assert html.startswith("<!doctype html>")
    assert "</html>" in html


def test_feed_stream_carries_unread_tracking_markup(db_path: Path) -> None:
    """Inbox v2: the feed is unread-tracked too — full alert cards carry
    ``data-when`` and the page embeds INBOX_JS. The hover ✓/✕ quick actions
    stay rail-only (the feed keeps its <a> approve links)."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    assert 'data-ix-surface="feed"' in html
    assert 'data-when="' in html
    assert "ix-last-seen:" in html
    assert 'class="ix-act' not in html
