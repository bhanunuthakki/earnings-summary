"""Bounded Work OS projection for persisted post-earnings readouts."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from provenance.selection import selected_transcripts_relation

_PURPOSE = "post_earnings_readout"
_TICKER_RE = re.compile(r"^[A-Z0-9.-]{1,16}$")
_QUARTER_TYPES = {"Q1", "Q2", "Q3", "Q4"}
_PROJECTION_WARNING = "earnings_readout_projection_unavailable"
log = logging.getLogger(__name__)


class EarningsReadoutSummary(BaseModel):
    """One durable, directly readable quarter artifact."""

    model_config = ConfigDict(frozen=True)

    artifact_id: int
    ticker: str = Field(pattern=r"^[A-Z0-9.-]{1,16}$")
    fiscal_period: str
    period_label: str
    generated_at: str | None
    route: str
    coverage_role: Literal["portfolio", "evaluation"] | None = None


class EarningsReadoutProjection(BaseModel):
    """Typed availability result so storage failure cannot look like a cache miss."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    readouts: dict[str, EarningsReadoutSummary]
    warnings: list[str]


def _period_label(fiscal_period_type: object, period_end: date) -> str:
    quarter = str(fiscal_period_type or "").strip().upper()
    if quarter in _QUARTER_TYPES:
        return f"{quarter} · {period_end.strftime('%b %Y')}"
    return f"Quarter ended {period_end.strftime('%b')} {period_end.day}, {period_end.year}"


def load_latest_earnings_readouts(
    conn: sqlite3.Connection,
    tickers: Sequence[str],
    *,
    coverage_roles: Mapping[str, str] | None = None,
) -> EarningsReadoutProjection:
    """Return the latest current, non-empty readout for each requested ticker.

    Artifact availability is independent of the short pre/post event window: a
    persisted readout remains useful and reachable after that event doorway has
    expired. Missing or legacy storage fails closed to an empty projection.
    """

    requested = {
        normalized
        for raw in tickers
        if (normalized := str(raw or "").strip().upper()) and _TICKER_RE.fullmatch(normalized)
    }
    if not requested:
        return EarningsReadoutProjection(status="ok", readouts={}, warnings=[])
    try:
        relation = selected_transcripts_relation(conn)
        placeholders = ", ".join("?" for _ in requested)
        rows = conn.execute(
            f"""
            WITH ranked AS (
                SELECT a.id, UPPER(a.ticker) AS ticker, a.fiscal_period, a.generated_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY UPPER(a.ticker)
                           ORDER BY date(a.fiscal_period) DESC, a.id DESC
                       ) AS artifact_rank
                FROM llm_artifacts AS a
                WHERE a.scope = 'ticker'
                  AND a.purpose = ?
                  AND a.superseded_by_id IS NULL
                  AND TRIM(COALESCE(a.content_md, '')) != ''
                  AND UPPER(COALESCE(a.ticker, '')) IN ({placeholders})
            )
            SELECT a.id, a.ticker, a.fiscal_period, a.generated_at,
                   (
                       SELECT t.fiscal_period_type
                       FROM {relation.sql} AS t
                       WHERE UPPER(t.ticker) = UPPER(a.ticker)
                         AND date(t.period_end) = date(a.fiscal_period)
                       ORDER BY t.id DESC
                       LIMIT 1
                   ) AS fiscal_period_type
            FROM ranked AS a
            WHERE a.artifact_rank = 1
            ORDER BY a.ticker
            """,  # nosec B608 -- relation and placeholders are internal constants; values remain bound
            (_PURPOSE, *sorted(requested)),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning(
            {
                "event": "work_os_earnings_projection_failed",
                "error_type": type(exc).__name__,
            }
        )
        return EarningsReadoutProjection(
            status="degraded",
            readouts={},
            warnings=[_PROJECTION_WARNING],
        )

    projected: dict[str, EarningsReadoutSummary] = {}
    normalized_roles: dict[str, Literal["portfolio", "evaluation"]] = {}
    for role_ticker, role in (coverage_roles or {}).items():
        if role == "portfolio":
            normalized_roles[str(role_ticker).strip().upper()] = "portfolio"
        elif role == "evaluation":
            normalized_roles[str(role_ticker).strip().upper()] = "evaluation"
    for row in rows:
        ticker = str(row["ticker"] or "").strip().upper()
        if ticker in projected or ticker not in requested:
            continue
        try:
            period_end = date.fromisoformat(str(row["fiscal_period"])[:10])
            artifact_id = int(row["id"])
        except (TypeError, ValueError):
            continue
        generated_at = str(row["generated_at"]) if row["generated_at"] is not None else None
        projected[ticker] = EarningsReadoutSummary(
            artifact_id=artifact_id,
            ticker=ticker,
            fiscal_period=period_end.isoformat(),
            period_label=_period_label(row["fiscal_period_type"], period_end),
            generated_at=generated_at,
            route=f"/api/peek/earnings-readout?ticker={ticker}&artifact_id={artifact_id}",
            coverage_role=normalized_roles.get(ticker),
        )
    return EarningsReadoutProjection(status="ok", readouts=projected, warnings=[])


__all__ = [
    "EarningsReadoutProjection",
    "EarningsReadoutSummary",
    "load_latest_earnings_readouts",
]
