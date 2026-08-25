"""Typed, read-only Evaluation/Candidates projection for the Work OS.

The evaluation universe is already assembled by :func:`build_cockpit_rows`.
This module only projects those rows into the shell contract and adds bounded
thesis and artifact doorways.  It never opens a second database connection,
fetches the network, or invents missing research values.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from dcf.availability import resolve_dcf_route_artifact
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_briefs import build_brief_library

EvaluationInstrument = Literal["company", "etf"]
ThesisSource = Literal["micro_thesis", "position_entry", "unavailable"]

_EXCERPT_LIMIT = 320
_EXPLANATION_LIMIT = 320
_NAME_LIMIT = 160
_EXCERPT_SUFFIX = "…"
_MACHINE_REFERENCE_RE = re.compile(r"\b(?:sha256:)?[a-f0-9]{40,}\b", re.IGNORECASE)


class WorkOsEvaluationItem(BaseModel):
    """One evaluation-list item with only governed, user-facing doorways."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    name: str = Field(max_length=_NAME_LIMIT)
    instrument_type: EvaluationInstrument
    score: float | None = None
    score_why: str | None = Field(default=None, max_length=_EXPLANATION_LIMIT)
    score_partial: bool = False
    fit: float | None = None
    fit_why: str | None = Field(default=None, max_length=_EXPLANATION_LIMIT)
    fit_partial: bool = False
    sharpe_delta_bps: float | None = None
    held_weight_pct: float | None = None
    dcf_upside_pct: float | None = None
    thesis_excerpt: str | None = Field(default=None, max_length=_EXCERPT_LIMIT)
    source: ThesisSource = "unavailable"
    company_desk_url: str | None = None
    workup_url: str | None = None
    dcf_url: str | None = None
    report_url: str | None = None

    @property
    def thesis_source(self) -> ThesisSource:
        """Descriptive alias for callers that do not use the compact API key."""

        return self.source

    @property
    def why(self) -> str | None:
        """Compatibility alias for the score explanation."""

        return self.score_why

    @property
    def partial(self) -> bool:
        """Compatibility alias for the score's partial-data marker."""

        return self.score_partial


class WorkOsEvaluationHydration(BaseModel):
    """The versioned Evaluation/Candidates response consumed by the shell."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["evaluation_surface.v1"] = "evaluation_surface.v1"
    generated_at: str
    count: int
    items: list[WorkOsEvaluationItem]
    warnings: list[str] = Field(default_factory=list)


def _finite(value: float | None) -> float | None:
    """Return finite numeric data only; JSON must never contain NaN or infinity."""

    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _dcf_upside_pct(row: CockpitRow) -> float | None:
    """Compute guarded fair-value upside from the DCF run's own price basis."""

    if row.is_etf or row.dcf_unreviewed:
        return None
    fair_value = _finite(row.fair_value)
    dcf_price = _finite(row.dcf_price)
    if fair_value is None or dcf_price is None or fair_value <= 0 or dcf_price <= 0:
        return None
    upside = (fair_value / dcf_price - 1.0) * 100.0
    return _finite(upside)


def _bounded_human_text(value: str, *, limit: int) -> str:
    """Remove machine references and bound human copy without splitting a word."""

    normalized = " ".join(value.split())
    normalized = _MACHINE_REFERENCE_RE.sub("source reference", normalized)
    if len(normalized) <= limit:
        return normalized

    budget = limit - len(_EXCERPT_SUFFIX)
    candidate = normalized[:budget]
    boundary = candidate.rsplit(" ", 1)[0]
    if not boundary:
        # A single unbroken token cannot have a word boundary; retain the hard
        # upper bound rather than leaking an arbitrarily long payload.
        boundary = candidate
    return boundary.rstrip() + _EXCERPT_SUFFIX


def _bounded_optional(value: str | None, *, limit: int) -> str | None:
    if value is None or not value.strip():
        return None
    return _bounded_human_text(value, limit=limit)


def _bounded_excerpt(value: str) -> str:
    return _bounded_human_text(value, limit=_EXCERPT_LIMIT)


def _micro_thesis(repo_root: Path, ticker: str) -> tuple[str | None, bool]:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.is_file():
        return None, False
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(raw, dict):
        return None, True
    thesis = cast("dict[str, object]", raw).get("thesis")
    if isinstance(thesis, str) and thesis.strip():
        return _bounded_excerpt(thesis), False
    return None, False


