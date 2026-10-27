"""Report synchronization ownership and publication regressions."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextvars import Context
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dcf.persist import DcfRunRow, persist_run
from dcf.provenance import DcfInputProvenance
from execution.build_artifacts import build_candidate_artifact
from pipeline.work_os_company import latest_brief
from report.artifacts import (
    ReportArtifactRef,
    ReportPublicationConflictError,
    ReportRequestConflictError,
    begin_or_reuse_report_request,
    build_report_sync_status,
    complete_report_request,
    hold_report_index,
    latest_report_artifacts_for_tickers,
    load_report_artifact_index,
    publish_report_candidate,
    reconcile_legacy_workspace_reports,
)
from research.decision_brief import DecisionBriefReadiness
from runtime.job_runtime import JobAlreadyRunningError


def _verified_readiness(artifact: ReportArtifactRef, as_of: datetime) -> DecisionBriefReadiness:
    return DecisionBriefReadiness(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        body_sha256=str(artifact.body_sha256 or ""),
        evaluated_at=as_of,
        status="verified",
        decision_grade=True,
        reason_codes=(),
    )


def _mock_assess_verified(
    _conn: sqlite3.Connection,
    *,
    repo_root: Path,
    artifact: ReportArtifactRef,
    as_of: datetime,
    **kwargs: object,
) -> DecisionBriefReadiness:
    return _verified_readiness(artifact, as_of)


def test_index_lock_is_cross_process_and_follows_resolved_output(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from report.artifacts import hold_report_index
from runtime.job_runtime import JobAlreadyRunningError
try:
    with hold_report_index(Path(sys.argv[1]), wait_s=0):
        pass
except JobAlreadyRunningError:
    sys.exit(75)
"""
    with hold_report_index(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 75, result.stderr
    with hold_report_index(tmp_path, wait_s=0):
        pass


def test_reconcile_cannot_read_modify_write_outside_index_owner(tmp_path: Path) -> None:
    # A different context in the same process must also contend. The public
    # mutation borrows its exact synchronous owner only when explicitly nested.
    with hold_report_index(tmp_path), pytest.raises(JobAlreadyRunningError):
        Context().run(lambda: reconcile_legacy_workspace_reports(tmp_path))


def test_concurrent_ticker_writers_preserve_each_others_index_entries(tmp_path: Path) -> None:
    script = """
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from report.artifacts import (
    RenderedReportBody, ReportInteractionManifest, persist_report_artifact,
)
root, prefix = Path(sys.argv[1]), sys.argv[2]
for number in range(3):
    ticker = prefix + str(number)
    standalone = root / (ticker + '.html')
    standalone.write_text('<html>synthetic ' + ticker + '</html>')
    body = RenderedReportBody.from_html(
        ticker=ticker, report_date=date(2026, 10, 24),
        body_html='<main data-report-body="v1">synthetic ' + ticker + '</main>',
        sections=(), interaction_manifest=ReportInteractionManifest(),
    )
    persist_report_artifact(
        repo_root=root, body=body, standalone_path=standalone,
        generated_at=datetime(2026, 10, 24, tzinfo=UTC),
        coverage_role='unknown', title='Synthetic concurrency fixture',
    )
"""
    processes: list[subprocess.Popen[str]] = []
    try:
        for prefix in ("SYNTHA", "SYNTHB"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(tmp_path), prefix],
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            _stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    assert {item.ticker for item in load_report_artifact_index(tmp_path).items} == {
        prefix + str(number) for prefix in ("SYNTHA", "SYNTHB") for number in range(3)
    }


def _seed_dcf_run(db_path: Path, ticker: str, valuation_date: date, npv_per_share: float) -> int:
    provenance = DcfInputProvenance(
        input_sha256=hashlib.sha256(
            f"synthetic:{ticker.upper()}:{valuation_date.isoformat()}".encode()
        ).hexdigest(),
        workbook_sha256=hashlib.sha256(
            f"synthetic-wb:{ticker.upper()}:{valuation_date.isoformat()}".encode()
        ).hexdigest(),
        engine_version="synthetic_test_fixture_v1",
        inputs_as_of=datetime(
            valuation_date.year, valuation_date.month, valuation_date.day, 12, 0, tzinfo=UTC
        ),
        detail={
            "equity_bridge_receipt": {"status": "verified"},
            "sources": [],
            "market_price": None,
        },
    )
    row = DcfRunRow(
        ticker=ticker.upper(),
        valuation_date=valuation_date,
        horizon_years=10,
        wacc=0.09,
        npv=1_000.0,
        npv_per_share=npv_per_share,
        shares_outstanding=10_000_000.0,
        currency="USD",
        live_price=None,
        live_price_at=None,
        mos_bar_used=0.2,
        assumption_snapshot_json='{"wacc":0.09}',
        notes="synthetic test fixture",
        provenance=provenance,
    )
    with sqlite3.connect(db_path) as conn:
        result = persist_run(conn, row)
        return result.revision.row_id


def test_candidate_review_blocks_unverified_publication(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    db_path = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('GOOG', 'Alphabet Inc.', 'portfolio')"
        )
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 24), 200.0)
    thesis_path = tmp_path / "micro_thesis" / "holdings" / "GOOG.json"
    thesis_path.parent.mkdir(parents=True)
    thesis_path.write_text('{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}}')

    res = build_candidate_artifact("GOOG", tmp_path, db_path=db_path)
    assert res["status"] == "ok"
    assert res["reused"] is False
    assert isinstance(res["candidate_review"], dict)
    assert res["candidate_review"]["ready"] is False
    with pytest.raises(ReportPublicationConflictError) as exc:
        publish_report_candidate(
            tmp_path,
            db_path=db_path,
            ticker="GOOG",
            artifact_id=str(res["artifact_id"]),
            expected_current_artifact_id=None,
            owner_action_id="act-unverified",
        )
    assert exc.value.reason == "candidate_review_blocked"


