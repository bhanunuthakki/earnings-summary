"""Tests for ``filings.event_study`` (P5 — the disclosure-events event study).

Weighted toward the failure modes that would silently corrupt the study
(docs/design/disclosure_change_build_stack.md §P5 and the P5 task's honesty
bar):

  * item-level events resolve their arrival date via ``source_ref`` ->
    ``filing_sections.filing_date`` — never ``period_end``.
  * metric-lifecycle events resolve to the NEXT filing after the last
    reported period, not that period's own (stale) date; quarterly-axis
    metric events must be explicitly SKIPPED (with a reason), never
    approximated.
  * multiple ``disclosure_events`` rows sharing one filing collapse into ONE
    event-study observation — the pseudo-replication fix is the single most
    important correctness property in this module.
  * window returns are benchmark-adjusted correctly and degrade to ``None``
    (not a fabricated 0.0) when the price calendar does not cover the window.
  * small buckets are flagged ``underpowered`` rather than reported as a
    clean number.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.event_study import (  # noqa: E402
    EVENT_STUDY_WINDOWS,
    MIN_EVENTS_FOR_HEADLINE,
    RawDisclosureEvent,
    _bootstrap_ci,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    build_bucket_stats,
    collapse_events,
    compute_window_returns,
    fit_surprise_regressions,
    resolve_arrival_dates,
    run_event_study,
)

_SCHEMA = """
CREATE TABLE filing_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    filing_date VARCHAR(10)
);
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    source_ref VARCHAR(255),
    canonical_id VARCHAR(64) NOT NULL DEFAULT '',
    subject VARCHAR(255) NOT NULL,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified'
);
CREATE TABLE earnings_surprises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    release_date VARCHAR(10) NOT NULL,
    eps_surprise_pct NUMERIC(12,4)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _insert_section(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    source_ref: str,
    fiscal_year: int,
    fiscal_period: str = "FY",
    filing_date: str | None,
    source: str = "edgar_text",
) -> None:
    conn.execute(
        "INSERT INTO filing_sections (ticker, source, source_ref, form, fiscal_year, "
        "fiscal_period, filing_date) VALUES (?,?,?,?,?,?,?)",
        (ticker, source, source_ref, "10-K", fiscal_year, fiscal_period, filing_date),
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    event_type: str,
    fiscal_year: int | None,
    fiscal_period: str | None,
    source_ref: str | None = None,
    subject: str = "risk_factor_x",
    verdict: str = "unclassified",
) -> None:
    conn.execute(
        "INSERT INTO disclosure_events (ticker, event_type, fiscal_year, fiscal_period, "
        "source_ref, subject, verdict) VALUES (?,?,?,?,?,?,?)",
        (ticker, event_type, fiscal_year, fiscal_period, source_ref, subject, verdict),
    )


