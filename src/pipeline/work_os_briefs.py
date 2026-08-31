"""Narrow Brief Library projection over the compact report artifact index."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, ValidationError

from pipeline.work_os_decisions import DecisionProjection
from provenance.selection import selected_transcripts_relation
from report.artifacts import CoverageRole, ReportArtifactRef, load_report_artifact_index

BriefStatus = Literal["available", "degraded"]
ArtifactKind = Literal["full_brief", "pre_earnings", "post_earnings"]
LibraryReaderMode = Literal["shared_body", "legacy_standalone", "peek"]

_ARTIFACT_KIND_LABELS: dict[ArtifactKind, str] = {
    "full_brief": "Brief",
    "pre_earnings": "Pre-Earnings",
    "post_earnings": "Post-Earnings",
}
_PURPOSE_KINDS: dict[str, ArtifactKind] = {
    "pre_earnings_brief": "pre_earnings",
    "post_earnings_readout": "post_earnings",
}
_PERIOD_PATTERNS = (
    re.compile(r"^Q([1-4])\s+(\d{4})$", re.IGNORECASE),
    re.compile(r"^(\d{4})\s+Q([1-4])$", re.IGNORECASE),
)


def _compact_period(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    first = _PERIOD_PATTERNS[0].fullmatch(raw)
    if first:
        return f"Q{first.group(1)} {first.group(2)[-2:]}"
    second = _PERIOD_PATTERNS[1].fullmatch(raw)
    if second:
        return f"Q{second.group(2)} {second.group(1)[-2:]}"
    return None


def format_artifact_title(ticker: str, period_label: str | None, kind: ArtifactKind) -> str:
    """Return the approved compact identity without inventing a period."""

    parts = [ticker.strip().upper()]
    compact_period = _compact_period(period_label)
    if compact_period:
        parts.append(compact_period)
    parts.append(_ARTIFACT_KIND_LABELS[kind])
    return " ".join(parts)


class BriefLibraryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str
    ticker: str
    title: str
    artifact_kind: ArtifactKind
    coverage_role: CoverageRole
    fiscal_period_label: str | None = None
    report_date: str
    generated_at: str
    reader_mode: LibraryReaderMode
    status: BriefStatus
    open_url: str
    body_url: str | None
    standalone_url: str
    section_count: int
    source_count: int | None = None
    comment_count: int | None = None


class BriefFacetOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    label: str
    count: int


class BriefLibraryFacets(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_kind: tuple[BriefFacetOption, ...]
    ticker: tuple[BriefFacetOption, ...]
    coverage_role: tuple[BriefFacetOption, ...]


class BriefLibraryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["brief_library.v2"] = "brief_library.v2"
    inventory_revision: str
    items: tuple[BriefLibraryItem, ...]
    facets: BriefLibraryFacets
    next_cursor: str | None


class _EarningsArtifactRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    ticker: str
    purpose: str
    fiscal_period: str | None
    generated_at: str
    fiscal_period_label: str | None


class ReportReaderSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_id: str
    dom_id: str
    label: str


class ReportReaderPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["report_reader_payload.v1"] = "report_reader_payload.v1"
    artifact_id: str
    ticker: str
    title: str
    body_html: str
    body_sha256: str
    section_ids: tuple[str, ...]
    sections: tuple[ReportReaderSection, ...]
    decision: DecisionProjection
    style_url: Literal["/api/work-os/report-reader.css"] = "/api/work-os/report-reader.css"


def _status(repo_root: Path, artifact: ReportArtifactRef) -> BriefStatus:
    if artifact.reader_mode == "legacy_standalone" or artifact.body_path is None:
        return "degraded"
    return "available" if (repo_root / artifact.body_path).is_file() else "degraded"


def build_brief_descriptor(
    repo_root: Path,
    artifact: ReportArtifactRef,
    *,
    coverage_role: CoverageRole | None = None,
) -> BriefLibraryItem:
    """Normalize every Work OS launcher onto one stable reader descriptor."""

    status = _status(repo_root, artifact)
    standalone_url = f"/reports/{artifact.ticker}?artifact_id={artifact.artifact_id}"
    return BriefLibraryItem(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        title=format_artifact_title(
            artifact.ticker,
            artifact.fiscal_period_label,
            "full_brief",
        ),
        artifact_kind=artifact.artifact_kind,
        coverage_role=coverage_role or artifact.coverage_role,
        fiscal_period_label=artifact.fiscal_period_label,
        report_date=artifact.report_date.isoformat(),
        generated_at=artifact.generated_at.isoformat().replace("+00:00", "Z"),
        reader_mode=artifact.reader_mode,
        status=status,
        open_url=standalone_url,
        body_url=(
            f"/api/work-os/briefs/{artifact.artifact_id}/body"
            if artifact.reader_mode == "shared_body"
            else None
        ),
        standalone_url=standalone_url,
        section_count=len(artifact.section_ids),
    )


def _current_company_info(
    conn: sqlite3.Connection | None, tickers: set[str]
) -> tuple[dict[str, CoverageRole], dict[str, str]]:
    """Return active governed coverage and names using the request connection."""

    if conn is None:
        return {}, {}
    normalized = sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()})
    if not normalized:
        return {}, {}
    placeholders = ", ".join("?" for _ in normalized)
    rows = conn.execute(
        "SELECT ticker, name, list_type FROM tracked_companies "
        f"WHERE archived_at IS NULL AND UPPER(ticker) IN ({placeholders})",  # nosec B608 -- placeholder count is internal; ticker values remain bound
        normalized,
    ).fetchall()
    resolved: dict[str, CoverageRole] = {}
    names: dict[str, str] = {}
    for row in rows:
        raw_role = str(row["list_type"] if isinstance(row, sqlite3.Row) else row[2]).lower()
        ticker = str(row["ticker"] if isinstance(row, sqlite3.Row) else row[0]).upper()
        raw_name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if raw_name is not None and str(raw_name).strip():
            names[ticker] = str(raw_name).strip()
        if raw_role == "portfolio":
            resolved[ticker] = "portfolio"
        elif raw_role == "evaluation":
            resolved[ticker] = "evaluation"
        else:
            resolved[ticker] = "unknown"
    return resolved, names


def _current_coverage_roles(
    conn: sqlite3.Connection | None, artifacts: tuple[ReportArtifactRef, ...]
) -> dict[str, CoverageRole]:
    """Compatibility helper for callers that project report artifacts only."""

    roles, _ = _current_company_info(conn, {artifact.ticker for artifact in artifacts})
    return roles


def _earnings_artifacts(conn: sqlite3.Connection | None) -> tuple[_EarningsArtifactRow, ...]:
    """Load the latest current pre/post artifact per ticker and purpose.

    Missing or pre-artifact schemas fail closed to an empty tuple. This keeps
    the indexed full-brief library usable during migrations while never
    inventing an earnings artifact from a partial row.
    """

    if conn is None:
        return ()
    try:
        relation = selected_transcripts_relation(conn)
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT a.id, UPPER(a.ticker) AS ticker, a.purpose,
                       a.fiscal_period, a.generated_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY UPPER(a.ticker), a.purpose
                           ORDER BY datetime(a.generated_at) DESC, a.id DESC
                       ) AS artifact_rank
                FROM llm_artifacts AS a
                WHERE a.scope = 'ticker'
                  AND a.purpose IN ('pre_earnings_brief', 'post_earnings_readout')
                  AND a.superseded_by_id IS NULL
                  AND TRIM(COALESCE(a.content_md, '')) != ''
                  AND TRIM(COALESCE(a.ticker, '')) != ''
            )
            SELECT a.id, a.ticker, a.purpose, a.fiscal_period, a.generated_at,
                   CASE WHEN a.purpose = 'pre_earnings_brief' THEN (
                       SELECT e.fiscal_period_label
                       FROM expected_earnings AS e
                       WHERE UPPER(e.ticker) = a.ticker
                         AND date(e.expected_date) = date(a.fiscal_period)
                       ORDER BY e.id DESC LIMIT 1
                   ) ELSE (
                       SELECT CASE
                           WHEN TRIM(COALESCE(t.fiscal_period_type, '')) != ''
                             AND strftime('%Y', t.period_end) IS NOT NULL
                           THEN TRIM(t.fiscal_period_type) || ' ' || strftime('%Y', t.period_end)
                           ELSE NULL END
                       FROM {relation.sql} AS t
                       WHERE UPPER(t.ticker) = a.ticker
                         AND date(t.period_end) = date(a.fiscal_period)
                       ORDER BY t.id DESC LIMIT 1
                   ) END AS fiscal_period_label
            FROM ranked AS a
            WHERE a.artifact_rank = 1
            """  # nosec B608 -- selected relation is a trusted internal SQL shape
        ).fetchall()
    except sqlite3.Error:
        return ()
    projected: list[_EarningsArtifactRow] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            row_keys = row.keys()
            values: dict[str, object] = {str(key): row[key] for key in row_keys}
        else:
            values = {
                "id": row[0],
                "ticker": row[1],
                "purpose": row[2],
                "fiscal_period": row[3],
                "generated_at": row[4],
                "fiscal_period_label": row[5],
            }
        try:
            projected.append(_EarningsArtifactRow.model_validate(values))
        except ValidationError:
            continue
    return tuple(projected)


