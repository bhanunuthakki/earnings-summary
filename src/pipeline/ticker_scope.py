"""Shared tracked-company capability resolution for daily pipeline entrypoints."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from models.companies import ListType
from pipeline.sec_xbrl import NO_SEC_FILERS
from pipeline.source_policy import ArtifactKind, CollectionSource, instrument_allows_artifact

_ACTIVE_LIST_TYPES: frozenset[ListType] = frozenset(
    {ListType.PORTFOLIO, ListType.EVALUATION, ListType.WATCHLIST}
)

_ROLE_PRIORITY: dict[ListType, int] = {
    ListType.PORTFOLIO: 0,
    ListType.EVALUATION: 1,
    ListType.WATCHLIST: 2,
    ListType.INDEX_MEMBER: 3,
    ListType.ETF: 4,
    ListType.NONE: 5,
}


@dataclass(frozen=True, slots=True)
class TickerCapability:
    """Resolved active roster row and source/pipeline eligibility flags."""

    ticker: str
    list_type: ListType
    instrument_type: str | None
    filing_regime: str | None
    fiscal_year_end: str | None
    has_instrument_column: bool = True

    @property
    def is_portfolio_holding(self) -> bool:
        return self.list_type is ListType.PORTFOLIO

    @property
    def is_corporate_instrument(self) -> bool:
        if not self.has_instrument_column:
            return True
        return instrument_allows_artifact(
            self.instrument_type,
            source=CollectionSource.IR,
            artifact_kind=ArtifactKind.IR_DOCUMENT,
        )

    @property
    def supports_sec_xbrl(self) -> bool:
        return self.is_corporate_instrument and self.ticker not in NO_SEC_FILERS

    @property
    def supports_earnings_calendar(self) -> bool:
        return self.is_corporate_instrument

    @property
    def supports_earnings_surprises(self) -> bool:
        return self.is_corporate_instrument

    @property
    def supports_ir_documents(self) -> bool:
        return self.is_corporate_instrument

    @property
    def supports_corporate_grades(self) -> bool:
        return self.is_corporate_instrument


def _clean_optional_text(value: object, *, lowercase: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lowercase else text


def load_active_capabilities(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    list_types: frozenset[ListType] | None = None,
) -> list[TickerCapability]:
    """Load non-archived tracked companies with derived capability flags."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")}
    if not {"ticker", "list_type"}.issubset(columns):
        return []

    has_instrument = "instrument_type" in columns
    has_regime = "filing_regime" in columns
    has_fye = "fiscal_year_end" in columns
    has_archived = "archived_at" in columns

    select_cols = [
        "ticker",
        "list_type",
        "instrument_type" if has_instrument else "NULL AS instrument_type",
        "filing_regime" if has_regime else "NULL AS filing_regime",
        "fiscal_year_end" if has_fye else "NULL AS fiscal_year_end",
    ]
    where_clauses: list[str] = []
    params: list[str] = []
    if has_archived:
        where_clauses.append("archived_at IS NULL")
    if ticker is not None:
        where_clauses.append("UPPER(ticker) = ?")
        params.append(ticker.strip().upper())
    else:
        allowed_roles = _ACTIVE_LIST_TYPES if list_types is None else list_types
        if not allowed_roles:
            return []
        placeholders = ",".join("?" for _ in allowed_roles)
        where_clauses.append(f"list_type IN ({placeholders})")
        params.extend(sorted(role.value for role in allowed_roles))

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = (
        f"SELECT {', '.join(select_cols)} FROM tracked_companies {where_sql} "  # nosec B608
        "ORDER BY ticker"
    )
    rows = conn.execute(sql, params).fetchall()
    capabilities: list[TickerCapability] = []
    for row in rows:
        raw_ticker = str(row[0]).strip().upper()
        if not raw_ticker:
            continue
        try:
            role = ListType(str(row[1]).strip())
        except ValueError:
            continue
        capabilities.append(
            TickerCapability(
                ticker=raw_ticker,
                list_type=role,
                instrument_type=_clean_optional_text(row[2], lowercase=True),
                filing_regime=_clean_optional_text(row[3]),
                fiscal_year_end=_clean_optional_text(row[4]),
                has_instrument_column=has_instrument,
            )
        )
    capabilities.sort(key=lambda cap: (_ROLE_PRIORITY.get(cap.list_type, 99), cap.ticker))
    return capabilities


def partition_by_capability(
    caps: Sequence[TickerCapability],
    *,
    predicate: Callable[[TickerCapability], bool],
    skip_reason: str,
) -> tuple[list[TickerCapability], list[dict[str, str]]]:
    """Partition capabilities into eligible items and structured skip records."""
    eligible: list[TickerCapability] = []
    skipped: list[dict[str, str]] = []
    for cap in caps:
        if predicate(cap):
            eligible.append(cap)
        else:
            skipped.append(
                {
                    "ticker": cap.ticker,
                    "list_type": cap.list_type.value,
                    "instrument_type": cap.instrument_type or "unknown",
                    "reason": skip_reason,
                }
            )
    return eligible, skipped
