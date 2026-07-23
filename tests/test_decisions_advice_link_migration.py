"""Round-trip test for ``0188_decisions_advice_artifact``.

Mirrors the repo alembic-test pattern (tests/test_alembic_owner_decision_extension.py,
tests/test_alembic_reader_perf_indexes.py): hand-create the full pre-0188
``decisions`` shape plus the 7 tables ``v_decision_journal`` reads, stamp the
prior head, run the one migration. Proves:

- ``advice_artifact_id`` lands as a plain nullable column (no FK/CHECK)
- existing (pre-migration) decisions rows remain readable, untouched
- ``v_decision_journal`` rebuilds and exposes ``advice_artifact_id`` /
  ``advice_purpose`` / ``advice_created_at`` / ``advice_preceded``
- ``advice_preceded`` is 1 when the artifact predates the decision, 0 when it
  doesn't, NULL when no advice artifact is linked
- the ``incremental_dollar_recommendation`` llm_budgets row is seeded
  ($10/mo, on_exceed='block'), idempotently
- downgrade refuses when a decision carries an advice link, else is clean
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0187_wealth_context_snapshot_history"
HEAD = "0188_decisions_advice_artifact"

_PRE_DDL = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value REAL,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    user_notes TEXT,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct REAL,
    outcome_notes TEXT,
    created_at DATETIME NOT NULL,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    source_memo_id INTEGER,
    source_dismissal_id INTEGER,
    source_prose TEXT,
    process_quality VARCHAR(16),
    decided_by VARCHAR(16) NOT NULL DEFAULT 'advisor',
    instrument VARCHAR(24),
    account VARCHAR(16),
    size_usd REAL,
    size_pct REAL,
    falsifier TEXT,
    tranche_of INTEGER,
    scope VARCHAR(16) NOT NULL DEFAULT 'ticker',
    basis_kind VARCHAR(16),
    basis_ref_id INTEGER,
    basis_value REAL,
    basis_as_of VARCHAR(32),
    basis_meta_json TEXT
);
CREATE UNIQUE INDEX uq_decisions_source_memo ON decisions (source_memo_id)
    WHERE source_memo_id IS NOT NULL;
CREATE UNIQUE INDEX uq_decisions_source_dismissal ON decisions (source_dismissal_id)
    WHERE source_dismissal_id IS NOT NULL;

CREATE TABLE advisor_memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ticker TEXT,
    counter_ticker TEXT,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    context_json TEXT,
    stance TEXT,
    horizon_days INTEGER,
    note_id INTEGER,
    ledger_entry_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE stance_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memo_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    benchmark_basis TEXT NOT NULL DEFAULT 'none',
    horizon_days INTEGER NOT NULL,
    start_date TEXT,
    end_date TEXT,
    ticker TEXT,
    counter_ticker TEXT,
    ticker_return_pct REAL,
    benchmark_return_pct REAL,
    excess_return_pct REAL,
    counter_return_pct REAL,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE coach_pings (
    id INTEGER PRIMARY KEY,
    class_ VARCHAR(32) NOT NULL,
    key VARCHAR(128) NOT NULL,
    ticker VARCHAR(16),
    body TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    source_ref VARCHAR(128),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE decision_nudges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER,
    status TEXT NOT NULL DEFAULT 'sent',
    sent_at TEXT NOT NULL,
    awaiting_until TEXT,
    filled_at TEXT
);

CREATE TABLE owner_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    provenance TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    affirmed_at TEXT,
    review_horizon_days INTEGER,
    source_detail TEXT,
    created_at TEXT NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    superseded_by_id INTEGER
);

CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    fiscal_period VARCHAR(10),
    content_md TEXT,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    output_sha256 VARCHAR(64),
    model VARCHAR(64),
    prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    generated_at DATETIME NOT NULL,
    expires_at DATETIME,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0,
    dirty_reason VARCHAR(128),
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
);

CREATE TABLE llm_budgets (
    purpose TEXT PRIMARY KEY,
    monthly_cap_usd REAL NOT NULL,
    warn_threshold_pct REAL NOT NULL DEFAULT 0.80,
    hard_block INTEGER NOT NULL DEFAULT 0,
    on_exceed TEXT NOT NULL DEFAULT 'warn',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT
);
"""


def _config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_db(tmp_path: Path, *, name: str = "m.db") -> Path:
    db = tmp_path / name
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


