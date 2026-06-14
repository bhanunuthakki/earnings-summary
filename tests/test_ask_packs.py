"""Portfolio evidence packs (src/ask/packs.py, S4): per-pack content,
absence-explicit lines, char budgets, tracker degradation, and load_packs
ordering/tolerance. Tracker calls are monkeypatched — nothing here touches
HTTP or an LLM."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import ask.packs as packs
from ask.packs import PACK_KEYS, PACKS, load_packs
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import (
    Concentration,
    LivePortfolio,
    LivePosition,
    PerformancePoint,
    PerformanceSeries,
    PortfolioAnalytics,
    PositionAlpha,
    PositionAlphaRow,
    Positioning,
)

_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL, archived_at TIMESTAMP
);
CREATE TABLE position_sizing_intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ticker TEXT NOT NULL, intent_kind TEXT NOT NULL,
    intent_value REAL, narrative TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, valuation_date TEXT NOT NULL,
    npv_per_share NUMERIC, live_price NUMERIC,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, recommendation_kind TEXT NOT NULL,
    recommendation_value REAL, conviction TEXT,
    source_artifact_id INTEGER NOT NULL DEFAULT 0, source_lens TEXT,
    rationale_excerpt TEXT, made_at TEXT NOT NULL, user_acted_at TEXT,
    user_action_kind TEXT, user_notes TEXT, outcome_at TEXT,
    outcome_label TEXT, outcome_pct REAL, outcome_notes TEXT,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL, ticker TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
    body TEXT NOT NULL, anchor_type TEXT, anchor_key TEXT,
    source TEXT NOT NULL DEFAULT 'manual', source_ref TEXT, supersedes_id INTEGER,
    resolution_note TEXT, context_json TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, 'portfolio')",
        [("NU", "Nu Holdings"), ("MELI", "MercadoLibre")],
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('TSM', 'TSMC', 'watchlist')"
    )
    intents = [
        ("NU", "conviction", 4.0, "compounder, hold through vol", "2026-05-20T10:00:00"),
        ("NU", "conviction", 3.0, "older row, must lose", "2026-01-05T10:00:00"),
        ("NU", "target_weight_pct", 12.0, None, "2026-05-21T10:00:00"),
        ("MELI", "target_weight_pct", 8.0, "trim into strength", "2026-04-01T10:00:00"),
    ]
    conn.executemany(
        "INSERT INTO position_sizing_intent "
        "(user_id, ticker, intent_kind, intent_value, narrative, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(DEFAULT_USER_ID, t, k, v, n, ts, ts) for t, k, v, n, ts in intents],
    )
    runs = [
        ("NU", "2026-06-08", 13.96, 12.10, "2026-06-08 09:00:00"),
        ("NU", "2026-01-02", 11.00, 10.00, "2026-01-02 09:00:00"),  # older, must lose
        ("MELI", "2026-06-01", 2400.0, 2000.0, "2026-06-01 09:00:00"),
        ("STNE", "2026-06-01", 62.0, 10.0, "2026-06-01 09:00:00"),  # +520% — suspect
    ]
    conn.executemany(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        runs,
    )
    conn.executemany(
        "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, conviction, "
        "made_at, user_action_kind, outcome_label, outcome_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("NU", "trim", 20.0, "high", "2026-05-02T09:00:00", "followed", None, None),
            ("MELI", "add", 15.0, "medium", "2026-03-15T09:00:00", None, "correct", 18.0),
        ],
    )
    notes = [
        ("NU", "watch", "open", "NPL formation above 8% trims the thesis", "2026-04-02T10:00:00"),
        (None, "question", "open", "Is the book too LatAm-heavy?", "2026-05-10T10:00:00"),
        ("NU", "watch", "archived", "stale archived note, must not appear", "2026-01-01T10:00:00"),
        ("TSM", "observation", "open", "TSM note outside focus", "2026-05-11T10:00:00"),
    ]
    conn.executemany(
        "INSERT INTO analyst_notes (user_id, ticker, kind, status, body, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(DEFAULT_USER_ID, t, k, s, b, ts, ts) for t, k, s, b, ts in notes],
    )
    conn.commit()
    conn.close()
    return path


def _live(available: bool = True) -> LivePortfolio:
    if not available:
        return LivePortfolio(available=False, api_url="x", error="ConnectionError: down")
    positions = [
        LivePosition(
            ticker="NU",
            name="Nu",
            quantity=1000,
            market_value=22_500.0,
            cost_basis=15_000.0,
            unrealized_pnl=7_500.0,
            percent_of_portfolio=18.2,
        ),
        LivePosition(
            ticker="MELI",
            name="Meli",
            quantity=10,
            market_value=15_000.0,
            cost_basis=16_000.0,
            unrealized_pnl=-1_000.0,
            percent_of_portfolio=12.1,
        ),
    ]
    return LivePortfolio(
        available=True, api_url="x", total_market_value=123_456.0, positions=positions
    )


def _analytics(available: bool = True) -> PortfolioAnalytics:
    if not available:
        return PortfolioAnalytics(available=False, api_url="x", errors={"performance": "down"})
    perf = PerformanceSeries(
        start_date="2025-06-11",
        end_date="2026-06-11",
        base_value=100_000.0,
        net_external_cashflow_in=0.0,
        backfill_start_unreliable=False,
        points=[
            PerformancePoint(
                date="2026-06-11",
                portfolio_return_pct=24.1,
                spy_return_pct=18.0,
                qqq_return_pct=21.3,
                policy_return_pct=None,
            )
        ],
    )
    alpha = PositionAlpha(
        start_date="2025-06-11",
        end_date="2026-06-11",
        has_policy=False,
        total_actual_pl=None,
        total_spy_pl=None,
        total_alpha=None,
        total_alpha_vs_qqq=None,
        total_alpha_vs_policy=None,
        rows=[
            PositionAlphaRow(
                ticker="NU",
                name="Nu",
                value_at_start=None,
                bought_in_window=None,
                sold_in_window=None,
                value_at_end=None,
                actual_pl=None,
                spy_counterfactual_pl=None,
                alpha=4_200.0,
                alpha_vs_qqq=None,
                alpha_vs_policy=None,
                incomplete=False,
            ),
            PositionAlphaRow(
                ticker="MELI",
                name="Meli",
                value_at_start=None,
                bought_in_window=None,
                sold_in_window=None,
                value_at_end=None,
                actual_pl=None,
                spy_counterfactual_pl=None,
                alpha=-1_100.0,
                alpha_vs_qqq=None,
                alpha_vs_policy=None,
                incomplete=False,
            ),
        ],
    )
    positioning = Positioning(
        snapshot_date="2026-06-11",
        total_value=123_456.0,
        concentration=Concentration(
            num_positions=12,
            top1_weight_pct=18.2,
            top5_weight_pct=55.0,
            top10_weight_pct=85.0,
            hhi=1100.0,
            effective_holdings=9.3,
        ),
        weighted_avg_correlation_spy=None,
    )
    return PortfolioAnalytics(
        available=True,
        api_url="x",
        performance=perf,
        position_alpha=alpha,
        positioning=positioning,
    )


def _fake_live(**_k: object) -> LivePortfolio:
    return _live()


def _fake_live_offline(**_k: object) -> LivePortfolio:
    return _live(available=False)


def _fake_analytics(**_k: object) -> PortfolioAnalytics:
    return _analytics()


def _fake_analytics_offline(**_k: object) -> PortfolioAnalytics:
    return _analytics(available=False)


def _patch_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packs, "fetch_live_portfolio", _fake_live)
    monkeypatch.setattr(packs, "fetch_portfolio_analytics", _fake_analytics)


def _one(
    db: Path, key: str, focus: list[str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, object] | None:
    _patch_tracker(monkeypatch)
    items = load_packs([key], db_path=db, focus_tickers=focus)
    assert len(items) <= 1
    return items[0] if items else None


# ---------------------------------------------------------------------------


def test_holdings_focus_lines_and_explicit_absence(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _one(db, "holdings", ["NU", "TSM"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert "total $123.5K" in text
    assert "NU 18.2%" in text
    assert "cost $15.0K" in text
    assert "P&L +$7.5K" in text
    assert "TSM: no current position" in text
    assert item["kind"] == "holdings"
    assert item["href"] == "/#portfolio"
    assert item["doc_id"] is None


def test_holdings_portfolio_wide_tops_by_value(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _one(db, "holdings", [], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert text.index("NU ") < text.index("MELI ")  # sorted by market value
    assert "P&L -$1.0K" in text  # negative P&L keeps its sign


def test_holdings_offline_is_explicit_not_absent(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packs, "fetch_live_portfolio", _fake_live_offline)
    items = load_packs(["holdings"], db_path=db, focus_tickers=["NU"])
    assert len(items) == 1
    assert "tracker offline" in str(items[0]["text"])
    assert "(offline)" in str(items[0]["label"])


def test_conviction_latest_per_kind_wins(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _one(db, "conviction", ["NU", "MELI", "TSM"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert "NU: conviction 4/5 (since 2026-05-20) · target 12%" in text
    assert "older row" not in text
    assert '"compounder, hold through vol"' in text
    assert "MELI: target 8%" in text
    assert "TSM: no stated conviction or target weight" in text


def test_conviction_empty_store_contributes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    assert load_packs(["conviction"], db_path=empty, focus_tickers=["NU"]) == []
    # Missing DB entirely → same answer, no raise.
    assert load_packs(["conviction"], db_path=tmp_path / "nope.db", focus_tickers=[]) == []


def test_dcf_upside_recomputed_and_absence_explicit(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _one(db, "dcf", ["NU", "V"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    # (13.96 / 12.10 - 1) = +15%; the latest run wins over January's.
    assert "NU: fair $13.96 vs live $12.10 → +15% upside (run 2026-06-08)" in text
    assert "V: no DCF run on file" in text


def test_dcf_portfolio_wide_excludes_non_portfolio_and_flags_suspect(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _one(db, "dcf", [], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert "NU" in text
    assert "MELI" in text
    assert "STNE" not in text  # not on the portfolio list
    # Focused on a suspect name, the implausible-upside flag rides along.
    focused = _one(db, "dcf", ["STNE"], monkeypatch)
    assert focused is not None
    assert "suspect" in str(focused["text"])


def test_decisions_ledger_lines(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _one(db, "decisions", ["NU", "MELI"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert "2026-05-02 NU trim 20% (conviction high) — acted: followed; outcome: pending" in text
    assert "2026-03-15 MELI add 15% (conviction medium) — acted: no action recorded" in text
    assert "outcome: correct (+18%)" in text
    # Focus filter: a ticker with no rows yields nothing extra, and a
    # focus excluding both rows drops the pack.
    assert load_packs(["decisions"], db_path=db, focus_tickers=["TSM"]) == []


# ---------------------------------------------------------------------------
# calibration — the analyst's own track record (decision_calibration)
# ---------------------------------------------------------------------------


def _calibration_db(tmp_path: Path, *, decisions: list[tuple[object, ...]]) -> Path:
    """A minimal DB carrying only the decisions a calibration test needs."""
    path = tmp_path / "calib.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, made_at, "
        "user_action_kind, outcome_label, outcome_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        decisions,
    )
    conn.commit()
    conn.close()
    return path


def test_calibration_overall_by_conviction_and_focus_cohort(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _one(db, "calibration", ["NU"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    # Fixture: 2 decisions, 1 graded (MELI add medium → correct). The sole
    # graded call is n=1, so every rate is honestly hedged, never asserted bare.
    assert "overall 100% correct" in text
    assert "low confidence" in text  # n=1 < the min-n floor
    assert "medium-conviction 100% correct" in text
    # NU's pending high-conviction trim is the live call; high has no graded
    # history, so the cohort line says so honestly instead of inventing a rate.
    assert "NU carries a pending high-conviction trim call" in text
    assert "no graded high-conviction calls yet" in text
    assert item["kind"] == "calibration"
    assert item["href"] == "/#decisions_record"


def test_calibration_focus_cohort_reports_graded_hit_rate(tmp_path: Path) -> None:
    db = _calibration_db(
        tmp_path,
        decisions=[
            # 10 graded high-conviction calls: 4 correct → 40% (n=10, at the floor).
            *[
                (
                    "AAA",
                    "add",
                    "high",
                    "2025-01-01T00:00:00",
                    "followed",
                    "correct" if k < 4 else "wrong",
                    "2025-06-01T00:00:00",
                )
                for k in range(10)
            ],
            # A live (pending) high-conviction call on NU to confront.
            ("NU", "trim", "high", "2026-05-01T00:00:00", None, None, None),
        ],
    )
    items = load_packs(["calibration"], db_path=db, focus_tickers=["NU"])
    assert len(items) == 1
    text = str(items[0]["text"])
    assert "high-conviction 40% correct (n=10)" in text  # at the floor → no hedge
    assert "low confidence" not in text
    # The keystone callout: the live call weighed against its own cohort.
    assert "NU carries a pending high-conviction trim call" in text
    assert "your high-conviction calls 40% correct (n=10)" in text
    assert "median days to outcome" in text  # outcome_at present → timing computed


def test_calibration_focus_uses_stated_conviction_when_no_pending_decision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calib2.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    # One graded call so the ledger isn't empty (the pack materializes).
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, conviction, made_at, "
        "outcome_label, outcome_at) VALUES ('XYZ', 'add', 'medium', "
        "'2025-01-01T00:00:00', 'correct', '2025-06-01T00:00:00')"
    )
    # ZZZ has a stated 5/5 conviction but no decision row — the fallback path.
    conn.execute(
        "INSERT INTO position_sizing_intent (user_id, ticker, intent_kind, intent_value, "
        "created_at, updated_at) VALUES (?, 'ZZZ', 'conviction', 5.0, "
        "'2026-05-01T00:00:00', '2026-05-01T00:00:00')",
        (DEFAULT_USER_ID,),
    )
    conn.commit()
    conn.close()
    items = load_packs(["calibration"], db_path=path, focus_tickers=["ZZZ"])
    assert len(items) == 1
    text = str(items[0]["text"])
    # 5/5 maps to the high cohort; no graded high calls → honest n=0, no invented rate.
    assert "ZZZ carries a stated conviction of 5/5 (high)" in text
    assert "no graded high-conviction calls yet" in text


def test_calibration_empty_ledger_contributes_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    assert load_packs(["calibration"], db_path=empty, focus_tickers=["NU"]) == []
    # Missing DB entirely → same answer, no raise.
    assert load_packs(["calibration"], db_path=tmp_path / "nope.db", focus_tickers=[]) == []


def test_journal_open_only_with_portfolio_level_notes(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _one(db, "journal", ["NU"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert "[NU · watch] NPL formation above 8%" in text
    assert "[portfolio · question] Is the book too LatAm-heavy?" in text
    assert "stale archived note" not in text
    assert "TSM note outside focus" not in text
    # Unfocused, the TSM note appears too.
    wide = _one(db, "journal", [], monkeypatch)
    assert wide is not None
    assert "TSM note outside focus" in str(wide["text"])


def test_performance_twr_alpha_concentration(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _one(db, "performance", ["NU"], monkeypatch)
    assert item is not None
    text = str(item["text"])
    assert (
        "Window TWR 2025-06-11 → 2026-06-11: portfolio +24.1% vs SPY +18.0% vs QQQ +21.3%" in text
    )
    assert "NU alpha +$4.2K" in text
    assert "MELI" not in text  # focus filters the alpha rows
    assert "top1 18.2%" in text
    assert "effective holdings 9.3" in text


def test_performance_offline_is_explicit(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packs, "fetch_portfolio_analytics", _fake_analytics_offline)
    items = load_packs(["performance"], db_path=db, focus_tickers=[])
    assert len(items) == 1
    assert "tracker offline" in str(items[0]["text"])


def test_load_packs_canonical_order_and_unknown_keys(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tracker(monkeypatch)
    items = load_packs(
        ["performance", "weather", "holdings", "dcf"], db_path=db, focus_tickers=["NU"]
    )
    assert [i["kind"] for i in items] == ["holdings", "dcf", "performance"]  # PACKS order
    assert all(i["label"] for i in items)


def test_one_failing_loader_drops_only_its_pack(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> LivePortfolio:
        raise RuntimeError("loader bug")

    monkeypatch.setattr(packs, "fetch_live_portfolio", _boom)
    monkeypatch.setattr(packs, "fetch_portfolio_analytics", _fake_analytics)
    items = load_packs(["holdings", "dcf"], db_path=db, focus_tickers=["NU"])
    assert [i["kind"] for i in items] == ["dcf"]


def test_char_budgets_hold_with_announced_truncation(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = sqlite3.connect(str(db))
    rows = [
        (
            DEFAULT_USER_ID,
            f"T{i:03d}",
            "conviction",
            3.0,
            "x" * 70,
            f"2026-05-{(i % 28) + 1:02d}T00:00:00",
        )
        for i in range(40)
    ]
    conn.executemany(
        "INSERT INTO position_sizing_intent "
        "(user_id, ticker, intent_kind, intent_value, narrative, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(*r, r[-1]) for r in rows],
    )
    conn.commit()
    conn.close()
    item = _one(db, "conviction", [], monkeypatch)
    assert item is not None
    spec = next(p for p in PACKS if p.key == "conviction")
    text = str(item["text"])
    assert len(text) <= spec.char_budget + 120  # header + the "(+N more)" marker
    assert "more)" in text  # truncation announced, never silent


def test_every_pack_key_loads(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key in PACK_KEYS has a working loader: selecting all of them on
    a fully-populated fixture yields one item per pack, in canonical order."""
    _patch_tracker(monkeypatch)
    items = load_packs(list(PACK_KEYS), db_path=db, focus_tickers=["NU"])
    assert [i["kind"] for i in items] == list(PACK_KEYS)
