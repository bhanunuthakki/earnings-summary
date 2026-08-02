"""Scenario-tail stress — the all-bears book drawdown from dcf_runs, and the
Risk-tab section markup."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from integrations.portfolio_tracker_client import PortfolioAnalytics
from pipeline.portfolio_panel import compose_risk_page
from portfolio_tail_stress import (
    FV_STALE_DAYS,
    PRICE_STALE_DAYS,
    TailStress,
    TailStressRow,
    build_tail_stress,
)

TODAY = date(2026, 7, 2)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, valuation_date TEXT, "
        "npv_per_share REAL, live_price REAL, live_price_at TEXT, "
        "assumption_snapshot_json TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _snapshot(bear: float | None = None, base: float | None = None) -> str | None:
    if bear is None and base is None:
        return None
    scenarios: dict[str, object] = {}
    if bear is not None:
        scenarios["bear"] = {"fair_value_per_share_usd": bear}
    if base is not None:
        scenarios["base"] = {"fair_value_per_share_usd": base}
    return json.dumps({"scenarios": scenarios})


def _insert_run(
    db_path: Path,
    ticker: str,
    *,
    created_at: str = "2026-06-01T00:00:00",
    valuation_date: str | None = "2026-06-15",
    live_price: float | None = 100.0,
    live_price_at: str | None = "2026-07-01",
    bear_fv: float | None = 60.0,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs "
        "(ticker, created_at, valuation_date, npv_per_share, live_price, live_price_at, "
        "assumption_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ticker,
            created_at,
            valuation_date,
            120.0,
            live_price,
            live_price_at,
            _snapshot(bear=bear_fv, base=120.0),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# build_tail_stress
# ---------------------------------------------------------------------------


def test_no_weights_returns_none(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    assert build_tail_stress(db_path, {}) is None
    assert build_tail_stress(db_path, {"AAA": 0.0}) is None  # non-positive excluded


def test_single_name_bear_contribution(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_run(db_path, "AAA", live_price=100.0, bear_fv=60.0)
    stress = build_tail_stress(db_path, {"AAA": 0.5}, today=TODAY)
    assert stress is not None
    assert stress.names_with_bear == 1
    assert stress.names_total == 1
    (row,) = stress.rows
    assert row.ticker == "AAA"
    assert row.bear_return_pct is not None and abs(row.bear_return_pct - (-40.0)) < 1e-9
    assert row.contribution_pct is not None and abs(row.contribution_pct - (-20.0)) < 1e-9
    assert abs(stress.book_drawdown_pct - (-20.0)) < 1e-9
    assert abs(stress.modeled_weight_pct - 50.0) < 1e-9
    assert row.low_confidence is False


def test_book_drawdown_sums_weighted_contributions(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_run(db_path, "AAA", live_price=100.0, bear_fv=50.0)  # -50%
    _insert_run(db_path, "BBB", live_price=100.0, bear_fv=90.0)  # -10%
    stress = build_tail_stress(db_path, {"AAA": 0.3, "BBB": 0.7}, today=TODAY)
    assert stress is not None
    expected = 0.3 * -50.0 + 0.7 * -10.0
    assert abs(stress.book_drawdown_pct - expected) < 1e-9
    # Worst contribution first.
    assert stress.rows[0].ticker == "AAA"


def test_missing_dcf_and_missing_bear_are_excluded_with_reasons(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_run(db_path, "AAA", live_price=100.0, bear_fv=60.0)
    _insert_run(db_path, "BBB", live_price=100.0, bear_fv=None)  # no bear leg
    stress = build_tail_stress(db_path, {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}, today=TODAY)
    assert stress is not None
    assert stress.names_with_bear == 1
    assert stress.names_total == 3
    reasons = {r.ticker: r.excluded_reason for r in stress.rows if r.excluded_reason is not None}
    assert reasons["CCC"] == "no DCF run on file"
    assert reasons["BBB"] == "no bear scenario persisted"
    assert any("CCC" in n and "BBB" in n for n in stress.notes)


def _make_versioned_db(tmp_path: Path) -> Path:
    """The versioned/sanity-gated dcf_runs shape (migration 0137+/0182) —
    ``_make_db``'s schema carries none of is_latest/segment_name/sanity_flag."""
    db_path = tmp_path / "versioned.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, valuation_date TEXT, "
        "npv_per_share REAL, live_price REAL, live_price_at TEXT, "
        "assumption_snapshot_json TEXT, is_latest INTEGER, segment_name TEXT, sanity_flag TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_versioned_run(
    db_path: Path,
    ticker: str,
    *,
    created_at: str,
    live_price: float | None = 100.0,
    bear_fv: float | None = 60.0,
    is_latest: int = 1,
    segment_name: str | None = None,
    sanity_flag: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs "
        "(ticker, created_at, valuation_date, npv_per_share, live_price, live_price_at, "
        "assumption_snapshot_json, is_latest, segment_name, sanity_flag) "
        "VALUES (?, ?, '2026-06-15', 120.0, ?, '2026-07-01', ?, ?, ?, ?)",
        (
            ticker,
            created_at,
            live_price,
            _snapshot(bear=bear_fv, base=120.0),
            is_latest,
            segment_name,
            sanity_flag,
        ),
    )
    conn.commit()
    conn.close()


