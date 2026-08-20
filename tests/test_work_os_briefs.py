"""Brief Library API and persisted report-body delivery."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.work_os_briefs import build_brief_library
from report.artifacts import (
    RenderedReportBody,
    ReportInteractionManifest,
    ReportSectionRef,
    load_report_artifact_index,
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


def _persist_shared_brief(
    repo_root: Path,
    ticker: str = "NU",
    *,
    report_date: date = date(2026, 8, 10),
    generated_at: datetime | None = None,
) -> RenderedReportBody:
    body = RenderedReportBody.from_html(
        ticker=ticker,
        report_date=report_date,
        body_html=(
            f'<main data-report-body="v1"><section id="company" data-tab="company">'
            f"{ticker} complete brief</section></main>"
        ),
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
        generated_at=generated_at or datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title=f"{ticker} Full Research Brief",
    )
    return body


def test_brief_library_projects_only_latest_artifact_per_ticker_before_pagination(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    older_nu = _persist_shared_brief(
        work_os_app_repo,
        report_date=date(2026, 8, 9),
        generated_at=datetime(2026, 8, 9, 18, tzinfo=UTC),
    )
    latest_nu = _persist_shared_brief(
        work_os_app_repo,
        report_date=date(2026, 8, 10),
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
    )
    latest_meli = _persist_shared_brief(
        work_os_app_repo,
        "MELI",
        report_date=date(2026, 8, 8),
        generated_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
    )

    first = work_os_client.get("/api/work-os/briefs", query_string={"limit": 1}).get_json()
    assert [item["artifact_id"] for item in first["items"]] == [latest_nu.artifact_id]
    assert first["next_cursor"] == latest_nu.artifact_id

    second = work_os_client.get(
        "/api/work-os/briefs",
        query_string={"limit": 1, "cursor": first["next_cursor"]},
    ).get_json()
    assert [item["artifact_id"] for item in second["items"]] == [latest_meli.artifact_id]
    assert second["next_cursor"] is None

    nu = work_os_client.get("/api/work-os/briefs", query_string={"ticker": "NU"}).get_json()
    assert [item["artifact_id"] for item in nu["items"]] == [latest_nu.artifact_id]
    assert older_nu.artifact_id not in {item["artifact_id"] for item in nu["items"]}

    # Historical identity remains directly resolvable even though the default
    # current-library projection no longer repeats it.
    assert work_os_client.get(f"/api/work-os/briefs/{older_nu.artifact_id}/body").status_code == 200


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


def test_brief_library_uses_current_coverage_for_display_and_role_filtering(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    """A historical artifact role cannot outlive governed tracked coverage."""

    body = _persist_shared_brief(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute("UPDATE tracked_companies SET list_type = 'evaluation' WHERE ticker = 'NU'")
    conn.commit()
    conn.close()

    evaluation = work_os_client.get(
        "/api/work-os/briefs", query_string={"coverage_role": "evaluation"}
    ).get_json()
    portfolio = work_os_client.get(
        "/api/work-os/briefs", query_string={"coverage_role": "portfolio"}
    ).get_json()

    assert [item["artifact_id"] for item in evaluation["items"]] == [body.artifact_id]
    assert evaluation["items"][0]["coverage_role"] == "evaluation"
    assert portfolio["items"] == []

    # An archived row no longer establishes current coverage, so the durable
    # artifact role remains the truthful fallback.
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "UPDATE tracked_companies SET archived_at = '2026-08-20T00:00:00Z' WHERE ticker = 'NU'"
    )
    conn.commit()
    conn.close()
    fallback = work_os_client.get("/api/work-os/briefs").get_json()
    assert fallback["items"][0]["coverage_role"] == "portfolio"


def test_brief_library_marks_active_unsupported_coverage_unknown(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    body = _persist_shared_brief(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute("UPDATE tracked_companies SET list_type = 'watchlist' WHERE ticker = 'NU'")
    conn.commit()
    conn.close()

    unknown = work_os_client.get(
        "/api/work-os/briefs", query_string={"coverage_role": "unknown"}
    ).get_json()
    portfolio = work_os_client.get(
        "/api/work-os/briefs", query_string={"coverage_role": "portfolio"}
    ).get_json()

    assert [item["artifact_id"] for item in unknown["items"]] == [body.artifact_id]
    assert unknown["items"][0]["coverage_role"] == "unknown"
    assert portfolio["items"] == []


def test_brief_library_fails_closed_when_a_supplied_connection_is_unavailable(
    work_os_app_repo: Path,
) -> None:
    _persist_shared_brief(work_os_app_repo)
    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.close()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        build_brief_library(work_os_app_repo, conn=conn)


def test_report_body_route_serves_complete_persisted_fragment(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    body = _persist_shared_brief(work_os_app_repo)

    conn = sqlite3.connect(work_os_app_repo / "data" / "portfolio.db")
    conn.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, "
        "recommendation_kind TEXT NOT NULL, recommendation_value REAL, conviction TEXT, "
        "made_at TEXT NOT NULL, decision_conditions TEXT, source_lens TEXT, decided_by TEXT)"
    )
    conn.executemany(
        "INSERT INTO decisions VALUES (?, 'NU', ?, NULL, ?, ?, '[]', ?, ?)",
        [
            (7, "add", "high", "2026-08-07T12:00:00Z", "owner_review", "owner"),
            (8, "add", "medium", "2026-08-08T12:00:00Z", "model_review", "advisor"),
        ],
    )
    conn.commit()
    conn.close()

    response = work_os_client.get(f"/api/work-os/briefs/{body.artifact_id}/body")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert payload["schema_version"] == "report_reader_payload.v1"
    assert payload["artifact_id"] == body.artifact_id
    assert payload["ticker"] == "NU"
    assert "NU complete brief" in payload["body_html"]
    assert payload["style_url"] == "/api/work-os/report-reader.css"
    assert payload["decision"]["relationship"] == "agree"
    assert payload["decision"]["owner"]["decision_id"] == 7
    assert payload["decision"]["owner"]["revision"] == "2026-08-07T12:00:00Z"
    assert payload["decision"]["model"]["decision_id"] == 8
    assert payload["decision"]["model"]["revision"] == "2026-08-08T12:00:00Z"
    assert payload["sections"] == [
        {
            "section_id": "company",
            "dom_id": next(
                value.split('id="', 1)[1].split('"', 1)[0]
                for value in payload["body_html"].split("<")
                if 'data-tab="company"' in value and 'id="' in value
            ),
            "label": "Company",
        }
    ]
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


def test_shared_body_descriptor_keeps_body_route_when_file_is_missing(
    work_os_client: FlaskClient, work_os_app_repo: Path
) -> None:
    body = _persist_shared_brief(work_os_app_repo)
    artifact = next(
        item
        for item in load_report_artifact_index(work_os_app_repo).items
        if item.artifact_id == body.artifact_id
    )
    assert artifact.body_path is not None
    body_path = work_os_app_repo / artifact.body_path
    body_path.unlink()

    item = work_os_client.get("/api/work-os/briefs", query_string={"ticker": "NU"}).get_json()[
        "items"
    ][0]
    assert item["reader_mode"] == "shared_body"
    assert item["status"] == "degraded"
    assert item["body_url"] == f"/api/work-os/briefs/{body.artifact_id}/body"
    unavailable = work_os_client.get(item["body_url"])
    assert unavailable.status_code == 409
    assert unavailable.get_json()["status"] == "body_missing"
