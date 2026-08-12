"""Pure, hash-bound catalog for owner-approved issuer IR pages."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, StringConstraints

from models.documents import DocType
from pipeline.source_policy import (
    IssuerAcquisitionPolicy,
    canonical_https_url,
    ir_url_is_authorized,
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class IrCatalogError(ValueError):
    """The approved page is ambiguous or a candidate escapes its source policy."""


class CatalogDisposition(StrEnum):
    IR_DOCUMENT = "ir_document"
    SEC_HANDOFF = "sec_handoff"
    TRANSCRIPT_CANDIDATE = "transcript_candidate"
    WEBCAST_EXCLUDED = "webcast_excluded"


class IrCatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    quarter_end: date
    title: str
    url: str
    disposition: CatalogDisposition
    doc_type: DocType | None = None
    observation_key: str
    evidence_locator: str


class LinkClassification(IrCatalogEntry):
    pass


class IrSourceObservation(BaseModel):
    """One hash-bound source/rendered-DOM observation consumed by an adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_key: str
    authority_url: str
    raw_sha256: Sha256Hex
    adapter_key: str
    adapter_version: str
    quarter_end: date
    evidence_locator: str


class IrParseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entries: tuple[IrCatalogEntry, ...]
    observed_reporting_periods: tuple[date, ...]
    observations: tuple[IrSourceObservation, ...]
    excluded_webcast_count: int = 0


class ApprovedIrCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    issuer_id: str
    issuer_policy_sha256: str
    authority_url: str
    adapter_key: str
    adapter_version: str
    reported_quarters: tuple[date, ...]
    observations: tuple[IrSourceObservation, ...]
    entries: tuple[IrCatalogEntry, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def catalog_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


_DOCUMENT_TYPES: dict[str, DocType] = {
    "earnings-release": DocType.IR_PRESS_RELEASE,
    "presentation": DocType.IR_PRESENTATION,
    "investor-update": DocType.IR_INVESTOR_UPDATE,
}
_TRANSCRIPT_KINDS = frozenset({"transcript", "prepared-remarks", "text-transcript"})
_WEBCAST_KINDS = frozenset({"webcast", "audio", "replay"})


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def _is_sec_archive_url(url: str) -> bool:
    canonical = canonical_https_url(url)
    if canonical is None:
        return False
    host, path = canonical
    return host in {"sec.gov", "www.sec.gov"} and path.startswith("/Archives/")


def classify_link(
    policy: IssuerAcquisitionPolicy,
    *,
    quarter_end: date,
    title: str,
    url: str,
    declared_kind: str,
    observation_key: str,
    evidence_locator: str,
) -> LinkClassification:
    """Classify one explicit page link without fetching or persisting it."""
    kind = declared_kind.strip().casefold()
    folded_title = title.casefold()
    canonical_url = _canonical_url(url)
    if kind in _WEBCAST_KINDS or "webcast" in folded_title or "listen live" in folded_title:
        return LinkClassification(
            quarter_end=quarter_end,
            title=title.strip(),
            url=canonical_url,
            disposition=CatalogDisposition.WEBCAST_EXCLUDED,
            observation_key=observation_key,
            evidence_locator=evidence_locator,
        )

    if kind == "sec-filing":
        if not _is_sec_archive_url(url):
            raise IrCatalogError(f"SEC handoff escaped the approved SEC archive: {url!r}")
        return LinkClassification(
            quarter_end=quarter_end,
            title=title.strip(),
            url=canonical_url,
            disposition=CatalogDisposition.SEC_HANDOFF,
            observation_key=observation_key,
            evidence_locator=evidence_locator,
        )

    if not ir_url_is_authorized(policy.ir, url):
        raise IrCatalogError(f"IR candidate escaped exact endpoint policy: {url!r}")
    if kind in _TRANSCRIPT_KINDS or "transcript" in folded_title:
        if DocType.IR_TRANSCRIPT not in policy.ir.admitted_doc_types:
            raise IrCatalogError("issuer policy does not admit text transcripts")
        return LinkClassification(
            quarter_end=quarter_end,
            title=title.strip(),
            url=canonical_url,
            disposition=CatalogDisposition.TRANSCRIPT_CANDIDATE,
            doc_type=DocType.IR_TRANSCRIPT,
            observation_key=observation_key,
            evidence_locator=evidence_locator,
        )

    doc_type = _DOCUMENT_TYPES.get(kind)
    if doc_type is None:
        raise IrCatalogError(f"unrecognized approved-IR document kind: {declared_kind!r}")
    if doc_type not in policy.ir.admitted_doc_types:
        raise IrCatalogError(f"issuer policy does not admit {doc_type.value}")
    return LinkClassification(
        quarter_end=quarter_end,
        title=title.strip(),
        url=canonical_url,
        disposition=CatalogDisposition.IR_DOCUMENT,
        doc_type=doc_type,
        observation_key=observation_key,
        evidence_locator=evidence_locator,
    )


def build_catalog(
    policy: IssuerAcquisitionPolicy,
    parsed: IrParseResult,
) -> ApprovedIrCatalog:
    """Deduplicate and cap candidates at the latest policy-approved quarters."""
    observation_by_key = {item.observation_key: item for item in parsed.observations}
    if len(observation_by_key) != len(parsed.observations):
        raise IrCatalogError("source observation keys must be unique")
    adapter_versions = {item.adapter_version for item in parsed.observations}
    if len(adapter_versions) != 1:
        raise IrCatalogError("one catalog cannot mix adapter versions")
    expected_adapter = policy.ir.adapter_key.value
    for observation in parsed.observations:
        if observation.authority_url != policy.ir.authority_url:
            raise IrCatalogError("source observation authority does not match issuer policy")
        if observation.adapter_key != expected_adapter:
            raise IrCatalogError("source observation adapter does not match issuer policy")
        if observation.quarter_end not in parsed.observed_reporting_periods:
            raise IrCatalogError("source observation period was not declared observed")
    latest_quarters = tuple(
        sorted(set(parsed.observed_reporting_periods), reverse=True)[
            : policy.ir.reported_quarter_window
        ]
    )
    quarter_set = frozenset(latest_quarters)
    admitted = tuple(
        entry
        for entry in parsed.entries
        if entry.disposition is not CatalogDisposition.WEBCAST_EXCLUDED
        and entry.quarter_end in quarter_set
    )
    for entry in admitted:
        observation = observation_by_key.get(entry.observation_key)
        if observation is None or observation.quarter_end != entry.quarter_end:
            raise IrCatalogError("catalog entry has no matching source observation")
        if entry.disposition is CatalogDisposition.SEC_HANDOFF:
            if not _is_sec_archive_url(entry.url):
                raise IrCatalogError(f"catalog SEC handoff escaped SEC archives: {entry.url!r}")
            if entry.doc_type is not None:
                raise IrCatalogError("SEC handoffs cannot masquerade as admitted IR documents")
        elif not ir_url_is_authorized(policy.ir, entry.url):
            raise IrCatalogError(f"catalog entry escaped exact endpoint policy: {entry.url!r}")
        if entry.disposition is CatalogDisposition.IR_DOCUMENT and entry.doc_type is None:
            raise IrCatalogError("admitted IR document requires an explicit document type")
        if (
            entry.disposition is CatalogDisposition.TRANSCRIPT_CANDIDATE
            and entry.doc_type is not DocType.IR_TRANSCRIPT
        ):
            raise IrCatalogError("transcript candidate requires ir_transcript document type")
        if entry.doc_type is not None and entry.doc_type not in policy.ir.admitted_doc_types:
            raise IrCatalogError(
                f"catalog entry has unapproved document type: {entry.doc_type.value}"
            )
    deduped: dict[tuple[date, str], IrCatalogEntry] = {}
    for entry in admitted:
        key = (entry.quarter_end, _canonical_url(entry.url))
        current = deduped.get(key)
        if current is not None and (
            current.disposition is not entry.disposition or current.doc_type is not entry.doc_type
        ):
            raise IrCatalogError(f"one catalog URL has conflicting classification: {entry.url!r}")
        candidate_tie_break = (
            entry.title.casefold(),
            entry.title,
            entry.observation_key,
            entry.evidence_locator,
        )
        current_tie_break = (
            (
                current.title.casefold(),
                current.title,
                current.observation_key,
                current.evidence_locator,
            )
            if current is not None
            else None
        )
        if current_tie_break is None or candidate_tie_break < current_tie_break:
            deduped[key] = entry.model_copy(update={"url": key[1]})
    ordered = tuple(
        sorted(
            deduped.values(),
            key=lambda entry: (
                -entry.quarter_end.toordinal(),
                entry.disposition.value,
                entry.url,
                entry.title.casefold(),
            ),
        )
    )
    return ApprovedIrCatalog(
        issuer_id=policy.issuer_id,
        issuer_policy_sha256=policy.policy_sha256,
        authority_url=policy.ir.authority_url,
        adapter_key=expected_adapter,
        adapter_version=next(iter(adapter_versions), "unobserved"),
        reported_quarters=latest_quarters,
        observations=tuple(sorted(parsed.observations, key=lambda item: item.observation_key)),
        entries=ordered,
    )
