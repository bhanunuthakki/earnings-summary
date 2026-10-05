"""Exercise requested memo recovery against migrated state and child receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable, Generator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from pipeline.sec_onboarding_identity import IdentityStatus, ensure_sec_onboarding_identity
from provenance.source_coverage import SourceCoverageLedger, SourceInventorySnapshot
from report.artifacts import (
    RenderedReportBody,
    ReportArtifactRef,
    ReportInteractionManifest,
    ReportSectionRef,
    persist_report_artifact,
)
from research import decision_brief_workflow as workflow
from research.decision_brief_workflow import DecisionBriefPreparationRequest, prepare_decision_brief
from tests.test_sec_onboarding_identity import STAMP, sec_identity_sources


@pytest.fixture
def state(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[tuple[Path, Path, str], None, None]:
    root = tmp_path / "state"
    root.mkdir()
    database = migrated_db(tmp_path / "authority" / "memo.db")
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO tracked_companies(user_id,ticker,name,list_type) VALUES('bhanu','NEW','New Issuer','evaluation')"
    )
    conn.commit()
    sources = sec_identity_sources()
    result = ensure_sec_onboarding_identity(
        conn, ticker="NEW", project_root=root, fetch=lambda url: sources[url], knowledge_at=STAMP
    )
    assert result.status is IdentityStatus.READY
    issuer = str(conn.execute("SELECT issuer_id FROM issuer_entities").fetchone()[0])
    conn.close()
    yield root, database, issuer


def request(state: tuple[Path, Path, str], **changes: object) -> DecisionBriefPreparationRequest:
    root, database, _issuer = state
    return DecisionBriefPreparationRequest(
        ticker="NEW",
        code_root=Path(__file__).resolve().parents[1],
        repo_root=root,
        database=database,
        apply=True,
        skip_fmp=True,
    ).model_copy(update=changes)


def report(root: Path) -> Path:
    body = RenderedReportBody.from_html(
        ticker="NEW",
        report_date=date.today(),
        body_html='<main data-report-body="v1"><section id="financials">Retained analyst draft.</section></main>',
        sections=(
            ReportSectionRef(section_id="financials", label="Financials", group_id="financials"),
        ),
        interaction_manifest=ReportInteractionManifest(),
    )
    standalone = root / "draft.html"
    standalone.write_text(body.body_html)
    artifact = persist_report_artifact(
        repo_root=root,
        body=body,
        standalone_path=standalone,
        generated_at=datetime.now(UTC),
        coverage_role="evaluation",
        title="New Issuer",
    )
    return root / artifact.manifest_path


class Children:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[tuple[str, ...]] = []
        self.outputs: dict[str, tuple[int, str]] = {}

    def __call__(self, command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == self.root
        self.commands.append(command)
        script = next(Path(item).name for item in reversed(command) if item.endswith(".py"))
        if script in self.outputs:
            status, output = self.outputs[script]
        elif script == "onboard_ticker.py":
            status, output = (
                0,
                json.dumps(
                    {
                        "event": "onboard_sec_ingestion",
                        "ticker": "NEW",
                        "run_id": "synthetic-run",
                        "status": "ok",
                        "rows_processed": 0,
                    }
                )
                + "\n[onboard] done\n",
            )
        elif script == "build_artifacts.py":
            status, output = (
                0,
                json.dumps([{"ticker": "NEW", "report_manifest": str(report(self.root))}]),
            )
        else:
            status, output = 3, ""
        return subprocess.CompletedProcess(
            command, status, stdout=output, stderr="provider diagnostics excluded"
        )


def test_independent_memo_survives_optional_processor_and_inventory_failure(
    state: tuple[Path, Path, str],
) -> None:
    children = Children(state[0])
    result = prepare_decision_brief(request(state), runner=children)
    assert result.status == "delivered_degraded"
    assert result.artifact_manifest and result.readiness and not result.readiness.decision_grade
    assert result.claim_inventory_path
    inventory = json.loads((state[0] / result.claim_inventory_path).read_bytes())
    assert inventory["status"] == "requires_analyst_review" and not inventory["decision_grade"]
    assert inventory["artifact_id"] == result.readiness.artifact_id
    assert inventory["body_sha256"] == result.readiness.body_sha256
    assert inventory["blocks"][0]["text"] == "Retained analyst draft."
    source = children.commands[0]
    assert "--skip-fmp" in source and "--skip-llm" in source
    assert not any(flag in source for flag in ("--skip-transcripts", "--skip-ir", "--skip-saydo"))
    assert "--skip-sec" not in source
    assert str(state[1]) in source and str(state[0]) in source
    assert list((state[0] / ".tmp/decision_brief/NEW/requests").glob("*.json"))


def test_claim_inventory_refuses_symlinked_output_authority(
    state: tuple[Path, Path, str], tmp_path: Path
) -> None:
    manifest = report(state[0])
    artifact = ReportArtifactRef.model_validate_json(manifest.read_bytes())
    outside = tmp_path / "outside"
    outside.mkdir()
    review_inputs = manifest.parent / "review_inputs"
    review_inputs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        workflow.prepare_memo_claim_inventory(request(state), artifact)

    assert list(outside.iterdir()) == []


def test_claim_inventory_refuses_body_outside_retained_root(
    state: tuple[Path, Path, str], tmp_path: Path
) -> None:
    manifest = report(state[0])
    artifact = ReportArtifactRef.model_validate_json(manifest.read_bytes())
    assert artifact.body_path
    body = state[0] / artifact.body_path
    outside = tmp_path / "outside-body.html"
    outside.write_bytes(body.read_bytes())
    body.unlink()
    body.symlink_to(outside)

    with pytest.raises(ValueError):
        workflow.prepare_memo_claim_inventory(request(state), artifact)

    assert not (manifest.parent / "review_inputs").exists()


def test_claim_inventory_uses_publishers_governed_output_authority(
    state: tuple[Path, Path, str], tmp_path: Path
) -> None:
    output_target = tmp_path / "canonical-output"
    output_target.mkdir()
    (state[0] / "output").symlink_to(output_target, target_is_directory=True)
    artifact = ReportArtifactRef.model_validate_json(report(state[0]).read_bytes())

    inventory = workflow.prepare_memo_claim_inventory(request(state), artifact)

    assert inventory.is_file()
    assert inventory.resolve().is_relative_to(output_target)


@pytest.mark.parametrize("output", ["", "{}", "[]", "not json", '{"status":"ok"}'])
def test_empty_or_wrong_child_receipt_cannot_mark_success(
    state: tuple[Path, Path, str], output: str
) -> None:
    children = Children(state[0])
    children.outputs["ingest_sec_filing_xbrl.py"] = (0, output)
    result = prepare_decision_brief(request(state), runner=children)
    stage = next(item for item in result.stages if item.stage == "native_preflight")
    assert stage.status == "blocked" and stage.reason_code == "stage_output_contract_failed"
    assert result.status == "delivered_degraded"


@pytest.mark.parametrize("failure", ["manifest", "context"])
def test_artifact_and_context_failure_keep_recovery_receipt(
    state: tuple[Path, Path, str], failure: str
) -> None:
    children = Children(state[0])
    changes: dict[str, object] = {}
    if failure == "manifest":
        children.outputs["build_artifacts.py"] = (
            0,
            json.dumps([{"ticker": "NEW", "report_manifest": str(state[0] / "missing.json")}]),
        )
    else:
        invalid = state[0] / "context.json"
        invalid.write_text('{"not":"a bound context review"}')
        changes["context_review"] = invalid
    result = prepare_decision_brief(request(state, **changes), runner=children)
    assert result.status == ("blocked" if failure == "manifest" else "delivered_degraded")
    assert any(
        item.reason_code
        == ("returned_artifact_invalid" if failure == "manifest" else "memo_context_review_invalid")
        for item in result.stages
    )
    receipts = list((state[0] / ".tmp/decision_brief/NEW/requests").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_bytes())["status"] == result.status
    assert "provider diagnostics" not in receipts[0].read_text()


def context(document: str, issuer: str) -> dict[str, object]:
    return {
        "document_version_id": document,
        "document_sha256": "a" * 64,
        "issuer_id": issuer,
        "reporting_entity_id": "reporting-new",
        "evidence_node_id": "heading-node",
        "evidence_locator_sha256": "b" * 64,
        "source_wording": "Consolidated financial statements",
        "accounting_basis": "us_gaap",
        "consolidation_scope": "consolidated",
        "source_scope_label": "consolidated",
        "period_end": "2025-12-31T00:00:00Z",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "reviewer": "synthetic-analyst",
        "reviewed_at": STAMP.isoformat(),
        "rationale": "Retained source heading establishes this exact accounting scope.",
    }


def test_companyfacts_other_issuer_request_never_reaches_writable_child(
    state: tuple[Path, Path, str],
) -> None:
    path = state[0] / "other-review.json"
    path.write_text(
        json.dumps(
            {
                "recorded_at": STAMP.isoformat(),
                "facts": [
                    {
                        "match_revision_id": "other-match",
                        "concept": "revenue",
                        "context": context("other-document", "other-issuer"),
                    }
                ],
            }
        )
    )
    children = Children(state[0])
    result = prepare_decision_brief(request(state, companyfacts_reviews=(path,)), runner=children)
    assert any(
        item.reason_code == "requested_issuer_review_scope_invalid" for item in result.stages
    )
    assert not any(
        "continue_companyfacts_statements.py" in " ".join(command) for command in children.commands
    )
    conn = sqlite3.connect(state[1])
    assert conn.execute("SELECT COUNT(*) FROM reported_observations").fetchone()[0] == 0
    conn.close()


def inventory(state: tuple[Path, Path, str]) -> str:
    _root, database, issuer = state
    conn = sqlite3.connect(database)
    observation = conn.execute(
        "SELECT observation_id FROM evidence_source_observations WHERE source_kind='sec_submissions'"
    ).fetchone()[0]
    key = f"{issuer}:sec-submissions"
    now = datetime.now(UTC)
    SourceCoverageLedger(conn).persist(
        SourceInventorySnapshot(
            snapshot_id="current-inventory",
            idempotency_key="current-inventory",
            inventory_key=key,
            revision=1,
            issuer_id=issuer,
            ticker="NEW",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0001234567.json",
            source_observation_id=observation,
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="c" * 64,
            collector_code_version="synthetic@test",
            started_at=now,
            completed_at=now,
            recorded_at=now,
        )
    )
    conn.commit()
    conn.close()
    return key


def capture_output(
    root: Path,
    key: str,
    number: int,
    *,
    task_id: str,
    has_more: bool,
    failed: int = 0,
    created: int = 1,
) -> str:
    items = [
        {
            "expected_document_id": f"expected-{number}",
            "expected_document_key": f"expected-{number}",
            "outcome": "fetched",
            "reason_code": "captured",
            "document_version_id": f"document-{number}",
            "records_created": created,
        }
    ]
    raw = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = root / f".tmp/sec_native_capture/{task_id}/results" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return json.dumps(
        {
            "task_id": task_id,
            "mode": "apply",
            "inventory_keys": [key],
            "considered": 1,
            "fetched": 1,
            "deferred": 0,
            "failed": failed,
            "records_created": created,
            "records_replayed": 0,
            "has_more": has_more,
            "sec_only_boundary": "SEC only",
            "items_path": str(path),
            "item_count": 1,
        }
    )


@pytest.mark.parametrize(
    "mode,expected_calls,reason",
    [
        ("progress", 2, None),
        ("error", 1, "sec_capture_pending_or_failed"),
        ("unchanged", 2, "sec_capture_no_progress"),
        ("zero", 1, "sec_capture_no_progress"),
        ("limit", 8, "sec_capture_batch_limit_reached"),
    ],
)
def test_capture_resumes_only_with_bounded_progress(
    state: tuple[Path, Path, str], mode: str, expected_calls: int, reason: str | None
) -> None:
    key = inventory(state)
    children = Children(state[0])
    calls = 0

    def run(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        if any(item.endswith("capture_expected_sec_documents.py") for item in command):
            calls += 1
            output = capture_output(
                state[0],
                key,
                1 if mode == "unchanged" else calls,
                task_id=command[command.index("--task-id") + 1],
                has_more=mode != "progress" or calls == 1,
                failed=int(mode == "error"),
                created=0 if mode == "zero" else 1,
            )
            children.commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        return children(command, cwd)

    result = prepare_decision_brief(request(state), runner=run)
    assert calls == expected_calls
    assert result.status == "delivered_degraded"
    if reason:
        assert any(item.reason_code == reason for item in result.stages)
    task_ids = [
        command[command.index("--task-id") + 1]
        for command in children.commands
        if "--task-id" in command
    ]
    assert len(set(task_ids)) == 1
    assert not any(
        "sync_sec_filing_inventory.py" in " ".join(command) for command in children.commands
    )


def prepared(root: Path, ticker: str) -> tuple[Path, str]:
    from dcf.cashflow_inputs import RECIPE
    from dcf.cashflow_refresh import PreparedCashflowDcfRequest
    from dcf.input_evidence import ModelInputRequest

    model = PreparedCashflowDcfRequest(
        model_inputs=ModelInputRequest(
            recipe=RECIPE,
            ticker=ticker,
            research_snapshot_id="synthetic-unsealed",
            financial_period_end=date(2025, 12, 31),
            facts={},
            assumptions={},
        ),
        effective_inputs={},
        as_of=STAMP,
        valuation_date=STAMP.date(),
        market_price=10,
        market_observed_at=STAMP,
        market_source="synthetic-market-observation",
    )
    path = root / "prepared.json"
    path.write_text(model.model_dump_json())
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepared_valuation_cannot_write_another_ticker(state: tuple[Path, Path, str]) -> None:
    path, digest = prepared(state[0], "OTHER")
    children = Children(state[0])
    result = prepare_decision_brief(
        request(
            state,
            valuation_request=path,
            valuation_request_sha256=digest,
            valuation_artifact=state[0] / "calculation.json",
        ),
        runner=children,
    )
    assert any(
        item.reason_code == "requested_issuer_review_scope_invalid" for item in result.stages
    )
    assert not any("prepare_cashflow_dcf.py" in " ".join(command) for command in children.commands)
    assert not (state[0] / "calculation.json").exists()


def test_prepared_valuation_preserves_supported_child_contract(
    state: tuple[Path, Path, str],
) -> None:
    path, digest = prepared(state[0], "NEW")
    children = Children(state[0])
    children.outputs["prepare_cashflow_dcf.py"] = (
        0,
        json.dumps(
            {
                "mode": "apply",
                "ticker": "NEW",
                "engine": "operating_cashflow_equity",
                "model_input_receipt": {},
                "effective_inputs": {},
                "model_output": {},
                "source_observed_at": STAMP.isoformat(),
                "calculated_at": STAMP.isoformat(),
            }
        ),
    )
    result = prepare_decision_brief(
        request(
            state,
            valuation_request=path,
            valuation_request_sha256=digest,
            valuation_artifact=state[0] / "calculation.json",
        ),
        runner=children,
    )
    stage = next(item for item in result.stages if item.stage == "valuation")
    assert stage.status == "completed"
    assert stage.command[stage.command.index("--request-sha256") + 1] == digest
    assert "--artifact" in stage.command and "--apply" in stage.command
    assert result.readiness and not result.readiness.decision_grade


def test_native_reviews_are_filtered_to_each_exact_document(state: tuple[Path, Path, str]) -> None:
    from pydantic import TypeAdapter

    from provenance.financial_statement_admission import FinancialStatementContextReview

    contexts = TypeAdapter(tuple[FinancialStatementContextReview, ...]).validate_python(
        [context("first-primary-document", state[2]), context("second-primary-document", state[2])]
    )
    first = workflow.prepare_native_review_file(request(state), contexts, "first-primary-document")
    second = workflow.prepare_native_review_file(
        request(state), contexts, "second-primary-document"
    )
    assert first and second and first != second
    assert [item["document_version_id"] for item in json.loads(first.read_bytes())] == [
        "first-primary-document"
    ]
    assert [item["document_version_id"] for item in json.loads(second.read_bytes())] == [
        "second-primary-document"
    ]
    assert (
        workflow.prepare_native_review_file(request(state), contexts, "unreviewed-document") is None
    )
    assert (
        workflow.prepare_native_review_file(request(state), contexts, "first-primary-document")
        == first
    )


def test_exact_idempotently_retained_artifact_is_used(state: tuple[Path, Path, str]) -> None:
    manifest = report(state[0])
    children = Children(state[0])
    children.outputs["build_artifacts.py"] = (
        0,
        json.dumps([{"ticker": "NEW", "report_manifest": str(manifest)}]),
    )
    result = prepare_decision_brief(request(state), runner=children)
    assert result.artifact_manifest == manifest.relative_to(state[0]).as_posix()
    assert (
        result.readiness
        and result.readiness.artifact_id == json.loads(manifest.read_bytes())["artifact_id"]
    )


def test_environment_failure_also_has_a_durable_recovery_receipt(
    state: tuple[Path, Path, str],
) -> None:
    missing = state[0] / "missing-authority.db"
    result = prepare_decision_brief(request(state, database=missing), runner=Children(state[0]))
    assert result.status == "blocked"
    assert result.stages[0].reason_code == "preparation_boundary_failed"
    assert list((state[0] / ".tmp/decision_brief/NEW/requests").glob("*.json"))


@pytest.mark.parametrize("status", ["already_running", "already_done"])
def test_source_singleflight_is_a_truthful_skip(state: tuple[Path, Path, str], status: str) -> None:
    children = Children(state[0])
    children.outputs["onboard_ticker.py"] = (
        0,
        json.dumps(
            {"status": status, "pipeline_key": "retained-key", "attempt_id": "retained-attempt"}
        ),
    )
    result = prepare_decision_brief(request(state), runner=children)
    assert result.stages[0].status == "skipped" and result.stages[0].reason_code == status
    assert result.artifact_manifest


def test_checked_companyfacts_bytes_are_pinned_before_child_execution(
    state: tuple[Path, Path, str],
) -> None:
    from provenance.evidence_ledger import DocumentVersion, EvidenceLedger

    conn = sqlite3.connect(state[1])
    observation = conn.execute(
        "SELECT observation_id,blob_sha256 FROM evidence_source_observations WHERE source_kind='sec_security_cover'"
    ).fetchone()
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="review-document",
            document_key="review-document",
            version_sequence=1,
            observation_id=str(observation[0]),
            blob_sha256=str(observation[1]),
            issuer_id=state[2],
            ticker="NEW",
            document_type="filing",
            form_type="10-Q",
            accession_number="0001234567-26-000001",
            language="en",
            recorded_at=STAMP,
        )
    )
    conn.commit()
    conn.close()
    path = state[0] / "review.json"
    original = json.dumps(
        {
            "recorded_at": STAMP.isoformat(),
            "facts": [
                {
                    "match_revision_id": "review-match",
                    "concept": "revenue",
                    "context": context("review-document", state[2]),
                }
            ],
        }
    )
    path.write_text(original)
    children = Children(state[0])
    observed: list[Path] = []

    def run(command: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        if any(item.endswith("continue_companyfacts_statements.py") for item in command):
            # The user-supplied request can change after validation. The child
            # must receive the retained bytes that actually passed validation.
            path.write_text("changed after scope validation")
            retained = Path(command[command.index("--request") + 1])
            observed.append(retained)
            assert retained != path and retained.read_text() == original
            payload = {
                "mode": "apply",
                "publication_id": "synthetic-publication",
                "observation_ids": ["synthetic-observation"],
                "match_revision_ids": ["review-match"],
                "source_document_version_id": "review-document",
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        return children(command, cwd)

    result = prepare_decision_brief(request(state, companyfacts_reviews=(path,)), runner=run)
    assert len(observed) == 1
    assert (
        next(item for item in result.stages if item.stage == "statement_admission").status
        == "completed"
    )
    assert result.readiness and not result.readiness.decision_grade
