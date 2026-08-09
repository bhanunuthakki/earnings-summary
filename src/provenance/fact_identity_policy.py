"""Fail-closed source-to-canonical identity admission policy.

Source identity is always exact.  This module only decides whether a reviewed
mapping is eligible to bind that retained source assertion to a canonical cell.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metric_ontology import MappingRevision, SourceTaxonomyComponent


@dataclass(frozen=True)
class FactIdentityAdmission:
    """The immutable binding authorization produced by one mapping revision."""

    source_component_id: str
    mapping_revision_id: str
    metric_id: str


def admit_fact_identity(
    component: SourceTaxonomyComponent,
    mapping: MappingRevision,
) -> FactIdentityAdmission:
    """Return an authorization only for an explicit metric-carrying mapping.

    Extensions are intentionally denied unless a named reviewer or audited
    policy path accompanies exact/equivalent admission.
    """
    if mapping.source_component_id != component.component_id:
        raise ValueError("mapping source component must equal the asserted source identity")
    if mapping.disposition not in {"exact", "equivalent", "derived"} or mapping.metric_id is None:
        raise ValueError("only metric-carrying mapping dispositions may bind a fact cell")
    if (
        component.is_extension
        and mapping.disposition in {"exact", "equivalent"}
        and not (mapping.reviewer_identity or mapping.audited_policy_path)
    ):
        raise ValueError("extension admission requires explicit reviewer or audited policy path")
    return FactIdentityAdmission(
        source_component_id=component.component_id,
        mapping_revision_id=mapping.mapping_revision_id,
        metric_id=mapping.metric_id,
    )
