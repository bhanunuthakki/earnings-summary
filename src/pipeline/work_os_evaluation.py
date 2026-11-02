"""Typed, read-only Evaluation/Candidates projection for the Work OS.

The evaluation universe is already assembled by :func:`build_cockpit_rows`.
This module only projects those rows into the shell contract and adds bounded
thesis and artifact doorways.  It never opens a second database connection,
fetches the network, or invents missing research values.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from allocation.candidate_fit import CandidateFit
from candidate_fit_cache import read_materialized_candidate_fit
from dcf.availability import resolve_dcf_route_artifact
from pipeline.cockpit_file_inputs import (
    HoldingsReadError,
    cockpit_input_is_current,
    project_micro_thesis_excerpt,
    read_holdings_payload,
    read_materialized_cockpit_file_inputs,
)
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_briefs import build_brief_library
from research.investment_profile import (
    CompanyProfileProjection,
    ValuationEvidence,
    project_company_profile,
)
from ticker_validation import safe_ticker

EvaluationInstrument = Literal["company", "etf"]
ThesisSource = Literal["micro_thesis", "position_entry", "unavailable"]

_EXCERPT_LIMIT = 320
_NAME_LIMIT = 160
_EXCERPT_SUFFIX = "…"
_MACHINE_REFERENCE_RE = re.compile(r"\b(?:sha256:)?[a-f0-9]{40,}\b", re.IGNORECASE)
_POSITION_ENTRY_QUERIES: dict[tuple[str, ...], str] = {
    (): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "LIMIT 1"
    ),
    ("id",): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY id DESC LIMIT 1"
    ),
    ("created_at",): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY created_at DESC LIMIT 1"
    ),
    ("created_at", "id"): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ),
    ("updated_at",): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY updated_at DESC LIMIT 1"
    ),
    ("updated_at", "id"): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY updated_at DESC, id DESC LIMIT 1"
    ),
    ("updated_at", "created_at"): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY updated_at DESC, created_at DESC LIMIT 1"
    ),
    ("updated_at", "created_at", "id"): (
        "SELECT entry_thesis_excerpt FROM position_entries "
        "WHERE UPPER(ticker) = ? AND TRIM(COALESCE(entry_thesis_excerpt, '')) != '' "
        "ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 1"
    ),
}


class WorkOsPortfolioIndicator(BaseModel):
    """One direct candidate-vs-book observation, never a composite score."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    detail: str
    effect: Literal["positive", "neutral", "negative", "unavailable"]
    missing: bool = False


class WorkOsEvaluationItem(BaseModel):
    """One evaluation-list item with only governed, user-facing doorways."""

    model_config = ConfigDict(frozen=True)

    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
    name: str = Field(max_length=_NAME_LIMIT)
    instrument_type: EvaluationInstrument
    sharpe_delta_bps: float | None = None
    held_weight_pct: float | None = None
    dcf_upside_pct: float | None = None
    revenue_growth_yoy_pct: float | None = None
    fcf_margin_pct: float | None = None
    profile: CompanyProfileProjection | None = None
    portfolio_indicators: list[WorkOsPortfolioIndicator] = Field(
        default_factory=list[WorkOsPortfolioIndicator]
    )
    portfolio_role_labels: list[str] = Field(default_factory=list[str])
    thesis_excerpt: str | None = Field(default=None, max_length=_EXCERPT_LIMIT)
    source: ThesisSource = "unavailable"
    company_desk_url: str | None = None
    dcf_url: str | None = None
    report_url: str | None = None

    @property
    def thesis_source(self) -> ThesisSource:
        """Descriptive alias for callers that do not use the compact API key."""

        return self.source


