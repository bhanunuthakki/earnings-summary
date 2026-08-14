"""Migration contract for thesis-episode acknowledgement and delivery state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0014_add_thesis_evaluation_episodes"
HEAD = "0015_add_thesis_episode_attention"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_episode(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO thesis_evaluation_episodes "
            "(episode_id,ticker,fingerprint_policy_version,semantic_input_json,"
            "semantic_input_sha256,evaluator_semantic_version,result_sha256,"
            "overall_status,provenance_completeness,first_evaluated_at,last_seen_at,"
            "last_checked_at,duplicate_run_count,rule_evaluations_json,created_at) "
            "VALUES ('episode-1','WIX','legacy_v0','{}',?,'legacy/v0',?,'warn',"
            "'partial','2026-08-14T12:00:00+00:00','2026-08-14T12:00:00+00:00',"
            "'2026-08-14T12:00:00+00:00',0,'[]','2026-08-14T12:00:00+00:00')",
            ("a" * 64, "b" * 64),
        )
        connection.commit()


def test_0015_adds_attention_links_and_delivery_uniqueness(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "attention-migration.db"
    migrated_db(
        database,
        target=HEAD,
        upgrade_from=PRIOR_HEAD,
        before_upgrade=_seed_episode,
    )

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
        assert connection.execute(
            "SELECT attention_state FROM thesis_evaluation_episodes WHERE episode_id='episode-1'"
        ).fetchone() == ("unreviewed",)
        episode_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(thesis_evaluation_episodes)")
        }
        assert {
            "attention_state",
            "acknowledged_at",
            "acknowledgement_note",
            "next_review_at",
            "acted_on_decision_id",
            "superseded_by_episode_id",
            "attention_updated_at",
        } <= episode_columns
        connection.execute(
            "INSERT INTO thesis_evaluation_episode_delivery_receipts "
            "(receipt_id,episode_id,review_cycle_id,channel,surface,status,reserved_at) "
            "VALUES ('receipt-1','episode-1','initial','telegram','coach','reserved',"
            "'2026-08-14T12:00:00+00:00')"
        )
        connection.commit()
        with sqlite3.connect(database) as competing:
            competing.execute("PRAGMA foreign_keys=ON")
            try:
                competing.execute(
                    "INSERT INTO thesis_evaluation_episode_delivery_receipts "
                    "(receipt_id,episode_id,review_cycle_id,channel,surface,status,"
                    "reserved_at) VALUES ('receipt-2','episode-1','initial','telegram',"
                    "'coach','reserved','2026-08-14T12:01:00+00:00')"
                )
            except sqlite3.IntegrityError:
                pass
            else:  # pragma: no cover - explicit assertion branch
                raise AssertionError("duplicate delivery cycle unexpectedly inserted")


def test_0015_downgrade_preserves_episode_rows(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = tmp_path / "attention-downgrade.db"
    migrated_db(
        database,
        target=HEAD,
        upgrade_from=PRIOR_HEAD,
        before_upgrade=_seed_episode,
    )
    command.downgrade(_config(database), PRIOR_HEAD)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            PRIOR_HEAD,
        )
        assert connection.execute("SELECT COUNT(*) FROM thesis_evaluation_episodes").fetchone() == (
            1,
        )
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
        assert "thesis_evaluation_episode_delivery_receipts" not in names
        episode_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(thesis_evaluation_episodes)")
        }
        assert "attention_state" not in episode_columns
        assert "thesis_evaluation_episode_id" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(alerts)")
        }
