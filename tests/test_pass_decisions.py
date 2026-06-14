"""Errors of omission — pass/avoid as first-class decisions (L11 PR1).

Covers the authoring path end to end: the ``record_pass_decision`` write
(manual + idempotent dismiss-sourced), the ``source_prose`` extraction bridge
(numeric + qualitative, reusing L9's rungs), the discovery dismiss wiring, the
manual REST route, and the ``/discovery dismiss T <why>`` chat path.

No real LLM: ``decision_conditions.call_llm_structured`` is monkeypatched where
the attach rungs run.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from flask.testing import FlaskClient

import decision_conditions as dc
import pass_decisions as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

from dispatch_registry import Registry  # noqa: E402

# The decisions table at the 0110 head: 0046 + 0086 + 0109 + 0110 columns, with
# the widened source-present CHECK and the dismissal partial-unique index.
_DECISIONS_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    source_prose TEXT,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    user_notes TEXT,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    qualitative_conditions TEXT,
    qualitative_conditions_extracted_at DATETIME,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid'
    )
);
CREATE UNIQUE INDEX uq_decisions_source_dismissal
    ON decisions(source_dismissal_id) WHERE source_dismissal_id IS NOT NULL;
CREATE TABLE llm_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, content_md TEXT);
CREATE TABLE advisor_memos (id INTEGER PRIMARY KEY AUTOINCREMENT, body_md TEXT);
"""

_DISCOVERY_DDL = """
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
    CONSTRAINT uq_discovery_candidates_user_ticker UNIQUE (user_id, ticker)
);
"""


def _make_db(path: Path, *, with_discovery: bool = False) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_DECISIONS_DDL)
        if with_discovery:
            conn.executescript(_DISCOVERY_DDL)
            ts = "2026-06-14T00:00:00"
            conn.execute(
                "INSERT INTO discovery_candidates "
                "(id, ticker, name, status, score, evidence_json, "
                " first_seen_at, last_seen_at, updated_at) "
                "VALUES (7, 'WDC', 'Western Digital', 'new', 4.0, '[]', ?, ?, ?)",
                (ts, ts, ts),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    _make_db(path)
    return path


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record_pass_decision
# ---------------------------------------------------------------------------


def test_manual_pass_inserts_avoid_row(db: Path) -> None:
    res = pd.record_pass_decision(
        ticker="nvda",
        reason="too rich at 40x forward, no margin of safety",
        revisit_text="revisit if the multiple halves below 20x",
        db_path=db,
    )
    assert res is not None and res.created is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "NVDA"  # upper-cased
    assert row["recommendation_kind"] == "avoid"
    assert row["source_lens"] == pd.LENS_MANUAL_PASS
    assert row["rationale_excerpt"].startswith("too rich")
    assert "halves below 20x" in row["source_prose"]
    assert row["outcome_label"] == "pending"
    # Extraction is DEFERRED to the batch rungs — stamps left NULL.
    assert row["conditions_extracted_at"] is None
    assert row["qualitative_conditions_extracted_at"] is None


def test_dismissal_pass_is_idempotent(db: Path) -> None:
    first = pd.record_pass_decision(
        ticker="WDC",
        reason="cyclical trough, prefer to wait",
        source_dismissal_id=7,
        source_lens=pd.LENS_DISCOVERY_DISMISSAL,
        db_path=db,
    )
    assert first is not None and first.created is True
    # Re-dismiss the same candidate: UPDATE, not a duplicate row.
    second = pd.record_pass_decision(
        ticker="WDC",
        reason="still cyclical, now also levered",
        revisit_text="revisit if net debt falls below 1x EBITDA",
        source_dismissal_id=7,
        source_lens=pd.LENS_DISCOVERY_DISMISSAL,
        db_path=db,
    )
    assert second is not None and second.created is False
    assert second.decision_id == first.decision_id
    rows = _rows(db)
    assert len(rows) == 1
    assert "now also levered" in rows[0]["rationale_excerpt"]
    assert "net debt" in rows[0]["source_prose"]


def test_record_pass_guards(tmp_path: Path, db: Path) -> None:
    # Empty ticker → None.
    assert pd.record_pass_decision(ticker="  ", reason="x", db_path=db) is None
    # Pre-0110 schema (no source_prose column) → None, never a raise.
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, ticker TEXT, "
        "recommendation_kind TEXT, made_at TEXT)"
    )
    conn.commit()
    conn.close()
    assert pd.record_pass_decision(ticker="AAPL", reason="x", db_path=legacy) is None


# ---------------------------------------------------------------------------
# resolve_extraction_section + source_prose attach bridge
# ---------------------------------------------------------------------------


