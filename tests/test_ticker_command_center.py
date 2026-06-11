"""PR B — per-ticker command-center drill-down.

Covers the artifact inventory (filesystem), the analysis log (DB, defensive),
the assembler + renderer, and the live /ticker/<t> and /api/ticker/<t>
endpoints. The portfolio-tracker DB is intentionally absent in the tmp repo,
so the position strip exercises its graceful "not connected" path.
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

from identity import DEFAULT_USER_ID  # noqa: E402
from pipeline.analysis_log import build_analysis_log  # noqa: E402
from pipeline.artifact_inventory import build_artifact_inventory  # noqa: E402
from pipeline.ticker_command_center import (  # noqa: E402
    build_holding_rail,
    build_ticker_command_center,
    render_holding_fragment,
    render_ticker_fragment,
    render_ticker_html,
)
from user_state.notes import create_note, resolve_note  # noqa: E402

_HOLDINGS = {
    "ticker": "NU",
    "name": "Nu Holdings",
    "last_updated": "2026-05-10",
    "thesis": "LatAm digital bank; monetization compounding.",
    "verdict": "Pending",
    "tier_1_kpis": [
        {"name": "ROE", "current": "30%", "status": "ok", "break_condition": "<25% for 2Q"}
    ],
    "break_rules": [
        {
            "kpi_name": "ROE",
            "comparator": "lt",
            "threshold": 25,
            "unit": "percent",
            "narrative": "sub-25 breaks",
        }
    ],
    "thesis_breakers_qualitative": ["ROE drifts below 25%"],
}


def _seed_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, archived_at TIMESTAMP);
        CREATE TABLE fmp_endpoint_status (ticker TEXT, last_pulled TIMESTAMP);
        CREATE TABLE transcripts (ticker TEXT, period_end TIMESTAMP);
        CREATE TABLE thesis_evaluations (ticker TEXT, evaluated_at TIMESTAMP, overall_status TEXT);
        CREATE TABLE timeseries_signals (ticker TEXT, severity TEXT, computed_at TIMESTAMP);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, trigger_kind TEXT,
                             fired_at TEXT, status TEXT, memo_artifact_id INTEGER,
                             evidence_json TEXT, signature_sha TEXT,
                             dismissed_at TEXT, approved_at TEXT);
        CREATE TABLE queued_actions (id INTEGER PRIMARY KEY, alert_id INTEGER, action_kind TEXT,
                                     payload_json TEXT, status TEXT, created_at TEXT,
                                     applied_at TEXT, cancelled_at TEXT);
        CREATE TABLE analyst_notes (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, kind TEXT,
                                    status TEXT, body TEXT, anchor_type TEXT, anchor_key TEXT,
                                    source TEXT, source_ref TEXT, supersedes_id INTEGER,
                                    resolution_note TEXT, context_json TEXT,
                                    created_at TEXT, updated_at TEXT, resolved_at TEXT);
        CREATE TABLE management_commitments (ticker TEXT, outcome TEXT, evaluated_at TIMESTAMP);
        CREATE TABLE dcf_runs (ticker TEXT, valuation_date TEXT, segment_name TEXT, over_under_pct REAL);
        CREATE TABLE llm_calls (ticker TEXT, purpose TEXT, model TEXT, cost_estimate_usd REAL, called_at TIMESTAMP);
        CREATE TABLE decisions (ticker TEXT, recommendation_kind TEXT, recommendation_value REAL,
                                conviction TEXT, made_at TIMESTAMP, outcome_label TEXT);
        CREATE TABLE brief_provenance_log (ticker TEXT, generated_at TIMESTAMP, trigger TEXT);
        """
    )
    now = datetime.now(UTC).isoformat()
    # alerts.fired_at is naive-UTC by repo convention (triggers write
    # now(UTC).replace(tzinfo=None)); the seed matches what prod rows look like.
    naive_now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    evidence = json.dumps(
        {
            "summary": "ROE inflected down two quarters running",
            "citations": [
                {
                    "kind": "kpi_observation",
                    "locator": "ROE 2026-03-31",
                    "excerpt": "ROE 30% -> 26%",
                }
            ],
        }
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','Nu Holdings','portfolio',NULL)")
    conn.execute("INSERT INTO fmp_endpoint_status VALUES ('NU','2026-05-11T01:02:14')")
    conn.execute("INSERT INTO transcripts VALUES ('NU','2026-03-31')")
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','2026-05-18T10:00:00','watch')")
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','red',?)", (now,))
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','green',?)", (now,))
    conn.execute(
        "INSERT INTO alerts VALUES (1, ?, 'NU', 'kpi_inflection', ?, 'pending', NULL, ?, "
        "'sig-nu-roe', NULL, NULL)",
        (DEFAULT_USER_ID, naive_now, evidence),
    )
    conn.execute(
        "INSERT INTO queued_actions VALUES (1, 1, 'thesis_update', ?, 'pending', ?, NULL, NULL)",
        (json.dumps({"body": "Tighten the ROE break rule"}), naive_now),
    )
    conn.execute("INSERT INTO management_commitments VALUES ('NU','met','2026-05-01')")
    conn.execute("INSERT INTO dcf_runs VALUES ('NU','2026-05-01',NULL,0.12)")
    conn.execute("INSERT INTO llm_calls VALUES ('NU','bear_case','claude-opus-4-7',0.42,?)", (now,))
    conn.execute(
        "INSERT INTO decisions VALUES ('NU','trim',20.0,'high','2026-05-15T00:00:00','pending')"
    )
    conn.execute("INSERT INTO brief_provenance_log VALUES ('NU',?,'manual')", (now,))
    conn.commit()
    conn.close()


def _seed_files(repo_root: Path) -> None:
    holdings = repo_root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    (holdings / "NU.json").write_text(json.dumps(_HOLDINGS), encoding="utf-8")
    research = repo_root / "output" / "research" / "NU"
    research.mkdir(parents=True)
    (research / "2026-05-18_workspace.html").write_text("<html>nu</html>", encoding="utf-8")
    raw = repo_root / "transcripts" / "raw"
    raw.mkdir(parents=True)
    (raw / "NU_Q1_2026.txt").write_text("transcript", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")
    _seed_files(tmp_path)
    return tmp_path


# ----- artifact inventory -----


def test_artifact_inventory_flags_present_and_absent(repo: Path) -> None:
    arts = {a.label: a for a in build_artifact_inventory(repo, "NU")}
    assert arts["Holdings JSON"].exists is True
    assert arts["Workspace report (HTML)"].exists is True
    assert arts["Raw transcripts"].exists is True
    assert arts["Raw transcripts"].count == 1
    # Something that wasn't seeded:
    assert arts["Bear case"].exists is False
    # Lowercase ticker resolves to the same upper-case paths.
    assert {a.label: a for a in build_artifact_inventory(repo, "nu")}["Holdings JSON"].exists


# ----- analysis log -----


def test_analysis_log_summarizes_each_table(repo: Path) -> None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        log = build_analysis_log(conn, "NU")
    finally:
        conn.close()
    by_name = {r.analysis: r for r in log.rows}
    assert by_name["Thesis evaluation"].summary.startswith("watch")
    assert "red" in by_name["Time-series signals"].summary
    assert by_name["Queued actions"].summary == "1 pending · 1 total"
    assert by_name["DCF valuation"].summary == "over/under +12%"
    assert log.llm_cost_30d_usd == pytest.approx(0.42)
    assert [a.trigger_kind for a in log.recent_alerts] == ["kpi_inflection"]


def test_analysis_log_empty_db_is_safe(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()  # no tables
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        log = build_analysis_log(conn, "NU")
    finally:
        conn.close()
    assert log.rows == []  # nothing present, no crash


# ----- assembler + render -----


def test_build_and_to_dict_round_trips(repo: Path) -> None:
    tcc = build_ticker_command_center(repo, "NU")
    assert tcc.identity.list_type == "portfolio"
    assert tcc.identity.breach_status == "watch"
    assert tcc.thesis.present is True
    assert tcc.thesis.tier1[0].name == "ROE"
    assert tcc.position.available is False  # no portfolio-tracker sibling in tmp
    json.loads(json.dumps(tcc.to_dict()))  # JSON round-trips cleanly


def test_render_has_all_panels(repo: Path) -> None:
    tcc = build_ticker_command_center(repo, "NU")
    html = render_ticker_html(tcc, generated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert html.startswith("<!doctype html>")
    for marker in ("Analyses run", "Artifacts", "Thesis", "Position", "Recent decisions"):
        assert marker in html
    assert "Open in Portfolio Tracker" in html  # deep link present


# ----- Holding tab (PR 8): report_date + head/foot-less fragments + embed -----


def test_report_date_derived_from_workspace_artifact(repo: Path) -> None:
    """report_date is parsed from the latest <DATE>_workspace.html filename — the
    (ticker, report_date) key the comment store + chat thread use."""
    tcc = build_ticker_command_center(repo, "NU")
    assert tcc.report_date == "2026-05-18"
    assert tcc.to_dict()["report_date"] == "2026-05-18"


def test_report_date_none_without_brief(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")  # DB but no output/research brief
    tcc = build_ticker_command_center(tmp_path, "NU")
    assert tcc.report_date is None


def test_render_ticker_fragment_is_headless(repo: Path) -> None:
    """PR4 (report-first): the fragment is a SLIM header — identity, verdict,
    freshness dot, links, and the two drawer buttons. The old inline
    position/thesis/analyses sections (duplicates of the embedded report's
    own tabs) are gone from the inline flow."""
    tcc = build_ticker_command_center(repo, "NU")
    frag = render_ticker_fragment(tcc)
    assert "<!doctype" not in frag.lower()
    assert "<html" not in frag.lower()
    assert 'href="/reports/NU"' in frag
    assert 'href="/dcf/NU"' in frag
    assert 'data-tcc-drawer="ops"' in frag
    assert 'data-tcc-drawer="notes"' in frag
    assert 'class="cc-fdot' in frag  # freshness dot replaces the 3-card strip
    for gone in ("Analyses run", "Artifacts", "Tier-1 KPIs", "kpi-strip"):
        assert gone not in frag


def test_render_holding_fragment_embeds_report(repo: Path) -> None:
    frag = render_holding_fragment(repo, "NU")
    # The full pipeline is carried by an iframe of the workspace report.
    assert 'src="/reports/NU"' in frag
    assert "cc-report-frame" in frag
    # Ops drawer carries the config/meta sections; the reread is a collapsed fold.
    assert 'data-tcc-panel="ops"' in frag
    assert "Analyses run" in frag
    assert "tcc-reread-fold" in frag
    assert "Per-holding 5-min rereads" in frag


def test_render_holding_fragment_no_brief_degrades(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _seed_db(tmp_path / "data" / "portfolio.db")  # no brief built
    frag = render_holding_fragment(tmp_path, "NU")
    assert "No workspace brief built yet" in frag
    assert "<iframe" not in frag  # nothing to embed


# ----- Holding rail (P1.3): open notes + recent alerts beside the report -----


def test_build_holding_rail_reads_open_notes_and_alerts(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    kept = create_note(ticker="NU", kind="watch", body="Watch FX drag on ARPAC", db_path=db)
    answered = create_note(ticker="NU", kind="question", body="NIM dip: mix or rate?", db_path=db)
    resolve_note(answered.id, db_path=db)
    create_note(ticker="MELI", kind="watch", body="Other name's note", db_path=db)

    rail = build_holding_rail(repo, "nu")  # lowercase in → uppercased lookup
    assert rail.notes is not None
    assert [n.id for n in rail.notes] == [kept.id]  # open-only, this ticker only
    assert rail.alerts is not None
    assert len(rail.alerts) == 1
    alert, actions = rail.alerts[0]
    assert alert.trigger_kind == "kpi_inflection"
    assert [qa.action_kind for qa in actions] == ["thesis_update"]


def test_holding_fragment_surfaces_notes_and_alerts_beside_report(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    create_note(
        ticker="NU",
        kind="watch",
        body="Watch FX drag on ARPAC next print",
        anchor_type="kpi",
        anchor_key="earnings.arpac",
        db_path=db,
    )
    answered = create_note(ticker="NU", kind="question", body="NIM dip: mix or rate?", db_path=db)
    resolve_note(answered.id, db_path=db)

    frag = render_holding_fragment(repo, "NU")
    # PR4: notes + alerts ride in the Notes drawer; the report is full-width.
    assert 'data-tcc-panel="notes"' in frag
    assert 'class="tcc-report-main"' in frag
    assert 'src="/reports/NU"' in frag
    # Open notes panel: the open note (with kind tone + anchor) shows; the
    # resolved one does not.
    assert "Open notes" in frag
    assert "Watch FX drag on ARPAC next print" in frag
    assert "nk-watch" in frag
    assert "earnings.arpac" in frag
    assert "NIM dip: mix or rate?" not in frag
    # Recent alerts panel: the digest/feed card shape with the evidence drawer
    # collapsed (rail density), memo line from evidence.summary, queued action,
    # and the filtered-feed deep link.
    assert "Recent alerts" in frag
    assert 'class="alert-card"' in frag
    assert "ROE inflected down two quarters running" in frag
    assert "Tighten the ROE break rule" in frag
    assert '<details class="evidence-drawer">' in frag
    assert '<details open class="evidence-drawer">' not in frag
    assert 'href="/feed?ticker=NU"' in frag


def test_holding_rail_empty_states(repo: Path) -> None:
    """No notes / no alerts (for a ticker with none) render the none-yet
    states, not the substrate-unavailable ones."""
    frag = render_holding_fragment(repo, "MELI")  # seeded DB, nothing for MELI
    assert "No open notes on this name" in frag
    assert "No alerts fired on this name yet" in frag


def test_holding_rail_degrades_without_substrate(tmp_path: Path) -> None:
    """A DB without the analyst_notes / alerts tables (pre-migration) degrades
    to the unavailable-state per source — never an exception."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
        "archived_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    frag = render_holding_fragment(tmp_path, "NU")
    assert "Notes substrate unavailable" in frag
    assert "Alerts substrate unavailable" in frag

    rail = build_holding_rail(tmp_path, "NU")
    assert rail.notes is None
    assert rail.alerts is None


def test_holding_rail_missing_db_is_unavailable(tmp_path: Path) -> None:
    rail = build_holding_rail(tmp_path, "NU")  # no data/portfolio.db at all
    assert rail.notes is None
    assert rail.alerts is None
    assert rail.brief_provenance is None


def test_holding_panel_endpoint(client) -> None:
    # No ticker → a pick-a-holding prompt, not a 404.
    empty = client.get("/api/panel/holding")
    assert empty.status_code == 200
    assert "Pick a holding" in empty.get_data(as_text=True)
    # With a ticker → head/foot-less fragment embedding the report at full
    # width, with notes + alerts in the Notes drawer (PR4 report-first).
    resp = client.get("/api/panel/holding?ticker=NU")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<!doctype" not in body.lower()
    assert 'src="/reports/NU"' in body
    assert 'data-tcc-panel="notes"' in body
    assert "Open notes" in body
    assert "Recent alerts" in body


# ----- live endpoints -----


@pytest.fixture
def client(repo: Path):
    import comments_server

    return comments_server.create_app(repo).test_client()


def test_ticker_api_returns_json(client) -> None:
    resp = client.get("/api/ticker/NU")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["identity"]["ticker"] == "NU"
    assert payload["thesis"]["present"] is True
    assert payload["tracker_url"].endswith("ticker=NU")


def test_ticker_page_redirects_to_shell(client) -> None:
    """/ticker/<t> is folded into the shell — it now 302-redirects to the
    Holding drill-down deep link (ticker uppercased)."""
    resp = client.get("/ticker/nu")  # lowercase in → uppercased in the deep link
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/#holding=NU"
