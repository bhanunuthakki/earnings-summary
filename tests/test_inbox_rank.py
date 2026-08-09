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

import pytest

from alerts import AlertRow
from dashboard import inbox_rank
from dashboard.inbox import InboxItem
from dashboard.inbox_rank import annotate_and_rank, inbox_label, note_semantic_kind

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
    semantic_kind: str | None = None,
) -> InboxItem:
    return InboxItem(
        kind=kind,
        ticker=ticker,
        when=when or NOW - timedelta(hours=2),
        title=title,
        body=body,
        status=status,
        semantic_kind=semantic_kind,
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


def test_yf_grades_feed_rows_categorize_as_rating(tmp_path: Path) -> None:
    """The live additive grades feed (execution/fetch_yf_grades.py) rides the
    same source_feed hook as fmp_grades."""
    db = _news_db(tmp_path, source="Morgan Stanley", source_feed="yf_grades")
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"news_id": 7, "headline": "Nu launches savings product"},
    )
    assert _rank([item], db_path=db)[0].category == "rating"


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        # Disclosure-only items (7.01 / 8.01, with or without 9.01 exhibits
        # boilerplate) read as company-published press.
        ("8-K 7.01: Regulation FD disclosure — Nu Holdings Ltd.", "press"),
        ("8-K 8.01, 9.01: other events — Nu Holdings Ltd.", "press"),
        # Any material item keeps the filing in News — even alongside 8.01.
        ("8-K 2.01, 9.01: completed acquisition or disposition — Nu Holdings Ltd.", "news"),
        ("8-K 5.02, 8.01: executive or director change — Nu Holdings Ltd.", "news"),
        ("8-K/A 4.02: non-reliance on prior financials (restatement) — Nu", "news"),
        # No parseable item codes -> a corporate event, News.
        ("8-K: filing — Nu Holdings Ltd.", "news"),
    ],
)
def test_edgar_8k_rows_categorize_by_item_codes(
    tmp_path: Path, headline: str, expected: str
) -> None:
    db = _news_db(tmp_path, source="SEC EDGAR", source_feed="edgar_8k")
    item = _alert_item(trigger_kind="material_news", evidence={"news_id": 7, "headline": headline})
    assert _rank([item], db_path=db)[0].category == expected


def test_edgar_ownership_rows_stay_news(tmp_path: Path) -> None:
    """13D/G stake disclosures are market events, not PR — and their headlines
    must not trip the rating regex or wire heuristics."""
    db = _news_db(tmp_path, source="SEC EDGAR", source_feed="edgar_13d")
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"news_id": 7, "headline": "SC 13D: activist stake (>5%) disclosed — Nu"},
    )
    assert _rank([item], db_path=db)[0].category == "news"


# ----------------------------------------------------------------------------
# Score factors
# ----------------------------------------------------------------------------


def test_pending_outranks_dismissed_at_equal_age() -> None:
    when = NOW - timedelta(hours=3)
    pending = _alert_item(status="pending", when=when)
    dismissed = _alert_item(status="dismissed", when=when)
    ranked = _rank([dismissed, pending])
    assert [it.status for it in ranked] == ["pending", "dismissed"]
    assert ranked[0].score > ranked[1].score


def test_recency_decay_is_monotonic() -> None:
    fresh = _item("ledger", when=NOW - timedelta(hours=1), body="a")
    older = _item("ledger", when=NOW - timedelta(hours=10), body="b")
    ranked = _rank([older, fresh])
    assert ranked[0].body == "a"
    assert ranked[0].score > ranked[1].score


def test_score_orders_flat_across_days() -> None:
    """No bucket tiers (owner feedback 2026-06-11): yesterday's pending
    earnings alert outranks today's low-severity watch note — the per-card
    relative stamp carries the "when", the score carries the "how much"."""
    yesterday_earnings = _alert_item(
        trigger_kind="earnings_tone", status="pending", when=NOW - timedelta(hours=26)
    )
    today_note = _item("note", when=NOW - timedelta(hours=1), body="watch x")
    ranked = _rank([today_note, yesterday_earnings])
    assert [it.kind for it in ranked] == ["alert", "note"]
    assert ranked[0].score > ranked[1].score


