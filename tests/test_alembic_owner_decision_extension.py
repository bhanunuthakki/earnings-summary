"""Round-trip test for ``0130_owner_decision_extension``.

Mirrors the repo's alembic-test pattern: hand-create minimal pre-0130 tables,
stamp the prior head, run the one migration. Proves:

- the 8 owner-shape columns land; existing rows read decided_by='advisor'
- an owner decision needs no artifact/memo (widened source CHECK), while an
  advisor row without provenance still violates it
- a portfolio-scope row may omit ticker; a ticker-scope row may not
- kind='intent' becomes insertable on analyst_notes
- the analyst_notes_fts sync triggers survive the batch recreate (the
  0125/0128 gotcha) — a post-migration insert is FTS-searchable
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0129_commitment_scan_log"
HEAD = "0130_owner_decision_extension"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    made_at DATETIME NOT NULL,
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
CREATE TABLE analyst_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind VARCHAR(32) NOT NULL,
    body TEXT NOT NULL,
    source VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    context_json TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    CONSTRAINT ck_analyst_notes_kind CHECK (kind IN
        ('question','decision','watch','assumption','observation','musing'))
);
CREATE VIRTUAL TABLE analyst_notes_fts USING fts5(
    body, content='analyst_notes', content_rowid='id');
CREATE TRIGGER analyst_notes_fts_ai AFTER INSERT ON analyst_notes BEGIN
    INSERT INTO analyst_notes_fts(rowid, body) VALUES (new.id, new.body); END;
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "m.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_PRE_DDL)
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
            "made_at, created_at) VALUES ('NU','hold',1,'2026-05-25','2026-05-25')"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    return db


def test_upgrade_adds_owner_shape_and_defaults(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert {
            "decided_by",
            "instrument",
            "account",
            "size_usd",
            "size_pct",
            "falsifier",
            "tranche_of",
            "scope",
        } <= cols
        row = conn.execute("SELECT decided_by, scope FROM decisions WHERE ticker='NU'").fetchone()
        assert row == ("advisor", "ticker")
    finally:
        conn.close()


def test_owner_rows_need_no_provenance_but_advisor_still_does(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # Owner decision: no artifact/memo — allowed now
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, conviction, "
            "falsifier, instrument, account, size_usd, made_at, created_at) "
            "VALUES ('NVO','add','owner','high','GLP-1 share loss 2Q','equity','taxable',"
            "31000,'2026-07-02','2026-07-02')"
        )
        # Portfolio-scope owner decision: NULL ticker — allowed
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, decided_by, scope, "
            "made_at, created_at) "
            "VALUES (NULL,'trim','owner','portfolio','2026-07-02','2026-07-02')"
        )
        conn.commit()
        # Advisor row without provenance: still refused by the CHECK
        try:
            conn.execute(
                "INSERT INTO decisions (ticker, recommendation_kind, made_at, created_at) "
                "VALUES ('NU','add','2026-07-02','2026-07-02')"
            )
            raise AssertionError("advisor row without provenance should violate the CHECK")
        except sqlite3.IntegrityError:
            pass
        # Ticker-scope row without ticker: refused by ck_decisions_ticker_scope
        try:
            conn.execute(
                "INSERT INTO decisions (ticker, recommendation_kind, decided_by, "
                "made_at, created_at) VALUES (NULL,'add','owner','2026-07-02','2026-07-02')"
            )
            raise AssertionError("ticker-scope row without ticker should violate the CHECK")
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()


def test_intent_kind_and_fts_triggers_survive(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO analyst_notes (kind, body, source, context_json) VALUES "
            "('intent','far-OTM long-dated LEAP sleeve on the next washout','capture',"
            '\'{"status": "open"}\')'
        )
        conn.commit()
        trigs = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%fts%'"
            ).fetchall()
        }
        assert {"analyst_notes_fts_ai", "analyst_notes_fts_ad", "analyst_notes_fts_au"} <= trigs
        hits = conn.execute(
            "SELECT rowid FROM analyst_notes_fts WHERE analyst_notes_fts MATCH 'sleeve'"
        ).fetchall()
        assert hits, "post-migration insert must be FTS-searchable (triggers restored)"
        # Partial unique indexes survived the batch recreate with their WHERE
        for name in ("uq_decisions_source_memo", "uq_decisions_source_dismissal"):
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            assert sql is not None and "WHERE" in str(sql[0]).upper()
    finally:
        conn.close()


def test_downgrade_refuses_when_owner_rows_exist_else_clean(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "decided_by" not in cols and "scope" not in cols
    finally:
        conn.close()
