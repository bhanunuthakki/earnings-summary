"""Provenance-backed admission helpers for KPI comparability bridges.

Each helper deterministically verifies overlap or additive sum witnesses before
appending immutable ``kpi_definition_comparability_revisions`` rows, failing
closed whenever proof is missing or exceeds tolerance.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from models.facts import FiscalPeriodType
from pipeline.kpi_comparability_bridges import (
    AdditiveBridgeDirection,
    AdditiveCompositeBridge,
    KpiComparabilityBridgeSpec,
    OverlapTolerancePolicy,
    OverlapVerificationError,
    RecastComparativeBridge,
    RenameOverlapBridge,
    SplitScaleAppliesTo,
    SplitScaleBridge,
    build_overlap_witness,
    evaluate_overlap_witness,
)
from pipeline.kpi_definition_revisions import (
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionComparabilityRevision,
    KpiDefinitionRelationKind,
    current_kpi_definition_comparability_revision,
    lookup_revision_reported_value,
    persist_kpi_definition_comparability_revision,
)
from runtime.deterministic import canonical_json_ascii_default_str as _canonical_json
from runtime.deterministic import sha256_text as _sha256


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str) -> Generator[None]:
    conn.execute(f"SAVEPOINT {name}")  # nosec B608 -- fixed internal identifier
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")  # nosec B608
        conn.execute(f"RELEASE SAVEPOINT {name}")  # nosec B608
        raise
    conn.execute(f"RELEASE SAVEPOINT {name}")  # nosec B608


def _next_comparability_chain(
    conn: sqlite3.Connection,
    *,
    predecessor_definition_revision_id: str,
    successor_definition_revision_id: str,
) -> tuple[int, str | None]:
    current = current_kpi_definition_comparability_revision(
        conn,
        predecessor_definition_revision_id=predecessor_definition_revision_id,
        successor_definition_revision_id=successor_definition_revision_id,
    )
    if current is None:
        return 1, None
    return current.revision + 1, current.comparability_revision_id


def admit_verified_rename_comparability(
    conn: sqlite3.Connection,
    *,
    predecessor_definition_revision_id: str,
    successor_definition_revision_id: str,
    comparative_period_end: str | date,
    successor_comparative_value: Decimal,
    source_document_version_id: str,
    source_evidence_node_id: str,
    source_locator: dict[str, object],
    reviewed_by: str,
    effective_at: datetime,
    knowledge_at: datetime,
    recorded_at: datetime,
    fiscal_period_type: FiscalPeriodType | None = None,
    predecessor_value: Decimal | None = None,
    tolerance_policy: OverlapTolerancePolicy | None = None,
    fail_on_mismatch: bool = False,
    comparability_revision_id: str | None = None,
    idempotency_key: str | None = None,
) -> KpiDefinitionComparabilityRevision:
    """Verify comparative prior-period overlap and admit a continuous rename or fail closed."""
    resolved_pred = (
        predecessor_value
        if predecessor_value is not None
        else lookup_revision_reported_value(
            conn,
            kpi_definition_revision_id=predecessor_definition_revision_id,
            period_end=comparative_period_end,
            fiscal_period_type=fiscal_period_type,
        )
    )
    if resolved_pred is None:
        raise OverlapVerificationError(
            "predecessor fact value is unavailable for comparative overlap verification"
        )
    ok, witness, mismatch_reason = evaluate_overlap_witness(
        period_end=comparative_period_end,
        predecessor_value=resolved_pred,
        successor_value=successor_comparative_value,
        source_locator=source_locator,
        fiscal_period_type=fiscal_period_type,
        policy=tolerance_policy,
    )
    if not ok or witness is None:
        if fail_on_mismatch:
            raise OverlapVerificationError(mismatch_reason or "rename overlap verification failed")
        relation_kind = KpiDefinitionRelationKind.REDEFINED
        disposition = KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
        reason_code = "rename_overlap_mismatch"
        bridge_spec: KpiComparabilityBridgeSpec | None = None
    else:
        relation_kind = KpiDefinitionRelationKind.RENAMED
        disposition = KpiDefinitionComparabilityDisposition.CONTINUOUS
        reason_code = "verified_rename_overlap"
        bridge_spec = RenameOverlapBridge(overlap_witness=witness)

    rev_num, supersedes_id = _next_comparability_chain(
        conn,
        predecessor_definition_revision_id=predecessor_definition_revision_id,
        successor_definition_revision_id=successor_definition_revision_id,
    )
    digest = _sha256(
        _canonical_json(
            {
                "bridge": None if bridge_spec is None else bridge_spec.model_dump(mode="json"),
                "disposition": disposition.value,
                "predecessor": predecessor_definition_revision_id,
                "relation_kind": relation_kind.value,
                "revision": rev_num,
                "successor": successor_definition_revision_id,
            }
        )
    )[:24]
    return persist_kpi_definition_comparability_revision(
        conn,
        KpiDefinitionComparabilityRevision(
            comparability_revision_id=comparability_revision_id or f"kcr-rename-{digest}",
            idempotency_key=idempotency_key or f"kcr-rename-key:{digest}",
            predecessor_definition_revision_id=predecessor_definition_revision_id,
            successor_definition_revision_id=successor_definition_revision_id,
            revision=rev_num,
            supersedes_comparability_revision_id=supersedes_id,
            relation_kind=relation_kind,
            disposition=disposition,
            reason_code=reason_code,
            reviewed_by=reviewed_by,
            source_document_version_id=source_document_version_id,
            source_evidence_node_id=source_evidence_node_id,
            source_locator=source_locator,
            bridge_spec=bridge_spec,
            effective_at=effective_at,
            knowledge_at=knowledge_at,
            recorded_at=recorded_at,
        ),
    )


def admit_split_scale_bridge(
    conn: sqlite3.Connection,
    *,
    predecessor_definition_revision_id: str,
    successor_definition_revision_id: str,
    scale_numerator: int,
    scale_denominator: int = 1,
    applies_to: SplitScaleAppliesTo,
    effective_date: str | date,
    comparative_period_end: str | date,
    successor_comparative_value: Decimal,
    source_document_version_id: str,
    source_evidence_node_id: str,
    source_locator: dict[str, object],
    reviewed_by: str,
    effective_at: datetime,
    knowledge_at: datetime,
    recorded_at: datetime,
    fiscal_period_type: FiscalPeriodType | None = None,
    predecessor_value: Decimal | None = None,
    tolerance_policy: OverlapTolerancePolicy | None = None,
    comparability_revision_id: str | None = None,
    idempotency_key: str | None = None,
) -> KpiDefinitionComparabilityRevision:
    """Verify stock-split scaling on a comparative prior period and persist a SplitScaleBridge."""
    resolved_pred = (
        predecessor_value
        if predecessor_value is not None
        else lookup_revision_reported_value(
            conn,
            kpi_definition_revision_id=predecessor_definition_revision_id,
            period_end=comparative_period_end,
            fiscal_period_type=fiscal_period_type,
        )
    )
    if resolved_pred is None:
        raise OverlapVerificationError(
            "predecessor fact value is unavailable for split-scale overlap verification"
        )
    num = Decimal(scale_numerator)
    den = Decimal(scale_denominator)
    expected = (
        (resolved_pred * den) / num
        if applies_to == "per_share_divide"
        else (resolved_pred * num) / den
    )
    witness = build_overlap_witness(
        period_end=comparative_period_end,
        predecessor_value=resolved_pred,
        successor_value=successor_comparative_value,
        expected_value=expected,
        source_locator=source_locator,
        fiscal_period_type=fiscal_period_type,
        policy=tolerance_policy,
    )
    iso_effective = (
        effective_date.isoformat() if isinstance(effective_date, date) else str(effective_date)[:10]
    )
    bridge = SplitScaleBridge(
        scale_numerator=scale_numerator,
        scale_denominator=scale_denominator,
        applies_to=applies_to,
        effective_date=iso_effective,
        overlap_witness=witness,
    )
    rev_num, supersedes_id = _next_comparability_chain(
        conn,
        predecessor_definition_revision_id=predecessor_definition_revision_id,
        successor_definition_revision_id=successor_definition_revision_id,
    )
    digest = _sha256(
        _canonical_json(
            {
                "bridge": bridge.model_dump(mode="json"),
                "predecessor": predecessor_definition_revision_id,
                "revision": rev_num,
                "successor": successor_definition_revision_id,
            }
        )
    )[:24]
    return persist_kpi_definition_comparability_revision(
        conn,
        KpiDefinitionComparabilityRevision(
            comparability_revision_id=comparability_revision_id or f"kcr-split-{digest}",
            idempotency_key=idempotency_key or f"kcr-split-key:{digest}",
            predecessor_definition_revision_id=predecessor_definition_revision_id,
            successor_definition_revision_id=successor_definition_revision_id,
            revision=rev_num,
            supersedes_comparability_revision_id=supersedes_id,
            relation_kind=KpiDefinitionRelationKind.RESTATED,
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
            reason_code=f"stock_split_{scale_numerator}_for_{scale_denominator}",
            reviewed_by=reviewed_by,
            source_document_version_id=source_document_version_id,
            source_evidence_node_id=source_evidence_node_id,
            source_locator=source_locator,
            bridge_spec=bridge,
            effective_at=effective_at,
            knowledge_at=knowledge_at,
            recorded_at=recorded_at,
        ),
    )


def admit_additive_composite_bridge(
    conn: sqlite3.Connection,
    *,
    direction: AdditiveBridgeDirection,
    member_definition_revision_ids: Sequence[str],
    composite_definition_revision_id: str,
    comparative_period_end: str | date,
    composite_comparative_value: Decimal,
    source_document_version_id: str,
    source_evidence_node_id: str,
    source_locator: dict[str, object],
    reviewed_by: str,
    effective_at: datetime,
    knowledge_at: datetime,
    recorded_at: datetime,
    component_values: Mapping[str, Decimal] | None = None,
    fiscal_period_type: FiscalPeriodType | None = None,
    tolerance_policy: OverlapTolerancePolicy | None = None,
) -> tuple[KpiDefinitionComparabilityRevision, ...]:
    """Verify additive sum equality and atomically persist pairwise composite bridge edges."""
    members = tuple(member_definition_revision_ids)
    if len(members) < 2 or len(set(members)) != len(members):
        raise ValueError(
            "additive composite bridge requires at least two distinct member revisions"
        )
    resolved_components: dict[str, Decimal] = {}
    for member_id in members:
        if component_values is not None and member_id in component_values:
            resolved_components[member_id] = component_values[member_id]
        else:
            looked_up = lookup_revision_reported_value(
                conn,
                kpi_definition_revision_id=member_id,
                period_end=comparative_period_end,
                fiscal_period_type=fiscal_period_type,
            )
            if looked_up is None:
                raise OverlapVerificationError(
                    f"missing member fact value for composite bridge member {member_id}"
                )
            resolved_components[member_id] = looked_up
    member_sum = sum(resolved_components.values(), Decimal("0"))
    if direction == "combine":
        witness = build_overlap_witness(
            period_end=comparative_period_end,
            predecessor_value=member_sum,
            successor_value=composite_comparative_value,
            expected_value=member_sum,
            source_locator=source_locator,
            fiscal_period_type=fiscal_period_type,
            policy=tolerance_policy,
        )
        relation_kind = KpiDefinitionRelationKind.COMBINED
        reason_code = "verified_additive_combine"
    else:
        witness = build_overlap_witness(
            period_end=comparative_period_end,
            predecessor_value=composite_comparative_value,
            successor_value=member_sum,
            expected_value=composite_comparative_value,
            source_locator=source_locator,
            fiscal_period_type=fiscal_period_type,
            policy=tolerance_policy,
        )
        relation_kind = KpiDefinitionRelationKind.SPLIT
        reason_code = "verified_additive_split"

    bridge = AdditiveCompositeBridge(
        direction=direction,
        composite_definition_revision_id=composite_definition_revision_id,
        member_definition_revision_ids=members,
        component_values=resolved_components,
        overlap_witness=witness,
    )
    persisted: list[KpiDefinitionComparabilityRevision] = []
    with _savepoint(conn, "admit_additive_composite_bridge"):
        for member_id in members:
            pred_id, succ_id = (
                (member_id, composite_definition_revision_id)
                if direction == "combine"
                else (composite_definition_revision_id, member_id)
            )
            rev_num, supersedes_id = _next_comparability_chain(
                conn,
                predecessor_definition_revision_id=pred_id,
                successor_definition_revision_id=succ_id,
            )
            digest = _sha256(
                _canonical_json(
                    {
                        "bridge": bridge.model_dump(mode="json"),
                        "predecessor": pred_id,
                        "revision": rev_num,
                        "successor": succ_id,
                    }
                )
            )[:24]
            edge = persist_kpi_definition_comparability_revision(
                conn,
                KpiDefinitionComparabilityRevision(
                    comparability_revision_id=f"kcr-{direction}-{digest}",
                    idempotency_key=f"kcr-{direction}-key:{digest}",
                    predecessor_definition_revision_id=pred_id,
                    successor_definition_revision_id=succ_id,
                    revision=rev_num,
                    supersedes_comparability_revision_id=supersedes_id,
                    relation_kind=relation_kind,
                    disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
                    reason_code=reason_code,
                    reviewed_by=reviewed_by,
                    source_document_version_id=source_document_version_id,
                    source_evidence_node_id=source_evidence_node_id,
                    source_locator=source_locator,
                    bridge_spec=bridge,
                    effective_at=effective_at,
                    knowledge_at=knowledge_at,
                    recorded_at=recorded_at,
                ),
            )
            persisted.append(edge)
    return tuple(persisted)


def admit_recast_comparative_bridge(
    conn: sqlite3.Connection,
    *,
    predecessor_definition_revision_id: str,
    successor_definition_revision_id: str,
    recast_reason: str,
    scope_change_summary: str,
    source_document_version_id: str,
    source_evidence_node_id: str,
    source_locator: dict[str, object],
    reviewed_by: str,
    effective_at: datetime,
    knowledge_at: datetime,
    recorded_at: datetime,
    comparative_periods_present: Sequence[str | date] = (),
    comparability_revision_id: str | None = None,
    idempotency_key: str | None = None,
) -> KpiDefinitionComparabilityRevision:
    """Persist a scope-change recast bridge that restricts YoY to recast comparatives."""
    normalized_periods = tuple(
        p.isoformat() if isinstance(p, date) else str(p)[:10] for p in comparative_periods_present
    )
    bridge = RecastComparativeBridge(
        recast_reason=recast_reason,
        scope_change_summary=scope_change_summary,
        comparative_periods_present=normalized_periods,
    )
    rev_num, supersedes_id = _next_comparability_chain(
        conn,
        predecessor_definition_revision_id=predecessor_definition_revision_id,
        successor_definition_revision_id=successor_definition_revision_id,
    )
    digest = _sha256(
        _canonical_json(
            {
                "bridge": bridge.model_dump(mode="json"),
                "predecessor": predecessor_definition_revision_id,
                "revision": rev_num,
                "successor": successor_definition_revision_id,
            }
        )
    )[:24]
    return persist_kpi_definition_comparability_revision(
        conn,
        KpiDefinitionComparabilityRevision(
            comparability_revision_id=comparability_revision_id or f"kcr-recast-{digest}",
            idempotency_key=idempotency_key or f"kcr-recast-key:{digest}",
            predecessor_definition_revision_id=predecessor_definition_revision_id,
            successor_definition_revision_id=successor_definition_revision_id,
            revision=rev_num,
            supersedes_comparability_revision_id=supersedes_id,
            relation_kind=KpiDefinitionRelationKind.RECAST,
            disposition=KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
            reason_code=f"recast:{recast_reason}"[:128],
            reviewed_by=reviewed_by,
            source_document_version_id=source_document_version_id,
            source_evidence_node_id=source_evidence_node_id,
            source_locator=source_locator,
            bridge_spec=bridge,
            effective_at=effective_at,
            knowledge_at=knowledge_at,
            recorded_at=recorded_at,
        ),
    )