class WorkOsEvaluationHydration(BaseModel):
    """The versioned Evaluation/Candidates response consumed by the shell."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["evaluation_surface.v2"] = "evaluation_surface.v2"
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


def _factor_effect(
    multiplier: float, *, missing: bool
) -> Literal["positive", "neutral", "negative", "unavailable"]:
    if missing:
        return "unavailable"
    if multiplier >= 1.05:
        return "positive"
    if multiplier <= 0.95:
        return "negative"
    return "neutral"


def _portfolio_projection(
    fit: CandidateFit | None,
    *,
    sharpe_delta_bps: float | None,
) -> tuple[list[WorkOsPortfolioIndicator], list[str]]:
    if fit is None:
        return [], []
    indicators = [
        WorkOsPortfolioIndicator(
            key=factor.key,
            label=factor.label,
            detail=factor.detail,
            effect=_factor_effect(factor.multiplier, missing=factor.missing),
            missing=factor.missing,
        )
        for factor in fit.factors
        if factor.key in {"sharpe", "divers", "factor", "sector", "overlap"}
    ]
    by_key = {factor.key: factor for factor in fit.factors}
    labels: list[str] = []
    divers = by_key.get("divers")
    if divers is not None and not divers.missing:
        if divers.multiplier >= 1.05:
            labels.append("Diversifier")
        elif divers.multiplier <= 0.95:
            labels.append("Correlation risk")
    factor = by_key.get("factor")
    if factor is not None and not factor.missing:
        if factor.multiplier >= 1.05:
            labels.append("Balances factor tilt")
        elif factor.multiplier <= 0.95:
            labels.append("Deepens factor tilt")
    sector = by_key.get("sector")
    if sector is not None and not sector.missing:
        if sector.multiplier >= 1.05:
            labels.append("Adds sector breadth")
        elif sector.multiplier <= 0.95:
            labels.append("Sector crowding")
    if sharpe_delta_bps is not None:
        if sharpe_delta_bps > 0:
            labels.append("Risk-adjusted accretive")
        elif sharpe_delta_bps < 0:
            labels.append("Risk-adjusted dilutive")
    return indicators, labels


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


def _bounded_excerpt(value: str) -> str:
    return _bounded_human_text(value, limit=_EXCERPT_LIMIT)


def _micro_thesis(raw: object | None, read_error: HoldingsReadError) -> tuple[str | None, bool]:
    if read_error == "missing":
        return None, False
    if read_error != "none":
        return None, True
    if not isinstance(raw, dict):
        return None, True
    data = cast("dict[str, object]", raw)
    cached_excerpt = data.get("thesis_excerpt")
    if isinstance(cached_excerpt, str) and cached_excerpt.strip():
        return cached_excerpt, False
    return project_micro_thesis_excerpt(data), False


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

    order_key = tuple(column for column in ("updated_at", "created_at", "id") if column in columns)
    query = _POSITION_ENTRY_QUERIES[order_key]
    if "superseded_by_entry_id" in columns:
        query = query.replace(
            "WHERE UPPER(ticker)", "WHERE superseded_by_entry_id IS NULL AND UPPER(ticker)"
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
    conn: sqlite3.Connection,
    ticker: str,
    warnings: set[str],
    holdings_payload: object | None,
    holdings_error: HoldingsReadError,
) -> tuple[str | None, ThesisSource]:
    excerpt, malformed = _micro_thesis(holdings_payload, holdings_error)
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
            artifact_kind="full_brief",
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
    """Project evaluation cockpit rows into ``evaluation_surface.v2``.

    ``rows`` is intentionally not sorted here: ``build_cockpit_rows`` owns the
    evaluation ordering, and preserving it makes the API deterministic and
    keeps one source of truth for ranking.
    """

    warnings: set[str] = set()
    report_urls = _report_urls(repo_root, conn, warnings)
    try:
        fit_cache = read_materialized_candidate_fit(repo_root)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        fit_cache = {}
        warnings.add("candidate_fit_unavailable")

    file_inputs = read_materialized_cockpit_file_inputs(repo_root)
    items: list[WorkOsEvaluationItem] = []
    for row in rows:
        try:
            ticker = safe_ticker(row.base.ticker)
        except ValueError:
            warnings.add("invalid_ticker_omitted")
            continue
        ticker_inputs = file_inputs.get(ticker)
        if (
            ticker_inputs is not None
            and ticker_inputs.holdings_readable
            and cockpit_input_is_current(repo_root, ticker, ticker_inputs, "holdings")
        ):
            holdings_payload = ticker_inputs.holdings_payload
            holdings_error = ticker_inputs.holdings_error
        else:
            holdings_payload, holdings_error = read_holdings_payload(repo_root, ticker)
        thesis_excerpt, source = _thesis_excerpt(
            conn,
            ticker,
            warnings,
            holdings_payload,
            holdings_error,
        )
        instrument_type: EvaluationInstrument = "etf" if row.is_etf else "company"
        dcf_upside_pct = _dcf_upside_pct(row) if instrument_type == "company" else None
        structured_fit = fit_cache.get(ticker)
        sharpe_delta_bps = _finite(row.sharpe_delta_bps)
        held_weight = _finite(row.held_weight)
        profile: CompanyProfileProjection | None = None
        portfolio_indicators: list[WorkOsPortfolioIndicator] = []
        portfolio_role_labels: list[str] = []
        if instrument_type == "company":
            portfolio_indicators, portfolio_role_labels = _portfolio_projection(
                structured_fit,
                sharpe_delta_bps=sharpe_delta_bps,
            )
            profile = project_company_profile(
                conn,
                ticker=ticker,
                valuation=ValuationEvidence(
                    revenue_growth_yoy_pct=_finite(row.rev_yoy_pct),
                    fcf_margin_pct=_finite(row.fcf_margin_pct),
                    dcf_upside_pct=dcf_upside_pct,
                ),
            )
        else:
            sharpe_delta_bps = None
        held_weight_pct = held_weight * 100.0 if held_weight is not None else None
        if held_weight_pct is not None and not math.isfinite(held_weight_pct):
            held_weight_pct = None

        dcf_url = None
        if instrument_type == "company":
            try:
                if holdings_error == "unicode":
                    # The original path re-read the holdings file here and the
                    # UnicodeDecodeError aborted resolution before the workbook
                    # fallback; keep that exact outcome with the preloaded payload.
                    warnings.add("dcf_route_unavailable")
                elif (
                    resolve_dcf_route_artifact(
                        repo_root,
                        ticker,
                        holdings_payload=holdings_payload,
                    )
                    is not None
                ):
                    dcf_url = f"/dcf/{ticker}"
            except (OSError, UnicodeDecodeError, ValueError, TypeError):
                warnings.add("dcf_route_unavailable")

        items.append(
            WorkOsEvaluationItem(
                ticker=ticker,
                name=_bounded_human_text((row.name or ticker).strip() or ticker, limit=_NAME_LIMIT),
                instrument_type=instrument_type,
                sharpe_delta_bps=sharpe_delta_bps,
                held_weight_pct=held_weight_pct,
                dcf_upside_pct=dcf_upside_pct,
                revenue_growth_yoy_pct=(
                    _finite(row.rev_yoy_pct) if instrument_type == "company" else None
                ),
                fcf_margin_pct=(
                    _finite(row.fcf_margin_pct) if instrument_type == "company" else None
                ),
                profile=profile,
                portfolio_indicators=portfolio_indicators,
                portfolio_role_labels=portfolio_role_labels,
                thesis_excerpt=thesis_excerpt,
                source=source,
                company_desk_url=(f"/ticker/{ticker}" if instrument_type == "company" else None),
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
