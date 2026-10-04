"""Calculated growth opens each original input and its calculation rule."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from calendar import monthrange
from collections.abc import Callable
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from report.models import SectionStatus, SegmentSeries, SegmentsSection
from report.renderers.workspace_sections import financials as financial_renderer
from report.renderers.workspace_sections.financials import _line_items_levels_panel
from report.sections import financials
from tests.test_canonical_financial_peek import content_client
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


def test_segment_drill_names_its_bounded_evidence_state() -> None:
    render = cast(
        Callable[[StringIO, list[SegmentSeries], list[str]], None],
        getattr(financial_renderer, "_segment_drill_table"),
    )
    body = StringIO()
    render(
        body,
        [
            SegmentSeries(
                segment_name="Synthetic segment",
                metric="revenue_by_product",
                quarters=["2025 Q1"],
                values=[50.0],
                source_label="vendor-only <breakout>",
            )
        ],
        ["2025 Q1"],
    )
    assert "Exact observation evidence is unavailable" in body.getvalue()
    assert "vendor-only &lt;breakout&gt;" in body.getvalue()
    assert "/api/peek/canonical-financial" not in body.getvalue()
    assert "50.0" in body.getvalue()


@pytest.mark.parametrize(
    "query",
    [
        "",
        "reference=invalid",
        "reference={}&reference={}",
        "reference={}&extra=1",
        "reference={}&fragment=0",
        "reference={}&fragment=1&fragment=1",
        pytest.param("reference=" + "x" * 32769, id="oversized-reference"),
    ],
)
def test_invalid_growth_reference_precedes_database(query: str, tmp_path: Path) -> None:
    client, reads = content_client(None, tmp_path)
    assert client.get("/api/peek/financial-calculation?" + query).status_code == 400
    assert reads == []


@pytest.mark.parametrize("change", ["observation", "order", "cutoff", "mode", "version"])
def test_growth_reference_cannot_substitute_inputs(
    database: sqlite3.Connection, tmp_path: Path, change: str
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "150000000", "USD"),
        ],
    )
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    reference = report.line_items[0].growth_evidence["qoq"]
    payload = json.loads(reference.model_dump_json())
    expected = 404
    if change == "observation":
        payload["inputs"][0]["observation_id"] = "unrelated"
    elif change == "order":
        payload["inputs"].reverse()
    elif change == "cutoff":
        payload["inputs"][0]["as_of"] = (STAMP + timedelta(days=1)).isoformat()
        expected = 400
    elif change == "mode":
        payload["inputs"][0]["reader_kind"] = "series"
        expected = 400
    else:
        payload["calculation_version"] = "arbitrary"
        expected = 400
    client, reads = content_client(database if expected == 404 else None, tmp_path)
    response = client.get(
        "/api/peek/financial-calculation", query_string={"reference": json.dumps(payload)}
    )
    assert response.status_code == expected
    assert len(reads) == (1 if expected == 404 else 0)


def test_growth_windows_keep_ttm_and_endpoint_formulas_distinct(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    rows: list[tuple[str, str, str, str, str, str]] = []
    levels = [100 + index * index for index in range(16)]
    for index, value in enumerate(levels):
        year = 2021 + index // 4
        month = index % 4 * 3 + 1
        start = date(year, month, 1)
        end = date(year, month + 2, monthrange(year, month + 2)[1])
        rows.append(
            (
                "revenue",
                start.isoformat(),
                end.isoformat(),
                f"Q{index % 4 + 1}",
                str(value * 1_000_000),
                "USD",
            )
        )
    seed_table(database, rows, fiscal_years={index: 2021 + index // 4 for index in range(16)})
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    row = report.line_items[0]
    ttm = row.growth_evidence["cagr_3y_ttm"]
    endpoint = row.level_cagr_evidence["3"]
    assert len(ttm.inputs) == 16 and len(endpoint.inputs) == 13
    assert ttm.formula == "cagr_3y_ttm" and endpoint.formula == "cagr_3y_level"
    expected_ttm = (sum(levels[-4:]) / sum(levels[:4])) ** (1 / 3) - 1
    expected_endpoint = (levels[-1] / levels[-13]) ** (1 / 3) - 1
    assert expected_ttm != pytest.approx(expected_endpoint)
    assert row.growth.cagr_3y_ttm == pytest.approx(expected_ttm)
    client, _ = content_client(database, tmp_path)
    for reference, expected in ((ttm, expected_ttm), (endpoint, expected_endpoint)):
        response = client.get(
            "/api/peek/financial-calculation",
            query_string={"reference": reference.model_dump_json()},
        )
        assert response.status_code == 200 and f"{expected:.1%}" in response.text
        assert response.headers["Cache-Control"] == "no-store"
        for point in reference.inputs:
            assert point.observation_id in response.text


def test_growth_value_opens_both_original_observations(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    seed_table(
        database,
        [
            ("revenue", "2025-01-01", "2025-03-31", "Q1", "100000000", "USD"),
            ("revenue", "2025-04-01", "2025-06-30", "Q2", "150000000", "USD"),
        ],
    )
    report = financials.build("SYNTH", tmp_path, conn=database, as_of=STAMP)
    assert report.line_items[0].growth.qoq == 0.5
    output = StringIO()
    _line_items_levels_panel(output, report, SegmentsSection(status=SectionStatus.MISSING_DATA))
    links = re.findall(r'href="([^"]*?/api/peek/financial-calculation[^\"]*)"', output.getvalue())
    assert links, "Calculated growth has no evidence doorway"
    client, _ = content_client(database, tmp_path)
    response = client.get(html.unescape(links[0]))
    assert response.status_code == 200
    assert "50.0%" in response.text
    assert "canonical-growth/v1" in response.text
    for source in report.line_items[0].sources_full:
        assert source is not None and source.canonical_reference is not None
        assert source.canonical_reference.observation_id in response.text
    assert "100000000" in response.text and "150000000" in response.text
    assert client.post(html.unescape(links[0])).status_code == 405
