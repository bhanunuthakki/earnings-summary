"""Resolve a requested KPI label to the canonical ``kpi_definitions.name``.

Shared by every consumer that joins a human-supplied KPI label — a holdings
``chart_priorities`` entry, a ``break_rules`` ``kpi_name``, a tier-N ledger name
— to the stored ``kpi_definitions`` row. KPI names fragment over time: the
issuer's IR spreadsheet ingests "Monthly ARPAC (USD)" while an older LLM brief
stored a bare "Monthly ARPAC", leaving two definitions for the same metric where
one is fully populated and the other near-empty.

Exact-name matching picks whichever spelling the label happens to use, so a
short holdings label can resolve to the sparse duplicate and the consumer then
charts / evaluates an almost-empty series. This module instead matches on a
parenthetical-insensitive normalized name and prefers the definition carrying
the MOST observations, so a fragmented duplicate can never shadow the canonical
series.

Originally ``financials._resolve_kpi_definition_name`` / ``_normalize_kpi_name``
(PR #195); extracted here so the §3 chart loader, the §2 break-rule ledger, and
the break-rule evaluator all share one resolver instead of three exact-match
lookups that drift apart.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.facts import Unit
from models.kpis import DefinitionOrigin
from models.unit_convert import same_family
from pipeline.kpi_definition_revisions import (
    KpiDefinitionComparabilityDisposition,
    KpiDefinitionLifecycle,
    KpiDefinitionStatus,
    kpi_definition_comparability_as_known,
    kpi_definition_revision_as_known,
    kpi_definition_revision_by_id,
)
from provenance.financial_fact_resolution import (
    HistoricalFactAuthorityUnavailableError,
    canonical_fact_relation,
    canonical_fact_row_ids_as_known,
)

# kpi_facts.fiscal_period_type values that denote a quarterly observation. The
# §3 chart cadence is quarterly, so the chart loader measures richness over
# these buckets only; the break-rule paths query every period type and pass
# period_types=None so "most observations" is measured over the rows they read.
QUARTERLY_FACT_PERIOD_TYPES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")

# kpi_facts.fiscal_period_type values that denote an annual (fiscal-year-end)
# observation — the cadence-aware twin of QUARTERLY_FACT_PERIOD_TYPES. Matches the
# financials annual set (report.sections._common.ANNUAL_PERIOD_TYPES) so an annual
# KPI series aligns with the annual line-item axis. Consumers select these rows
# when a definition's reporting_cadence is 'annual' (see reporting_cadence_for).
ANNUAL_FACT_PERIOD_TYPES: tuple[str, ...] = ("FY", "annual")

_REVISION_DEFINITION_COLUMNS = frozenset(
    {
        "kpi_definition_revision_id",
        "idempotency_key",
        "kpi_definition_id",
        "reporting_entity_id",
        "scope_security_id",
        "revision",
        "supersedes_definition_revision_id",
        "status",
        "lifecycle",
        "reported_label",
        "reported_definition_text",
        "definition_text_status",
        "period_kind",
        "stock_flow_behavior",
        "unit_family",
        "unit_key",
        "unit_scale",
        "currency_disposition",
        "currency",
        "accounting_basis",
        "consolidation_scope",
        "dimensions_json",
        "source_document_version_id",
        "source_evidence_node_id",
        "source_locator_json",
        "source_locator_sha256",
        "reason_code",
        "reviewed_by",
        "commitment_json",
        "commitment_sha256",
        "effective_at",
        "knowledge_at",
        "recorded_at",
    }
)
_REVISION_COMPARABILITY_COLUMNS = frozenset(
    {
        "comparability_revision_id",
        "idempotency_key",
        "predecessor_definition_revision_id",
        "successor_definition_revision_id",
        "revision",
        "supersedes_comparability_revision_id",
        "relation_kind",
        "disposition",
        "reason_code",
        "reviewed_by",
        "source_document_version_id",
        "source_evidence_node_id",
        "source_locator_json",
        "source_locator_sha256",
        "commitment_json",
        "commitment_sha256",
        "effective_at",
        "knowledge_at",
        "recorded_at",
    }
)
_REVISION_CONTEXT_COLUMNS = frozenset(
    {
        "id",
        "kpi_fact_id",
        "supersedes_context_id",
        "metric_name_as_reported",
        "reported_period_end",
        "accounting_basis",
        "consolidation_scope",
        "dimensions_json",
        "unit_scale",
        "status",
        "knowledge_at",
        "created_at",
        "kpi_definition_revision_id",
    }
)
_REVISION_FACT_COLUMNS = frozenset(
    {
        "id",
        "ticker",
        "period_end",
        "fiscal_period_type",
        "kpi_definition_id",
        "value",
        "currency",
        "unit",
        "source_doc_id",
        "confidence",
        "extracted_by",
        "locator",
        "computed_from",
    }
)


class KpiRevisionSeriesStatus(StrEnum):
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_BREAK = "eligible_with_break"
    LEGACY_UNBOUND = "legacy_unbound"
    QUARANTINED_DEFINITION = "quarantined_definition"
    DISCONTINUED = "discontinued"
    HISTORICAL_FACT_AUTHORITY_UNAVAILABLE = "historical_fact_authority_unavailable"


class KpiRevisionSeriesExclusionReason(StrEnum):
    LEGACY_UNBOUND = "legacy_unbound"
    MISSING_SEMANTIC_HEAD = "missing_semantic_head"
    SEMANTIC_NOT_ADMITTED = "semantic_not_admitted"
    QUARANTINED_DEFINITION = "quarantined_definition"
    DISCONTINUED_DEFINITION = "discontinued_definition"
    DEFINITION_CONTEXT_MISMATCH = "definition_context_mismatch"
    NO_EXPLICIT_COMPARABILITY = "no_explicit_comparability"
    NOT_COMPARABLE = "not_comparable"
    HISTORICAL_FACT_AUTHORITY_UNAVAILABLE = "historical_fact_authority_unavailable"


class KpiRevisionSeriesBreak(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comparability_revision_id: str = Field(min_length=1, max_length=128)
    related_definition_revision_id: str = Field(min_length=1, max_length=128)
    relation_kind: str = Field(min_length=1, max_length=32)


class KpiRevisionSeriesComparability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comparability_revision_id: str = Field(min_length=1, max_length=128)
    related_definition_revision_id: str = Field(min_length=1, max_length=128)
    relation_kind: str = Field(min_length=1, max_length=32)
    disposition: KpiDefinitionComparabilityDisposition


class KpiRevisionSeriesExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: KpiRevisionSeriesExclusionReason
    count: int = Field(gt=0)
    fact_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _exact_fact_id_count(self) -> KpiRevisionSeriesExclusion:
        if self.fact_ids and (
            self.fact_ids != tuple(sorted(set(self.fact_ids))) or self.count != len(self.fact_ids)
        ):
            raise ValueError("exclusion fact_ids must be sorted, unique, and match count")
        return self


class KpiRevisionSeriesResolution(BaseModel):
    """Shadow result; legacy readers do not consume this in BHA-100 slice one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: KpiRevisionSeriesStatus
    kpi_definition_id: int = Field(gt=0)
    anchor_definition_revision_id: str | None = None
    included_definition_revision_ids: tuple[str, ...] = ()
    comparability_revisions: tuple[KpiRevisionSeriesComparability, ...] = ()
    eligible_fact_ids: tuple[int, ...] = ()
    breaks: tuple[KpiRevisionSeriesBreak, ...] = ()
    exclusions: tuple[KpiRevisionSeriesExclusion, ...] = ()


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _database_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _unbound_revision_resolution(kpi_definition_id: int) -> KpiRevisionSeriesResolution:
    return KpiRevisionSeriesResolution(
        status=KpiRevisionSeriesStatus.LEGACY_UNBOUND,
        kpi_definition_id=kpi_definition_id,
        exclusions=(
            KpiRevisionSeriesExclusion(
                reason=KpiRevisionSeriesExclusionReason.LEGACY_UNBOUND,
                count=1,
            ),
        ),
    )


