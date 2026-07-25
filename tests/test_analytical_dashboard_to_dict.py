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

from flask.testing import FlaskClient  # noqa: E402

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
            thesis TEXT,
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
        -- Faithful mirror of migration 0035 (see tests/test_llm_artifact_store.py).
        -- The Risk panel reads this table via llm_artifact_store.read_current,
        -- whose query references fiscal_period/scope — a stale minimal schema here
        -- 500s that panel (regression: test_panel_fragment_portfolio_risk_offline).
        CREATE TABLE llm_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16),
            scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
            purpose VARCHAR(64) NOT NULL,
            fiscal_period VARCHAR(10),
            content_md TEXT,
            content_json TEXT,
            input_sha256 VARCHAR(64) NOT NULL DEFAULT '',
            output_sha256 VARCHAR(64),
            model VARCHAR(64),
            prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
            generated_at DATETIME NOT NULL,
            expires_at DATETIME,
            superseded_by_id INTEGER REFERENCES llm_artifacts(id),
            dirty BOOLEAN NOT NULL DEFAULT 0,
            dirty_reason VARCHAR(128),
            source_doc_ids TEXT,
            parent_artifact_ids TEXT,
            llm_call_id INTEGER
        );
        """
    )
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('NU', 'Nu', 'portfolio')"
    )
    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, breach_status) "
        "VALUES ('NU', 'Digital bank compounding deposits and cross-sell.', 'watch')"
    )
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


def _seed_ladder_extras(db_path: Path) -> None:
    """On top of _seed_db (NU: portfolio, real thesis): a watchlist name with a
    REAL thesis (WLR), a watchlist name with the prod STUB placeholder (STB),
    and an evaluation name with a real thesis (EVA) — all with fresh DCFs."""
    conn = sqlite3.connect(str(db_path))
    for ticker, list_type, thesis in (
        ("WLR", "watchlist", "Real watchlist thesis."),
        ("STB", "watchlist", "STUB: needs user-authored thesis"),
        # Wave B: the marker also appears EMBEDDED mid-text on prod (ROP) —
        # matched as a substring — while stub-ish words without the literal
        # `STUB:` token stay a real thesis.
        ("EMB", "watchlist", "Diversified industrial software. STUB: needs user-authored thesis"),
        ("SBRN", "watchlist", "A stubbornly durable moat; the STUBHUB comp is irrelevant."),
        ("EVA", "evaluation", "Real evaluation thesis."),
    ):
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
            (ticker, ticker.title(), list_type),
        )
        conn.execute(
            "INSERT INTO thesis_state (ticker, thesis, breach_status) VALUES (?, ?, 'ok')",
            (ticker, thesis),
        )
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, segment_name, over_under_pct, "
            "mos_bar_used, live_price, npv_per_share) VALUES (?, '2026-05-01', NULL, "
            "0.30, 0.25, 10.0, 7.0)",
            (ticker,),
        )
    conn.commit()
    conn.close()


def test_trigger_ladder_excludes_stub_thesis_names(tmp_path: Path) -> None:
    """Red-team wave A: 74 prod thesis_state rows carry the literal
    "STUB: needs user-authored thesis" — the ladder's has-a-thesis predicate
    must exclude them (they are exactly the irrelevant rows the owner flagged),
    while a real thesis on the same list still qualifies."""
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    _seed_ladder_extras(db_path)

    dash = build_analytical_dashboard(db_path, sections={"trigger_ladder"})
    tickers = {r.ticker for r in dash.trigger_ladder}
    assert "NU" in tickers and "WLR" in tickers  # real theses stay
    assert "STB" not in tickers  # the stub never enters
    assert "EMB" not in tickers  # embedded marker (ROP-style) excluded too
    assert "SBRN" in tickers  # stubborn/STUBHUB prose is NOT a stub


def test_trigger_ladder_list_types_scopes_to_evaluation(tmp_path: Path) -> None:
    """`list_types=("portfolio", "evaluation")` (the Record console's scope)
    swaps watchlist rows for evaluation ones — and the stub is excluded under
    ANY scope."""
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    _seed_ladder_extras(db_path)

    dash = build_analytical_dashboard(
        db_path, sections={"trigger_ladder"}, list_types=("portfolio", "evaluation")
    )
    tickers = {r.ticker for r in dash.trigger_ladder}
    assert tickers == {"NU", "EVA"}  # no watchlist rows, no stub


def test_record_console_triggers_scope_and_stub_filter(tmp_path: Path) -> None:
    """The Record console's Triggers section renders portfolio + evaluation
    theses only — watchlist rows (real or stub) stay out of it."""
    from pipeline.portfolio_console_panel import (
        _render_triggers,  # pyright: ignore[reportPrivateUsage]  # scope contract under test
    )

    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    _seed_ladder_extras(db_path)

    html = _render_triggers(db_path)
    assert "NU" in html and "EVA" in html
    assert "STB" not in html
    assert "WLR" not in html  # watchlist scope removed from the console


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


def test_analytical_page_redirects_to_shell(client) -> None:
    """/analytical is folded into the shell — it now 302-redirects to the
    Triggers tab deep link rather than rendering a standalone page."""
    resp = client.get("/analytical")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/#holdings"


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


def test_retired_panel_fragments_404(client) -> None:
    """P6.1: the theme migration's dead fragment endpoints are retired —
    insiders / predictions were killed from the nav (P1.1) with no remaining
    fetchers, and "decisions" was superseded by the Decisions record (P2.2,
    /api/panel/decisions_record). Their sections still render inside the
    full static export page (render_html)."""
    assert client.get("/api/panel/insiders?ticker=NU").status_code == 404
    assert client.get("/api/panel/predictions").status_code == 404
    assert client.get("/api/panel/decisions").status_code == 404


def test_panel_fragment_portfolio_degrades_without_tracker(client, monkeypatch) -> None:
    """The Performance panel is the tracker view. With the tracker offline
    (dead URL) it must still render head/foot-less: the live section shows the
    offline note. The synthesis layer lives on its own panel now — none of it
    rides along here."""
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:59999")  # nothing listening
    resp = client.get("/api/panel/portfolio")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()  # head/foot-less fragment
    # Tracker down → the prominent start-tracker banner leads the page.
    assert "Portfolio tracker" in body and "Start tracker" in body
    assert "Portfolio synthesis" not in body  # moved to /api/panel/portfolio_synthesis


def test_panel_fragment_portfolio_synthesis(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Synthesis tab fragment (UX round 4): the insights layer + the
    cached lens memo (the seeded DB has none cached → the run-hint stub) at
    its own lazy route. Tracker down → quiet equal-weight fallback; the
    offline/start-tracker card stays on Performance."""
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:59999")  # nothing listening
    resp = client.get("/api/panel/portfolio_synthesis")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()  # head/foot-less fragment
    assert "Portfolio synthesis" in body  # the lens-memo section
    assert "Exposure" in body  # the insights layer renders from the seeded DB
    assert "Live portfolio" not in body  # no offline card on this tab


def test_panel_fragment_portfolio_window_args_flow_to_the_tracker_fetch(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``?start_date/?end_date/?include_backfill`` on the portfolio panel route
    must reach the analytics fetch (validated) and echo back into the window
    controls (now embedded in the Performance panel header)."""
    import pipeline.portfolio_panel as pp
    from integrations.portfolio_tracker_client import (
        LivePortfolio,
        PerformanceSeries,
        PortfolioAnalytics,
    )

    captured: dict[str, object] = {}

    def _fake_analytics(
        *,
        api_url: str | None = None,
        timeout: float = 6.0,
        start_date: str | None = None,
        end_date: str | None = None,
        include_backfill: bool = False,
    ) -> PortfolioAnalytics:
        captured.update(start=start_date, end=end_date, backfill=include_backfill)
        # Available (with an empty series) so the Performance panel — and its
        # embedded window controls — renders and echoes the applied window.
        return PortfolioAnalytics(
            available=True,
            api_url="http://x",
            errors={},
            performance=PerformanceSeries(
                start_date=None,
                end_date=None,
                base_value=None,
                net_external_cashflow_in=None,
                backfill_start_unreliable=False,
                points=[],
            ),
        )

    def _fake_live(
        *,
        api_url: str | None = None,
        timeout: float = 4.0,
        transactions_limit: int = 25,
    ) -> LivePortfolio:
        return LivePortfolio(available=True, api_url="http://x", total_market_value=0.0)

    # Wave B (B4b): a liveness probe now gates the data walk — mark it up so
    # the patched fetchers are reached.
    monkeypatch.setattr(pp, "probe_tracker", lambda api_url=None: (True, "http://x"))
    monkeypatch.setattr(pp, "fetch_portfolio_analytics", _fake_analytics)
    monkeypatch.setattr(pp, "fetch_live_portfolio", _fake_live)
    resp = client.get(
        "/api/panel/portfolio?start_date=2026-01-01&end_date=2026-06-10&include_backfill=1"
    )
    assert resp.status_code == 200
    assert captured == {"start": "2026-01-01", "end": "2026-06-10", "backfill": True}
    body = resp.get_data(as_text=True)
    assert 'value="2026-01-01"' in body and 'value="2026-06-10"' in body
    # Garbage dates are sanitized before the fetch (tracker defaults instead).
    captured.clear()
    resp = client.get("/api/panel/portfolio?start_date=garbage&end_date=2026-06-10")
    assert resp.status_code == 200
    assert captured == {"start": None, "end": "2026-06-10", "backfill": False}


# ----- L5: Portfolio → Risk panel + the run-scenario SSE route -----


def test_panel_fragment_portfolio_risk_offline(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Risk panel (L5): tracker offline → the drawdown / factor sections
    degrade to one note, but the macro-stress picker + scenario options still
    render (they read the local cache, not the tracker)."""
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:59999")  # nothing listening
    resp = client.get("/api/panel/portfolio_risk")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()  # head/foot-less fragment
    assert "Whole-book macro stress" in body
    assert 'id="pfr-scenario"' in body and 'value="fed_cuts_50bps"' in body
    assert "/actions/run-scenario" in body  # the picker's SSE wiring
    assert "live portfolio tracker" in body  # the tracker-fed offline note


class _StubJob:
    def __init__(self, ticker: str, kind: str, argv: list[str]) -> None:
        self.job_id = "job_test"
        self.ticker = ticker
        self.kind = kind
        self.argv = argv
        self.started_at = datetime(2026, 6, 13, tzinfo=UTC)


class _StubRegistry:
    def __init__(self) -> None:
        self.calls: list[_StubJob] = []

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
    ) -> _StubJob:
        job = _StubJob(ticker, kind, argv)
        self.calls.append(job)
        return job


def test_run_scenario_route_rejects_unknown_scenario(client: FlaskClient) -> None:
    resp = client.post("/actions/run-scenario", json={"scenario": "not_a_scenario"})
    assert resp.status_code == 400
    assert "unknown scenario" in resp.get_json()["error"]
    # No scenario at all is rejected too (not silently dispatched).
    assert client.post("/actions/run-scenario", json={}).status_code == 400


def test_run_scenario_route_dispatches_valid_scenario(tmp_path: Path) -> None:
    import comments_server

    (tmp_path / "data").mkdir()
    stub = _StubRegistry()
    app = comments_server.create_app(tmp_path, registry=stub)  # type: ignore[arg-type]
    resp = app.test_client().post("/actions/run-scenario", json={"scenario": "fed_cuts_50bps"})
    assert resp.status_code == 201
    assert resp.get_json()["stream_url"] == "/actions/stream/job_test"
    assert len(stub.calls) == 1
    job = stub.calls[0]
    assert job.kind == "run-scenario"
    assert "run_scenario.py" in job.argv[1]
    assert "--scenario" in job.argv and "fed_cuts_50bps" in job.argv
    assert "--portfolio" in job.argv


def test_trigger_ladder_one_row_per_ticker_when_same_day_reruns_exist(tmp_path: Path) -> None:
    """Render-seam dedupe (D3.2): the nightly sweep can write SEVERAL dcf_runs
    on one valuation_date (prod: FCX/LLY/LITE each carried two on their max
    date, minutes apart). The old (ticker, valuation_date) join fanned out and
    the owner's ladder listed FCX twice at two live prices. Latest run (max id
    on the max date) must win, alone."""
    db_path = tmp_path / "portfolio.db"
    _seed_db(db_path)
    conn = sqlite3.connect(str(db_path))
    # Two same-day reruns for NU, newer than _seed_db's row: the earlier rerun
    # carries a stale live price, the later one the corrected price.
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, segment_name, over_under_pct, "
        "mos_bar_used, live_price, npv_per_share) VALUES ('NU', '2026-06-02', NULL, "
        "0.30, 0.25, 60.97, 35.45)"
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, segment_name, over_under_pct, "
        "mos_bar_used, live_price, npv_per_share) VALUES ('NU', '2026-06-02', NULL, "
        "0.32, 0.25, 63.50, 35.45)"
    )
    conn.commit()
    conn.close()

    dash = build_analytical_dashboard(db_path, sections={"trigger_ladder"})
    nu_rows = [r for r in dash.trigger_ladder if r.ticker == "NU"]
    assert len(nu_rows) == 1  # fanned out to 2 pre-fix
    assert nu_rows[0].live_price == pytest.approx(63.50)  # the LATER rerun wins
    assert nu_rows[0].over_under_pct == pytest.approx(0.32)
