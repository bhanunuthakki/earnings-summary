from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "0063_document_product_completion"
RETIRED_TABLES = {
    "pending_telegram_replies",
    "coach_pings",
    "coach_mutes",
    "decision_nudges",
    "weekly_packet_items",
    "weekly_packet_runs",
}


def _migration() -> ModuleType:
    path = ROOT / "alembic/versions/0064_retired_capture_coaching_stores.py"
    spec = importlib.util.spec_from_file_location("retired_capture_coaching_stores", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(database: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            _migration().upgrade()
    finally:
        engine.dispose()


def _seed_retired_history(connection: sqlite3.Connection) -> int:
    connection.execute(
        "INSERT INTO decisions "
        "(ticker,recommendation_kind,recommendation_value,conviction,decided_by,made_at,"
        "created_at,user_action_kind,user_acted_at,outcome_label,outcome_pct,outcome_at,"
        "process_quality) VALUES "
        "('SYNTH','position',1.0,'medium','owner','2026-01-01','2026-01-01',"
        "'hold','2026-01-02','gain',12.5,'2026-04-01','sound')"
    )
    decision_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO pending_telegram_replies "
        "(chat_id,kind,ref_id,expires_at,created_at) "
        "VALUES (1,'decision',?, '2026-01-03','2026-01-01')",
        (decision_id,),
    )
    connection.execute(
        "INSERT INTO coach_mutes (class_,muted_at,reason) "
        "VALUES ('retired','2026-01-01','synthetic fixture')"
    )
    connection.execute(
        "INSERT INTO coach_pings "
        '(class_,"key",ticker,body,status,source_ref,created_at,updated_at) '
        "VALUES ('retired','synthetic','SYNTH','synthetic ping','sent',NULL,"
        "'2026-01-01','2026-01-01')"
    )
    connection.execute(
        "INSERT INTO decision_nudges (decision_id,chat_id,status,sent_at) "
        "VALUES (?,1,'sent','2026-01-01')",
        (decision_id,),
    )
    connection.execute(
        "INSERT INTO weekly_packet_runs "
        "(iso_year,iso_week,status,total_items,created_at,updated_at,completed_at) "
        "VALUES (2026,1,'complete',1,'2026-01-01','2026-01-02','2026-01-02')"
    )
    run_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO weekly_packet_items "
        "(run_id,item_kind,ref_id,ticker,title,verdict,acted_at,order_index,created_at) "
        "VALUES (?, 'decision', ?, 'SYNTH', 'Synthetic acted decision', 'hold',"
        "'2026-01-02',0,'2026-01-01')",
        (run_id, decision_id),
    )
    return decision_id


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_upgrade_drops_only_retired_history_and_preserves_decision_series(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "retired-stores.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        decision_id = _seed_retired_history(connection)
        decision_before = connection.execute(
            "SELECT id,ticker,user_action_kind,user_acted_at,outcome_label,outcome_pct,"
            "outcome_at,process_quality FROM decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        journal_before = connection.execute(
            "SELECT decision_id,ticker,user_action_kind,user_acted_at,outcome_label,"
            "outcome_pct,outcome_at,process_quality FROM v_decision_journal WHERE decision_id=?",
            (decision_id,),
        ).fetchone()

    _apply(database)

    with sqlite3.connect(database) as connection:
        assert RETIRED_TABLES.isdisjoint(_table_names(connection))
        assert (
            connection.execute(
                "SELECT id,ticker,user_action_kind,user_acted_at,outcome_label,outcome_pct,"
                "outcome_at,process_quality FROM decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            == decision_before
        )
        assert (
            connection.execute(
                "SELECT decision_id,ticker,user_action_kind,user_acted_at,outcome_label,"
                "outcome_pct,outcome_at,process_quality FROM v_decision_journal WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            == journal_before
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upgrade_refuses_unknown_incoming_foreign_key_without_partial_drop(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "retired-stores-fk.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE retained_extension (id INTEGER PRIMARY KEY, nudge_id INTEGER "
            "REFERENCES decision_nudges(id))"
        )

    with pytest.raises(RuntimeError, match="unexpected foreign-key dependency"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert _table_names(connection) >= RETIRED_TABLES


def test_upgrade_refuses_view_dependency_without_partial_drop(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "retired-stores-view.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIEW retained_coach_extension AS SELECT id FROM coach_pings")

    with pytest.raises(RuntimeError, match="still references retired table"):
        _apply(database)

    with sqlite3.connect(database) as connection:
        assert _table_names(connection) >= RETIRED_TABLES


@pytest.mark.parametrize("retired_table", ["coach_pings", "decision_nudges"])
def test_upgrade_refuses_retained_trigger_dependency_before_any_drop(
    tmp_path: Path, migrated_db: Callable[..., Path], retired_table: str
) -> None:
    database = migrated_db(tmp_path / "retired-stores-trigger.db", target=PREDECESSOR)
    with sqlite3.connect(database) as connection:
        _seed_retired_history(connection)
        connection.execute(
            "CREATE TRIGGER retained_decision_extension BEFORE INSERT ON decisions "
            f'BEGIN SELECT COUNT(*) FROM "{retired_table}"; END'
        )
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in RETIRED_TABLES | {"decisions"}
        }

    with pytest.raises(RuntimeError, match=r"trigger .* still references retired table"):
        migrated_db(database, upgrade_existing=True, target="0064_retired_capture_coaching_stores")

    with sqlite3.connect(database) as connection:
        assert _table_names(connection) >= RETIRED_TABLES
        assert {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in before
        } == before
