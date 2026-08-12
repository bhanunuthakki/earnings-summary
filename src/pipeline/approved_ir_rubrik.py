"""Pure adapter for normalized Rubrik quarterly-row observations."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, TypeAdapter

from pipeline.approved_ir_catalog import (
    CatalogDisposition,
    IrCatalogEntry,
    IrParseResult,
    IrSourceObservation,
    Sha256Hex,
    classify_link,
)
from pipeline.source_policy import AdapterKey, IssuerAcquisitionPolicy

ADAPTER_VERSION = "rubrik-quarter-rows-v1"


class RubrikLinkObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    url: str
    declared_kind: str
    evidence_locator: str


class RubrikQuarterObservation(BaseModel):
    """Typed browser/extractor boundary preserving a structured source-row locator."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_key: str
    authority_url: str
    raw_sha256: Sha256Hex
    quarter_end: date
    row_locator: str
    links: tuple[RubrikLinkObservation, ...]


_OBSERVATION_ADAPTER = TypeAdapter(tuple[RubrikQuarterObservation, ...])


def load_rubrik_row_observations(payload: str) -> tuple[RubrikQuarterObservation, ...]:
    """Validate deterministic rendered-row JSON emitted by the capture boundary."""
    return _OBSERVATION_ADAPTER.validate_json(payload)


def parse_rubrik_quarter_rows(
    observations: tuple[RubrikQuarterObservation, ...],
    *,
    policy: IssuerAcquisitionPolicy,
) -> IrParseResult:
    if policy.ir.adapter_key is not AdapterKey.RUBRIK_QUARTER_TABLE:
        raise ValueError("Rubrik adapter requires rubrik_quarter_table policy")
    entries: list[IrCatalogEntry] = []
    source_observations: list[IrSourceObservation] = []
    excluded = 0
    for observation in observations:
        source_observations.append(
            IrSourceObservation(
                observation_key=observation.observation_key,
                authority_url=observation.authority_url,
                raw_sha256=observation.raw_sha256,
                adapter_key=policy.ir.adapter_key.value,
                adapter_version=ADAPTER_VERSION,
                quarter_end=observation.quarter_end,
                evidence_locator=observation.row_locator,
            )
        )
        for link in observation.links:
            classified = classify_link(
                policy,
                quarter_end=observation.quarter_end,
                title=link.title,
                url=link.url,
                declared_kind=link.declared_kind,
                observation_key=observation.observation_key,
                evidence_locator=link.evidence_locator,
            )
            if classified.disposition is CatalogDisposition.WEBCAST_EXCLUDED:
                excluded += 1
            else:
                entries.append(classified)
    periods = tuple(sorted({item.quarter_end for item in observations}, reverse=True))
    return IrParseResult(
        entries=tuple(entries),
        observed_reporting_periods=periods,
        observations=tuple(source_observations),
        excluded_webcast_count=excluded,
    )
