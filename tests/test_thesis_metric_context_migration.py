"""Additive v3 context protection preserves historical thesis receipts."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from alembic import command

_PRIOR = "0051_thesis_check_context"
_CURRENT = "0052_thesis_metric_check_context_guard"
_AT = "2026-10-03T00:00:00+00:00"
_TABLES = (
    "thesis_evaluations",
    "thesis_evaluation_episodes",
    "thesis_evaluation_episode_members",
    "thesis_evaluation_episode_check_receipts",
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _episode(connection: sqlite3.Connection, name: str, version: str) -> None:
    connection.execute(
        "INSERT INTO thesis_evaluation_episodes "
        "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
        "semantic_input_sha256,evaluator_semantic_version,result_sha256,overall_status,"
        "provenance_completeness,first_evaluated_at,last_seen_at,last_checked_at,"
        "rule_evaluations_json,created_at) "
        "VALUES (?,'SYNTH','forward_v1','{}',?,? ,?,'unresolved','partial',?,?,?,'[]',?)",
        (name, _sha(name), version, _sha("result:" + name), _AT, _AT, _AT, _AT),
    )


def _receipt(
    connection: sqlite3.Connection,
    name: str,
    episode: str,
    *,
    context_json: str | None = None,
    context_sha256: str | None = None,
    before_context_columns: bool = False,
) -> None:
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES (?,?,'migration-test','[]','ok')",
        (name, _AT),
    )
    columns = (
        "receipt_id,idempotency_key_sha256,episode_id,ticker,run_id,checked_at,outcome,"
        "semantic_input_sha256,result_sha256,receipt_sha256"
    )
    values: tuple[object, ...] = (
        name,
        _sha("key:" + name),
        episode,
        "SYNTH",
        name,
        _AT,
        "created",
        _sha(episode),
        _sha("result:" + episode),
        _sha("receipt:" + name),
    )
    if not before_context_columns:
        columns += ",context_json,context_sha256"
        values += (context_json, context_sha256)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO thesis_evaluation_episode_check_receipts ({columns}) VALUES ({placeholders})",  # nosec B608 -- closed columns and bound values
        values,
    )


def _rows(connection: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    return {
        table: list(connection.execute(f"SELECT * FROM {table} ORDER BY 1"))  # nosec B608 -- closed table list
        for table in _TABLES
    }


def _shape_trigger(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_thesis_check_context_shape'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def _append_only_triggers(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return list(
        connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE '%_no_update' OR name LIKE '%_no_delete') ORDER BY name"
        )
    )


def test_direct_v3_missing_context_insert_is_refused(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    """This fails on 0051: its insert trigger admits context-free v3 receipts."""
    database = migrated_db(tmp_path / "v3-guard.db")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _episode(connection, "v3", "thesis-evaluator/v3")
        with pytest.raises(sqlite3.IntegrityError, match="context required"):
            _receipt(connection, "missing-v3", "v3")


def test_context_guard_forward_reverse_preserves_actual_historical_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    prior_rows: dict[str, list[tuple[Any, ...]]] = {}

    def seed_before_context_columns(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            _episode(connection, "historical-v1", "thesis-evaluator/v1")
            _receipt(connection, "historical", "historical-v1", before_context_columns=True)
            connection.execute(
                "INSERT INTO thesis_evaluations "
                "(id,ticker,evaluated_at,overall_status,rule_evaluations_json,run_id) "
                "VALUES (1,'SYNTH',?,'unresolved','[]','historical')",
                (_AT,),
            )
            connection.execute(
                "INSERT INTO thesis_evaluation_episode_members "
                "(episode_id,evaluation_id,membership_role,member_ordinal,recorded_at) "
                "VALUES ('historical-v1',1,'anchor',1,?)",
                (_AT,),
            )
            prior_rows.update(_rows(connection))

    database = migrated_db(
        tmp_path / "historical.db",
        target=_PRIOR,
        upgrade_from="0050_kpi_fact_supersedes_lookup_index",
        before_upgrade=seed_before_context_columns,
    )
    with sqlite3.connect(database) as connection:
        expected = dict(prior_rows)
        expected["thesis_evaluation_episode_check_receipts"] = [
            (*row, None, None) for row in prior_rows["thesis_evaluation_episode_check_receipts"]
        ]
        assert _rows(connection) == expected
        prior_trigger = _shape_trigger(connection)
        append_only = _append_only_triggers(connection)
        _episode(connection, "pre-guard-v3", "thesis-evaluator/v3")
        _receipt(connection, "pre-guard-v3", "pre-guard-v3")
        before_upgrade = _rows(connection)

    migrated_db(database, target=_CURRENT, upgrade_existing=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _CURRENT,
        )
        assert _rows(connection) == before_upgrade
        assert _append_only_triggers(connection) == append_only
        for version in ("v1", "v2", "v3"):
            _episode(connection, version, "thesis-evaluator/" + version)
            if version == "v1":
                _receipt(connection, "legacy-no-context", version)
            else:
                with pytest.raises(sqlite3.IntegrityError, match="context required"):
                    _receipt(connection, "missing-" + version, version)
            for label, text, digest in (("json-only", "{}", None), ("hash-only", None, _sha("{}"))):
                with pytest.raises(sqlite3.IntegrityError, match="context required"):
                    _receipt(
                        connection,
                        label + version,
                        version,
                        context_json=text,
                        context_sha256=digest,
                    )
            _receipt(
                connection,
                "complete-" + version,
                version,
                context_json="{}",
                context_sha256=_sha("{}"),
            )
        for label, text, digest in (
            ("non-object", "[]", _sha("[]")),
            ("invalid-json", "{", _sha("{")),
            ("short-hash", "{}", "0"),
            ("uppercase-hash", "{}", "A" * 64),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                _receipt(connection, label, "v3", context_json=text, context_sha256=digest)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE thesis_evaluation_episode_check_receipts SET context_json='{}'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM thesis_evaluation_episode_check_receipts")
        before_downgrade = _rows(connection)

    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.downgrade(config, _PRIOR)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_PRIOR,)
        assert _rows(connection) == before_downgrade
        assert _shape_trigger(connection) == prior_trigger
        assert _append_only_triggers(connection) == append_only
        _receipt(connection, "restored-v3-without-context", "v3")
        with pytest.raises(sqlite3.IntegrityError, match="context required"):
            _receipt(connection, "restored-v2-missing-context", "v2")
        with pytest.raises(sqlite3.IntegrityError, match="context required"):
            _receipt(connection, "restored-v3-unpaired", "v3", context_json="{}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM thesis_evaluation_episode_check_receipts")
