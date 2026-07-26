"""Round-trip tests for 0208 (transcripts.source) and 0209
(UNIQUE(ticker, fiscal_period_type, period_end) on transcripts) — the
2026-07-25 transcript-duplication incident fix. Numbered 0208/0209 (not
0206/0207) because origin/main's 0206_llm_calls_trace_context claimed that
number first.

Built with the real chain (init_db + alembic), like test_migration_0178's
comp_set_metrics_daily.locator test: `db.init_db()` creates the baseline
tables `0000_baseline` only records as already-established, then alembic
runs 0001 forward for real. The at-PRIOR_HEAD state is built once per
module and copied per test to avoid re-running ~200 migrations per test.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0206_llm_calls_trace_context"
SOURCE_HEAD = "0208_transcripts_source"
UNIQUE_HEAD = "0209_transcripts_period_unique"

_TABLE = "transcripts"

_INSERT = (
    "INSERT INTO transcripts "
    "(document_id, ticker, fiscal_period_type, period_end) "
    "VALUES ({document_id}, 'NVDA', 'Q1', '2025-03-31')"
)


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()


def _indexes(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA index_list({_TABLE})")}
    finally:
        conn.close()


def _seed_document(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "INSERT INTO documents "
            "(ticker, source_type, doc_type, file_path, sha256, fetched_at, "
            " fetch_status, raw_bytes_size) "
            "VALUES ('NVDA', 'transcript_audio', 'earnings_call_transcript', "
            " 'transcripts/processed/NVDA_Q1_2025.pdf', ?, '2025-01-01', 'ok', 100)",
            (f"sha_{datetime.now().isoformat()}_{id(conn)}",),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("transcripts_source_tmpl") / "at_0206.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "transcripts_source.db"
    shutil.copy(prior_template, db)
    return db


# ---------------------------------------------------------------------------
# 0208 — source column
# ---------------------------------------------------------------------------


def test_prior_head_lacks_source_column(db_at_prior: Path) -> None:
    cols = _columns(db_at_prior)
    assert cols  # transcripts already exists at baseline
    assert "source" not in cols


def test_upgrade_adds_source_column(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), SOURCE_HEAD)
    assert "source" in _columns(db_at_prior)


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, SOURCE_HEAD)
    command.upgrade(cfg, SOURCE_HEAD)  # must not raise
    assert "source" in _columns(db_at_prior)


def test_existing_rows_survive_upgrade_with_null_source(db_at_prior: Path) -> None:
    doc_id = _seed_document(db_at_prior)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(_INSERT.format(document_id=doc_id))
    conn.commit()
    conn.close()

    command.upgrade(_build_config(db_at_prior), SOURCE_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    row = conn.execute("SELECT ticker, source FROM transcripts").fetchone()
    conn.close()
    assert row == ("NVDA", None)


def test_downgrade_drops_source_column(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, SOURCE_HEAD)
    assert "source" in _columns(db_at_prior)
    command.downgrade(cfg, PRIOR_HEAD)
    assert "source" not in _columns(db_at_prior)


# ---------------------------------------------------------------------------
# 0209 — UNIQUE(ticker, fiscal_period_type, period_end)
# ---------------------------------------------------------------------------


def test_upgrade_adds_unique_index(db_at_prior: Path) -> None:
    command.upgrade(_build_config(db_at_prior), UNIQUE_HEAD)
    assert "uq_transcripts_ticker_period_type_end" in _indexes(db_at_prior)


def test_unique_index_rejects_duplicate_period(db_at_prior: Path) -> None:
    """The exact invariant the 2026-07-25 incident violated: two rows for the
    same (ticker, fiscal_period_type, period_end) must now be impossible."""
    command.upgrade(_build_config(db_at_prior), UNIQUE_HEAD)

    doc_id_1 = _seed_document(db_at_prior)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(_INSERT.format(document_id=doc_id_1))
    conn.commit()
    conn.close()

    doc_id_2 = _seed_document(db_at_prior)
    conn = sqlite3.connect(str(db_at_prior))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT.format(document_id=doc_id_2))
            conn.commit()
    finally:
        conn.close()


def test_unique_migration_fails_loudly_on_preexisting_duplicates(db_at_prior: Path) -> None:
    """Upgrading straight to 0209 over stale duplicate rows must fail (not
    silently create an index that can't actually hold) — the cleanup sweep
    (execution/dedupe_transcripts.py) must run first, same order this
    incident's fix required against the real portfolio.db."""
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, SOURCE_HEAD)

    doc_id_1 = _seed_document(db_at_prior)
    doc_id_2 = _seed_document(db_at_prior)
    conn = sqlite3.connect(str(db_at_prior))
    conn.execute(_INSERT.format(document_id=doc_id_1))
    conn.execute(_INSERT.format(document_id=doc_id_2))
    conn.commit()
    conn.close()

    # command.upgrade runs through SQLAlchemy's engine, which wraps the
    # underlying sqlite3.IntegrityError as sqlalchemy.exc.IntegrityError.
    with pytest.raises(sa.exc.IntegrityError):
        command.upgrade(cfg, UNIQUE_HEAD)


def test_downgrade_drops_unique_index(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, UNIQUE_HEAD)
    command.downgrade(cfg, SOURCE_HEAD)
    assert "uq_transcripts_ticker_period_type_end" not in _indexes(db_at_prior)