def _load_events(conn: sqlite3.Connection) -> list[RawDisclosureEvent]:
    rows = conn.execute(
        "SELECT id, ticker, event_type, verdict, fiscal_year, fiscal_period, source_ref, "
        "subject FROM disclosure_events ORDER BY id"
    ).fetchall()
    return [
        RawDisclosureEvent(
            id=r["id"],
            ticker=r["ticker"],
            event_type=r["event_type"],
            verdict=r["verdict"],
            fiscal_year=r["fiscal_year"],
            fiscal_period=r["fiscal_period"],
            source_ref=r["source_ref"],
            subject=r["subject"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Arrival-date resolution
# ---------------------------------------------------------------------------


def test_item_event_resolves_via_source_ref_join() -> None:
    conn = _conn()
    _insert_section(
        conn, ticker="MELI", source_ref="0001-24-000001", fiscal_year=2023, filing_date="2024-02-23"
    )
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_added",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="0001-24-000001",
    )
    events = _load_events(conn)
    resolved, skipped = resolve_arrival_dates(conn, events)
    assert not skipped
    assert resolved[events[0].id] == date(2024, 2, 23)


def test_item_event_unresolved_when_no_matching_filing_section() -> None:
    conn = _conn()
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_removed",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="not-on-file",
    )
    events = _load_events(conn)
    resolved, skipped = resolve_arrival_dates(conn, events)
    assert not resolved
    assert len(skipped) == 1
    assert skipped[0].reason == "arrival_unresolved:no_filing_sections_match"


def test_metric_event_annual_axis_resolves_to_next_10k_filing_date() -> None:
    conn = _conn()
    _insert_section(
        conn, ticker="META", source_ref="acc-2023", fiscal_year=2023, filing_date="2024-01-30"
    )
    _insert_section(
        conn, ticker="META", source_ref="acc-2024", fiscal_year=2024, filing_date="2025-01-30"
    )
    # Last observed the metric in FY2023 -> arrival is the FY2024 10-K's filing date,
    # NEVER the FY2023 filing date (that would be period_end-flavored look-ahead bias).
    _insert_event(
        conn,
        ticker="META",
        event_type="metric_discontinued",
        fiscal_year=2023,
        fiscal_period="FY",
    )
    events = _load_events(conn)
    resolved, skipped = resolve_arrival_dates(conn, events)
    assert not skipped
    assert resolved[events[0].id] == date(2025, 1, 30)


def test_metric_event_quarterly_axis_is_explicitly_skipped_not_approximated() -> None:
    conn = _conn()
    _insert_event(
        conn,
        ticker="NU",
        event_type="metric_discontinued",
        fiscal_year=2022,
        fiscal_period="Q2",
    )
    events = _load_events(conn)
    resolved, skipped = resolve_arrival_dates(conn, events)
    assert not resolved
    assert skipped[0].reason == "arrival_unresolved:quarterly_axis_no_data"


def test_metric_event_with_no_subsequent_10k_is_skipped() -> None:
    conn = _conn()
    _insert_section(
        conn, ticker="WIX", source_ref="acc-1", fiscal_year=2024, filing_date="2025-03-05"
    )
    _insert_event(
        conn,
        ticker="WIX",
        event_type="metric_relabeled",
        fiscal_year=2024,
        fiscal_period="FY",
    )
    events = _load_events(conn)
    resolved, skipped = resolve_arrival_dates(conn, events)
    assert not resolved
    assert skipped[0].reason == "arrival_unresolved:no_subsequent_10k_on_file"


# ---------------------------------------------------------------------------
# Pseudo-replication collapse
# ---------------------------------------------------------------------------


def test_collapse_events_merges_same_filing_same_verdict() -> None:
    conn = _conn()
    _insert_section(
        conn, ticker="MELI", source_ref="acc-1", fiscal_year=2023, filing_date="2024-02-23"
    )
    for i in range(5):
        _insert_event(
            conn,
            ticker="MELI",
            event_type="item_reworded",
            fiscal_year=2023,
            fiscal_period="FY",
            source_ref="acc-1",
            subject=f"risk_{i}",
        )
    events = _load_events(conn)
    resolved, _skipped = resolve_arrival_dates(conn, events)
    observations = collapse_events(events, resolved)
    # 5 disclosure_events rows on the SAME filing must become ONE observation —
    # they share an identical forward return and are not 5 independent draws.
    assert len(observations) == 1
    assert observations[0].n_underlying_events == 5


def test_collapse_events_keeps_different_verdicts_separate() -> None:
    conn = _conn()
    _insert_section(
        conn, ticker="MELI", source_ref="acc-1", fiscal_year=2023, filing_date="2024-02-23"
    )
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_removed",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="acc-1",
        verdict="boilerplate_update",
    )
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_removed",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="acc-1",
        verdict="substantive",
    )
    events = _load_events(conn)
    resolved, _ = resolve_arrival_dates(conn, events)
    observations = collapse_events(events, resolved)
    assert len(observations) == 2
    assert {o.verdict for o in observations} == {"boilerplate_update", "substantive"}


def test_collapse_events_drops_unresolved_arrivals() -> None:
    conn = _conn()
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_added",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="missing",
    )
    events = _load_events(conn)
    resolved, _ = resolve_arrival_dates(conn, events)
    observations = collapse_events(events, resolved)
    assert observations == []


# ---------------------------------------------------------------------------
# Window returns (price calendar arithmetic)
# ---------------------------------------------------------------------------


def _write_price_chart(repo_root: Path, ticker: str, closes: list[tuple[str, float]]) -> None:
    fmp = repo_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rows = [{"date": d, "adjClose": v, "close": v} for d, v in closes]
    (fmp / f"{ticker}_price_chart_10y_div_adj.json").write_text(json.dumps(rows), encoding="utf-8")


