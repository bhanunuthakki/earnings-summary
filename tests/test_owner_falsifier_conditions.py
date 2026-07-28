"""Owner falsifiers → the break-condition engine (task 9, 2026-07-02 grill).

Three behaviors:
- attach_conditions extracts an OWNER row's conditions from its ``falsifier``
  column (not the rationale), skips unratified '(inferred)' falsifiers with
  zero LLM spend, and stamps portfolio-scope rows '[]'
- load_open_decisions keeps a graded owner decision's falsifier evaluable
  while the ticker is still a portfolio holding (an advisor row retires on
  grading as before)
- close_intent stamps closure provenance (the claude_session channel's write)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

import decision_conditions as dc
from alembic import command
from decision_conditions import attach_conditions, load_open_decisions
from research.decision_feed import persist_owner_decision
from synthesis.reconcile import close_intent

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0130_owner_decision_extension"

_PRE_DDL = """
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
    user_notes TEXT,
    made_at DATETIME NOT NULL,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid')
);
CREATE TABLE tenants (id TEXT PRIMARY KEY);
INSERT INTO tenants (id) VALUES ('bhanu');
CREATE TABLE analyst_notes (
    id INTEGER NOT NULL,
    user_id TEXT DEFAULT 'bhanu' NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'open' NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    source_ref TEXT,
    context_json TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question','decision','watch','assumption','observation','musing'))
);
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    purpose TEXT NOT NULL,
    content_md TEXT,
    generated_at TEXT NOT NULL,
    superseded_by_id INTEGER
);
CREATE TABLE advisor_memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ticker TEXT,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE tracked_companies (
    ticker TEXT PRIMARY KEY,
    list_type TEXT NOT NULL
);
INSERT INTO tracked_companies (ticker, list_type) VALUES ('NU','portfolio');
INSERT INTO tracked_companies (ticker, list_type) VALUES ('MU','index_member');
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(path))
    try:
        # Deliberately minimal 0130 contract fixture, not a production
        # versioned database; guarded writers enforce the local tables here.
        conn.execute("DROP TABLE alembic_version")
        conn.commit()
    finally:
        conn.close()
    return path


def _condition_obj() -> dict[str, object]:
    return {
        "metric": "NPL 15-90d",
        "metric_source": "kpi",
        "op": "gt",
        "threshold": 5.0,
        "unit": "percent",
        "for_periods": 2,
        "note": "15-90d NPL above 5% for two straight quarters",
    }


def test_owner_falsifier_feeds_extraction_not_rationale(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_owner_decision(
        ticker="NU",
        direction="add",
        conviction="high",
        falsifier="15-90d NPL >5% for 2Q",
        rationale="Credit book compounding; the rationale is NOT a tripwire.",
        db_path=db,
    )
    prompts: list[str] = []

    def fake_structured(prompt: str, **kwargs: object) -> object:
        prompts.append(prompt)
        return [_condition_obj()]

    monkeypatch.setattr(dc, "call_llm_structured", fake_structured)
    tally = attach_conditions(db_path=db)
    assert tally["extracted"] == 1
    assert "15-90d NPL >5% for 2Q" in prompts[0]
    assert "NOT a tripwire" not in prompts[0]  # rationale never reaches the extractor

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT decision_conditions FROM decisions WHERE decided_by='owner'"
        ).fetchone()
        assert json.loads(row[0])[0]["metric"] == "NPL 15-90d"
    finally:
        conn.close()


def test_inferred_falsifier_waits_for_ratification(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist_owner_decision(
        ticker="MU",
        direction="sell",
        falsifier="Memory cycle rolls over. (inferred)",
        db_path=db,
    )

    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("LLM called on an unratified falsifier")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    tally = attach_conditions(db_path=db)
    assert tally["awaiting_ratification"] == 1
    assert tally["extracted"] == 0
    conn = sqlite3.connect(str(db))
    try:
        # unstamped — retried after the reconcile pass strips the marker
        assert (
            conn.execute(
                "SELECT conditions_extracted_at FROM decisions WHERE ticker='MU'"
            ).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_portfolio_scope_row_stamps_empty(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, scope, "
            "falsifier, made_at, created_at) VALUES "
            "(NULL,'trim','owner','portfolio','LatAm credit exposure > $150k',"
            "'2026-07-02','2026-07-02')"
        )
        conn.commit()
    finally:
        conn.close()

    def exploding(prompt: str, **kwargs: object) -> object:
        raise AssertionError("LLM called on a portfolio-scope row")

    monkeypatch.setattr(dc, "call_llm_structured", exploding)
    tally = attach_conditions(db_path=db)
    assert tally["no_section"] == 1


def test_owner_falsifier_outlives_grading_while_held(db: Path) -> None:
    conditions = json.dumps([_condition_obj()])
    conn = sqlite3.connect(str(db))
    try:
        # Graded OWNER decision on a held name (NU = portfolio) → still open
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('NU','add','owner',?, '2026-07-01','wrong','2026-03-15','2026-07-02')",
            (conditions,),
        )
        # Graded ADVISOR decision → retired on grading, as before
        conn.execute(
            "INSERT INTO llm_artifacts (ticker, purpose, content_md, generated_at) "
            "VALUES ('NU','lens:five_min_reread','x','2026-05-01')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('NU','hold',1,?, '2026-07-01','correct','2026-05-01','2026-07-02')",
            (conditions,),
        )
        # Graded OWNER decision on an UNHELD name (MU = index_member) → retired
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
            "decision_conditions, outcome_at, outcome_label, made_at, created_at) VALUES "
            "('MU','sell','owner',?, '2026-07-01','wrong','2025-12-15','2026-07-02')",
            (conditions,),
        )
        conn.commit()
        conn.row_factory = sqlite3.Row
        nu_open = load_open_decisions(conn, "NU")
        mu_open = load_open_decisions(conn, "MU")
    finally:
        conn.close()
    assert [d.recommendation_kind for d in nu_open] == ["add"]  # owner survives, advisor retired
    assert mu_open == []


def test_close_intent_stamps_provenance(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, source_ref, created_at, "
            "updated_at) VALUES ('intent','LEAP sleeve','capture','seed:intent:leap-sleeve',"
            "'2026-07-01','2026-07-01')"
        )
        conn.commit()
    finally:
        conn.close()

    ok = close_intent(
        "seed:intent:leap-sleeve",
        "resolved-rejected",
        reason="deletes the NVO hedge",
        closed_by="claude_session:test",
        db_path=db,
    )
    assert ok
    conn = sqlite3.connect(str(db))
    try:
        status, ctx = conn.execute(
            "SELECT status, context_json FROM analyst_notes WHERE source_ref=?",
            ("seed:intent:leap-sleeve",),
        ).fetchone()
        assert status == "resolved"
        parsed = json.loads(ctx)
        assert parsed["closed_by"] == "claude_session:test"
        assert parsed["status"] == "resolved-rejected"
    finally:
        conn.close()
    # Idempotent: already-resolved intents don't re-close
    assert not close_intent(
        "seed:intent:leap-sleeve",
        "done",
        reason="x",
        closed_by="y",
        db_path=db,
    )
    with pytest.raises(ValueError):
        close_intent("seed:intent:leap-sleeve", "live", reason="x", closed_by="y", db_path=db)
