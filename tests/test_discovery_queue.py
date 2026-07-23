"""P5.4 discovery queue: the approval-queue panel, the /api/discovery REST
surface, the budget-gated build/run actions (non-spawning registry — no
real subprocesses), the deterministic /discovery chat commands, and the
discovery_build worker's status ladder.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from discovery.store import list_candidates, upsert_candidate
from pipeline.command_center_shell import render_shell
from pipeline.discovery_panel import render_discovery_list, render_discovery_panel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402
import discovery_build  # noqa: E402

from dispatch_registry import Registry  # noqa: E402

_DDL = """
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    list_type TEXT NOT NULL,
    processing_tier TEXT,
    archived_at TIMESTAMP
);
CREATE TABLE discovery_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    score FLOAT NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL,
    score_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT ck_discovery_candidates_status
        CHECK (status IN ('new', 'queued', 'building', 'built', 'dismissed')),
    CONSTRAINT ck_discovery_candidates_evidence_json CHECK (json_valid(evidence_json)),
    CONSTRAINT ck_discovery_candidates_score_json
        CHECK (score_json IS NULL OR json_valid(score_json)),
    CONSTRAINT uq_discovery_candidates_user_ticker UNIQUE (user_id, ticker)
);
CREATE TABLE discovery_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    ticker TEXT NOT NULL,
    signal_class TEXT NOT NULL,
    source_key TEXT NOT NULL,
    weight FLOAT NOT NULL DEFAULT 1.0,
    raw_strength FLOAT NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL,
    detail TEXT,
    meta_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT uq_discovery_signals_identity
        UNIQUE (user_id, ticker, signal_class, source_key)
);
CREATE TABLE discovery_sources (
    source_key TEXT PRIMARY KEY,
    signal_class TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_weight FLOAT NOT NULL DEFAULT 1.0,
    tier TEXT NOT NULL DEFAULT 'structural',
    style_tags TEXT,
    cik TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    last_calibrated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class _NonSpawningRegistry(Registry):
    """Registry that records starts but never spawns a real subprocess."""

    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.executemany(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type)"
        " VALUES ('bhanu', ?, ?, 'index_member')",
        [("WDC", "Western Digital"), ("EVR", "Evercore"), ("OLD", "Oldco"), ("DONE", "Doneco")],
    )
    conn.executemany(
        "INSERT INTO discovery_sources (source_key, signal_class, display_name, base_weight,"
        " tier, style_tags, cik, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', 'seed')",
        [
            ("quality_compounder", "screen", "Quality compounder", 1.0, "structural", None, None),
            ("watchlist", "adjacency", "Watchlist", 1.0, "structural", None, None),
            ("appaloosa", "investor_13f", "Appaloosa", 0.85, "multi_cycle", "hedge", "0001656456"),
        ],
    )
    conn.commit()
    conn.close()
    upsert_candidate(
        ticker="WDC",
        name="Western Digital",
        score=4.0,
        evidence=[
            {"source": "screen:quality_compounder", "detail": "ROIC 27.3% TTM"},
            {"source": "adjacency:watchlist", "holding": "MU", "detail": "on MU's watchlist"},
        ],
        score_json={
            "total": 4.0,
            "terms": {"screen": 2.0, "adjacency": 2.0},
            "corroboration": {"n_funds": 0, "multiplier": 1.0},
            "clamped": False,
            "signals": [
                {
                    "class": "screen",
                    "source_key": "quality_compounder",
                    "contribution": 2.0,
                    "detail": "ROIC 27.3% TTM",
                }
            ],
        },
        db_path=db,
    )
    upsert_candidate(ticker="EVR", name="Evercore", score=3.0, evidence=[], db_path=db)
    upsert_candidate(ticker="OLD", name="Oldco", score=1.0, evidence=[], db_path=db)
    upsert_candidate(ticker="DONE", name="Doneco", score=2.0, evidence=[], db_path=db)
    rows = {c.ticker: c for c in list_candidates(db_path=db)}
    from discovery.store import set_status

    set_status(rows["EVR"].id, "queued", db_path=db)
    set_status(rows["OLD"].id, "dismissed", db_path=db)
    set_status(rows["DONE"].id, "built", db_path=db)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    _seed(db)
    return tmp_path


@pytest.fixture
def registry() -> _NonSpawningRegistry:
    return _NonSpawningRegistry()


