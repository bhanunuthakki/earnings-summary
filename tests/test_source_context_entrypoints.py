"""Explicit byte authority reaches real entrypoints; no LLM, network or child runs."""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from advisor import context as advisor_context
from advisor import memos, socratic
from dcf.input_evidence import SourceReadContext
from execution import run_advisor_memos, run_socratic_questions


@pytest.mark.parametrize("entrypoint", ["memos", "questions"])
def test_advisor_entrypoint_forwards_explicit_state_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    state = tmp_path / "state"
    code = tmp_path / "code"
    seen: list[object] = []
    ctx = SimpleNamespace(audit_rows=[], candidates_val={}, live=SimpleNamespace(available=False))

    def build(_root: Path, **kwargs: object) -> SimpleNamespace:
        assert _root == code
        seen.append(kwargs.get("source_context"))
        return ctx

    monkeypatch.setattr(advisor_context, "build_advisor_context", build)
    if entrypoint == "memos":

        def memo(_root: Path, **kwargs: object) -> SimpleNamespace:
            assert kwargs["ctx"] is ctx
            return SimpleNamespace(ok=True, memo_id=1, title="synthetic")

        monkeypatch.setattr(memos, "generate_next_dollar_memo", memo)
        result = run_advisor_memos.main(
            ["--kind", "next_dollar", "--repo-root", str(code), "--state-root", str(state)]
        )
    else:

        def questions(_root: Path, ticker: str, **kwargs: object) -> socratic.SocraticPrelude:
            supplied = kwargs.get("source_context")
            seen.append(supplied)
            assert supplied == SourceReadContext.for_sec_state_root(state)
            return socratic.SocraticPrelude(
                ticker=ticker, questions=["one?", "two?", "three?"], context_block="synthetic"
            )

        monkeypatch.setattr(socratic, "generate_questions", questions)

        def persist(*_args: object, **_kwargs: object) -> int:
            return 1

        monkeypatch.setattr(socratic, "persist_prelude", persist)
        result = run_socratic_questions.main(
            ["ONON", "--repo-root", str(code), "--state-root", str(state)]
        )
    assert result == 0
    assert seen == [SourceReadContext.for_sec_state_root(state)]


def test_dashboard_source_authority_reaches_supported_child_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatch_registry import Job, Registry
    from execution.comments_server import create_app

    root = tmp_path / "code"
    state = tmp_path / "state"
    registry = Registry(repo_root=root)
    original = registry.start

    def no_spawn(*, ticker: str, kind: str, argv: list[str], **_kwargs: object) -> Job:
        return original(ticker=ticker, kind=kind, argv=argv, spawn=False)

    monkeypatch.setattr(registry, "start", no_spawn)
    app = create_app(
        root, registry=registry, db_path=tmp_path / "explicit-synthetic.db", source_state_root=state
    )
    client = app.test_client()
    for route, body in (
        ("/actions/advisor-memo", {"kind": "next_dollar"}),
        ("/actions/socratic-questions", {"ticker": "ONON"}),
        ("/actions/dcf-import", {"ticker": "META"}),
    ):
        response = client.post(route, json=body)
        assert response.status_code == 201
        job = registry.get(response.get_json()["job_id"])
        assert job is not None
        assert job.argv[job.argv.index("--state-root") + 1] == str(state)
        assert job.argv[job.argv.index("--repo-root") + 1] == str(root)