def _peek_descriptor(
    row: _EarningsArtifactRow, *, coverage_role: CoverageRole
) -> BriefLibraryItem | None:
    kind = _PURPOSE_KINDS.get(row.purpose)
    ticker = row.ticker.strip().upper()
    numeric_id = row.id
    if kind is None or not ticker:
        return None
    generated_at = row.generated_at.strip()
    report_date = str(row.fiscal_period or "").strip()[:10]
    fiscal_period_label = str(row.fiscal_period_label or "").strip() or None
    endpoint = "earnings-prep" if kind == "pre_earnings" else "earnings-readout"
    open_url = f"/api/peek/{endpoint}?ticker={ticker}&artifact_id={numeric_id}"
    return BriefLibraryItem(
        artifact_id=f"llm:{numeric_id}",
        ticker=ticker,
        title=format_artifact_title(ticker, fiscal_period_label, kind),
        artifact_kind=kind,
        coverage_role=coverage_role,
        fiscal_period_label=fiscal_period_label,
        report_date=report_date,
        generated_at=generated_at,
        reader_mode="peek",
        status="available",
        open_url=open_url,
        body_url=None,
        standalone_url=open_url,
        section_count=0,
    )


def _matches(
    item: BriefLibraryItem,
    *,
    artifact_kind: ArtifactKind | None,
    ticker: str | None,
    coverage_role: CoverageRole | None,
    status: BriefStatus | None,
) -> bool:
    return (
        (artifact_kind is None or item.artifact_kind == artifact_kind)
        and (ticker is None or item.ticker == ticker.upper())
        and (coverage_role is None or item.coverage_role == coverage_role)
        and (status is None or item.status == status)
    )


