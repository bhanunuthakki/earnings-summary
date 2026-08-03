"""THE diet-vs-alert guard (interaction_paradigm §2 rule 9; design_language
"Diet-vs-alert").

The structural invariant this PR exists to enforce: a `signals` DIET row NEVER
enters the inbox_rank decaying scorer (or the materiality veto). Proven three
ways:

  1. Definitively — over a FULL inbox schema with the alert/note tables present
     but empty and the `signals` table populated, ``collect_inbox`` returns []
     while ``load_diet_signals`` returns the rows. Same DB: the diet lane has
     content, the scorer sees none. So the [] is "the inbox ignores signals",
     not "the tables are missing".
  2. Type-level — ``load_diet_signals`` yields ``SignalRow``, never an
     ``InboxItem``; the diet panel never imports the scorer.
  3. Identity — ``_categorize`` consults the STORED ``signal_type`` to type a
     material_news alert (consensus_rating → Rating), authoritative over the
     headline regex, scoped to the news-mirrored types — extending the S3
     categorizer, not re-cutting it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alerts import AlertRow
from dashboard.inbox import InboxItem, collect_inbox
from dashboard.inbox_rank import annotate_and_rank
from identity import DEFAULT_USER_ID
from signals.store import (
    SIGNAL_CONSENSUS_RATING,
    SignalRow,
    load_diet_signals,
    record_investor_day,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Same launch point as tests/test_dashboard_inbox.py — everything collect_inbox
# reads (alerts, queued_actions, analyst_notes, thesis_*, news, signals) is
# created at or after this revision, so stamp-then-upgrade-head is a full,
# working inbox schema.
PRIOR_HEAD = "0059_kpi_facts_restatement"
NOW = datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture
def full_db(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    return migrated_db(tmp_path / "diet_guard.db", stamp=PRIOR_HEAD)


def _seed_signals(db: Path) -> None:
    """Populate the diet lane (and only the diet lane) with every type that has
    live data: a sell-side rating, general news, a forward investor day."""
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO signals (ticker, signal_type, title, published_at, weight, cadence, "
            "source_feed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "META",
                    "consensus_rating",
                    "MS upgrades META",
                    "2026-06-11 09:00:00",
                    0.8,
                    "event",
                    "yf_grades",
                    "2026-06-11 10:00:00",
                ),
                (
                    "NU",
                    "general_news",
                    "Nu launches product",
                    "2026-06-12 09:00:00",
                    0.5,
                    "event",
                    "fmp_stock_news",
                    "2026-06-12 10:00:00",
                ),
            ],
        )
        conn.commit()
        record_investor_day(conn, "META", datetime(2026, 9, 18).date(), "Analyst Day")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The definitive structural proof
# ---------------------------------------------------------------------------


def test_diet_rows_never_enter_the_inbox_scorer(full_db: Path) -> None:
    """Full inbox schema, NO alerts/notes/drafts — only diet `signals` rows.
    The scorer returns nothing; the diet reader returns the rows. The two lanes
    are disjoint."""
    _seed_signals(full_db)

    inbox = collect_inbox(full_db, now=NOW, position_weights={})
    assert inbox == [], "a signals DIET row leaked into the decaying inbox scorer"

    diet = load_diet_signals(full_db)
    assert len(diet) == 2  # the two news-backed reading lanes
    assert all(isinstance(r, SignalRow) for r in diet)


def test_collect_inbox_ignores_signals_even_with_real_inbox_content(full_db: Path) -> None:
    """Add a genuine alert: it ranks. The diet signals beside it still don't —
    the stream is exactly the one alert."""
    _seed_signals(full_db)
    conn = sqlite3.connect(str(full_db))
    try:
        conn.execute(
            "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
            "evidence_json, signature_sha) VALUES (?, 'NU', 'earnings_tone', "
            "'2026-06-13 08:00:00', 'pending', '{}', 'sig1')",
            (DEFAULT_USER_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    inbox = collect_inbox(full_db, now=NOW, position_weights={})
    assert [it.title for it in inbox] == ["earnings_tone"]
    assert all(it.kind != "signal" for it in inbox)


# ---------------------------------------------------------------------------
# 2. Type-level — the diet panel never reaches for the scorer
# ---------------------------------------------------------------------------


def test_diet_panel_does_not_import_the_decaying_scorer() -> None:
    src = (PROJECT_ROOT / "src" / "pipeline" / "diet_panel.py").read_text(encoding="utf-8")
    assert "inbox_rank" not in src
    assert "annotate_and_rank" not in src


def test_load_diet_signals_is_non_decaying(full_db: Path) -> None:
    """Ordering is a pure stored-field sort (published_at DESC) — independent of
    `now`. A decaying scorer would reorder as the clock moves; this never does."""
    _seed_signals(full_db)
    order_a = [r.id for r in load_diet_signals(full_db)]
    # The reader takes no `now` and applies no half-life — re-reading is stable.
    order_b = [r.id for r in load_diet_signals(full_db)]
    assert order_a == order_b
    stamps = [r.published_at for r in load_diet_signals(full_db)]
    assert stamps == sorted(stamps, reverse=True)


# ---------------------------------------------------------------------------
# 3. Identity over source — _categorize consults the stored signal_type
# ---------------------------------------------------------------------------


def _news_and_signal_db(
    tmp_path: Path, *, headline: str, source: str, source_feed: str, signal_type: str | None
) -> Path:
    """A `news` row (id=7) + optionally a mirrored `signals` row pointing at it.
    Minimal hand-built schema — this is a unit test of the categorizer leg."""
    db = tmp_path / "cat.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE news (id INTEGER PRIMARY KEY, ticker TEXT, headline TEXT, url TEXT, "
        "published_at TEXT, snippet TEXT, source TEXT, source_feed TEXT, fetched_at TEXT)"
    )
    conn.execute(
        "INSERT INTO news (id, ticker, headline, url, published_at, source, source_feed) "
        "VALUES (7, 'NU', ?, 'https://x', '2026-06-10 09:00:00', ?, ?)",
        (headline, source, source_feed),
    )
    conn.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY, news_id INTEGER, signal_type TEXT)")
    if signal_type is not None:
        conn.execute("INSERT INTO signals (news_id, signal_type) VALUES (7, ?)", (signal_type,))
    conn.commit()
    conn.close()
    return db


def _material_news_item() -> InboxItem:
    when = NOW - timedelta(hours=2)
    row = AlertRow(
        id=1,
        user_id="default",
        ticker="NU",
        trigger_kind="material_news",
        fired_at=when,
        status="pending",
        memo_artifact_id=None,
        evidence_json=json.dumps({"news_id": 7, "headline": "Nu provides corporate update"}),
        signature_sha="sig",
        dismissed_at=None,
        approved_at=None,
    )
    return InboxItem(
        kind="alert",
        ticker="NU",
        when=when,
        title="material_news",
        body="",
        status="pending",
        alert=row,
    )


def test_stored_consensus_rating_types_the_alert_as_rating(tmp_path: Path) -> None:
    """A neutral headline + a non-grades source — neither the regex nor the
    source-feed leg fires. Only the STORED consensus_rating signal_type lands it
    in Rating changes. Identity over source."""
    db = _news_and_signal_db(
        tmp_path,
        headline="Nu provides corporate update",
        source="Bloomberg",
        source_feed="fmp_stock_news",
        signal_type=SIGNAL_CONSENSUS_RATING,
    )
    ranked = annotate_and_rank([_material_news_item()], db_path=db, now=NOW, position_weights={})
    assert ranked[0].category == "rating"


def test_general_news_signal_does_not_force_rating(tmp_path: Path) -> None:
    """The scoping: a `general_news` mirror is news-mirrored too, but it must NOT
    push a neutral story into Rating — only consensus_rating does."""
    db = _news_and_signal_db(
        tmp_path,
        headline="Nu provides corporate update",
        source="Bloomberg",
        source_feed="fmp_stock_news",
        signal_type="general_news",
    )
    ranked = annotate_and_rank([_material_news_item()], db_path=db, now=NOW, position_weights={})
    assert ranked[0].category == "news"


def test_categorize_degrades_without_signals_table(tmp_path: Path) -> None:
    """Pre-0094 DB (no `signals`): the categorizer falls back to the existing
    source-feed / regex legs exactly as before — no regression."""
    db = _news_and_signal_db(
        tmp_path,
        headline="Nu provides corporate update",
        source="Bloomberg",
        source_feed="fmp_stock_news",
        signal_type=None,
    )
    # drop the signals table to simulate a pre-migration DB
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE signals")
    conn.commit()
    conn.close()
    ranked = annotate_and_rank([_material_news_item()], db_path=db, now=NOW, position_weights={})
    assert ranked[0].category == "news"