@pytest.mark.parametrize("explicit_source", [False, True])
def test_dashboard_dispatch_and_bulk_rebuild_do_not_infer_byte_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_source: bool
) -> None:
    from dispatch_registry import Job, Registry
    from execution import comments_server

    root = tmp_path / "output-state"
    code = Path(__file__).resolve().parents[1]
    sources = tmp_path / "source-state"
    registry = Registry(repo_root=root)
    original = registry.start

    def no_spawn(*, ticker: str, kind: str, argv: list[str], **_kwargs: object) -> Job:
        return original(ticker=ticker, kind=kind, argv=argv, spawn=False)

    def python(_root: Path) -> Path:
        assert _root == code
        return Path("/Applications/earnings-summary/.venv/bin/python")

    monkeypatch.setattr(registry, "start", no_spawn)
    monkeypatch.setattr(comments_server, "application_python_executable", python)
    client = comments_server.create_app(
        root,
        code_root=code,
        registry=registry,
        db_path=tmp_path / "explicit-synthetic.db",
        source_state_root=sources if explicit_source else None,
    ).test_client()
    response = client.post("/actions/refresh", json={"ticker": "ONON", "steps": ["dcf"]})
    assert response.status_code == 201
    job = registry.get(response.get_json()["job_id"])
    assert job is not None
    assert job.argv[job.argv.index("--state-root") + 1] == str(root)
    if explicit_source:
        assert job.argv[job.argv.index("--source-state-root") + 1] == str(sources)
    else:
        assert "--source-state-root" not in job.argv
    response = client.post("/actions/rebuild-dcfs", json={})
    assert response.status_code == 201
    job = registry.get(response.get_json()["job_id"])
    assert job is not None
    if explicit_source:
        assert job.argv[job.argv.index("--state-root") + 1] == str(sources)
    else:
        assert "--state-root" not in job.argv


@pytest.mark.parametrize("explicit_source", [False, True])
def test_sheet_import_forwards_only_explicit_source_root_to_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_source: bool
) -> None:
    from execution import dcf_sheets

    code = tmp_path / "code"
    sources = tmp_path / "source-state"
    database = tmp_path / "synthetic.db"
    database.touch()
    workbook = tmp_path / "input.xlsx"
    workbook.write_bytes(b"isolated workbook child boundary")

    def configured(_root: Path) -> Path:
        assert _root == code
        return database

    def require(_override: Path) -> Path:
        assert _override == database
        return database

    seen: list[object] = []

    def refresh(ticker: str, repo_root: Path, db_path: Path, **kwargs: object) -> dict[str, object]:
        assert ticker == "META" and repo_root == code and db_path == database
        candidate = kwargs["input_workbook"]
        assert isinstance(candidate, Path)
        assert candidate.read_bytes() == workbook.read_bytes()
        seen.append(kwargs["source_state_root"])
        return {"ticker": ticker, "status": "ok"}

    monkeypatch.setattr(dcf_sheets, "configured_db_path", configured)
    monkeypatch.setattr(dcf_sheets, "require_db_path", require)
    monkeypatch.setattr(dcf_sheets.refresh_dcf, "refresh_one", refresh)
    argv = ["import", "--ticker", "META", "--repo-root", str(code), "--file", str(workbook)]
    if explicit_source:
        argv.extend(("--state-root", str(sources)))
    assert dcf_sheets.main(argv) == 0
    assert seen == [sources if explicit_source else None]


