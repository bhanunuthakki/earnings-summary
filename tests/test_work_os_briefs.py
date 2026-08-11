"""Brief Library API and persisted report-body delivery."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from report.artifacts import (
    RenderedReportBody,
    ReportInteractionManifest,
    ReportSectionRef,
    persist_report_artifact,
    reconcile_legacy_workspace_reports,
)
from tests.test_comments_server_dashboard import comments_server, create_dashboard_test_schema


@pytest.fixture(name="work_os_app_repo")
def _work_os_app_repo(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "portfolio.db")
    create_dashboard_test_schema(conn)
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type, instrument_type) "
        "VALUES ('NU', 'Nu Holdings', 'portfolio', 'equity')"
    )
    conn.commit()
    conn.close()
    return tmp_path


@pytest.fixture(name="work_os_client")
def _work_os_client(work_os_app_repo: Path) -> FlaskClient:
    return comments_server.create_app(work_os_app_repo).test_client()


def _persist_shared_brief(repo_root: Path, ticker: str = "NU") -> RenderedReportBody:
    report_date = date(2026, 8, 10)
    body = RenderedReportBody.from_html(
        ticker=ticker,
        report_date=report_date,
        body_html=f'<main data-report-body="v1"><section>{ticker} complete brief</section></main>',
        sections=(ReportSectionRef(section_id="company", label="Company", group_id="overview"),),
        interaction_manifest=ReportInteractionManifest(),
    )
    workspace = (
        repo_root / "output" / "research" / ticker / f"{report_date.isoformat()}_workspace.html"
    )
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.write_text(f"<html>{body.body_html}</html>", encoding="utf-8")
    persist_report_artifact(
        repo_root=repo_root,
        body=body,
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title=f"{ticker} Full Research Brief",
    )
    return body


def test_brief_library_returns_stable_indexed_artifacts(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    body = _persist_shared_brief(work_os_app_repo)

    response = work_os_client.get("/api/work-os/briefs", query_string={"ticker": "nu"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["schema_version"] == "brief_library.v1"
    assert payload["items"] == [
        {
            "artifact_id": body.artifact_id,
            "ticker": "NU",
            "title": "NU Full Research Brief",
            "artifact_kind": "full_brief",
            "coverage_role": "portfolio",
            "report_date": "2026-08-10",
            "generated_at": "2026-08-10T12:00:00Z",
            "reader_mode": "shared_body",
            "status": "available",
            "body_url": f"/api/work-os/briefs/{body.artifact_id}/body",
            "standalone_url": "/reports/NU?artifact_id=" + body.artifact_id,
            "section_count": 1,
            "source_count": None,
            "comment_count": None,
        }
    ]
    assert payload["next_cursor"] is None


def test_report_body_route_serves_complete_persisted_fragment(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    body = _persist_shared_brief(work_os_app_repo)

    response = work_os_client.get(f"/api/work-os/briefs/{body.artifact_id}/body")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert payload["schema_version"] == "report_reader_payload.v1"
    assert payload["artifact_id"] == body.artifact_id
    assert payload["ticker"] == "NU"
    assert "NU complete brief" in payload["body_html"]
    assert payload["style_url"] == "/api/work-os/report-reader.css"
    assert response.headers["X-Report-Artifact-ID"] == body.artifact_id
    assert response.headers["X-Report-Body-SHA256"] == payload["body_sha256"]


def test_report_reader_css_is_lazy_current_shell_owned_asset(
    work_os_client: FlaskClient,
) -> None:
    response = work_os_client.get("/api/work-os/report-reader.css")

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    css = response.get_data(as_text=True)
    assert "Keep the lede in the document flow" in css
    assert ".reader-group-title" in css


def test_legacy_brief_returns_structured_standalone_fallback(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    workspace = work_os_app_repo / "output" / "research" / "NU" / "2026-07-01_workspace.html"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.write_text("<html>legacy brief</html>", encoding="utf-8")
    reconcile_legacy_workspace_reports(work_os_app_repo)
    artifact_id = str(
        work_os_client.get("/api/work-os/briefs").get_json()["items"][0]["artifact_id"]
    )

    response = work_os_client.get(f"/api/work-os/briefs/{artifact_id}/body")

    assert response.status_code == 409
    assert response.get_json() == {
        "schema_version": "report_body_unavailable.v1",
        "artifact_id": artifact_id,
        "status": "legacy_standalone",
        "standalone_url": f"/reports/NU?artifact_id={artifact_id}",
    }
    standalone = work_os_client.get(response.get_json()["standalone_url"])
    assert standalone.status_code == 200
    assert "legacy brief" in standalone.get_data(as_text=True)