def _historical_authority_unavailable_resolution(
    kpi_definition_id: int,
    *,
    anchor_definition_revision_id: str | None = None,
    included_definition_revision_ids: tuple[str, ...] = (),
    comparability_revisions: tuple[KpiRevisionSeriesComparability, ...] = (),
    breaks: tuple[KpiRevisionSeriesBreak, ...] = (),
) -> KpiRevisionSeriesResolution:
    return KpiRevisionSeriesResolution(
        status=KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
        kpi_definition_id=kpi_definition_id,
        anchor_definition_revision_id=anchor_definition_revision_id,
        included_definition_revision_ids=included_definition_revision_ids,
        comparability_revisions=comparability_revisions,
        breaks=breaks,
        exclusions=(
            KpiRevisionSeriesExclusion(
                reason=KpiRevisionSeriesExclusionReason.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
                count=1,
            ),
        ),
    )


def _context_matches_revision(row: sqlite3.Row, revision_id: str) -> bool:
    return (
        str(row["definition_revision_id"]) == revision_id
        and int(row["definition_root_id"]) == int(row["kpi_definition_id"])
        and str(row["reported_period_end"])[:10] == str(row["fact_period_end"])[:10]
        and str(row["definition_reported_label"]) == str(row["metric_name_as_reported"])
        and str(row["definition_accounting_basis"]) == str(row["accounting_basis"])
        and str(row["definition_consolidation_scope"]) == str(row["consolidation_scope"])
        and str(row["definition_dimensions_json"]) == str(row["dimensions_json"])
        and str(row["definition_unit_scale"]) == str(row["unit_scale"])
        and str(row["definition_unit_key"]) == str(row["fact_unit"])
        and row["definition_currency"] == row["fact_currency"]
        and str(row["definition_currency_disposition"])
        == ("explicit" if row["fact_currency"] is not None else "not_applicable")
    )


def resolve_revision_aware_kpi_series(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
) -> KpiRevisionSeriesResolution:
    """Resolve in one caller-aware SQLite read snapshot."""

    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        return _resolve_revision_aware_kpi_series(
            conn,
            kpi_definition_id=kpi_definition_id,
            effective_at=effective_at,
            known_at=known_at,
        )
    finally:
        if owns_snapshot:
            conn.rollback()


