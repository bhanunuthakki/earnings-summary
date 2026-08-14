"""Migration contract for owner-facing thesis-evaluation episodes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0013_add_readme_update_budgets"
HEAD = "0014_add_thesis_evaluation_episodes"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _rule_payload(*, threshold: str, evaluated_at: str) -> str:
    return json.dumps(
        [
            {
                "rule_id": "growth-floor",
                "threshold": threshold,
                "status": "warn",
                "evaluated_at": evaluated_at,
                "observations": [
                    {
                        "period_end": "2026-06-30",
                        "value": "11.0",
                        "unit": "percent",
                    }
                ],
            }
        ]
    )


def test_0014_backfills_all_wix_rows_into_two_partial_legacy_episodes(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "episodes.db"

    def seed(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            for index in range(34):
                run_id = f"wix-run-{index + 1}"
                evaluated_at = (datetime(2026, 7, 1, 8) + timedelta(days=index)).isoformat()
                connection.execute(
                    "INSERT INTO ingestion_runs "
                    "(run_id,started_at,directive,ticker_scope,status) VALUES (?,?,?,?,?)",
                    (run_id, "2026-07-01T08:00:00", "test", '["WIX"]', "ok"),
                )
                threshold = "10" if index < 8 else "12"
                connection.execute(
                    "INSERT INTO thesis_evaluations "
                    "(ticker,evaluated_at,overall_status,rule_evaluations_json,"
                    "soft_rule_results_json,run_id) VALUES (?,?,?,?,?,?)",
                    (
                        "WIX",
                        evaluated_at,
                        "warn",
                        _rule_payload(threshold=threshold, evaluated_at=evaluated_at),
                        "[]",
                        run_id,
                    ),
                )
            connection.commit()

    migrated_db(database, upgrade_from=PRIOR_HEAD, before_upgrade=seed)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        episodes = connection.execute(
            "SELECT fingerprint_policy_version,provenance_completeness,overall_status,"
            "duplicate_run_count+1 AS occurrence_count,duplicate_run_count "
            "FROM thesis_evaluation_episodes "
            "WHERE ticker='WIX' ORDER BY occurrence_count"
        ).fetchall()
        assert episodes == [
            ("legacy_v0", "partial", "warn", 8, 7),
            ("legacy_v0", "partial", "warn", 26, 25),
        ]
        assert connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT evaluation_id) FROM thesis_evaluation_episode_members"
        ).fetchone() == (34, 34)
        assert connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluations WHERE ticker='WIX'"
        ).fetchone() == (34,)
        compatibility = connection.execute(
            "SELECT ticker,overall_status,occurrence_count "
            "FROM v_thesis_evaluation_history WHERE ticker='WIX' "
            "ORDER BY occurrence_count"
        ).fetchall()
        assert compatibility == [("WIX", "warn", 8), ("WIX", "warn", 26)]


def test_0014_backfill_keeps_unresolved_as_a_real_episode(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "unresolved.db"

    def seed(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO ingestion_runs "
                "(run_id,started_at,directive,ticker_scope,status) "
                "VALUES ('nu-run-1','2026-08-01T08:00:00','test','[\"NU\"]','ok')"
            )
            connection.execute(
                "INSERT INTO thesis_evaluations "
                "(ticker,evaluated_at,overall_status,rule_evaluations_json,run_id) "
                "VALUES ('NU','2026-08-01T08:00:00','unresolved','[]','nu-run-1')"
            )
            connection.commit()

    migrated_db(database, upgrade_from=PRIOR_HEAD, before_upgrade=seed)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT overall_status,provenance_completeness FROM thesis_evaluation_episodes"
        ).fetchone() == ("unresolved", "partial")


def test_0014_view_keeps_unmapped_legacy_writes_visible(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "unmapped.db", target=HEAD)
    with sqlite3.connect(database) as connection:
        cursor = connection.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker,evaluated_at,overall_status,rule_evaluations_json) "
            "VALUES ('NU','2026-08-14T12:00:00','ok','[]')"
        )
        raw_id = int(cursor.lastrowid or 0)
        row = connection.execute(
            "SELECT id,ticker,overall_status,fingerprint_policy_version,"
            "occurrence_count FROM v_thesis_evaluation_history WHERE id=?",
            (raw_id,),
        ).fetchone()
    assert row == (raw_id, "NU", "ok", "legacy_unmapped", 1)


def test_0014_downgrade_removes_episode_projection_without_touching_raw_history(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "downgrade.db"
    config = _config(database)

    def seed(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO thesis_evaluations "
                "(ticker,evaluated_at,overall_status,rule_evaluations_json) "
                "VALUES ('WIX','2026-08-01T08:00:00','warn','[]')"
            )
            connection.commit()

    migrated_db(database, upgrade_from=PRIOR_HEAD, before_upgrade=seed)
    command.downgrade(config, PRIOR_HEAD)

    with sqlite3.connect(database) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        assert "thesis_evaluation_episodes" not in names
        assert "thesis_evaluation_episode_members" not in names
        assert "thesis_evaluation_episode_check_receipts" not in names
        assert "v_thesis_evaluation_history" not in names
        assert connection.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone() == (1,)
