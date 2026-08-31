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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from allocation.candidate_fit import CandidateFit
from candidate_fit_cache import read_materialized_candidate_fit
from dcf.availability import resolve_dcf_route_artifact
from etf_score_cache import read_materialized_etf_loadings, read_materialized_etf_whatif
from instrument_store import get_etf_profile
from pipeline.research_cockpit import CockpitRow
from pipeline.work_os_briefs import build_brief_library
from research.investment_profile import (
    CompanyProfileProjection,
    EtfProfileInputs,
    EtfProfileProjection,
    EtfStyleEvidence,
    ValuationEvidence,
    project_company_profile,
    project_etf_profile,
)
from ticker_validation import safe_ticker

EvaluationInstrument = Literal["company", "etf"]
ThesisSource = Literal["micro_thesis", "position_entry", "unavailable"]

_EXCERPT_LIMIT = 320
_EXPLANATION_LIMIT = 320
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
    # Frontend-removed compatibility seam. Delete these six scalar fields after
    # 2026-09-28 unless an evidence-backed design explicitly restores them.
    score: float | None = None
    score_why: str | None = Field(default=None, max_length=_EXPLANATION_LIMIT)
    score_partial: bool = False
    fit: float | None = None
    fit_why: str | None = Field(default=None, max_length=_EXPLANATION_LIMIT)
    fit_partial: bool = False
    sharpe_delta_bps: float | None = None
    held_weight_pct: float | None = None
    dcf_upside_pct: float | None = None
    revenue_growth_yoy_pct: float | None = None
    fcf_margin_pct: float | None = None
    profile: CompanyProfileProjection | EtfProfileProjection | None = None
    portfolio_indicators: list[WorkOsPortfolioIndicator] = Field(
        default_factory=list[WorkOsPortfolioIndicator]
    )
    portfolio_role_labels: list[str] = Field(default_factory=list[str])
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


def _finite_object(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _finite(float(value))


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


def _etf_profile_inputs(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    fit: CandidateFit | None,
    sharpe_delta_bps: float | None,
    loadings_cache: Mapping[str, Sequence[object]],
    whatif_cache: Mapping[str, Mapping[str, dict[str, object]]],
    warnings: set[str],
) -> EtfProfileInputs:
    try:
        fund_profile = get_etf_profile(conn, ticker)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        fund_profile = None
        warnings.add("etf_profile_unavailable")

    style_rows = loadings_cache.get(ticker, [])
    style_evidence: list[EtfStyleEvidence] = []
    for row in style_rows:
        key = getattr(row, "key", None)
        beta = _finite_object(getattr(row, "beta", None))
        r_squared = _finite_object(getattr(row, "r_squared", None))
        if key not in {"value", "size", "momentum"} or beta is None or r_squared is None:
            continue
        try:
            style_evidence.append(EtfStyleEvidence(key=key, beta=beta, r_squared=r_squared))
        except ValueError:
            continue

    factors = {factor.key: factor for factor in fit.factors} if fit is not None else {}
    divers = factors.get("divers")
    overlap = factors.get("overlap")
    book_available = fit is not None and any(not factor.missing for factor in fit.factors)
    divers_multiplier = (
        _finite(divers.multiplier) if divers is not None and not divers.missing else None
    )
    overlap_multiplier = (
        _finite(overlap.multiplier) if overlap is not None and not overlap.missing else None
    )

    whatif_row = whatif_cache.get(ticker, {}).get("0.03")
    whatif_available = False
    vol_before_ann: float | None = None
    vol_after_ann: float | None = None
    whatif_sharpe = sharpe_delta_bps
    if isinstance(whatif_row, dict):
        degraded = whatif_row.get("degraded")
        whatif_available = not isinstance(degraded, list) or not degraded
        vol_before_ann = _finite_object(whatif_row.get("vol_before_ann"))
        vol_after_ann = _finite_object(whatif_row.get("vol_after_ann"))
        cached_delta = _finite_object(whatif_row.get("sharpe_delta_bps"))
        if cached_delta is not None:
            whatif_sharpe = cached_delta

    return EtfProfileInputs(
        profile_available=fund_profile is not None,
        asset_class=fund_profile.asset_class if fund_profile is not None else None,
        benchmark_index=fund_profile.benchmark_index if fund_profile is not None else None,
        sector_label=fund_profile.sector_label if fund_profile is not None else None,
        expense_ratio=_finite(fund_profile.expense_ratio) if fund_profile is not None else None,
        distribution_yield=(
            _finite(fund_profile.distribution_yield) if fund_profile is not None else None
        ),
        style_evidence_available=ticker in loadings_cache,
        style_loadings=style_evidence,
        book_evidence_available=book_available,
        diversification_multiplier=divers_multiplier,
        overlap_multiplier=overlap_multiplier,
        sharpe_delta_bps=whatif_sharpe,
        whatif_evidence_available=whatif_available,
        vol_before_ann=vol_before_ann,
        vol_after_ann=vol_after_ann,
    )


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

    order_key = tuple(column for column in ("updated_at", "created_at", "id") if column in columns)
    query = _POSITION_ENTRY_QUERIES[order_key]
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
    try:
        etf_loadings_cache = read_materialized_etf_loadings(repo_root)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        etf_loadings_cache = {}
        warnings.add("etf_style_loadings_unavailable")
    try:
        etf_whatif_cache = read_materialized_etf_whatif(repo_root)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        etf_whatif_cache = {}
        warnings.add("etf_whatif_unavailable")
    items: list[WorkOsEvaluationItem] = []
    for row in rows:
        try:
            ticker = safe_ticker(row.base.ticker)
        except ValueError:
            warnings.add("invalid_ticker_omitted")
            continue
        thesis_excerpt, source = _thesis_excerpt(repo_root, conn, ticker, warnings)
        instrument_type: EvaluationInstrument = "etf" if row.is_etf else "company"
        dcf_upside_pct = _dcf_upside_pct(row)
        structured_fit = fit_cache.get(ticker)
        sharpe_delta_bps = _finite(row.sharpe_delta_bps)
        portfolio_indicators, portfolio_role_labels = _portfolio_projection(
            structured_fit,
            sharpe_delta_bps=sharpe_delta_bps,
        )
        if instrument_type == "company":
            profile: CompanyProfileProjection | EtfProfileProjection | None = (
                project_company_profile(
                    conn,
                    ticker=ticker,
                    valuation=ValuationEvidence(
                        revenue_growth_yoy_pct=_finite(row.rev_yoy_pct),
                        fcf_margin_pct=_finite(row.fcf_margin_pct),
                        dcf_upside_pct=dcf_upside_pct,
                    ),
                )
            )
        else:
            profile = project_etf_profile(
                conn,
                ticker=ticker,
                inputs=_etf_profile_inputs(
                    conn,
                    ticker=ticker,
                    fit=structured_fit,
                    sharpe_delta_bps=sharpe_delta_bps,
                    loadings_cache=etf_loadings_cache,
                    whatif_cache=etf_whatif_cache,
                    warnings=warnings,
                ),
            )
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
                sharpe_delta_bps=sharpe_delta_bps,
                held_weight_pct=held_weight_pct,
                dcf_upside_pct=dcf_upside_pct,
                revenue_growth_yoy_pct=_finite(row.rev_yoy_pct),
                fcf_margin_pct=_finite(row.fcf_margin_pct),
                profile=profile,
                portfolio_indicators=portfolio_indicators,
                portfolio_role_labels=portfolio_role_labels,
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