def test_segment_row_never_wins_the_bear_leg_even_when_newer(tmp_path: Path) -> None:
    """PART A: a segment row landed AFTER the consolidated row must not win
    the bear leg — the shared dcf.latest reader fix."""
    db_path = _make_versioned_db(tmp_path)
    _insert_versioned_run(db_path, "AAA", created_at="2026-06-01T00:00:00", bear_fv=60.0)
    _insert_versioned_run(
        db_path,
        "AAA",
        created_at="2026-06-02T00:00:00",  # newer than the consolidated row
        segment_name="Consumer",
        bear_fv=999.0,
    )
    stress = build_tail_stress(db_path, {"AAA": 1.0}, today=TODAY)
    assert stress is not None
    (row,) = stress.rows
    assert row.bear_fv == 60.0  # the consolidated row's bear leg, not the segment one


def test_sanity_flagged_row_keeps_price_drops_bear_leg(tmp_path: Path) -> None:
    """Caller policy (ratified): skip the fair-value (bear) leg for a
    sanity-flagged run but keep the price-only leg — the row is excluded from
    the drawdown (no bear signal to stress against) with a reason naming WHY,
    not folded silently into 'no bear scenario persisted'."""
    db_path = _make_versioned_db(tmp_path)
    _insert_versioned_run(
        db_path,
        "AAA",
        created_at="2026-06-01T00:00:00",
        live_price=100.0,
        bear_fv=60.0,
        sanity_flag="outlier",
    )
    stress = build_tail_stress(db_path, {"AAA": 1.0}, today=TODAY)
    assert stress is not None
    (row,) = stress.rows
    assert row.live_price == 100.0  # price-only leg survives
    assert row.bear_fv is None  # fair-value leg dropped
    assert row.contribution_pct is None
    assert row.excluded_reason == "DCF sanity-flagged (outlier) — bear leg excluded"


def test_stale_fair_value_and_price_flag_low_confidence(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    stale_val = (TODAY - timedelta(days=FV_STALE_DAYS + 5)).isoformat()
    stale_price = (TODAY - timedelta(days=PRICE_STALE_DAYS + 3)).isoformat()
    _insert_run(
        db_path,
        "AAA",
        live_price=100.0,
        bear_fv=60.0,
        valuation_date=stale_val,
        live_price_at=stale_price,
    )
    stress = build_tail_stress(db_path, {"AAA": 1.0}, today=TODAY)
    assert stress is not None
    (row,) = stress.rows
    assert row.low_confidence is True
    assert row.confidence_reason is not None
    assert "fair value" in row.confidence_reason and "price" in row.confidence_reason
    assert stress.stale_weight_pct == pytest.approx(100.0)
    # Still counted in the drawdown — low-confidence flags, doesn't exclude.
    assert row.contribution_pct is not None


def test_zero_or_negative_live_price_excluded(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _insert_run(db_path, "AAA", live_price=0.0, bear_fv=60.0)
    stress = build_tail_stress(db_path, {"AAA": 1.0}, today=TODAY)
    assert stress is not None
    (row,) = stress.rows
    assert row.excluded_reason == "DCF has no usable live price"


def test_missing_db_degrades_to_all_excluded(tmp_path: Path) -> None:
    stress = build_tail_stress(tmp_path / "nope.db", {"AAA": 1.0}, today=TODAY)
    assert stress is not None
    assert stress.names_with_bear == 0
    assert stress.book_drawdown_pct == 0.0
    (row,) = stress.rows
    assert row.excluded_reason == "no DCF run on file"


# ---------------------------------------------------------------------------
# Risk-tab section markup
# ---------------------------------------------------------------------------


def _stress() -> TailStress:
    return TailStress(
        rows=[
            TailStressRow(
                ticker="MELI",
                weight_pct=20.0,
                live_price=2000.0,
                bear_fv=1200.0,
                bear_return_pct=-40.0,
                contribution_pct=-8.0,
            ),
            TailStressRow(
                ticker="FLKR",
                weight_pct=5.0,
                live_price=None,
                bear_fv=None,
                bear_return_pct=None,
                contribution_pct=None,
                excluded_reason="no DCF run on file",
            ),
        ],
        book_drawdown_pct=-8.0,
        modeled_weight_pct=20.0,
        stale_weight_pct=0.0,
        names_with_bear=1,
        names_total=2,
        notes=["outside the stress (no bear leg): FLKR (no DCF run on file)"],
    )


def _page(analytics: PortfolioAnalytics, stress: TailStress | None) -> str:
    return compose_risk_page(
        analytics, drawdown=None, factor=None, scenarios=[], digest="", tail_stress=stress
    )


def test_tail_stress_section_renders_rows_and_headline() -> None:
    # 20% modeled is well below COVERAGE_WARN_PCT — the coverage gate takes over
    # the sub-line (Monthly Red Team Phase 1 guard 1), so the plain "% of book
    # modeled" reading no longer appears; see test_low_coverage_* below.
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), _stress())
    assert "Scenario-tail stress" in html
    assert "-8.0%" in html  # headline book drawdown
    assert "1 of 2" in html
    assert "MELI" in html and "-40%" in html and "-8.0pp" in html
    assert "FLKR" in html and "not modeled" in html and "no DCF run on file" in html