def test_earnings_carry_the_single_highest_severity() -> None:
    """Owner feedback 2026-06-11: earnings outrank every other category at
    equal age/status — including thesis changes, the old top weight."""
    when = NOW - timedelta(hours=2)
    ranked = _rank(
        [
            _alert_item(
                trigger_kind="material_news",
                status="open",
                when=when,
                evidence={"headline": "NU opens hub"},
            ),
            _alert_item(trigger_kind="kpi_inflection", status="open", when=when),
            _alert_item(trigger_kind="earnings_tone", status="open", when=when),
        ]
    )
    assert [it.category for it in ranked] == ["earnings", "thesis", "news"]


def test_synthesis_ranks_below_news() -> None:
    """Owner feedback 2026-06-11: memo cards are background reading — a fresh
    synthesis section sits below a plain news alert of the same age."""
    when = NOW - timedelta(hours=2)
    news = _alert_item(
        trigger_kind="material_news",
        status="open",
        when=when,
        evidence={"headline": "NU opens hub"},
    )
    memo = _item("synthesis", ticker=None, when=when, body="weekly memo section")
    ranked = _rank([memo, news])
    assert [it.category for it in ranked] == ["news", "synthesis"]


def test_advisor_memo_ledger_entry_rides_the_synthesis_weight() -> None:
    """The advisor's persist_memo ledger echo (title "Advisor memo") is memo
    commentary, not a thesis change: it categorizes as synthesis and lands
    below plain news — the "advisor memo up top" complaint, fixed."""
    when = NOW - timedelta(hours=2)
    memo = _item("ledger", when=when, title="Advisor memo", body="swap check: hold")
    plain = _item("ledger", when=when, body="thesis edit")
    news = _alert_item(
        trigger_kind="material_news",
        status="open",
        when=when,
        evidence={"headline": "NU opens hub"},
    )
    ranked = _rank([memo, plain, news])
    assert [it.category for it in ranked] == ["thesis", "news", "synthesis"]
    assert ranked[-1].title == "Advisor memo"


def test_advisor_memo_note_demotes_to_synthesis_by_identity() -> None:
    """The portfolio next-dollar memo reaches the inbox ONLY as an advisor
    analyst_note (no ledger echo — ticker=None, write_ledger=False). It must
    still demote to synthesis weight by IDENTITY (semantic_kind), not by the
    ledger title — that path doesn't exist for it."""
    when = NOW - timedelta(hours=2)
    memo_note = _item(
        "note", ticker=None, when=when, title="observation", semantic_kind="advisor_memo"
    )
    plain_note = _item("note", ticker="NU", when=when, title="watch")
    news = _alert_item(
        trigger_kind="material_news",
        status="open",
        when=when,
        evidence={"headline": "NU opens hub"},
    )
    # annotate_and_rank returns fresh objects (dataclasses.replace), so locate
    # the ranked items by stable attributes, not input identity.
    ranked = _rank([memo_note, plain_note, news])
    memo = next(it for it in ranked if it.semantic_kind == "advisor_memo")
    plain = next(it for it in ranked if it.kind == "note" and it.semantic_kind is None)
    event = next(it for it in ranked if it.kind == "alert")
    assert memo.category == "synthesis"
    assert plain.category == "watch"
    # The machine-authored memo sits below the plain news event of equal age.
    assert ranked.index(event) < ranked.index(memo)


def test_inbox_label_resolves_identity_not_source_table() -> None:
    """The one shared resolver: advisor memos read as "Advisor memo" whichever
    table they echoed through; note kinds humanize (the raw enum never shows);
    reconcile-pending notes carry the prefix."""
    advisor_note = _item("note", title="observation", semantic_kind="advisor_memo")
    advisor_ledger = _item("ledger", title="Advisor memo")  # back-compat: title only
    plain_obs = _item("note", title="observation")
    watch = _item("note", title="watch")
    reconcile = _item("note", title="watch", status="pending")
    assert inbox_label(advisor_note) == "Advisor memo"
    assert inbox_label(advisor_ledger) == "Advisor memo"
    assert inbox_label(plain_obs) == "Note"
    assert inbox_label(watch) == "Watch item"
    assert inbox_label(reconcile) == "Reconcile · Watch item"
    # No raw enum survives the resolver.
    for it in (advisor_note, plain_obs, watch, reconcile):
        assert "observation" not in inbox_label(it)