def test_resolve_extraction_section_precedence() -> None:
    artifact = "## What would change my mind\n\n- NPLs above 7%.\n"
    assert dc.resolve_extraction_section(artifact, None, None) == "- NPLs above 7%."
    # Memo header path.
    assert dc.resolve_extraction_section(None, artifact, None) == "- NPLs above 7%."
    # Owner prose: taken whole, no header required.
    assert dc.resolve_extraction_section(None, None, "valuation halves below $90") == (
        "valuation halves below $90"
    )
    # Nothing usable.
    assert dc.resolve_extraction_section(None, None, "   ") is None
    # Artifact present but section absent → None (not a fall-through to prose).
    assert dc.resolve_extraction_section("no section here", None, "x") is None


def test_attach_numeric_conditions_from_source_prose(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pd.record_pass_decision(
        ticker="NU",
        reason="passed on valuation",
        revisit_text="revisit if the forward P/E falls below 20",
        db_path=db,
    )
    monkeypatch.setattr(
        dc,
        "call_llm_structured",
        lambda *a, **k: [
            {
                "metric": "forward P/E",
                "metric_source": None,
                "op": "lt",
                "threshold": 20,
                "unit": "ratio",
                "for_periods": 1,
                "note": "forward P/E below 20",
            }
        ],
    )
    tally = dc.attach_conditions(db_path=db)
    assert tally["extracted"] == 1
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        opens = dc.load_open_decisions(conn, "NU")
    finally:
        conn.close()
    assert len(opens) == 1
    assert opens[0].recommendation_kind == "avoid"
    assert opens[0].conditions[0].metric == "forward P/E"


def test_attach_qualitative_conditions_from_source_prose(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pd.record_pass_decision(
        ticker="WDC",
        reason="incumbent moat looks durable",
        revisit_text="revisit if a credible competitor stumbles or the CEO departs",
        db_path=db,
    )
    monkeypatch.setattr(
        dc,
        "call_llm_structured",
        lambda *a, **k: [
            {"phrase": "a credible competitor stumbles", "watch_for": "news", "note": "x"}
        ],
    )
    tally = dc.attach_qualitative_conditions(db_path=db)
    assert tally["extracted"] == 1
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conds = dc.load_open_qualitative_conditions(conn, "WDC", surface="news")
    finally:
        conn.close()
    assert len(conds) == 1
    assert conds[0].phrase == "a credible competitor stumbles"


# ---------------------------------------------------------------------------
# routes + chat
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data / "portfolio.db", with_discovery=True)
    return tmp_path


@pytest.fixture
def client(repo: Path) -> FlaskClient:
    return comments_server.create_app(repo, registry=Registry()).test_client()


def test_dismiss_with_reason_records_avoid(client: FlaskClient, repo: Path) -> None:
    res = client.post(
        "/api/discovery/candidates/7/status",
        json={
            "status": "dismissed",
            "reason": "cyclical trough, prefer to wait",
            "revisit_if": "revisit if a credible competitor stumbles",
        },
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["candidate"]["status"] == "dismissed"
    assert body["pass_decision"]["created"] is True
    rows = _rows(repo / "data" / "portfolio.db")
    assert len(rows) == 1
    assert rows[0]["recommendation_kind"] == "avoid"
    assert rows[0]["source_dismissal_id"] == 7
    assert rows[0]["source_lens"] == pd.LENS_DISCOVERY_DISMISSAL
    assert "competitor stumbles" in rows[0]["source_prose"]


def test_dismiss_without_reason_records_nothing(client: FlaskClient, repo: Path) -> None:
    res = client.post("/api/discovery/candidates/7/status", json={"status": "dismissed"})
    assert res.status_code == 200
    assert "pass_decision" not in res.get_json()
    assert _rows(repo / "data" / "portfolio.db") == []


def test_manual_pass_route(client: FlaskClient, repo: Path) -> None:
    ok = client.post(
        "/api/decisions/pass",
        json={
            "ticker": "tsla",
            "reason": "governance risk",
            "revisit_if": "if the board turns over",
        },
    )
    assert ok.status_code == 200
    assert ok.get_json()["pass_decision"]["ticker"] == "TSLA"
    rows = _rows(repo / "data" / "portfolio.db")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "TSLA"
    assert rows[0]["source_dismissal_id"] is None
    assert rows[0]["source_lens"] == pd.LENS_MANUAL_PASS
    # Validation.
    assert client.post("/api/decisions/pass", json={"reason": "x"}).status_code == 400
    assert client.post("/api/decisions/pass", json={"ticker": "T"}).status_code == 400


def test_chat_dismiss_with_reason_records(client: FlaskClient, repo: Path) -> None:
    res = client.post(
        "/chat/WDC",
        json={"report_date": "2026-06-11", "message": "/discovery dismiss WDC too cyclical for me"},
    )
    assert res.status_code == 200
    out = res.get_data(as_text=True)
    assert "avoid decision" in out
    rows = _rows(repo / "data" / "portfolio.db")
    assert len(rows) == 1
    assert rows[0]["recommendation_kind"] == "avoid"
    assert rows[0]["rationale_excerpt"] == "too cyclical for me"