def _trading_days(start: str, n: int) -> list[str]:
    from datetime import timedelta

    d = date.fromisoformat(start)
    out: list[str] = []
    weekend_start = 5  # Mon=0..Fri=4; a plain trading-calendar stub
    while len(out) < n:
        if d.weekday() < weekend_start:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_compute_window_returns_benchmark_adjusts_correctly(tmp_path: Path) -> None:
    days = _trading_days("2024-01-01", 40)
    # Ticker returns +1% every day; benchmark flat (0%) every day.
    ticker_closes = [(days[0], 100.0)]
    price = 100.0
    for d in days[1:]:
        price *= 1.01
        ticker_closes.append((d, price))
    bench_closes = [(d, 100.0) for d in days]

    _write_price_chart(tmp_path, "ZZZ", ticker_closes)
    _write_price_chart(tmp_path, "SPY", bench_closes)

    conn = _conn()
    from filings.event_study import EventObservation

    arrival = date.fromisoformat(days[10])
    obs = [
        EventObservation(
            ticker="ZZZ",
            arrival_date=arrival,
            event_type="item_added",
            verdict="unclassified",
            n_underlying_events=1,
            subjects=["x"],
        )
    ]
    returns = compute_window_returns(conn, obs, tmp_path, benchmark_ticker="SPY")
    assert len(returns) == 1
    by_window = {wr.window: wr for wr in returns[0]}
    announce = by_window["announce_3d"]
    assert announce.raw_car is not None
    # Benchmark is flat, so adjusted == raw here.
    assert announce.adjusted_car == announce.raw_car
    assert announce.raw_car > 0  # ticker rose every day
    conn.close()


def test_compute_window_returns_none_when_window_does_not_fit(tmp_path: Path) -> None:
    # Only 5 trading days on file -- a 6-month drift window cannot fit.
    days = _trading_days("2024-06-01", 5)
    closes = [(d, 100.0 + i) for i, d in enumerate(days)]
    _write_price_chart(tmp_path, "ZZZ", closes)
    _write_price_chart(tmp_path, "SPY", closes)

    conn = _conn()
    from filings.event_study import EventObservation

    obs = [
        EventObservation(
            ticker="ZZZ",
            arrival_date=date.fromisoformat(days[2]),
            event_type="item_added",
            verdict="unclassified",
            n_underlying_events=1,
        )
    ]
    returns = compute_window_returns(conn, obs, tmp_path, benchmark_ticker="SPY")
    by_window = {wr.window: wr for wr in returns[0]}
    assert by_window["drift_6m"].raw_car is None
    assert by_window["drift_6m"].adjusted_car is None
    conn.close()


# ---------------------------------------------------------------------------
# Bootstrap CI + underpowered flagging
# ---------------------------------------------------------------------------


def test_bootstrap_ci_none_below_two_observations() -> None:
    assert _bootstrap_ci(np.array([0.01]), seed=1) is None
    assert _bootstrap_ci(np.array([]), seed=1) is None


def test_bootstrap_ci_contains_sample_mean_for_tight_distribution() -> None:
    values = np.array([0.01, 0.011, 0.0105, 0.0102, 0.0098, 0.0103] * 5)
    ci = _bootstrap_ci(values, seed=42)
    assert ci is not None
    low, high = ci
    assert low <= float(np.mean(values)) <= high


def test_bucket_stats_flags_underpowered_small_bucket() -> None:
    from filings.event_study import EventObservation, WindowReturn

    obs = [
        EventObservation(
            ticker="X",
            arrival_date=date(2024, 1, 1),
            event_type="item_added",
            verdict="unclassified",
            n_underlying_events=1,
        )
    ]
    returns_by_obs = [
        [
            WindowReturn(
                observation_index=0,
                window=w,
                raw_car=0.01,
                adjusted_car=0.01,
                eps_surprise_pct=None,
            )
            for w in EVENT_STUDY_WINDOWS
        ]
    ]
    stats = build_bucket_stats(obs, returns_by_obs, residuals={})
    all_bucket = next(s for s in stats if s.bucket == "ALL" and s.window == "announce_3d")
    assert all_bucket.n == 1
    assert all_bucket.n < MIN_EVENTS_FOR_HEADLINE
    assert all_bucket.underpowered is True