def test_candidate_build_and_publish_lifecycle(
    tmp_path: Path, migrated_db: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "execution.build_artifacts.assess_candidate_decision_brief",
        _mock_assess_verified,
    )
    db_path = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('GOOG', 'Alphabet Inc.', 'portfolio')"
        )
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 24), 200.0)
    thesis_path = tmp_path / "micro_thesis" / "holdings" / "GOOG.json"
    thesis_path.parent.mkdir(parents=True)
    thesis_path.write_text('{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}}')

    res = build_candidate_artifact("GOOG", tmp_path, db_path=db_path)
    assert res["status"] == "ok"
    assert res["reused"] is False
    assert isinstance(res["candidate_review"], dict)
    assert res["candidate_review"]["ready"] is True
    assert latest_report_artifacts_for_tickers(tmp_path, ("GOOG",)) == {}
    assert not (
        tmp_path / "output" / "research" / "GOOG" / f"{date.today().isoformat()}_workspace.html"
    ).exists()

    generated_at = datetime.fromisoformat(
        str(load_report_artifact_index(tmp_path).items[0].generated_at)
    )
    reused = build_candidate_artifact("GOOG", tmp_path, db_path=db_path, cutoff_at=generated_at)
    assert reused["reused"] is True
    assert reused["artifact_id"] == res["artifact_id"]

    _approval, pub_reused = publish_report_candidate(
        tmp_path,
        db_path=db_path,
        ticker="GOOG",
        artifact_id=str(res["artifact_id"]),
        expected_current_artifact_id=None,
        owner_action_id="act-1",
    )
    assert pub_reused is False
    assert (
        latest_report_artifacts_for_tickers(tmp_path, ("GOOG",))["GOOG"].artifact_id
        == res["artifact_id"]
    )
    _approval2, rep_reused = publish_report_candidate(
        tmp_path,
        db_path=db_path,
        ticker="GOOG",
        artifact_id=str(res["artifact_id"]),
        expected_current_artifact_id=None,
        owner_action_id="act-1",
    )
    assert rep_reused is True
    with pytest.raises(ReportPublicationConflictError) as exc:
        publish_report_candidate(
            tmp_path,
            db_path=db_path,
            ticker="GOOG",
            artifact_id=str(res["artifact_id"]),
            expected_current_artifact_id="other",
            owner_action_id="act-1",
        )
    assert exc.value.reason == "owner_action_id_conflict"