def _resolve_revision_aware_kpi_series(
    conn: sqlite3.Connection,
    *,
    kpi_definition_id: int,
    effective_at: datetime,
    known_at: datetime,
) -> KpiRevisionSeriesResolution:
    """Resolve an exact revision series for shadow comparison only.

    Membership is anchored by integer registry identity and direct reviewed
    comparability rows. Names are never read, normalized, or used to widen the
    result. Missing rollout bindings return an explicit ``legacy_unbound``.
    """

    definition_columns = _table_columns(conn, "kpi_definition_revisions")
    if not definition_columns:
        return _unbound_revision_resolution(kpi_definition_id)
    if (
        not _REVISION_DEFINITION_COLUMNS.issubset(definition_columns)
        or not _REVISION_CONTEXT_COLUMNS.issubset(
            _table_columns(conn, "kpi_fact_semantic_contexts")
        )
        or not _REVISION_COMPARABILITY_COLUMNS.issubset(
            _table_columns(conn, "kpi_definition_comparability_revisions")
        )
        or not _REVISION_FACT_COLUMNS.issubset(_table_columns(conn, "kpi_facts"))
    ):
        return _historical_authority_unavailable_resolution(kpi_definition_id)
    anchor = kpi_definition_revision_as_known(
        conn,
        kpi_definition_id=kpi_definition_id,
        effective_at=effective_at,
        known_at=known_at,
    )
    if anchor is None:
        return _unbound_revision_resolution(kpi_definition_id)
    anchor_id = anchor.kpi_definition_revision_id
    if anchor.status is KpiDefinitionStatus.QUARANTINED:
        return KpiRevisionSeriesResolution(
            status=KpiRevisionSeriesStatus.QUARANTINED_DEFINITION,
            kpi_definition_id=kpi_definition_id,
            anchor_definition_revision_id=anchor_id,
        )
    if anchor.lifecycle is KpiDefinitionLifecycle.DISCONTINUED:
        return KpiRevisionSeriesResolution(
            status=KpiRevisionSeriesStatus.DISCONTINUED,
            kpi_definition_id=kpi_definition_id,
            anchor_definition_revision_id=anchor_id,
        )

    pair_rows = conn.execute(
        "SELECT DISTINCT predecessor_definition_revision_id,successor_definition_revision_id "
        "FROM kpi_definition_comparability_revisions "
        "WHERE predecessor_definition_revision_id=? OR successor_definition_revision_id=?",
        (anchor_id, anchor_id),
    ).fetchall()
    disposition_by_revision: dict[str, KpiDefinitionComparabilityDisposition] = {}
    included_ids = {anchor_id}
    related_ids: set[str] = set()
    breaks: list[KpiRevisionSeriesBreak] = []
    comparability_revisions: list[KpiRevisionSeriesComparability] = []
    for pair in pair_rows:
        predecessor_id = str(pair[0])
        successor_id = str(pair[1])
        related_id = successor_id if predecessor_id == anchor_id else predecessor_id
        relation = kpi_definition_comparability_as_known(
            conn,
            first_definition_revision_id=anchor_id,
            second_definition_revision_id=related_id,
            effective_at=effective_at,
            known_at=known_at,
        )
        if relation is None:
            continue
        related = kpi_definition_revision_by_id(
            conn,
            kpi_definition_revision_id=related_id,
        )
        if (
            related is None
            or related.status is not KpiDefinitionStatus.ADMITTED
            or related.lifecycle is not KpiDefinitionLifecycle.ACTIVE
            or related.effective_at > effective_at
            or related.knowledge_at > known_at
            or related.recorded_at > known_at
        ):
            continue
        related_ids.add(related_id)
        disposition_by_revision[related_id] = relation.disposition
        comparability_revisions.append(
            KpiRevisionSeriesComparability(
                comparability_revision_id=relation.comparability_revision_id,
                related_definition_revision_id=related_id,
                relation_kind=relation.relation_kind.value,
                disposition=relation.disposition,
            )
        )
        if relation.disposition in {
            KpiDefinitionComparabilityDisposition.CONTINUOUS,
            KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK,
        }:
            included_ids.add(related_id)
        if relation.disposition is KpiDefinitionComparabilityDisposition.COMPARABLE_WITH_BREAK:
            breaks.append(
                KpiRevisionSeriesBreak(
                    comparability_revision_id=relation.comparability_revision_id,
                    related_definition_revision_id=related_id,
                    relation_kind=relation.relation_kind.value,
                )
            )

    same_root_rows = conn.execute(
        "SELECT kpi_definition_revision_id FROM kpi_definition_revisions "
        "WHERE kpi_definition_id=? AND datetime(effective_at)<=datetime(?) "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?)",
        (
            kpi_definition_id,
            effective_at.isoformat(),
            known_at.isoformat(),
            known_at.isoformat(),
        ),
    ).fetchall()
    candidate_revision_ids = related_ids | {str(row[0]) for row in same_root_rows} | {anchor_id}
    candidate_roots = {kpi_definition_id}
    for revision_id in related_ids:
        related = kpi_definition_revision_by_id(conn, kpi_definition_revision_id=revision_id)
        if related is None:
            raise RuntimeError("eligible related KPI definition disappeared")
        candidate_roots.add(related.kpi_definition_id)
    root_placeholders = ",".join("?" for _ in candidate_roots)
    try:
        canonical_fact_ids = canonical_fact_row_ids_as_known(
            conn,
            fact_table="kpi_facts",
            effective_at=effective_at,
            known_at=known_at,
            concept_keys=tuple(f"kpi_definition:{root_id}" for root_id in sorted(candidate_roots)),
        )
    except HistoricalFactAuthorityUnavailableError:
        return _historical_authority_unavailable_resolution(
            kpi_definition_id,
            anchor_definition_revision_id=anchor_id,
            included_definition_revision_ids=tuple(sorted(included_ids)),
            comparability_revisions=tuple(
                sorted(
                    comparability_revisions,
                    key=lambda item: item.related_definition_revision_id,
                )
            ),
            breaks=tuple(sorted(breaks, key=lambda item: item.related_definition_revision_id)),
        )
    fact_id_predicate = "0"
    fact_id_parameters: tuple[object, ...] = ()
    if canonical_fact_ids:
        fact_id_predicate = ",".join("?" for _ in canonical_fact_ids)
        fact_id_predicate = f"fact.id IN ({fact_id_predicate})"
        fact_id_parameters = tuple(canonical_fact_ids)
    fact_rows = conn.execute(
        "SELECT fact.id,fact.kpi_definition_id,fact.period_end AS fact_period_end,"
        "fact.unit AS fact_unit,"
        "fact.currency AS fact_currency,context.status AS context_status,"
        "context.metric_name_as_reported,context.reported_period_end,context.accounting_basis,"
        "context.consolidation_scope,context.dimensions_json,context.unit_scale,"
        "context.kpi_definition_revision_id,"
        "definition.kpi_definition_revision_id AS definition_revision_id,"
        "definition.kpi_definition_id AS definition_root_id,"
        "definition.status AS definition_status,definition.lifecycle AS definition_lifecycle,"
        "definition.reported_label AS definition_reported_label,"
        "definition.accounting_basis AS definition_accounting_basis,"
        "definition.consolidation_scope AS definition_consolidation_scope,"
        "definition.dimensions_json AS definition_dimensions_json,"
        "definition.unit_scale AS definition_unit_scale,"
        "definition.unit_key AS definition_unit_key,"
        "definition.currency AS definition_currency,"
        "definition.currency_disposition AS definition_currency_disposition,"
        "definition.effective_at AS definition_effective_at,"
        "definition.knowledge_at AS definition_knowledge_at,"
        "definition.recorded_at AS definition_recorded_at "
        "FROM kpi_facts fact "
        "LEFT JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id "
        "AND datetime(context.knowledge_at)<=datetime(?) "
        "AND datetime(context.created_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts context_successor "
        "WHERE context_successor.supersedes_context_id=context.id "
        "AND datetime(context_successor.knowledge_at)<=datetime(?) "
        "AND datetime(context_successor.created_at)<=datetime(?)) "
        "LEFT JOIN kpi_definition_revisions definition ON "
        "definition.kpi_definition_revision_id=context.kpi_definition_revision_id "
        f"WHERE {fact_id_predicate} "  # nosec B608 -- integer ids from canonical resolver
        "AND datetime(fact.period_end)<=datetime(?) "
        f"AND fact.kpi_definition_id IN ({root_placeholders}) ORDER BY fact.id",  # nosec B608
        (
            known_at.isoformat(),
            known_at.isoformat(),
            known_at.isoformat(),
            known_at.isoformat(),
            *fact_id_parameters,
            effective_at.isoformat(),
            *sorted(candidate_roots),
        ),
    ).fetchall()
    eligible_fact_ids: list[int] = []
    exclusion_fact_ids: dict[KpiRevisionSeriesExclusionReason, list[int]] = {}

    def exclude(reason: KpiRevisionSeriesExclusionReason, fact_id: int) -> None:
        exclusion_fact_ids.setdefault(reason, []).append(fact_id)

    for row in fact_rows:
        if row["context_status"] is None:
            exclude(KpiRevisionSeriesExclusionReason.MISSING_SEMANTIC_HEAD, int(row["id"]))
            continue
        if str(row["context_status"]) != "admitted":
            exclude(KpiRevisionSeriesExclusionReason.SEMANTIC_NOT_ADMITTED, int(row["id"]))
            continue
        if row["kpi_definition_revision_id"] is None:
            exclude(KpiRevisionSeriesExclusionReason.LEGACY_UNBOUND, int(row["id"]))
            continue
        if (
            row["definition_revision_id"] is not None
            and row["definition_currency"] != row["fact_currency"]
        ):
            return _historical_authority_unavailable_resolution(
                kpi_definition_id,
                anchor_definition_revision_id=anchor_id,
                included_definition_revision_ids=tuple(sorted(included_ids)),
                comparability_revisions=tuple(
                    sorted(
                        comparability_revisions,
                        key=lambda item: item.related_definition_revision_id,
                    )
                ),
                breaks=tuple(sorted(breaks, key=lambda item: item.related_definition_revision_id)),
            )
        revision_id = str(row["kpi_definition_revision_id"])
        if revision_id not in candidate_revision_ids or row["definition_revision_id"] is None:
            exclude(KpiRevisionSeriesExclusionReason.DEFINITION_CONTEXT_MISMATCH, int(row["id"]))
            continue
        if str(row["definition_status"]) != "admitted":
            exclude(KpiRevisionSeriesExclusionReason.QUARANTINED_DEFINITION, int(row["id"]))
            continue
        if str(row["definition_lifecycle"]) == "discontinued":
            exclude(KpiRevisionSeriesExclusionReason.DISCONTINUED_DEFINITION, int(row["id"]))
            continue
        if (
            _database_datetime(row["definition_knowledge_at"]) > known_at
            or _database_datetime(row["definition_recorded_at"]) > known_at
            or _database_datetime(row["definition_effective_at"]) > effective_at
        ):
            exclude(KpiRevisionSeriesExclusionReason.DEFINITION_CONTEXT_MISMATCH, int(row["id"]))
            continue
        if not _context_matches_revision(row, revision_id):
            exclude(KpiRevisionSeriesExclusionReason.DEFINITION_CONTEXT_MISMATCH, int(row["id"]))
            continue
        if revision_id in included_ids:
            eligible_fact_ids.append(int(row["id"]))
            continue
        disposition = disposition_by_revision.get(revision_id)
        exclude(
            KpiRevisionSeriesExclusionReason.NOT_COMPARABLE
            if disposition is KpiDefinitionComparabilityDisposition.NOT_COMPARABLE
            else KpiRevisionSeriesExclusionReason.NO_EXPLICIT_COMPARABILITY,
            int(row["id"]),
        )
    exclusions = tuple(
        KpiRevisionSeriesExclusion(
            reason=reason,
            count=len(fact_ids),
            fact_ids=tuple(sorted(fact_ids)),
        )
        for reason, fact_ids in sorted(exclusion_fact_ids.items(), key=lambda item: item[0].value)
    )
    return KpiRevisionSeriesResolution(
        status=(
            KpiRevisionSeriesStatus.ELIGIBLE_WITH_BREAK
            if breaks
            else KpiRevisionSeriesStatus.ELIGIBLE
        ),
        kpi_definition_id=kpi_definition_id,
        anchor_definition_revision_id=anchor_id,
        included_definition_revision_ids=tuple(sorted(included_ids)),
        comparability_revisions=tuple(
            sorted(
                comparability_revisions,
                key=lambda item: item.related_definition_revision_id,
            )
        ),
        eligible_fact_ids=tuple(eligible_fact_ids),
        breaks=tuple(sorted(breaks, key=lambda item: item.related_definition_revision_id)),
        exclusions=exclusions,
    )