def _position_entry_excerpt(
    conn: sqlite3.Connection,
    ticker: str,
) -> tuple[str | None, bool]:
    """Read the latest nonblank position-entry excerpt without opening a DB."""

    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(position_entries)")}
    except sqlite3.Error:
        return None, True
    required = {"ticker", "entry_thesis_excerpt"}
    if not required <= columns:
        return None, True

    order_columns = [
        f"{column} DESC" for column in ("updated_at", "created_at", "id") if column in columns
    ]
    order_sql = f" ORDER BY {', '.join(order_columns)}" if order_columns else ""
    query = (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != ''"
        f"{order_sql} LIMIT 1"
    )
    try:
        row = conn.execute(query, (ticker,)).fetchone()
    except sqlite3.Error:
        return None, True
    if row is None:
        return None, False
    value = row[0]
    if not isinstance(value, str) or not value.strip():
        return None, False
    return _bounded_excerpt(value), False


def _thesis_excerpt(
    repo_root: Path,
    conn: sqlite3.Connection,
    ticker: str,
    warnings: set[str],
) -> tuple[str | None, ThesisSource]:
    excerpt, malformed = _micro_thesis(repo_root, ticker)
    if malformed:
        warnings.add("micro_thesis_unavailable")
    if excerpt is not None:
        return excerpt, "micro_thesis"

    excerpt, unavailable = _position_entry_excerpt(conn, ticker)
    if unavailable:
        warnings.add("position_entries_unavailable")
    if excerpt is not None:
        return excerpt, "position_entry"
    return None, "unavailable"


def _report_urls(
    repo_root: Path,
    conn: sqlite3.Connection,
    warnings: set[str],
) -> dict[str, str]:
    """Return artifact-verified evaluation report routes keyed by ticker."""

    try:
        response = build_brief_library(
            repo_root,
            conn=conn,
            coverage_role="evaluation",
            limit=10_000,
        )
    except (OSError, UnicodeDecodeError, ValueError, sqlite3.Error):
        warnings.add("evaluation_briefs_unavailable")
        return {}
    return {
        item.ticker.strip().upper(): item.standalone_url
        for item in response.items
        if item.coverage_role == "evaluation" and item.standalone_url
    }


def _generated_at(value: datetime | None) -> str:
    stamp = (value or datetime.now(UTC)).astimezone(UTC)
    return stamp.isoformat().replace("+00:00", "Z")


def build_work_os_evaluation(
    rows: Sequence[CockpitRow],
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    generated_at: datetime | None = None,
) -> WorkOsEvaluationHydration:
    """Project evaluation cockpit rows into ``evaluation_surface.v1``.

    ``rows`` is intentionally not sorted here: ``build_cockpit_rows`` owns the
    evaluation ordering, and preserving it makes the API deterministic and
    keeps one source of truth for ranking.
    """

    warnings: set[str] = set()
    report_urls = _report_urls(repo_root, conn, warnings)
    items: list[WorkOsEvaluationItem] = []
    for row in rows:
        ticker = row.base.ticker.strip().upper()
        thesis_excerpt, source = _thesis_excerpt(repo_root, conn, ticker, warnings)
        instrument_type: EvaluationInstrument = "etf" if row.is_etf else "company"
        held_weight = _finite(row.held_weight)
        held_weight_pct = held_weight * 100.0 if held_weight is not None else None
        if held_weight_pct is not None and not math.isfinite(held_weight_pct):
            held_weight_pct = None

        dcf_url = None
        if instrument_type == "company":
            try:
                if resolve_dcf_route_artifact(repo_root, ticker) is not None:
                    dcf_url = f"/dcf/{ticker}"
            except (OSError, UnicodeDecodeError, ValueError, TypeError):
                warnings.add("dcf_route_unavailable")

        items.append(
            WorkOsEvaluationItem(
                ticker=ticker,
                name=_bounded_human_text((row.name or ticker).strip() or ticker, limit=_NAME_LIMIT),
                instrument_type=instrument_type,
                score=_finite(row.attractiveness),
                score_why=_bounded_optional(
                    row.attractiveness_why,
                    limit=_EXPLANATION_LIMIT,
                ),
                score_partial=row.attractiveness_partial,
                fit=_finite(row.fit),
                fit_why=_bounded_optional(row.fit_why, limit=_EXPLANATION_LIMIT),
                fit_partial=row.fit_partial,
                sharpe_delta_bps=_finite(row.sharpe_delta_bps),
                held_weight_pct=held_weight_pct,
                dcf_upside_pct=_dcf_upside_pct(row),
                thesis_excerpt=thesis_excerpt,
                source=source,
                company_desk_url=(f"/ticker/{ticker}" if instrument_type == "company" else None),
                workup_url=(
                    f"/api/peek/etf_workup?ticker={ticker}" if instrument_type == "etf" else None
                ),
                dcf_url=dcf_url,
                report_url=report_urls.get(ticker),
            )
        )

    return WorkOsEvaluationHydration(
        generated_at=_generated_at(generated_at),
        count=len(items),
        items=items,
        warnings=sorted(warnings),
    )


__all__ = [
    "EvaluationInstrument",
    "ThesisSource",
    "WorkOsEvaluationHydration",
    "WorkOsEvaluationItem",
    "build_work_os_evaluation",
]
