"""Pure fail-closed adapter for normalized Wix rendered-panel observations."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, TypeAdapter

from pipeline.approved_ir_catalog import (
    CatalogDisposition,
    IrCatalogEntry,
    IrCatalogError,
    IrParseResult,
    IrSourceObservation,
    Sha256Hex,
    classify_link,
)
from pipeline.source_policy import AdapterKey, IssuerAcquisitionPolicy

ADAPTER_VERSION = "wix-visible-panel-v1"


class WixLinkObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    url: str
    declared_kind: str
    evidence_locator: str


class WixPanelObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    panel_locator: str
    quarter_end: date
    selected: bool
    visible: bool
    links: tuple[WixLinkObservation, ...]


class WixRenderedObservation(BaseModel):
    """One rendered DOM state after selecting a quarter in the Wix UI."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_key: str
    authority_url: str
    raw_sha256: Sha256Hex
    requested_quarter_end: date
    panels: tuple[WixPanelObservation, ...]


_OBSERVATION_ADAPTER = TypeAdapter(tuple[WixRenderedObservation, ...])


def load_wix_rendered_observations(payload: str) -> tuple[WixRenderedObservation, ...]:
    """Validate deterministic rendered-panel JSON emitted by the browser boundary."""
    return _OBSERVATION_ADAPTER.validate_json(payload)


def parse_wix_visible_quarters(
    observations: tuple[WixRenderedObservation, ...],
    *,
    policy: IssuerAcquisitionPolicy,
) -> IrParseResult:
    if policy.ir.adapter_key is not AdapterKey.WIX_VISIBLE_QUARTER:
        raise ValueError("Wix adapter requires wix_visible_quarter policy")
    entries: list[IrCatalogEntry] = []
    source_observations: list[IrSourceObservation] = []
    excluded = 0
    for observation in observations:
        selected = [panel for panel in observation.panels if panel.selected and panel.visible]
        if len(selected) != 1:
            raise IrCatalogError(
                "expected exactly one visible selected Wix panel; "
                f"found {len(selected)} in {observation.observation_key!r}"
            )
        panel = selected[0]
        if panel.quarter_end != observation.requested_quarter_end:
            raise IrCatalogError("visible Wix panel does not match the requested reporting period")
        source_observations.append(
            IrSourceObservation(
                observation_key=observation.observation_key,
                authority_url=observation.authority_url,
                raw_sha256=observation.raw_sha256,
                adapter_key=policy.ir.adapter_key.value,
                adapter_version=ADAPTER_VERSION,
                quarter_end=panel.quarter_end,
                evidence_locator=panel.panel_locator,
            )
        )
        for link in panel.links:
            classified = classify_link(
                policy,
                quarter_end=panel.quarter_end,
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
    periods = tuple(sorted({item.requested_quarter_end for item in observations}, reverse=True))
    return IrParseResult(
        entries=tuple(entries),
        observed_reporting_periods=periods,
        observations=tuple(source_observations),
        excluded_webcast_count=excluded,
    )
