"""Bootstrap the canonical reporting universe from SEC ticker authority evidence.

The SEC company-ticker registry is evidence for a ticker-to-CIK assertion, not
an issuer ID.  Only a ticker with exactly one SEC registry entry is mapped.
Missing and duplicate mappings remain explicit unresolved legacy bindings.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pipeline.queries import ANALYZED_LIST_TYPE_VALUES
from provenance.evidence_ledger import ContentBlob, EvidenceLedger, SourceObservation
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerProfileRevision,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
    ReportingScopeRevision,
    Security,
    ensure_sec_cik_evidence_binding,
    identifier_candidate_digest,
    normalize_identifier,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityIdentifierAssertion,
    ReportingEntityIdentifierResolution,
    ReportingEntityRegistry,
    SecurityIdentifierAssertion,
    SecurityIdentifierResolution,
    SecurityReportingEntityRevision,
    SourceObligationRevision,
    reporting_identifier_candidate_digest,
    security_identifier_candidate_digest,
)

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_MUTUAL_FUND_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
_COLLECTOR_VERSION = "issuer-registry-bootstrap@1"
_FUND_COLLECTOR_VERSION = "sec-fund-registry-bootstrap@1"
_POLICY_NAME = "unique_sec_ticker_to_cik"
_POLICY_VERSION = "1"
_ACTIVE_LIST_TYPES = frozenset(ANALYZED_LIST_TYPE_VALUES)
_LEGACY_TICKER_PREFIX = "legacy-ticker:"

InclusionState = Literal["core", "monitored", "historical"]


class SecCompanyTickerContractError(ValueError):
    """The SEC registry response is not the closed company-ticker contract."""


class SecCompanyTickerFetchError(RuntimeError):
    """A single authorized SEC registry request did not succeed."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SecCompanyTickerEntry(_ClosedModel):
    cik_str: int = Field(gt=0, le=9_999_999_999)
    ticker: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1)

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must not be blank")
        return normalized

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized

    @property
    def normalized_cik(self) -> str:
        return normalize_identifier("sec_cik", str(self.cik_str))


