"""Round-trip + enforcement tests for ``0140_advisor_memos_position_review_kind``.

The migration widens ``ck_advisor_memos_kind`` (0077's named CHECK) to admit
'position_review' — a memo kind ``advisor.store.MEMO_KINDS`` has carried since
the position-review service landed, but 0077's constraint never caught up
(the prod IntegrityError from the 2026-07-02 adversarial review + live smoke
test). Built with the real chain like the 0076 test, so the batch_alter
recreation runs against the actual production table shape (the two ix_*
indexes, the score_status + stance CHECKs) rather than a hand-rolled
approximation that could drift.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0077_advisor_memos"
NEW_HEAD = "0140_advisor_memos_position_review_kind"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real pre-0140 head via init_db + alembic, shared
    (read-only) across the module and copied per test."""
    db = tmp_path_factory.mktemp("position_review_kind_tmpl") / "at_0077.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "position_review_kind.db"
    shutil.copy(prior_template, db)
    return db


def _insert(conn: sqlite3.Connection, kind: str, ticker: str | None = None) -> None:
    conn.execute(
        "INSERT INTO advisor_memos (user_id, kind, ticker, title, body_md, created_at)"
        " VALUES ('bhanu', ?, ?, 't', 'b', '2026-07-02T00:00:00')",
        (kind, ticker),
    )


def test_upgrade_keeps_rows_and_admits_position_review(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    _insert(conn, "next_dollar")
    _insert(conn, "swap_check", "NU")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="ck_advisor_memos_kind"):
        _insert(conn, "position_review", "RBRK")  # not yet widened
    conn.rollback()
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    assert conn.execute("SELECT COUNT(*) FROM advisor_memos").fetchone()[0] == 2
    # The batch recreation must preserve both query indexes.
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='advisor_memos'"
        )
    }
    assert "ix_advisor_memos_user_kind_created" in names
    assert "ix_advisor_memos_user_score_status" in names
    _insert(conn, "position_review", "RBRK")  # now accepted
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="ck_advisor_memos_kind"):
        _insert(conn, "bogus", "RBRK")
    conn.rollback()
    conn.close()


def test_downgrade_drops_position_review_when_no_rows_use_it(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    with pytest.raises(sqlite3.IntegrityError, match="ck_advisor_memos_kind"):
        _insert(conn, "position_review", "RBRK")  # constraint restored -> rejected
    conn.rollback()
    conn.close()


def test_downgrade_refuses_to_destroy_position_review_rows(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    _insert(conn, "position_review", "RBRK")
    conn.commit()
    conn.close()

    command.downgrade(cfg, PRIOR_HEAD)  # never destructive — leaves the widened CHECK

    conn = sqlite3.connect(str(db_at_prior))
    _insert(conn, "position_review", "FLKR")  # still accepted: downgrade was a no-op here
    conn.commit()
    conn.close()