def normalize_kpi_name(name: str) -> str:
    """Return the conservative semantic match key used on both read and write.

    "Monthly ARPAC (USD)" and "Monthly ARPAC" both normalize to "monthly arpac"
    because USD is a unit-only qualifier. Semantic qualifiers such as ``(GAAP)``,
    ``(non-GAAP)``, ``(Brazil)``, ``(active)``, or ``(consolidated)`` remain part
    of the key. A duplicate is recoverable; a false merge corrupts the series.
    """
    return _canonical_match_key(name)


# The capture-all extractor (table_extractors.generic_xbrl_capture._build_name)
# qualifies a captured metric as ``section — axis — leaf`` joined by this exact
# separator; the LEAF after the last one is the metric itself. Mirrors
# ask.grounding._KPI_QUALIFIER_SEP so the picker (kpi_group_key) and the ask
# name-match agree on what "the same metric" is.
_KPI_QUALIFIER_SEP = " — "


def kpi_group_key(name: str) -> str:
    """The de-fragmentation key: surface variants of ONE metric share it.

    Mirrors the ask leaf logic (``ask.grounding._label_match_keys``) then folds
    with :func:`normalize_kpi_name`. Peel a ``section — axis —`` qualifier to
    its LEAF when that leaf is a distinct ≥2-word phrase (a generic single-word
    leaf — "Total" / "Net" — is NOT peeled, so distinct metrics that merely
    share it can't false-merge), otherwise key on the whole name.

    Conservative by construction (the §7a.4 invariant: a duplicate token is
    acceptable, a false merge is not): only unit / casing / whitespace /
    qualifier-prefix variants collapse; true synonyms ("NIM" vs "Net interest
    margin") never do, because their normalized leaves differ.
    """
    full = normalize_kpi_name(name)
    if _KPI_QUALIFIER_SEP in name:
        leaf = normalize_kpi_name(name.rsplit(_KPI_QUALIFIER_SEP, 1)[-1])
        if leaf and leaf != full and len(leaf.split()) >= 2:
            return leaf
    return full


def resolve_kpi_definition_name(
    conn: sqlite3.Connection,
    ticker: str,
    requested: str,
    *,
    period_types: Sequence[str] | None = None,
) -> str | None:
    """Choose which stored ``kpi_definitions.name`` to use for a requested label.

    Among this ticker's definitions that carry facts (optionally restricted to
    ``period_types``), accept an exact name match or a normalized-equal
    (parenthetical-insensitive) one, then pick the candidate with the MOST
    observations — exactness only breaks ties. This keeps a near-empty
    fragmented duplicate (e.g. a stray "Monthly ARPAC" with 2 rows) from
    shadowing the fully-populated canonical "Monthly ARPAC (USD)".

    ``period_types`` filters the observation COUNT to those fiscal_period_type
    buckets so richness is measured over the rows the caller will actually read:
    the quarterly chart loader passes ``QUARTERLY_FACT_PERIOD_TYPES``; the
    break-rule paths (which query every period type) pass None. Returns None when
    no stored definition — among those carrying facts — matches the label.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer's connection
    already sets it).
    """
    return resolve_kpi_definition_names(
        conn,
        ticker,
        (requested,),
        period_types=period_types,
    )[requested]


