"""Canonical source links preserve the displayed selection and read authority."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from comments_server_content_routes import ContentRouteContext, register_content_routes
from flask import Flask
from flask.testing import FlaskClient

from pipeline.peeks import render_canonical_financial_peek
from provenance.fact_plane_v2 import CanonicalJSONObject
from report.sections import financials
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database
from ui.source_chip import viewer_href


def content_client(conn: sqlite3.Connection | None, root: Path) -> tuple[FlaskClient, list[str]]:
    reads: list[str] = []

    def get_read_db() -> sqlite3.Connection:
        reads.append("read")
        if conn is None:
            pytest.fail("invalid reference must be rejected before database access")
        return conn

    app = Flask(__name__)
    register_content_routes(
        app,
        ContentRouteContext(
            repo_root=root,
            db_path=root / "synthetic.sqlite",
            open_db=get_read_db,
            get_read_db=get_read_db,
            safe_ticker=lambda ticker: ticker,
            build_ticker_command_center=lambda _root, _ticker: pytest.fail(
                "unrelated product access"
            ),
            linked_gsheet=lambda _root, _ticker: (None, None),
            probe_tracker=lambda: (False, "isolated"),
            fetch_live_portfolio=lambda: pytest.fail("tracker access prohibited"),
            default_user_id="synthetic",
        ),
    )
    return app.test_client(), reads


@pytest.mark.parametrize(
    "query",
    [
        "",
        "reference=invalid",
        "reference={}&reference={}",
        "reference={}&extra=1",
        "reference={}&fragment=0",
        "reference={}&fragment=1&fragment=1",
        "reference=" + "x" * 4097,
    ],
)
def test_invalid_reference_rejected_before_database(query: str, tmp_path: Path) -> None:
    client, reads = content_client(None, tmp_path)
    response = client.get("/api/peek/canonical-financial?" + query)
    assert response.status_code == 400
    assert "Invalid evidence reference" in response.text
    assert not reads


def _reference(database: sqlite3.Connection, root: Path) -> dict[str, object]:
    report = financials.build("SYNTH", root, conn=database, as_of=STAMP)
    source = report.line_items[0].sources_full[0]
    assert source is not None and source.canonical_reference is not None
    return source.canonical_reference.model_dump(mode="json")


def test_source_link_opens_exact_value_without_legacy_rows(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    source = report.line_items[0].sources_full[0]
    assert source is not None and source.doc_id is None
    href = viewer_href(source)
    assert href is not None
    client, reads = content_client(database, tmp_path)
    response = client.get(href)
    assert response.status_code == 200
    assert "100000000" in response.text and "USD" in response.text
    assert "/facts/0/value" in response.text
    assert source.canonical_reference is not None
    assert source.canonical_reference.observation_id in response.text
    assert source.canonical_reference.canonical_resolution_revision_id in response.text
    assert source.canonical_reference.metric_definition_revision_id in response.text
    assert len(reads) == 1
    # Operations disposition: the new endpoint is a read, with no action authority.
    assert client.post(href).status_code == 405
    assert "<form" not in response.text and "/actions/" not in response.text
    assert '<html lang="en"' in response.text
    fragment = client.get(href + "&fragment=1")
    assert fragment.status_code == 200 and '<div class="sv-frag">' in fragment.text
    assert "<html" not in fragment.text and "<style>" not in fragment.text
    assert source.canonical_reference.observation_id in fragment.text


@pytest.mark.parametrize(
    "field",
    [
        "observation_id",
        "canonical_metric_cell_id",
        "canonical_resolution_revision_id",
        "metric_definition_revision_id",
        "ticker",
        "concept",
    ],
)
def test_substituted_reference_never_falls_back(
    field: str, database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    reference = _reference(database, tmp_path)
    reference[field] = {"ticker": "OTHER", "concept": "net_income"}.get(field, "unrelated")
    client, _ = content_client(database, tmp_path)
    response = client.get(
        "/api/peek/canonical-financial", query_string={"reference": json.dumps(reference)}
    )
    assert response.status_code == 404
    assert "Evidence unavailable" in response.text
    assert "100000000" not in response.text and "/source/" not in response.text


@pytest.mark.parametrize("change", ["future", "naive", "unknown", "wrong_type", "oversized"])
def test_reference_boundary_is_strict_before_database(
    change: str, database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    reference = _reference(database, tmp_path)
    if change == "future":
        reference["as_of"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif change == "naive":
        reference["as_of"] = "2025-01-01T00:00:00"
    elif change == "unknown":
        reference["latest"] = True
    elif change == "wrong_type":
        reference["observation_id"] = 12
    else:
        reference["observation_id"] = "x" * 129
    client, reads = content_client(None, tmp_path)
    response = client.get(
        "/api/peek/canonical-financial", query_string={"reference": json.dumps(reference)}
    )
    assert response.status_code == 400 and not reads


def test_unavailable_cutoff_and_tampered_evidence_are_not_values(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    reference = _reference(database, tmp_path)
    client, _ = content_client(database, tmp_path)
    earlier = {**reference, "as_of": (STAMP - timedelta(days=1)).isoformat()}
    assert (
        client.get(
            "/api/peek/canonical-financial", query_string={"reference": json.dumps(earlier)}
        ).status_code
        == 404
    )
    # Simulate corrupted retained bytes on this disposable fixture. Normal
    # writes are blocked by the append-only trigger.
    database.execute("DROP TRIGGER trg_fact_observation_payload_commitments_v2_append_only")
    database.execute(
        "UPDATE fact_observation_payload_commitments_v2 SET observation_payload_sha256=? "
        "WHERE observation_id=?",
        ("0" * 64, reference["observation_id"]),
    )
    response = client.get(
        "/api/peek/canonical-financial", query_string={"reference": json.dumps(reference)}
    )
    assert response.status_code == 404
    assert "100000000" not in response.text


def test_retained_source_text_is_escaped(database: sqlite3.Connection, tmp_path: Path) -> None:
    seed_table(database, [("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD")])
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert report.canonical_financial_table is not None
    cell = report.canonical_financial_table.cells[0]
    assert cell.provenance is not None and cell.provenance.evidence is not None
    source = report.line_items[0].sources_full[0]
    assert source is not None and source.canonical_reference is not None
    hostile = '<script>alert("source")</script>'
    evidence = cell.provenance.evidence.model_copy(
        update={"source_locator": CanonicalJSONObject.model_validate({"path": hostile})}
    )
    escaped_cell = cell.model_copy(
        update={"provenance": cell.provenance.model_copy(update={"evidence": evidence})}
    )
    html = render_canonical_financial_peek(escaped_cell, source.canonical_reference)
    assert "<script>" not in html and "&lt;script&gt;" in html
