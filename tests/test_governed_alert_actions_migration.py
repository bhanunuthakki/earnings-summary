"""Schema contract for append-only governed alert action receipts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0021_managed_ir_publications"
HEAD = "0022_add_governed_alert_action_receipts"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_0022_adds_append_only_governed_alert_action_receipts(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "governed-actions-migration.db", target=HEAD)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(governed_alert_action_receipts)")
        }
        assert columns == {
            "receipt_id",
            "idempotency_key",
            "request_sha256",
            "actor",
            "alert_id",
            "source_ref",
            "evidence_ref",
            "action_type",
            "occurred_at",
            "note_sha256",
            "dismiss_reason_sha256",
            "defer_until",
            "decision_id",
            "replacement_episode_id",
            "result_state",
        }
        connection.execute(
            "INSERT INTO alerts "
            "(id,user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) "
            "VALUES (1,'bhanu','WIX','material_news','2026-08-23T12:00:00+00:00',"
            "'pending','{}',?)",
            ("d" * 64,),
        )
        connection.execute(
            "INSERT INTO governed_alert_action_receipts "
            "(receipt_id,idempotency_key,request_sha256,actor,alert_id,source_ref,evidence_ref,"
            "action_type,occurred_at,result_state) "
            "VALUES ('governed-alert-action:' || ?,'one',?,'owner',1,'alert:1',?,'review',"
            "'2026-08-23T12:00:00+00:00','reviewed')",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE governed_alert_action_receipts SET actor='other'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM governed_alert_action_receipts")


@pytest.mark.parametrize(
    (
        "idempotency_key",
        "action_type",
        "result_state",
        "note_sha256",
        "dismiss_reason_sha256",
        "defer_until",
        "decision_id",
        "replacement_episode_id",
    ),
    [
        ("dismiss-without-reason", "dismiss", "dismissed", None, None, None, None, None),
        ("defer-without-until", "defer", "deferred", None, None, None, None, None),
        ("complete-without-decision", "complete", "completed", None, None, None, None, None),
        (
            "supersede-without-replacement",
            "supersede",
            "superseded",
            None,
            None,
            None,
            None,
            None,
        ),
        ("review-with-note", "review", "reviewed", "e" * 64, None, None, None, None),
    ],
)
def test_0022_rejects_action_receipts_with_incoherent_optional_fields(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    idempotency_key: str,
    action_type: str,
    result_state: str,
    note_sha256: str | None,
    dismiss_reason_sha256: str | None,
    defer_until: str | None,
    decision_id: int | None,
    replacement_episode_id: str | None,
) -> None:
    database = migrated_db(tmp_path / f"{idempotency_key}.db", target=HEAD)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO alerts "
            "(id,user_id,ticker,trigger_kind,fired_at,status,evidence_json,signature_sha) "
            "VALUES (1,'bhanu','WIX','material_news','2026-08-23T12:00:00+00:00',"
            "'pending','{}',?)",
            ("d" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO governed_alert_action_receipts "
                "(receipt_id,idempotency_key,request_sha256,actor,alert_id,source_ref,"
                "evidence_ref,action_type,occurred_at,note_sha256,dismiss_reason_sha256,"
                "defer_until,decision_id,replacement_episode_id,result_state) "
                "VALUES ('governed-alert-action:' || ?,?,?,?,1,'alert:1',?,?,"
                "'2026-08-23T12:00:00+00:00',?,?,?,?,?,?)",
                (
                    "a" * 64,
                    idempotency_key,
                    "b" * 64,
                    "owner",
                    "c" * 64,
                    action_type,
                    note_sha256,
                    dismiss_reason_sha256,
                    defer_until,
                    decision_id,
                    replacement_episode_id,
                    result_state,
                ),
            )


def test_0022_downgrade_removes_only_its_receipt_table(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "governed-actions-downgrade.db", target=HEAD)
    command.downgrade(_config(database), PRIOR_HEAD)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PRIOR_HEAD,
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='governed_alert_action_receipts'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='thesis_evaluation_episode_delivery_receipts'"
            ).fetchone()
            is not None
        )
