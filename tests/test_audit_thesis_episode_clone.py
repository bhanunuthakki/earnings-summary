"""Source-bound receipt for the thesis episode clone migration."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from execution.audit_thesis_episode_clone import (
    CloneMigrationReceipt,
    audit_clone_migration,
    main,
)
from sqlite_snapshot import SnapshotRequest, create_snapshot

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0013_add_readme_update_budgets"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _seed_wix(path: Path) -> None:
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
            rules = json.dumps(
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
            connection.execute(
                "INSERT INTO thesis_evaluations "
                "(ticker,evaluated_at,overall_status,rule_evaluations_json,"
                "soft_rule_results_json,run_id) VALUES (?,?,?,?,?,?)",
                ("WIX", evaluated_at, "warn", rules, "[]", run_id),
            )
        connection.commit()


def _source_and_clone(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path]:
    live_like = tmp_path / "live-like.db"
    migrated_db(live_like, target=PRIOR_HEAD)
    _seed_wix(live_like)
    source_snapshot = tmp_path / "source-snapshot.db"
    snapshot = create_snapshot(
        SnapshotRequest(source_path=live_like, destination_path=source_snapshot)
    )
    clone = tmp_path / "migrated-clone.db"
    shutil.copy2(source_snapshot, clone)
    command.upgrade(_config(clone), "head")
    return snapshot.manifest_path, clone


def test_clone_receipt_proves_wix_rows_unchanged_and_grouped(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, clone = _source_and_clone(tmp_path, migrated_db)

    receipt = audit_clone_migration(
        source_snapshot_manifest=manifest,
        migrated_clone_db=clone,
    )

    assert receipt.verified is True
    assert receipt.blocking_reasons == ()
    assert receipt.source_raw_row_count == 34
    assert receipt.clone_raw_row_count == 34
    assert receipt.raw_rows_unchanged is True
    assert receipt.legacy_episode_count == 2
    assert sorted(episode.occurrence_count for episode in receipt.legacy_episodes) == [8, 26]
    assert receipt.membership_count == 34
    assert receipt.distinct_membership_count == 34
    assert receipt.every_source_row_mapped_once is True
    assert receipt.point_in_time is True
    assert receipt.authorizes_live_database_change is False

    assert (
        main(
            [
                "--source-snapshot-manifest",
                str(manifest),
                "--migrated-clone-db",
                str(clone),
            ]
        )
        == 0
    )
    cli_receipt = CloneMigrationReceipt.model_validate_json(capsys.readouterr().out)
    assert cli_receipt.verified is True


def test_clone_receipt_fails_closed_on_an_extra_raw_row(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    manifest, clone = _source_and_clone(tmp_path, migrated_db)
    with sqlite3.connect(clone) as connection:
        connection.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker,evaluated_at,overall_status,rule_evaluations_json) "
            "VALUES ('WIX','2026-08-14T12:00:00','warn','[]')"
        )
        connection.commit()

    receipt = audit_clone_migration(
        source_snapshot_manifest=manifest,
        migrated_clone_db=clone,
    )

    assert receipt.verified is False
    assert receipt.raw_rows_unchanged is False
    assert "clone raw row count does not match expectation" in receipt.blocking_reasons
    assert "raw thesis evaluation rows changed during clone migration" in receipt.blocking_reasons


def test_clone_receipt_rejects_source_snapshot_as_migrated_clone(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    manifest, _clone = _source_and_clone(tmp_path, migrated_db)
    source_path = Path(json.loads(manifest.read_text(encoding="utf-8"))["snapshot"]["path"])

    with pytest.raises(ValueError, match="distinct file"):
        audit_clone_migration(
            source_snapshot_manifest=manifest,
            migrated_clone_db=source_path,
        )
