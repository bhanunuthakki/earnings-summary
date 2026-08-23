"""Parse Mercado Libre's embedded quarterly-results inventory without side effects.

Mercado Libre renders its results cards from the Nordic rendering context instead
of exposing their document anchors in the initial page DOM.  This module turns
that publisher-provided context into a closed, typed inventory before any caller
chooses to download, stage, or register a document.
"""

from __future__ import annotations

import json
from datetime import date
from html.parser import HTMLParser
from typing import Literal, Self, TypeAlias, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CONTEXT_SCRIPT_ID = "__NORDIC_RENDERING_CTX__"
MeliDocumentType: TypeAlias = Literal[
    "ir_investor_update", "ir_presentation", "sec_10q", "ir_transcript"
]

_DOCUMENT_TYPES: dict[str, MeliDocumentType] = {
    "Letter to Shareholders": "ir_investor_update",
    "Earnings Presentation": "ir_presentation",
    "SEC Filing": "sec_10q",
    "Webcast Transcript": "ir_transcript",
}
_REQUIRED_DOCUMENT_TYPES = frozenset(_DOCUMENT_TYPES.values())


class MeliEmbeddedDiscoveryError(ValueError):
    """The official page state cannot prove a complete requested inventory."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _PublisherModel(BaseModel):
    """Permissive vendor envelope; only the closed output leaves this boundary."""

    model_config = ConfigDict(extra="ignore")


class _PublisherLink(_PublisherModel):
    label: str = Field(min_length=1, max_length=256)
    href: str = Field(min_length=1, max_length=4_096)


class _PublisherResult(_PublisherModel):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=128)
    links: tuple[_PublisherLink, ...] = Field(min_length=1, max_length=16)


class _PublisherQuarterlyResults(_PublisherModel):
    items: tuple[_PublisherResult, ...] = Field(min_length=1, max_length=200)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MeliQuarterlyDocumentCandidate(_ClosedModel):
    """One publisher-labeled document, typed before transport or persistence."""

    document_type: MeliDocumentType
    label: str = Field(min_length=1, max_length=256)
    source_url: str = Field(min_length=1, max_length=4_096)

    @field_validator("source_url")
    @classmethod
    def _official_pdf_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.lower().endswith(".pdf")
        ):
            raise ValueError("source_url must be an absolute credential-free HTTPS PDF URL")
        return value


class MeliEmbeddedQuarterlyInventory(_ClosedModel):
    """The complete, closed publisher inventory for one reported quarter."""

    schema_version: Literal["meli_embedded_quarterly_inventory.v1"] = (
        "meli_embedded_quarterly_inventory.v1"
    )
    source_page: str = Field(min_length=1, max_length=4_096)
    result_id: str = Field(min_length=1, max_length=256)
    fiscal_year: int = Field(ge=2000, le=2100)
    fiscal_quarter: int = Field(ge=1, le=4)
    period_end: date
    documents: tuple[MeliQuarterlyDocumentCandidate, ...] = Field(min_length=4, max_length=4)

    @field_validator("source_page")
    @classmethod
    def _source_page(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source_page must be an absolute credential-free HTTPS URL")
        return value

    @model_validator(mode="after")
    def _complete_period(self) -> Self:
        if self.period_end != _quarter_end(self.fiscal_year, self.fiscal_quarter):
            raise ValueError("period_end must match the fiscal quarter")
        document_types = frozenset(document.document_type for document in self.documents)
        if document_types != _REQUIRED_DOCUMENT_TYPES:
            raise ValueError("documents must exactly cover the required publisher document types")
        if len({document.source_url for document in self.documents}) != len(self.documents):
            raise ValueError("document URLs must be unique")
        if (
            tuple(sorted(self.documents, key=lambda document: document.document_type))
            != self.documents
        ):
            raise ValueError("documents must be sorted by document type")
        return self


class _NordicContextParser(HTMLParser):
    """Extract one known inline publisher context without evaluating page JavaScript."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_context = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inside_context = tag == "script" and dict(attrs).get("id") == _CONTEXT_SCRIPT_ID

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside_context = False

    def handle_data(self, data: str) -> None:
        if self._inside_context:
            self.parts.append(data)


def discover_embedded_quarterly_inventory(
    rendered_html: str,
    *,
    source_page: str,
    fiscal_year: int,
    fiscal_quarter: int,
) -> MeliEmbeddedQuarterlyInventory:
    """Build one Q1–Q4 inventory from rendered official page bytes; never writes state."""

    quarterly_results = _quarterly_results_from_html(rendered_html)
    requested_title = f"Results Q{fiscal_quarter}'{fiscal_year % 100:02d}"
    matches = [item for item in quarterly_results.items if item.title == requested_title]
    if not matches:
        raise MeliEmbeddedDiscoveryError("requested_period_missing")
    if len(matches) != 1:
        raise MeliEmbeddedDiscoveryError("duplicate_requested_period")
    result = matches[0]
    documents: list[MeliQuarterlyDocumentCandidate] = []
    for link in result.links:
        document_type = _DOCUMENT_TYPES.get(link.label)
        if document_type is None:
            if urlsplit(link.href).path.lower().endswith(".pdf"):
                raise MeliEmbeddedDiscoveryError("unknown_document_label")
            continue
        documents.append(
            MeliQuarterlyDocumentCandidate(
                document_type=document_type,
                label=link.label,
                source_url=link.href,
            )
        )
    try:
        return MeliEmbeddedQuarterlyInventory(
            source_page=source_page,
            result_id=result.id,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_end=_quarter_end(fiscal_year, fiscal_quarter),
            documents=tuple(sorted(documents, key=lambda document: document.document_type)),
        )
    except ValueError as exc:
        raise MeliEmbeddedDiscoveryError("incomplete_document_inventory") from exc


def _quarterly_results_from_html(rendered_html: str) -> _PublisherQuarterlyResults:
    parser = _NordicContextParser()
    parser.feed(rendered_html)
    context = "".join(parser.parts)
    if not context.startswith("_n.ctx.r="):
        raise MeliEmbeddedDiscoveryError("nordic_context_missing")
    try:
        payload, _ = json.JSONDecoder().raw_decode(context.removeprefix("_n.ctx.r="))
    except json.JSONDecodeError as exc:
        raise MeliEmbeddedDiscoveryError("nordic_context_invalid") from exc
    candidates = _find_quarterly_results(payload)
    if len(candidates) != 1:
        raise MeliEmbeddedDiscoveryError("quarterly_results_ambiguous")
    try:
        return _PublisherQuarterlyResults.model_validate(candidates[0])
    except ValueError as exc:
        raise MeliEmbeddedDiscoveryError("quarterly_results_invalid") from exc


def _find_quarterly_results(value: object) -> list[object]:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        matches: list[object] = (
            [mapping["quarterlyResults"]] if "quarterlyResults" in mapping else []
        )
        for child in mapping.values():
            matches.extend(_find_quarterly_results(child))
        return matches
    if isinstance(value, list):
        sequence = cast(list[object], value)
        return [match for child in sequence for match in _find_quarterly_results(child)]
    return []


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    next_month = 1 if month == 12 else month + 1
    next_year = year + int(month == 12)
    return date(next_year, next_month, 1) - date.resolution
