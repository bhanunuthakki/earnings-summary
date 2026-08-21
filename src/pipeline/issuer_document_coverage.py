"""Auditable issuer-document coverage for canonical KPI and segment facts.

The capture pipeline can persist a fact without proving whether each reported
item in its source document became usable downstream.  This module provides a
small, deterministic reconciliation seam: an extractor supplies its expected
reported facts and explicit rejection reasons, and the reconciler records which
facts were captured from that document plus the current (or historical-as-of)
canonical winner.  It deliberately performs no extraction and no database
mutation, so fixture replay and production activation stay separate.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compute.kpi_resolver import normalize_kpi_name
from models.facts import Currency, Unit
from provenance.source_coverage import (
    IssuerFactCoverageReceiptRecord,
    PersistResult,
    SourceCoverageLedger,
)
from timeseries.loaders import reader_source_order_sql


class IssuerFactKind(StrEnum):
    """The two canonical fact planes an issuer document can cover."""

    KPI = "kpi"
    SEGMENT = "segment"


class DownstreamAvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    MISSING = "missing"
    NOT_AVAILABLE_AS_OF = "not_available_as_of"
    UNVERIFIABLE = "unverifiable"


class _CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedIssuerFact(_CoverageModel):
    """One fact the reviewed document says should reach a canonical plane.

    ``canonical_name`` is deliberately a stable business label.  KPI lookup
    resolves harmless unit-only spelling variants while segment lookup uses the
    explicitly typed identity tuple rather than guessing from a display label.
    """

    ticker: str = Field(min_length=1, max_length=16)
    kind: IssuerFactKind
    canonical_name: str = Field(min_length=1, max_length=200)
    period_end: date
    fiscal_period_type: str = Field(min_length=1, max_length=16)
    unit: Unit
    currency: Currency | None = None
    segment_dim_type: str | None = None
    segment_name: str | None = None
    metric: str | None = None

    @model_validator(mode="after")
    def _segment_identity_is_complete(self) -> ExpectedIssuerFact:
        if self.kind is IssuerFactKind.SEGMENT and not all(
            (self.segment_dim_type, self.segment_name, self.metric)
        ):
            raise ValueError("segment coverage requires dim type, segment name, and metric")
        if (
            self.unit in {Unit.ACTUAL, Unit.THOUSANDS, Unit.MILLIONS, Unit.BILLIONS}
            and self.currency is None
        ):
            raise ValueError("monetary expected facts require an explicit currency")
        return self

    @property
    def identity_key(self) -> str:
        """Full fact identity used by deterministic extractor rejections.

        A display name is not unique: a KPI can recur in multiple periods and a
        segment name can exist under multiple axes.  Keeping this key explicit
        makes a rejection impossible to apply to a different expected fact.
        """
        return "|".join(
            (
                self.ticker.upper(),
                self.kind.value,
                normalize_kpi_name(self.canonical_name)
                if self.kind is IssuerFactKind.KPI
                else self.canonical_name,
                self.period_end.isoformat(),
                self.fiscal_period_type,
                self.unit.value,
                self.currency.value if self.currency is not None else "",
                self.segment_dim_type or "",
                self.segment_name or "",
                self.metric or "",
            )
        )


class DownstreamAvailability(_CoverageModel):
    """The winning canonical row a reader may use, or an honest gap."""

    status: DownstreamAvailabilityStatus
    fact_id: int | None = None
    document_id: int | None = None
    source_type: str | None = None
    source_url: str | None = None
    fetched_at: datetime | None = None
    reason: str | None = None


class IssuerFactCoverageResult(_CoverageModel):
    expected: ExpectedIssuerFact
    coverage_status: Literal["captured", "rejected", "missing"]
    captured_fact_ids: list[int] = Field(default_factory=list[int])
    rejection_reason: str | None = None
    downstream: DownstreamAvailability


class IssuerDocumentCoverageReceipt(_CoverageModel):
    """One immutable, replayable document-to-canonical-fact accounting."""

    schema_version: Literal["issuer_document_coverage.v1"] = "issuer_document_coverage.v1"
    document_id: int
    ticker: str
    source_type: str
    doc_type: str
    source_url: str | None = None
    source_fetched_at: datetime
    as_of: datetime | None = None
    stale_before: datetime | None = None
    extracted_at: datetime
    population_frame_json: str | None = None
    population_frame_sha256: str | None = None
    rejection_frame_json: str | None = None
    rejection_frame_sha256: str | None = None
    application_manifest_json: str | None = None
    application_manifest_sha256: str | None = None
    results: list[IssuerFactCoverageResult]

    @model_validator(mode="after")
    def _nested_fact_tickers_match_header(self) -> IssuerDocumentCoverageReceipt:
        header_ticker = self.ticker.upper()
        if any(result.expected.ticker.upper() != header_ticker for result in self.results):
            raise ValueError("receipt expected-fact ticker must match the receipt header")
        if not self.results:
            if self.population_frame_json is None or self.population_frame_sha256 is None:
                raise ValueError("zero expected receipts require an authoritative extractor frame")
            if (
                hashlib.sha256(self.population_frame_json.encode("utf-8")).hexdigest()
                != self.population_frame_sha256
            ):
                raise ValueError("population frame hash does not match extractor evidence")
            frame = ExtractorFactPopulationFrame.model_validate_json(self.population_frame_json)
            if (
                frame.expected_population_status != "zero_expected"
                or frame.document_id != self.document_id
                or frame.ticker.upper() != header_ticker
            ):
                raise ValueError("zero expected receipt frame must match document and ticker")
        elif self.population_frame_json is not None or self.population_frame_sha256 is not None:
            raise ValueError("population frame evidence is only valid for zero expected receipts")
        rejected = [result for result in self.results if result.coverage_status == "rejected"]
        if not rejected:
            if self.rejection_frame_json is not None or self.rejection_frame_sha256 is not None:
                raise ValueError("rejection evidence is only valid when a result is rejected")
            return self
        if self.rejection_frame_json is None or self.rejection_frame_sha256 is None:
            raise ValueError("rejected receipt results require immutable extractor-frame evidence")
        if (
            hashlib.sha256(self.rejection_frame_json.encode("utf-8")).hexdigest()
            != self.rejection_frame_sha256
        ):
            raise ValueError("rejection frame hash does not match rejection evidence")
        frame = ExtractorFactPopulationFrame.model_validate_json(self.rejection_frame_json)
        if frame.document_id != self.document_id or frame.ticker.upper() != header_ticker:
            raise ValueError("rejection frame document and ticker must match receipt")
        for result in rejected:
            if frame.rejected.get(result.expected.identity_key) != result.rejection_reason:
                raise ValueError("rejected result must match the authoritative extractor frame")
        return self

    @model_validator(mode="after")
    def _application_manifest_evidence_is_bound(self) -> IssuerDocumentCoverageReceipt:
        manifest_json = self.application_manifest_json
        manifest_sha256 = self.application_manifest_sha256
        if (manifest_json is None) != (manifest_sha256 is None):
            raise ValueError("application manifest JSON and SHA-256 must be supplied together")
        if manifest_json is None or manifest_sha256 is None:
            return self
        if hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != manifest_sha256:
            raise ValueError("application manifest hash does not match manifest evidence")
        try:
            decoded: object = json.loads(manifest_json)
        except json.JSONDecodeError as exc:
            raise ValueError("application manifest evidence must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("application manifest evidence must be a JSON object")
        payload = cast("dict[str, object]", decoded)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if canonical != manifest_json:
            raise ValueError("application manifest evidence must use canonical JSON")
        if payload.get("schema_version") != "issuer_fact_manifest.v1":
            raise ValueError("application manifest evidence has an unsupported schema version")
        if payload.get("source_doc_id") != self.document_id:
            raise ValueError("application manifest document must match receipt document")
        ticker = payload.get("ticker")
        if not isinstance(ticker, str) or ticker.upper() != self.ticker.upper():
            raise ValueError("application manifest ticker must match receipt ticker")
        return self

    @property
    def expected_count(self) -> int:
        return len(self.results)

    @property
    def captured_count(self) -> int:
        return sum(result.coverage_status == "captured" for result in self.results)

    @property
    def rejected_count(self) -> int:
        return sum(result.coverage_status == "rejected" for result in self.results)

    @property
    def missing_count(self) -> int:
        return sum(result.coverage_status == "missing" for result in self.results)


class ExtractorFactPopulationFrame(_CoverageModel):
    """Authoritative extractor hand-off for one document's fact population.

    The reconciliation boundary consumes this typed frame rather than tests or
    report code assembling an ad-hoc expected list.  It remains read-only: the
    extractor and its persistence lifecycle retain ownership of the database.
    """

    document_id: int
    ticker: str = Field(min_length=1, max_length=16)
    expected: tuple[ExpectedIssuerFact, ...]
    rejected: dict[str, str] = Field(default_factory=dict[str, str])
    extracted_at: datetime
    expected_population_status: Literal["populated", "zero_expected"] = "populated"

    @model_validator(mode="after")
    def _population_contract(self) -> ExtractorFactPopulationFrame:
        identities = [fact.identity_key for fact in self.expected]
        if any(fact.ticker.upper() != self.ticker.upper() for fact in self.expected):
            raise ValueError("expected fact tickers must match the extractor frame ticker")
        if len(identities) != len(set(identities)):
            raise ValueError("expected facts must have unique full identities")
        if self.expected_population_status == "populated" and not identities:
            raise ValueError("populated extractor frame requires expected facts")
        if self.expected_population_status == "zero_expected" and identities:
            raise ValueError("zero_expected frame cannot include expected facts")
        if set(self.rejected) - set(identities):
            raise ValueError("rejection keys must refer to expected fact identities")
        if any(not reason.strip() for reason in self.rejected.values()):
            raise ValueError("rejection reasons must be non-empty")
        return self


class ExtractorCoverageReconciliationOutput(_CoverageModel):
    """Stable CLI artifact for a reconciled extractor fact population."""

    idempotency_key: str = Field(min_length=64, max_length=64)
    receipt: IssuerDocumentCoverageReceipt


class PortfolioCoverageRow(_CoverageModel):
    """Document-level coverage totals grouped for a ticker-period portfolio view."""

    ticker: str
    period_end: date
    document_count: int
    expected_count: int
    captured_count: int
    rejected_count: int
    missing_count: int
    downstream_available_count: int
    downstream_stale_count: int
    downstream_missing_count: int
    downstream_unverifiable_count: int


class PortfolioCoverageReport(_CoverageModel):
    schema_version: Literal["issuer_portfolio_coverage.v1"] = "issuer_portfolio_coverage.v1"
    rows: list[PortfolioCoverageRow]


class CoverageSchemaError(RuntimeError):
    """Raised when a read cannot truthfully evaluate its required fact plane."""


# The receipt is a read-only evidence observation.  It introduces no operator
# control, Scheduler registration, listener, or health signal; surfacing it in
# Operations & Governance requires a separately approved product projection.
OPERATIONS_GOVERNANCE_DISPOSITION = "no_surface_change_read_only_coverage_observation"


def _utc(value: datetime) -> datetime:
    """Normalize both caller and SQLite timestamps to comparable UTC instants."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_datetime(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return _utc(raw)
    text = str(raw).strip()
    if not text:
        raise CoverageSchemaError("document fetched_at is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoverageSchemaError(f"document fetched_at is not ISO-like: {text!r}") from exc
    return _utc(parsed)


def _require_tables(conn: sqlite3.Connection, tables: set[str]) -> None:
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing = sorted(tables - present)
    if missing:
        raise CoverageSchemaError(f"issuer coverage needs tables: {', '.join(missing)}")


def _document_row(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row:
    _require_tables(conn, {"documents"})
    row = conn.execute(
        "SELECT id, ticker, source_type, doc_type, source_url, fetched_at "
        "FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        raise CoverageSchemaError(f"document {document_id} does not exist")
    return cast("sqlite3.Row", row)


def _iso_date(value: date) -> str:
    return value.isoformat()


def _source_preference_sql(source_type_column: str) -> str:
    """Rank issuer-published documents above a same-tier vendor normalisation.

    SEC official rows remain above both through ``tier_rank_case_sql``.  This
    is intentionally a *tie-break* within the source-quality tier: it does not
    claim that a transcript-derived number outranks an SEC filed fact, but it
    prevents generic FMP rows from winning over a same-period IR document just
    because they were inserted later.
    """
    # Kept as a private compatibility seam for callers outside this module.
    # New readers use ``reader_source_order_sql`` so tier-first policy has one
    # canonical implementation.
    from timeseries.loaders import issuer_origin_rank_sql

    return issuer_origin_rank_sql(source_type_column)


def _kpi_matches(expected: ExpectedIssuerFact, name: object) -> bool:
    return normalize_kpi_name(str(name)) == normalize_kpi_name(expected.canonical_name)


def _captured_kpi_ids(
    conn: sqlite3.Connection, document_id: int, expected: ExpectedIssuerFact
) -> list[int]:
    _require_tables(conn, {"kpi_definitions", "kpi_facts"})
    currency_sql = "kf.currency IS NULL" if expected.currency is None else "kf.currency = ?"
    currency_params: tuple[object, ...] = (
        () if expected.currency is None else (expected.currency.value,)
    )
    rows = conn.execute(
        "SELECT kf.id, kd.name FROM kpi_facts kf "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kf.source_doc_id = ? AND date(kf.period_end) = ? "
        f"AND kf.fiscal_period_type = ? AND kf.unit = ? AND {currency_sql} ORDER BY kf.id",
        (
            expected.ticker.upper(),
            document_id,
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.unit.value,
            *currency_params,
        ),
    ).fetchall()
    return [int(row["id"]) for row in rows if _kpi_matches(expected, row["name"])]


def _captured_segment_ids(
    conn: sqlite3.Connection, document_id: int, expected: ExpectedIssuerFact
) -> list[int]:
    _require_tables(conn, {"segment_periods", "segment_dimensions"})
    currency_sql = "sp.currency IS NULL" if expected.currency is None else "sp.currency = ?"
    currency_params: tuple[object, ...] = (
        () if expected.currency is None else (expected.currency.value,)
    )
    rows = conn.execute(
        "SELECT sd.id FROM segment_periods sp "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "JOIN segment_dimensions sd ON sd.period_id = sp.id "
        "WHERE sp.ticker = ? AND sp.source_doc_id = ? AND date(sp.period_end) = ? "
        "AND sp.fiscal_period_type = ? AND sd.dim_type = ? AND sd.dim_name = ? "
        "AND sd.metric = ? AND COALESCE(sd.unit, sp.unit) = ? "
        f"AND {currency_sql} ORDER BY sd.id",
        (
            expected.ticker.upper(),
            document_id,
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.segment_dim_type,
            expected.segment_name,
            expected.metric,
            expected.unit.value,
            *currency_params,
        ),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _as_of_clause(as_of: datetime | None) -> tuple[str, tuple[str, ...]]:
    if as_of is None:
        return ("", ())
    # SQLite does not order ISO-like timestamp spellings uniformly: a space,
    # ``T``, and ``Z`` all appear in historical document rows.  Compare the
    # parsed instants instead of lexical TEXT so an as-of boundary is stable.
    return (" AND julianday(d.fetched_at) <= julianday(?)", (_utc(as_of).isoformat(),))


def _availability_from_row(
    row: sqlite3.Row | None, stale_before: datetime | None, *, as_of: datetime | None
) -> DownstreamAvailability:
    if row is None:
        return DownstreamAvailability(
            status=(
                DownstreamAvailabilityStatus.NOT_AVAILABLE_AS_OF
                if as_of is not None
                else DownstreamAvailabilityStatus.MISSING
            ),
            reason="no canonical fact matched the document expectation",
        )
    fetched_at = _parse_datetime(row["fetched_at"])
    stale = stale_before is not None and fetched_at < _utc(stale_before)
    return DownstreamAvailability(
        status=DownstreamAvailabilityStatus.STALE
        if stale
        else DownstreamAvailabilityStatus.AVAILABLE,
        fact_id=int(row["fact_id"]),
        document_id=int(row["document_id"]),
        source_type=str(row["source_type"]),
        source_url=str(row["source_url"]) if row["source_url"] is not None else None,
        fetched_at=fetched_at,
        reason="canonical fact predates freshness threshold" if stale else None,
    )


def _downstream_kpi(
    conn: sqlite3.Connection,
    expected: ExpectedIssuerFact,
    *,
    as_of: datetime | None,
    stale_before: datetime | None,
) -> DownstreamAvailability:
    _require_tables(conn, {"documents", "kpi_definitions", "kpi_facts"})
    as_of_sql, as_of_params = _as_of_clause(as_of)
    currency_sql = "kf.currency IS NULL" if expected.currency is None else "kf.currency = ?"
    currency_params: tuple[object, ...] = (
        () if expected.currency is None else (expected.currency.value,)
    )
    rows = conn.execute(
        f"""
        SELECT kf.id AS fact_id, kf.source_doc_id AS document_id, kd.name,
               d.source_type, d.source_url, d.fetched_at
        FROM kpi_facts kf
        JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
        JOIN documents d ON d.id = kf.source_doc_id
        WHERE kf.ticker = ? AND date(kf.period_end) = ? AND kf.fiscal_period_type = ?
          AND kf.unit = ? AND {currency_sql}{as_of_sql}
        ORDER BY {reader_source_order_sql(conn)} ,
                 julianday(d.fetched_at) DESC, kf.id DESC
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
        (
            expected.ticker.upper(),
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.unit.value,
            *currency_params,
            *as_of_params,
        ),
    ).fetchall()
    row = next((row for row in rows if _kpi_matches(expected, row["name"])), None)
    availability = _availability_from_row(
        cast("sqlite3.Row | None", row), stale_before, as_of=as_of
    )
    if availability.status is not DownstreamAvailabilityStatus.NOT_AVAILABLE_AS_OF:
        return availability
    later = conn.execute(
        "SELECT kd.name FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id=kf.kpi_definition_id "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "JOIN documents d ON d.id=kf.source_doc_id WHERE kf.ticker=? AND date(kf.period_end)=? "
        "AND kf.fiscal_period_type=? AND kf.unit=? AND "
        f"{currency_sql}",
        (
            expected.ticker.upper(),
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.unit.value,
            *currency_params,
        ),
    ).fetchall()
    if any(_kpi_matches(expected, candidate[0]) for candidate in later):
        return availability.model_copy(
            update={"reason": "matching canonical fact was captured after as_of"}
        )
    return availability


def _downstream_segment(
    conn: sqlite3.Connection,
    expected: ExpectedIssuerFact,
    *,
    as_of: datetime | None,
    stale_before: datetime | None,
) -> DownstreamAvailability:
    _require_tables(conn, {"documents", "segment_periods", "segment_dimensions"})
    as_of_sql, as_of_params = _as_of_clause(as_of)
    currency_sql = "sp.currency IS NULL" if expected.currency is None else "sp.currency = ?"
    currency_params: tuple[object, ...] = (
        () if expected.currency is None else (expected.currency.value,)
    )
    row = conn.execute(
        f"""
        SELECT sd.id AS fact_id, sp.source_doc_id AS document_id,
               d.source_type, d.source_url, d.fetched_at
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        JOIN documents d ON d.id = sp.source_doc_id
        WHERE sp.ticker = ? AND date(sp.period_end) = ? AND sp.fiscal_period_type = ?
          AND sd.dim_type = ? AND sd.dim_name = ? AND sd.metric = ?
          AND COALESCE(sd.unit, sp.unit) = ?
          AND {currency_sql}{as_of_sql}
        ORDER BY {reader_source_order_sql(conn)} ,
                 julianday(d.fetched_at) DESC, sd.id DESC
        LIMIT 1
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
        (
            expected.ticker.upper(),
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.segment_dim_type,
            expected.segment_name,
            expected.metric,
            expected.unit.value,
            *currency_params,
            *as_of_params,
        ),
    ).fetchone()
    availability = _availability_from_row(
        cast("sqlite3.Row | None", row), stale_before, as_of=as_of
    )
    if availability.status is not DownstreamAvailabilityStatus.NOT_AVAILABLE_AS_OF:
        return availability
    later = conn.execute(
        "SELECT 1 FROM segment_periods sp JOIN segment_dimensions sd ON sd.period_id=sp.id "  # nosec B608 -- trusted internal SQL shape; values remain bound
        "WHERE sp.ticker=? AND date(sp.period_end)=? AND sp.fiscal_period_type=? "
        "AND sd.dim_type=? AND sd.dim_name=? AND sd.metric=? "
        f"AND COALESCE(sd.unit,sp.unit)=? AND {currency_sql}",
        (
            expected.ticker.upper(),
            _iso_date(expected.period_end),
            expected.fiscal_period_type,
            expected.segment_dim_type,
            expected.segment_name,
            expected.metric,
            expected.unit.value,
            *currency_params,
        ),
    ).fetchone()
    if later is not None:
        return availability.model_copy(
            update={"reason": "matching canonical fact was captured after as_of"}
        )
    return availability


def _captured_ids(
    conn: sqlite3.Connection, document_id: int, expected: ExpectedIssuerFact
) -> list[int]:
    if expected.kind is IssuerFactKind.KPI:
        return _captured_kpi_ids(conn, document_id, expected)
    return _captured_segment_ids(conn, document_id, expected)


def _downstream(
    conn: sqlite3.Connection,
    expected: ExpectedIssuerFact,
    *,
    as_of: datetime | None,
    stale_before: datetime | None,
) -> DownstreamAvailability:
    if expected.kind is IssuerFactKind.KPI:
        return _downstream_kpi(conn, expected, as_of=as_of, stale_before=stale_before)
    return _downstream_segment(conn, expected, as_of=as_of, stale_before=stale_before)


def build_document_coverage_receipt(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    expected: tuple[ExpectedIssuerFact, ...],
    rejected: dict[str, str] | None = None,
    as_of: datetime | None = None,
    stale_before: datetime | None = None,
    extracted_at: datetime | None = None,
    rejection_frame_json: str | None = None,
    rejection_frame_sha256: str | None = None,
    population_frame_json: str | None = None,
    population_frame_sha256: str | None = None,
    application_manifest_json: str | None = None,
    application_manifest_sha256: str | None = None,
) -> IssuerDocumentCoverageReceipt:
    """Reconcile one issuer document's declared facts without mutating the DB.

    ``rejected`` is supplied by the deterministic extractor keyed by each
    expected fact's :attr:`ExpectedIssuerFact.identity_key`, never a display
    name.  That makes a rejection explanation explicit rather than inferring a
    reason from an arbitrary validation row.  Every expected
    item is exactly one of captured, rejected, or missing; each result also
    records downstream availability separately, which prevents a present source
    capture from being misrepresented as an available reader value.
    """
    document = _document_row(conn, document_id)
    ticker = str(document["ticker"]).upper()
    rejected = rejected or {}
    now = _utc(extracted_at or datetime.now(UTC))
    normalized_as_of = _utc(as_of) if as_of is not None else None
    normalized_stale_before = _utc(stale_before) if stale_before is not None else None
    results: list[IssuerFactCoverageResult] = []
    for item in expected:
        if item.ticker.upper() != ticker:
            raise ValueError(
                f"expected ticker {item.ticker.upper()} does not match document {document_id} ticker {ticker}"
            )
        ids = _captured_ids(conn, document_id, item)
        downstream = _downstream(
            conn,
            item,
            as_of=normalized_as_of,
            stale_before=normalized_stale_before,
        )
        rejection_reason = rejected.get(item.identity_key)
        if ids:
            status: Literal["captured", "rejected", "missing"] = "captured"
            rejection_reason = None
        elif rejection_reason:
            status = "rejected"
        else:
            status = "missing"
        results.append(
            IssuerFactCoverageResult(
                expected=item,
                coverage_status=status,
                captured_fact_ids=ids,
                rejection_reason=rejection_reason,
                downstream=downstream,
            )
        )
    return IssuerDocumentCoverageReceipt(
        document_id=document_id,
        ticker=ticker,
        source_type=str(document["source_type"]),
        doc_type=str(document["doc_type"]),
        source_url=str(document["source_url"]) if document["source_url"] is not None else None,
        source_fetched_at=_parse_datetime(document["fetched_at"]),
        as_of=normalized_as_of,
        stale_before=normalized_stale_before,
        extracted_at=now,
        population_frame_json=population_frame_json,
        population_frame_sha256=population_frame_sha256,
        rejection_frame_json=rejection_frame_json,
        rejection_frame_sha256=rejection_frame_sha256,
        application_manifest_json=application_manifest_json,
        application_manifest_sha256=application_manifest_sha256,
        results=results,
    )


def reconcile_extractor_fact_population(
    conn: sqlite3.Connection,
    frame: ExtractorFactPopulationFrame,
    *,
    as_of: datetime | None = None,
    stale_before: datetime | None = None,
    application_manifest_json: str | None = None,
    application_manifest_sha256: str | None = None,
) -> IssuerDocumentCoverageReceipt:
    """Read-only reconciliation entry point for a persisted extractor frame."""
    frame_json = json.dumps(
        frame.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    frame_sha256 = hashlib.sha256(frame_json.encode("utf-8")).hexdigest()
    return build_document_coverage_receipt(
        conn,
        document_id=frame.document_id,
        expected=frame.expected,
        rejected=frame.rejected,
        as_of=as_of,
        stale_before=stale_before,
        extracted_at=frame.extracted_at,
        rejection_frame_json=frame_json if frame.rejected else None,
        rejection_frame_sha256=frame_sha256 if frame.rejected else None,
        population_frame_json=frame_json if not frame.expected else None,
        population_frame_sha256=frame_sha256 if not frame.expected else None,
        application_manifest_json=application_manifest_json,
        application_manifest_sha256=application_manifest_sha256,
    )


def persist_document_coverage_receipt(
    conn: sqlite3.Connection, receipt: IssuerDocumentCoverageReceipt
) -> tuple[PersistResult, ...]:
    """Atomically persist one fact-level receipt through the coverage ledger."""
    validate_receipt_against_sqlite(conn, receipt)
    records: list[IssuerFactCoverageReceiptRecord] = []
    as_of = receipt.as_of.isoformat() if receipt.as_of is not None else "current"
    stale_before = receipt.stale_before.isoformat() if receipt.stale_before is not None else "none"
    for result in receipt.results or [None]:
        fact_identity = (
            "__zero_expected_population__" if result is None else result.expected.identity_key
        )
        reconciliation_key = f"{receipt.document_id}|{fact_identity}|{as_of}|{stale_before}"
        payload = json.dumps(
            {
                "receipt": receipt.model_dump(mode="json", exclude={"results"}),
                "result": None if result is None else result.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        records.append(
            IssuerFactCoverageReceiptRecord(
                record_id=f"issuer-fact-coverage:{digest}",
                idempotency_key=digest,
                reconciliation_key=reconciliation_key,
                document_id=receipt.document_id,
                ticker=receipt.ticker,
                fact_identity=fact_identity,
                receipt_json=payload,
                receipt_sha256=digest,
                recorded_at=receipt.extracted_at,
            )
        )
    return SourceCoverageLedger(conn).persist_many(tuple(records))


def validate_receipt_against_sqlite(
    conn: sqlite3.Connection, receipt: IssuerDocumentCoverageReceipt
) -> None:
    """Prove each receipt result against authoritative fact rows before append."""
    # ``model_copy(update=...)`` intentionally avoids validation in Pydantic.
    # Re-parse here because this public persistence boundary must reject a
    # forged frame/hash even when a caller constructed a frozen model locally.
    IssuerDocumentCoverageReceipt.model_validate(receipt.model_dump(mode="json"))
    document = _document_row(conn, receipt.document_id)
    if str(document["ticker"]).upper() != receipt.ticker.upper():
        raise ValueError("receipt ticker must match the referenced document")
    document_source_url = (
        str(document["source_url"]) if document["source_url"] is not None else None
    )
    if (
        receipt.source_type != str(document["source_type"])
        or receipt.doc_type != str(document["doc_type"])
        or receipt.source_url != document_source_url
        or _utc(receipt.source_fetched_at) != _parse_datetime(document["fetched_at"])
    ):
        raise ValueError("receipt document header must exactly match the referenced document")
    if receipt.application_manifest_json is not None:
        manifest_payload = cast("dict[str, object]", json.loads(receipt.application_manifest_json))
        try:
            source_sha_row = conn.execute(
                "SELECT sha256 FROM documents WHERE id = ?", (receipt.document_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise CoverageSchemaError(
                "application manifest validation requires documents.sha256"
            ) from exc
        if source_sha_row is None or source_sha_row[0] is None:
            raise ValueError("application manifest source document must have a persisted SHA-256")
        manifest_source_sha = manifest_payload.get("source_doc_sha256")
        if (
            not isinstance(manifest_source_sha, str)
            or manifest_source_sha.lower() != str(source_sha_row[0]).lower()
        ):
            raise ValueError(
                "application manifest source SHA-256 must match the referenced document"
            )
    for result in receipt.results:
        expected = result.expected
        if expected.ticker.upper() != receipt.ticker.upper():
            raise ValueError("receipt expected-fact ticker must match the receipt header")
        captured_ids = (
            _captured_kpi_ids(conn, receipt.document_id, expected)
            if expected.kind is IssuerFactKind.KPI
            else _captured_segment_ids(conn, receipt.document_id, expected)
        )
        if captured_ids and result.coverage_status != "captured":
            raise ValueError(
                "receipt cannot mark an existing source-document fact missing or rejected"
            )
        if not captured_ids and result.coverage_status == "captured":
            raise ValueError("receipt cannot mark absent source-document facts captured")
        if result.coverage_status == "captured":
            if not result.captured_fact_ids or sorted(result.captured_fact_ids) != captured_ids:
                raise ValueError(
                    "captured receipt fact ids must exactly match source-document facts"
                )
            if result.rejection_reason is not None:
                raise ValueError("captured receipt result cannot carry a rejection reason")
        elif result.coverage_status == "rejected":
            if result.captured_fact_ids or not result.rejection_reason:
                raise ValueError(
                    "rejected receipt result needs a reason and cannot claim captured facts"
                )
        else:
            if result.captured_fact_ids or result.rejection_reason is not None:
                raise ValueError("missing receipt result cannot claim captured facts or rejection")
        downstream = _downstream(
            conn,
            expected,
            as_of=receipt.as_of,
            stale_before=receipt.stale_before,
        )
        if result.downstream != downstream:
            raise ValueError("receipt downstream evidence must match authoritative fact lineage")


def reconciliation_output(
    receipt: IssuerDocumentCoverageReceipt,
) -> ExtractorCoverageReconciliationOutput:
    canonical = receipt.model_dump_json(exclude_none=False)
    return ExtractorCoverageReconciliationOutput(
        idempotency_key=hashlib.sha256(canonical.encode("utf-8")).hexdigest(), receipt=receipt
    )


def portfolio_coverage_report(
    receipts: list[IssuerDocumentCoverageReceipt],
) -> PortfolioCoverageReport:
    """Roll document receipts into a deterministic ticker-period completeness view.

    Counts retain the document axis: a fact expected in both a filing and an
    earnings presentation is two independent coverage obligations, not a
    silently collapsed metric union.  This exposes partial extraction of a
    particular source document.
    """
    grouped: dict[tuple[str, date], list[IssuerDocumentCoverageReceipt]] = {}
    for receipt in receipts:
        for period_end in {result.expected.period_end for result in receipt.results}:
            relevant = [
                result for result in receipt.results if result.expected.period_end == period_end
            ]
            grouped.setdefault((receipt.ticker, period_end), []).append(
                receipt.model_copy(update={"results": relevant})
            )

    rows: list[PortfolioCoverageRow] = []
    for (ticker, period_end), grouped_receipts in sorted(grouped.items()):
        results = [result for receipt in grouped_receipts for result in receipt.results]
        statuses = [result.downstream.status for result in results]
        rows.append(
            PortfolioCoverageRow(
                ticker=ticker,
                period_end=period_end,
                document_count=len({receipt.document_id for receipt in grouped_receipts}),
                expected_count=len(results),
                captured_count=sum(result.coverage_status == "captured" for result in results),
                rejected_count=sum(result.coverage_status == "rejected" for result in results),
                missing_count=sum(result.coverage_status == "missing" for result in results),
                downstream_available_count=sum(
                    status is DownstreamAvailabilityStatus.AVAILABLE for status in statuses
                ),
                downstream_stale_count=sum(
                    status is DownstreamAvailabilityStatus.STALE for status in statuses
                ),
                downstream_missing_count=sum(
                    status
                    in {
                        DownstreamAvailabilityStatus.MISSING,
                        DownstreamAvailabilityStatus.NOT_AVAILABLE_AS_OF,
                    }
                    for status in statuses
                ),
                downstream_unverifiable_count=sum(
                    status is DownstreamAvailabilityStatus.UNVERIFIABLE for status in statuses
                ),
            )
        )
    return PortfolioCoverageReport(rows=rows)
