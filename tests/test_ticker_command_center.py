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

from pipeline.analysis_log import build_analysis_log  # noqa: E402
from pipeline.artifact_inventory import build_artifact_inventory  # noqa: E402
from pipeline.ticker_command_center import (  # noqa: E402
    build_ticker_command_center,
    render_ticker_html,
)

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
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, ticker TEXT, trigger_kind TEXT, fired_at TEXT, status TEXT);
        CREATE TABLE queued_actions (id INTEGER PRIMARY KEY, alert_id INTEGER, status TEXT);
        CREATE TABLE management_commitments (ticker TEXT, outcome TEXT, evaluated_at TIMESTAMP);
        CREATE TABLE dcf_runs (ticker TEXT, valuation_date TEXT, segment_name TEXT, over_under_pct REAL);
        CREATE TABLE llm_calls (ticker TEXT, purpose TEXT, model TEXT, cost_estimate_usd REAL, called_at TIMESTAMP);
        CREATE TABLE decisions (ticker TEXT, recommendation_kind TEXT, recommendation_value REAL,
                                conviction TEXT, made_at TIMESTAMP, outcome_label TEXT);
        CREATE TABLE brief_provenance_log (ticker TEXT, generated_at TIMESTAMP, trigger TEXT);
        """
    )
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO tracked_companies VALUES ('NU','Nu Holdings','portfolio',NULL)")
    conn.execute("INSERT INTO fmp_endpoint_status VALUES ('NU','2026-05-11T01:02:14')")
    conn.execute("INSERT INTO transcripts VALUES ('NU','2026-03-31')")
    conn.execute("INSERT INTO thesis_evaluations VALUES ('NU','2026-05-18T10:00:00','watch')")
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','red',?)", (now,))
    conn.execute("INSERT INTO timeseries_signals VALUES ('NU','green',?)", (now,))
    conn.execute("INSERT INTO alerts VALUES (1,'NU','kpi_inflection',?,'pending')", (now,))
    conn.execute("INSERT INTO queued_actions VALUES (1,1,'pending')")
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


def test_ticker_page_returns_html(client) -> None:
    resp = client.get("/ticker/NU")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "Nu Holdings" in resp.get_data(as_text=True)
