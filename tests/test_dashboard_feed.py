"""Tests for src/dashboard/feed.py — the persistent chronological alert log."""

from __future__ import annotations

import json
import sqlite3
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
    assert "No items match" in html
    # Nothing is filtered, so the header shows NO filter band (the redesign
    # drops the old "ALL · ALL · ALL" strip — only ACTIVE filters render).
    assert 'class="dash-filters"' not in html
    # Complete HTML document
    assert "<!doctype html>" in html


def test_feed_never_shows_disclosure_events_even_at_high_materiality(db_path: Path) -> None:
    """Disclosure drift is a research substrate, not a decision feed (owner
    ruling 2026-07-30; Disclosure Intelligence v1 PRD ruling 4 puts it on Ask +
    the ticker workspace).

    Seeds BOTH a high-materiality (0.92, 'substantive') and a low-materiality
    (0.20, 'noise') event. Neither may reach the feed. The high-materiality row
    is the one that matters: it would have passed the retired
    ``materiality >= 0.8`` gate, so this asserts the kind is gone rather than
    the threshold merely being tightened.
    """
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    base = (
        "NU",
        "item_reworded",
        "10-Q",
        2026,
        "Q2",
        2026,
        "Q1",
        "0001",
        None,
        "risk_factors",
    )
    conn.execute(
        """
        INSERT INTO disclosure_events
        (ticker, event_type, form, fiscal_year, fiscal_period,
         prior_fiscal_year, prior_fiscal_period, source_ref, source_doc_id,
         canonical_id, subject, subject_label, prior_excerpt, current_excerpt,
         evidence_quote, materiality, verdict, interpretation_md, confidence,
         detector_version, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            *base,
            "credit quality",
            "Credit quality",
            None,
            None,
            "Delinquency formation increased in the youngest vintages.",
            0.92,
            "substantive",
            "More company-specific risk language.",
            0.9,
            "test_v1",
            "new",
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO disclosure_events
        (ticker, event_type, form, fiscal_year, fiscal_period,
         prior_fiscal_year, prior_fiscal_period, source_ref, source_doc_id,
         canonical_id, subject, subject_label, prior_excerpt, current_excerpt,
         evidence_quote, materiality, verdict, interpretation_md, confidence,
         detector_version, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            *base,
            "formatting",
            "Formatting",
            None,
            None,
            "Minor punctuation changed.",
            0.20,
            "noise",
            "No substantive change.",
            0.9,
            "test_v1",
            "new",
            now,
        ),
    )
    conn.commit()
    conn.close()

    html = render_alert_feed(db_path=db_path)

    assert "Disclosure drift" not in html
    # The high-materiality row cleared the OLD gate — its absence is the proof
    # the kind was removed, not just re-thresholded.
    assert "Delinquency formation increased in the youngest vintages." not in html
    assert "Minor punctuation changed." not in html


def test_capped_feed_names_the_remainder_instead_of_truncating_silently(
    db_path: Path,
) -> None:
    """A capped stream must say what it dropped.

    The cap is real (``collect_inbox(limit=80)``) and was silently hiding 19
    live items — a truncated list reads as the whole queue. The seed is 5
    alerts, but the default feed carries pending + approved only (the dismissed
    one is settled noise), so 4 are eligible; ``limit=2`` cuts 2 of them.
    """
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, limit=2)

    assert 'class="ix-more"' in html
    assert "Showing the top 2 of 4" in html
    assert "2 lower-ranked items not shown" in html


def test_uncapped_feed_shows_no_remainder_line(db_path: Path) -> None:
    """The receipt appears ONLY when something was actually cut — otherwise it
    is noise on a complete list."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, limit=80)

    assert 'class="ix-more"' not in html
    assert "not shown" not in html


def test_active_filters_render_as_removable_chips(db_path: Path) -> None:
    """A narrowed feed shows each active filter as a chip linking back to the
    feed with that one constraint dropped — the others preserved."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path, ticker="GOOG", status="approved")
    assert 'class="dash-filters"' in html
    # The ticker chip drops itself but keeps status; the status chip vice-versa.
    assert 'href="/feed?status=approved"' in html
    assert 'href="/feed?ticker=GOOG"' in html
    assert 'ticker:</span> <span class="filter-value">GOOG' in html


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


def test_mixed_status_default_feed_shows_pending_and_approved_only(
    db_path: Path,
) -> None:
    """Red-team wave A: the DEFAULT stream carries the queue (pending) plus
    approved history — dismissed rows are settled noise and stay out unless
    explicitly requested via ?status=dismissed."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    # 2 pending + 2 approved; the dismissed META earnings_tone stays out.
    assert "4 shown" in html
    assert html.count(">pending<") >= 2
    assert html.count(">approved<") >= 2
    assert html.count(">dismissed<") == 0
    for kind in (
        "kpi_inflection",
        "material_news",
        "saydo_due",
        "thesis_drift",
    ):
        assert kind in html
    assert "earnings_tone" not in html  # the dismissed alert's kind


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
    assert '<button class="ix-act' not in html  # rail-only: no quick-action buttons in feed HTML


# ----------------------------------------------------------------------------
# Inbox v2: full stream, category chips, transparent ranking
# ----------------------------------------------------------------------------


def test_plain_feed_is_the_full_stream(db_path: Path) -> None:
    """With no alert-specific filters, /feed renders the WHOLE inbox — ledger
    entries and journal notes alongside alerts (Inbox v2)."""
    from user_state.ledger import append_entry
    from user_state.notes import create_note

    _seed_mixed_statuses(db_path)
    append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="ROE floor re-tested and held",
        db_path=db_path,
    )
    create_note(ticker="NU", kind="watch", body="Watch NIM trajectory", db_path=db_path)

    html = render_alert_feed(db_path=db_path)
    # 4 alerts (the dismissed one stays out of the default stream) + 1 ledger
    # entry + 1 note.
    assert "6 shown" in html
    assert "ROE floor re-tested and held" in html
    assert "Watch NIM trajectory" in html


