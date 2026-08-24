"""BHA-93 schema guarantees for mutable state and append-only evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from alembic.config import Config

from alembic import command
from alerts.store import TRIGGER_KINDS, fire_alert

ROOT = Path(__file__).resolve().parents[1]


def test_price_action_kind_and_sensor_tables_are_at_head(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "price-action-migration.db", target="head")
    assert "price_action" in TRIGGER_KINDS
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"price_action_sensor_state", "price_action_sensor_events"} <= tables
        try:
            conn.execute(
                "INSERT INTO alerts(user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) VALUES(?,?,?,?,?,?,?)",
                ("bhanu", "ACME", "not-a-kind", "2026-08-23T00:00:00", "pending", "{}", "a" * 64),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("alert trigger-kind check accepted an invalid kind")
        conn.execute(
            "INSERT INTO alerts(user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) VALUES(?,?,?,?,?,?,?)",
            ("bhanu", "ACME", "price_action", "2026-08-23T00:00:00", "pending", "{}", "b" * 64),
        )
        conn.execute(
            "INSERT OR IGNORE INTO price_action_sensor_events(event_key,user_id,ticker,ladder_id,ladder_revision_sha256,rung_id,generation,transition,observed_at,source_ref,source_sha256,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "f" * 64,
                "bhanu",
                "ACME",
                "ladder",
                "d" * 64,
                "trim",
                0,
                "breached",
                "2026-08-23T00:01:00",
                "dcf:2",
                "f" * 64,
                "{}",
                "2026-08-23T00:01:00",
            ),
        )
        assert conn.execute("SELECT count(*) FROM price_action_sensor_events").fetchone()[0] == 1
        conn.execute(
            "INSERT OR IGNORE INTO price_action_sensor_events(event_key,user_id,ticker,ladder_id,ladder_revision_sha256,rung_id,generation,transition,observed_at,source_ref,source_sha256,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c" * 64,
                "bhanu",
                "ACME",
                "ladder",
                "d" * 64,
                "trim",
                0,
                "breached",
                "2026-08-23T00:00:00",
                "dcf:1",
                "e" * 64,
                "{}",
                "2026-08-23T00:00:00",
            ),
        )
        try:
            conn.execute("DELETE FROM price_action_sensor_events WHERE event_key=?", ("f" * 64,))
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("price-action events permitted deletion")
        try:
            conn.execute(
                "UPDATE price_action_sensor_events SET transition='approaching' WHERE event_key=?",
                ("f" * 64,),
            )
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("price-action events permitted update")
    finally:
        conn.close()


def test_sensor_migration_round_trips_on_a_clean_database(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "price-action-round-trip.db", target="head")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.downgrade(cfg, "0022_add_governed_alert_action_receipts")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='price_action_sensor_events'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='price_action_sensor_events'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_fire_alert_joins_caller_transaction(
    migrated_db: Callable[..., Path], tmp_path: Path
) -> None:
    path = migrated_db(tmp_path / "price-action-transaction.db", target="head")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        alert = fire_alert(
            user_id="bhanu",
            ticker="ACME",
            trigger_kind="price_action",
            fired_at=datetime(2026, 8, 23),
            evidence_json="{}",
            signature_sha="a" * 64,
            conn=conn,
        )
        assert alert.id > 0
        assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0
    finally:
        conn.close()