def test_bucket_stats_n_unique_ticker_dates_deduplicates_cross_type_pooling() -> None:
    """One filing producing an item_added AND an item_removed event on the
    SAME (ticker, arrival_date) shares one identical price draw. The
    ``ALL``/``event_type``-pooled cuts must report that as n=2 rows but
    n_unique_ticker_dates=1 — the true independent-draw count — and gate
    ``underpowered`` on the latter, not the former.
    """
    from filings.event_study import EventObservation, WindowReturn

    same_day = date(2024, 1, 1)
    obs = [
        EventObservation(
            ticker="X",
            arrival_date=same_day,
            event_type="item_added",
            verdict="unclassified",
            n_underlying_events=1,
        ),
        EventObservation(
            ticker="X",
            arrival_date=same_day,
            event_type="item_removed",
            verdict="unclassified",
            n_underlying_events=1,
        ),
    ]
    returns_by_obs = [
        [
            WindowReturn(
                observation_index=i,
                window=w,
                raw_car=0.01,
                adjusted_car=0.01,
                eps_surprise_pct=None,
            )
            for w in EVENT_STUDY_WINDOWS
        ]
        for i in range(2)
    ]
    stats = build_bucket_stats(obs, returns_by_obs, residuals={})
    all_bucket = next(s for s in stats if s.bucket == "ALL" and s.window == "announce_3d")
    assert all_bucket.n == 2
    assert all_bucket.n_unique_ticker_dates == 1
    assert all_bucket.underpowered is True  # 1 independent draw, well below the floor

    # The finest (event_type, verdict) cross never pools across the boundary,
    # so n and n_unique_ticker_dates agree there.
    item_added_bucket = next(
        s
        for s in stats
        if s.bucket == "event_type=item_added|verdict=unclassified" and s.window == "announce_3d"
    )
    assert item_added_bucket.n == 1
    assert item_added_bucket.n_unique_ticker_dates == 1


# ---------------------------------------------------------------------------
# Surprise regression
# ---------------------------------------------------------------------------


def test_surprise_regression_skipped_below_minimum_observations() -> None:
    from filings.event_study import WindowReturn

    returns_by_obs = [
        [
            WindowReturn(
                observation_index=0,
                window="announce_3d",
                raw_car=0.01,
                adjusted_car=0.01,
                eps_surprise_pct=2.0,
            )
        ]
    ]
    regressions, residuals = fit_surprise_regressions(returns_by_obs)
    announce = next(r for r in regressions if r.window == "announce_3d")
    assert announce.slope is None
    assert announce.fit_skipped_reason is not None
    assert residuals == {}


def test_surprise_regression_fits_and_residualizes_with_enough_data() -> None:
    from filings.event_study import (
        _MIN_REGRESSION_OBS,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
        WindowReturn,
    )

    rng = np.random.default_rng(0)
    n = _MIN_REGRESSION_OBS + 5
    surprises = rng.normal(0, 5, n)
    # Perfect linear relationship: raw_car = 0.5% + 0.001 * surprise_pct.
    raw = 0.005 + 0.001 * surprises
    row = [
        WindowReturn(
            observation_index=i,
            window="announce_3d",
            raw_car=float(raw[i]),
            adjusted_car=float(raw[i]),
            eps_surprise_pct=float(surprises[i]),
        )
        for i in range(n)
    ]
    returns_by_obs = [[r] for r in row]
    regressions, residuals = fit_surprise_regressions(returns_by_obs)
    announce = next(r for r in regressions if r.window == "announce_3d")
    assert announce.slope is not None
    assert announce.intercept is not None
    assert abs(announce.slope - 0.001) < 1e-6
    assert abs(announce.intercept - 0.005) < 1e-6
    # A near-perfect fit leaves near-zero residuals.
    assert all(abs(v) < 1e-6 for v in residuals.values())


# ---------------------------------------------------------------------------
# End-to-end smoke test
# ---------------------------------------------------------------------------


def test_run_event_study_end_to_end(tmp_path: Path) -> None:
    days = _trading_days("2024-01-01", 200)
    ticker_closes = [(d, 100.0 + i * 0.1) for i, d in enumerate(days)]
    bench_closes = [(d, 100.0 + i * 0.05) for i, d in enumerate(days)]
    _write_price_chart(tmp_path, "MELI", ticker_closes)
    _write_price_chart(tmp_path, "SPY", bench_closes)

    conn = _conn()
    filing_date = days[50]
    _insert_section(
        conn, ticker="MELI", source_ref="acc-1", fiscal_year=2023, filing_date=filing_date
    )
    _insert_event(
        conn,
        ticker="MELI",
        event_type="item_added",
        fiscal_year=2023,
        fiscal_period="FY",
        source_ref="acc-1",
    )
    _insert_event(
        conn,
        ticker="MELI",
        event_type="metric_discontinued",
        fiscal_year=2020,
        fiscal_period="Q2",  # quarterly axis -> must be skipped, not approximated
    )

    report = run_event_study(conn, tmp_path, benchmark_ticker="SPY")
    assert report.n_events_total == 2
    assert report.n_observations_collapsed == 1
    assert report.n_events_skipped == 1
    assert report.skip_reasons.get("arrival_unresolved:quarterly_axis_no_data") == 1
    assert any(b.bucket == "ALL" for b in report.buckets)
    conn.close()
