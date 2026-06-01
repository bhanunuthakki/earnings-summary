"""PR A — shared overview module: AnalyticalDashboard.to_dict() + the relocated
renderer + the live /api/overview and /analytical endpoints.

The data layer (pipeline.analytical_dashboard) was already pure-read and shared;
PR A adds a JSON view (`to_dict`) and moves the render layer into
pipeline.analytical_dashboard_html so the static export and the live command
center render from one code path. These tests lock that contract.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

from pipeline.analytical_dashboard import (  # noqa: E402
    AnalyticalDashboard,
    build_analytical_dashboard,
)
from pipeline.analytical_dashboard_html import render_html  # noqa: E402

_EXPECTED_KEYS = {
    "trigger_ladder",
    "insider_events",
    "prediction_outcomes",
    "portfolio_synthesis_md",
    "per_ticker_reread",
    "decisions",
    "llm_budgets",
}


def _seed_db(db_path: Path) -> None:
    """Minimal schema + rows to make the budget, decisions, and trigger-ladder
    panels non-empty."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT,
            list_type TEXT NOT NULL,
            archived_at TIMESTAMP
        );
        CREATE TABLE thesis_state (
            ticker TEXT NOT NULL,
            breach_status TEXT
        );
        CREATE TABLE dcf_runs (
            ticker TEXT NOT NULL,
            valuation_date TEXT NOT NULL,
            segment_name TEXT,
            over_under_pct REAL,
            mos_bar_used REAL,
            live_price REAL,
            npv_per_share REAL
        );
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            recommendation_kind TEXT NOT NULL,
            recommendation_value REAL,
            conviction TEXT,
            made_at TIMESTAMP NOT NULL,
            outcome_label TEXT,
            outcome_pct REAL,
            rationale_excerpt TEXT
        );
        CREATE TABLE llm_budgets (
            purpose TEXT NOT NULL UNIQUE,
            monthly_cap_usd NUMERIC NOT NULL,
            warn_threshold_pct REAL DEFAULT 0.80,
            hard_block INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            notes TEXT
        );
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT,
            ticker TEXT,
            cost_estimate_usd REAL,
            called_at TIMESTAMP
        );
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, purpose TEXT, scope TEXT, content_md TEXT,
            generated_at TEXT, superseded_by_id INTEGER
        );
        """
    )
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('NU', 'Nu', 'portfolio')"
    )
    conn.execute("INSERT INTO thesis_state (ticker, breach_status) VALUES ('NU', 'watch')")
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, segment_name, over_under_pct, "
        "mos_bar_used, live_price, npv_per_share) "
        "VALUES ('NU', '2026-05-01', NULL, 0.25, 0.25, 14.0, 11.0)"
    )
    conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, recommendation_value, conviction, "
        "made_at, outcome_label, outcome_pct, rationale_excerpt) "
        "VALUES ('NU', 'trim', 20.0, 'high', ?, 'correct', 0.08, 'rich vs DCF')",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, hard_block, "
        "created_at, updated_at) VALUES ('bear_case', 50.0, 0.80, 0, ?, ?)",
        (now.isoformat(), now.isoformat()),
    )
    conn.execute(
        "INSERT INTO llm_calls (purpose, ticker, cost_estimate_usd, called_at) "
        "VALUES ('bear_case', 'NU', 12.50, ?)",
        (now.isoformat(),),
    )
    # A second call on an UN-budgeted purpose, attributed to a different ticker —
    # proves the by-ticker view spans every purpose, not just capped ones.
    conn.execute(
        "INSERT INTO llm_calls (purpose, ticker, cost_estimate_usd, called_at) "
        "VALUES ('recent_developments', 'AMD', 5.00, ?)",
        (now.isoformat(),),
    )
    conn.execute(
        "INSERT INTO llm_artifacts (ticker, purpose, content_md, generated_at, superseded_by_id) "
        "VALUES ('NU', 'lens:five_min_reread', '## NU 5-min reread', ?, NULL)",
        (now.isoformat(),),
    )
    conn.commit()
    conn.close()


def test_to_dict_empty_round_trips() -> None:
    payload = AnalyticalDashboard().to_dict()
    assert set(payload.keys()) == _EXPECTED_KEYS
    # JSON-serializable with no custom encoder (proves no Decimal/datetime leaks).
    restored = json.loads(json.dumps(payload))
    assert restored["trigger_ladder"] == []
    assert restored["decisions"]["recent"] == []
    assert restored["llm_budgets"]["rows"] == []


def test_build_missing_db_returns_empty(tmp_path: Path) -> None:
    dash = build_analytical_dashboard(tmp_path / "does_not_exist.db")
    payload = dash.to_dict()
    assert set(payload.keys()) == _EXPECTED_KEYS
    json.dumps(payload)  # must not raise


def test_render_html_empty_has_all_panels() -> None:
    html = render_html(
        AnalyticalDashboard(),
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</body></html>")
    # Every panel renders its header even when empty. (The budget <h2> uses a
    # literal '&' in the source markup — match it verbatim, don't assume escaping.)
    for marker in ("Trigger ladder", "LLM spend & budget", "Decisions", "Portfolio synthesis"):
        assert marker in html


def test_build_and_to_dict_with_seeded_db(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    dash = build_analytical_dashboard(db_path)

    # Budget panel picked up the seeded cap + spend.
    assert [r.purpose for r in dash.llm_budgets.rows] == ["bear_case"]
    assert dash.llm_budgets.rows[0].current_spend_usd == pytest.approx(12.50)
    # Decisions ledger picked up the seeded recommendation.
    assert [d.ticker for d in dash.decisions.recent] == ["NU"]
    # Trigger ladder positioned NU as 'sell' (over_under 0.25 > 0.20).
    assert [r.trigger_status for r in dash.trigger_ladder] == ["sell"]

    payload = dash.to_dict()
    restored = json.loads(json.dumps(payload))  # round-trips cleanly
    assert restored["llm_budgets"]["rows"][0]["purpose"] == "bear_case"
    assert restored["decisions"]["recent"][0]["ticker"] == "NU"


def test_render_html_with_seeded_db_shows_content(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    dash = build_analytical_dashboard(db_path)
    html = render_html(dash, generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert "bear_case" in html
    assert "NU" in html


def test_trigger_ladder_null_price_row_is_well_formed() -> None:
    """Regression: a watchlist row (live_price / over_under / mos all NULL) must
    render a COMPLETE <tr> — ticker link, list, verdict + em-dash cells — not a
    malformed values-only row. The old inline `… if live_price is not None else …`
    ternary bound to the entire concatenated f-string and dropped the row opener +
    first three cells whenever the live price was NULL (every watchlist ticker)."""
    from pipeline.analytical_dashboard import TriggerLadderRow

    row = TriggerLadderRow(
        ticker="AMAT",
        list_type="watchlist",
        over_under_pct=None,
        mos_bar=None,
        trigger_status="unknown",
        live_price=None,  # the bug trigger
        dcf_fair_value=167.0,
        verdict="Pending",
    )
    html = render_html(
        AnalyticalDashboard(trigger_ladder=[row]),
        generated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # Isolate the data row (its opener was the casualty of the bug).
    section = html[html.index("Trigger ladder") :]
    rstart = section.index('<tr class="tone-muted">')
    row_html = section[rstart : section.index("</tr>", rstart)]
    # Ticker link + verdict cells (also dropped by the bug) are present...
    assert 'class="ticker-link">AMAT</a>' in row_html
    assert ">Pending<" in row_html
    # ...and the row has all 8 cells (ticker, list, verdict, live, fair, over/under,
    # mos, trigger) — the malformed bug row had fewer.
    assert row_html.count("<td") == 8


def test_llm_budget_panel_by_ticker(tmp_path: Path) -> None:
    """The by-ticker companion view: MTD spend grouped by attributed ticker,
    spanning every purpose (budgeted or not), with a synthetic bucket for
    unattributed calls."""
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    dash = build_analytical_dashboard(db_path)

    by_ticker = {r.ticker: r for r in dash.llm_budgets.by_ticker}
    # NU (bear_case, budgeted) + AMD (recent_developments, UN-budgeted) both appear.
    assert by_ticker["NU"].current_spend_usd == pytest.approx(12.50)
    assert by_ticker["AMD"].current_spend_usd == pytest.approx(5.00)
    assert by_ticker["AMD"].call_count == 1
    # The by-ticker total (17.50) exceeds the capped by-purpose total (12.50 —
    # bear_case only) precisely because it also counts the un-budgeted call.
    assert dash.llm_budgets.total_spend_mtd_usd == pytest.approx(12.50)
    assert sum(r.current_spend_usd for r in dash.llm_budgets.by_ticker) == pytest.approx(17.50)

    # Renders a By-ticker table and survives the JSON round-trip used by /api/overview.
    html = render_html(dash, generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert "By ticker" in html
    assert "AMD" in html
    json.loads(json.dumps(dash.to_dict()))


# ----- live endpoints (wired into comments_server) -----


@pytest.fixture
def client(tmp_path: Path):
    import comments_server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _seed_db(data_dir / "portfolio.db")
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_overview_api_returns_json(client) -> None:
    resp = client.get("/api/overview")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert _EXPECTED_KEYS.issubset(payload.keys())
    assert "tier_coverage" in payload
    assert payload["llm_budgets"]["rows"][0]["purpose"] == "bear_case"


def test_analytical_page_returns_html(client) -> None:
    resp = client.get("/analytical")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = resp.get_data(as_text=True)
    assert body.startswith("<!doctype html>")
    assert "Trigger ladder" in body


# ----- PR 4: shell backend seams -----


def test_build_with_section_selector(tmp_path: Path) -> None:
    """`sections=` builds only the requested panel; the rest stay empty. `None`
    (default) still builds everything — the /api/overview + static-export path."""
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)

    only_ladder = build_analytical_dashboard(db_path, sections={"trigger_ladder"})
    assert [r.trigger_status for r in only_ladder.trigger_ladder] == ["sell"]  # requested
    assert only_ladder.llm_budgets.rows == []  # not requested → default empty
    assert only_ladder.decisions.recent == []

    full = build_analytical_dashboard(db_path)  # sections=None
    assert full.llm_budgets.rows  # everything built, as before


def test_tickers_api(client) -> None:
    resp = client.get("/api/tickers")
    assert resp.status_code == 200
    tickers = resp.get_json()["tickers"]
    assert any(t["ticker"] == "NU" and t["list_type"] == "portfolio" for t in tickers)


def test_dcf_route_404_then_serves(client, tmp_path: Path) -> None:
    # The client fixture roots the app at tmp_path; no workbook yet → 404.
    assert client.get("/dcf/NU").status_code == 404
    dcf_dir = tmp_path / "dcf"
    dcf_dir.mkdir()
    (dcf_dir / "NU.xlsx").write_bytes(b"PK\x03\x04 fake xlsx bytes")
    resp = client.get("/dcf/NU")
    assert resp.status_code == 200


# ----- PR 5: panel fragment endpoints -----


def test_panel_fragment_budget_is_headless(client) -> None:
    resp = client.get("/api/panel/budget")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()  # a fragment, not a full page
    assert "bear_case" in body  # the seeded budget row rendered


def test_panel_fragment_unknown_404(client) -> None:
    assert client.get("/api/panel/nope").status_code == 404


def test_panel_fragment_prereads_ticker_filter(client) -> None:
    # The seed has one NU reread; the full panel shows its card.
    all_body = client.get("/api/panel/prereads").get_data(as_text=True)
    assert 'class="reread-card"' in all_body
    assert "NU" in all_body
    # ?ticker=AMD (no AMD reread) → header still renders, but no card.
    amd_body = client.get("/api/panel/prereads?ticker=AMD").get_data(as_text=True)
    assert "Per-holding 5-min rereads" in amd_body
    assert 'class="reread-card"' not in amd_body


def test_panel_fragment_insiders_headless(client) -> None:
    # No insider_transactions seeded → empty section, but headless + 200.
    resp = client.get("/api/panel/insiders?ticker=NU")
    assert resp.status_code == 200
    assert "<!doctype" not in resp.get_data(as_text=True).lower()


def test_panel_fragment_portfolio_degrades_without_tracker(client, monkeypatch) -> None:
    """The Portfolio panel layers live tracker data over the cached synthesis.
    With the tracker offline (dead URL) it must still render head/foot-less: the
    live section shows the offline note and the synthesis still renders."""
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:59999")  # nothing listening
    resp = client.get("/api/panel/portfolio")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()  # head/foot-less fragment
    assert "Live portfolio" in body  # the live section (offline note)
    assert "Portfolio synthesis" in body  # the cached synthesis still renders
