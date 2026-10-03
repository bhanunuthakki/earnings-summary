"""One selected accession uses native evidence owners without unrelated work."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from execution import refresh_sec_accession as cli
from pipeline import sec_accession_refresh as owner
from pipeline.sec_accession_refresh import (
    AccessionRefreshPlan,
    AccessionRefreshRequest,
    apply_accession_refresh,
    plan_accession_refresh,
)
from pipeline.sec_accession_request import (
    BoundAccessionRequest,
    HttpAnalysisRequest,
    SecAccessionPlanInput,
    prepare_bound_request,
)
from provenance.analysis_scope import AnalysisEvidenceScope
from provenance.sec_native_capture import SecNativeCaptureRequest, capture_expected_sec_documents
from provenance.source_coverage import ExpectedDocument, SourceCoverageLedger
from tests.test_sec_native_capture import (
    INVENTORY_KEY,
    FakeResponse,
    FakeSession,
    seed_sec_capture_inventory,
    seed_second_sec_accession,
)


def request(tmp_path: Path) -> AccessionRefreshRequest:
    return AccessionRefreshRequest(
        request_id="selected-acme",
        ticker="ACME",
        issuer_id="issuer-acme",
        cik="0000000001",
        inventory_key=INVENTORY_KEY,
        accession_number="0000000001-26-000001",
        repo_root=tmp_path,
    )


def test_plan_is_read_only_and_apply_replays_native_text(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn = seed_sec_capture_inventory(tmp_path, migrated_db)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.total_changes
        plan = plan_accession_refresh(conn, request(tmp_path))
        assert conn.total_changes == before
        assert not (tmp_path / ".tmp").exists()
        assert len(plan.documents) == 1
        session = FakeSession([FakeResponse()])
        first = apply_accession_refresh(
            conn, plan, session=session, user_agent="research-agent test@example.test"
        )
        assert first.state == "succeeded"
        assert first.items[0].status == "extracted"
        assert first.items[0].document_version_id
        replay = apply_accession_refresh(
            conn, plan, session=FakeSession([]), user_agent="research-agent test@example.test"
        )
        assert replay.state == "succeeded"
        assert replay.items[0].status == "reused"
        assert conn.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 1
    finally:
        conn.close()


def add_member(conn: sqlite3.Connection, *, unavailable: bool = False) -> None:
    original = ExpectedDocument.model_validate(
        dict(
            conn.execute(
                "SELECT * FROM expected_documents WHERE expected_document_id='expected-10k'"
            ).fetchone()
        )
    )
    assert original.source_url is not None
    member = original.model_copy(
        update={
            "expected_document_id": "expected-member",
            "idempotency_key": "expected-member",
            "expected_document_key": original.expected_document_key + ":member",
            "document_type": "sec_attachment",
            "source_url": None
            if unavailable
            else original.source_url.replace("acme-20251231x10k.htm", "member.htm"),
            "primary_document": None if unavailable else "member.htm",
        }
    )
    SourceCoverageLedger(conn).persist(member)
    conn.commit()


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    conn = seed_sec_capture_inventory(tmp_path, migrated_db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def test_plan_never_creates_a_transport_or_checkpoint(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    def forbidden() -> None:
        raise AssertionError("plan created transport")

    monkeypatch.setattr(cli.requests, "Session", forbidden)
    plan = plan_accession_refresh(database, request(tmp_path))
    result = owner.inspect_accession_refresh(database, plan)
    assert result.state == "planned" and result.items[0].status == "capture_needed"
    assert not plan.request.operation_root.exists()


def test_captured_before_request_still_extracts(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    req = SecNativeCaptureRequest(
        inventory_keys=(INVENTORY_KEY,),
        checkpoint_root=tmp_path / "prior-capture",
        blob_root=request(tmp_path).blob_root,
        task_id="prior-capture",
        user_agent="research-agent test@example.test",
        apply=True,
    )
    capture_expected_sec_documents(database, req, session=FakeSession([FakeResponse()]))
    plan = plan_accession_refresh(database, request(tmp_path))
    result = apply_accession_refresh(
        database, plan, session=FakeSession([]), user_agent="research-agent test@example.test"
    )
    assert result.capture is None and result.items[0].status == "extracted"


def test_budget_resume_passes_completed_members(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    add_member(database)
    req = request(tmp_path).model_copy(update={"extraction_batch_size": 1})
    plan = plan_accession_refresh(database, req)
    first = apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse(), FakeResponse()]),
        user_agent="research-agent test@example.test",
    )
    assert [item.status for item in first.items] == ["extracted", "not_attempted"]
    second = apply_accession_refresh(
        database, plan, session=FakeSession([]), user_agent="research-agent test@example.test"
    )
    assert second.state == "succeeded"
    assert [item.status for item in second.items] == ["reused", "extracted"]


def test_failed_extraction_resumes_without_capture(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    actual = owner.backfill_fulltext_evidence

    def failure(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic extraction failure")

    monkeypatch.setattr(owner, "backfill_fulltext_evidence", failure)
    first = apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse()]),
        user_agent="research-agent test@example.test",
    )
    assert first.state == "partial" and first.items[0].status == "quarantined"
    monkeypatch.setattr(owner, "backfill_fulltext_evidence", actual)
    second = apply_accession_refresh(
        database, plan, session=FakeSession([]), user_agent="research-agent test@example.test"
    )
    assert second.state == "succeeded" and second.capture is None


def test_other_accession_is_not_selected(database: sqlite3.Connection, tmp_path: Path) -> None:
    seed_second_sec_accession(database)
    plan = plan_accession_refresh(database, request(tmp_path))
    session = FakeSession([FakeResponse()])
    result = apply_accession_refresh(
        database, plan, session=session, user_agent="research-agent test@example.test"
    )
    assert result.state == "succeeded" and len(session.calls) == 1
    assert result.capture is not None
    assert len(result.items) == 1 and result.capture.pending_outside_selection == 1


def test_missing_locator_keeps_request_partial(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    add_member(database, unavailable=True)
    plan = plan_accession_refresh(database, request(tmp_path))
    result = apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse()]),
        user_agent="research-agent test@example.test",
    )
    assert result.state == "partial"
    assert [item.status for item in result.items] == ["extracted", "authority_unavailable"]


@pytest.mark.parametrize("status,expected", [(403, "blocked"), (503, "partial")])
def test_transport_failure_is_not_success(
    database: sqlite3.Connection, tmp_path: Path, status: int, expected: str
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    first = apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse(status_code=status)]),
        user_agent="research-agent test@example.test",
    )
    assert first.state == expected
    assert database.execute("SELECT COUNT(*) FROM evidence_extraction_runs").fetchone()[0] == 0
    if status == 503:
        second = apply_accession_refresh(
            database,
            plan,
            session=FakeSession([FakeResponse()]),
            user_agent="research-agent test@example.test",
        )
        assert second.state == "succeeded"


def test_changed_population_refuses_before_network(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    add_member(database)
    session = FakeSession([])
    with pytest.raises(owner.RefreshBoundaryError, match="no_longer_current"):
        apply_accession_refresh(
            database, plan, session=session, user_agent="research-agent test@example.test"
        )
    assert not session.calls


def test_byte_mutation_refuses_even_after_success(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse()]),
        user_agent="research-agent test@example.test",
    )
    file = next(path for path in plan.request.blob_root.rglob("*") if path.is_file())
    file.write_bytes(b"changed raw")
    with pytest.raises(owner.RefreshBoundaryError, match="bytes_mismatch"):
        apply_accession_refresh(
            database, plan, session=FakeSession([]), user_agent="research-agent test@example.test"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"issuer_id": "different"},
        {"cik": "0000000002"},
        {"accession_number": "0000000001-26-000003"},
        {"inventory_key": "absent"},
    ],
)
def test_unknown_or_wrong_selection_refuses(
    database: sqlite3.Connection, tmp_path: Path, change: dict[str, str]
) -> None:
    with pytest.raises(owner.RefreshBoundaryError):
        plan_accession_refresh(database, request(tmp_path).model_copy(update=change))


def test_etf_is_not_a_corporate_filing_target(database: sqlite3.Connection, tmp_path: Path) -> None:
    database.execute("UPDATE tracked_companies SET instrument_type='etf' WHERE ticker='ACME'")
    database.commit()
    with pytest.raises(owner.RefreshBoundaryError, match="source_policy_denied"):
        plan_accession_refresh(database, request(tmp_path))


def test_cli_offline_plan_and_exact_apply(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = database.execute("PRAGMA database_list").fetchone()[2]
    common = ["--db", db_path, "--repo-root", str(tmp_path), "--request-id", "cli-acme"]

    def no_transport() -> None:
        raise AssertionError("offline planning used transport")

    monkeypatch.setattr(cli.requests, "Session", no_transport)
    assert (
        cli.main(
            [
                *common,
                "--ticker",
                "ACME",
                "--issuer-id",
                "issuer-acme",
                "--cik",
                "0000000001",
                "--inventory-key",
                INVENTORY_KEY,
                "--accession-number",
                "0000000001-26-000001",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(cli, "sec_user_agent", lambda: "research-agent test@example.test")
    monkeypatch.setattr(cli.requests, "Session", lambda: FakeSession([FakeResponse()]))
    assert cli.main([*common, "--apply", "--plan-sha256", result["plan_sha256"]]) == 0
    assert list(
        (tmp_path / ".tmp/operations/runtime/sec-accession-refresh/cli-acme/attempts").glob(
            "*.result.json"
        )
    )
    assert cli.main([*common, "--apply", "--plan-sha256", "0" * 64]) == 2


def test_cli_lock_contention_starts_nothing(database: sqlite3.Connection, tmp_path: Path) -> None:
    from run_lock import hold_run_lock

    db_path = Path(database.execute("PRAGMA database_list").fetchone()[2])
    with hold_run_lock(db_path, owner="other-writer", timeout_s=0):
        assert (
            cli.main(
                [
                    "--db",
                    str(db_path),
                    "--repo-root",
                    str(tmp_path),
                    "--request-id",
                    "blocked",
                    "--apply",
                    "--plan-sha256",
                    "0" * 64,
                ]
            )
            == 75
        )
    assert not (
        tmp_path / ".tmp/operations/runtime/sec-accession-refresh/blocked/attempts"
    ).exists()


def test_capture_budget_resumes_only_remaining_member(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    add_member(database)
    plan = plan_accession_refresh(
        database, request(tmp_path).model_copy(update={"capture_batch_size": 1})
    )
    first = apply_accession_refresh(
        database,
        plan,
        session=FakeSession([FakeResponse()]),
        user_agent="research-agent test@example.test",
    )
    assert first.state == "partial"
    assert [item.status for item in first.items] == ["extracted", "not_attempted"]
    second_session = FakeSession([FakeResponse()])
    second = apply_accession_refresh(
        database, plan, session=second_session, user_agent="research-agent test@example.test"
    )
    assert second.state == "succeeded"
    assert [item.status for item in second.items] == ["reused", "extracted"]
    assert len(second_session.calls) == 1
    assert second_session.calls[0].endswith("/member.htm")


def test_interrupted_cli_retains_unconfirmed_attempt_and_exact_resume(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    cli.publish_text_no_clobber(plan.request.operation_root / "plan.json", plan.model_dump_json())
    common = [
        "--db",
        plan.database_path,
        "--repo-root",
        str(tmp_path),
        "--request-id",
        plan.request.request_id,
        "--apply",
        "--plan-sha256",
        plan.commitment,
    ]
    monkeypatch.setattr(cli, "sec_user_agent", lambda: "research-agent test@example.test")
    monkeypatch.setattr(cli.requests, "Session", lambda: FakeSession([FakeResponse()]))
    actual = cli.apply_accession_refresh

    def interrupted(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(cli, "apply_accession_refresh", interrupted)
    assert cli.main(common) == 2
    attempts = plan.request.operation_root / "attempts"
    assert len(list(attempts.glob("*.started.json"))) == 1
    assert not list(attempts.glob("*.result.json"))
    partial = json.loads(next(attempts.glob("*.unconfirmed.json")).read_text())
    assert partial["state"] == "completion_unconfirmed"
    assert partial["reason_code"] == "RuntimeError"
    monkeypatch.setattr(cli, "apply_accession_refresh", actual)
    assert cli.main([*common, "--capture-batch-size", "2"]) == 2
    assert len(list(attempts.glob("*.started.json"))) == 1
    assert cli.main(common) == 0
    assert len(list(attempts.glob("*.started.json"))) == 2
    assert len(list(attempts.glob("*.result.json"))) == 1


def test_changed_policy_role_refuses_exact_resume(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    plan = plan_accession_refresh(database, request(tmp_path))
    changed = plan.model_copy(update={"coverage_role": "different"})
    with pytest.raises(owner.RefreshBoundaryError, match="no_longer_current"):
        owner.inspect_accession_refresh(database, changed)


def scoped_plan(
    database: sqlite3.Connection, root: Path
) -> tuple[BoundAccessionRequest, AccessionRefreshPlan]:
    body = SecAccessionPlanInput(
        request_id="scoped-acme",
        ticker="ACME",
        cik="0000000001",
        accession_number="0000000001-26-000001",
        analysis=HttpAnalysisRequest(
            purpose="Read the annual reported period",
            issuer_id="issuer-acme",
            inventory_key=INVENTORY_KEY,
            required_period_ends=(date(2025, 12, 31),),
            cutoff_at=datetime(2026, 7, 28, tzinfo=UTC),
            observed_through=datetime(2026, 7, 28, tzinfo=UTC),
        ),
    )
    return prepare_bound_request(database, root, body)


@pytest.mark.parametrize("commitment", [None, "0" * 64])
def test_cli_bound_request_requires_exact_scope_before_transport(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commitment: str | None,
) -> None:
    _bound, plan = scoped_plan(database, tmp_path)

    def forbidden() -> None:
        raise AssertionError("invalid scope reached transport")

    monkeypatch.setattr(cli.requests, "Session", forbidden)
    args = [
        "--db",
        plan.database_path,
        "--repo-root",
        str(tmp_path),
        "--request-id",
        "scoped-acme",
        "--apply",
        "--plan-sha256",
        plan.commitment,
    ]
    if commitment is not None:
        args.extend(["--request-sha256", commitment])
    assert cli.main(args) == 2
    assert not (plan.request.operation_root / "attempts").exists()


def test_cli_self_rehashed_wrong_period_scope_is_not_cosmetic(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound, plan = scoped_plan(database, tmp_path)
    material = bound.scope.model_dump(mode="json", exclude={"scope_sha256", "scope_id"})
    declared = material["request"]
    assert isinstance(declared, dict)
    cast("dict[str, object]", declared)["required_period_ends"] = ["2024-12-31"]
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fake_scope = AnalysisEvidenceScope.model_validate(
        material | {"scope_sha256": digest, "scope_id": "analysis-scope:" + digest}
    )
    fake = BoundAccessionRequest(
        request_id=bound.request_id, plan_sha256=plan.commitment, scope=fake_scope
    )
    (plan.request.operation_root / "request.json").write_text(fake.model_dump_json() + "\n")

    def forbidden() -> None:
        raise AssertionError("fabricated analysis selection reached transport")

    monkeypatch.setattr(cli.requests, "Session", forbidden)
    assert (
        cli.main(
            [
                "--db",
                plan.database_path,
                "--repo-root",
                str(tmp_path),
                "--request-id",
                "scoped-acme",
                "--apply",
                "--plan-sha256",
                plan.commitment,
                "--request-sha256",
                fake.commitment,
            ]
        )
        == 2
    )
    assert not (plan.request.operation_root / "attempts").exists()
