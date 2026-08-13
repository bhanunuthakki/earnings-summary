from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import sec_delta_planner as planner
from pipeline.sec_delta_planner import (
    EvaluationAuthorization,
    SecDeltaPlannerRequest,
    SecDeltaPlanReceipt,
    SecDeltaPlanTerminalReceipt,
    SnapshotSafetyError,
    build_sec_delta_plan,
    write_sec_delta_plan,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_db(path: Path, *, complete_schema: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('{planner.SUPPORTED_ALEMBIC_REVISION}');
        CREATE TABLE tracked_companies (
            ticker TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TEXT
        );
        """
    )
    if complete_schema:
        conn.executescript(
            """
            CREATE TABLE source_inventory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                inventory_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                issuer_id TEXT NOT NULL,
                ticker TEXT,
                source_kind TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE source_inventory_snapshot_seals (
                snapshot_id TEXT PRIMARY KEY,
                expected_component_count INTEGER NOT NULL,
                component_digest_sha256 TEXT NOT NULL,
                completion_status TEXT NOT NULL,
                sealed_at TEXT NOT NULL
            );
            CREATE TABLE expected_documents (
                expected_document_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL,
                source_kind TEXT NOT NULL
            );
            CREATE TABLE source_coverage_assessments (
                assessment_id TEXT PRIMARY KEY,
                expected_document_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                coverage_status TEXT NOT NULL,
                document_version_id TEXT,
                recorded_at TEXT NOT NULL
            );
            """
        )
    conn.commit()
    conn.close()


def _seed_roster(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?,?,?)",
        (
            ("META", "portfolio", None),
            ("RBRK", "portfolio", None),
            ("IVN", "portfolio", None),
            ("ZZZZ", "portfolio", None),
            ("DUOL", "evaluation", None),
            ("WIX", "evaluation", None),
            ("AMD", "watchlist", None),
            ("GOOG", "index_member", None),
            ("ARCH", "portfolio", "2026-01-01"),
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?,?,?,?,?)",
        (
            "rbrk-sec-1",
            "sec-cik-0001943896:sec-submissions",
            1,
            "sec-cik-0001943896",
            "RBRK",
            "sec_submissions",
            "2026-08-11T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?,?,?,?)",
        ("rbrk-sec-1", 4, "a" * 64, "complete", "2026-08-11T00:00:00Z"),
    )
    conn.executemany(
        "INSERT INTO expected_documents VALUES (?,?,?)",
        (
            ("rbrk-doc-1", "rbrk-sec-1", "sec_filing"),
            ("rbrk-doc-2", "rbrk-sec-1", "sec_filing"),
            ("rbrk-doc-3", "rbrk-sec-1", "sec_filing"),
        ),
    )
    conn.execute(
        "INSERT INTO source_coverage_assessments VALUES (?,?,?,?,?,?)",
        (
            "rbrk-cov-1",
            "rbrk-doc-1",
            1,
            "captured",
            "rbrk-version-1",
            "2026-08-11T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def _request(path: Path, *evaluation: EvaluationAuthorization) -> SecDeltaPlannerRequest:
    return SecDeltaPlannerRequest(
        database_path=path,
        as_of=date(2026, 8, 12),
        evaluation_requests=tuple(evaluation),
    )


def _task(receipt: SecDeltaPlanReceipt, ticker: str, kind: str):
    plan = next(item for item in receipt.ticker_plans if item.ticker == ticker)
    return next(item for item in plan.tasks if item.kind == kind)


def test_plan_is_deterministic_tiered_and_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    _seed_roster(db)

    evaluation = EvaluationAuthorization(ticker="DUOL", owner_request_id="owner-req-42")
    first = build_sec_delta_plan(_request(db, evaluation))
    second = build_sec_delta_plan(_request(db, evaluation))

    assert first == second
    assert first.computed_receipt_sha256() == first.receipt_sha256
    assert first.status == "BLOCKED"
    assert first.network_policy == "FORBIDDEN"
    assert first.database_total_changes == 0
    assert first.alembic_revision == planner.SUPPORTED_ALEMBIC_REVISION
    assert first.database_storage_identity.entries[0].suffix == ""
    assert len(first.database_storage_identity.entries) == 1
    assert first.source_policy_version == "2026-08-12.2"
    assert first.source_policy_sha256 == _sha256(
        Path(__file__).parents[1] / "src" / "pipeline" / "source_policy.py"
    )
    assert [plan.ticker for plan in first.ticker_plans] == ["DUOL", "META", "RBRK", "ZZZZ"]

    roster = {item.ticker: item for item in first.roster}
    assert roster["DUOL"].selection == "OWNER_REQUESTED_EVALUATION"
    assert roster["WIX"].selection == "EXCLUDED_EVALUATION_REQUEST_REQUIRED"
    assert roster["AMD"].selection == "EXCLUDED_LIST_TYPE"
    assert roster["GOOG"].selection == "EXCLUDED_LIST_TYPE"
    assert roster["ARCH"].selection == "EXCLUDED_ARCHIVED"
    assert roster["IVN"].selection == "EXCLUDED_NO_SEC_FILER"
    assert first.computed_roster_sha256() == first.roster_sha256

    assert _task(first, "META", "COMPANYFACTS_DELTA").status == "READY"
    assert _task(first, "META", "NATIVE_INVENTORY_PACKAGES").status == ("BLOCKED_ISSUER_POLICY")
    assert _task(first, "RBRK", "NATIVE_INVENTORY_PACKAGES").status == "READY"
    assert _task(first, "RBRK", "NATIVE_FILING_SECTIONS").status == ("BLOCKED_EVIDENCE_LINKAGE")
    assert _task(first, "ZZZZ", "COMPANYFACTS_DELTA").status == "BLOCKED_MISSING_CIK"

    rbrk = next(item for item in first.ticker_plans if item.ticker == "RBRK")
    assert rbrk.inventory.current_sealed_revision == 1
    assert rbrk.inventory.next_inventory_revision == 2
    assert rbrk.inventory.sealed_native_document_count == 3
    assert rbrk.inventory.outstanding_native_document_count == 2

    companyfacts = _task(first, "DUOL", "COMPANYFACTS_DELTA")
    assert companyfacts.authorization == "OWNER_REQUEST"
    assert companyfacts.authorization_attestation == "CALLER_ATTESTED"
    assert companyfacts.source_policy_version == first.source_policy_version
    assert companyfacts.source_policy_sha256 == first.source_policy_sha256
    assert companyfacts.owner_request_id == "owner-req-42"
    assert companyfacts.task_id.startswith("sec-delta:companyfacts_delta:DUOL:")
    assert {item.name: item.status for item in companyfacts.dependencies} == {
        "ACTIVE_TRACKED_TICKER": "SATISFIED",
        "CIK_MAPPING": "SATISFIED",
        "EXPLICIT_TICKER_SCOPE": "SATISFIED",
        "OWNER_AUTHORIZATION": "SATISFIED",
        "SOURCE_POLICY_AUTHORIZATION": "SATISFIED",
    }


def test_evaluation_authorization_requires_active_evaluation_ticker(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    _seed_roster(db)

    receipt = build_sec_delta_plan(
        _request(
            db,
            EvaluationAuthorization(ticker="WIX", owner_request_id="req-wix"),
            EvaluationAuthorization(ticker="AMD", owner_request_id="req-not-eval"),
            EvaluationAuthorization(ticker="MISSING", owner_request_id="req-missing"),
        )
    )

    assert "WIX" in [plan.ticker for plan in receipt.ticker_plans]
    assert [(item.ticker, item.reason_code) for item in receipt.authorization_rejections] == [
        ("AMD", "ticker_is_not_active_evaluation"),
        ("MISSING", "ticker_is_not_active_evaluation"),
    ]
    assert receipt.status == "BLOCKED"


def test_plan_refuses_wal_snapshot_before_opening_or_mutating_shm(tmp_path: Path) -> None:
    db = tmp_path / "active.db"
    _create_db(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("INSERT INTO tracked_companies VALUES ('META','portfolio',NULL)")
        connection.commit()
        wal = Path(f"{db}-wal")
        shm = Path(f"{db}-shm")
        assert wal.exists() and shm.exists()
        before = (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns)
        time.sleep(0.01)

        with pytest.raises(SnapshotSafetyError, match="closed sidecar-free immutable snapshot"):
            build_sec_delta_plan(_request(db))

        assert (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns) == before
    finally:
        connection.close()


def test_incomplete_inventory_seal_is_not_current_or_outstanding(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    _seed_roster(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES (?,?,?,?,?,?,?)",
        (
            "rbrk-sec-2",
            "sec-cik-0001943896:sec-submissions",
            2,
            "sec-cik-0001943896",
            "RBRK",
            "sec_submissions",
            "2026-08-12T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshot_seals VALUES (?,?,?,?,?)",
        ("rbrk-sec-2", 1, "b" * 64, "incomplete", "2026-08-12T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,?,?)",
        ("rbrk-incomplete-doc", "rbrk-sec-2", "sec_filing"),
    )
    conn.commit()
    conn.close()

    receipt = build_sec_delta_plan(_request(db))
    rbrk = next(item for item in receipt.ticker_plans if item.ticker == "RBRK")

    assert rbrk.inventory.current_sealed_snapshot_id == "rbrk-sec-1"
    assert rbrk.inventory.current_sealed_revision == 1
    assert rbrk.inventory.next_inventory_revision == 3
    assert rbrk.inventory.sealed_native_document_count == 3
    assert rbrk.inventory.outstanding_native_document_count == 2


@pytest.mark.parametrize("revision", [None, "0008_add_fmp_recovery", "unknown-head"])
def test_absent_or_unsupported_alembic_revision_is_rejected(
    tmp_path: Path, revision: str | None
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM alembic_version")
    if revision is not None:
        conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="supported planner schema revision"):
        build_sec_delta_plan(_request(db))


def test_missing_schema_returns_sealed_blocked_receipt(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db, complete_schema=False)

    receipt = build_sec_delta_plan(_request(db))

    assert receipt.status == "BLOCKED"
    assert receipt.ticker_plans == ()
    assert receipt.roster == ()
    assert receipt.missing_schema == (
        "expected_documents",
        "source_coverage_assessments",
        "source_inventory_snapshot_seals",
        "source_inventory_snapshots",
    )
    assert receipt.computed_receipt_sha256() == receipt.receipt_sha256


def test_receipt_pydantic_boundary_rejects_tampering(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    receipt = build_sec_delta_plan(_request(db))
    payload = receipt.model_dump(mode="json")
    payload["as_of"] = "2026-08-13"

    with pytest.raises(ValidationError, match="receipt_sha256"):
        SecDeltaPlanReceipt.model_validate_json(json.dumps(payload))


def test_source_policy_hash_participates_in_plan_and_task_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    _seed_roster(db)
    baseline = build_sec_delta_plan(_request(db))

    monkeypatch.setattr(planner, "_source_policy_sha256", lambda: "b" * 64)
    changed = build_sec_delta_plan(_request(db))

    assert changed.source_policy_sha256 == "b" * 64
    assert changed.request_sha256 != baseline.request_sha256
    assert (
        _task(changed, "META", "COMPANYFACTS_DELTA").task_id
        != _task(baseline, "META", "COMPANYFACTS_DELTA").task_id
    )


def test_cli_emits_one_receipt_is_read_only_and_exits_nonzero_for_blocked_work(
    tmp_path: Path,
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    _seed_roster(db)
    before = _sha256(db)
    script = Path(__file__).parents[1] / "execution" / "plan_sec_delta_refresh.py"
    output = tmp_path / "governed" / "sec-delta-plan.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db),
            "--as-of",
            "2026-08-12",
            "--output",
            str(output),
            "--evaluation-request",
            "DUOL:owner-req-42",
        ],
        cwd=script.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2, completed.stderr
    stdout_lines = completed.stdout.splitlines()
    assert len(stdout_lines) == 1
    assert len(completed.stdout.encode("utf-8")) < 100_000
    terminal = SecDeltaPlanTerminalReceipt.model_validate_json(stdout_lines[0])
    assert terminal.status == "BLOCKED"
    assert terminal.plan_path == str(output.resolve())
    plan_bytes = output.read_bytes()
    assert hashlib.sha256(plan_bytes).hexdigest() == terminal.plan_sha256
    plan = SecDeltaPlanReceipt.model_validate_json(plan_bytes)
    assert plan.receipt_sha256 == terminal.plan_receipt_sha256
    assert plan.database_storage_identity.entries[0].content_sha256 == before == _sha256(db)
    stderr_lines = completed.stderr.splitlines()
    assert len(stderr_lines) == 1
    event = json.loads(stderr_lines[0])
    assert event["event"] == "sec_delta_plan_completed"
    assert event["status"] == "BLOCKED"
    assert event["terminal_receipt_sha256"] == terminal.receipt_sha256


def test_cli_failure_is_compact_truthful_and_does_not_write_output(tmp_path: Path) -> None:
    db = tmp_path / "active.db"
    _create_db(db)
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("INSERT INTO tracked_companies VALUES ('META','portfolio',NULL)")
        connection.commit()
        script = Path(__file__).parents[1] / "execution" / "plan_sec_delta_refresh.py"
        output = tmp_path / "governed" / "must-not-exist.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db",
                str(db),
                "--as-of",
                "2026-08-12",
                "--output",
                str(output),
            ],
            cwd=script.parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        connection.close()

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not output.exists()
    events = completed.stderr.splitlines()
    assert len(events) == 1
    assert json.loads(events[0]) == {
        "error_code": "unsafe_or_invalid_snapshot",
        "error_type": "SnapshotSafetyError",
        "event": "sec_delta_plan_failed",
    }


def test_cli_refuses_output_that_aliases_database(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    before = db.read_bytes()
    script = Path(__file__).parents[1] / "execution" / "plan_sec_delta_refresh.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db),
            "--as-of",
            "2026-08-12",
            "--output",
            str(db),
        ],
        cwd=script.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert db.read_bytes() == before
    assert json.loads(completed.stderr)["error_code"] == "invalid_request_or_plan_artifact"


def test_hardlinked_database_snapshot_is_rejected_before_open(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    alias = tmp_path / "portfolio-alias.db"
    _create_db(db)
    os.link(db, alias)
    before = planner.database_storage_identity(db)

    with pytest.raises(SnapshotSafetyError, match="exactly one filesystem link"):
        build_sec_delta_plan(_request(db))

    assert planner.database_storage_identity(db) == before
    assert db.read_bytes() == alias.read_bytes()


def test_existing_output_hardlink_to_database_is_rejected_without_storage_change(
    tmp_path: Path,
) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan = build_sec_delta_plan(_request(db))
    output = tmp_path / "governed" / "plan.json"
    output.parent.mkdir()
    os.link(db, output)
    before = planner.database_storage_identity(db)

    with pytest.raises(ValueError, match="alias SQLite storage"):
        write_sec_delta_plan(plan, output)

    assert planner.database_storage_identity(db) == before
    assert output.read_bytes() == db.read_bytes()


def test_ordinary_output_rechecks_immutable_database_storage(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _create_db(db)
    plan = build_sec_delta_plan(_request(db))
    output = tmp_path / "governed" / "plan.json"

    terminal = write_sec_delta_plan(plan, output)

    assert terminal.plan_path == str(output.resolve())
    assert output.exists()
    assert planner.database_storage_identity(db) == plan.database_storage_identity
