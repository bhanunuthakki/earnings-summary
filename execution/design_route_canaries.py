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
import sqlite3
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup

from pipeline.explore_panel import render_explore_panel
from pipeline.operations_panel import OperationsPanelView, render_operations_panel
from pipeline.portfolio_console_panel import (
    render_portfolio_allocation_panel,
    render_portfolio_health_panel,
    render_portfolio_record_panel,
)
from pipeline.work_os_shell import SCREEN_SPECS, render_work_os_shell
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
    "performance": "screen-performance",
    "risk-allocations": "screen-allocation",
    "company-desk": "screen-workspace",
    "brief-library": "screen-brief-library",
    "fact-metric-playground": "screen-analytics-playground",
    "decision-audit": "screen-audit-log",
    "operations": "screen-execution-queue",
    # The reader is mounted by the production shell beside the library. The
    # canary targets that actual reader seam rather than relabelling the
    # inventory screen as a full brief.
    "full-brief": "workOsBriefReader",
}

_PERSISTENT_SCREEN_IDS = frozenset(
    screen_id for route, screen_id in ROUTE_SCREEN_IDS.items() if route != "full-brief"
)
_DECLARED_SCREEN_IDS = frozenset(screen.screen_id for screen in SCREEN_SPECS)
if _PERSISTENT_SCREEN_IDS != _DECLARED_SCREEN_IDS:
    missing = sorted(_DECLARED_SCREEN_IDS - _PERSISTENT_SCREEN_IDS)
    unexpected = sorted(_PERSISTENT_SCREEN_IDS - _DECLARED_SCREEN_IDS)
    raise RuntimeError(
        "design route census must exactly match SCREEN_SPECS; "
        f"missing={missing!r}, unexpected={unexpected!r}"
    )

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


def _canary_explore_fragment() -> str:
    """Render the production Explore fragment against an isolated empty store."""

    with TemporaryDirectory(prefix="design-canary-") as directory:
        return render_explore_panel(
            Path(directory) / "explore.db",
            initial_tickers=[],
            include_runtime=False,
        )


def _canary_operations_fragment() -> str:
    """Render the production Operations fragment from a deterministic typed view."""

    return render_operations_panel(
        OperationsPanelView(
            observed_label="Observed 2026-01-01 00:00 UTC",
            attention_count=0,
            runtime_summary_tone="ok",
            tasks=(),
            runtime_rows=(),
        )
    )


def _canary_shell_payloads() -> dict[str, object]:
    """Return production-schema payloads for dynamic shell card populations."""

    company = {
        "ticker": "NU",
        "name": "Canary Company",
        "coverage_role": "portfolio",
        "current_weight_pct": 4.2,
        "price": 12.0,
        "fair_value": 15.0,
        "thesis_status": "intact",
        "report_url": "/reports/NU",
        "earnings_route": "/api/peek/earnings-readout?ticker=NU",
        "earnings_label": "Open earnings research",
    }
    brief = {
        "artifact_id": "design-canary-brief",
        "ticker": "NU",
        "title": "NU Complete Research Brief",
        "report_date": "2026-01-01",
        "coverage_role": "portfolio",
        "reader_mode": "shared_body",
        "artifact_kind": "full_brief",
        "status": "available",
        "body_url": "/design-canary/body",
        "standalone_url": "/reports/NU",
    }
    return {
        "portfolio": {
            "status": "ok",
            "as_of": "2026-01-01",
            "total_market_value": 100000.0,
            "companies": [company],
            "actions": [
                {
                    "ticker": "NU",
                    "headline": "Review the canary thesis",
                    "detail": "Deterministic dynamic action-card population",
                }
            ],
            "earnings_readouts": [
                {
                    "artifact_id": "design-canary-readout",
                    "ticker": "NU",
                    "period_label": "Q4 2025",
                    "fiscal_period": "2025-12-31",
                    "coverage_role": "portfolio",
                    "generated_at": "2026-01-01T00:00:00Z",
                    "route": "/api/peek/earnings-readout?ticker=NU",
                }
            ],
        },
        "tickers": {
            "tickers": [{"ticker": "NU", "name": "Canary Company", "list_type": "portfolio"}]
        },
        "briefs": {"items": [brief]},
        "desk": {
            "company": company,
            "current_decision": {"relationship": "unavailable", "freshness": "current"},
            "position": {"weight_pct": 4.2, "price": 12.0, "fair_value": 15.0, "currency": "USD"},
            "latest_brief": brief,
            "conditions": [],
            "open_questions": [],
            "warnings": [],
        },
        "operations": _canary_operations_fragment(),
    }


