"""Seed-corpus → decisions backfill (the Brier denominator, day one).

Covers the deterministic mapping (action→kind, mid-month made_at, $k size
parse, ETF/Roth detection), verbatim "(inferred)" falsifiers, idempotency,
and the LEAP intent landing pre-marked resolved-rejected (corpus freshness)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from synthesis.seed_decisions import backfill_seed_decisions
from user_state import notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0130_owner_decision_extension"

_SEED = {
    "decisions": [
        {
            "ticker": "MU",
            "action": "buy",
            "approx_date": "2025-07",
            "conviction": "high",
            "rationale": "Inside view that memory prices were exploding.",
            "falsifier": "Memory pricing cycle rolls over. (inferred)",
        },
        {
            "ticker": "WIX",
            "action": "trim",
            "approx_date": "2026-04-10",
            "conviction": "low",
            "rationale": "Held in the Roth IRA; cut ~$26k as Base44 margin drag persisted.",
            "falsifier": "n/a",
        },
        {
            "ticker": "FLKR",
            "action": "buy",
            "approx_date": "2026-02",
            "conviction": "medium",
            "rationale": "South Korea value-up basket.",
            "falsifier": "Value-up program stalls.",
        },
        {"ticker": "XLV", "action": "watch", "approx_date": "2026-01"},
    ]
}


# Pre-0130 shapes, verbatim from prod DDL (the 0114-test pattern: hand-build
# the prior state, stamp the prior head, upgrade ONE migration).
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
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_decisions_source_present CHECK (
        source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL
        OR recommendation_kind = 'avoid')
);
CREATE UNIQUE INDEX uq_decisions_source_memo ON decisions (source_memo_id)
    WHERE source_memo_id IS NOT NULL;
CREATE UNIQUE INDEX uq_decisions_source_dismissal ON decisions (source_dismissal_id)
    WHERE source_dismissal_id IS NOT NULL;
CREATE TABLE tenants (id TEXT PRIMARY KEY);
INSERT INTO tenants (id) VALUES ('bhanu');
CREATE TABLE analyst_notes (
    id INTEGER NOT NULL,
    user_id TEXT DEFAULT 'bhanu' NOT NULL,
    ticker TEXT,
    kind TEXT NOT NULL,
    status TEXT DEFAULT 'open' NOT NULL,
    body TEXT NOT NULL,
    anchor_type TEXT,
    anchor_key TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    supersedes_id INTEGER,
    resolution_note TEXT,
    context_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    decision_id INTEGER,
    position_entry_id INTEGER,
    link_auto_resolve INTEGER DEFAULT 0 NOT NULL,
    fact_ref TEXT,
    PRIMARY KEY (id),
    CONSTRAINT ck_analyst_notes_source CHECK (source IN
        ('comment', 'chat', 'alert', 'manual', 'advisor', 'capture')),
    CONSTRAINT fk_analyst_notes_user_id_tenants FOREIGN KEY(user_id) REFERENCES tenants (id),
    CONSTRAINT ck_analyst_notes_status CHECK (status IN
        ('open', 'resolved', 'superseded', 'archived')),
    CONSTRAINT fk_analyst_notes_supersedes FOREIGN KEY(supersedes_id)
        REFERENCES analyst_notes (id),
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question', 'decision', 'watch', 'assumption', 'observation', 'musing'))
);
CREATE UNIQUE INDEX uq_analyst_notes_source_ref ON analyst_notes (user_id, source, source_ref)
    WHERE source_ref IS NOT NULL;
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN
    INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END;
"""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.commit()
    finally:
        conn.close()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


@pytest.fixture
def seed_path(tmp_path: Path) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_SEED), encoding="utf-8")
    return p


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM decisions WHERE decided_by='owner' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_backfill_maps_the_owner_shape(db_path: Path, seed_path: Path) -> None:
    tally = backfill_seed_decisions(db_path, seed_path)
    assert tally == {"inserted": 3, "skipped_existing": 0, "skipped_unmapped": 1, "intent": 1}

    mu, wix, flkr = _rows(db_path)
    assert (mu["recommendation_kind"], mu["conviction"]) == ("initiate", "high")
    assert mu["made_at"].startswith("2025-07-15")  # mid-month convention
    assert mu["falsifier"] == "Memory pricing cycle rolls over. (inferred)"  # verbatim
    assert mu["instrument"] == "equity" and mu["scope"] == "ticker"
    assert mu["user_notes"].startswith("seed:decision:1 ")
    assert mu["outcome_label"] is None  # grading is the standing grader's job

    assert (wix["recommendation_kind"], wix["account"]) == ("trim", "roth")
    assert wix["size_usd"] == 26000.0
    assert wix["made_at"].startswith("2026-04-10")

    assert flkr["instrument"] == "etf"


def test_backfill_is_idempotent(db_path: Path, seed_path: Path) -> None:
    backfill_seed_decisions(db_path, seed_path)
    again = backfill_seed_decisions(db_path, seed_path)
    assert again == {"inserted": 0, "skipped_existing": 3, "skipped_unmapped": 1, "intent": 0}
    assert len(_rows(db_path)) == 3


def test_leap_intent_lands_resolved_rejected(db_path: Path, seed_path: Path) -> None:
    backfill_seed_decisions(db_path, seed_path)
    rows = notes.list_notes(kind="intent", db_path=db_path, limit=10, status=None)
    assert len(rows) == 1
    intent = rows[0]
    assert intent.source_ref == "seed:intent:leap-sleeve"
    assert intent.status == "resolved"
    ctx = intent.context or {}
    assert ctx["status"] == "resolved-rejected"
    assert str(ctx["closed_by"]).startswith("claude_session:")
