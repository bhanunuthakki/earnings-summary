"""Software-derived SEC scope manifests; never issuer-reported disclosure."""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from filings.sec_submissions_inventory import (
    ParsedSecInventory,
    SecFilingInventoryEntry,
    SecInventoryIssue,
)

SCOPE_MANIFEST_VERSION = "sec_inventory_duty_scope.v1"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScopeSource(_Closed):
    component_name: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    blob_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FilingScopeDisposition(_Closed):
    filing: SecFilingInventoryEntry
    disposition: Literal["governed_reporting", "inventory_only", "unclassified"]
    reason_code: Literal[
        "governed_reporting_form", "outside_governed_reporting_policy", "unknown_sec_form"
    ]

    @model_validator(mode="after")
    def _reason_matches(self) -> Self:
        expected = {
            "governed_reporting": "governed_reporting_form",
            "inventory_only": "outside_governed_reporting_policy",
            "unclassified": "unknown_sec_form",
        }
        if self.reason_code != expected[self.disposition]:
            raise ValueError("scope disposition reason differs")
        return self


class SecInventoryScopeManifest(_Closed):
    schema_version: Literal["sec_inventory_duty_scope.v1"] = SCOPE_MANIFEST_VERSION
    claim_basis: Literal["software_derived"] = "software_derived"
    clock_basis: Literal["first_local_manifest_capture"] = "first_local_manifest_capture"
    policy_version: str = Field(min_length=1)
    inventory_key: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    source_issuer_id: str = Field(min_length=1)
    primary_component_name: str = Field(min_length=1)
    root_source_observation_id: str = Field(min_length=1)
    required_component_names: tuple[str, ...] = Field(min_length=1)
    source_inputs: tuple[ScopeSource, ...] = Field(min_length=1)
    filings: tuple[FilingScopeDisposition, ...]
    parsing_issues: tuple[SecInventoryIssue, ...]

    @model_validator(mode="after")
    def _complete_identity(self) -> Self:
        names = [item.component_name for item in self.source_inputs]
        if len(names) != len(set(names)) or not set(names).issubset(self.required_component_names):
            raise ValueError("scope sources must uniquely match authority components")
        if len(self.required_component_names) != len(set(self.required_component_names)):
            raise ValueError("scope required components must be unique")
        roots = [
            item
            for item in self.source_inputs
            if item.component_name == self.primary_component_name
        ]
        if len(roots) != 1 or roots[0].source_observation_id != self.root_source_observation_id:
            raise ValueError("scope root must match the primary source component")
        if self.primary_component_name != self.required_component_names[0]:
            raise ValueError("scope primary component differs from parser authority")
        if len({item.source_observation_id for item in self.source_inputs}) != len(names):
            raise ValueError("scope source observations must be unique")
        accessions = [item.filing.accession_number for item in self.filings]
        if accessions != sorted(set(accessions)):
            raise ValueError("scope accession population must be unique and sorted")
        if any(
            item.filing.source_component_name not in names
            or item.filing.ticker != self.ticker
            or item.filing.issuer_id != self.issuer_id
            for item in self.filings
        ):
            raise ValueError("scope accession is outside its source, ticker or issuer")
        return self

    def verify_reconstruction(
        self, *, parsed: ParsedSecInventory, source_inputs: tuple[ScopeSource, ...]
    ) -> None:
        """Compare against the complete parser output and ledger-bound parent hashes."""
        expected = tuple(
            item.model_copy(update={"issuer_id": self.issuer_id})
            for item in sorted(parsed.filings, key=lambda item: item.accession_number)
        )
        if (
            self.source_issuer_id != parsed.issuer_id
            or self.ticker != parsed.ticker
            or self.required_component_names != parsed.required_component_names
            or self.parsing_issues != parsed.issues
            or tuple(item.filing for item in self.filings) != expected
            or self.source_inputs != source_inputs
        ):
            raise ValueError("scope manifest differs from complete authoritative inputs")

    def encoded(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
