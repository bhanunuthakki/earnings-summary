"""Seal exact retained statement caches without inferring entitlement or readiness."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.immutable_artifact import read_stable_artifact

STATEMENT_TYPES = {
    "fmp_income_statement": "income-statement",
    "fmp_balance_sheet": "balance-sheet-statement",
    "fmp_cashflow": "cash-flow-statement",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CanaryPeriod(_Frozen):
    row_index: int = Field(ge=0)
    period_end: date
    vendor_period: Literal["FY", "Q1", "Q2", "Q3", "Q4"]
    vendor_fiscal_year: int | None = Field(default=None, ge=1900, le=2200)
    vendor_calendar_year: int | None = Field(default=None, ge=1900, le=2200)
    reported_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    # Vendor labels are retained verbatim. Calendar year is never relabelled fiscal year.


class CanaryFileSeal(_Frozen):
    document_id: int = Field(gt=0)
    ticker: str
    document_type: str
    endpoint: str
    endpoint_identity_basis: Literal["document_type_mapping"] = "document_type_mapping"
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)
    fetched_at: datetime
    evidence_document_version_id: str | None
    source_observation_id: str | None
    observed_at: datetime | None
    retrieved_at: datetime | None
    response_representation: Literal["retained_provider_cache"] = "retained_provider_cache"
    cadence: Literal["annual", "quarterly"]
    periods: tuple[CanaryPeriod, ...] = Field(min_length=1)


class CanaryCorpusSeal(_Frozen):
    schema_version: Literal["canary-corpus-seal/v2"] = "canary-corpus-seal/v2"
    cutoff_at: datetime
    files: tuple[CanaryFileSeal, ...] = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_capture_lineage_complete: bool
    acquisition_provenance_complete: Literal[False] = False
    current_entitlement: Literal["unverified"] = "unverified"
    output_readiness: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def _seal(self) -> Self:
        if self.cutoff_at.tzinfo is None:
            raise ValueError("canary cutoff requires timezone")
        identities = [(item.ticker, item.document_type, item.cadence) for item in self.files]
        expected = {
            (ticker, kind, cadence)
            for ticker in ("WIX", "RBRK")
            for kind in STATEMENT_TYPES
            for cadence in ("annual", "quarterly")
        }
        if len(identities) != 12 or set(identities) != expected:
            raise ValueError("canary requires exact12 WIX/RBRK annual/quarterly statement cohorts")
        if self.corpus_sha256 != _digest(self.files):
            raise ValueError("canary corpus commitment mismatch")
        if self.immutable_capture_lineage_complete != all(
            item.source_observation_id and item.observed_at and item.retrieved_at
            for item in self.files
        ):
            raise ValueError("canary immutable capture lineage claim mismatch")
        return self


def _digest(files: tuple[CanaryFileSeal, ...]) -> str:
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in sorted(files, key=lambda item: item.document_id)],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _time(raw: object) -> datetime | None:
    if raw is None:
        return None
    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _year(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not str(raw).isdigit():
        raise ValueError("invalid vendor year")
    return int(str(raw))


def _periods(payload: bytes, ticker: str) -> tuple[CanaryPeriod, ...]:
    raw: object = json.loads(payload)
    if not isinstance(raw, list) or not raw:
        raise ValueError("statement cache must contain nonempty records")
    result: list[CanaryPeriod] = []
    for index, value in enumerate(cast(list[object], raw)):
        if not isinstance(value, dict):
            raise ValueError("statement record is not an object")
        row = cast(dict[str, object], value)
        if row.get("symbol") != ticker:
            raise ValueError("statement ticker mismatch")
        period = row.get("period")
        if period not in ("FY", "Q1", "Q2", "Q3", "Q4"):
            raise ValueError("statement fiscal period unavailable")
        result.append(
            CanaryPeriod.model_validate(
                {
                    "row_index": index,
                    "period_end": row.get("date"),
                    "vendor_period": period,
                    "vendor_fiscal_year": _year(row.get("fiscalYear")),
                    "vendor_calendar_year": _year(row.get("calendarYear")),
                    "reported_currency": row.get("reportedCurrency"),
                }
            )
        )
    if len({(r.period_end, r.vendor_period) for r in result}) != len(result):
        raise ValueError("duplicate statement period requires explicit amendment disposition")
    return tuple(result)


def seal_statement_corpus(
    conn: sqlite3.Connection, *, document_ids: tuple[int, ...], repo_root: Path, cutoff_at: datetime
) -> CanaryCorpusSeal:
    """Read exactly caller-selected DB identities and their existing raw files."""
    if len(document_ids) != 12 or len(set(document_ids)) != 12:
        raise ValueError("select exactly12 distinct statement documents")
    if cutoff_at.tzinfo is None:
        raise ValueError("cutoff requires timezone")
    files: list[CanaryFileSeal] = []
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for document_id in sorted(document_ids):
            row = conn.execute(
                "SELECT document.*,version.document_version_id,version.blob_sha256,version.observation_id,source.observed_at,source.retrieved_at,source.blob_sha256 AS source_blob_sha256 FROM documents document LEFT JOIN evidence_document_versions version ON version.legacy_document_id=document.id LEFT JOIN evidence_source_observations source ON source.observation_id=version.observation_id WHERE document.id=?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise ValueError("selected statement document missing")
            ticker = str(row["ticker"])
            kind = str(row["doc_type"])
            if (
                ticker not in ("WIX", "RBRK")
                or kind not in STATEMENT_TYPES
                or row["source_type"] != "fmp"
            ):
                raise ValueError("selected document outside canary cohort")
            if row["fetch_status"] != "ok":
                raise ValueError("selected source acquisition did not succeed")
            path = Path(str(row["file_path"]))
            if not path.is_absolute():
                path = repo_root / path
            snapshot, payload = read_stable_artifact(path)
            if (snapshot.file_sha256, snapshot.size_bytes) != (
                row["sha256"],
                row["raw_bytes_size"],
            ):
                raise ValueError("statement source byte identity mismatch")
            if row["blob_sha256"] is not None and row["blob_sha256"] != snapshot.file_sha256:
                raise ValueError("immutable capture disagrees with legacy file")
            if (
                row["source_blob_sha256"] is not None
                and row["source_blob_sha256"] != snapshot.file_sha256
            ):
                raise ValueError("source observation byte identity mismatch")
            fetched = _time(row["fetched_at"])
            observed = _time(row["observed_at"])
            retrieved = _time(row["retrieved_at"])
            if fetched is None or any(
                stamp > cutoff_at for stamp in (fetched, observed, retrieved) if stamp is not None
            ):
                raise ValueError("statement acquisition unavailable at cutoff")
            periods = _periods(payload, ticker)
            annual = all(period.vendor_period == "FY" for period in periods)
            if not annual and any(period.vendor_period == "FY" for period in periods):
                raise ValueError("mixed annual and quarterly cache scope")
            if any(period.period_end > cutoff_at.date() for period in periods):
                raise ValueError("statement reports a period beyond cutoff")
            files.append(
                CanaryFileSeal(
                    document_id=document_id,
                    ticker=ticker,
                    document_type=kind,
                    endpoint=STATEMENT_TYPES[kind],
                    filename=path.name,
                    sha256=snapshot.file_sha256,
                    byte_size=snapshot.size_bytes,
                    fetched_at=fetched,
                    evidence_document_version_id=row["document_version_id"],
                    source_observation_id=row["observation_id"],
                    observed_at=observed,
                    retrieved_at=retrieved,
                    cadence="annual" if annual else "quarterly",
                    periods=periods,
                )
            )
    finally:
        conn.row_factory = old_factory
    sealed = tuple(files)
    return CanaryCorpusSeal(
        cutoff_at=cutoff_at,
        files=sealed,
        corpus_sha256=_digest(sealed),
        immutable_capture_lineage_complete=all(
            item.source_observation_id and item.observed_at and item.retrieved_at for item in sealed
        ),
    )


def retained_coverage_inventory(
    conn: sqlite3.Connection, *, cutoff_at: datetime
) -> dict[str, object]:
    """Read current active roster plus governed foreign roster; metadata is not completeness."""
    from sources.foreign_filers import FOREIGN_FILER_ROSTER

    tracked = conn.execute(
        "SELECT ticker,list_type FROM tracked_companies WHERE archived_at IS NULL AND list_type IN ('portfolio','watchlist','evaluation') ORDER BY ticker,list_type"
    ).fetchall()
    ticker_lists: dict[str, set[str]] = {}
    for ticker, list_type in tracked:
        ticker_lists.setdefault(str(ticker), set()).add(str(list_type))
    population = sorted(set(ticker_lists) | set(FOREIGN_FILER_ROSTER))
    items: list[dict[str, object]] = []
    for ticker in population:
        records = conn.execute(
            "SELECT source_type,doc_type,count(*),max(fetched_at) FROM documents WHERE ticker=? AND datetime(fetched_at)<=datetime(?) GROUP BY source_type,doc_type ORDER BY source_type,doc_type",
            (ticker, cutoff_at.isoformat()),
        ).fetchall()
        items.append(
            {
                "ticker": ticker,
                "active_lists": sorted(ticker_lists.get(ticker, set())),
                "governed_foreign_filer": ticker in FOREIGN_FILER_ROSTER,
                "retained_metadata_groups": [
                    {
                        "source_type": str(row[0]),
                        "document_type": str(row[1]),
                        "document_count": int(row[2]),
                        "latest_fetched_at": row[3],
                    }
                    for row in records
                ],
                "cache_coverage": "metadata_present_unverified" if records else "unavailable",
                "acquisition_completeness": "unverified",
                "current_entitlement": "unverified",
                "output_readiness": "unverified",
            }
        )
    return {
        "roster_scope": "current_active_portfolio_watchlist_evaluation_plus_governed_foreign_roster",
        "historical_61_name_identity": "unverified",
        "active_ticker_count": len(ticker_lists),
        "governed_foreign_ticker_count": len(FOREIGN_FILER_ROSTER),
        "cutoff_at": cutoff_at.isoformat(),
        "tickers": items,
    }