def test_tail_stress_section_empty_state() -> None:
    html = _page(PortfolioAnalytics(available=True, api_url="http://x"), None)
    assert "Scenario-tail stress" in html
    assert "No weighted holdings to stress" in html


def test_tail_stress_section_renders_offline() -> None:
    offline = PortfolioAnalytics(
        available=False, api_url="http://x", errors={"performance": "refused"}
    )
    html = _page(offline, _stress())
    assert "live portfolio tracker" in html
    assert "Scenario-tail stress" in html and "-8.0%" in html


# ---------------------------------------------------------------------------
# Coverage gate (Monthly Red Team Phase 1 guard 1) — the 2026-07 adversarial
# review's #1 failure mode: tail stress modeled 42% of the book and still
# rendered a clean, healthy-looking headline.
# ---------------------------------------------------------------------------


def _stress_with_coverage(modeled_pct: float) -> TailStress:
    s = _stress()
    return TailStress(
        rows=s.rows,
        book_drawdown_pct=s.book_drawdown_pct,
        modeled_weight_pct=modeled_pct,
        stale_weight_pct=s.stale_weight_pct,
        names_with_bear=s.names_with_bear,
        names_total=s.names_total,
        notes=s.notes,
    )


def test_low_coverage_leads_with_bad_pill_and_subordinates_headline() -> None:
    # 20% modeled < COVERAGE_BAD_PCT (60%) -> the red pill, and the headline
    # number's tone is stripped (never a clean green/red as if confident).
    html = _page(
        PortfolioAnalytics(available=True, api_url="http://x"), _stress_with_coverage(20.0)
    )
    assert "80% OF BOOK UNMODELED" in html
    assert "k-pill-bad" in html
    assert "over the 20% modeled" in html
    assert "NOT the whole book" in html
    assert "20% of book modeled" not in html  # the plain healthy sub-line is gone
    # The drawdown card itself must not carry a pos/neg tone class — never a
    # clean healthy-looking number when majority-unmodeled.
    assert '<div class="kpi-card"><div class="kpi-label">All-bears book drawdown</div>' in html


def test_mid_coverage_leads_with_warn_pill() -> None:
    # 75% modeled is between COVERAGE_BAD_PCT and COVERAGE_WARN_PCT -> amber.
    html = _page(
        PortfolioAnalytics(available=True, api_url="http://x"), _stress_with_coverage(75.0)
    )
    assert "25% OF BOOK UNMODELED" in html
    assert "k-pill-warn" in html
    assert "k-pill-bad" not in html


def test_high_coverage_has_no_warning_pill() -> None:
    # 95% modeled clears COVERAGE_WARN_PCT (90%) -> no coverage-warning pill,
    # the plain "% of book modeled" sub-line stays, and the headline keeps its
    # normal pos/neg tone.
    html = _page(
        PortfolioAnalytics(available=True, api_url="http://x"), _stress_with_coverage(95.0)
    )
    assert "OF BOOK UNMODELED" not in html
    assert "95% of book modeled" in html