@pytest.fixture
def client(repo: Path, registry: _NonSpawningRegistry) -> FlaskClient:
    return comments_server.create_app(repo, registry=registry).test_client()


# ----------------------------------------------------------------------------
# panel + shell
# ----------------------------------------------------------------------------


def test_panel_renders_queue(repo: Path) -> None:
    html_out = render_discovery_panel(repo / "data" / "portfolio.db")
    assert 'id="dq-root"' in html_out
    assert "WDC" in html_out
    assert "ROIC 27.3% TTM" in html_out  # why-surfaced evidence is verbatim
    assert 'data-act="build"' in html_out
    assert "OLD" not in html_out  # dismissed is out of the live view
    dismissed = render_discovery_list(repo / "data" / "portfolio.db", status="dismissed")
    assert "OLD" in dismissed
    assert "Re-open" in dismissed


def test_panel_route_and_fragment(client: FlaskClient) -> None:
    page = client.get("/api/panel/discovery")
    assert page.status_code == 200
    assert b'id="dq-root"' in page.data
    frag = client.get("/api/panel/discovery?fragment=list&min_score=4")
    assert frag.status_code == 200
    assert b"WDC" in frag.data
    assert b"EVR" not in frag.data  # min_score filter applies
    assert b"dq-root" not in frag.data  # fragment is just the table


def test_shell_carries_discovery_tab() -> None:
    html_out = render_shell(overview_html="<div>x</div>")
    assert 'data-tab-target="discovery"' in html_out
    assert 'data-endpoint="/api/panel/discovery"' in html_out


def test_panel_is_one_band_on_the_kit(repo: Path) -> None:
    """The rebuilt panel: ONE toolbar band (no title band over a filter band),
    chip status filters, .p-table rows, ticker_label, score pill, and a
    collapsed score peek (design_language §6.1 + §10)."""
    html_out = render_discovery_panel(repo / "data" / "portfolio.db")
    assert "k-toolbar" in html_out  # the single operating band
    assert "<h2>Discovery</h2>" not in html_out  # nav owns the title
    assert "dq-statusfilter" in html_out and "k-chip-btn" in html_out  # chip filters
    assert 'class="p-table"' in html_out  # kit table, not the bespoke .dq-table
    assert "k-tick-sym" in html_out  # ticker_label, not a concatenated string
    assert "k-pill" in html_out  # the score pill
    assert 'class="dq-peek"' in html_out  # evidence behind a peek
    # WDC's score_json drives the inline evidence line; the verbatim detail is
    # in the (hidden) peek row.
    assert "screens 2.0" in html_out
    detail_marker = 'id="dq-detail-'
    assert detail_marker in html_out and "ROIC 27.3% TTM" in html_out
    assert "dq-sources-toggle" in html_out  # the weight-edit surface


def test_dismiss_never_uses_window_prompt() -> None:
    """Red-team wave B: Dismiss used to chain TWO window.prompt() calls (why
    passed, then what would make you revisit) — blocking OS modals that hide
    the row being dismissed. Replaced with the in-card editor idiom
    (ledger_panel.beginRewrite / journal_panel.beginEdit): both fields swap
    into the row's own evidence-detail cell as one form, restoring the prior
    content on cancel or failure."""
    import inspect

    from pipeline import discovery_panel

    src = inspect.getsource(discovery_panel)
    assert "window.prompt(" not in src
    assert "data-dismiss-save" in src and "data-dismiss-cancel" in src
    assert "data-dismiss-reason" in src and "data-dismiss-revisit" in src
    assert "beginDismiss" in src


def test_render_cap_discloses_elision(repo: Path) -> None:
    from pipeline import discovery_panel

    db = repo / "data" / "portfolio.db"
    for i in range(discovery_panel.RENDER_TOP_N + 5):
        upsert_candidate(ticker=f"CAND{i}", name=None, score=float(i), evidence=[], db_path=db)
    out = render_discovery_list(db)
    assert f"top {discovery_panel.RENDER_TOP_N} of" in out  # the cap is disclosed, not silent


# ----------------------------------------------------------------------------
# sources weight registry (the Discovery rule's editable lever)
# ----------------------------------------------------------------------------