def resolve_kpi_definition_names(
    conn: sqlite3.Connection,
    ticker: str,
    requested: Sequence[str],
    *,
    period_types: Sequence[str] | None = None,
) -> dict[str, str | None]:
    """Resolve several labels from one canonical observation-count scan.

    A report commonly resolves every KPI in one issuer ledger. Re-running the
    canonical fact relation once per label multiplies the provenance resolver's
    cost without changing the candidate population. This batch seam preserves
    the single-label ranking contract while evaluating that population once.
    """
    if not requested:
        return {}
    cur = conn.cursor()
    fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
    if period_types:
        placeholders = ",".join("?" * len(period_types))
        cur.execute(
            f"""
            SELECT kd.name AS name, COUNT(*) AS n
            FROM {fact_relation} kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
              AND kf.fiscal_period_type IN ({placeholders})
            GROUP BY kd.name
            """,
            (ticker.upper(), *period_types),
        )
    else:
        cur.execute(
            f"""
            SELECT kd.name AS name, COUNT(*) AS n
            FROM {fact_relation} kf
            JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
            WHERE kf.ticker = ?
            GROUP BY kd.name
            """,
            (ticker.upper(),),
        )
    candidates = [(str(row["name"]), int(row["n"])) for row in cur.fetchall()]
    resolved: dict[str, str | None] = {}
    for label in requested:
        want = normalize_kpi_name(label)
        best_name: str | None = None
        best_rank: tuple[int, int] = (-1, -1)  # (obs_count, exactness)
        for stored, observation_count in candidates:
            if stored == label:
                exactness = 1
            elif normalize_kpi_name(stored) == want:
                exactness = 0
            else:
                continue
            rank = (observation_count, exactness)
            if rank > best_rank:
                best_rank = rank
                best_name = stored
        resolved[label] = best_name
    return resolved


def matching_kpi_definition_ids(
    conn: sqlite3.Connection, ticker: str, requested: str
) -> tuple[int, ...]:
    """All definition IDs in the read-side normalized KPI family.

    Readers must reconcile aliases *before* filtering and window partitioning;
    choosing a single rich definition first leaves a same-period fact under a
    less-populated alias invisible.  This is intentionally read-only and uses
    the existing conservative normalized-name contract.
    """
    want = normalize_kpi_name(requested)
    rows = conn.execute(
        "SELECT id, name, unit FROM kpi_definitions WHERE ticker = ? ORDER BY id",
        (ticker.upper(),),
    ).fetchall()
    family = [row for row in rows if normalize_kpi_name(str(row["name"])) == want]
    if not family:
        return ()
    exact = [row for row in family if str(row["name"]) == requested]
    anchor = exact[0] if exact else family[0]
    anchor_unit = str(anchor["unit"])
    # Names alone are not a fact identity: readers cannot merge an actual-dollar
    # row with a millions-scaled alias and silently alter a displayed value. Nor
    # may a USD alias pull a BRL observation into the same report cell. Currency
    # is a fact (not definition) attribute, so compare each definition's observed
    # currency set; a definition with no observations is harmless to retain.
    fact_columns = {
        str(column["name"]) for column in conn.execute("PRAGMA table_info(kpi_facts)").fetchall()
    }
    if "currency" not in fact_columns:
        return tuple(int(row["id"]) for row in family if str(row["unit"]) == anchor_unit)
    fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
    fact_currency_rows = conn.execute(
        f"SELECT kpi_definition_id, currency FROM {fact_relation} WHERE ticker = ?",
        (ticker.upper(),),
    ).fetchall()
    currencies_by_definition: dict[int, set[str | None]] = {}
    for fact in fact_currency_rows:
        definition_id = int(fact["kpi_definition_id"])
        currencies_by_definition.setdefault(definition_id, set()).add(
            str(fact["currency"]) if fact["currency"] is not None else None
        )
    anchor_currencies = currencies_by_definition.get(int(anchor["id"]), set())
    semantic_columns = {
        str(column["name"])
        for column in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)").fetchall()
    }

    def latest_signature(definition_id: int) -> tuple[str, str, str, str, str] | None:
        signature_fields = (
            "metric_name_as_reported",
            "accounting_basis",
            "consolidation_scope",
            "dimensions_json",
            "unit_scale",
        )
        if not set(signature_fields).issubset(semantic_columns):
            return None
        row = conn.execute(
            "SELECT "  # nosec B608
            + ",".join(f"context.{field}" for field in signature_fields)
            + " "
            f"FROM {fact_relation} fact JOIN kpi_fact_semantic_contexts context "  # nosec B608
            "ON context.kpi_fact_id=fact.id AND NOT EXISTS ("
            "SELECT 1 FROM kpi_fact_semantic_contexts successor "
            "WHERE successor.supersedes_context_id=context.id) "
            "WHERE fact.kpi_definition_id=? AND context.status='admitted' "
            "AND context.publication_lane='current_actual' "
            "ORDER BY fact.period_end DESC,fact.id DESC LIMIT 1",
            (definition_id,),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
        )

    signatures = {int(row["id"]): latest_signature(int(row["id"])) for row in family}
    anchor_signature = signatures[int(anchor["id"])]
    if anchor_signature is None:
        anchor_signature = next((signature for signature in signatures.values() if signature), None)
    return tuple(
        int(row["id"])
        for row in family
        if str(row["unit"]) == anchor_unit
        and (
            not anchor_currencies
            or not currencies_by_definition.get(int(row["id"]))
            or currencies_by_definition[int(row["id"])] == anchor_currencies
        )
        and (
            (anchor_signature is None and signatures[int(row["id"])] is None)
            or signatures[int(row["id"])] == anchor_signature
        )
    )