def _facet_options(
    universe: tuple[BriefLibraryItem, ...],
    *,
    artifact_kind: ArtifactKind | None,
    ticker: str | None,
    coverage_role: CoverageRole | None,
    status: BriefStatus | None,
    names: dict[str, str],
) -> BriefLibraryFacets:
    kind_counts: dict[ArtifactKind, int] = {
        kind: sum(
            _matches(
                item,
                artifact_kind=kind,
                ticker=ticker,
                coverage_role=coverage_role,
                status=status,
            )
            for item in universe
        )
        for kind in _ARTIFACT_KIND_LABELS
    }
    ticker_values = sorted({item.ticker for item in universe})
    ticker_counts = {
        value: sum(
            _matches(
                item,
                artifact_kind=artifact_kind,
                ticker=value,
                coverage_role=coverage_role,
                status=status,
            )
            for item in universe
        )
        for value in ticker_values
    }
    coverage_values: tuple[CoverageRole, ...] = ("portfolio", "evaluation", "unknown")
    coverage_counts = {
        value: sum(
            _matches(
                item,
                artifact_kind=artifact_kind,
                ticker=ticker,
                coverage_role=value,
                status=status,
            )
            for item in universe
        )
        for value in coverage_values
    }
    coverage_labels = {
        "portfolio": "Portfolio",
        "evaluation": "Evaluation",
        "unknown": "Unknown",
    }
    return BriefLibraryFacets(
        artifact_kind=tuple(
            BriefFacetOption(value=value, label=_ARTIFACT_KIND_LABELS[value], count=count)
            for value, count in kind_counts.items()
            if count
        ),
        ticker=tuple(
            BriefFacetOption(
                value=value,
                label=f"{value} · {names[value]}" if value in names else value,
                count=count,
            )
            for value, count in ticker_counts.items()
            if count
        ),
        coverage_role=tuple(
            BriefFacetOption(value=value, label=coverage_labels[value], count=count)
            for value, count in coverage_counts.items()
            if count
        ),
    )