@pytest.mark.parametrize("entrypoint", ["prepare-memo", "verify-memo", "cashflow"])
@pytest.mark.parametrize("binding", ["argument", "environment", "absent"])
def test_decision_entrypoints_use_only_explicit_source_byte_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, binding: str
) -> None:
    from dcf.cashflow_refresh import PreparedCashflowDcfRequest, PreparedCashflowDcfResult
    from dcf.input_evidence import InputEvidenceError, verify_input_source_bytes
    from execution import prepare_cashflow_dcf, prepare_decision_brief, verify_decision_brief
    from research.decision_brief import DecisionBriefReadiness
    from research.decision_brief_workflow import (
        DecisionBriefPreparationReceipt,
        DecisionBriefPreparationRequest,
    )
    from tests.test_decision_brief_workflow import prepared, report

    output = tmp_path / "output-state"
    output.mkdir()
    database = tmp_path / "database-authority" / "synthetic.db"
    sources = tmp_path / "source-authority"
    expected = SourceReadContext.for_sec_state_root(sources) if binding != "absent" else None
    seen: list[SourceReadContext | None] = []
    monkeypatch.delenv("DCF_SOURCE_STATE_ROOT", raising=False)
    if binding == "environment":
        monkeypatch.setenv("DCF_SOURCE_STATE_ROOT", str(sources))
    elif binding == "argument":
        # An explicit argument wins over a separate explicitly configured source.
        monkeypatch.setenv("DCF_SOURCE_STATE_ROOT", str(tmp_path / "other-source-authority"))

    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    def connect(path: Path, **_kwargs: object) -> Connection:
        assert path == database
        return connection

    def require(path: Path | str) -> Path:
        assert Path(path) == database
        return database

    def lock(*_args: object, **_kwargs: object):
        return nullcontext()

    def load_env(root: Path) -> None:
        assert root == output

    def prepare(request: DecisionBriefPreparationRequest) -> DecisionBriefPreparationReceipt:
        assert request.repo_root == output and request.database == database
        assert request.source_state_root == (sources if binding != "absent" else None)
        seen.append(
            SourceReadContext.for_sec_state_root(request.source_state_root)
            if request.source_state_root is not None
            else None
        )
        return DecisionBriefPreparationReceipt(
            ticker=request.ticker, requested_at=datetime.now(UTC), stages=(), status="planned"
        )

    def assess(_conn: sqlite3.Connection, **kwargs: object) -> DecisionBriefReadiness:
        supplied = kwargs["source_context"]
        assert supplied is None or isinstance(supplied, SourceReadContext)
        seen.append(supplied)
        assert kwargs["repo_root"] == output
        return DecisionBriefReadiness(
            artifact_id="synthetic-memo",
            ticker="NEW",
            body_sha256=None,
            evaluated_at=datetime.now(UTC),
            reason_codes=("synthetic_evidence_incomplete",),
        )

    def calculate(
        conn: sqlite3.Connection, request: PreparedCashflowDcfRequest, **kwargs: object
    ) -> PreparedCashflowDcfResult:
        supplied = kwargs["source_context"]
        assert supplied is None or isinstance(supplied, SourceReadContext)
        seen.append(supplied)
        assert kwargs["repo_root"] == output and kwargs["apply"] is False
        if supplied is None:
            # The actual byte verifier refuses before any connection access.
            verify_input_source_bytes(conn, (), supplied)
        return PreparedCashflowDcfResult(
            mode="dry_run",
            ticker=request.model_inputs.ticker,
            model_input_receipt={},
            effective_inputs={},
            model_output={},
            calculated_at=datetime.now(UTC),
            source_observed_at=datetime.now(UTC),
        )

    module = {
        "prepare-memo": prepare_decision_brief,
        "verify-memo": verify_decision_brief,
        "cashflow": prepare_cashflow_dcf,
    }[entrypoint]
    monkeypatch.setattr(module, "require_db_path", require)
    monkeypatch.setattr(module, "load_project_env", load_env)
    argv = ["--db", str(database), "--repo-root", str(output)]
    if binding == "argument":
        argv.extend(
            ("--state-root" if entrypoint == "cashflow" else "--source-state-root", str(sources))
        )
    if entrypoint == "prepare-memo":
        monkeypatch.setattr(prepare_decision_brief, "JobLock", lock)
        monkeypatch.setattr(prepare_decision_brief, "prepare_decision_brief", prepare)
        assert prepare_decision_brief.main([*argv, "--ticker", "NEW"]) == 0
    elif entrypoint == "verify-memo":
        manifest = report(output)
        monkeypatch.setattr(verify_decision_brief, "connect_sqlite", connect)
        monkeypatch.setattr(verify_decision_brief, "assess_decision_brief", assess)
        assert verify_decision_brief.main([*argv, "--artifact-manifest", str(manifest)]) == 3
        assert connection.closed
    else:
        path, digest = prepared(output, "NEW")
        artifact = output / "calculation.json"
        monkeypatch.setattr(prepare_cashflow_dcf, "JobLock", lock)
        monkeypatch.setattr(prepare_cashflow_dcf, "connect_sqlite", connect)
        monkeypatch.setattr(prepare_cashflow_dcf, "prepare_cashflow_dcf", calculate)
        argv.extend(
            ("--request", str(path), "--request-sha256", digest, "--artifact", str(artifact))
        )
        if binding == "absent":
            with pytest.raises(InputEvidenceError, match="model_input_source_context_unavailable"):
                prepare_cashflow_dcf.main(argv)
        else:
            assert prepare_cashflow_dcf.main(argv) == 0
        assert connection.closed and not artifact.exists()
    assert seen == [expected]