def semantic_series_identity_sql(
    conn: sqlite3.Connection,
    *,
    fact_alias: str = "kf",
    context_alias: str = "ksc",
    fact_relation: str | None = None,
) -> str:
    """Restrict an admitted series to its latest basis/scope/dimension identity.

    ``fact_relation`` must be the same canonical relation used by the caller's
    outer KPI query. When omitted, resolve it here so anchor selection cannot
    accidentally fall back to raw ``kpi_facts`` while the outer query uses the
    resolved-current view. Any active legacy scalar ``replace`` or ``drop``
    rejects the whole compatible definition-alias family: consumers may resume
    only after a source-reviewed superseding fact has its own admitted head.
    """
    resolved_fact_relation = fact_relation or canonical_fact_relation(conn, "kpi_facts").sql
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)").fetchall()
    }
    required = {
        "metric_name_as_reported",
        "accounting_basis",
        "consolidation_scope",
        "dimensions_json",
        "unit_scale",
        "revision",
        "supersedes_context_id",
        "publication_lane",
    }
    if not required.issubset(columns):
        return "1=1"
    anchor_fact = f"{fact_alias}_identity_fact"
    anchor_context = f"{context_alias}_identity_context"
    successor = f"{anchor_context}_successor"
    signature_fields = (
        "metric_name_as_reported",
        "accounting_basis",
        "consolidation_scope",
        "dimensions_json",
        "unit_scale",
    )
    anchor_relation = (
        f"FROM {resolved_fact_relation} {anchor_fact} "  # nosec B608
        f"JOIN kpi_fact_semantic_contexts {anchor_context} "
        f"ON {anchor_context}.kpi_fact_id={anchor_fact}.id AND NOT EXISTS ("
        f"SELECT 1 FROM kpi_fact_semantic_contexts {successor} WHERE "
        f"{successor}.supersedes_context_id={anchor_context}.id) "
        f"WHERE {anchor_fact}.kpi_definition_id={fact_alias}.kpi_definition_id "
        f"AND {anchor_context}.status='admitted' "
        f"AND {anchor_context}.publication_lane='current_actual'"
    )
    current_signature = (
        "(" + ",".join(f"{context_alias}.{field}" for field in signature_fields) + ")"
    )
    anchor_signature = (
        "(SELECT "
        + ",".join(f"{anchor_context}.{field}" for field in signature_fields)
        + f" {anchor_relation} ORDER BY {anchor_fact}.period_end DESC,"
        + f"{anchor_fact}.id DESC LIMIT 1)"
    )
    qualified = f"{current_signature}={anchor_signature}"
    admitted_anchor_exists = f"EXISTS (SELECT 1 {anchor_relation})"  # nosec B608
    admitted_identity = (
        f"((({context_alias}.id IS NULL OR {context_alias}.status='legacy_unknown') "
        f"AND NOT {admitted_anchor_exists}) OR ({qualified}))"
    )
    override_columns = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(fact_overrides)").fetchall()
    }
    required_override_columns = {
        "ticker",
        "fact_kind",
        "fact_key",
        "action",
        "status",
    }
    if not required_override_columns.issubset(override_columns):
        return admitted_identity
    active_overrides = conn.execute(
        "SELECT DISTINCT ticker,fact_key FROM fact_overrides "
        "WHERE status='active' AND action IN ('replace','drop') AND fact_kind='kpi'"
    ).fetchall()
    blocked_definition_ids: set[int] = set()
    for override in active_overrides:
        blocked_definition_ids.update(
            matching_kpi_definition_ids(
                conn,
                str(override["ticker"]),
                str(override["fact_key"]),
            )
        )
    if not blocked_definition_ids:
        return admitted_identity
    blocked_ids_sql = ",".join(
        str(definition_id) for definition_id in sorted(blocked_definition_ids)
    )
    return f"({admitted_identity}) AND {fact_alias}.kpi_definition_id NOT IN ({blocked_ids_sql})"


def engine_formula_definition(name: str) -> str | None:
    """If ``name`` is a ``metrics_engine`` REGISTRY ``formula_key``, return a
    rich "display_formula — method_notes" tooltip string; ``None`` otherwise
    (the caller falls back to the generic kpi_definitions-derived tooltip).

    Phase 3 wiring (docs/design/bottoms_up_metrics_engine.md §6): every
    ``metrics_engine`` computed value is persisted with ``kpi_definitions.name
    == formula.formula_key`` verbatim (``metrics_engine.io._persist_attempt``
    calls ``find_or_create_kpi_definition(..., name=formula.formula_key,
    ...)``), so a ``formula_key`` like ``"pe_ttm"`` or ``"gross_margin"`` IS a
    KPI name the moment the engine has run for a ticker. Without this
    hookup, the DIY picker / Ask tooltip would fall back to the generic
    "Company KPI 'pe_ttm'" placeholder instead of surfacing the registry's
    own documented formula + method notes — the read-side half of the
    engine's provenance contract (§3: "what formula produced this number").
    A lazy, function-local import avoids a module-load-time dependency from
    this low-level resolver onto the higher-level metrics_engine package.
    """
    from compute.metrics_engine.registry import latest as latest_formula

    formula = latest_formula(name)
    if formula is None:
        return None
    return f"{formula.display_formula} — {formula.method_notes}"


def reporting_cadence_for(conn: sqlite3.Connection, ticker: str, requested: str) -> str:
    """Return the reporting_cadence ('quarterly' | 'annual' | 'ttm') for the
    definition best matching ``requested``, defaulting to ``'quarterly'``.

    Matching is *fact-independent* (a tracked-but-empty annual KPI still
    resolves): exact name, then a parenthetical-insensitive normalized name. When
    several definitions normalize-match, an ``'annual'`` marker wins over
    ``'quarterly'`` so a sparse fragmented duplicate can't mask the annual
    cadence. Defensive: returns ``'quarterly'`` when the column is absent
    (pre-0072 / minimal test DBs) or no definition matches — so every caller can
    treat the result as authoritative without re-checking the schema.

    Requires ``conn.row_factory = sqlite3.Row`` (every consumer sets it).
    """
    cols = {c["name"] for c in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()}
    if "reporting_cadence" not in cols:
        return "quarterly"
    want = normalize_kpi_name(requested)
    best = "quarterly"
    found = False
    for row in conn.execute(
        "SELECT name, reporting_cadence FROM kpi_definitions WHERE ticker = ?",
        (ticker.upper(),),
    ):
        stored = str(row["name"])
        if stored == requested or normalize_kpi_name(stored) == want:
            found = True
            cadence = str(row["reporting_cadence"] or "quarterly")
            if cadence == "annual":
                return "annual"
            best = cadence
    return best if found else "quarterly"


