"""Tests for src/dashboard/inbox_rank.py — categorization + transparent ranking.

The scorer is deliberately DB-light: category/severity/recency are pure
lookups, position weights are injected, and the two DB factors (thesis tone,
news-source refinement) read tiny hand-built SQLite files — no alembic
needed. Cross-kind dedupe lives in collect_inbox and is covered by
tests/test_dashboard_inbox.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from alerts import AlertRow
from dashboard.inbox import InboxItem
from dashboard.inbox_rank import annotate_and_rank, bucket_label

NOW = datetime(2026, 6, 10, 12, 0, 0)


def _alert_item(
    *,
    ticker: str = "NU",
    trigger_kind: str = "earnings_tone",
    status: str = "pending",
    when: datetime | None = None,
    evidence: dict[str, object] | None = None,
) -> InboxItem:
    when = when or NOW - timedelta(hours=2)
    row = AlertRow(
        id=1,
        user_id="default",
        ticker=ticker,
        trigger_kind=trigger_kind,
        fired_at=when,
        status=status,
        memo_artifact_id=None,
        evidence_json=json.dumps(evidence or {"summary": "x"}),
        signature_sha="sig",
        dismissed_at=None,
        approved_at=None,
    )
    return InboxItem(
        kind="alert",
        ticker=ticker,
        when=when,
        title=trigger_kind,
        body="",
        status=status,
        alert=row,
    )


def _item(
    kind: str,
    *,
    ticker: str | None = "NU",
    when: datetime | None = None,
    body: str = "body",
    status: str | None = None,
    title: str = "t",
) -> InboxItem:
    return InboxItem(
        kind=kind,
        ticker=ticker,
        when=when or NOW - timedelta(hours=2),
        title=title,
        body=body,
        status=status,
    )


def _rank(items: list[InboxItem], db_path: Path | None = None) -> list[InboxItem]:
    return annotate_and_rank(items, db_path=db_path, now=NOW, position_weights={})


# ----------------------------------------------------------------------------
# Categories
# ----------------------------------------------------------------------------


def test_category_mapping_per_kind_and_trigger() -> None:
    ranked = _rank(
        [
            _alert_item(trigger_kind="earnings_tone"),
            _alert_item(trigger_kind="kpi_inflection"),
            _alert_item(trigger_kind="thesis_drift"),
            _alert_item(trigger_kind="saydo_due"),
            _alert_item(trigger_kind="material_news", evidence={"headline": "NU opens hub"}),
            _item("draft", status="pending"),
            _item("ledger"),
            _item("note"),
            _item("synthesis", ticker=None),
        ]
    )
    by_title = {(it.kind, it.title): it.category for it in ranked}
    assert by_title[("alert", "earnings_tone")] == "earnings"
    assert by_title[("alert", "kpi_inflection")] == "thesis"
    assert by_title[("alert", "thesis_drift")] == "thesis"
    assert by_title[("alert", "saydo_due")] == "watch"
    assert by_title[("alert", "material_news")] == "news"
    assert by_title[("draft", "t")] == "drafts"
    assert by_title[("ledger", "t")] == "thesis"
    assert by_title[("note", "t")] == "watch"
    assert by_title[("synthesis", "t")] == "synthesis"


def test_rating_shaped_headline_refines_material_news_to_rating() -> None:
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"headline": "Morgan Stanley downgrades NU to Equal-Weight"},
    )
    assert _rank([item])[0].category == "rating"


def _news_db(tmp_path: Path, *, source: str, source_feed: str) -> Path:
    db = tmp_path / "news.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE news (id INTEGER PRIMARY KEY, ticker TEXT, headline TEXT, url TEXT, "
        "published_at TEXT, snippet TEXT, source TEXT, source_feed TEXT, fetched_at TEXT)"
    )
    conn.execute(
        "INSERT INTO news (id, ticker, headline, url, published_at, source, source_feed) "
        "VALUES (7, 'NU', 'Nu launches savings product', 'https://x', "
        "'2026-06-10 09:00:00', ?, ?)",
        (source, source_feed),
    )
    conn.commit()
    conn.close()
    return db


def test_press_wire_source_refines_material_news_to_press(tmp_path: Path) -> None:
    """The news row behind the alert (evidence.news_id) carries the source —
    a PR-wire source lands the story in Press releases."""
    db = _news_db(tmp_path, source="PRNewswire", source_feed="fmp_stock_news")
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"news_id": 7, "headline": "Nu launches savings product"},
    )
    assert _rank([item], db_path=db)[0].category == "press"


def test_future_grades_feed_rows_categorize_as_rating(tmp_path: Path) -> None:
    """Forward-compat: a news row from the (stubbed) dedicated grades feed
    categorizes as Rating changes via source_feed, no code change needed."""
    db = _news_db(tmp_path, source="UBS", source_feed="fmp_grades")
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"news_id": 7, "headline": "Nu launches savings product"},
    )
    assert _rank([item], db_path=db)[0].category == "rating"


# ----------------------------------------------------------------------------
# Score factors
# ----------------------------------------------------------------------------


def test_pending_outranks_dismissed_within_a_bucket() -> None:
    when = NOW - timedelta(hours=3)
    pending = _alert_item(status="pending", when=when)
    dismissed = _alert_item(status="dismissed", when=when)
    ranked = _rank([dismissed, pending])
    assert [it.status for it in ranked] == ["pending", "dismissed"]
    assert ranked[0].score > ranked[1].score


def test_recency_decay_is_monotonic_within_a_bucket() -> None:
    fresh = _item("ledger", when=NOW - timedelta(hours=1), body="a")
    older = _item("ledger", when=NOW - timedelta(hours=10), body="b")
    ranked = _rank([older, fresh])
    assert ranked[0].body == "a"
    assert ranked[0].score > ranked[1].score


def test_bucket_order_beats_score() -> None:
    """An Earlier item never outranks a Today item, whatever its severity —
    score orders WITHIN buckets, not across them."""
    today_low = _item("note", when=NOW - timedelta(hours=1), body="watch x")  # low severity
    old_high = _alert_item(
        trigger_kind="kpi_inflection", status="pending", when=NOW - timedelta(days=20)
    )
    ranked = _rank([old_high, today_low])
    assert ranked[0].kind == "note"
    assert bucket_label(ranked[0].when, NOW) == "Today"
    assert bucket_label(ranked[1].when, NOW) == "Earlier"


def test_position_weight_boosts_the_bigger_holding() -> None:
    when = NOW - timedelta(hours=2)
    big = _alert_item(ticker="NU", when=when)
    small = _alert_item(ticker="MELI", when=when)
    ranked = annotate_and_rank(
        [small, big],
        db_path=None,
        now=NOW,
        position_weights={"NU": 0.20, "MELI": 0.02},
    )
    assert [it.ticker for it in ranked] == ["NU", "MELI"]
    assert "20.0% of book" in ranked[0].score_why
    assert "2.0% of book" in ranked[1].score_why


def test_equal_weight_fallback_when_no_tracker_weights() -> None:
    ranked = _rank([_alert_item()])
    assert "equal-weight" in ranked[0].score_why


def test_thesis_relevance_boosts_warn_and_breach_tickers(tmp_path: Path) -> None:
    db = tmp_path / "thesis.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO thesis_evaluations VALUES (?,?,?)",
        [
            ("NU", "warn", "2026-06-09"),
            ("NU", "ok", "2026-06-01"),  # older row must not win
            ("GOOG", "breach", "2026-06-09"),
            ("META", "ok", "2026-06-09"),
        ],
    )
    conn.commit()
    conn.close()

    when = NOW - timedelta(hours=2)
    ranked = annotate_and_rank(
        [
            _alert_item(ticker="META", when=when),
            _alert_item(ticker="NU", when=when),
            _alert_item(ticker="GOOG", when=when),
        ],
        db_path=db,
        now=NOW,
        position_weights={},
    )
    assert [it.ticker for it in ranked] == ["GOOG", "NU", "META"]
    assert "thesis 1.50 (breach)" in ranked[0].score_why
    assert "thesis 1.25 (warn)" in ranked[1].score_why
    assert "thesis 1.00 (ok)" in ranked[2].score_why


def test_why_string_names_every_factor() -> None:
    why = _rank([_alert_item()])[0].score_why
    for token in ("severity", "x recency", "x position", "x thesis", "="):
        assert token in why
