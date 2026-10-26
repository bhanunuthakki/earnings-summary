"""Saved ETF research history — the old ETF workup peek, now a read-only reader.

``GET /api/peek/etf_workup`` still resolves a tracked ETF, but the fragment no
longer renders an active workup. It lists the saved ``etf_role_synthesis``
artifacts (current and superseded) from ``llm_artifacts`` so the historical
record stays readable. No profile/style/overlap/what-if generation, no current
weight recommendation, and no provider/LLM/artifact writes happen on this path.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from llm_artifact_store import Artifact, HistoricalArtifactVersion, historical_versions
from pipeline.analysis_styles import ANALYSIS_STYLE
from report.etf_models import EtfRoleSynthesis
from ui.prose import render_prose

_PAGE_SIZE = 20
_PURPOSE = "etf_role_synthesis"
_SCOPE = "etf"


def render_etf_workup(
    conn: sqlite3.Connection,
    repo_root: Path,
    db_path: Path,
    ticker: str,
    *,
    page: int = 0,
) -> str | None:
    """Saved ETF research history; None when the ticker isn't a tracked ETF."""
    del repo_root  # retained in the route signature; this reader uses the DB only
    t = ticker.strip().upper()
    if not _is_tracked_etf(conn, t):
        return None
    if page < 0:
        page = 0
    try:
        versions = historical_versions(
            conn,
            ticker=t,
            purpose=_PURPOSE,
            fiscal_period=None,
            scope=_SCOPE,
            verify_readability=True,
        )
    except (sqlite3.Error, ValueError, TypeError):
        rows = "<p class='etfw-miss' role='status'>Saved ETF research is unavailable.</p>"
        has_next = False
    else:
        versions.reverse()
        offset = page * _PAGE_SIZE
        has_next = len(versions) > offset + _PAGE_SIZE
        rows = "".join(_render_saved_version(v) for v in versions[offset : offset + _PAGE_SIZE])
        if not rows:
            rows = "<p class='etfw-miss' role='status'>No saved versions on this page.</p>"
    nav = _pagination(t, page, has_next)
    return (
        "<div class='etfw'>"
        "<h4>Saved ETF research</h4>"
        "<p class='k-card-meta'>Historical saved versions; no active workup or "
        "current sizing advice is generated here.</p>"
        f"{rows}{nav}</div>{ANALYSIS_STYLE}"
    )


def _is_tracked_etf(conn: sqlite3.Connection, ticker: str) -> bool:
    try:
        row = conn.execute(
            "SELECT instrument_type FROM tracked_companies WHERE UPPER(ticker) = ? LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0] or "").lower() == "etf"


def _render_saved_version(version: HistoricalArtifactVersion) -> str:
    artifact = version.artifact
    artifact_id = escape(str(version.artifact_id))
    saved = escape(
        artifact.generated_at.isoformat(timespec="minutes")
        if artifact is not None
        else version.generated_at_raw or "date unavailable"
    )
    state = (
        "superseded" if artifact is not None and artifact.superseded_by_id is not None else "saved"
    )
    header = f"<details><summary>Version #{artifact_id} · {saved} · {state}</summary>"
    synthesis = _decode_synthesis(artifact) if artifact is not None else None
    if synthesis is None:
        body = (
            "<p class='etfw-miss'>Unavailable — this saved version has no readable "
            "role synthesis content.</p>"
        )
    else:
        body = render_prose(synthesis.role_summary)
        body += (
            f"<p class='etfw-src'>Saved verdict: {escape(synthesis.verdict.replace('_', ' '))}</p>"
        )
        if synthesis.suggested_weight_band:
            body += (
                "<p class='etfw-src'>Historical proposed weight: "
                f"{escape(synthesis.suggested_weight_band)}</p>"
            )
        if synthesis.what_it_adds:
            items = "".join(f"<li>{escape(item)}</li>" for item in synthesis.what_it_adds)
            body += f"<ul>{items}</ul>"
        if synthesis.overlap_caution:
            body += f"<p class='etfw-miss'>&#9888; {escape(synthesis.overlap_caution)}</p>"
        if synthesis.watch_items:
            body += (
                "<div class='etfw-src'>watch: "
                + escape("; ".join(synthesis.watch_items))
                + "</div>"
            )
    return f"{header}{body}</details>"


def _decode_synthesis(artifact: Artifact) -> EtfRoleSynthesis | None:
    if artifact.content_json is None:
        return None
    try:
        return EtfRoleSynthesis.model_validate(artifact.content_json)
    except (TypeError, ValueError):
        return None


def _pagination(ticker: str, page: int, has_next: bool) -> str:
    links: list[str] = []
    if page > 0:
        url = f"/api/peek/etf_workup?ticker={escape(ticker, quote=True)}&amp;page={page - 1}"
        links.append(
            f"<a href='{url}' data-peek-url='{url}' data-peek-title='Saved ETF research'>"
            "&larr; previous saved versions</a>"
        )
    if has_next:
        url = f"/api/peek/etf_workup?ticker={escape(ticker, quote=True)}&amp;page={page + 1}"
        links.append(
            f"<a href='{url}' data-peek-url='{url}' data-peek-title='Saved ETF research'>"
            "more saved versions &rarr;</a>"
        )
    if not links:
        return ""
    return "<div class='cc-peek-foot'>" + " · ".join(links) + "</div>"