# ---------------------------------------------------------------------------
# Write-path canonicalizer — canonical_metric_name
#
# `resolve_kpi_definition_name` (above) and `canonical_metric_name` now share the
# same conservative key: only unit surface variants may collapse. The WRITE side
# is used by the
# "capture every reported number" program (directives/capture_every_number_program.md)
# routes every freshly-extracted label through it BEFORE persisting, to decide
# whether the value joins an existing kpi_definitions row or mints a new one.
#
# A duplicate is recoverable; a false merge silently routes two different
# metrics' facts into one series. Therefore the shared match key only
# collapses UNIT / CASING / WHITESPACE surface variants and KEEPS semantic
# qualifiers, and a normalized match is additionally gated by unit-family
# compatibility.
# ---------------------------------------------------------------------------

# Tokens that denote a UNIT rather than the metric itself, used to recognize a
# "unit-only" trailing parenthetical — "(USD)", "($ in millions)", "(%)" — that is
# safe to strip from a match key. A parenthetical is unit-only iff EVERY non-filler
# token in it is here; a single non-unit token ("gross", "annualized", "of total")
# keeps the whole parenthetical, so distinct metrics that merely share a stem never
# collapse. Symbols ($/%/# and the major currency glyphs) are tokenized standalone.
_UNIT_PAREN_TOKENS: frozenset[str] = frozenset(
    {
        # currency codes / glyphs (us$, r$ split to 'us'/'r' + '$'; 'us' is filler)
        "usd",
        "eur",
        "gbp",
        "dkk",
        "brl",
        "cad",
        "inr",
        "aud",
        "krw",
        "jpy",
        "chf",
        "$",
        "€",
        "£",
        "¥",
        "₹",
        # magnitudes
        "thousands",
        "thousand",
        "millions",
        "million",
        "billions",
        "billion",
        "mm",
        "mn",
        "mln",
        "bn",
        "bln",
        "k",
        "m",
        "b",
        # proportions
        "percent",
        "pct",
        "%",
        "bps",
        "bp",
        "ratio",
        "x",
        # counts
        "count",
        "number",
        "#",
        "units",
        "unit",
    }
)
# Connective words admitted inside a unit parenthetical without disqualifying it:
# "(in millions)" / "(per share)" are unit-only, but "(% of revenue)" is NOT
# (because 'revenue' survives), so the rate-of-X case never merges into the level.
_UNIT_PAREN_FILLER: frozenset[str] = frozenset({"in", "of", "per", "us", "the", "a"})

# A trailing "( ... )" group, capturing its inner text (no nested parens).
_PAREN_TAIL_GROUP_RX = re.compile(r"\s*\(([^()]*)\)\s*$")
# Tokenizer for parenthetical content: alphanumeric runs OR a standalone unit glyph.
_PAREN_TOKEN_RX = re.compile(r"[a-z0-9]+|[$%#€£¥₹]")

# Bare (non-parenthetical) trailing unit suffixes that are safe to strip from a
# match key — "Gross margin %" / "NIM bps" / "Revenue USD". DELIBERATELY tiny: the
# risky single letters (m / b / k / x) are excluded so a real trailing word is
# never truncated. Word tokens require a preceding space; glyphs may be glued.
_BARE_SUFFIX_WORDS: tuple[str, ...] = ("percent", "pct", "bps", "usd")
_BARE_SUFFIX_GLYPHS: tuple[str, ...] = ("%", "$")


def _is_unit_only_parenthetical(inner: str) -> bool:
    """True iff every non-filler token in a parenthetical's content is a unit."""
    tokens = [t for t in _PAREN_TOKEN_RX.findall(inner.lower()) if t not in _UNIT_PAREN_FILLER]
    return bool(tokens) and all(t in _UNIT_PAREN_TOKENS for t in tokens)


def _canonical_match_key(name: str) -> str:
    """Normalize ``name`` to the conservative write-path match key.

    Three passes: (1) peel trailing UNIT-ONLY parentheticals while keeping
    semantic ones; (2) casefold + collapse whitespace; (3) peel a trailing bare
    unit suffix from a tiny safe set. The result is the key two labels must share
    EXACTLY (plus unit-family compatibility) to be treated as the same metric on
    write — no edit-distance / fuzzy similarity is used, because an approximate
    match is exactly how a false merge happens.
    """
    s = name.strip()
    while True:
        m = _PAREN_TAIL_GROUP_RX.search(s)
        if m is None or not _is_unit_only_parenthetical(m.group(1)):
            break
        s = s[: m.start()].rstrip()
    s = " ".join(s.split()).lower()
    changed = True
    while changed and s:
        changed = False
        for glyph in _BARE_SUFFIX_GLYPHS:
            if s.endswith(glyph) and len(s) > len(glyph):
                s = s[: -len(glyph)].rstrip()
                changed = True
        for word in _BARE_SUFFIX_WORDS:
            suffix = " " + word
            if s.endswith(suffix):
                s = s[: -len(suffix)].rstrip()
                changed = True
    return s


def _clean_new_name(raw_label: str) -> str:
    """The name a never-before-seen metric is minted under: the raw label with
    whitespace collapsed/trimmed, content otherwise preserved.

    Deliberately minimal — case, parentheticals, and unit tokens are KEPT so the
    first surface form an extractor emits becomes a readable canonical name and so
    a minted name can never collide (via over-stripping) with a different existing
    definition. Later variants collapse onto it through ``_canonical_match_key``,
    not by mangling this name.
    """
    return " ".join(raw_label.split())


def _parse_unit(raw: object) -> Unit | None:
    """Best-effort parse of a stored ``kpi_definitions.unit`` string to ``Unit``;
    None when absent or out-of-vocabulary (a junk unit must not block a merge)."""
    if raw is None:
        return None
    try:
        return Unit(str(raw).strip().lower())
    except ValueError:
        return None


def _units_mergeable(incoming: Unit, stored_raw: object) -> bool:
    """True iff a value in ``incoming`` may join a definition stored in
    ``stored_raw``'s unit — i.e. they share a unit family (a dollar LEVEL never
    absorbs a percentage RATE that shares a stem). An unparseable stored unit is
    treated as compatible: don't split a series over a garbage unit string."""
    stored = _parse_unit(stored_raw)
    if stored is None:
        return True
    return same_family(incoming, stored)