def test_note_semantic_kind_reads_provenance() -> None:
    """Identity is read from the note's source/source_ref/context — ANY of the
    three advisor signals is sufficient; an ordinary note gets None."""
    assert note_semantic_kind("advisor", "advisor_memo:7", {"memo_id": 7}) == "advisor_memo"
    assert note_semantic_kind("manual", "advisor_memo:7", None) == "advisor_memo"
    assert note_semantic_kind("comment", None, {"memo_id": 7}) == "advisor_memo"
    assert note_semantic_kind("comment", "TICK/2026-06-10/c1", None) is None
    assert note_semantic_kind("manual", None, {"intent": "edit_thesis"}) is None


def test_recency_floor_ties_break_newest_first() -> None:
    """Two items deep past the decay floor score identically — the newer one
    renders first (the flat sort's only secondary key)."""
    newer = _item("ledger", when=NOW - timedelta(days=10), body="newer")
    older = _item("ledger", when=NOW - timedelta(days=12), body="older")
    ranked = _rank([older, newer])
    assert [it.body for it in ranked] == ["newer", "older"]
    assert ranked[0].score == ranked[1].score


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
    # Tone text is the kit vocabulary (ui.controls.thesis_status_tone), not
    # the raw thesis_evaluations status word — 'breach' normalizes to 'bad'.
    assert "thesis 1.50 (bad)" in ranked[0].score_why
    assert "thesis 1.25 (warn)" in ranked[1].score_why
    assert "thesis 1.00 (ok)" in ranked[2].score_why


