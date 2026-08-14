"""Source-bound receipt for the thesis episode clone migration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from execution.audit_thesis_episode_clone import (
    AuditExpectations,
    CloneMigrationReceipt,
    audit_clone_migration,
    main,
    write_pre_migration_clone_receipt,
)
from sqlite_snapshot import SnapshotRequest, create_snapshot

ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0013_add_readme_update_budgets"
EPISODE_HEAD = "0014_add_thesis_evaluation_episodes"


def _evidence_id(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _source_and_clone(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path, Path]:
    live_like = tmp_path / "live-like.db"
    migrated_db(live_like, target=PRIOR_HEAD)
    _seed_wix(live_like)
    source_snapshot = tmp_path / "source-snapshot.db"
    snapshot = create_snapshot(
        SnapshotRequest(source_path=live_like, destination_path=source_snapshot)
    )
    clone = tmp_path / "migrated-clone.db"
    shutil.copy2(source_snapshot, clone)
    custody_receipt = tmp_path / "pre-migration-clone-receipt.json"
    write_pre_migration_clone_receipt(
        source_snapshot_manifest=snapshot.manifest_path,
        clone_db=clone,
        output_path=custody_receipt,
    )
    migrated_db(clone, target=EPISODE_HEAD, upgrade_existing=True)
    return snapshot.manifest_path, clone, custody_receipt


def test_clone_receipt_proves_wix_rows_unchanged_and_grouped(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest, clone, custody_receipt = _source_and_clone(tmp_path, migrated_db)

    receipt = audit_clone_migration(
        source_snapshot_manifest=manifest,
        migrated_clone_db=clone,
        pre_migration_clone_receipt=custody_receipt,
    )

    assert receipt.verified is True
    assert receipt.blocking_reasons == ()
    assert receipt.source_raw_row_count == 34
    assert receipt.clone_raw_row_count == 34
    assert receipt.raw_rows_unchanged is True
    assert receipt.legacy_episode_count == 2
    assert receipt.expectations.expected_raw_rows == 34
    assert receipt.expectations.expected_legacy_episodes == 2
    assert receipt.expectations.expected_group_sizes == (8, 26)
    assert receipt.actual_group_sizes == (8, 26)
    assert (
        receipt.pre_migration_clone.clone_before_migration.sha256
        == receipt.pre_migration_clone.source_snapshot.sha256
        == receipt.source.snapshot.sha256
    )
    assert receipt.pre_migration_clone.manifest_sha256 == receipt.source.manifest_sha256
    assert receipt.evidence_id == _evidence_id(
        receipt.model_dump(mode="json", exclude={"evidence_id"})
    )
    assert sorted(episode.occurrence_count for episode in receipt.legacy_episodes) == [8, 26]
    assert receipt.semantic_identity_matches_source is True
    assert receipt.exact_membership_mapping is True
    assert receipt.membership_structure_valid is True
    assert receipt.per_episode_counts_match is True
    assert len(receipt.memberships) == 34
    assert all(member.mapping_matches for member in receipt.memberships)
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
                "--pre-migration-clone-receipt",
                str(custody_receipt),
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
    manifest, clone, custody_receipt = _source_and_clone(tmp_path, migrated_db)
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
        pre_migration_clone_receipt=custody_receipt,
    )

    assert receipt.verified is False
    assert receipt.raw_rows_unchanged is False
    assert "clone raw row count does not match expectation" in receipt.blocking_reasons
    assert "raw thesis evaluation rows changed during clone migration" in receipt.blocking_reasons


def test_clone_receipt_rejects_source_snapshot_as_migrated_clone(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    manifest, _clone, custody_receipt = _source_and_clone(tmp_path, migrated_db)
    source_path = Path(json.loads(manifest.read_text(encoding="utf-8"))["snapshot"]["path"])

    with pytest.raises(ValueError, match="distinct file"):
        audit_clone_migration(
            source_snapshot_manifest=manifest,
            migrated_clone_db=source_path,
            pre_migration_clone_receipt=custody_receipt,
        )


@pytest.mark.parametrize("mutation", ["membership", "semantic_hash", "count"])
def test_clone_receipt_fails_closed_on_semantic_or_membership_corruption(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    mutation: str,
) -> None:
    manifest, clone, custody_receipt = _source_and_clone(tmp_path, migrated_db)
    with sqlite3.connect(clone) as connection:
        if mutation == "membership":
            connection.execute("DROP TRIGGER trg_thesis_evaluation_episode_members_no_update")
            rows = connection.execute(
                "SELECT episode_id,evaluation_id FROM thesis_evaluation_episode_members "
                "WHERE membership_role='anchor' ORDER BY episode_id"
            ).fetchall()
            assert len(rows) == 2
            connection.execute(
                "UPDATE thesis_evaluation_episode_members SET evaluation_id=? "
                "WHERE episode_id=? AND evaluation_id=?",
                (-1, str(rows[0][0]), int(rows[0][1])),
            )
            connection.execute(
                "UPDATE thesis_evaluation_episode_members SET evaluation_id=? "
                "WHERE episode_id=? AND evaluation_id=?",
                (int(rows[0][1]), str(rows[1][0]), int(rows[1][1])),
            )
            connection.execute(
                "UPDATE thesis_evaluation_episode_members SET evaluation_id=? "
                "WHERE episode_id=? AND evaluation_id=-1",
                (int(rows[1][1]), str(rows[0][0])),
            )
        elif mutation == "semantic_hash":
            connection.execute(
                "UPDATE thesis_evaluation_episodes SET semantic_input_sha256=? "
                "WHERE episode_id=(SELECT episode_id FROM thesis_evaluation_episodes "
                "ORDER BY episode_id LIMIT 1)",
                ("a" * 64,),
            )
        else:
            connection.execute(
                "UPDATE thesis_evaluation_episodes SET duplicate_run_count=duplicate_run_count+1 "
                "WHERE episode_id=(SELECT episode_id FROM thesis_evaluation_episodes "
                "ORDER BY episode_id LIMIT 1)"
            )
        connection.commit()

    receipt = audit_clone_migration(
        source_snapshot_manifest=manifest,
        migrated_clone_db=clone,
        pre_migration_clone_receipt=custody_receipt,
    )

    assert receipt.verified is False
    if mutation == "membership":
        assert receipt.exact_membership_mapping is False
    elif mutation == "semantic_hash":
        assert receipt.semantic_identity_matches_source is False
    else:
        assert receipt.per_episode_counts_match is False


@pytest.mark.parametrize("field", ["schema_version", "code_config_version"])
def test_clone_receipt_rejects_unsupported_snapshot_manifest_contract(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    field: str,
) -> None:
    live_like = migrated_db(tmp_path / "live-like.db", target=PRIOR_HEAD)
    source_snapshot = tmp_path / "source-snapshot.db"
    snapshot = create_snapshot(
        SnapshotRequest(source_path=live_like, destination_path=source_snapshot)
    )
    manifest_payload = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    manifest_payload[field] = "unsupported/v999"
    snapshot.manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    clone = tmp_path / "clone.db"
    shutil.copy2(source_snapshot, clone)

    with pytest.raises(ValueError, match="unsupported snapshot manifest"):
        write_pre_migration_clone_receipt(
            source_snapshot_manifest=snapshot.manifest_path,
            clone_db=clone,
            output_path=tmp_path / "custody.json",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_raw_rows": 0, "expected_legacy_episodes": 1, "expected_group_sizes": (1,)},
        {
            "expected_raw_rows": 1,
            "expected_legacy_episodes": 0,
            "expected_group_sizes": (1,),
        },
        {
            "expected_raw_rows": 34,
            "expected_legacy_episodes": 2,
            "expected_group_sizes": (0, 34),
        },
        {
            "expected_raw_rows": 34,
            "expected_legacy_episodes": 2,
            "expected_group_sizes": (7, 26),
        },
    ],
)
def test_audit_expectations_are_positive_bounded_and_coherent(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AuditExpectations.model_validate(payload)


def test_clone_receipt_rejects_resealed_custody_for_a_different_manifest(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    manifest, clone, custody_receipt = _source_and_clone(tmp_path, migrated_db)
    payload = json.loads(custody_receipt.read_text(encoding="utf-8"))
    payload["manifest_sha256"] = "b" * 64
    evidence_payload = {key: value for key, value in payload.items() if key != "evidence_id"}
    payload["evidence_id"] = _evidence_id(evidence_payload)
    custody_receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="not bound to this manifest and clone"):
        audit_clone_migration(
            source_snapshot_manifest=manifest,
            migrated_clone_db=clone,
            pre_migration_clone_receipt=custody_receipt,
        )
