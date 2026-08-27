"""Exhaustive, deterministic parsing of the SEC submissions file chain.

Unlike the bounded ``edgar_fetch.list_filings`` selector, this module defines
the upstream reporting universe.  It follows every filename advertised by the
authority, validates the columnar contract without truncation, and makes
missing components and duplicate-accession disagreement explicit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Literal, Self, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
_REQUIRED_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "act",
    "form",
    "fileNumber",
    "filmNumber",
    "items",
    "size",
    "isXBRL",
    "isInlineXBRL",
    "primaryDocument",
    "primaryDocDescription",
)


class SecInventoryContractError(ValueError):
    """The authority response cannot support an auditable inventory."""


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HistoricalComponent(_Closed):
    name: str = Field(min_length=1, max_length=512)
    body: bytes | None = None
    failure_reason: str | None = Field(default=None, min_length=1, max_length=128)
    required: bool = True

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if (self.body is None) == (self.failure_reason is None):
            raise ValueError("historical component requires exactly one body or failure_reason")
        return self


IssueCode: TypeAlias = Literal[
    "component_missing",
    "component_fetch_failed",
    "component_count_mismatch",
    "unexpected_required_component",
    "accession_conflict",
]


class SecInventoryIssue(_Closed):
    code: IssueCode
    component_name: str = Field(min_length=1)
    details: tuple[tuple[str, str], ...] = Field(min_length=1)


class SecFilingInventoryEntry(_Closed):
    issuer_id: str
    ticker: str
    accession_number: str
    form_type: str
    filing_date: str
    report_date: str | None
    accepted_at: str | None
    primary_document: str | None
    primary_document_url: str | None
    source_component_name: str


class ParsedSecInventory(_Closed):
    issuer_id: str
    ticker: str
    authority_name: str
    required_component_names: tuple[str, ...]
    filings: tuple[SecFilingInventoryEntry, ...]
    issues: tuple[SecInventoryIssue, ...]

    @property
    def complete(self) -> bool:
        return not self.issues


def parse_sec_submissions_inventory(
    *,
    cik: str,
    ticker: str,
    primary_body: bytes,
    historical: tuple[HistoricalComponent, ...],
) -> ParsedSecInventory:
    """Parse the complete SEC file chain without form filters or history caps."""

    normalized_cik = _normalize_cik(cik)
    issuer_id = f"sec-cik:{normalized_cik}"
    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise SecInventoryContractError("ticker must not be empty")
    root = _json_object(primary_body, "primary submissions response")
    payload_cik = root.get("cik")
    if payload_cik is not None and _normalize_cik(str(payload_cik)) != normalized_cik:
        raise SecInventoryContractError("primary submissions CIK conflicts with requested CIK")
    authority_name = str(root.get("name") or "").strip()
    if not authority_name:
        raise SecInventoryContractError("primary submissions response has no issuer name")
    filings = root.get("filings")
    if not isinstance(filings, dict):
        raise SecInventoryContractError("primary submissions response has no filings object")
    filings_map = cast(Mapping[str, object], filings)
    recent = filings_map.get("recent")
    if not isinstance(recent, dict):
        raise SecInventoryContractError("primary submissions response has no filings.recent")
    root_name = f"CIK{normalized_cik}.json"
    rows = _parse_columns(
        cast(Mapping[str, object], recent),
        component_name=root_name,
        issuer_id=issuer_id,
        ticker=normalized_ticker,
        cik=normalized_cik,
    )
    advertised = _advertised_files(filings_map.get("files"))
    required_names = (root_name, *(item[0] for item in advertised))
    supplied = {component.name: component for component in historical}
    if len(supplied) != len(historical):
        raise SecInventoryContractError("historical component names must be unique")
    issues: list[SecInventoryIssue] = []
    for name, advertised_count in advertised:
        component = supplied.get(name)
        if component is None:
            issues.append(
                _issue(
                    "component_missing",
                    name,
                    advertised_filing_count=str(advertised_count),
                )
            )
            continue
        if component.failure_reason is not None:
            issues.append(
                _issue(
                    "component_fetch_failed",
                    name,
                    failure_reason=component.failure_reason,
                )
            )
            continue
        component_rows = _parse_columns(
            _json_object(component.body or b"", name),
            component_name=name,
            issuer_id=issuer_id,
            ticker=normalized_ticker,
            cik=normalized_cik,
        )
        if advertised_count >= 0 and len(component_rows) != advertised_count:
            issues.append(
                _issue(
                    "component_count_mismatch",
                    name,
                    advertised=str(advertised_count),
                    parsed=str(len(component_rows)),
                )
            )
        rows.extend(component_rows)
    for name, component in sorted(supplied.items()):
        if name in required_names or not component.required:
            if name not in required_names and component.body is not None:
                rows.extend(
                    _parse_columns(
                        _json_object(component.body, name),
                        component_name=name,
                        issuer_id=issuer_id,
                        ticker=normalized_ticker,
                        cik=normalized_cik,
                    )
                )
            continue
        issues.append(_issue("unexpected_required_component", name, advertised="false"))

    by_accession: dict[str, SecFilingInventoryEntry] = {}
    conflicted: set[str] = set()
    for row in rows:
        prior = by_accession.get(row.accession_number)
        if prior is None:
            by_accession[row.accession_number] = row
            continue
        if _filing_identity(prior) == _filing_identity(row):
            continue
        conflicted.add(row.accession_number)
        issues.append(
            _issue(
                "accession_conflict",
                row.source_component_name,
                accession_number=row.accession_number,
                first_component=prior.source_component_name,
            )
        )
    for accession in conflicted:
        by_accession.pop(accession, None)
    return ParsedSecInventory(
        issuer_id=issuer_id,
        ticker=normalized_ticker,
        authority_name=authority_name,
        required_component_names=tuple(required_names),
        filings=tuple(by_accession[key] for key in sorted(by_accession)),
        issues=tuple(issues),
    )


def _parse_columns(
    value: Mapping[str, object],
    *,
    component_name: str,
    issuer_id: str,
    ticker: str,
    cik: str,
) -> list[SecFilingInventoryEntry]:
    columns: dict[str, list[object]] = {}
    for name in _REQUIRED_COLUMNS:
        column = value.get(name)
        if not isinstance(column, list):
            raise SecInventoryContractError(
                f"{component_name} missing required parallel column {name}"
            )
        columns[name] = cast(list[object], column)
    sizes = {len(column) for column in columns.values()}
    if len(sizes) != 1:
        rendered = ", ".join(f"{key}={len(value)}" for key, value in columns.items())
        raise SecInventoryContractError(
            f"{component_name} parallel columns differ in length: {rendered}"
        )
    result: list[SecFilingInventoryEntry] = []
    for index in range(next(iter(sizes), 0)):
        accession = str(columns["accessionNumber"][index] or "").strip()
        primary = str(columns["primaryDocument"][index] or "").strip()
        form = str(columns["form"][index] or "").strip()
        filing_date = str(columns["filingDate"][index] or "").strip()
        if not _ACCESSION.fullmatch(accession):
            raise SecInventoryContractError(
                f"{component_name} row {index} has invalid accessionNumber"
            )
        if not form or not filing_date:
            raise SecInventoryContractError(
                f"{component_name} row {index} has empty form or filingDate"
            )
        result.append(
            SecFilingInventoryEntry(
                issuer_id=issuer_id,
                ticker=ticker,
                accession_number=accession,
                form_type=form,
                filing_date=filing_date,
                report_date=_optional(columns["reportDate"][index]),
                accepted_at=_optional(columns["acceptanceDateTime"][index]),
                primary_document=primary or None,
                primary_document_url=(
                    None
                    if not primary
                    else f"{_ARCHIVE_BASE}/{int(cik)}/{accession.replace('-', '')}/{primary}"
                ),
                source_component_name=component_name,
            )
        )
    return result


def _advertised_files(value: object) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SecInventoryContractError("filings.files must be an array")
    result: list[tuple[str, int]] = []
    for index, raw in enumerate(cast(list[object], value)):
        if not isinstance(raw, dict):
            raise SecInventoryContractError(f"filings.files[{index}] must be an object")
        item = cast(Mapping[str, object], raw)
        name = str(item.get("name") or "").strip()
        if not name or "/" in name or "\\" in name:
            raise SecInventoryContractError(
                f"filings.files[{index}] has invalid authority filename"
            )
        count_raw = item.get("filingCount")
        try:
            count = int(str(count_raw))
        except (TypeError, ValueError) as exc:
            raise SecInventoryContractError(
                f"filings.files[{index}] has invalid filingCount"
            ) from exc
        if count < 0:
            raise SecInventoryContractError(
                f"filings.files[{index}] filingCount must be non-negative"
            )
        result.append((name, count))
    names = [name for name, _ in result]
    if len(names) != len(set(names)):
        raise SecInventoryContractError("filings.files contains duplicate filenames")
    return tuple(result)


def historical_component_url(name: str) -> str:
    """Resolve only a filename supplied by the SEC authority response."""

    if not name or "/" in name or "\\" in name:
        raise ValueError("historical component name must be an authority filename")
    return f"{_SUBMISSIONS_BASE}/{name}"


def advertised_historical_components(primary_body: bytes) -> tuple[tuple[str, int], ...]:
    """Return the exact filename/count chain supplied by the SEC root response."""

    root = _json_object(primary_body, "primary submissions response")
    filings = root.get("filings")
    if not isinstance(filings, dict):
        raise SecInventoryContractError("primary submissions response has no filings object")
    return _advertised_files(cast(Mapping[str, object], filings).get("files"))


def _json_object(body: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecInventoryContractError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SecInventoryContractError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], value)


def _normalize_cik(value: str) -> str:
    stripped = value.strip()
    if not stripped.isdigit() or len(stripped) > 10:
        raise SecInventoryContractError("CIK must contain at most ten digits")
    return stripped.zfill(10)


def _optional(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _issue(code: IssueCode, component_name: str, **details: str) -> SecInventoryIssue:
    return SecInventoryIssue(
        code=code,
        component_name=component_name,
        details=tuple(sorted(details.items())),
    )


def _filing_identity(item: SecFilingInventoryEntry) -> tuple[object, ...]:
    return (
        item.issuer_id,
        item.accession_number,
        item.form_type,
        item.filing_date,
        item.report_date,
        item.accepted_at,
        item.primary_document,
        item.primary_document_url,
    )