def _unit_family_token(unit: Unit) -> str:
    """A short, stable tag for ``unit``'s dimensional FAMILY — used to disambiguate
    a minted name from a surface-identical definition of an incompatible family.

    Family-level (not the specific unit) so every member of one family shares a
    single disambiguated name: a later capture in a different magnitude of the same
    family (``count``→``thousands`` say) re-derives the SAME tag and reuses the row
    rather than minting a per-magnitude duplicate. Magnitudes → ``amount``,
    proportions → ``rate``, everything else (raw counts, out-of-vocab) → its own
    unit value, which is its own singleton family under ``same_family``."""
    if same_family(unit, Unit.ACTUAL):
        return "amount"
    if same_family(unit, Unit.RATIO):
        return "rate"
    return unit.value


def _mint_unit_safe_name(cleaned: str, unit: Unit, rows: list[sqlite3.Row]) -> str:
    """The name to mint for a captured ``(cleaned, unit)`` that found no
    family-compatible normalized match — GUARANTEED not to collide on
    ``(ticker, name)`` with an existing definition of an INCOMPATIBLE unit family.

    ``find_or_create_kpi_definition`` keys only on ``(ticker, name)`` with the unit
    absent from its identity (``UNIQUE(ticker, name)``), so returning ``cleaned``
    bare when a surface-identical row of an incompatible family already exists
    would silently route this distinct metric's facts onto that row — the exact
    false merge the candidate-step unit gate exists to prevent, and the governing
    invariant says a false merge is worse than a duplicate. Append a family tag (and
    in the pathological case that the tagged name ALSO collides incompatibly, a
    numeric suffix) until the name is unit-safe. Deterministic in the common
    single-collision case, so a re-encounter of the same ``(label, unit)`` reuses
    the disambiguated row instead of minting another."""

    def _collides(name: str) -> bool:
        return any(str(r["name"]) == name and not _units_mergeable(unit, r["unit"]) for r in rows)

    if not _collides(cleaned):
        return cleaned
    tagged = f"{cleaned} ({_unit_family_token(unit)})"
    if not _collides(tagged):
        return tagged
    suffix = 2
    while _collides(f"{tagged} #{suffix}"):
        suffix += 1
    return f"{tagged} #{suffix}"


def _kpi_definitions_has_origin(conn: sqlite3.Connection) -> bool:
    """True iff kpi_definitions carries ``definition_origin`` (migration 0113).
    Schema-defensive: a pre-0113 / minimal test DB simply skips origin-aware
    tie-breaking."""
    return any(
        str(r["name"] if hasattr(r, "keys") else r[1]) == "definition_origin"
        for r in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()
    )


def _definition_fact_counts(conn: sqlite3.Connection, ticker: str) -> dict[int, int]:
    """``{kpi_definition_id: fact_count}`` for ``ticker`` — INCLUDING factless
    definitions (LEFT JOIN → 0), unlike the read resolver which counts only
    definitions that carry facts. The write path must see every existing
    definition so a not-yet-populated capture row is reused, not re-minted.
    Tolerates a missing kpi_facts table ({} → all counts treated as 0)."""
    try:
        rows = conn.execute(
            "SELECT kd.id AS id, COUNT(kf.id) AS n "
            "FROM kpi_definitions kd "
            f"LEFT JOIN {canonical_fact_relation(conn, 'kpi_facts').sql} kf "
            "ON kf.kpi_definition_id = kd.id "
            "WHERE kd.ticker = ? GROUP BY kd.id",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {int(r["id"]): int(r["n"]) for r in rows}


def canonical_metric_name(
    conn: sqlite3.Connection,
    ticker: str,
    raw_label: str,
    unit: Unit,
) -> str:
    """Return the ``kpi_definitions.name`` a captured ``(raw_label, unit)`` should
    be stored under for ``ticker`` — the write-path canonicalizer for the
    capture-every-number program.

    Resolution:

    1. Compute the conservative match key of ``raw_label`` (``_canonical_match_key``
       — unit/casing/whitespace folded, semantic qualifiers kept).
    2. Among this ticker's existing definitions, keep those whose own match key
       equals it AND whose stored unit is family-compatible with ``unit``
       (``_units_mergeable``). The unit gate is what stops a dollar *level* from
       absorbing a *rate* that happens to share a stem.
    3. If any match, return the canonical one: most facts wins (mirrors the read
       resolver's most-observations defragmentation so capture reuses the rich
       series), then analyst-origin over capture-origin, then an exact surface
       match, then lowest id — a fully deterministic order.
    4. Otherwise mint a clean new name (``_clean_new_name``) — but if that bare
       name is surface-identical to an existing definition of an INCOMPATIBLE unit
       family, disambiguate it with a family tag (``_mint_unit_safe_name``) so the
       unit gate's split is not silently undone downstream.

    The returned name is fed straight to ``find_or_create_kpi_definition``, which
    keys on ``(ticker, name)`` with the unit ABSENT from its identity. So when no
    family-compatible normalized match exists, returning a bare name that collides
    with a surface-identical row of a DIFFERENT unit family would false-merge two
    distinct metrics; step 4 prevents that by minting a unit-safe name instead.
    Requires ``conn.row_factory = sqlite3.Row``.
    """
    cleaned = _clean_new_name(raw_label)
    if not cleaned:
        return cleaned
    want = _canonical_match_key(cleaned)
    has_origin = _kpi_definitions_has_origin(conn)
    select_cols = "id, name, unit" + (", definition_origin" if has_origin else "")
    try:
        rows = conn.execute(
            f"SELECT {select_cols} FROM kpi_definitions WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchall()
    except sqlite3.Error:
        return cleaned

    candidates = [
        r
        for r in rows
        if _canonical_match_key(str(r["name"])) == want and _units_mergeable(unit, r["unit"])
    ]
    if not candidates:
        # No family-compatible normalized match. A bare ``cleaned`` here would let
        # find_or_create_kpi_definition — keyed on (ticker, name), unit ABSENT —
        # silently merge this value onto a surface-identical definition of an
        # incompatible unit family (a COUNT "Deposits" onto the dollar "Deposits").
        # Mint a unit-disambiguated name so the unit gate's split survives to
        # persistence.
        return _mint_unit_safe_name(cleaned, unit, rows)

    counts = _definition_fact_counts(conn, ticker)

    def _rank(r: sqlite3.Row) -> tuple[int, int, int, int]:
        obs = counts.get(int(r["id"]), 0)
        analyst = int(has_origin and str(r["definition_origin"]) == DefinitionOrigin.ANALYST.value)
        exact_surface = int(str(r["name"]) == cleaned)
        return (obs, analyst, exact_surface, -int(r["id"]))

    return str(max(candidates, key=_rank)["name"])