class SecMutualFundTickerEntry(_ClosedModel):
    cik: int = Field(gt=0, le=9_999_999_999)
    series_id: str = Field(pattern=r"^S\d{9}$")
    class_id: str = Field(pattern=r"^C\d{9}$")
    symbol: str = Field(max_length=32)

    @field_validator("series_id", "class_id", "symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def normalized_cik(self) -> str:
        return normalize_identifier("sec_cik", str(self.cik))


class SecFundRegistrantEvidence(_ClosedModel):
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    source_url: str = Field(min_length=1)
    raw_body: bytes = Field(min_length=1)

    @field_validator("normalized_cik")
    @classmethod
    def _cik(cls, value: str) -> str:
        return normalize_identifier("sec_cik", value)


class SecFundBootstrapRequest(_ClosedModel):
    source_url: str = Field(min_length=1)
    registrants: tuple[SecFundRegistrantEvidence, ...]
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SecFundTickerResult(_ClosedModel):
    ticker: str
    outcome: Literal[
        "selected",
        "unresolved_missing",
        "unresolved_duplicate",
        "missing_registrant",
    ]
    inclusion_state: InclusionState
    canonical_issuer_id: str | None = None
    normalized_cik: str | None = None
    sec_series_id: str | None = None
    sec_class_contract_id: str | None = None
    candidate_count: int = Field(ge=0)
    records_created: int = Field(ge=0)
    reason_code: str


class SecFundBootstrapResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    source_sha256: str
    source_observation_id: str | None
    selected_tickers: tuple[str, ...]
    excluded_index_member_count: int = Field(ge=0)
    results: tuple[SecFundTickerResult, ...]
    records_created: int = Field(ge=0)


class SecHistoricalIssuerIdentity(_ClosedModel):
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    legal_name: str = Field(min_length=1)
    termination_form: Literal["15-12G", "15-15D"]
    termination_date: date
    termination_accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    termination_primary_document: str = Field(min_length=1)


class SecHistoricalIssuerRequest(_ClosedModel):
    ticker: str = Field(min_length=1, max_length=32)
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    source_url: str = Field(min_length=1)
    raw_body: bytes = Field(min_length=1)
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("normalized_cik")
    @classmethod
    def _cik(cls, value: str) -> str:
        return normalize_identifier("sec_cik", value)

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SecHistoricalIssuerResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    ticker: str
    normalized_cik: str
    canonical_issuer_id: str
    legal_name: str
    termination_form: str
    termination_date: date
    source_sha256: str
    source_observation_id: str | None
    records_created: int = Field(ge=0)


class SecFormerTickerIdentity(_ClosedModel):
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    legal_name: str = Field(min_length=1)
    former_ticker: str = Field(min_length=1, max_length=32)
    successor_ticker: str = Field(min_length=1, max_length=32)
    transition_date: date
    transition_accession: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")


class SecFormerTickerRequest(_ClosedModel):
    former_ticker: str = Field(min_length=1, max_length=32)
    successor_ticker: str = Field(min_length=1, max_length=32)
    normalized_cik: str = Field(pattern=r"^\d{10}$")
    transition_date: date
    submissions_source_url: str = Field(min_length=1)
    submissions_raw_body: bytes = Field(min_length=1)
    transition_source_url: str = Field(min_length=1)
    transition_raw_body: bytes = Field(min_length=1)
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    @field_validator("former_ticker", "successor_ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("normalized_cik")
    @classmethod
    def _cik(cls, value: str) -> str:
        return normalize_identifier("sec_cik", value)

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SecFormerTickerResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    former_ticker: str
    successor_ticker: str
    normalized_cik: str
    canonical_issuer_id: str
    legal_name: str
    transition_date: date
    transition_accession: str
    submissions_sha256: str
    transition_sha256: str
    submissions_observation_id: str | None
    transition_observation_id: str | None
    records_created: int = Field(ge=0)


class BootstrapRequest(_ClosedModel):
    source_url: str = Field(min_length=1)
    blob_root: Path
    apply: bool = False
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BootstrapTickerResult(_ClosedModel):
    ticker: str
    outcome: Literal["selected", "unresolved_duplicate", "unresolved_missing"]
    inclusion_state: InclusionState
    canonical_issuer_id: str | None = None
    normalized_cik: str | None = None
    candidate_count: int = Field(ge=0)
    records_created: int = Field(ge=0)
    reason_code: str


class BootstrapResult(_ClosedModel):
    mode: Literal["dry_run", "apply"]
    source_sha256: str
    source_observation_id: str | None
    selected_tickers: tuple[str, ...]
    excluded_index_member_count: int = Field(ge=0)
    results: tuple[BootstrapTickerResult, ...]
    records_created: int = Field(ge=0)


class _HTTPResponse(Protocol):
    status_code: int
    content: bytes


class HTTPSession(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: tuple[int, int],
    ) -> _HTTPResponse: ...


class _TrackedTicker(_ClosedModel):
    ticker: str
    inclusion_state: InclusionState
    list_types: tuple[str, ...]


def parse_sec_company_tickers(raw_body: bytes) -> tuple[SecCompanyTickerEntry, ...]:
    """Validate the complete SEC object without dropping malformed entries."""

    try:
        decoded_object: object = json.loads(
            raw_body,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecCompanyTickerContractError("SEC company-ticker body is not valid JSON") from exc
    if not isinstance(decoded_object, dict) or not decoded_object:
        raise SecCompanyTickerContractError("SEC company-ticker body must be a non-empty object")
    decoded = cast(dict[object, object], decoded_object)
    entries: list[SecCompanyTickerEntry] = []
    try:
        for key, value in decoded.items():
            if not isinstance(key, str) or not key.isdigit():
                raise SecCompanyTickerContractError(
                    "SEC company-ticker object keys must be decimal strings"
                )
            entries.append(SecCompanyTickerEntry.model_validate(value))
    except ValidationError as exc:
        raise SecCompanyTickerContractError(
            "SEC company-ticker entry violates the closed contract"
        ) from exc
    return tuple(entries)


def parse_sec_mutual_fund_tickers(
    raw_body: bytes,
) -> tuple[SecMutualFundTickerEntry, ...]:
    """Validate the SEC series/class ticker registry without dropping rows."""

    try:
        decoded_object: object = json.loads(
            raw_body,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecCompanyTickerContractError(
            "SEC mutual-fund ticker body is not valid JSON"
        ) from exc
    if not isinstance(decoded_object, dict):
        raise SecCompanyTickerContractError("SEC mutual-fund ticker body must be an object")
    decoded = cast(dict[object, object], decoded_object)
    if set(decoded) != {"fields", "data"}:
        raise SecCompanyTickerContractError("SEC mutual-fund ticker root contract changed")
    if decoded["fields"] != ["cik", "seriesId", "classId", "symbol"]:
        raise SecCompanyTickerContractError("SEC mutual-fund ticker fields contract changed")
    raw_rows = decoded["data"]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise SecCompanyTickerContractError("SEC mutual-fund ticker data must be a non-empty array")
    entries: list[SecMutualFundTickerEntry] = []
    try:
        for index, row in enumerate(cast(list[object], raw_rows)):
            if not isinstance(row, list):
                raise SecCompanyTickerContractError(
                    f"SEC mutual-fund ticker row {index} violates the four-column contract"
                )
            values = cast(list[object], row)
            if len(values) != 4:
                raise SecCompanyTickerContractError(
                    f"SEC mutual-fund ticker row {index} violates the four-column contract"
                )
            entries.append(
                SecMutualFundTickerEntry.model_validate(
                    {
                        "cik": values[0],
                        "series_id": values[1],
                        "class_id": values[2],
                        "symbol": values[3],
                    }
                )
            )
    except ValidationError as exc:
        raise SecCompanyTickerContractError(
            "SEC mutual-fund ticker row violates the closed contract"
        ) from exc
    identities = {
        (entry.normalized_cik, entry.series_id, entry.class_id, entry.symbol) for entry in entries
    }
    if len(identities) != len(entries):
        raise SecCompanyTickerContractError(
            "SEC mutual-fund ticker registry repeats an identical row"
        )
    return tuple(entries)


def parse_sec_fund_registrant_identity(
    raw_body: bytes,
    *,
    normalized_cik: str,
) -> str:
    """Extract the legal registrant name from its SEC submissions authority file."""

    expected_cik = normalize_identifier("sec_cik", normalized_cik)
    try:
        decoded_object: object = json.loads(
            raw_body,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecCompanyTickerContractError(
            "SEC registrant submissions body is not valid JSON"
        ) from exc
    if not isinstance(decoded_object, dict):
        raise SecCompanyTickerContractError("SEC registrant submissions body must be an object")
    decoded = cast(dict[object, object], decoded_object)
    if normalize_identifier("sec_cik", str(decoded.get("cik", ""))) != expected_cik:
        raise SecCompanyTickerContractError(
            "SEC registrant submissions CIK conflicts with requested CIK"
        )
    legal_name = str(decoded.get("name") or "").strip()
    if not legal_name:
        raise SecCompanyTickerContractError("SEC registrant submissions body has no legal name")
    if str(decoded.get("entityType") or "").strip().lower() != "investment":
        raise SecCompanyTickerContractError(
            "SEC registrant submissions body is not an investment-company registrant"
        )
    return legal_name


def parse_sec_historical_issuer_identity(
    raw_body: bytes,
    *,
    normalized_cik: str,
    ticker: str,
) -> SecHistoricalIssuerIdentity:
    expected_cik = normalize_identifier("sec_cik", normalized_cik)
    normalized_ticker = ticker.strip().upper()
    try:
        decoded_object: object = json.loads(
            raw_body,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecCompanyTickerContractError(
            "SEC historical submissions body is not valid JSON"
        ) from exc
    if not isinstance(decoded_object, dict):
        raise SecCompanyTickerContractError("SEC historical submissions body must be an object")
    decoded = cast(dict[object, object], decoded_object)
    try:
        payload_cik = normalize_identifier("sec_cik", str(decoded["cik"]))
    except (KeyError, ValueError) as exc:
        raise SecCompanyTickerContractError(
            "SEC historical submissions body has no valid CIK"
        ) from exc
    if payload_cik != expected_cik:
        raise SecCompanyTickerContractError(
            "SEC historical submissions CIK conflicts with requested CIK"
        )
    if str(decoded.get("entityType") or "").strip().lower() != "operating":
        raise SecCompanyTickerContractError(
            "SEC historical issuer is not an operating-company registrant"
        )
    legal_name = str(decoded.get("name") or "").strip()
    if not legal_name:
        raise SecCompanyTickerContractError("SEC historical submissions body has no legal name")
    tickers = decoded.get("tickers")
    if not isinstance(tickers, list):
        raise SecCompanyTickerContractError("SEC historical submissions tickers contract changed")
    current_tickers = {str(value).strip().upper() for value in cast(list[object], tickers)}
    if normalized_ticker in current_tickers:
        raise SecCompanyTickerContractError(
            "requested historical ticker is still current in SEC submissions"
        )
    filings = decoded.get("filings")
    if not isinstance(filings, dict):
        raise SecCompanyTickerContractError("SEC historical submissions has no filings object")
    recent = cast(dict[object, object], filings).get("recent")
    if not isinstance(recent, dict):
        raise SecCompanyTickerContractError("SEC historical submissions has no filings.recent")
    recent_map = cast(dict[object, object], recent)
    required_columns = (
        "form",
        "filingDate",
        "accessionNumber",
        "primaryDocument",
    )
    columns: dict[str, list[object]] = {}
    for column_name in required_columns:
        column = recent_map.get(column_name)
        if not isinstance(column, list):
            raise SecCompanyTickerContractError(
                f"SEC historical submissions missing recent.{column_name}"
            )
        columns[column_name] = cast(list[object], column)
    if len({len(column) for column in columns.values()}) != 1:
        raise SecCompanyTickerContractError(
            "SEC historical submissions termination columns differ in length"
        )
    candidates: list[SecHistoricalIssuerIdentity] = []
    for index, form_value in enumerate(columns["form"]):
        form = str(form_value).strip().upper()
        if form not in {"15-12G", "15-15D"}:
            continue
        try:
            termination_date = date.fromisoformat(str(columns["filingDate"][index]))
            candidates.append(
                SecHistoricalIssuerIdentity(
                    normalized_cik=expected_cik,
                    legal_name=legal_name,
                    termination_form=cast(
                        Literal["15-12G", "15-15D"],
                        form,
                    ),
                    termination_date=termination_date,
                    termination_accession=str(columns["accessionNumber"][index]),
                    termination_primary_document=str(columns["primaryDocument"][index]),
                )
            )
        except (ValueError, ValidationError) as exc:
            raise SecCompanyTickerContractError(
                "SEC historical submissions has an invalid Form 15 identity row"
            ) from exc
    if not candidates:
        raise SecCompanyTickerContractError(
            "SEC historical submissions has no Form 15 reporting termination"
        )
    return max(candidates, key=lambda item: item.termination_date)


def parse_sec_former_ticker_identity(
    submissions_raw_body: bytes,
    transition_raw_body: bytes,
    *,
    normalized_cik: str,
    former_ticker: str,
    successor_ticker: str,
    transition_date: date,
    transition_source_url: str,
) -> SecFormerTickerIdentity:
    """Verify a retired ticker against current SEC identity and a transition filing."""

    expected_cik = normalize_identifier("sec_cik", normalized_cik)
    former = former_ticker.strip().upper()
    successor = successor_ticker.strip().upper()
    if former == successor:
        raise SecCompanyTickerContractError("former and successor tickers must differ")
    try:
        decoded_object: object = json.loads(
            submissions_raw_body,
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecCompanyTickerContractError(
            "SEC former-ticker submissions body is not valid JSON"
        ) from exc
    if not isinstance(decoded_object, dict):
        raise SecCompanyTickerContractError("SEC former-ticker submissions body must be an object")
    decoded = cast(dict[object, object], decoded_object)
    try:
        payload_cik = normalize_identifier("sec_cik", str(decoded["cik"]))
    except (KeyError, ValueError) as exc:
        raise SecCompanyTickerContractError(
            "SEC former-ticker submissions body has no valid CIK"
        ) from exc
    if payload_cik != expected_cik:
        raise SecCompanyTickerContractError(
            "SEC former-ticker submissions CIK conflicts with requested CIK"
        )
    if str(decoded.get("entityType") or "").strip().lower() != "operating":
        raise SecCompanyTickerContractError(
            "SEC former-ticker issuer is not an operating-company registrant"
        )
    legal_name = str(decoded.get("name") or "").strip()
    if not legal_name:
        raise SecCompanyTickerContractError("SEC former-ticker submissions body has no legal name")
    tickers = decoded.get("tickers")
    if not isinstance(tickers, list):
        raise SecCompanyTickerContractError(
            "SEC former-ticker submissions tickers contract changed"
        )
    current_tickers = {str(value).strip().upper() for value in cast(list[object], tickers)}
    if successor not in current_tickers:
        raise SecCompanyTickerContractError(
            "requested successor ticker is not current in SEC submissions"
        )
    if former in current_tickers:
        raise SecCompanyTickerContractError(
            "requested former ticker is still current in SEC submissions"
        )

    match = re.fullmatch(
        r"https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{18})/[^/?#]+\.html?",
        transition_source_url,
    )
    if match is None or match.group(1) != str(int(expected_cik)):
        raise SecCompanyTickerContractError(
            "SEC former-ticker transition URL is not an exact issuer filing document"
        )
    accession_digits = match.group(2)
    accession = f"{accession_digits[:10]}-{accession_digits[10:12]}-{accession_digits[12:]}"
    transition_text = " ".join(
        BeautifulSoup(transition_raw_body, "html.parser").get_text(" ", strip=True).split()
    )
    folded = transition_text.casefold()
    required_cik = expected_cik.casefold()
    date_markers = {
        transition_date.strftime("%B %d, %Y").replace(" 0", " ").casefold(),
        transition_date.isoformat().casefold(),
    }
    former_pattern = re.compile(
        rf"(?:previously|prior).{{0,240}}\b{re.escape(former.casefold())}\b",
    )
    successor_pattern = re.compile(
        rf"(?:ticker symbol|under the symbol).{{0,120}}\b{re.escape(successor.casefold())}\b",
    )
    if required_cik not in folded:
        raise SecCompanyTickerContractError(
            "SEC former-ticker transition filing does not identify the requested CIK"
        )
    if former_pattern.search(folded) is None or successor_pattern.search(folded) is None:
        raise SecCompanyTickerContractError(
            "SEC filing does not prove the requested former-to-successor ticker transition"
        )
    if not any(marker in folded for marker in date_markers):
        raise SecCompanyTickerContractError(
            "SEC filing does not prove the requested ticker transition date"
        )
    return SecFormerTickerIdentity(
        normalized_cik=expected_cik,
        legal_name=legal_name,
        former_ticker=former,
        successor_ticker=successor,
        transition_date=transition_date,
        transition_accession=accession,
    )


def target_sec_fund_registrant_ciks(
    conn: sqlite3.Connection,
    *,
    raw_body: bytes,
) -> tuple[str, ...]:
    entries = parse_sec_mutual_fund_tickers(raw_body)
    tracked, _ = _tracked_reporting_scope(conn)
    tracked_tickers = {item.ticker for item in tracked}
    by_symbol: dict[str, list[SecMutualFundTickerEntry]] = defaultdict(list)
    for entry in entries:
        if entry.symbol in tracked_tickers:
            by_symbol[entry.symbol].append(entry)
    return tuple(
        sorted(
            {
                candidates[0].normalized_cik
                for candidates in by_symbol.values()
                if len(candidates) == 1
            }
        )
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SecCompanyTickerContractError(
                f"SEC company-ticker JSON repeats object key {key!r}"
            )
        result[key] = value
    return result


def fetch_sec_company_tickers(
    session: HTTPSession,
    *,
    source_url: str,
    user_agent: str,
) -> bytes:
    """Perform exactly one SEC company-ticker request."""

    response = session.get(
        source_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=(10, 60),
    )
    if response.status_code in {401, 403}:
        raise SecCompanyTickerFetchError(
            f"SEC returned {response.status_code}; verify the declared User-Agent"
        )
    if response.status_code != 200:
        raise SecCompanyTickerFetchError(
            f"SEC company-ticker request returned status {response.status_code}"
        )
    return bytes(response.content)


def bootstrap_issuer_reporting_registry(
    conn: sqlite3.Connection,
    *,
    raw_body: bytes,
    request: BootstrapRequest,
) -> BootstrapResult:
    """Plan or atomically apply a canonical registry bootstrap."""

    entries = parse_sec_company_tickers(raw_body)
    tracked, excluded_index_member_count = _tracked_reporting_scope(conn)
    candidates: dict[str, list[SecCompanyTickerEntry]] = defaultdict(list)
    for entry in entries:
        candidates[entry.ticker].append(entry)
    source_sha = hashlib.sha256(raw_body).hexdigest()

    if not request.apply:
        planned = tuple(
            _planned_result(item, tuple(candidates.get(item.ticker, ()))) for item in tracked
        )
        return BootstrapResult(
            mode="dry_run",
            source_sha256=source_sha,
            source_observation_id=None,
            selected_tickers=tuple(item.ticker for item in tracked),
            excluded_index_member_count=excluded_index_member_count,
            results=planned,
            records_created=0,
        )

    with conn:
        observation_id, evidence_created, recorded_at = _capture_source(
            conn,
            raw_body=raw_body,
            request=request,
            source_sha=source_sha,
        )
        registry = IssuerRegistry(conn)
        results: list[BootstrapTickerResult] = []
        created = evidence_created
        issuer_scope: dict[str, Literal["core", "monitored"]] = {}
        issuer_scope_types: dict[str, set[str]] = defaultdict(set)
        for item in tracked:
            if item.inclusion_state == "historical":
                continue
            ticker_candidates = tuple(candidates.get(item.ticker, ()))
            if len(ticker_candidates) != 1:
                continue
            issuer_id = _issuer_id(ticker_candidates[0].normalized_cik)
            if issuer_id not in issuer_scope or item.inclusion_state == "core":
                issuer_scope[issuer_id] = item.inclusion_state
            issuer_scope_types[issuer_id].update(item.list_types)
        for item in tracked:
            ticker_candidates = tuple(candidates.get(item.ticker, ()))
            if len(ticker_candidates) != 1:
                unresolved, unresolved_created = _persist_unresolved_binding(
                    conn,
                    registry=registry,
                    item=item,
                    candidate_count=len(ticker_candidates),
                    source_sha=source_sha,
                    recorded_at=recorded_at,
                )
                results.append(unresolved)
                created += unresolved_created
                continue
            selected, selected_created = _persist_selected_ticker(
                conn,
                registry=registry,
                item=item,
                entry=ticker_candidates[0],
                observation_id=observation_id,
                source_sha=source_sha,
                recorded_at=recorded_at,
                scope_inclusion_state=(
                    None
                    if item.inclusion_state == "historical"
                    else issuer_scope[_issuer_id(ticker_candidates[0].normalized_cik)]
                ),
                scope_list_types=(
                    ()
                    if item.inclusion_state == "historical"
                    else tuple(
                        sorted(issuer_scope_types[_issuer_id(ticker_candidates[0].normalized_cik)])
                    )
                ),
            )
            results.append(selected)
            created += selected_created

    return BootstrapResult(
        mode="apply",
        source_sha256=source_sha,
        source_observation_id=observation_id,
        selected_tickers=tuple(item.ticker for item in tracked),
        excluded_index_member_count=excluded_index_member_count,
        results=tuple(results),
        records_created=created,
    )


def bootstrap_sec_fund_registry(
    conn: sqlite3.Connection,
    *,
    raw_body: bytes,
    request: SecFundBootstrapRequest,
) -> SecFundBootstrapResult:
    """Resolve SEC fund series and share classes without collapsing registrants."""

    entries = parse_sec_mutual_fund_tickers(raw_body)
    tracked, excluded_index_member_count = _tracked_reporting_scope(conn)
    candidates: dict[str, list[SecMutualFundTickerEntry]] = defaultdict(list)
    tracked_by_ticker = {item.ticker: item for item in tracked}
    for entry in entries:
        if entry.symbol in tracked_by_ticker:
            candidates[entry.symbol].append(entry)
    targets = tuple(
        tracked_by_ticker[ticker] for ticker in sorted(candidates) if candidates[ticker]
    )
    registrants: dict[str, tuple[SecFundRegistrantEvidence, str]] = {}
    for evidence in request.registrants:
        if evidence.normalized_cik in registrants:
            raise ValueError("SEC fund registrant evidence repeats a CIK")
        legal_name = parse_sec_fund_registrant_identity(
            evidence.raw_body,
            normalized_cik=evidence.normalized_cik,
        )
        registrants[evidence.normalized_cik] = (evidence, legal_name)
    source_sha = hashlib.sha256(raw_body).hexdigest()

    if not request.apply:
        return SecFundBootstrapResult(
            mode="dry_run",
            source_sha256=source_sha,
            source_observation_id=None,
            selected_tickers=tuple(item.ticker for item in targets),
            excluded_index_member_count=excluded_index_member_count,
            results=tuple(
                _planned_fund_result(
                    item,
                    tuple(candidates[item.ticker]),
                    registrants=registrants,
                )
                for item in targets
            ),
            records_created=0,
        )

    issuer_scope: dict[str, Literal["core", "monitored"]] = {}
    issuer_scope_types: dict[str, set[str]] = defaultdict(set)
    for item in targets:
        ticker_candidates = tuple(candidates[item.ticker])
        if len(ticker_candidates) != 1 or item.inclusion_state == "historical":
            continue
        issuer_id = _issuer_id(ticker_candidates[0].normalized_cik)
        if issuer_id not in issuer_scope or item.inclusion_state == "core":
            issuer_scope[issuer_id] = item.inclusion_state
        issuer_scope_types[issuer_id].update(item.list_types)

    with conn:
        registry_observation_id, created, registry_recorded_at = _capture_source(
            conn,
            raw_body=raw_body,
            request=BootstrapRequest(
                source_url=request.source_url,
                blob_root=request.blob_root,
                apply=True,
                recorded_at=request.recorded_at,
            ),
            source_sha=source_sha,
            source_kind="sec_mutual_fund_tickers",
            collector_version=_FUND_COLLECTOR_VERSION,
        )
        captured_registrants: dict[str, tuple[str, str, str, datetime]] = {}
        for normalized_cik in sorted(registrants):
            evidence, legal_name = registrants[normalized_cik]
            registrant_sha = hashlib.sha256(evidence.raw_body).hexdigest()
            observation_id, evidence_created, recorded_at = _capture_source(
                conn,
                raw_body=evidence.raw_body,
                request=BootstrapRequest(
                    source_url=evidence.source_url,
                    blob_root=request.blob_root,
                    apply=True,
                    recorded_at=request.recorded_at,
                ),
                source_sha=registrant_sha,
                source_kind="sec_submissions",
                collector_version=_FUND_COLLECTOR_VERSION,
            )
            created += evidence_created
            captured_registrants[normalized_cik] = (
                observation_id,
                registrant_sha,
                legal_name,
                recorded_at,
            )

        issuer_registry = IssuerRegistry(conn)
        reporting_registry = ReportingEntityRegistry(conn)
        results: list[SecFundTickerResult] = []
        for item in targets:
            ticker_candidates = tuple(candidates[item.ticker])
            planned = _planned_fund_result(
                item,
                ticker_candidates,
                registrants=registrants,
            )
            if planned.outcome != "selected":
                results.append(planned)
                continue
            entry = ticker_candidates[0]
            captured = captured_registrants[entry.normalized_cik]
            item_created = _persist_selected_fund_ticker(
                conn,
                issuer_registry=issuer_registry,
                reporting_registry=reporting_registry,
                item=item,
                entry=entry,
                legal_name=captured[2],
                registry_observation_id=registry_observation_id,
                registry_source_sha=source_sha,
                registrant_observation_id=captured[0],
                registrant_source_sha=captured[1],
                registry_recorded_at=registry_recorded_at,
                registrant_recorded_at=captured[3],
                scope_inclusion_state=(
                    None
                    if item.inclusion_state == "historical"
                    else issuer_scope[_issuer_id(entry.normalized_cik)]
                ),
                scope_list_types=(
                    ()
                    if item.inclusion_state == "historical"
                    else tuple(sorted(issuer_scope_types[_issuer_id(entry.normalized_cik)]))
                ),
            )
            created += item_created
            results.append(
                planned.model_copy(
                    update={
                        "records_created": item_created,
                    }
                )
            )

    return SecFundBootstrapResult(
        mode="apply",
        source_sha256=source_sha,
        source_observation_id=registry_observation_id,
        selected_tickers=tuple(item.ticker for item in targets),
        excluded_index_member_count=excluded_index_member_count,
        results=tuple(results),
        records_created=created,
    )


def bootstrap_sec_historical_issuer(
    conn: sqlite3.Connection,
    *,
    request: SecHistoricalIssuerRequest,
) -> SecHistoricalIssuerResult:
    """Resolve a delisted SEC registrant while ending future source duties."""

    identity = parse_sec_historical_issuer_identity(
        request.raw_body,
        normalized_cik=request.normalized_cik,
        ticker=request.ticker,
    )
    source_sha = hashlib.sha256(request.raw_body).hexdigest()
    issuer_id = _issuer_id(identity.normalized_cik)
    if not request.apply:
        return SecHistoricalIssuerResult(
            mode="dry_run",
            ticker=request.ticker,
            normalized_cik=identity.normalized_cik,
            canonical_issuer_id=issuer_id,
            legal_name=identity.legal_name,
            termination_form=identity.termination_form,
            termination_date=identity.termination_date,
            source_sha256=source_sha,
            source_observation_id=None,
            records_created=0,
        )

    with conn:
        observation_id, created, recorded_at = _capture_source(
            conn,
            raw_body=request.raw_body,
            request=BootstrapRequest(
                source_url=request.source_url,
                blob_root=request.blob_root,
                apply=True,
                recorded_at=request.recorded_at,
            ),
            source_sha=source_sha,
            source_kind="sec_submissions",
            collector_version="sec-historical-issuer-bootstrap@1",
        )
        issuer_registry = IssuerRegistry(conn)
        reporting_registry = ReportingEntityRegistry(conn)
        termination_at = datetime.combine(
            identity.termination_date,
            datetime.min.time(),
            tzinfo=UTC,
        )
        created += _persist_entity(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            recorded_at=recorded_at,
        )
        created += _persist_profile(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            legal_name=identity.legal_name,
            observation_id=observation_id,
            source_sha=source_sha,
            recorded_at=recorded_at,
            filing_regime="SEC",
            reason_code="sec_form15_reporting_termination",
            status="inactive",
            effective_at=termination_at,
        )
        assertion_id = _record_id(
            "identifier-assertion",
            issuer_id,
            identity.normalized_cik,
            observation_id,
        )
        assertion = IdentifierAssertion(
            assertion_id=assertion_id,
            idempotency_key=assertion_id,
            issuer_id=issuer_id,
            identifier_type="sec_cik",
            identifier_value=identity.normalized_cik,
            normalized_value=identity.normalized_cik,
            authority="sec_registry",
            source_observation_id=observation_id,
            effective_at=recorded_at,
            knowledge_at=recorded_at,
            recorded_at=recorded_at,
        )
        created += int(issuer_registry.persist(assertion).created)
        created += _persist_identifier_resolution(
            conn,
            issuer_registry,
            assertion=assertion,
            recorded_at=recorded_at,
        )
        created += _persist_sec_surface(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            normalized_cik=identity.normalized_cik,
            observation_id=observation_id,
            source_sha=source_sha,
            recorded_at=recorded_at,
            verification_method="sec_form15_identity_contract",
        )
        reporting_entity_id = f"reporting:sec:{identity.normalized_cik}"
        created += _persist_reporting_entity(
            conn,
            reporting_registry,
            reporting_entity_id=reporting_entity_id,
            issuer_id=issuer_id,
            reporting_entity_kind="legal_registrant",
            display_name=identity.legal_name,
            recorded_at=recorded_at,
        )
        created += _persist_reporting_identifier_assertion(
            conn,
            reporting_registry,
            reporting_entity_id=reporting_entity_id,
            identifier_type="sec_cik",
            identifier_value=identity.normalized_cik,
            authority="sec_registry",
            source_observation_id=observation_id,
            recorded_at=recorded_at,
        )
        created += _persist_binding(
            conn,
            issuer_registry,
            ticker=request.ticker,
            issuer_id=issuer_id,
            outcome="selected",
            reason_code="sec_form15_historical_identity_selected",
            reason_details=(
                ("termination_form", identity.termination_form),
                ("termination_accession", identity.termination_accession),
                ("termination_date", identity.termination_date.isoformat()),
                ("source_observation_id", observation_id),
            ),
            material_dissent=False,
            source_sha=source_sha,
            recorded_at=recorded_at,
        )
        created += _persist_subject_binding(
            conn,
            reporting_registry,
            ticker=request.ticker,
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            security_id=None,
            registry_observation_id=observation_id,
            registry_source_sha=source_sha,
            recorded_at=recorded_at,
            reason_code="sec_form15_historical_identity_selected",
            reason_details=(
                ("termination_form", identity.termination_form),
                ("termination_accession", identity.termination_accession),
                ("termination_date", identity.termination_date.isoformat()),
                ("source_observation_id", observation_id),
            ),
        )
        created += _persist_historical_reporting_scope(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            termination_at=termination_at,
            source_observation_id=observation_id,
            recorded_at=recorded_at,
        )
    return SecHistoricalIssuerResult(
        mode="apply",
        ticker=request.ticker,
        normalized_cik=identity.normalized_cik,
        canonical_issuer_id=issuer_id,
        legal_name=identity.legal_name,
        termination_form=identity.termination_form,
        termination_date=identity.termination_date,
        source_sha256=source_sha,
        source_observation_id=observation_id,
        records_created=created,
    )


def bootstrap_sec_former_ticker(
    conn: sqlite3.Connection,
    *,
    request: SecFormerTickerRequest,
) -> SecFormerTickerResult:
    """Resolve historical evidence after an SEC registrant changes ticker."""

    identity = parse_sec_former_ticker_identity(
        request.submissions_raw_body,
        request.transition_raw_body,
        normalized_cik=request.normalized_cik,
        former_ticker=request.former_ticker,
        successor_ticker=request.successor_ticker,
        transition_date=request.transition_date,
        transition_source_url=request.transition_source_url,
    )
    submissions_sha = hashlib.sha256(request.submissions_raw_body).hexdigest()
    transition_sha = hashlib.sha256(request.transition_raw_body).hexdigest()
    bundle_sha = _digest(
        "sec-former-ticker-authority-bundle",
        f"{submissions_sha}\0{transition_sha}",
    )
    issuer_id = _issuer_id(identity.normalized_cik)
    if not request.apply:
        return SecFormerTickerResult(
            mode="dry_run",
            former_ticker=identity.former_ticker,
            successor_ticker=identity.successor_ticker,
            normalized_cik=identity.normalized_cik,
            canonical_issuer_id=issuer_id,
            legal_name=identity.legal_name,
            transition_date=identity.transition_date,
            transition_accession=identity.transition_accession,
            submissions_sha256=submissions_sha,
            transition_sha256=transition_sha,
            submissions_observation_id=None,
            transition_observation_id=None,
            records_created=0,
        )

    with conn:
        submissions_observation_id, created, submissions_recorded_at = _capture_source(
            conn,
            raw_body=request.submissions_raw_body,
            request=BootstrapRequest(
                source_url=request.submissions_source_url,
                blob_root=request.blob_root,
                apply=True,
                recorded_at=request.recorded_at,
            ),
            source_sha=submissions_sha,
            source_kind="sec_submissions",
            collector_version="sec-former-ticker-bootstrap@1",
        )
        transition_observation_id, transition_created, transition_recorded_at = _capture_source(
            conn,
            raw_body=request.transition_raw_body,
            request=BootstrapRequest(
                source_url=request.transition_source_url,
                blob_root=request.blob_root,
                apply=True,
                recorded_at=request.recorded_at,
            ),
            source_sha=transition_sha,
            source_kind="sec_filing",
            collector_version="sec-former-ticker-bootstrap@1",
            media_type="text/html",
            accept="text/html",
        )
        created += transition_created
        recorded_at = max(submissions_recorded_at, transition_recorded_at)
        transition_at = datetime.combine(
            identity.transition_date,
            datetime.min.time(),
            tzinfo=UTC,
        )
        issuer_registry = IssuerRegistry(conn)
        reporting_registry = ReportingEntityRegistry(conn)
        created += _persist_entity(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            recorded_at=recorded_at,
        )
        created += _persist_profile(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            legal_name=identity.legal_name,
            observation_id=submissions_observation_id,
            source_sha=submissions_sha,
            recorded_at=recorded_at,
            filing_regime="SEC",
            reason_code="sec_current_registrant_after_ticker_transition",
            status="active",
            effective_at=recorded_at,
        )
        assertion_id = _record_id(
            "identifier-assertion",
            issuer_id,
            identity.normalized_cik,
            submissions_observation_id,
        )
        assertion = IdentifierAssertion(
            assertion_id=assertion_id,
            idempotency_key=assertion_id,
            issuer_id=issuer_id,
            identifier_type="sec_cik",
            identifier_value=identity.normalized_cik,
            normalized_value=identity.normalized_cik,
            authority="sec_registry",
            source_observation_id=submissions_observation_id,
            effective_at=recorded_at,
            knowledge_at=recorded_at,
            recorded_at=recorded_at,
        )
        created += int(issuer_registry.persist(assertion).created)
        created += _persist_identifier_resolution(
            conn,
            issuer_registry,
            assertion=assertion,
            recorded_at=recorded_at,
        )
        created += _persist_sec_surface(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            normalized_cik=identity.normalized_cik,
            observation_id=submissions_observation_id,
            source_sha=submissions_sha,
            recorded_at=recorded_at,
            verification_method="sec_former_ticker_transition_contract",
        )
        reporting_entity_id = f"reporting:sec:{identity.normalized_cik}"
        created += _persist_reporting_entity(
            conn,
            reporting_registry,
            reporting_entity_id=reporting_entity_id,
            issuer_id=issuer_id,
            reporting_entity_kind="legal_registrant",
            display_name=identity.legal_name,
            recorded_at=recorded_at,
        )
        created += _persist_reporting_identifier_assertion(
            conn,
            reporting_registry,
            reporting_entity_id=reporting_entity_id,
            identifier_type="sec_cik",
            identifier_value=identity.normalized_cik,
            authority="sec_registry",
            source_observation_id=submissions_observation_id,
            recorded_at=recorded_at,
        )
        reason_details = (
            ("successor_ticker", identity.successor_ticker),
            ("transition_accession", identity.transition_accession),
            ("transition_date", identity.transition_date.isoformat()),
            ("submissions_observation_id", submissions_observation_id),
            ("transition_observation_id", transition_observation_id),
        )
        created += _persist_binding(
            conn,
            issuer_registry,
            ticker=identity.former_ticker,
            issuer_id=issuer_id,
            outcome="selected",
            reason_code="sec_former_ticker_transition_selected",
            reason_details=reason_details,
            material_dissent=False,
            source_sha=bundle_sha,
            recorded_at=recorded_at,
        )
        created += _persist_subject_binding(
            conn,
            reporting_registry,
            ticker=identity.former_ticker,
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            security_id=None,
            registry_observation_id=transition_observation_id,
            registry_source_sha=bundle_sha,
            recorded_at=recorded_at,
            reason_code="sec_former_ticker_transition_selected",
            reason_details=reason_details,
        )
        created += _persist_retired_ticker_reporting_scope(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            transition_at=transition_at,
            successor_ticker=identity.successor_ticker,
            source_observation_id=transition_observation_id,
            recorded_at=recorded_at,
        )
    return SecFormerTickerResult(
        mode="apply",
        former_ticker=identity.former_ticker,
        successor_ticker=identity.successor_ticker,
        normalized_cik=identity.normalized_cik,
        canonical_issuer_id=issuer_id,
        legal_name=identity.legal_name,
        transition_date=identity.transition_date,
        transition_accession=identity.transition_accession,
        submissions_sha256=submissions_sha,
        transition_sha256=transition_sha,
        submissions_observation_id=submissions_observation_id,
        transition_observation_id=transition_observation_id,
        records_created=created,
    )


def _planned_fund_result(
    item: _TrackedTicker,
    candidates: tuple[SecMutualFundTickerEntry, ...],
    *,
    registrants: dict[str, tuple[SecFundRegistrantEvidence, str]],
) -> SecFundTickerResult:
    if len(candidates) != 1:
        return SecFundTickerResult(
            ticker=item.ticker,
            outcome=("unresolved_missing" if not candidates else "unresolved_duplicate"),
            inclusion_state=item.inclusion_state,
            candidate_count=len(candidates),
            records_created=0,
            reason_code=(
                "sec_fund_ticker_missing" if not candidates else "duplicate_sec_fund_ticker"
            ),
        )
    entry = candidates[0]
    if entry.normalized_cik not in registrants:
        return SecFundTickerResult(
            ticker=item.ticker,
            outcome="missing_registrant",
            inclusion_state=item.inclusion_state,
            normalized_cik=entry.normalized_cik,
            sec_series_id=entry.series_id,
            sec_class_contract_id=entry.class_id,
            candidate_count=1,
            records_created=0,
            reason_code="sec_fund_registrant_evidence_missing",
        )
    return SecFundTickerResult(
        ticker=item.ticker,
        outcome="selected",
        inclusion_state=item.inclusion_state,
        canonical_issuer_id=_issuer_id(entry.normalized_cik),
        normalized_cik=entry.normalized_cik,
        sec_series_id=entry.series_id,
        sec_class_contract_id=entry.class_id,
        candidate_count=1,
        records_created=0,
        reason_code="unique_sec_series_class_selected",
    )


def _planned_result(
    item: _TrackedTicker,
    candidates: tuple[SecCompanyTickerEntry, ...],
) -> BootstrapTickerResult:
    if len(candidates) == 1:
        entry = candidates[0]
        return BootstrapTickerResult(
            ticker=item.ticker,
            outcome="selected",
            inclusion_state=item.inclusion_state,
            canonical_issuer_id=_issuer_id(entry.normalized_cik),
            normalized_cik=entry.normalized_cik,
            candidate_count=1,
            records_created=0,
            reason_code="unique_sec_ticker_selected",
        )
    outcome: Literal["unresolved_duplicate", "unresolved_missing"]
    outcome = "unresolved_missing" if not candidates else "unresolved_duplicate"
    return BootstrapTickerResult(
        ticker=item.ticker,
        outcome=outcome,
        inclusion_state=item.inclusion_state,
        candidate_count=len(candidates),
        records_created=0,
        reason_code="sec_ticker_missing" if not candidates else "duplicate_sec_ticker",
    )


def _tracked_reporting_scope(
    conn: sqlite3.Connection,
) -> tuple[tuple[_TrackedTicker, ...], int]:
    rows = conn.execute(
        """
        SELECT ticker, list_type
        FROM tracked_companies
        WHERE archived_at IS NULL
        ORDER BY upper(trim(ticker)), list_type
        """
    ).fetchall()
    active: dict[str, set[str]] = defaultdict(set)
    excluded_index_members: set[str] = set()
    for row in rows:
        ticker = str(row[0]).strip().upper()
        list_type = str(row[1]).strip().lower()
        if not ticker:
            continue
        if list_type in _ACTIVE_LIST_TYPES:
            active[ticker].add(list_type)
        elif list_type == "index_member":
            excluded_index_members.add(ticker)
    historical_tickers: set[str] = set()
    evidence_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence_document_versions'"
    ).fetchone()
    if evidence_table is not None:
        for row in conn.execute(
            "SELECT DISTINCT issuer_id FROM evidence_document_versions "
            "WHERE issuer_id LIKE 'legacy-ticker:%'"
        ):
            recorded_issuer_id = str(row[0]).strip()
            ticker = recorded_issuer_id[len(_LEGACY_TICKER_PREFIX) :].strip().upper()
            if ticker and ticker not in active:
                historical_tickers.add(ticker)
    tracked = tuple(
        _TrackedTicker(
            ticker=ticker,
            inclusion_state=(
                "historical"
                if ticker in historical_tickers
                else "core"
                if "portfolio" in list_types
                else "monitored"
            ),
            list_types=(
                ("historical_evidence",)
                if ticker in historical_tickers
                else tuple(sorted(list_types))
            ),
        )
        for ticker, list_types in sorted(
            {
                **active,
                **{ticker: {"historical_evidence"} for ticker in historical_tickers},
            }.items()
        )
    )
    return tracked, len(excluded_index_members - set(active))


def _capture_source(
    conn: sqlite3.Connection,
    *,
    raw_body: bytes,
    request: BootstrapRequest,
    source_sha: str,
    source_kind: str = "sec_company_tickers",
    collector_version: str = _COLLECTOR_VERSION,
    media_type: str = "application/json",
    accept: str = "application/json",
) -> tuple[str, int, datetime]:
    config_sha = _digest(
        "retrieval-config",
        json.dumps(
            {"accept": accept, "source_url": request.source_url},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    observation_id = _record_id("source-observation", request.source_url, source_sha, config_sha)
    existing_observation = conn.execute(
        "SELECT observed_at FROM evidence_source_observations WHERE idempotency_key = ?",
        (observation_id,),
    ).fetchone()
    recorded_at = (
        request.recorded_at
        if existing_observation is None
        else _parse_datetime(existing_observation[0])
    )
    path = request.blob_root / source_sha[:2] / source_sha
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != source_sha:
            raise RuntimeError("existing SEC authority blob fails hash verification")
    else:
        path.write_bytes(raw_body)

    ledger = EvidenceLedger(conn)
    created = 0
    existing_blob = conn.execute(
        "SELECT byte_size FROM evidence_content_blobs WHERE sha256 = ?",
        (source_sha,),
    ).fetchone()
    if existing_blob is None:
        created += int(
            ledger.persist(
                ContentBlob(
                    sha256=source_sha,
                    byte_size=len(raw_body),
                    media_type=media_type,
                    storage_uri=path.resolve().as_uri(),
                    recorded_at=recorded_at,
                )
            ).created
        )
    elif int(existing_blob[0]) != len(raw_body):
        raise ValueError("existing SEC authority evidence blob metadata conflicts")
    storage_uri = path.resolve().as_uri()
    location_id = _record_id("blob-location", source_sha, storage_uri)
    created += int(
        EvidenceLinkLedger(conn)
        .persist_location(
            BlobLocationObservation(
                location_observation_id=location_id,
                idempotency_key=location_id,
                blob_sha256=source_sha,
                storage_uri=storage_uri,
                location_kind="local",
                availability_state="present",
                location_sequence=1,
                verified_at=recorded_at,
                verified_byte_size=len(raw_body),
                verified_sha256=source_sha,
                recorded_at=recorded_at,
            )
        )
        .created
    )
    if existing_observation is None:
        created += int(
            ledger.persist(
                SourceObservation(
                    observation_id=observation_id,
                    idempotency_key=observation_id,
                    source_kind=source_kind,
                    source_url=request.source_url,
                    blob_sha256=source_sha,
                    source_published_at=None,
                    filing_at=None,
                    accepted_at=None,
                    observed_at=recorded_at,
                    retrieved_at=recorded_at,
                    retrieval_config_sha256=config_sha,
                    collector_code_version=collector_version,
                )
            ).created
        )
    return observation_id, created, recorded_at


def _persist_selected_ticker(
    conn: sqlite3.Connection,
    *,
    registry: IssuerRegistry,
    item: _TrackedTicker,
    entry: SecCompanyTickerEntry,
    observation_id: str,
    source_sha: str,
    recorded_at: datetime,
    scope_inclusion_state: Literal["core", "monitored"] | None,
    scope_list_types: tuple[str, ...],
) -> tuple[BootstrapTickerResult, int]:
    normalized_cik = entry.normalized_cik
    issuer_id = _issuer_id(normalized_cik)
    created = 0
    created += _persist_entity(
        conn,
        registry,
        issuer_id=issuer_id,
        recorded_at=recorded_at,
    )
    created += _persist_profile(
        conn,
        registry,
        issuer_id=issuer_id,
        legal_name=entry.title,
        observation_id=observation_id,
        source_sha=source_sha,
        recorded_at=recorded_at,
    )
    assertion_id = _record_id("identifier-assertion", issuer_id, normalized_cik, observation_id)
    assertion = IdentifierAssertion(
        assertion_id=assertion_id,
        idempotency_key=assertion_id,
        issuer_id=issuer_id,
        identifier_type="sec_cik",
        identifier_value=str(entry.cik_str),
        normalized_value=normalized_cik,
        authority="sec_registry",
        source_observation_id=observation_id,
        effective_at=recorded_at,
        knowledge_at=recorded_at,
        recorded_at=recorded_at,
    )
    created += int(registry.persist(assertion).created)
    created += _persist_identifier_resolution(
        conn,
        registry,
        assertion=assertion,
        recorded_at=recorded_at,
    )
    recorded_sec_cik = f"sec-cik-{normalized_cik}"
    has_recorded_sec_evidence = conn.execute(
        "SELECT 1 FROM evidence_document_versions WHERE issuer_id = ? LIMIT 1",
        (recorded_sec_cik,),
    ).fetchone()
    if has_recorded_sec_evidence is not None:
        created += ensure_sec_cik_evidence_binding(
            conn,
            recorded_issuer_id=recorded_sec_cik,
            recorded_at=recorded_at,
        )
    created += _persist_reporting_boundary(
        conn,
        issuer_id=issuer_id,
        legal_name=entry.title,
        normalized_cik=normalized_cik,
        observation_id=observation_id,
        recorded_at=recorded_at,
        active_scope=scope_inclusion_state is not None,
    )
    created += _persist_sec_surface(
        conn,
        registry,
        issuer_id=issuer_id,
        normalized_cik=normalized_cik,
        observation_id=observation_id,
        source_sha=source_sha,
        recorded_at=recorded_at,
    )
    created += _persist_binding(
        conn,
        registry,
        ticker=item.ticker,
        issuer_id=issuer_id,
        outcome="selected",
        reason_code="unique_sec_ticker_selected",
        reason_details=(
            ("candidate_count", "1"),
            ("normalized_cik", normalized_cik),
        ),
        material_dissent=False,
        source_sha=source_sha,
        recorded_at=recorded_at,
    )
    if scope_inclusion_state is not None:
        created += _persist_scope(
            conn,
            registry,
            issuer_id=issuer_id,
            inclusion_state=scope_inclusion_state,
            scope_list_types=scope_list_types,
            recorded_at=recorded_at,
        )
    return (
        BootstrapTickerResult(
            ticker=item.ticker,
            outcome="selected",
            inclusion_state=item.inclusion_state,
            canonical_issuer_id=issuer_id,
            normalized_cik=normalized_cik,
            candidate_count=1,
            records_created=created,
            reason_code="unique_sec_ticker_selected",
        ),
        created,
    )


def _persist_reporting_boundary(
    conn: sqlite3.Connection,
    *,
    issuer_id: str,
    legal_name: str,
    normalized_cik: str,
    observation_id: str,
    recorded_at: datetime,
    active_scope: bool,
) -> int:
    schema_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reporting_entities'"
    ).fetchone()
    if schema_present is None:
        return 0
    registry = ReportingEntityRegistry(conn)
    reporting_entity_id = f"reporting:sec:{normalized_cik}"
    created = _persist_reporting_entity(
        conn,
        registry,
        reporting_entity_id=reporting_entity_id,
        issuer_id=issuer_id,
        reporting_entity_kind="legal_registrant",
        display_name=legal_name,
        recorded_at=recorded_at,
    )
    created += _persist_reporting_identifier_assertion(
        conn,
        registry,
        reporting_entity_id=reporting_entity_id,
        identifier_type="sec_cik",
        identifier_value=normalized_cik,
        authority="sec_registry",
        source_observation_id=observation_id,
        recorded_at=recorded_at,
    )
    if not active_scope:
        return created
    obligations = (
        (
            "sec_edgar",
            "operating_company_periodic",
            "regulator_inventory",
        ),
        (
            "sec_edgar",
            "continuous_disclosure",
            "regulator_inventory",
        ),
        (
            "issuer_publisher",
            "issuer_financial_statements",
            "publisher_surface_exhaustion",
        ),
        (
            "issuer_publisher",
            "issuer_presentations",
            "publisher_surface_exhaustion",
        ),
        (
            "issuer_publisher",
            "issuer_earnings_materials",
            "publisher_surface_exhaustion",
        ),
    )
    for authority_kind, document_family, completeness_rule in obligations:
        created += _persist_source_obligation(
            conn,
            registry,
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            authority_kind=cast(
                Literal["sec_edgar", "sedar_plus", "edinet", "issuer_publisher"],
                authority_kind,
            ),
            document_family=cast(
                Literal[
                    "operating_company_periodic",
                    "investment_company_periodic",
                    "continuous_disclosure",
                    "annual_securities_report",
                    "issuer_financial_statements",
                    "issuer_presentations",
                    "issuer_earnings_materials",
                ],
                document_family,
            ),
            obligation_state="required",
            completeness_rule=cast(
                Literal[
                    "regulator_inventory",
                    "publisher_surface_exhaustion",
                    "manual_exception",
                ],
                completeness_rule,
            ),
            source_observation_id=observation_id,
            recorded_at=recorded_at,
            reason_code="active_operating_company_scope",
        )
    return created


def _persist_selected_fund_ticker(
    conn: sqlite3.Connection,
    *,
    issuer_registry: IssuerRegistry,
    reporting_registry: ReportingEntityRegistry,
    item: _TrackedTicker,
    entry: SecMutualFundTickerEntry,
    legal_name: str,
    registry_observation_id: str,
    registry_source_sha: str,
    registrant_observation_id: str,
    registrant_source_sha: str,
    registry_recorded_at: datetime,
    registrant_recorded_at: datetime,
    scope_inclusion_state: Literal["core", "monitored"] | None,
    scope_list_types: tuple[str, ...],
) -> int:
    recorded_at = max(registry_recorded_at, registrant_recorded_at)
    issuer_id = _issuer_id(entry.normalized_cik)
    legal_reporting_id = f"reporting:sec:{entry.normalized_cik}"
    series_reporting_id = f"reporting:sec-series:{entry.series_id}"
    security_id = f"security:sec-class:{entry.class_id}"
    created = _persist_entity(
        conn,
        issuer_registry,
        issuer_id=issuer_id,
        recorded_at=recorded_at,
        entity_kind="fund",
    )
    created += _persist_profile(
        conn,
        issuer_registry,
        issuer_id=issuer_id,
        legal_name=legal_name,
        observation_id=registrant_observation_id,
        source_sha=registrant_source_sha,
        recorded_at=registrant_recorded_at,
        filing_regime="SEC Investment Company",
        reason_code="sec_submissions_fund_registrant_import",
    )
    issuer_assertion_id = _record_id(
        "identifier-assertion",
        issuer_id,
        entry.normalized_cik,
        registrant_observation_id,
    )
    issuer_assertion = IdentifierAssertion(
        assertion_id=issuer_assertion_id,
        idempotency_key=issuer_assertion_id,
        issuer_id=issuer_id,
        identifier_type="sec_cik",
        identifier_value=str(entry.cik),
        normalized_value=entry.normalized_cik,
        authority="sec_registry",
        source_observation_id=registrant_observation_id,
        effective_at=registrant_recorded_at,
        knowledge_at=registrant_recorded_at,
        recorded_at=registrant_recorded_at,
    )
    created += int(issuer_registry.persist(issuer_assertion).created)
    created += _persist_identifier_resolution(
        conn,
        issuer_registry,
        assertion=issuer_assertion,
        recorded_at=registrant_recorded_at,
    )
    created += _persist_sec_surface(
        conn,
        issuer_registry,
        issuer_id=issuer_id,
        normalized_cik=entry.normalized_cik,
        observation_id=registrant_observation_id,
        source_sha=registrant_source_sha,
        recorded_at=registrant_recorded_at,
        verification_method="sec_submissions_identity_contract",
    )
    created += _persist_reporting_entity(
        conn,
        reporting_registry,
        reporting_entity_id=legal_reporting_id,
        issuer_id=issuer_id,
        reporting_entity_kind="legal_registrant",
        display_name=legal_name,
        recorded_at=registrant_recorded_at,
    )
    created += _persist_reporting_identifier_assertion(
        conn,
        reporting_registry,
        reporting_entity_id=legal_reporting_id,
        identifier_type="sec_cik",
        identifier_value=entry.normalized_cik,
        authority="sec_registry",
        source_observation_id=registrant_observation_id,
        recorded_at=registrant_recorded_at,
    )
    created += _persist_reporting_entity(
        conn,
        reporting_registry,
        reporting_entity_id=series_reporting_id,
        issuer_id=issuer_id,
        reporting_entity_kind="fund_series",
        display_name=f"SEC fund series {entry.series_id} ({item.ticker})",
        recorded_at=recorded_at,
    )
    created += _persist_reporting_identifier_assertion(
        conn,
        reporting_registry,
        reporting_entity_id=series_reporting_id,
        identifier_type="sec_series_id",
        identifier_value=entry.series_id,
        authority="sec_registry",
        source_observation_id=registry_observation_id,
        recorded_at=registry_recorded_at,
    )
    created += _persist_security(
        conn,
        issuer_registry,
        security_id=security_id,
        issuer_id=issuer_id,
        security_kind="fund_share",
        share_class=entry.class_id,
        recorded_at=registry_recorded_at,
    )
    created += _persist_security_identifier_assertion(
        conn,
        reporting_registry,
        security_id=security_id,
        identifier_value=entry.class_id,
        source_observation_id=registry_observation_id,
        recorded_at=registry_recorded_at,
    )
    created += _persist_security_reporting_relationship(
        conn,
        reporting_registry,
        security_id=security_id,
        reporting_entity_id=series_reporting_id,
        source_observation_id=registry_observation_id,
        recorded_at=registry_recorded_at,
    )
    created += _persist_binding(
        conn,
        issuer_registry,
        ticker=item.ticker,
        issuer_id=issuer_id,
        outcome="selected",
        reason_code="unique_sec_series_class_selected",
        reason_details=(
            ("sec_cik", entry.normalized_cik),
            ("sec_series_id", entry.series_id),
            ("sec_class_contract_id", entry.class_id),
            ("source_observation_id", registry_observation_id),
        ),
        material_dissent=False,
        source_sha=registry_source_sha,
        recorded_at=recorded_at,
    )
    created += _persist_subject_binding(
        conn,
        reporting_registry,
        ticker=item.ticker,
        issuer_id=issuer_id,
        reporting_entity_id=series_reporting_id,
        security_id=security_id,
        registry_observation_id=registry_observation_id,
        registry_source_sha=registry_source_sha,
        recorded_at=recorded_at,
    )
    if scope_inclusion_state is not None:
        created += _persist_scope(
            conn,
            issuer_registry,
            issuer_id=issuer_id,
            inclusion_state=scope_inclusion_state,
            scope_list_types=scope_list_types,
            recorded_at=recorded_at,
            require_sec=True,
            require_ir=True,
            require_earnings=False,
            reason_code="registered_fund_reporting_scope",
        )
        created += _persist_fund_source_obligations(
            conn,
            reporting_registry,
            issuer_id=issuer_id,
            reporting_entity_id=series_reporting_id,
            source_observation_id=registry_observation_id,
            recorded_at=recorded_at,
        )
    return created


def _persist_reporting_identifier_assertion(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    reporting_entity_id: str,
    identifier_type: Literal[
        "sec_cik",
        "sec_series_id",
        "sedar_profile",
        "edinet_code",
        "lei",
    ],
    identifier_value: str,
    authority: Literal[
        "issuer_publisher",
        "sec_registry",
        "exchange_registry",
        "regulator",
        "manual",
        "imported",
    ],
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    assertion_id = _record_id(
        "reporting-identifier-assertion",
        reporting_entity_id,
        identifier_type,
        identifier_value,
        source_observation_id,
    )
    assertion = ReportingEntityIdentifierAssertion(
        assertion_id=assertion_id,
        idempotency_key=assertion_id,
        reporting_entity_id=reporting_entity_id,
        identifier_type=identifier_type,
        identifier_value=identifier_value,
        normalized_value=identifier_value.strip()
        .upper()
        .zfill(10 if identifier_type == "sec_cik" else 0),
        authority=authority,
        source_observation_id=source_observation_id,
        effective_at=recorded_at,
        knowledge_at=recorded_at,
        recorded_at=recorded_at,
    )
    created = int(registry.persist(assertion).created)
    rows = conn.execute(
        "SELECT assertion_id, idempotency_key, reporting_entity_id, "
        "identifier_value, normalized_value, authority, source_observation_id, "
        "effective_at, knowledge_at, recorded_at "
        "FROM reporting_entity_identifier_assertions "
        "WHERE identifier_type = ? AND normalized_value = ? ORDER BY assertion_id",
        (assertion.identifier_type, assertion.normalized_value),
    ).fetchall()
    assertions = tuple(
        ReportingEntityIdentifierAssertion(
            assertion_id=str(row[0]),
            idempotency_key=str(row[1]),
            reporting_entity_id=str(row[2]),
            identifier_type=identifier_type,
            identifier_value=str(row[3]),
            normalized_value=str(row[4]),
            authority=cast(
                Literal[
                    "issuer_publisher",
                    "sec_registry",
                    "exchange_registry",
                    "regulator",
                    "manual",
                    "imported",
                ],
                str(row[5]),
            ),
            source_observation_id=None if row[6] is None else str(row[6]),
            effective_at=_parse_datetime(row[7]),
            knowledge_at=_parse_datetime(row[8]),
            recorded_at=_parse_datetime(row[9]),
        )
        for row in rows
    )
    candidate_digest = reporting_identifier_candidate_digest(assertions)
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM "
        "reporting_entity_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    selected = max(assertions, key=lambda item: (item.knowledge_at, item.assertion_id))
    if (
        current is not None
        and str(current[2]) == candidate_digest
        and str(current[3]) == selected.assertion_id
    ):
        return created
    revision = 1 if current is None else int(current[1]) + 1
    resolution_id = _record_id(
        "reporting-identifier-resolution",
        assertion.resolution_key,
        candidate_digest,
    )
    created += int(
        registry.persist(
            ReportingEntityIdentifierResolution(
                resolution_id=resolution_id,
                idempotency_key=resolution_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=selected.assertion_id,
                candidate_digest_sha256=candidate_digest,
                policy_name="regulator_exact_match",
                policy_version="1",
                policy_config_sha256=_digest(
                    "reporting-identifier-policy",
                    f"{identifier_type}:regulator_exact_match:1",
                ),
                reason_code="unique_regulator_identifier",
                reason_details=(
                    ("candidate_count", str(len(assertions))),
                    ("selected_assertion_id", selected.assertion_id),
                ),
                material_dissent=(len({item.reporting_entity_id for item in assertions}) > 1),
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )
    return created


def _persist_security_identifier_assertion(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    security_id: str,
    identifier_value: str,
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    assertion_id = _record_id(
        "security-identifier-assertion",
        security_id,
        "sec_class_contract_id",
        identifier_value,
        source_observation_id,
    )
    assertion = SecurityIdentifierAssertion(
        assertion_id=assertion_id,
        idempotency_key=assertion_id,
        security_id=security_id,
        identifier_type="sec_class_contract_id",
        identifier_value=identifier_value,
        normalized_value=identifier_value.strip().upper(),
        authority="sec_registry",
        source_observation_id=source_observation_id,
        effective_at=recorded_at,
        knowledge_at=recorded_at,
        recorded_at=recorded_at,
    )
    created = int(registry.persist(assertion).created)
    rows = conn.execute(
        "SELECT assertion_id, idempotency_key, security_id, identifier_value, "
        "normalized_value, authority, source_observation_id, effective_at, "
        "knowledge_at, recorded_at FROM security_identifier_assertions "
        "WHERE identifier_type = 'sec_class_contract_id' AND normalized_value = ? "
        "ORDER BY assertion_id",
        (assertion.normalized_value,),
    ).fetchall()
    assertions = tuple(
        SecurityIdentifierAssertion(
            assertion_id=str(row[0]),
            idempotency_key=str(row[1]),
            security_id=str(row[2]),
            identifier_type="sec_class_contract_id",
            identifier_value=str(row[3]),
            normalized_value=str(row[4]),
            authority=cast(
                Literal[
                    "issuer_publisher",
                    "sec_registry",
                    "exchange_registry",
                    "regulator",
                    "manual",
                    "imported",
                ],
                str(row[5]),
            ),
            source_observation_id=None if row[6] is None else str(row[6]),
            effective_at=_parse_datetime(row[7]),
            knowledge_at=_parse_datetime(row[8]),
            recorded_at=_parse_datetime(row[9]),
        )
        for row in rows
    )
    candidate_digest = security_identifier_candidate_digest(assertions)
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM security_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (assertion.resolution_key,),
    ).fetchone()
    selected = max(assertions, key=lambda item: (item.knowledge_at, item.assertion_id))
    if (
        current is not None
        and str(current[2]) == candidate_digest
        and str(current[3]) == selected.assertion_id
    ):
        return created
    revision = 1 if current is None else int(current[1]) + 1
    resolution_id = _record_id(
        "security-identifier-resolution",
        assertion.resolution_key,
        candidate_digest,
    )
    created += int(
        registry.persist(
            SecurityIdentifierResolution(
                resolution_id=resolution_id,
                idempotency_key=resolution_id,
                resolution_key=assertion.resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=selected.assertion_id,
                candidate_digest_sha256=candidate_digest,
                policy_name="regulator_exact_match",
                policy_version="1",
                policy_config_sha256=_digest(
                    "security-identifier-policy",
                    "sec_class_contract_id:regulator_exact_match:1",
                ),
                reason_code="unique_regulator_identifier",
                reason_details=(
                    ("candidate_count", str(len(assertions))),
                    ("selected_assertion_id", selected.assertion_id),
                ),
                material_dissent=len({item.security_id for item in assertions}) > 1,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )
    return created


def _persist_subject_binding(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    ticker: str,
    issuer_id: str,
    reporting_entity_id: str,
    security_id: str | None,
    registry_observation_id: str,
    registry_source_sha: str,
    recorded_at: datetime,
    reason_code: str = "sec_series_class_mapping",
    reason_details: tuple[tuple[str, str], ...] | None = None,
) -> int:
    recorded_issuer_id = f"legacy-ticker:{ticker}"
    current = conn.execute(
        "SELECT binding_revision_id, revision, issuer_id, reporting_entity_id, "
        "security_id, outcome FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    semantics = (issuer_id, reporting_entity_id, security_id, "selected")
    current_semantics = (
        None
        if current is None
        else (
            str(current[2]),
            str(current[3]),
            None if current[4] is None else str(current[4]),
            str(current[5]),
        )
    )
    if current_semantics == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "recorded-subject-binding",
        recorded_issuer_id,
        issuer_id,
        reporting_entity_id,
        security_id or "none",
        registry_source_sha,
        str(revision),
    )
    return int(
        registry.persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id=record_id,
                idempotency_key=record_id,
                recorded_issuer_id=recorded_issuer_id,
                revision=revision,
                issuer_id=issuer_id,
                reporting_entity_id=reporting_entity_id,
                security_id=security_id,
                outcome="selected",
                decision_kind="deterministic",
                reason_code=reason_code,
                reason_details=reason_details
                or (
                    ("source_observation_id", registry_observation_id),
                    ("source_sha256", registry_source_sha),
                ),
                material_dissent=False,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_binding_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _persist_source_obligation(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    issuer_id: str,
    reporting_entity_id: str,
    authority_kind: Literal[
        "sec_edgar",
        "sedar_plus",
        "edinet",
        "issuer_publisher",
    ],
    document_family: Literal[
        "operating_company_periodic",
        "investment_company_periodic",
        "continuous_disclosure",
        "annual_securities_report",
        "issuer_financial_statements",
        "issuer_presentations",
        "issuer_earnings_materials",
    ],
    obligation_state: Literal["required", "optional", "not_applicable"],
    completeness_rule: Literal[
        "regulator_inventory",
        "publisher_surface_exhaustion",
        "manual_exception",
    ],
    source_observation_id: str,
    recorded_at: datetime,
    reason_code: str,
) -> int:
    obligation_key = f"{reporting_entity_id}:{authority_kind}:{document_family}"
    current = conn.execute(
        "SELECT obligation_revision_id, revision, issuer_id, reporting_entity_id, "
        "authority_kind, document_family, obligation_state, completeness_rule, active_to "
        "FROM source_obligation_revisions WHERE obligation_key = ? "
        "ORDER BY revision DESC LIMIT 1",
        (obligation_key,),
    ).fetchone()
    expected = (
        issuer_id,
        reporting_entity_id,
        authority_kind,
        document_family,
        obligation_state,
        completeness_rule,
    )
    if (
        current is not None
        and tuple(str(value) for value in current[2:8]) == expected
        and current[8] is None
    ):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "source-obligation",
        obligation_key,
        source_observation_id,
        str(revision),
    )
    return int(
        registry.persist(
            SourceObligationRevision(
                obligation_revision_id=record_id,
                idempotency_key=record_id,
                obligation_key=obligation_key,
                revision=revision,
                issuer_id=issuer_id,
                reporting_entity_id=reporting_entity_id,
                authority_kind=authority_kind,
                document_family=document_family,
                obligation_state=obligation_state,
                completeness_rule=completeness_rule,
                active_from=recorded_at,
                active_to=None,
                decision_kind="deterministic",
                reason_code=reason_code,
                reason_details=(("source_observation_id", source_observation_id),),
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_obligation_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _persist_fund_source_obligations(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    issuer_id: str,
    reporting_entity_id: str,
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    obligations = (
        (
            "sec_edgar",
            "investment_company_periodic",
            "required",
            "regulator_inventory",
        ),
        (
            "issuer_publisher",
            "issuer_financial_statements",
            "required",
            "publisher_surface_exhaustion",
        ),
        (
            "issuer_publisher",
            "issuer_presentations",
            "optional",
            "publisher_surface_exhaustion",
        ),
        (
            "issuer_publisher",
            "issuer_earnings_materials",
            "not_applicable",
            "manual_exception",
        ),
    )
    created = 0
    for authority_kind, document_family, state, completeness_rule in obligations:
        created += _persist_source_obligation(
            conn,
            registry,
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            authority_kind=cast(
                Literal["sec_edgar", "sedar_plus", "edinet", "issuer_publisher"],
                authority_kind,
            ),
            document_family=cast(
                Literal[
                    "operating_company_periodic",
                    "investment_company_periodic",
                    "continuous_disclosure",
                    "annual_securities_report",
                    "issuer_financial_statements",
                    "issuer_presentations",
                    "issuer_earnings_materials",
                ],
                document_family,
            ),
            obligation_state=cast(
                Literal["required", "optional", "not_applicable"],
                state,
            ),
            completeness_rule=cast(
                Literal[
                    "regulator_inventory",
                    "publisher_surface_exhaustion",
                    "manual_exception",
                ],
                completeness_rule,
            ),
            source_observation_id=source_observation_id,
            recorded_at=recorded_at,
            reason_code="registered_investment_company_series",
        )
    return created


def _persist_unresolved_binding(
    conn: sqlite3.Connection,
    *,
    registry: IssuerRegistry,
    item: _TrackedTicker,
    candidate_count: int,
    source_sha: str,
    recorded_at: datetime,
) -> tuple[BootstrapTickerResult, int]:
    reason_code = "sec_ticker_missing" if candidate_count == 0 else "duplicate_sec_ticker"
    created = _persist_binding(
        conn,
        registry,
        ticker=item.ticker,
        issuer_id=None,
        outcome="unresolved",
        reason_code=reason_code,
        reason_details=(
            ("candidate_count", str(candidate_count)),
            ("source_sha256", source_sha),
        ),
        material_dissent=candidate_count > 1,
        source_sha=source_sha,
        recorded_at=recorded_at,
    )
    outcome: Literal["unresolved_duplicate", "unresolved_missing"]
    outcome = "unresolved_missing" if candidate_count == 0 else "unresolved_duplicate"
    return (
        BootstrapTickerResult(
            ticker=item.ticker,
            outcome=outcome,
            inclusion_state=item.inclusion_state,
            candidate_count=candidate_count,
            records_created=created,
            reason_code=reason_code,
        ),
        created,
    )


def _persist_entity(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    recorded_at: datetime,
    entity_kind: Literal["operating_company", "fund", "partnership", "other"] = (
        "operating_company"
    ),
) -> int:
    existing = conn.execute(
        "SELECT entity_kind FROM issuer_entities WHERE issuer_id = ?",
        (issuer_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != entity_kind:
            raise ValueError("deterministic SEC issuer ID conflicts with entity kind")
        return 0
    return int(
        registry.persist(
            IssuerEntity(
                issuer_id=issuer_id,
                idempotency_key=f"issuer-entity:{issuer_id}",
                entity_kind=entity_kind,
                created_at=recorded_at,
            )
        ).created
    )


def _persist_reporting_entity(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    reporting_entity_id: str,
    issuer_id: str,
    reporting_entity_kind: Literal[
        "legal_registrant",
        "fund_series",
        "foreign_reporting_entity",
        "other",
    ],
    display_name: str,
    recorded_at: datetime,
) -> int:
    existing = conn.execute(
        "SELECT issuer_id, reporting_entity_kind, display_name "
        "FROM reporting_entities WHERE reporting_entity_id = ?",
        (reporting_entity_id,),
    ).fetchone()
    expected = (issuer_id, reporting_entity_kind, display_name)
    if existing is not None:
        if tuple(str(value) for value in existing) != expected:
            raise ValueError("reporting entity ID conflicts with immutable identity")
        return 0
    return int(
        registry.persist(
            ReportingEntity(
                reporting_entity_id=reporting_entity_id,
                idempotency_key=reporting_entity_id,
                issuer_id=issuer_id,
                reporting_entity_kind=reporting_entity_kind,
                display_name=display_name,
                created_at=recorded_at,
            )
        ).created
    )


def _persist_security(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    security_id: str,
    issuer_id: str,
    security_kind: Literal[
        "common_stock",
        "preferred_stock",
        "adr",
        "fund_share",
        "partnership_unit",
        "debt",
        "other",
    ],
    share_class: str | None,
    recorded_at: datetime,
) -> int:
    existing = conn.execute(
        "SELECT issuer_id, security_kind, share_class FROM securities WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    expected = (issuer_id, security_kind, share_class)
    if existing is not None:
        stored = tuple(None if value is None else str(value) for value in existing)
        if stored != expected:
            raise ValueError("security ID conflicts with immutable identity")
        return 0
    return int(
        registry.persist(
            Security(
                security_id=security_id,
                idempotency_key=security_id,
                issuer_id=issuer_id,
                security_kind=security_kind,
                share_class=share_class,
                created_at=recorded_at,
            )
        ).created
    )


def _persist_security_reporting_relationship(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    security_id: str,
    reporting_entity_id: str,
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    relationship_key = f"{security_id}:reports-through"
    current = conn.execute(
        "SELECT relationship_revision_id, revision, security_id, reporting_entity_id, "
        "relationship_kind FROM security_reporting_entity_revisions "
        "WHERE relationship_key = ? ORDER BY revision DESC LIMIT 1",
        (relationship_key,),
    ).fetchone()
    expected = (security_id, reporting_entity_id, "reports_through")
    if current is not None and tuple(str(value) for value in current[2:]) == expected:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    relationship_id = _record_id(
        "security-reporting-relationship",
        relationship_key,
        reporting_entity_id,
        str(revision),
    )
    return int(
        registry.persist(
            SecurityReportingEntityRevision(
                relationship_revision_id=relationship_id,
                idempotency_key=relationship_id,
                relationship_key=relationship_key,
                revision=revision,
                security_id=security_id,
                reporting_entity_id=reporting_entity_id,
                relationship_kind="reports_through",
                decision_kind="deterministic",
                reason_code="sec_series_class_mapping",
                reason_details=(("source_observation_id", source_observation_id),),
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_relationship_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _persist_profile(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    legal_name: str,
    observation_id: str,
    source_sha: str,
    recorded_at: datetime,
    filing_regime: str = "SEC",
    reason_code: str = "sec_company_tickers_import",
    status: Literal["active", "inactive", "merged", "dissolved"] = "active",
    effective_at: datetime | None = None,
) -> int:
    current = conn.execute(
        "SELECT profile_revision_id, revision, legal_name, filing_regime, status "
        "FROM issuer_profile_revisions WHERE issuer_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (issuer_id,),
    ).fetchone()
    if current is not None and tuple(str(value) for value in current[2:]) == (
        legal_name,
        filing_regime,
        status,
    ):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id("issuer-profile", issuer_id, source_sha, legal_name)
    return int(
        registry.persist(
            IssuerProfileRevision(
                profile_revision_id=record_id,
                idempotency_key=record_id,
                issuer_id=issuer_id,
                revision=revision,
                legal_name=legal_name,
                domicile_country=None,
                filing_regime=filing_regime,
                fiscal_year_end=None,
                status=status,
                decision_kind="imported",
                reason_code=reason_code,
                reason_details=(("source_observation_id", observation_id),),
                effective_at=effective_at or recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_profile_revision_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _persist_identifier_resolution(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    assertion: IdentifierAssertion,
    recorded_at: datetime,
) -> int:
    rows = conn.execute(
        """
        SELECT assertion_id, idempotency_key, issuer_id, identifier_value,
               normalized_value, authority, source_observation_id,
               effective_at, knowledge_at, recorded_at
        FROM issuer_identifier_assertions
        WHERE identifier_type = 'sec_cik' AND normalized_value = ?
        ORDER BY assertion_id
        """,
        (assertion.normalized_value,),
    ).fetchall()
    assertions = tuple(
        IdentifierAssertion(
            assertion_id=str(row[0]),
            idempotency_key=str(row[1]),
            issuer_id=str(row[2]),
            identifier_type="sec_cik",
            identifier_value=str(row[3]),
            normalized_value=str(row[4]),
            authority=cast(
                Literal[
                    "issuer_publisher",
                    "sec_registry",
                    "exchange_registry",
                    "regulator",
                    "manual",
                    "imported",
                ],
                str(row[5]),
            ),
            source_observation_id=None if row[6] is None else str(row[6]),
            effective_at=_parse_datetime(row[7]),
            knowledge_at=_parse_datetime(row[8]),
            recorded_at=_parse_datetime(row[9]),
        )
        for row in rows
    )
    candidate_digest = identifier_candidate_digest(assertions)
    resolution_key = assertion.resolution_key
    current = conn.execute(
        "SELECT resolution_id, revision, candidate_digest_sha256, "
        "selected_assertion_id FROM issuer_identifier_resolution_outcomes "
        "WHERE resolution_key = ? ORDER BY revision DESC LIMIT 1",
        (resolution_key,),
    ).fetchone()
    selected = max(assertions, key=lambda item: (item.knowledge_at, item.assertion_id))
    dissenting_issuers = {item.issuer_id for item in assertions} - {selected.issuer_id}
    if (
        current is not None
        and str(current[2]) == candidate_digest
        and str(current[3]) == selected.assertion_id
    ):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    policy_sha = _digest(
        "identifier-policy",
        json.dumps(
            {
                "policy": _POLICY_NAME,
                "version": _POLICY_VERSION,
                "require_unique_ticker": True,
                "selection": "latest_sec_registry_assertion",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    record_id = _record_id("identifier-resolution", resolution_key, candidate_digest)
    return int(
        registry.persist(
            IdentifierResolution(
                resolution_id=record_id,
                idempotency_key=record_id,
                resolution_key=resolution_key,
                revision=revision,
                outcome="selected",
                selected_assertion_id=selected.assertion_id,
                candidate_digest_sha256=candidate_digest,
                policy_name=_POLICY_NAME,
                policy_version=_POLICY_VERSION,
                policy_config_sha256=policy_sha,
                reason_code="unique_sec_registry_identifier",
                reason_details=(
                    ("candidate_count", str(len(assertions))),
                    ("selected_assertion_id", selected.assertion_id),
                ),
                material_dissent=bool(dissenting_issuers),
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_resolution_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _persist_sec_surface(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    normalized_cik: str,
    observation_id: str,
    source_sha: str,
    recorded_at: datetime,
    verification_method: str = "sec_company_tickers_registry",
) -> int:
    source_url = f"https://data.sec.gov/submissions/CIK{normalized_cik}.json"
    current = conn.execute(
        "SELECT surface_revision_id, revision, source_url, status "
        "FROM issuer_authority_surface_revisions "
        "WHERE issuer_id = ? AND surface_key = 'sec-submissions' "
        "ORDER BY revision DESC LIMIT 1",
        (issuer_id,),
    ).fetchone()
    if current is not None and (str(current[2]), str(current[3])) == (
        source_url,
        "verified",
    ):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id("authority-surface", issuer_id, "sec-submissions", source_sha)
    return int(
        registry.persist(
            AuthoritySurfaceRevision(
                surface_revision_id=record_id,
                idempotency_key=record_id,
                issuer_id=issuer_id,
                surface_key="sec-submissions",
                revision=revision,
                surface_kind="sec_submissions",
                source_url=source_url,
                status="verified",
                authority_level="regulator",
                source_observation_id=observation_id,
                verification_method=verification_method,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_surface_revision_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _persist_binding(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    ticker: str,
    issuer_id: str | None,
    outcome: Literal["selected", "unresolved"],
    reason_code: str,
    reason_details: tuple[tuple[str, str], ...],
    material_dissent: bool,
    source_sha: str,
    recorded_at: datetime,
) -> int:
    recorded_issuer_id = f"legacy-ticker:{ticker}"
    current = conn.execute(
        "SELECT binding_revision_id, revision, issuer_id, outcome, reason_code "
        "FROM legacy_issuer_binding_revisions WHERE recorded_issuer_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    if current is not None and (
        None if current[2] is None else str(current[2]),
        str(current[3]),
        str(current[4]),
    ) == (issuer_id, outcome, reason_code):
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "legacy-binding",
        recorded_issuer_id,
        outcome,
        issuer_id or "none",
        source_sha,
        str(revision),
    )
    return int(
        registry.persist(
            LegacyIssuerBindingRevision(
                binding_revision_id=record_id,
                idempotency_key=record_id,
                recorded_issuer_id=recorded_issuer_id,
                revision=revision,
                issuer_id=issuer_id,
                outcome=outcome,
                decision_kind="deterministic",
                reason_code=reason_code,
                reason_details=reason_details,
                material_dissent=material_dissent,
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_binding_revision_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _persist_scope(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    inclusion_state: Literal["core", "monitored"],
    scope_list_types: tuple[str, ...],
    recorded_at: datetime,
    require_sec: bool = True,
    require_ir: bool = True,
    require_earnings: bool = True,
    reason_code: str | None = None,
) -> int:
    scope_key = "investor-research"
    current = conn.execute(
        "SELECT scope_revision_id, revision, inclusion_state, history_policy, "
        "require_sec, require_ir, require_earnings "
        "FROM issuer_reporting_scope_revisions "
        "WHERE scope_key = ? AND issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (scope_key, issuer_id),
    ).fetchone()
    semantics = (
        inclusion_state,
        "all_available",
        int(require_sec),
        int(require_ir),
        int(require_earnings),
    )
    if current is not None and tuple(current[2:]) == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id("reporting-scope", scope_key, issuer_id, inclusion_state, str(revision))
    return int(
        registry.persist(
            ReportingScopeRevision(
                scope_revision_id=record_id,
                idempotency_key=record_id,
                scope_key=scope_key,
                issuer_id=issuer_id,
                revision=revision,
                inclusion_state=inclusion_state,
                history_policy="all_available",
                history_start=None,
                latest_years=None,
                require_sec=require_sec,
                require_ir=require_ir,
                require_earnings=require_earnings,
                decision_kind="deterministic",
                reason_code=reason_code
                or (
                    "portfolio_reporting_scope"
                    if inclusion_state == "core"
                    else "monitored_reporting_scope"
                ),
                reason_details=(("list_types", ",".join(scope_list_types)),),
                effective_at=recorded_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_scope_revision_id=None if current is None else str(current[0]),
            )
        ).created
    )


def _persist_historical_reporting_scope(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    termination_at: datetime,
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    scope_key = "investor-research"
    current = conn.execute(
        "SELECT scope_revision_id, revision, inclusion_state, history_policy, "
        "require_sec, require_ir, require_earnings "
        "FROM issuer_reporting_scope_revisions "
        "WHERE scope_key = ? AND issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (scope_key, issuer_id),
    ).fetchone()
    semantics = ("discovery", "all_available", 0, 0, 0)
    if current is not None and tuple(current[2:]) == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "reporting-scope",
        scope_key,
        issuer_id,
        "historical",
        str(revision),
    )
    return int(
        registry.persist(
            ReportingScopeRevision(
                scope_revision_id=record_id,
                idempotency_key=record_id,
                scope_key=scope_key,
                issuer_id=issuer_id,
                revision=revision,
                inclusion_state="discovery",
                history_policy="all_available",
                history_start=None,
                latest_years=None,
                require_sec=False,
                require_ir=False,
                require_earnings=False,
                decision_kind="deterministic",
                reason_code="delisted_historical_retention",
                reason_details=(
                    ("reporting_terminated_at", termination_at.isoformat()),
                    ("source_observation_id", source_observation_id),
                ),
                effective_at=termination_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_scope_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _persist_retired_ticker_reporting_scope(
    conn: sqlite3.Connection,
    registry: IssuerRegistry,
    *,
    issuer_id: str,
    transition_at: datetime,
    successor_ticker: str,
    source_observation_id: str,
    recorded_at: datetime,
) -> int:
    scope_key = "investor-research"
    current = conn.execute(
        "SELECT scope_revision_id, revision, inclusion_state, history_policy, "
        "require_sec, require_ir, require_earnings "
        "FROM issuer_reporting_scope_revisions "
        "WHERE scope_key = ? AND issuer_id = ? ORDER BY revision DESC LIMIT 1",
        (scope_key, issuer_id),
    ).fetchone()
    if current is not None and str(current[2]) in {"core", "monitored"}:
        return 0
    semantics = ("discovery", "all_available", 0, 0, 0)
    if current is not None and tuple(current[2:]) == semantics:
        return 0
    revision = 1 if current is None else int(current[1]) + 1
    record_id = _record_id(
        "reporting-scope",
        scope_key,
        issuer_id,
        "retired-ticker",
        str(revision),
    )
    return int(
        registry.persist(
            ReportingScopeRevision(
                scope_revision_id=record_id,
                idempotency_key=record_id,
                scope_key=scope_key,
                issuer_id=issuer_id,
                revision=revision,
                inclusion_state="discovery",
                history_policy="all_available",
                history_start=None,
                latest_years=None,
                require_sec=False,
                require_ir=False,
                require_earnings=False,
                decision_kind="deterministic",
                reason_code="retired_ticker_historical_retention",
                reason_details=(
                    ("successor_ticker", successor_ticker),
                    ("ticker_transition_at", transition_at.isoformat()),
                    ("source_observation_id", source_observation_id),
                ),
                effective_at=transition_at,
                knowledge_at=recorded_at,
                recorded_at=recorded_at,
                supersedes_scope_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    )


def _issuer_id(normalized_cik: str) -> str:
    return (
        "issuer:" + hashlib.sha256(f"canonical-sec-issuer\0{normalized_cik}".encode()).hexdigest()
    )


def _record_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _digest(namespace: str, payload: str) -> str:
    return hashlib.sha256(f"{namespace}\0{payload}".encode()).hexdigest()


def _parse_datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