def test_publish_fences_detect_model_and_file_drift(
    tmp_path: Path, migrated_db: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "execution.build_artifacts.assess_candidate_decision_brief",
        _mock_assess_verified,
    )
    db_path = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('GOOG', 'Alphabet Inc.', 'portfolio')"
        )
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 24), 200.0)
    thesis_path = tmp_path / "micro_thesis" / "holdings" / "GOOG.json"
    thesis_path.parent.mkdir(parents=True)
    thesis_contents = '{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}}'
    thesis_path.write_text(thesis_contents)
    res = build_candidate_artifact("GOOG", tmp_path, db_path=db_path)

    thesis_path.write_text(
        '{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}, "summary": "drifted"}'
    )
    with pytest.raises(ReportPublicationConflictError) as exc:
        publish_report_candidate(
            tmp_path,
            db_path=db_path,
            ticker="GOOG",
            artifact_id=str(res["artifact_id"]),
            expected_current_artifact_id=None,
            owner_action_id="act-file",
        )
    assert exc.value.reason == "file_snapshot_drifted"

    thesis_path.write_text(thesis_contents)
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 25), 210.0)
    with pytest.raises(ReportPublicationConflictError) as exc:
        publish_report_candidate(
            tmp_path,
            db_path=db_path,
            ticker="GOOG",
            artifact_id=str(res["artifact_id"]),
            expected_current_artifact_id=None,
            owner_action_id="act-model",
        )
    assert exc.value.reason == "model_changed_since_candidate"


def test_request_dedup_and_dead_pid_recovery(tmp_path: Path) -> None:
    rec1, reused1 = begin_or_reuse_report_request(
        tmp_path,
        semantic_request_key="k1",
        kind="candidate_refresh",
        ticker="GOOG",
        owner_action_id="oa1",
    )
    assert not reused1
    assert rec1.status == "running"
    rec2, reused2 = begin_or_reuse_report_request(
        tmp_path, semantic_request_key="k1", kind="candidate_refresh", ticker="GOOG"
    )
    assert reused2
    assert rec2.semantic_request_key == "k1"
    with pytest.raises(ReportRequestConflictError):
        begin_or_reuse_report_request(
            tmp_path,
            semantic_request_key="k2",
            kind="candidate_refresh",
            ticker="GOOG",
            owner_action_id="oa1",
        )
    complete_report_request(
        tmp_path, semantic_request_key="k1", status="completed", artifact_id="rep_1"
    )
    rec3, reused3 = begin_or_reuse_report_request(
        tmp_path, semantic_request_key="k1", kind="candidate_refresh", ticker="GOOG"
    )
    assert reused3
    assert rec3.status == "completed"


def test_unified_current_report_selectors_and_sync_status(
    tmp_path: Path, migrated_db: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "execution.build_artifacts.assess_candidate_decision_brief",
        _mock_assess_verified,
    )
    db_path = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('GOOG', 'Alphabet Inc.', 'portfolio')"
        )
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 24), 200.0)
    thesis_path = tmp_path / "micro_thesis" / "holdings" / "GOOG.json"
    thesis_path.parent.mkdir(parents=True)
    thesis_path.write_text('{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}}')
    stray = tmp_path / "output" / "research" / "GOOG" / "2099-01-01_workspace.html"
    stray.parent.mkdir(parents=True)
    stray.write_text("<html>stray</html>")
    assert latest_brief(tmp_path, "GOOG") is None

    res = build_candidate_artifact("GOOG", tmp_path, db_path=db_path)
    publish_report_candidate(
        tmp_path,
        db_path=db_path,
        ticker="GOOG",
        artifact_id=str(res["artifact_id"]),
        expected_current_artifact_id=None,
        owner_action_id="act-publish",
    )
    brief = latest_brief(tmp_path, "GOOG")
    assert brief is not None
    assert brief.artifact_id == res["artifact_id"]
    with sqlite3.connect(db_path) as conn:
        assert build_report_sync_status(tmp_path, "GOOG", conn=conn)["report_stale"] is False


