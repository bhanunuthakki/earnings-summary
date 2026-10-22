"""Saved preview uses the existing owner without becoming the latest accepted brief."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from report.artifacts import (
    RenderedReportBody,
    ReportArtifactRef,
    ReportInteractionManifest,
    ReportSectionRef,
    latest_report_artifacts,
    latest_report_artifacts_for_tickers,
    load_report_artifact_index,
    persist_report_artifact,
)
from research.decision_brief import assess_decision_brief
from research.report_contract import BoundReport, ReportDraft
from research.report_preview import save_report_preview
from tests.test_report_contract import connection as connection
from tests.test_report_contract import draft


def add_source_locations(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE evidence_nodes ADD COLUMN extraction_run_id TEXT")
    conn.execute("ALTER TABLE evidence_nodes ADD COLUMN locator_json TEXT")
    conn.execute(
        "CREATE TABLE evidence_extraction_runs(extraction_run_id TEXT,document_version_id TEXT)"
    )
    conn.execute("INSERT INTO evidence_extraction_runs VALUES ('run-1','document-1')")
    conn.execute(
        "UPDATE evidence_nodes SET extraction_run_id='run-1',locator_json=?",
        ('{"page_number":3,"section":"Results"}',),
    )
    conn.commit()


def test_manual_preview_save_reopen_preserves_evidence_without_latest_promotion(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    add_source_locations(connection)
    accepted_body = RenderedReportBody.from_html(
        ticker="SYNTH",
        report_date=date(2025, 1, 1),
        body_html='<main data-report-body="v1"><section id="accepted">Prior brief</section></main>',
        sections=(ReportSectionRef(section_id="accepted", label="Prior brief", group_id="prior"),),
        interaction_manifest=ReportInteractionManifest(),
    )
    accepted_standalone = tmp_path / "accepted.html"
    accepted_standalone.write_text("<html>Prior brief</html>")
    accepted = persist_report_artifact(
        repo_root=tmp_path,
        body=accepted_body,
        standalone_path=accepted_standalone,
        generated_at=datetime(2025, 1, 1, tzinfo=UTC),
        coverage_role="portfolio",
        title="Prior brief",
    )
    item = draft()
    first = (
        item.sections[0]
        .paragraphs[0]
        .model_copy(
            update={
                "voice": "analyst_inference",
                "text": "<img src=x onerror=bad()> I see room for improvement.",
            }
        )
    )
    item = item.model_copy(
        update={
            "sections": (
                item.sections[0].model_copy(update={"paragraphs": (first,)}),
                *item.sections[1:],
            )
        }
    )
    ref = save_report_preview(connection, item, repo_root=tmp_path)
    reopened = ReportArtifactRef.model_validate_json((tmp_path / ref.manifest_path).read_bytes())
    assert reopened == ref and ref.publication_status == "draft_preview"
    assert ref.body_path is not None
    body = (tmp_path / ref.body_path).read_bytes()
    assert hashlib.sha256(body).hexdigest() == ref.body_sha256
    standalone = (tmp_path / ref.standalone_path).read_bytes()
    assert hashlib.sha256(standalone).hexdigest() == ref.workspace_sha256
    soup = BeautifulSoup(body, "html.parser")
    standalone_soup = BeautifulSoup(standalone, "html.parser")
    reader_main = soup.select_one("main")
    standalone_main = standalone_soup.select_one("main")
    assert reader_main is not None and standalone_main is not None
    assert reader_main.get_text() == standalone_main.get_text()
    assert "Not decision-grade." in soup.get_text()
    assert "Analyst inference" in soup.get_text() and "Reported source text" in soup.get_text()
    assert soup.find("img") is None and soup.find("script") is None
    assert soup.select_one('a[href="/source/version/document-1?node=node-1"]') is not None
    assert '"page_number":3' in soup.get_text()
    artifact_root = (tmp_path / ref.manifest_path).parent
    saved = ReportDraft.model_validate_json((artifact_root / "draft.json").read_bytes())
    binding = BoundReport.model_validate_json((artifact_root / "binding.json").read_bytes())
    assert saved == item
    assert (
        reader_main["data-binding-sha256"]
        == hashlib.sha256(binding.model_dump_json().encode()).hexdigest()
    )
    assert latest_report_artifacts(load_report_artifact_index(tmp_path)) == (accepted,)
    assert latest_report_artifacts_for_tickers(tmp_path, ["SYNTH"]) == {"SYNTH": accepted}
    assert list((tmp_path / ".tmp").iterdir()) == []
    readiness = assess_decision_brief(
        connection, repo_root=tmp_path, artifact=ref, as_of=datetime.now(UTC)
    )
    assert not readiness.decision_grade
    assert readiness.reason_codes == ("memo_draft_preview_not_accepted",)


def test_preview_cannot_be_replayed_as_an_accepted_artifact(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    add_source_locations(connection)
    ref = save_report_preview(connection, draft(), repo_root=tmp_path)
    assert ref.body_path is not None
    body = RenderedReportBody(
        artifact_id=ref.artifact_id,
        ticker=ref.ticker,
        report_date=ref.report_date,
        body_html=(tmp_path / ref.body_path).read_text(),
        body_sha256=str(ref.body_sha256),
        sections=(),
        interaction_manifest=ReportInteractionManifest(),
    )
    with pytest.raises(ValueError, match="publication status"):
        persist_report_artifact(
            repo_root=tmp_path,
            body=body,
            standalone_path=tmp_path / ref.standalone_path,
            generated_at=ref.generated_at,
            coverage_role="portfolio",
            title="Must not promote",
            publication_status="retained",
        )


def test_post_preview_retains_and_renders_unavailable_baseline(
    connection: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from earnings_readout import ReportedQuarter
    from research import earnings_report_contract as adapters
    from research.earnings_report_contract import BoundPostReport
    from tests.test_report_contract import STAMP

    add_source_locations(connection)

    def missing_baseline(_conn: sqlite3.Connection, _quarter: ReportedQuarter) -> dict[str, str]:
        return {"status": "unavailable", "reason": "missing_exact_event_baseline"}

    monkeypatch.setattr(adapters, "pre_call_baseline", missing_baseline)
    quarter = ReportedQuarter(
        ticker="SYNTH",
        list_type="portfolio",
        transcript_id=1,
        document_id=1,
        fiscal_period_type="Q4",
        period_end="2025-12-31",
        call_date=STAMP.date().isoformat(),
    )
    ref = save_report_preview(
        connection,
        draft(family="post").model_copy(update={"event_date": STAMP.date()}),
        repo_root=tmp_path,
        quarter=quarter,
    )
    receipt = BoundPostReport.model_validate_json(
        (tmp_path / ref.manifest_path).with_name("post-baseline.json").read_text()
    )
    assert receipt.baseline_status == "unavailable"
    assert receipt.baseline_binding == "not_verified"
    assert ref.body_path is not None
    body = BeautifulSoup((tmp_path / ref.body_path).read_text(), "html.parser")
    standalone = BeautifulSoup((tmp_path / ref.standalone_path).read_text(), "html.parser")
    expected = hashlib.sha256(receipt.model_dump_json().encode()).hexdigest()
    for document in (body, standalone):
        label = document.select_one(f'[data-baseline-sha256="{expected}"]')
        assert label is not None
        assert label.get_text() == "No verified PRE baseline was selected."
    assert latest_report_artifacts(load_report_artifact_index(tmp_path)) == ()


def test_renderer_rejects_changed_draft_or_post_receipt(connection: sqlite3.Connection) -> None:
    from earnings_brief import SECTION_TITLES
    from report.renderers.bound_draft import render_bound_draft
    from research.earnings_report_contract import BoundPostReport
    from research.report_contract import bind_report_draft

    item = draft()
    binding = bind_report_draft(connection, item, required_titles=SECTION_TITLES)
    paragraph = item.sections[0].paragraphs[0].model_copy(update={"text": "Changed claim."})
    changed = item.model_copy(
        update={
            "sections": (
                item.sections[0].model_copy(update={"paragraphs": (paragraph,)}),
                *item.sections[1:],
            )
        }
    )
    with pytest.raises(ValueError, match="draft differs"):
        render_bound_draft(connection, changed, binding)
    different = binding.model_copy(update={"snapshot_sha256": "different"})
    post = BoundPostReport(
        report=different,
        baseline_status="unavailable",
        baseline_selection_sha256="none",
        baseline_artifact_id=None,
        baseline_reason="missing",
    )
    with pytest.raises(ValueError, match="POST baseline differs"):
        render_bound_draft(connection, item, binding, post=post)
