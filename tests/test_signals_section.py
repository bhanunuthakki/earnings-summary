"""Tests for §3.5 Signals — accessor + builder + renderer fan-out.

Mirrors the fixture pattern in test_signal_writer.py: build a minimal
portfolio.db with the timeseries_signals table, seed rows spanning all
three severities, then assert that:

  - the builder buckets + orders rows correctly,
  - workspace / markdown renderers each emit the §3.5 region,
  - empty-table / missing-DB returns a non-rendering section.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import date
from io import StringIO
from pathlib import Path

from report.models import SectionStatus, SignalsSection
from report.renderers import markdown as markdown_renderer
from report.renderers import workspace_html
from report.renderers.workspace_styles import CSS as WORKSPACE_CSS
from report.sections import signals as signals_section

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_db(path: Path) -> None:
    """Create only the timeseries_signals table — that's the accessor's
    sole dependency, and keeping the schema inline keeps the test fast."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE timeseries_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR(16) NOT NULL,
                metric_name VARCHAR(128) NOT NULL,
                metric_kind VARCHAR(16) NOT NULL,
                signal_type VARCHAR(32) NOT NULL,
                value_json TEXT NOT NULL,
                severity VARCHAR(8) NOT NULL,
                narrative TEXT,
                computed_at DATETIME NOT NULL,
                run_id VARCHAR(64),
                CONSTRAINT uq_timeseries_signals_logical
                    UNIQUE (ticker, metric_name, metric_kind, signal_type),
                CHECK (metric_kind IN ('financial', 'kpi', 'segment')),
                CHECK (signal_type IN ('trend', 'inflection', 'anomaly',
                                      'yoy_acceleration', 'seasonal', 'correlation')),
                CHECK (severity IN ('green', 'yellow', 'red'))
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_rows(db: Path) -> None:
    """Seed 5 rows mixing severities and signal types so every code path
    in the magnitude / value-summary mappers gets exercised."""
    conn = sqlite3.connect(str(db))
    try:
        rows = [
            # Red anomaly: z=3.1 on the latest point.
            (
                "GOOG",
                "free_cash_flow",
                "financial",
                "anomaly",
                json.dumps(
                    {
                        "anomalies": [
                            {"period_end": "2025-12-31", "zscore": 1.6},
                            {"period_end": "2026-03-31", "zscore": 3.1},
                        ]
                    }
                ),
                "red",
                "Free Cash Flow: 2 anomalous quarter(s); most recent 2026-03-31 z=+3.10.",
            ),
            # Red trend: large slope, decelerating.
            (
                "GOOG",
                "operating_income",
                "financial",
                "trend",
                json.dumps(
                    {
                        "direction": "inflecting",
                        "slope_pct_of_mean": -0.14,
                        "statistical_significance": True,
                    }
                ),
                "red",
                "Operating Income: inflecting trend (slope -14.0% of mean, significant).",
            ),
            # Yellow yoy_acceleration: thesis-input deceleration.
            (
                "GOOG",
                "GCP revenue growth (YoY)",
                "kpi",
                "yoy_acceleration",
                json.dumps(
                    {
                        "most_recent_yoy": 0.18,
                        "most_recent_delta": -0.04,
                        "trend": "decelerating",
                    }
                ),
                "yellow",
                "GCP revenue growth (YoY): YoY +18.0% (delta -4.00% QoQ, decelerating).",
            ),
            # Green trend: gentle acceleration.
            (
                "GOOG",
                "revenue",
                "financial",
                "trend",
                json.dumps(
                    {
                        "direction": "accelerating",
                        "slope_pct_of_mean": 0.05,
                        "statistical_significance": True,
                    }
                ),
                "green",
                "Revenue: accelerating trend (slope +5.0% of mean, significant).",
            ),
            # Green seasonal: low strength.
            (
                "GOOG",
                "net_income",
                "financial",
                "seasonal",
                json.dumps({"n": 16, "period": 4, "method": "stl", "seasonal_strength": 0.25}),
                "green",
                "Net Income: seasonal_strength=0.25 (method=stl).",
            ),
        ]
        for ticker, metric, kind, sig_type, payload, sev, narrative in rows:
            conn.execute(
                "INSERT INTO timeseries_signals(ticker, metric_name, metric_kind, "
                "signal_type, value_json, severity, narrative, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    ticker,
                    metric,
                    kind,
                    sig_type,
                    payload,
                    sev,
                    narrative,
                    "2026-05-26 12:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _populated_repo(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "portfolio.db"
    _build_db(db_path)
    _seed_rows(db_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Accessor / builder
# ---------------------------------------------------------------------------


def test_build_signals_section_buckets_and_orders(tmp_path: Path) -> None:
    repo = _populated_repo(tmp_path)
    section = signals_section.build("GOOG", repo, as_of=date(2026, 8, 23))
    assert section.status == SectionStatus.OK
    assert len(section.red_signals) == 2
    assert len(section.yellow_signals) == 1
    assert len(section.green_signals) == 2

    # Anomaly z=3.1 has the larger magnitude than trend slope_pct_of_mean=-0.14
    # → it should come first among the reds. Slopes are fractions (0.14), while
    # anomaly magnitudes are absolute z-scores (3.1).
    red_first = section.red_signals[0]
    assert red_first.signal_type == "anomaly"
    assert red_first.metric_name == "free_cash_flow"
    assert red_first.value_summary is not None
    assert "z=" in red_first.value_summary

    yellow_first = section.yellow_signals[0]
    assert yellow_first.signal_type == "yoy_acceleration"
    assert yellow_first.metric_kind == "kpi"
    assert yellow_first.value_summary is not None
    assert "YoY=" in yellow_first.value_summary


def test_build_signal_summary_keeps_direction_separate_from_statistical_severity(
    tmp_path: Path,
) -> None:
    """A statistically unusual KPI is not automatically an investment negative.

    The persisted payload is the writer/report boundary: direction is derived
    from known KPI polarity, while missing polarity and stale source periods
    stay explicit instead of being guessed away by the renderer.
    """
    repo = _populated_repo(tmp_path)
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO timeseries_signals(ticker, metric_name, metric_kind, signal_type, "
            "value_json, severity, narrative, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "GOOG",
                "Monthly churn",
                "kpi",
                "trend",
                json.dumps(
                    {
                        "direction": "decelerating",
                        "slope_pct_of_mean": 0.12,
                        "statistical_significance": True,
                        "investment_direction": "unfavorable",
                        "polarity": "lower_is_better",
                        "source_period": "2026-03-31",
                        "is_thesis_kpi": True,
                    }
                ),
                "yellow",
                "Churn increased materially.",
                "2026-08-01 12:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO timeseries_signals(ticker, metric_name, metric_kind, signal_type, "
            "value_json, severity, narrative, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "GOOG",
                "Unknown operating metric",
                "kpi",
                "trend",
                json.dumps(
                    {
                        "direction": "decelerating",
                        "slope_pct_of_mean": -0.12,
                        "statistical_significance": False,
                        "investment_direction": "ambiguous",
                        "source_period": "2020-03-31",
                        "is_thesis_kpi": False,
                    }
                ),
                "red",
                "An old, non-significant movement.",
                "2020-04-01 12:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    section = signals_section.build("GOOG", repo, as_of=date(2026, 8, 23))
    churn = next(row for row in section.summary_signals if row.metric_name == "Monthly churn")
    assert churn.investment_direction == "unfavorable"
    assert churn.statistical_significance is True
    assert churn.is_thesis_kpi is True
    assert churn.freshness == "fresh"
    assert churn.rank == 1

    unknown = next(
        row for row in section.all_signals if row.metric_name == "Unknown operating metric"
    )
    assert unknown.investment_direction == "ambiguous"
    assert unknown.statistical_significance is False
    assert unknown.freshness == "stale"
    assert [row.metric_name for row in section.summary_signals] == ["Monthly churn"]


def test_summary_only_elevates_current_significant_decision_evidence(tmp_path: Path) -> None:
    repo = _populated_repo(tmp_path)
    db_path = repo / "data" / "portfolio.db"
    rows = [
        (
            "Monthly churn",
            "trend",
            "yellow",
            {
                "statistical_significance": True,
                "investment_direction": "unfavorable",
                "source_period": "2026-06-30",
                "is_thesis_kpi": True,
            },
        ),
        (
            "Custom KPI",
            "trend",
            "red",
            {
                "statistical_significance": True,
                "investment_direction": "ambiguous",
                "source_period": "2026-07-31",
                "is_thesis_kpi": False,
            },
        ),
        (
            "High quality revenue",
            "anomaly",
            "red",
            {
                "anomalies": [{"period_end": "2026-08-01", "zscore": 3.4}],
                "statistical_significance": True,
                "investment_direction": "favorable",
                "source_period": "2026-08-01",
                "is_thesis_kpi": True,
            },
        ),
        (
            "Old churn",
            "trend",
            "red",
            {
                "statistical_significance": True,
                "investment_direction": "unfavorable",
                "source_period": "2020-03-31",
                "is_thesis_kpi": True,
            },
        ),
        (
            "Noisy churn",
            "trend",
            "red",
            {
                "statistical_significance": False,
                "investment_direction": "unfavorable",
                "source_period": "2026-08-01",
                "is_thesis_kpi": True,
            },
        ),
    ]
    conn = sqlite3.connect(str(db_path))
    try:
        for metric, signal_type, severity, payload in rows:
            conn.execute(
                "INSERT INTO timeseries_signals(ticker, metric_name, metric_kind, signal_type, "
                "value_json, severity, narrative, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "GOOG",
                    metric,
                    "kpi",
                    signal_type,
                    json.dumps(payload),
                    severity,
                    f"{metric} test signal.",
                    "2026-08-01 12:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    section = signals_section.build("GOOG", repo)
    assert [row.metric_name for row in section.summary_signals] == [
        "Monthly churn",
        "Custom KPI",
        "High quality revenue",
    ]
    assert [row.rank for row in section.summary_signals] == [1, 2, 3]
    assert all(row.freshness == "fresh" for row in section.summary_signals)
    assert all(row.statistical_significance is True for row in section.summary_signals)
    assert {row.metric_name for row in section.all_signals} >= {"Old churn", "Noisy churn"}
    assert all(
        row.metric_name not in {"Old churn", "Noisy churn"} for row in section.summary_signals
    )


def test_ticker_case_insensitive(tmp_path: Path) -> None:
    """Accessor must upper-case the ticker before querying — every signal
    row in production lands with an upper-case ticker, and callers may
    pass mixed case."""
    repo = _populated_repo(tmp_path)
    upper = signals_section.build("GOOG", repo)
    lower = signals_section.build("goog", repo)
    assert len(upper.red_signals) + len(upper.yellow_signals) + len(upper.green_signals) == len(
        lower.red_signals
    ) + len(lower.yellow_signals) + len(lower.green_signals)


def test_missing_table_returns_empty_rollup(tmp_path: Path) -> None:
    """An empty DB without the timeseries_signals table should yield an
    OK section with all three tiers empty — the renderer omits the
    section entirely in that case."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    sqlite3.connect(str(db_path)).close()
    section = signals_section.build("GOOG", tmp_path)
    assert section.status == SectionStatus.OK
    assert section.red_signals == []
    assert section.yellow_signals == []
    assert section.green_signals == []


def test_no_db_returns_missing_data(tmp_path: Path) -> None:
    """No portfolio.db on disk → MISSING_DATA so callers can detect the
    bare-repo case if they want to."""
    section = signals_section.build("GOOG", tmp_path)
    assert section.status == SectionStatus.MISSING_DATA


# ---------------------------------------------------------------------------
# Renderer fan-out
# ---------------------------------------------------------------------------


def _make_section_with_data() -> SignalsSection:
    return SignalsSection(
        status=SectionStatus.OK,
        red_signals=[
            _row(
                "free_cash_flow",
                "financial",
                "anomaly",
                "red",
                "FCF: anomaly z=3.1",
                "z=+3.10 (max |z|=3.10, 2 pts)",
                3.1,
            ),
        ],
        yellow_signals=[
            _row(
                "GCP revenue growth (YoY)",
                "kpi",
                "yoy_acceleration",
                "yellow",
                "GCP revenue growth (YoY): YoY 18%, decelerating",
                "YoY=+18.0%, delta=-4.00%",
                -0.04,
            ),
        ],
        green_signals=[
            _row(
                "revenue",
                "financial",
                "trend",
                "green",
                "Revenue: accelerating",
                "slope=+5.0% of mean, accelerating",
                0.05,
            ),
        ],
    )


def _row(
    metric: str,
    kind: str,
    sig: str,
    sev: str,
    narrative: str,
    stat: str,
    mag: float,
):
    from report.models import SignalRow

    return SignalRow(
        metric_name=metric,
        metric_kind=kind,  # type: ignore[arg-type]
        signal_type=sig,  # type: ignore[arg-type]
        severity=sev,  # type: ignore[arg-type]
        narrative=narrative,
        value_summary=stat,
        severity_magnitude=mag,
    )


def test_markdown_renderer_emits_signals_block() -> None:
    out = StringIO()
    markdown_renderer._signals(out, _make_section_with_data())  # pyright: ignore[reportPrivateUsage]
    rendered = out.getvalue()
    assert "## §3.5 Signals" in rendered
    assert "### Fires" in rendered
    assert "[RED]" in rendered
    assert "[YELLOW]" in rendered
    # Green should not appear in the Fires bullets — only in the table.
    fires_block = rendered.split("### Fires")[1].split("<details")[0]
    assert "[GREEN]" not in fires_block
    # But green data should be in the table.
    table_block = rendered.split("<details")[1]
    assert "| green |" in table_block.lower() or "| green | revenue" in table_block


def test_markdown_renderer_skips_when_empty() -> None:
    out = StringIO()
    markdown_renderer._signals(  # pyright: ignore[reportPrivateUsage]
        out, SignalsSection(status=SectionStatus.OK)
    )
    assert out.getvalue() == ""


def test_workspace_renderer_emits_signals_panel() -> None:
    body = StringIO()
    workspace_html._signals_panel(body, _make_section_with_data())
    rendered = body.getvalue()
    assert "§3.5 Signals" in rendered
    assert "1 red · 1 yellow · 1 green" in rendered
    assert "free_cash_flow" in rendered
    assert "GCP revenue growth (YoY)" in rendered
    assert "All signals (3)" in rendered


def test_workspace_card_uses_direction_safe_status_for_favorable_anomaly() -> None:
    from report.models import SignalRow

    favorable = SignalRow(
        metric_name="High quality revenue",
        metric_kind="kpi",
        signal_type="anomaly",
        severity="red",
        narrative="A high positive z-score on a higher-is-better KPI.",
        value_summary="z=+3.4",
        investment_direction="favorable",
        statistical_significance=True,
        freshness="fresh",
    )
    section = SignalsSection(
        status=SectionStatus.OK,
        red_signals=[favorable],
        summary_signals=[favorable],
        all_signals=[favorable],
    )
    body = StringIO()
    workspace_html._signals_panel(body, section)
    rendered = body.getvalue()
    assert "signal-card sig-favorable" in rendered
    assert "signal-card sig-red" not in rendered
    assert "statistical severity red" in rendered
    assert "All signals (1)" in rendered


def test_workspace_signal_disclosure_is_keyboard_reachable_at_both_widths() -> None:
    """The compact ranked scan expands its full evidence table with the keyboard."""
    playwright_api = importlib.import_module("playwright.sync_api")
    body = StringIO()
    workspace_html._signals_panel(body, _make_section_with_data())
    html = f"<!doctype html><style>{WORKSPACE_CSS}</style><main>{body.getvalue()}</main>"

    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                try:
                    page = context.new_page()
                    page.set_content(html, wait_until="load")
                    disclosure = page.locator("details.signals-all")
                    summary = disclosure.locator("summary")
                    assert disclosure.get_attribute("open") is None
                    assert summary.bounding_box() is not None
                    summary.focus()
                    assert summary.evaluate("node => document.activeElement === node")
                    summary.press("Enter")
                    assert disclosure.get_attribute("open") == ""
                    assert disclosure.locator("tbody tr").count() == 3
                finally:
                    context.close()
        finally:
            browser.close()


def test_workspace_renderer_skips_when_empty() -> None:
    body = StringIO()
    workspace_html._signals_panel(body, SignalsSection(status=SectionStatus.OK))
    assert body.getvalue() == ""


def test_workspace_renderer_omits_fires_block_when_no_red_or_yellow() -> None:
    """When only green signals exist, the visible fires grid is omitted —
    just the collapsible "All signals" remains. Important contract since the
    fires block is the user-facing alert and shouldn't be empty noise."""
    section = SignalsSection(
        status=SectionStatus.OK,
        green_signals=[
            _row(
                "revenue", "financial", "trend", "green", "Revenue accelerating", "slope=+5%", 0.05
            ),
        ],
    )
    body = StringIO()
    workspace_html._signals_panel(body, section)
    rendered = body.getvalue()
    assert "All signals (1)" in rendered
    assert "signals-fires" not in rendered
