"""Build deterministic browser canaries from the production Work OS renderer.

The browser guard must exercise the same HTML/CSS seam that the application
serves.  This module adds only test instrumentation (route identity and
semantic-role markers) to a production-rendered shell; it does not maintain a
second hand-written page or CSS vocabulary.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup

from pipeline.work_os_shell import render_work_os_shell
from report.legacy_body import extract_legacy_reader_body
from report.models import (
    AppendixSection,
    BearCaseSection,
    CompanyDescriptionSection,
    EarningsSection,
    FinancialsSection,
    IrDocsSection,
    ProvenanceSection,
    RecentDevelopmentsSection,
    ReportSpec,
    SayDoSection,
    SectionStatus,
    SegmentsSection,
    SnapshotSection,
    ThesisSection,
    ValuationSnapshot,
)
from report.renderers.workspace_html import render_report_body
from report.renderers.workspace_styles import CSS as WORKSPACE_REPORT_CSS
from report.renderers.workspace_styles import READER_OVERRIDE_CSS

ROUTE_SCREEN_IDS: dict[str, str] = {
    "cockpit": "screen-cockpit",
    "company-desk": "screen-workspace",
    "fact-metric-playground": "screen-analytics-playground",
    "operations": "screen-execution-queue",
    # The reader is mounted by the production shell beside the library. The
    # canary targets that actual reader seam rather than relabelling the
    # inventory screen as a full brief.
    "full-brief": "workOsBriefReader",
}

_CANARY_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def _canary_report_body() -> str:
    """Render and sanitize a complete deterministic persisted reader body."""

    spec = ReportSpec(
        ticker="CANARY",
        generation_date=_CANARY_TIMESTAMP.date(),
        repo_root=".",
        snapshot=SnapshotSection(
            status=SectionStatus.OK,
            ticker="CANARY",
            valuation=ValuationSnapshot(),
        ),
        company_description=CompanyDescriptionSection(status=SectionStatus.OK),
        thesis=ThesisSection(status=SectionStatus.OK),
        financials=FinancialsSection(status=SectionStatus.OK),
        segments=SegmentsSection(status=SectionStatus.OK),
        earnings=EarningsSection(status=SectionStatus.OK),
        saydo=SayDoSection(status=SectionStatus.OK),
        ir_docs=IrDocsSection(status=SectionStatus.OK),
        recent_developments=RecentDevelopmentsSection(status=SectionStatus.OK),
        bear_case=BearCaseSection(status=SectionStatus.OK),
        provenance=ProvenanceSection(status=SectionStatus.OK),
        appendix=AppendixSection(status=SectionStatus.OK),
    )
    rendered = render_report_body(spec).body_html
    return extract_legacy_reader_body(rendered, artifact_id="design-canary").body_html


def _canary_reader_payload() -> dict[str, object]:
    """Build the payload consumed by the production shared-body loader."""

    body = _canary_report_body()
    soup = BeautifulSoup(body, "html.parser")
    sections = [
        {
            "section_id": str(node.get("data-tab")),
            "dom_id": str(node.get("id")),
            "label": str(node.get("data-tab")).replace("_", " ").replace("-", " ").title(),
        }
        for node in soup.select("[data-tab][id]")
        if node.get("data-tab") and node.get("id")
    ]
    # Mirror workspace_reader_assets.READER_CSS without creating a second
    # visual-emitter edge in the route-canary manifest.
    reader_css = WORKSPACE_REPORT_CSS + READER_OVERRIDE_CSS
    stylesheet = base64.b64encode(reader_css.encode("utf-8")).decode("ascii")
    return {
        "schema_version": "report_reader_payload.v1",
        "artifact_id": "design-canary",
        "ticker": "CANARY",
        "title": "CANARY Complete Research Brief",
        "body_html": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "style_url": f"data:text/css;base64,{stylesheet}",
        "sections": sections,
        "decision": {"relationship": "agree", "freshness": "current"},
    }


def render_route_canary(*, route: str, viewport: str, db_path: Path | None = None) -> str:
    """Render one route specimen from the production shell.

    ``viewport`` is intentionally part of the deterministic input even though
    responsive layout is owned by production CSS.  The marker makes fixture
    provenance auditable without changing the application's route registry.
    """

    try:
        screen_id = ROUTE_SCREEN_IDS[route]
    except KeyError as exc:
        raise ValueError(f"unknown design canary route: {route!r}") from exc
    if viewport not in {"desktop", "narrow"}:
        raise ValueError(f"unknown design canary viewport: {viewport!r}")

    html = render_work_os_shell(generated_at=_CANARY_TIMESTAMP, db_path=db_path)
    if route == "full-brief":
        payload_json = json.dumps(_canary_reader_payload()).replace("</", "<\\/")
        artifact_json = json.dumps(
            {
                "artifact_id": "design-canary",
                "ticker": "CANARY",
                "title": "Complete Research Brief",
                "report_date": "2026-01-01",
                "coverage_role": "active",
                "reader_mode": "shared_body",
                "body_url": "/design-canary/body",
                "standalone_url": "/reports/CANARY",
            }
        )
        loader = (
            '<script id="design-canary-reader-loader">'
            f"const designCanaryPayload={payload_json};"
            "const designCanaryFetch=window.fetch.bind(window);"
            "window.fetch=(input,init)=>String(input).includes('/design-canary/body')"
            "?Promise.resolve(new Response(JSON.stringify(designCanaryPayload),"
            "{status:200,headers:{'Content-Type':'application/json'}}))"
            ":designCanaryFetch(input,init);"
            f"window.__designCanaryReaderReady=window.openWorkOsBriefReader({artifact_json});"
            "</script>"
        )
        html = html.replace("</body>", loader + "\n</body>", 1)
    marker = (
        f'<script id="design-canary-instrumentation">'
        f"window.__designCanaryRoute={escape(route)!r};"
        f"window.__designCanaryScreen={escape(screen_id)!r};"
        f"window.__designCanaryViewport={escape(viewport)!r};"
        "</script>"
    )
    return html.replace("</body>", marker + "\n</body>", 1)


def write_route_canary_fixtures(root: Path) -> tuple[Path, ...]:
    """Materialize generated fixtures for adversarial mutation tests only."""

    destination = root / "tests" / "fixtures" / "design_canaries"
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for route in ROUTE_SCREEN_IDS:
        for viewport in ("desktop", "narrow"):
            path = destination / f"{route}.{viewport}.html"
            path.write_text(render_route_canary(route=route, viewport=viewport), encoding="utf-8")
            paths.append(path)
    return tuple(paths)


__all__ = ["ROUTE_SCREEN_IDS", "render_route_canary", "write_route_canary_fixtures"]