def test_sources_editor_fragment(client: FlaskClient) -> None:
    frag = client.get("/api/panel/discovery?fragment=sources")
    assert frag.status_code == 200
    body = frag.get_data(as_text=True)
    assert "Appaloosa" in body
    assert 'data-src-weight="appaloosa"' in body  # the editable weight input
    assert "0001656456" in body  # the seeded CIK
    assert "dq-root" not in body  # fragment is just the editor


def test_sources_json_api(client: FlaskClient) -> None:
    rows = client.get("/api/discovery/sources").get_json()["sources"]
    keys = {r["source_key"] for r in rows}
    assert {"quality_compounder", "watchlist", "appaloosa"} <= keys
    investors = client.get("/api/discovery/sources?signal_class=investor_13f").get_json()["sources"]
    assert [r["source_key"] for r in investors] == ["appaloosa"]


def test_source_weight_edit(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    res = client.post("/api/discovery/sources/appaloosa/weight", json={"weight": 0.95})
    assert res.status_code == 200
    assert res.get_json()["source"]["base_weight"] == 0.95
    from discovery.sources import get_source

    assert get_source("appaloosa", db_path=db).base_weight == 0.95  # type: ignore[union-attr]
    # Validation + unknown key.
    assert client.post("/api/discovery/sources/appaloosa/weight", json={}).status_code == 400
    assert (
        client.post("/api/discovery/sources/nope/weight", json={"weight": 1.0})
    ).status_code == 404


# ----------------------------------------------------------------------------
# REST
# ----------------------------------------------------------------------------


def test_candidates_json(client: FlaskClient) -> None:
    live = client.get("/api/discovery/candidates").get_json()["candidates"]
    assert [c["ticker"] for c in live] == ["WDC", "EVR", "DONE"]  # score-ranked, no dismissed
    dismissed = client.get("/api/discovery/candidates?status=dismissed").get_json()["candidates"]
    assert [c["ticker"] for c in dismissed] == ["OLD"]
    assert client.get("/api/discovery/candidates?status=bogus").status_code == 400


def test_status_transitions(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    wdc = next(c for c in list_candidates(db_path=db) if c.ticker == "WDC")
    queued = client.post(f"/api/discovery/candidates/{wdc.id}/status", json={"status": "queued"})
    assert queued.status_code == 200
    assert queued.get_json()["candidate"]["status"] == "queued"
    # The build pathway owns building/built — the queue rejects them.
    assert (
        client.post(f"/api/discovery/candidates/{wdc.id}/status", json={"status": "built"})
    ).status_code == 400
    assert (
        client.post("/api/discovery/candidates/99999/status", json={"status": "queued"})
    ).status_code == 404


# ----------------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------------


def test_discovery_run_action(client: FlaskClient, registry: _NonSpawningRegistry) -> None:
    res = client.post("/actions/discovery-run", json={})
    assert res.status_code == 201
    body = res.get_json()
    assert body["kind"] == "discovery-run"
    job = registry.get(body["job_id"])
    assert job is not None
    assert any("run_discovery.py" in a for a in job.argv)


def test_discovery_build_single_and_bulk(
    client: FlaskClient, registry: _NonSpawningRegistry
) -> None:
    res = client.post("/actions/discovery-build", json={"tickers": ["WDC"]})
    assert res.status_code == 201
    body = res.get_json()
    assert body["kind"] == "discovery-build"
    assert body["ticker"] == "WDC"
    job = registry.get(body["job_id"])
    assert job is not None
    assert "--tickers" in job.argv
    assert "WDC" in job.argv
    # A second build of the same name while one runs is a slot conflict.
    assert client.post("/actions/discovery-build", json={"tickers": ["WDC"]}).status_code == 409
    bulk = client.post("/actions/discovery-build", json={"tickers": ["EVR", "WDC"]})
    assert bulk.status_code == 201
    assert bulk.get_json()["ticker"] == "DISCOVERY-BULK"
    assert bulk.get_json()["tickers"] == ["EVR", "WDC"]


def test_discovery_build_validation(client: FlaskClient) -> None:
    assert client.post("/actions/discovery-build", json={}).status_code == 400
    assert client.post("/actions/discovery-build", json={"tickers": []}).status_code == 400
    # Unknown name, dismissed name, and built name are all non-buildable.
    for bad in ("NOPE", "OLD", "DONE"):
        res = client.post("/actions/discovery-build", json={"tickers": [bad]})
        assert res.status_code == 400
        assert bad in res.get_json()["error"]
    too_many = [f"T{i}" for i in range(discovery_build.MAX_BUILD_BATCH + 1)]
    res = client.post("/actions/discovery-build", json={"tickers": too_many})
    assert res.status_code == 400
    assert "at most" in res.get_json()["error"]


# ----------------------------------------------------------------------------
# chat commands
# ----------------------------------------------------------------------------


def _chat(client: FlaskClient, message: str) -> str:
    res = client.post("/chat/WDC", json={"report_date": "2026-06-11", "message": message})
    assert res.status_code == 200
    assert res.mimetype == "text/event-stream"
    return res.get_data(as_text=True)


def test_chat_discovery_list(client: FlaskClient) -> None:
    out = _chat(client, "/discovery list")
    assert "WDC" in out
    assert "score 4" in out
    assert "OLD" not in out  # dismissed stays out of chat too


def test_chat_discovery_queue_and_dismiss(client: FlaskClient, repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    out = _chat(client, "/discovery queue WDC")
    assert "queued" in out
    wdc = next(c for c in list_candidates(db_path=db) if c.ticker == "WDC")
    assert wdc.status == "queued"
    out = _chat(client, "/discovery dismiss WDC")
    assert "dismissed" in out


def test_chat_discovery_build(client: FlaskClient, registry: _NonSpawningRegistry) -> None:
    out = _chat(client, "/discovery build WDC")
    assert "Eval build started" in out
    jobs = registry.list_jobs()
    assert any(j["kind"] == "discovery-build" for j in jobs)
    # Non-buildable names get a refusal, not a job.
    out = _chat(client, "/discovery build DONE")
    assert "isn't buildable" in out


def test_chat_discovery_usage(client: FlaskClient) -> None:
    out = _chat(client, "/discovery frobnicate")
    assert "Usage:" in out


# ----------------------------------------------------------------------------
# the build worker's status ladder
# ----------------------------------------------------------------------------


def test_build_one_success_ladder(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    seen: list[list[str]] = []
    statuses: list[str] = []

    def runner(argv: list[str]) -> int:
        seen.append(argv)
        cand = next(c for c in list_candidates(db_path=db) if c.ticker == "WDC")
        statuses.append(cand.status)
        return 0

    ok = discovery_build.build_one("wdc", repo_root=repo, runner=runner)
    assert ok is True
    # Three steps since P1.1 (#963): onboard -> artifacts -> Investment
    # Decision Card (runs last so its deterministic inputs are fresh).
    assert len(seen) == 3
    assert any("onboard_ticker.py" in a for a in seen[0])
    assert any("build_artifacts.py" in a for a in seen[1])
    assert "--flavor" in seen[1] and "evaluation" in seen[1]
    assert "--enable-llm" in seen[1]
    assert any("build_investment_decision_card.py" in a for a in seen[2])
    assert statuses == ["building", "building", "building"]  # in-flight while steps run
    wdc = next(c for c in list_candidates(db_path=db) if c.ticker == "WDC")
    assert wdc.status == "built"
    conn = sqlite3.connect(db)
    list_type = conn.execute(
        "SELECT list_type FROM tracked_companies WHERE ticker = 'WDC'"
    ).fetchone()[0]
    conn.close()
    assert list_type == "evaluation"  # promoted off the index bench


def test_build_one_failure_requeues(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"

    def runner(argv: list[str]) -> int:
        return 1  # the onboard step fails

    ok = discovery_build.build_one("WDC", repo_root=repo, runner=runner)
    assert ok is False
    wdc = next(c for c in list_candidates(db_path=db) if c.ticker == "WDC")
    assert wdc.status == "queued"  # back to the queue for the owner to retry


def test_build_main_caps_batch(repo: Path) -> None:
    too_many = ",".join(f"T{i}" for i in range(discovery_build.MAX_BUILD_BATCH + 1))
    code = discovery_build.main(["--tickers", too_many, "--repo-root", str(repo)])
    assert code == 2


def test_build_emits_streamable_events(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    discovery_build.build_one("WDC", repo_root=repo, runner=lambda _argv: 0)
    out = capsys.readouterr().out
    events = [json.loads(line)["event"] for line in out.splitlines() if line.strip()]
    assert events[0] == "discovery_build_start"
    assert events[-1] == "discovery_build_done"