def _canary_shell_loader() -> str:
    payload_json = json.dumps(_canary_shell_payloads()).replace("</", "<\\/")
    return (
        '<script id="design-canary-shell-loader">'
        f"const designCanaryShellPayloads={payload_json};"
        "const designCanaryShellFetch=window.fetch.bind(window);"
        "window.fetch=(input,init)=>{const url=String(input);"
        "if(url==='/api/work-os/portfolio')return Promise.resolve(new Response(JSON.stringify(designCanaryShellPayloads.portfolio),{status:200,headers:{'Content-Type':'application/json'}}));"
        "if(url==='/api/tickers')return Promise.resolve(new Response(JSON.stringify(designCanaryShellPayloads.tickers),{status:200,headers:{'Content-Type':'application/json'}}));"
        "if(url.startsWith('/api/work-os/briefs?'))return Promise.resolve(new Response(JSON.stringify(designCanaryShellPayloads.briefs),{status:200,headers:{'Content-Type':'application/json'}}));"
        "if(url==='/api/work-os/companies/NU/desk')return Promise.resolve(new Response(JSON.stringify(designCanaryShellPayloads.desk),{status:200,headers:{'Content-Type':'application/json'}}));"
        "if(url==='/api/panel/operations')return Promise.resolve(new Response(designCanaryShellPayloads.operations,{status:200,headers:{'Content-Type':'text/html'}}));"
        "return designCanaryShellFetch(input,init);};"
        "</script>"
    )


def _canary_portfolio_fragment(route: str, db_path: Path | None) -> str:
    """Render the real portfolio-console fragment for one persistent route."""

    if db_path is not None:
        if route == "performance":
            return render_portfolio_allocation_panel(db_path, db_path.parent.parent)
        if route == "risk-allocations":
            return render_portfolio_health_panel(db_path)
        if route == "decision-audit":
            return render_portfolio_record_panel(db_path)
        raise ValueError(f"unknown portfolio design canary route: {route!r}")

    with TemporaryDirectory(prefix="design-canary-portfolio-") as directory:
        isolated_root = Path(directory)
        isolated_db = isolated_root / "data" / "portfolio.db"
        isolated_db.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(isolated_db).close()
        if route == "performance":
            return render_portfolio_allocation_panel(isolated_db, isolated_root)
        if route == "risk-allocations":
            return render_portfolio_health_panel(isolated_db)
        if route == "decision-audit":
            return render_portfolio_record_panel(isolated_db)
    raise ValueError(f"unknown portfolio design canary route: {route!r}")


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
    html = html.replace(
        '<script id="work-os-production-runtime">',
        _canary_shell_loader() + '\n<script id="work-os-production-runtime">',
        1,
    )
    if route in {"performance", "risk-allocations", "decision-audit"}:
        fragment_json = json.dumps(_canary_portfolio_fragment(route, db_path)).replace("</", "<\\/")
        endpoint = next(screen.endpoint for screen in SCREEN_SPECS if screen.screen_id == screen_id)
        loader = (
            '<script id="design-canary-portfolio-loader">'
            f"const designCanaryPortfolioFragment={fragment_json};"
            "const designCanaryPortfolioFetch=window.fetch.bind(window);"
            f"window.fetch=(input,init)=>String(input)==={json.dumps(endpoint)}"
            "?Promise.resolve(new Response(designCanaryPortfolioFragment,"
            "{status:200,headers:{'Content-Type':'text/html'}}))"
            ":designCanaryPortfolioFetch(input,init);"
            "</script>"
        )
        html = html.replace("</body>", loader + "\n</body>", 1)
    if route == "fact-metric-playground":
        fragment_json = json.dumps(_canary_explore_fragment()).replace("</", "<\\/")
        loader = (
            '<script id="design-canary-explore-loader">'
            f"const designCanaryExploreFragment={fragment_json};"
            "const designCanaryExploreFetch=window.fetch.bind(window);"
            "window.fetch=(input,init)=>String(input)==='/api/tickers'"
            "?Promise.resolve(new Response(JSON.stringify({tickers:[{ticker:'CANARY',name:'Canary Company',list_type:'evaluation'}]}),"
            "{status:200,headers:{'Content-Type':'application/json'}}))"
            ":String(input).startsWith('/api/panel/explore?')"
            "?Promise.resolve(new Response(designCanaryExploreFragment,"
            "{status:200,headers:{'Content-Type':'text/html'}}))"
            ":designCanaryExploreFetch(input,init);"
            "</script>"
        )
        html = html.replace("</body>", loader + "\n</body>", 1)
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