def build_brief_library(
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    ticker: str | None = None,
    artifact_kind: ArtifactKind | None = None,
    coverage_role: CoverageRole | None = None,
    status: BriefStatus | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> BriefLibraryResponse:
    """Return one bounded page of all persisted research artifacts."""

    index = load_report_artifact_index(repo_root)
    latest_by_ticker: dict[str, ReportArtifactRef] = {}
    for artifact in index.items:
        prior = latest_by_ticker.get(artifact.ticker)
        if prior is None or (artifact.generated_at, artifact.artifact_id) > (
            prior.generated_at,
            prior.artifact_id,
        ):
            latest_by_ticker[artifact.ticker] = artifact
    current = sorted(
        latest_by_ticker.values(),
        key=lambda artifact: (artifact.generated_at, artifact.artifact_id),
        reverse=True,
    )
    earnings_rows = _earnings_artifacts(conn)
    all_tickers = {artifact.ticker.upper() for artifact in current} | {
        row.ticker.strip().upper() for row in earnings_rows
    }
    current_roles, company_names = _current_company_info(conn, all_tickers)
    full_items = tuple(
        build_brief_descriptor(
            repo_root,
            artifact,
            coverage_role=current_roles.get(artifact.ticker.upper()),
        )
        for artifact in current
    )
    peek_items = tuple(
        descriptor
        for row in earnings_rows
        if (
            descriptor := _peek_descriptor(
                row,
                coverage_role=current_roles.get(row.ticker.strip().upper(), "unknown"),
            )
        )
        is not None
    )
    universe = tuple(
        sorted(
            (*full_items, *peek_items),
            key=lambda item: (item.generated_at, item.artifact_id),
            reverse=True,
        )
    )
    filtered = [
        item
        for item in universe
        if _matches(
            item,
            artifact_kind=artifact_kind,
            ticker=ticker,
            coverage_role=coverage_role,
            status=status,
        )
    ]
    start = 0
    if cursor is not None:
        for position, item in enumerate(filtered):
            if item.artifact_id == cursor:
                start = position + 1
                break
        else:
            raise ValueError("unknown brief-library cursor")
    page = filtered[start : start + limit]
    has_more = start + limit < len(filtered)
    revision_material = "\n".join(
        f"{item.artifact_id}:{item.generated_at}" for item in universe
    ).encode("utf-8")
    return BriefLibraryResponse(
        inventory_revision=hashlib.sha256(revision_material).hexdigest()[:16],
        items=tuple(page),
        facets=_facet_options(
            universe,
            artifact_kind=artifact_kind,
            ticker=ticker,
            coverage_role=coverage_role,
            status=status,
            names=company_names,
        ),
        next_cursor=page[-1].artifact_id if has_more and page else None,
    )


def resolve_report_artifact(repo_root: Path, artifact_id: str) -> ReportArtifactRef | None:
    return next(
        (
            artifact
            for artifact in load_report_artifact_index(repo_root).items
            if artifact.artifact_id == artifact_id
        ),
        None,
    )


def load_report_reader_payload(
    repo_root: Path,
    artifact_id: str,
    *,
    decision: DecisionProjection,
) -> ReportReaderPayload:
    """Load one checksum-verified inert body; never parse a standalone at request time."""

    artifact = resolve_report_artifact(repo_root, artifact_id)
    if artifact is None:
        raise LookupError(artifact_id)
    if artifact.reader_mode != "shared_body" or artifact.body_path is None:
        raise ValueError("legacy_standalone")
    body_path = repo_root / artifact.body_path
    if not body_path.is_file():
        raise FileNotFoundError(body_path)
    body_html = body_path.read_text(encoding="utf-8")
    body_sha256 = hashlib.sha256(body_html.encode("utf-8")).hexdigest()
    if artifact.body_sha256 is None or body_sha256 != artifact.body_sha256:
        raise ValueError("body_checksum_mismatch")
    soup = BeautifulSoup(body_html, "html.parser")
    dom_by_logical: dict[str, str] = {}
    for node in soup.select("[data-tab][id]"):
        logical = str(node.get("data-tab") or "").strip()
        dom_id = str(node.get("id") or "").strip()
        if logical and dom_id and logical not in dom_by_logical:
            dom_by_logical[logical] = dom_id
    sections = tuple(
        ReportReaderSection(
            section_id=section_id,
            dom_id=dom_by_logical[section_id],
            label=section_id.replace("_", " ").replace("-", " ").title(),
        )
        for section_id in artifact.section_ids
        if section_id in dom_by_logical
    )
    return ReportReaderPayload(
        artifact_id=artifact.artifact_id,
        ticker=artifact.ticker,
        title=format_artifact_title(
            artifact.ticker,
            artifact.fiscal_period_label,
            "full_brief",
        ),
        body_html=body_html,
        body_sha256=body_sha256,
        section_ids=artifact.section_ids,
        sections=sections,
        decision=decision,
    )


__all__ = [
    "BriefLibraryFacets",
    "BriefLibraryItem",
    "BriefLibraryResponse",
    "BriefStatus",
    "ReportReaderPayload",
    "ReportReaderSection",
    "build_brief_descriptor",
    "build_brief_library",
    "format_artifact_title",
    "load_report_reader_payload",
    "resolve_report_artifact",
]