def test_ranking_uses_borrowed_connection_for_all_db_enrichment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request-scoped ranking must not open or close nested DB connections."""
    db = tmp_path / "ranking.db"
    borrowed = sqlite3.connect(db)
    borrowed.executescript(
        """
        CREATE TABLE thesis_evaluations (
            ticker TEXT, overall_status TEXT, evaluated_at TEXT
        );
        CREATE TABLE news (
            id INTEGER PRIMARY KEY, source TEXT, source_feed TEXT
        );
        CREATE TABLE signals (
            news_id INTEGER, signal_type TEXT
        );
        INSERT INTO thesis_evaluations VALUES ('NU', 'warn', '2026-06-09');
        INSERT INTO news VALUES (7, 'Newswire', 'stock_news');
        INSERT INTO signals VALUES (7, 'consensus_rating');
        """
    )
    borrowed.commit()

    def reject_nested_open(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise AssertionError("ranking opened a nested SQLite connection")

    monkeypatch.setattr(inbox_rank, "connect_sqlite", reject_nested_open)
    item = _alert_item(
        trigger_kind="material_news",
        evidence={"news_id": 7, "headline": "Nu update"},
    )

    ranked = annotate_and_rank(
        [item],
        db_path=db,
        now=NOW,
        position_weights={},
        conn=borrowed,
    )

    assert ranked[0].category == "rating"
    assert "thesis 1.25 (warn)" in ranked[0].score_why
    assert borrowed.execute("SELECT 1").fetchone() == (1,)
    borrowed.close()


def test_unresolved_thesis_status_does_not_get_the_breach_factor(tmp_path: Path) -> None:
    """An unrecognized/unresolved overall_status must normalize to neutral
    (1.0), never fall through a bare `else` into the breach multiplier."""
    db = tmp_path / "thesis.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT)"
    )
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','unresolved','2026-06-09')")
    conn.commit()
    conn.close()

    when = NOW - timedelta(hours=2)
    ranked = annotate_and_rank(
        [_alert_item(ticker="NU", when=when)], db_path=db, now=NOW, position_weights={}
    )
    assert "thesis 1.00 (n/a)" in ranked[0].score_why


def test_genuine_breach_status_still_gets_the_severity_boost(tmp_path: Path) -> None:
    """'breach' and 'broken' (the kit's other bad-tone status word) both still
    earn the 1.5x multiplier after routing through thesis_status_tone."""
    db = tmp_path / "thesis.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO thesis_evaluations VALUES (?,?,?)",
        [
            ("NU", "breach", "2026-06-09"),
            ("MELI", "broken", "2026-06-09"),
        ],
    )
    conn.commit()
    conn.close()

    when = NOW - timedelta(hours=2)
    ranked = annotate_and_rank(
        [_alert_item(ticker="NU", when=when), _alert_item(ticker="MELI", when=when)],
        db_path=db,
        now=NOW,
        position_weights={},
    )
    for it in ranked:
        assert "thesis 1.50 (bad)" in it.score_why


def test_none_weights_read_materialized_cache_not_network(tmp_path: Path) -> None:
    """``position_weights=None`` reads the disk cache beside the DB (no tracker
    call) — the render-path weight source after the S12 latency fix. The cache
    lives at ``<repo>/data/portfolio_weights.json`` and the DB at
    ``<repo>/data/portfolio.db``, so the repo root is two parents up."""
    import portfolio_weights as pw
    from integrations.portfolio_tracker_client import LivePortfolio, LivePosition

    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "portfolio.db"
    db.write_bytes(b"")  # exists (thesis_tones degrades to {} on the empty file)
    pw.materialize_weights(
        tmp_path,
        LivePortfolio(
            available=True,
            api_url="x",
            positions=[
                LivePosition("NU", "NU", 1.0, None, None, None, 25.0),
                LivePosition("MELI", "MELI", 1.0, None, None, None, 1.0),
            ],
        ),
    )
    when = NOW - timedelta(hours=2)
    ranked = annotate_and_rank(
        [_alert_item(ticker="MELI", when=when), _alert_item(ticker="NU", when=when)],
        db_path=db,
        now=NOW,
        position_weights=None,
    )
    assert [it.ticker for it in ranked] == ["NU", "MELI"]
    assert "25.0% of book" in ranked[0].score_why


def test_none_weights_no_cache_falls_back_to_equal_weight(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "portfolio.db"
    db.write_bytes(b"")  # no portfolio_weights.json beside it
    ranked = annotate_and_rank([_alert_item()], db_path=db, now=NOW, position_weights=None)
    assert "equal-weight" in ranked[0].score_why


def test_thesis_tones_memo_invalidates_on_db_write(tmp_path: Path) -> None:
    """The (path, mtime) memo serves repeated renders for free but busts when
    the DB file changes — no stale ranking factor after a re-evaluation."""
    import os

    db = tmp_path / "thesis.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT)"
    )
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','ok','2026-06-09')")
    conn.commit()
    conn.close()

    when = NOW - timedelta(hours=2)
    first = annotate_and_rank(
        [_alert_item(ticker="NU", when=when)], db_path=db, now=NOW, position_weights={}
    )
    assert "thesis 1.00 (ok)" in first[0].score_why

    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','breach','2026-06-11')")
    conn.commit()
    conn.close()
    # Force a clearly-later mtime so the bust is observable regardless of
    # filesystem mtime resolution.
    later = db.stat().st_mtime + 5
    os.utime(db, (later, later))

    second = annotate_and_rank(
        [_alert_item(ticker="NU", when=when)], db_path=db, now=NOW, position_weights={}
    )
    # Tone text is the kit vocabulary — 'breach' normalizes to 'bad'.
    assert "thesis 1.50 (bad)" in second[0].score_why


def test_thesis_tones_memo_invalidates_on_uncheckpointed_wal_write(tmp_path: Path) -> None:
    db = tmp_path / "thesis-wal.db"
    anchor = sqlite3.connect(db)
    try:
        assert anchor.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        anchor.execute(
            "CREATE TABLE thesis_evaluations (ticker TEXT, overall_status TEXT, evaluated_at TEXT)"
        )
        anchor.execute("INSERT INTO thesis_evaluations VALUES ('NU','ok','2026-06-09')")
        anchor.commit()
        when = NOW - timedelta(hours=2)
        first = annotate_and_rank(
            [_alert_item(ticker="NU", when=when)], db_path=db, now=NOW, position_weights={}
        )
        assert "thesis 1.00 (ok)" in first[0].score_why
        main_mtime = db.stat().st_mtime_ns

        writer = sqlite3.connect(db)
        try:
            writer.execute("INSERT INTO thesis_evaluations VALUES ('NU','breach','2026-06-11')")
            writer.commit()
        finally:
            writer.close()

        assert db.stat().st_mtime_ns == main_mtime
        second = annotate_and_rank(
            [_alert_item(ticker="NU", when=when)], db_path=db, now=NOW, position_weights={}
        )
        assert "thesis 1.50 (bad)" in second[0].score_why
    finally:
        anchor.close()


def test_why_string_names_every_factor() -> None:
    why = _rank([_alert_item()])[0].score_why
    for token in ("severity", "x recency", "x position", "x thesis", "x strength", "="):
        assert token in why


def test_signal_strength_orders_within_a_category() -> None:
    """Owner feedback 2026-07-14: ordering felt random because a marginal and a
    screaming alert of the same kind scored identically. A high-|z| KPI inflection
    now outranks a barely-significant one (same ticker, recency, position)."""
    strong = _alert_item(trigger_kind="kpi_inflection", evidence={"zscore": 6.0})
    weak = _alert_item(trigger_kind="kpi_inflection", evidence={"zscore": 2.1})
    ranked = _rank([weak, strong])
    assert ranked[0].score > ranked[1].score
    assert "|z| 6.0" in ranked[0].score_why


def test_owner_falsifier_breach_is_max_strength() -> None:
    """A decision_condition breach whose evidence says the OWNER authored the
    decision is the strongest signal class — it takes the strength ceiling."""
    it = _alert_item(
        trigger_kind="decision_condition",
        evidence={"detail": "NPL > 5%", "decided_by": "owner"},
    )
    why = _rank([it])[0].score_why
    assert "strength 1.50 (owner falsifier breach)" in why


def test_advisor_condition_breach_gets_modest_strength() -> None:
    """An advisor-authored condition breach is informative, not screaming —
    it never takes the owner-falsifier ceiling."""
    it = _alert_item(
        trigger_kind="decision_condition",
        evidence={"detail": "NPL > 5%", "decided_by": "advisor"},
    )
    why = _rank([it])[0].score_why
    assert "strength 1.15 (advisor condition breach)" in why


def test_condition_breach_missing_decided_by_treated_as_advisor() -> None:
    """Evidence rows written before decided_by was threaded through lack the
    key — treated as advisor (modest), never boosted to the ceiling."""
    it = _alert_item(trigger_kind="decision_condition", evidence={"detail": "NPL > 5%"})
    why = _rank([it])[0].score_why
    assert "strength 1.15 (advisor condition breach)" in why


def test_owner_capacity_breach_is_max_strength() -> None:
    """tenet-2 Phase 3: an owner_capacity_breach alert (human-capital cap or
    cash-floor policy crossed) earns the SAME tier-1 ceiling as an owner
    falsifier breach — decisive_alert_reason is the one shared definition
    driving both ranking weight and severity color, by construction."""
    it = _alert_item(
        trigger_kind="owner_capacity_breach",
        evidence={"policy_kind": "owner_capacity", "capacity_kind": "human_capital", "bucket": "x"},
    )
    why = _rank([it])[0].score_why
    assert "strength 1.50 (owner policy breach)" in why


def test_capacity_breach_missing_policy_kind_is_not_decisive() -> None:
    """A malformed/foreign evidence row under this trigger_kind (missing the
    policy_kind marker) is never treated as decisive — no guessing."""
    it = _alert_item(
        trigger_kind="owner_capacity_breach",
        evidence={"capacity_kind": "human_capital"},
    )
    why = _rank([it])[0].score_why
    assert "strength 1.00 (n/a)" in why


def test_decisive_alert_reason_owner_capacity_breach_cases() -> None:
    from dashboard.inbox_rank import decisive_alert_reason

    ev = json.dumps({"policy_kind": "owner_capacity", "capacity_kind": "cash_floor"})
    assert decisive_alert_reason("owner_capacity_breach", ev) == "owner policy breach"
    assert decisive_alert_reason("owner_capacity_breach", json.dumps({})) is None
    assert decisive_alert_reason("owner_capacity_breach", None) is None


def test_strength_is_inert_without_a_magnitude() -> None:
    """An alert with no recognised magnitude field is neutral (1.0) — the factor
    never penalises alerts it can't score."""
    why = _rank([_alert_item(trigger_kind="earnings_tone")])[0].score_why
    assert "strength 1.00 (n/a)" in why