def test_upgrade_adds_advice_artifact_column(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "advice_artifact_id" in cols
        # No FK/REFERENCES — plain integer, code-level RI only.
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
        ).fetchone()[0]
        assert (
            "REFERENCES" not in create_sql.upper()
            or "advice_artifact_id" not in create_sql.split("REFERENCES")[0].split("\n")[-1]
        )
    finally:
        conn.close()


def test_existing_decisions_rows_still_readable(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT ticker, recommendation_kind, advice_artifact_id FROM decisions WHERE ticker='NU'"
        ).fetchone()
        assert row == ("NU", "hold", None)
    finally:
        conn.close()


def test_v_decision_journal_exposes_advice_columns_and_preceded_flag(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # Artifact generated BEFORE the decision -> advice_preceded = 1.
        conn.execute(
            "INSERT INTO llm_artifacts (id, scope, purpose, input_sha256, generated_at) "
            "VALUES (1, 'portfolio', 'incremental_dollar_recommendation', 'sha1', "
            "'2026-07-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, scope, decided_by, "
            "advice_artifact_id, made_at, created_at) "
            "VALUES ('NVO','allocate','ticker','owner',1,'2026-07-02','2026-07-02')"
        )
        # Artifact generated AFTER the decision -> advice_preceded = 0.
        conn.execute(
            "INSERT INTO llm_artifacts (id, scope, purpose, input_sha256, generated_at) "
            "VALUES (2, 'portfolio', 'incremental_dollar_recommendation', 'sha2', "
            "'2026-07-10T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, scope, decided_by, "
            "advice_artifact_id, made_at, created_at) "
            "VALUES ('WIX','allocate','ticker','owner',2,'2026-07-05','2026-07-05')"
        )
        conn.commit()

        cols = {d[0] for d in conn.execute("SELECT * FROM v_decision_journal LIMIT 1").description}
        assert {
            "advice_artifact_id",
            "advice_purpose",
            "advice_created_at",
            "advice_preceded",
        } <= cols

        row_nvo = conn.execute(
            "SELECT advice_artifact_id, advice_purpose, advice_preceded "
            "FROM v_decision_journal WHERE ticker='NVO'"
        ).fetchone()
        assert row_nvo == (1, "incremental_dollar_recommendation", 1)

        row_wix = conn.execute(
            "SELECT advice_artifact_id, advice_preceded FROM v_decision_journal WHERE ticker='WIX'"
        ).fetchone()
        assert row_wix == (2, 0)

        row_nu = conn.execute(
            "SELECT advice_artifact_id, advice_preceded FROM v_decision_journal WHERE ticker='NU'"
        ).fetchone()
        assert row_nu == (None, None)
    finally:
        conn.close()


def test_llm_budget_seeded_block_mode(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT monthly_cap_usd, warn_threshold_pct, hard_block, on_exceed "
            "FROM llm_budgets WHERE purpose='incremental_dollar_recommendation'"
        ).fetchone()
        assert row == (10.0, 0.80, 1, "block")
    finally:
        conn.close()


def test_upgrade_idempotent(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path)
    cfg = _config(db)
    # Re-stamp and re-run: no crash, no duplicate budget row, column unchanged.
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, HEAD)
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM llm_budgets WHERE purpose='incremental_dollar_recommendation'"
        ).fetchone()[0]
        assert n == 1
        cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()]
        assert cols.count("advice_artifact_id") == 1
    finally:
        conn.close()


def test_downgrade_refuses_when_advice_linked_else_clean(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path, name="clean.db")
    cfg = _config(db)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "advice_artifact_id" not in cols
        budget = conn.execute(
            "SELECT 1 FROM llm_budgets WHERE purpose='incremental_dollar_recommendation'"
        ).fetchone()
        assert budget is None
    finally:
        conn.close()


def test_downgrade_refuses_with_linked_row(tmp_path: Path) -> None:
    db = _migrated_db(tmp_path, name="linked.db")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO llm_artifacts (id, scope, purpose, input_sha256, generated_at) "
            "VALUES (9, 'portfolio', 'incremental_dollar_recommendation', 'sha9', "
            "'2026-07-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO decisions (ticker, recommendation_kind, scope, decided_by, "
            "advice_artifact_id, made_at, created_at) "
            "VALUES ('META','allocate','ticker','owner',9,'2026-07-02','2026-07-02')"
        )
        conn.commit()
    finally:
        conn.close()
    cfg = _config(db)
    try:
        command.downgrade(cfg, PRIOR_HEAD)
        raise AssertionError("downgrade should refuse when a decision carries an advice link")
    except RuntimeError as exc:
        assert "advice_artifact_id" in str(exc)