def test_alert_filters_narrow_back_to_alerts_only(db_path: Path) -> None:
    """status / trigger_kind are alert-vocabulary filters — setting either
    drops the non-alert kinds from the stream."""
    from user_state.ledger import append_entry

    _seed_mixed_statuses(db_path)
    append_entry(
        ticker="NU",
        entry_kind="thesis_update",
        body="ROE floor re-tested and held",
        db_path=db_path,
    )
    html = render_alert_feed(db_path=db_path, status="pending")
    assert "ROE floor re-tested and held" not in html
    assert "2 shown" in html


def test_category_chips_render_with_counts(db_path: Path) -> None:
    """The feed renders the category filter chips: every seeded category
    appears with its count (kpi_inflection / thesis_drift → Thesis changes,
    saydo_due → Watch items, material_news → News), and cards carry the
    matching data-cat hooks the chip JS filters on. The dismissed
    earnings_tone alert is out of the default stream, so no Earnings chip."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    assert 'class="ix-cats"' in html
    assert "All <span>4</span>" in html
    assert "Earnings <span>" not in html  # only the dismissed alert was Earnings
    assert "News <span>1</span>" in html
    assert "Thesis changes <span>2</span>" in html
    assert "Watch items <span>1</span>" in html
    assert 'data-cat="thesis"' in html


def test_rating_change_headline_categorizes_as_rating(db_path: Path) -> None:
    """A material_news alert whose headline is upgrade/downgrade-shaped lands
    in the Rating-changes facet (the live leg of the category while the
    dedicated FMP grades feed stays unverified on the free tier)."""
    fired = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)
    fire_alert(
        ticker="NU",
        trigger_kind="material_news",
        fired_at=fired,
        evidence_json=json.dumps(
            {"headline": "UBS upgrades Nu Holdings to Buy", "why_material": "street turn"}
        ),
        signature_sha="sig-grade",
        db_path=db_path,
    )
    html = render_alert_feed(db_path=db_path)
    assert "Rating changes <span>1</span>" in html
    assert 'data-cat="rating"' in html


def test_every_card_carries_the_ranking_tooltip(db_path: Path) -> None:
    """The 'why ranked here' factor breakdown rides the trigger chip's title —
    one per card, naming every factor."""
    _seed_mixed_statuses(db_path)
    html = render_alert_feed(db_path=db_path)
    assert html.count('title="ranked: severity') == 4
    assert "x recency" in html
    assert "x position" in html
    assert "x thesis" in html
