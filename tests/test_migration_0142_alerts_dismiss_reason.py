"""Round-trip tests for ``0142_alerts_dismiss_reason``.

Built with the real chain (init_db + alembic), like the 0140 migration test,
so the ADD COLUMN runs against the actual production ``alerts`` table shape
rather than a hand-rolled approximation. Plain nullable TEXT column, no FK,
no CHECK — the simplest possible migration, but still round-tripped: upgrade
adds the column and preserves existing rows, downgrade removes it cleanly
when the column carries no data (never destructive when it does)."""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

PRIOR_HEAD = "0141_reader_perf_indexes"
NEW_HEAD = "0142_alerts_dismiss_reason"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def prior_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One DB built to the real pre-0142 head via init_db + alembic, shared
    (read-only) across the module and copied per test."""
    db = tmp_path_factory.mktemp("dismiss_reason_tmpl") / "at_0141.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, PRIOR_HEAD)
    return db


@pytest.fixture
def db_at_prior(prior_template: Path, tmp_path: Path) -> Path:
    db = tmp_path / "dismiss_reason.db"
    shutil.copy(prior_template, db)
    return db


def _insert_alert(conn: sqlite3.Connection, *, signature: str) -> int:
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    cur = conn.execute(
        "INSERT INTO alerts (user_id, ticker, trigger_kind, fired_at, status, "
        "evidence_json, signature_sha) "
        "VALUES ('bhanu', 'NU', 'earnings_tone', ?, 'pending', '{}', ?)",
        (now, signature),
    )
    return int(cur.lastrowid)


def test_alerts_lacks_dismiss_reason_before_upgrade(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    conn.close()
    assert "dismiss_reason" not in cols


def test_upgrade_adds_column_and_preserves_rows(db_at_prior: Path) -> None:
    conn = sqlite3.connect(str(db_at_prior))
    _insert_alert(conn, signature="sig-a")
    _insert_alert(conn, signature="sig-b")
    conn.commit()
    conn.close()

    command.upgrade(_build_config(db_at_prior), NEW_HEAD)

    conn = sqlite3.connect(str(db_at_prior))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    assert "dismiss_reason" in cols
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
    # New column defaults to NULL on existing rows.
    values = [r[0] for r in conn.execute("SELECT dismiss_reason FROM alerts").fetchall()]
    assert values == [None, None]
    conn.close()


def test_upgrade_is_idempotent_on_a_rerun(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.upgrade(cfg, NEW_HEAD)  # re-running must not raise (guarded add)
    conn = sqlite3.connect(str(db_at_prior))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()]
    assert cols.count("dismiss_reason") == 1
    conn.close()


def test_dismiss_reason_round_trips_via_store(db_at_prior: Path) -> None:
    """The migration's whole point: alerts.store.dismiss_alert(reason=) can
    now persist and read back a reason."""
    command.upgrade(_build_config(db_at_prior), NEW_HEAD)
    import sys as _sys

    _sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from alerts.store import dismiss_alert, fire_alert, get_alert

    alert = fire_alert(
        ticker="NU",
        trigger_kind="earnings_tone",
        fired_at=datetime.now(UTC),
        evidence_json="{}",
        signature_sha="sig-roundtrip",
        db_path=db_at_prior,
    )
    dismissed = dismiss_alert(alert.id, db_path=db_at_prior, reason="already knew")
    assert dismissed.dismiss_reason == "already knew"
    assert get_alert(alert.id, db_path=db_at_prior).dismiss_reason == "already knew"


def test_downgrade_drops_column(db_at_prior: Path) -> None:
    cfg = _build_config(db_at_prior)
    command.upgrade(cfg, NEW_HEAD)
    command.downgrade(cfg, PRIOR_HEAD)
    conn = sqlite3.connect(str(db_at_prior))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    conn.close()
    assert "dismiss_reason" not in cols