def test_report_sync_api_routes_etag_and_lifecycle(
    tmp_path: Path, migrated_db: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from execution import comments_server

    monkeypatch.setattr(
        "execution.build_artifacts.assess_candidate_decision_brief",
        _mock_assess_verified,
    )
    db_path = migrated_db(tmp_path / "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('GOOG', 'Alphabet Inc.', 'portfolio')"
        )
    _seed_dcf_run(db_path, "GOOG", date(2026, 10, 24), 200.0)
    thesis_path = tmp_path / "micro_thesis" / "holdings" / "GOOG.json"
    thesis_path.parent.mkdir(parents=True)
    thesis_path.write_text('{"ticker": "GOOG", "dcf_defaults": {"valuation_model": "fcff_dcf"}}')

    client = comments_server.create_app(tmp_path, db_path=db_path).test_client()

    # Initial sync status: report_stale is True
    res_get = client.get("/api/report-sync/GOOG")
    assert res_get.status_code == 200
    status_initial = res_get.get_json()
    assert status_initial["report_stale"] is True
    initial_etag = res_get.headers.get("ETag")
    assert initial_etag

    # Refresh candidate
    res_cand = client.post(
        "/api/report-sync/GOOG/candidate",
        headers={"If-Match": initial_etag},
    )
    assert res_cand.status_code == 201
    cand_data = res_cand.get_json()
    candidate_id = cand_data["artifact_id"]
    assert cand_data["reused"] is False

    # Second candidate call without changes reuses candidate
    res_cand_reused = client.post(
        "/api/report-sync/GOOG/candidate",
        headers={"If-Match": initial_etag},
    )
    assert res_cand_reused.status_code == 200
    assert res_cand_reused.get_json()["reused"] is True
    assert res_cand_reused.get_json()["artifact_id"] == candidate_id

    # Fetch status with candidate selected
    res_selected = client.get(f"/api/report-sync/GOOG?candidate_artifact_id={candidate_id}")
    assert res_selected.status_code == 200
    candidate_etag = res_selected.headers.get("ETag")
    assert candidate_etag
    assert candidate_etag != initial_etag

    # Publish with mismatching ETag fails 412
    res_bad_publish = client.post(
        "/api/report-sync/GOOG/publish",
        headers={"If-Match": '"wrong-etag"'},
        json={"artifact_id": candidate_id, "owner_action_id": "act-bad"},
    )
    assert res_bad_publish.status_code == 412

    # Publish with candidate-selected ETag succeeds
    res_pub = client.post(
        "/api/report-sync/GOOG/publish",
        headers={"If-Match": candidate_etag},
        json={"artifact_id": candidate_id, "owner_action_id": "act-api-pub"},
    )
    assert res_pub.status_code == 201

    # Post-publish status: in sync
    res_final = client.get("/api/report-sync/GOOG")
    assert res_final.status_code == 200
    assert res_final.get_json()["report_stale"] is False


def test_populated_canonical_financial_candidate_generation(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> None:
    from execution import build_artifacts
    from provenance.source_read_context import SourceReadContext
    from report.artifacts import load_report_file_capture_receipt
    from report.file_capture import replay_file_capture
    from report.sections import financials
    from tests.test_report_canonical_financials import STAMP, seed_table
    from tests.test_source_fact_repository import seed_foundation

    root = tmp_path.resolve()
    db = migrated_db(root / "test.sqlite")
    authority = SourceReadContext(content_roots=(root / "approved",))
    with sqlite3.connect(db) as conn:
        seed_foundation(conn, source_path=root / "approved/document.json")
        seed_table(conn, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type) "
            "VALUES ('SYNTH','Synthetic capture audit','portfolio')"
        )
    _seed_dcf_run(db, "SYNTH", date(2026, 10, 24), 200.0)
    thesis = root / "micro_thesis/holdings/SYNTH.json"
    thesis.parent.mkdir(parents=True)
    thesis.write_text(
        '{"ticker":"SYNTH","thesis":"Synthetic populated financial audit",'
        '"dcf_defaults":{"valuation_model":"fcff_dcf"}}'
    )

    with sqlite3.connect(db) as conn:
        baseline = financials.build("SYNTH", root, conn=conn, as_of=STAMP, source_context=authority)
    assert [(row.line_item, row.values) for row in baseline.line_items] == [("Revenue", [100.0])]

    result = build_artifacts.build_candidate_artifact(
        "SYNTH", root, db_path=db, source_context=authority
    )
    assert result["status"] == "ok"
    artifact_id = str(result["artifact_id"])

    file_receipt = load_report_file_capture_receipt(root, "SYNTH", artifact_id)
    assert file_receipt is not None
    replay_file_capture(file_receipt, root)

    item = load_report_artifact_index(root).items[0]
    assert item.body_path is not None
    body: str = (root / item.body_path).read_text(encoding="utf-8")
    assert "$100" in body or "100.0" in body
