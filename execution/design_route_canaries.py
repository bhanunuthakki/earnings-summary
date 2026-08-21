"""Build deterministic browser canaries from the production Work OS renderer.

The browser guard must exercise the same HTML/CSS seam that the application
serves.  This module adds only test instrumentation (route identity and
semantic-role markers) to a production-rendered shell; it does not maintain a
second hand-written page or CSS vocabulary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from pathlib import Path

from pipeline.work_os_shell import render_work_os_shell
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
    """Render a complete deterministic brief through the report renderer."""

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
    return render_report_body(spec).body_html


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
        body = _canary_report_body()
        html = html.replace(
            '<div class="work-os-reader-body" id="workOsBriefReaderBody" role="region"'
            ' aria-live="polite"></div>',
            '<div class="work-os-reader-body" id="workOsBriefReaderBody" role="region"'
            f' aria-live="polite"><style id="design-canary-report-css">{WORKSPACE_REPORT_CSS}</style>{body}</div>',
            1,
        )
    marker = (
        f'<script id="design-canary-instrumentation">'
        f"window.__designCanaryRoute={escape(route)!r};"
        f"window.__designCanaryScreen={escape(screen_id)!r};"
        f"window.__designCanaryViewport={escape(viewport)!r};"
        "</script>"
    )
    harness = (
        '<style id="design-canary-harness">'
        ".design-canary-overlay{display:flex!important;transform:translateX(0)!important;"
        "visibility:visible!important;opacity:1!important;pointer-events:auto!important;"
        "z-index:9999!important;max-width:calc(100vw - 2 * var(--sp-4));}"
        "</style>"
    )
    return html.replace("</head>", harness + "\n</head>", 1).replace(
        "</body>", marker + "\n</body>", 1
    )


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
