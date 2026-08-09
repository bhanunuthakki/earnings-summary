"""Read-only, cutoff-exact legacy-to-canonical fact parity.

The scanner deliberately starts from the two legacy fact tables and follows
only durable identity bridges.  It never guesses a canonical fact from a
label, QName, or approximate period.  Projection state is supplied by a
reader Protocol so this module does not duplicate the projection module's
private delta-chain SQL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.population_completeness import PopulationTemporalScope
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

MAX_PAGE_SIZE = 1_000
_FACT_TABLES = ("financial_facts", "kpi_facts")


class ParityDisposition(StrEnum):
    EQUAL = "equal"
    VALUE_MISMATCH = "value_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNIT_MISMATCH = "unit_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    VALUE_KIND_MISMATCH = "value_kind_mismatch"
    LEGACY_UNRESOLVED_EXCLUDED = "legacy_unresolved_excluded"
    LEGACY_BINDING_MISSING_OR_STALE = "legacy_binding_missing_or_stale"
    LEGACY_MATCH_RETRYABLE = "legacy_match_retryable"
    LEGACY_MATCH_TERMINAL_NO_CANDIDATE = "legacy_match_terminal_no_candidate"
    LEGACY_MATCH_TERMINAL_NO_EXACT = "legacy_match_terminal_no_exact"
    LEGACY_MATCH_TERMINAL_AMBIGUOUS = "legacy_match_terminal_ambiguous"
    LEGACY_KPI_LINEAGE_REQUIRED = "legacy_kpi_lineage_required"
    V2_BRIDGE_MISSING = "v2_bridge_missing"
    ONTOLOGY_BINDING_MISSING = "ontology_binding_missing"
    ONTOLOGY_BINDING_QUARANTINED = "ontology_binding_quarantined"
    ONTOLOGY_BINDING_RETIRED = "ontology_binding_retired"
    CANONICAL_RESOLUTION_MISSING = "canonical_resolution_missing"
    CANONICAL_UNRESOLVED = "canonical_unresolved"
    CANONICAL_RETIRED = "canonical_retired"
    CANONICAL_PROJECTION_MISSING = "canonical_projection_missing"
    CANONICAL_TOMBSTONED = "canonical_tombstoned"
    CANONICAL_ONLY_NATIVE = "canonical_only_native"
    MULTIPLE_LEGACY_ROWS_TO_ONE_COORDINATE = "multiple_legacy_rows_to_one_coordinate"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyFactCursor(_Frozen):
    issuer_id: str = Field(min_length=1, max_length=128)
    temporal_scope: PopulationTemporalScope
    projection_generation_id: str = Field(min_length=1, max_length=128)
    fact_table_rank: int = Field(ge=0, le=1)
    fact_row_id: int = Field(ge=1)


class ParityRequest(_Frozen):
    temporal_scope: PopulationTemporalScope
    projection_generation_id: str = Field(min_length=1, max_length=128)
    issuer_id: str = Field(min_length=1, max_length=128)
    page_size: int = Field(default=1_000, ge=1, le=MAX_PAGE_SIZE)
    max_pages: int = Field(default=10_000, ge=1)
    max_rows: int = Field(default=2_000_000, ge=1)
    after: LegacyFactCursor | None = None

    @model_validator(mode="after")
    def _scope_bound_cursor(self) -> Self:
        for name, value in (
            ("knowledge_cutoff", self.temporal_scope.knowledge_cutoff),
            ("observed_through", self.temporal_scope.observed_through),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"parity {name} must be timezone-aware")
        if self.after is not None and self.after.issuer_id != self.issuer_id:
            raise ValueError("parity cursor issuer differs from request issuer")
        if self.after is not None and self.after.temporal_scope != self.temporal_scope:
            raise ValueError("parity cursor temporal scope differs from request scope")
        if (
            self.after is not None
            and self.after.projection_generation_id != self.projection_generation_id
        ):
            raise ValueError("parity cursor projection differs from request projection")
        return self

    @property
    def cutoff_at(self) -> datetime:
        return self.temporal_scope.knowledge_cutoff


class ProjectionCoordinate(_Frozen):
    generation_id: str
    canonical_metric_cell_id: str
    change_kind: Literal["upsert", "tombstone"]
    audit_verified: bool
    canonical_resolution_revision_id: str | None = None
    selected_observation_id: str | None = None
    value_kind: Literal["numeric", "text", "nil"] | None = None
    canonical_value: str | None = None
    period_end: str | None = None
    unit_key: str | None = None
    currency: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.change_kind == "upsert":
            required = (
                self.canonical_resolution_revision_id,
                self.selected_observation_id,
                self.value_kind,
                self.period_end,
                self.unit_key,
            )
            if any(item is None for item in required):
                raise ValueError("projection upsert is missing committed fact fields")
        return self


class ProjectionCoordinateReader(Protocol):
    """Narrow public boundary over a sealed, audited effective projection."""

    def read_coordinates(
        self,
        *,
        generation_id: str,
        canonical_metric_cell_ids: Sequence[str],
        cutoff_at: datetime,
    ) -> Mapping[str, ProjectionCoordinate]: ...

    def read_coordinate_page(
        self,
        *,
        generation_id: str,
        after_coordinate: str | None,
        limit: int,
        cutoff_at: datetime,
    ) -> Sequence[ProjectionCoordinate]: ...


class _HashWriter(Protocol):
    def update(self, data: bytes, /) -> object: ...


class FieldDiff(_Frozen):
    field: Literal["value_kind", "value", "period_end", "unit", "currency"]
    legacy_value: str | None
    canonical_value: str | None


class ParityRow(_Frozen):
    fact_table: Literal["financial_facts", "kpi_facts"] | None
    fact_row_id: int | None
    disposition: ParityDisposition
    comparable: bool
    canonical_metric_cell_id: str | None = None
    match_revision_id: str | None = None
    v2_observation_id: str | None = None
    ontology_binding_revision_id: str | None = None
    canonical_resolution_revision_id: str | None = None
    projection_generation_id: str | None = None
    field_diffs: tuple[FieldDiff, ...] = ()

    @model_validator(mode="after")
    def _identity(self) -> Self:
        legacy = self.fact_table is not None and self.fact_row_id is not None
        native = (
            self.disposition is ParityDisposition.CANONICAL_ONLY_NATIVE
            and self.fact_table is None
            and self.fact_row_id is None
        )
        if not (legacy or native):
            raise ValueError("parity row must identify a legacy or native coordinate")
        return self


class ParityReport(_Frozen):
    knowledge_cutoff: datetime
    observed_through: datetime
    projection_generation_id: str
    issuer_id: str
    complete: bool
    truncated: bool
    cutover_ready: bool
    pages_scanned: int = Field(ge=0)
    projection_pages_scanned: int = Field(ge=0)
    legacy_rows_scanned: int = Field(ge=0)
    canonical_coordinates_scanned: int = Field(ge=0)
    comparable_rows: int = Field(ge=0)
    equal_rows: int = Field(ge=0)
    mismatch_rows: int = Field(ge=0)
    blocking_legacy_rows: int = Field(ge=0)
    disposition_counts: dict[str, int]
    legacy_fact_universe_sha256: str = Field(min_length=64, max_length=64)
    parity_rows_sha256: str = Field(min_length=64, max_length=64)
    next_cursor: LegacyFactCursor | None
    projection_next_cursor: str | None
    rows: tuple[ParityRow, ...]


class ParityContractError(RuntimeError):
    """The parity source or projection reader violated its exact contract."""


class _LegacyKeyRow(_Frozen):
    fact_table_rank: int
    fact_table: Literal["financial_facts", "kpi_facts"]
    fact_row_id: int
    kpi_lineage_required: bool


def run_legacy_canonical_parity(
    database_path: str | Path,
    request: ParityRequest,
    projection_reader: ProjectionCoordinateReader,
) -> ParityReport:
    """Scan parity using an owned SQLite read-only connection."""

    path = Path(database_path).resolve(strict=True)
    conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        conn.execute("PRAGMA query_only = ON")
        return _scan(conn, request, projection_reader)
    finally:
        conn.close()


def scan_legacy_canonical_parity(
    conn: sqlite3.Connection,
    request: ParityRequest,
    projection_reader: ProjectionCoordinateReader,
) -> ParityReport:
    """Scan parity on a caller-owned connection without mutating it."""

    return _scan(conn, request, projection_reader)


def _scan(
    conn: sqlite3.Connection,
    request: ParityRequest,
    projection_reader: ProjectionCoordinateReader,
) -> ParityReport:
    knowledge_cutoff = _db_time(request.temporal_scope.knowledge_cutoff)
    observed_through = _db_time(request.temporal_scope.observed_through)
    after = request.after
    rows: list[ParityRow] = []
    pages = 0
    legacy_count = 0
    truncated = False
    next_cursor: LegacyFactCursor | None = None
    exhausted = False
    legacy_key_hasher = hashlib.sha256()

    while pages < request.max_pages and legacy_count < request.max_rows:
        remaining = request.max_rows - legacy_count
        page_limit = min(request.page_size, remaining)
        page = _legacy_page(conn, request, after, page_limit)
        pages += 1
        if not page:
            exhausted = True
            break
        selected = page
        for key in selected:
            _commit_model(
                legacy_key_hasher,
                {
                    "fact_table": key.fact_table,
                    "fact_row_id": key.fact_row_id,
                },
            )
        evaluated = _evaluate_page(
            conn,
            selected,
            knowledge_cutoff,
            observed_through,
            request,
            projection_reader,
        )
        rows.extend(evaluated)
        legacy_count += len(selected)
        tail = selected[-1]
        after = LegacyFactCursor(
            issuer_id=request.issuer_id,
            temporal_scope=request.temporal_scope,
            projection_generation_id=request.projection_generation_id,
            fact_table_rank=tail.fact_table_rank,
            fact_row_id=tail.fact_row_id,
        )
        has_more = _has_legacy_after(conn, request, after)
        if has_more:
            if pages >= request.max_pages or legacy_count >= request.max_rows:
                truncated = True
                next_cursor = after
                break
            continue
        exhausted = True
        break

    if not exhausted and not truncated:
        truncated = True
        next_cursor = after

    rows = _mark_many_to_one(rows)
    canonical_count = 0
    projection_pages = 0
    projection_next_cursor: str | None = None
    if not truncated and request.after is None:
        (
            canonical_rows,
            canonical_count,
            projection_pages,
            projection_truncated,
            projection_next_cursor,
        ) = _canonical_only_rows(
            projection_reader,
            request,
            {
                row.canonical_metric_cell_id
                for row in rows
                if row.canonical_metric_cell_id is not None
            },
        )
        rows.extend(canonical_rows)
        truncated = projection_truncated

    counts = Counter(row.disposition.value for row in rows)
    comparable = sum(row.comparable for row in rows)
    equal = counts[ParityDisposition.EQUAL.value]
    mismatch = sum(
        counts[item.value]
        for item in (
            ParityDisposition.VALUE_MISMATCH,
            ParityDisposition.PERIOD_MISMATCH,
            ParityDisposition.UNIT_MISMATCH,
            ParityDisposition.CURRENCY_MISMATCH,
            ParityDisposition.VALUE_KIND_MISMATCH,
        )
    )
    blocking = sum(
        1
        for row in rows
        if row.fact_table is not None and row.disposition is not ParityDisposition.EQUAL
    )
    complete = not truncated and request.after is None
    return ParityReport(
        knowledge_cutoff=_utc(request.temporal_scope.knowledge_cutoff),
        observed_through=_utc(request.temporal_scope.observed_through),
        projection_generation_id=request.projection_generation_id,
        issuer_id=request.issuer_id,
        complete=complete,
        truncated=truncated,
        cutover_ready=complete and mismatch == 0 and blocking == 0,
        pages_scanned=pages,
        projection_pages_scanned=projection_pages,
        legacy_rows_scanned=legacy_count,
        canonical_coordinates_scanned=canonical_count,
        comparable_rows=comparable,
        equal_rows=equal,
        mismatch_rows=mismatch,
        blocking_legacy_rows=blocking,
        disposition_counts=dict(sorted(counts.items())),
        legacy_fact_universe_sha256=legacy_key_hasher.hexdigest(),
        parity_rows_sha256=_parity_rows_sha256(rows),
        next_cursor=next_cursor,
        projection_next_cursor=projection_next_cursor,
        rows=tuple(rows),
    )


def _legacy_page(
    conn: sqlite3.Connection,
    request: ParityRequest,
    after: LegacyFactCursor | None,
    limit: int,
) -> list[_LegacyKeyRow]:
    rank = -1 if after is None else after.fact_table_rank
    row_id = 0 if after is None else after.fact_row_id
    knowledge_cutoff = _db_time(request.temporal_scope.knowledge_cutoff)
    observed_through = _db_time(request.temporal_scope.observed_through)
    cursor = conn.execute(
        """
        SELECT fact_table_rank,fact_table,fact_row_id,kpi_lineage_required
        FROM (
          SELECT 0 AS fact_table_rank,'financial_facts' AS fact_table,
                 fact.id AS fact_row_id,0 AS kpi_lineage_required
          FROM financial_facts fact
          JOIN documents document ON document.id=fact.source_doc_id
          JOIN legacy_fact_evidence_match_revisions current_match
            ON current_match.fact_table='financial_facts'
           AND current_match.fact_row_id=fact.id
           AND current_match.issuer_id=?
           AND datetime(current_match.knowledge_at)<=datetime(?)
           AND datetime(current_match.recorded_at)<=datetime(?)
          WHERE datetime(document.fetched_at)<=datetime(?)
            AND NOT EXISTS (
            SELECT 1
            FROM legacy_fact_evidence_match_revisions newer
            WHERE newer.fact_table=current_match.fact_table
              AND newer.fact_row_id=current_match.fact_row_id
              AND datetime(newer.knowledge_at)<=datetime(?)
              AND datetime(newer.recorded_at)<=datetime(?)
              AND (
                newer.revision>current_match.revision
                OR (
                  newer.revision=current_match.revision
                  AND newer.match_revision_id>current_match.match_revision_id
                )
              )
          )
          UNION ALL
          SELECT 1,'kpi_facts',fact.id,
                 CASE WHEN fact.computed_from IS NOT NULL
                        OR fact.formula_id IS NOT NULL OR fact.formula_version IS NOT NULL
                        OR lower(coalesce(fact.extracted_by,'')) LIKE '%derived%'
                      THEN 1 ELSE 0 END
          FROM kpi_facts fact
          JOIN documents document ON document.id=fact.source_doc_id
          JOIN legacy_fact_evidence_match_revisions current_match
            ON current_match.fact_table='kpi_facts'
           AND current_match.fact_row_id=fact.id
           AND current_match.issuer_id=?
           AND datetime(current_match.knowledge_at)<=datetime(?)
           AND datetime(current_match.recorded_at)<=datetime(?)
          WHERE datetime(document.fetched_at)<=datetime(?)
            AND NOT EXISTS (
            SELECT 1
            FROM legacy_fact_evidence_match_revisions newer
            WHERE newer.fact_table=current_match.fact_table
              AND newer.fact_row_id=current_match.fact_row_id
              AND datetime(newer.knowledge_at)<=datetime(?)
              AND datetime(newer.recorded_at)<=datetime(?)
              AND (
                newer.revision>current_match.revision
                OR (
                  newer.revision=current_match.revision
                  AND newer.match_revision_id>current_match.match_revision_id
                )
              )
          )
        )
        WHERE fact_table_rank > ?
           OR (fact_table_rank = ? AND fact_row_id > ?)
        ORDER BY fact_table_rank,fact_row_id
        LIMIT ?
        """,
        (
            request.issuer_id,
            knowledge_cutoff,
            observed_through,
            knowledge_cutoff,
            knowledge_cutoff,
            observed_through,
            request.issuer_id,
            knowledge_cutoff,
            observed_through,
            knowledge_cutoff,
            knowledge_cutoff,
            observed_through,
            rank,
            rank,
            row_id,
            limit,
        ),
    )
    raw = cursor.fetchmany(limit + 1)
    if len(raw) > limit:
        raise ParityContractError("legacy fact query exceeded requested page size")
    return [
        _LegacyKeyRow(
            fact_table_rank=int(row["fact_table_rank"]),
            fact_table=cast(
                Literal["financial_facts", "kpi_facts"],
                str(row["fact_table"]),
            ),
            fact_row_id=int(row["fact_row_id"]),
            kpi_lineage_required=bool(row["kpi_lineage_required"]),
        )
        for row in raw
    ]


def _has_legacy_after(
    conn: sqlite3.Connection,
    request: ParityRequest,
    after: LegacyFactCursor,
) -> bool:
    return bool(_legacy_page(conn, request, after, 1))


def _evaluate_page(
    conn: sqlite3.Connection,
    keys: Sequence[_LegacyKeyRow],
    knowledge_cutoff: str,
    observed_through: str,
    request: ParityRequest,
    reader: ProjectionCoordinateReader,
) -> list[ParityRow]:
    legacy_resolved = _legacy_resolved_keys(
        conn,
        keys,
        knowledge_cutoff,
        observed_through,
    )
    matches = _latest_matches(
        conn,
        keys,
        knowledge_cutoff,
        observed_through,
        issuer_id=request.issuer_id,
    )
    bindings = _current_document_bindings(
        conn,
        matches.values(),
        knowledge_cutoff,
        observed_through,
    )
    bridges = _v2_bridges(
        conn,
        matches.values(),
        knowledge_cutoff,
        observed_through,
    )
    observation_ids = [
        str(items[0]["observation_id"]) for items in bridges.values() if len(items) == 1
    ]
    ontology = _ontology_bindings(
        conn,
        observation_ids,
        knowledge_cutoff,
        observed_through,
    )
    coordinates = [
        str(row["canonical_metric_cell_id"])
        for row in ontology.values()
        if row is not None and row["binding_status"] == "bound"
    ]
    resolutions = _canonical_resolutions(
        conn,
        coordinates,
        knowledge_cutoff,
        observed_through,
    )
    projection_ids = [
        coordinate
        for coordinate, resolution in resolutions.items()
        if resolution is not None and resolution["status"] == "resolved"
    ]
    projection = dict(
        reader.read_coordinates(
            generation_id=request.projection_generation_id,
            canonical_metric_cell_ids=tuple(sorted(set(projection_ids))),
            cutoff_at=request.cutoff_at,
        )
    )
    _validate_projection_batch(projection, request)

    output: list[ParityRow] = []
    for key in keys:
        pair = (key.fact_table, key.fact_row_id)
        if key.kpi_lineage_required:
            output.append(_terminal(key, ParityDisposition.LEGACY_KPI_LINEAGE_REQUIRED))
            continue
        if pair not in legacy_resolved:
            output.append(_terminal(key, ParityDisposition.LEGACY_UNRESOLVED_EXCLUDED))
            continue
        match = matches.get(pair)
        if match is None:
            output.append(_terminal(key, ParityDisposition.LEGACY_BINDING_MISSING_OR_STALE))
            continue
        match_id = str(match["match_revision_id"])
        outcome = str(match["outcome"])
        if outcome == "retryable":
            output.append(
                _terminal(
                    key,
                    ParityDisposition.LEGACY_MATCH_RETRYABLE,
                    match_revision_id=match_id,
                )
            )
            continue
        if outcome == "terminal":
            output.append(
                _terminal(
                    key,
                    _terminal_match_disposition(match),
                    match_revision_id=match_id,
                )
            )
            continue
        binding = bindings.get(_match_source_doc_id(match))
        if binding is None or not _binding_is_exact(match, binding):
            output.append(
                _terminal(
                    key,
                    ParityDisposition.LEGACY_BINDING_MISSING_OR_STALE,
                    match_revision_id=match_id,
                )
            )
            continue
        bridge_rows = bridges.get(match_id, ())
        if len(bridge_rows) != 1:
            output.append(
                _terminal(
                    key,
                    ParityDisposition.V2_BRIDGE_MISSING,
                    match_revision_id=match_id,
                )
            )
            continue
        bridge = bridge_rows[0]
        observation_id = str(bridge["observation_id"])
        ontology_row = ontology.get(observation_id)
        if ontology_row is None:
            output.append(
                _terminal(
                    key,
                    ParityDisposition.ONTOLOGY_BINDING_MISSING,
                    match_revision_id=match_id,
                    v2_observation_id=observation_id,
                )
            )
            continue
        binding_status = str(ontology_row["binding_status"])
        binding_revision_id = str(ontology_row["binding_revision_id"])
        if binding_status != "bound":
            disposition = (
                ParityDisposition.ONTOLOGY_BINDING_QUARANTINED
                if binding_status == "quarantined"
                else ParityDisposition.ONTOLOGY_BINDING_RETIRED
            )
            output.append(
                _terminal(
                    key,
                    disposition,
                    match_revision_id=match_id,
                    v2_observation_id=observation_id,
                    ontology_binding_revision_id=binding_revision_id,
                )
            )
            continue
        coordinate = str(ontology_row["canonical_metric_cell_id"])
        resolution = resolutions.get(coordinate)
        common = {
            "match_revision_id": match_id,
            "v2_observation_id": observation_id,
            "ontology_binding_revision_id": binding_revision_id,
            "canonical_metric_cell_id": coordinate,
        }
        if resolution is None:
            output.append(_terminal(key, ParityDisposition.CANONICAL_RESOLUTION_MISSING, **common))
            continue
        resolution_status = str(resolution["status"])
        resolution_id = str(resolution["canonical_resolution_revision_id"])
        common["canonical_resolution_revision_id"] = resolution_id
        if resolution_status != "resolved":
            disposition = (
                ParityDisposition.CANONICAL_UNRESOLVED
                if resolution_status == "unresolved"
                else ParityDisposition.CANONICAL_RETIRED
            )
            output.append(_terminal(key, disposition, **common))
            continue
        coordinate_state = projection.get(coordinate)
        if coordinate_state is None:
            output.append(_terminal(key, ParityDisposition.CANONICAL_PROJECTION_MISSING, **common))
            continue
        if coordinate_state.change_kind == "tombstone":
            output.append(_terminal(key, ParityDisposition.CANONICAL_TOMBSTONED, **common))
            continue
        if (
            coordinate_state.canonical_resolution_revision_id != resolution_id
            or coordinate_state.selected_observation_id
            != str(resolution["selected_observation_id"])
        ):
            raise ParityContractError(
                f"projection identity mismatch for canonical coordinate {coordinate}"
            )
        output.append(
            _compare(
                key,
                match,
                coordinate_state,
                projection_generation_id=request.projection_generation_id,
                **common,
            )
        )
    return output


def _legacy_resolved_keys(
    conn: sqlite3.Connection,
    keys: Sequence[_LegacyKeyRow],
    knowledge_cutoff: str,
    observed_through: str,
) -> set[tuple[str, int]]:
    clause, params = _key_clause(keys, "link")
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked_link AS (
          SELECT link.*,
                 row_number() OVER (
                   PARTITION BY fact_table,fact_row_id
                   ORDER BY fact_revision DESC,observation_id DESC
                 ) AS row_rank
          FROM fact_observation_revisions link
          WHERE datetime(captured_at) <= datetime(?) AND ({clause})
        ),
        ranked_resolution AS (
          SELECT resolution.*,
                 row_number() OVER (
                   PARTITION BY logical_key
                   ORDER BY revision DESC,resolution_id DESC
                 ) AS row_rank
          FROM observation_resolution_revisions resolution
          WHERE datetime(knowledge_cutoff) <= datetime(?)
            AND datetime(recorded_at) <= datetime(?)
        )
        SELECT link.fact_table,link.fact_row_id
        FROM ranked_link link
        JOIN ranked_resolution resolution
          ON resolution.logical_key=link.logical_key
         AND resolution.row_rank=1
         AND resolution.selected_observation_id=link.observation_id
        JOIN fact_resolution_outcomes outcome
          ON outcome.resolution_id=resolution.resolution_id
         AND outcome.resolution_status='resolved'
        WHERE link.row_rank=1
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (observed_through, *params, knowledge_cutoff, observed_through),
        ),
        limit=len(keys),
    )
    return {(str(row[0]), int(row[1])) for row in rows}


def _latest_matches(
    conn: sqlite3.Connection,
    keys: Sequence[_LegacyKeyRow],
    knowledge_cutoff: str,
    observed_through: str,
    *,
    issuer_id: str | None,
) -> dict[tuple[str, int], sqlite3.Row]:
    clause, params = _key_clause(keys, "match")
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked AS (
          SELECT match.*,
                 row_number() OVER (
                   PARTITION BY fact_table,fact_row_id
                   ORDER BY revision DESC,match_revision_id DESC
                 ) AS row_rank
          FROM legacy_fact_evidence_match_revisions match
          WHERE datetime(knowledge_at) <= datetime(?)
            AND datetime(recorded_at) <= datetime(?)
            AND ({clause})
        )
        SELECT * FROM ranked
        WHERE row_rank=1 AND (? IS NULL OR issuer_id=?)
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (
                knowledge_cutoff,
                observed_through,
                *params,
                issuer_id,
                issuer_id,
            ),
        ),
        limit=len(keys),
    )
    return {(str(row["fact_table"]), int(row["fact_row_id"])): row for row in rows}


def _current_document_bindings(
    conn: sqlite3.Connection,
    matches: Iterable[sqlite3.Row],
    knowledge_cutoff: str,
    observed_through: str,
) -> dict[int, sqlite3.Row]:
    document_ids = sorted({_match_source_doc_id(row) for row in matches})
    if not document_ids:
        return {}
    placeholders = ",".join("?" for _ in document_ids)
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked AS (
          SELECT binding.*,
                 row_number() OVER (
                   PARTITION BY legacy_document_id
                   ORDER BY revision DESC,binding_revision_id DESC
                 ) AS row_rank
          FROM legacy_document_evidence_binding_revisions binding
          WHERE datetime(knowledge_at) <= datetime(?)
            AND datetime(recorded_at) <= datetime(?)
            AND legacy_document_id IN ({placeholders})
        )
        SELECT * FROM ranked WHERE row_rank=1
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (knowledge_cutoff, observed_through, *document_ids),
        ),
        limit=len(document_ids),
    )
    return {int(row["legacy_document_id"]): row for row in rows}


def _v2_bridges(
    conn: sqlite3.Connection,
    matches: Iterable[sqlite3.Row],
    knowledge_cutoff: str,
    observed_through: str,
) -> dict[str, tuple[sqlite3.Row, ...]]:
    match_ids = sorted({str(row["match_revision_id"]) for row in matches})
    if not match_ids:
        return {}
    placeholders = ",".join("?" for _ in match_ids)
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked AS (
          SELECT observation.observation_id,observation.legacy_match_revision_id,
                 observation.fact_cell_id,observation.knowledge_at,
                 observation.recorded_at,
                 row_number() OVER (
                   PARTITION BY observation.legacy_match_revision_id
                   ORDER BY observation.observation_id
                 ) row_rank
          FROM fact_observations_v2 observation
          JOIN fact_cells_v2 cell ON cell.fact_cell_id=observation.fact_cell_id
          JOIN reporting_entities entity
            ON entity.reporting_entity_id=cell.reporting_entity_id
          JOIN legacy_fact_evidence_match_revisions match
            ON match.match_revision_id=observation.legacy_match_revision_id
           AND match.issuer_id=entity.issuer_id
           AND match.evidence_node_id=observation.evidence_node_id
           AND match.matched_entry_sha256=observation.source_entry_sha256
          WHERE observation.legacy_match_revision_id IN ({placeholders})
            AND datetime(observation.knowledge_at) <= datetime(?)
            AND datetime(observation.recorded_at) <= datetime(?)
        )
        SELECT * FROM ranked
        WHERE row_rank<=2
        ORDER BY legacy_match_revision_id,observation_id
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (*match_ids, knowledge_cutoff, observed_through),
        ),
        limit=2 * len(match_ids),
    )
    grouped: defaultdict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["legacy_match_revision_id"])].append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _ontology_bindings(
    conn: sqlite3.Connection,
    observation_ids: Sequence[str],
    knowledge_cutoff: str,
    observed_through: str,
) -> dict[str, sqlite3.Row | None]:
    if not observation_ids:
        return {}
    placeholders = ",".join("?" for _ in observation_ids)
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked AS (
          SELECT binding.*,
                 row_number() OVER (
                   PARTITION BY source_observation_id
                   ORDER BY revision DESC,binding_revision_id DESC
                 ) AS row_rank
          FROM fact_cell_canonical_binding_revisions binding
          WHERE source_observation_id IN ({placeholders})
            AND datetime(knowledge_at) <= datetime(?)
            AND datetime(recorded_at) <= datetime(?)
        )
        SELECT * FROM ranked WHERE row_rank=1
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (*observation_ids, knowledge_cutoff, observed_through),
        ),
        limit=len(observation_ids),
    )
    result: dict[str, sqlite3.Row | None] = {
        observation_id: None for observation_id in observation_ids
    }
    result.update({str(row["source_observation_id"]): row for row in rows})
    return result


def _canonical_resolutions(
    conn: sqlite3.Connection,
    coordinates: Sequence[str],
    knowledge_cutoff: str,
    observed_through: str,
) -> dict[str, sqlite3.Row | None]:
    if not coordinates:
        return {}
    unique = sorted(set(coordinates))
    placeholders = ",".join("?" for _ in unique)
    rows = _bounded_rows(
        conn.execute(
            f"""
        WITH ranked AS (
          SELECT resolution.*,
                 row_number() OVER (
                   PARTITION BY canonical_metric_cell_id
                   ORDER BY revision DESC,canonical_resolution_revision_id DESC
                 ) AS row_rank
          FROM canonical_fact_resolution_revisions resolution
          WHERE canonical_metric_cell_id IN ({placeholders})
            AND datetime(knowledge_at) <= datetime(?)
            AND datetime(recorded_at) <= datetime(?)
        )
        SELECT * FROM ranked WHERE row_rank=1
        """,  # nosec B608 -- trusted internal SQL shape; values remain bound
            (*unique, knowledge_cutoff, observed_through),
        ),
        limit=len(unique),
    )
    result: dict[str, sqlite3.Row | None] = {coordinate: None for coordinate in unique}
    result.update({str(row["canonical_metric_cell_id"]): row for row in rows})
    return result


def _canonical_only_rows(
    reader: ProjectionCoordinateReader,
    request: ParityRequest,
    legacy_coordinates: set[str],
) -> tuple[list[ParityRow], int, int, bool, str | None]:
    output: list[ParityRow] = []
    after: str | None = None
    scanned = 0
    pages = 0
    while pages < request.max_pages and scanned < request.max_rows:
        limit = min(request.page_size, request.max_rows - scanned)
        page = tuple(
            reader.read_coordinate_page(
                generation_id=request.projection_generation_id,
                after_coordinate=after,
                limit=limit,
                cutoff_at=request.cutoff_at,
            )
        )
        pages += 1
        if len(page) > limit:
            raise ParityContractError("projection reader exceeded requested page size")
        if not page:
            return output, scanned, pages, False, None
        ordered = sorted(page, key=lambda item: item.canonical_metric_cell_id)
        if list(page) != ordered:
            raise ParityContractError("projection coordinate page is not ordered")
        for item in page:
            _validate_projection_coordinate(item, request)
            scanned += 1
            if (
                item.change_kind == "upsert"
                and item.canonical_metric_cell_id not in legacy_coordinates
            ):
                output.append(
                    ParityRow(
                        fact_table=None,
                        fact_row_id=None,
                        disposition=ParityDisposition.CANONICAL_ONLY_NATIVE,
                        comparable=False,
                        canonical_metric_cell_id=item.canonical_metric_cell_id,
                        canonical_resolution_revision_id=(item.canonical_resolution_revision_id),
                        projection_generation_id=request.projection_generation_id,
                    )
                )
        new_after = page[-1].canonical_metric_cell_id
        if after is not None and new_after <= after:
            raise ParityContractError("projection coordinate cursor did not advance")
        after = new_after
        if len(page) < limit:
            return output, scanned, pages, False, None
    if after is None:
        return output, scanned, pages, False, None
    probe = tuple(
        reader.read_coordinate_page(
            generation_id=request.projection_generation_id,
            after_coordinate=after,
            limit=1,
            cutoff_at=request.cutoff_at,
        )
    )
    if not probe:
        return output, scanned, pages, False, None
    return output, scanned, pages, True, after


def _validate_projection_batch(
    coordinates: Mapping[str, ProjectionCoordinate], request: ParityRequest
) -> None:
    for key, value in coordinates.items():
        if key != value.canonical_metric_cell_id:
            raise ParityContractError("projection reader returned a mismatched key")
        _validate_projection_coordinate(value, request)


def _validate_projection_coordinate(
    coordinate: ProjectionCoordinate, request: ParityRequest
) -> None:
    if coordinate.generation_id != request.projection_generation_id:
        raise ParityContractError("projection reader returned another generation")
    if not coordinate.audit_verified:
        raise ParityContractError("projection coordinate is not sealed and audit-verified")


def _compare(
    key: _LegacyKeyRow,
    match: sqlite3.Row,
    projection: ProjectionCoordinate,
    **identity: object,
) -> ParityRow:
    payload_raw = json.loads(str(match["fact_payload_json"]))
    if not isinstance(payload_raw, dict):
        raise ParityContractError("legacy match payload is not an object")
    payload = cast(dict[str, object], payload_raw)
    legacy_kind = "numeric"
    legacy_value = _exact_decimal_text(payload.get("value"))
    canonical_value = (
        _exact_decimal_text(projection.canonical_value)
        if projection.value_kind == "numeric"
        else projection.canonical_value
    )
    legacy_period = _date_period(payload.get("period_end"))
    canonical_period = _date_period(projection.period_end)
    legacy_unit = _required_text(payload.get("unit"), "legacy unit")
    canonical_unit = _required_text(projection.unit_key, "canonical unit")
    legacy_currency = _currency(payload.get("currency"))
    canonical_currency = _currency(projection.currency)
    diffs: list[FieldDiff] = []
    if projection.value_kind != legacy_kind:
        diffs.append(
            FieldDiff(
                field="value_kind",
                legacy_value=legacy_kind,
                canonical_value=projection.value_kind,
            )
        )
    if legacy_value != canonical_value:
        diffs.append(
            FieldDiff(
                field="value",
                legacy_value=legacy_value,
                canonical_value=canonical_value,
            )
        )
    if legacy_period != canonical_period:
        diffs.append(
            FieldDiff(
                field="period_end",
                legacy_value=legacy_period.isoformat(),
                canonical_value=canonical_period.isoformat(),
            )
        )
    if legacy_unit != canonical_unit:
        diffs.append(
            FieldDiff(
                field="unit",
                legacy_value=legacy_unit,
                canonical_value=canonical_unit,
            )
        )
    if legacy_currency != canonical_currency:
        diffs.append(
            FieldDiff(
                field="currency",
                legacy_value=legacy_currency,
                canonical_value=canonical_currency,
            )
        )
    fields = {item.field for item in diffs}
    disposition = ParityDisposition.EQUAL
    for field, candidate in (
        ("value_kind", ParityDisposition.VALUE_KIND_MISMATCH),
        ("period_end", ParityDisposition.PERIOD_MISMATCH),
        ("unit", ParityDisposition.UNIT_MISMATCH),
        ("currency", ParityDisposition.CURRENCY_MISMATCH),
        ("value", ParityDisposition.VALUE_MISMATCH),
    ):
        if field in fields:
            disposition = candidate
            break
    return ParityRow.model_validate(
        {
            "fact_table": key.fact_table,
            "fact_row_id": key.fact_row_id,
            "disposition": disposition,
            "comparable": True,
            "field_diffs": tuple(diffs),
            **identity,
        }
    )


def _mark_many_to_one(rows: list[ParityRow]) -> list[ParityRow]:
    by_coordinate: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if row.fact_table is not None and row.canonical_metric_cell_id is not None:
            by_coordinate[row.canonical_metric_cell_id].append(index)
    result = list(rows)
    for indexes in by_coordinate.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            row = result[index]
            result[index] = row.model_copy(
                update={
                    "disposition": (ParityDisposition.MULTIPLE_LEGACY_ROWS_TO_ONE_COORDINATE),
                    "comparable": False,
                }
            )
    return result


def _bounded_rows(cursor: sqlite3.Cursor, *, limit: int) -> list[sqlite3.Row]:
    rows = cursor.fetchmany(limit + 1)
    if len(rows) > limit:
        raise ParityContractError("bounded parity query exceeded its page cardinality")
    return rows


def _commit_model(hasher: _HashWriter, value: object) -> None:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    hasher.update(payload.encode("utf-8"))
    hasher.update(b"\n")


def _parity_rows_sha256(rows: Sequence[ParityRow]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        _commit_model(hasher, row.model_dump(mode="json"))
    return hasher.hexdigest()


def _terminal(
    key: _LegacyKeyRow,
    disposition: ParityDisposition,
    **identity: object,
) -> ParityRow:
    return ParityRow.model_validate(
        {
            "fact_table": key.fact_table,
            "fact_row_id": key.fact_row_id,
            "disposition": disposition,
            "comparable": False,
            **identity,
        }
    )


def _terminal_match_disposition(match: sqlite3.Row) -> ParityDisposition:
    if int(match["candidate_count"]) == 0:
        return ParityDisposition.LEGACY_MATCH_TERMINAL_NO_CANDIDATE
    if int(match["matched_candidate_count"]) > 1:
        return ParityDisposition.LEGACY_MATCH_TERMINAL_AMBIGUOUS
    return ParityDisposition.LEGACY_MATCH_TERMINAL_NO_EXACT


def _binding_is_exact(match: sqlite3.Row, binding: sqlite3.Row) -> bool:
    return (
        str(binding["binding_revision_id"]) == str(match["legacy_binding_revision_id"])
        and int(binding["revision"]) == int(match["legacy_binding_revision"])
        and str(binding["scope_content_sha256"]) == str(match["binding_scope_content_sha256"])
        and str(binding["evidence_node_id"]) == str(match["evidence_node_id"])
    )


def _match_source_doc_id(match: sqlite3.Row) -> int:
    payload_raw = json.loads(str(match["fact_payload_json"]))
    if not isinstance(payload_raw, dict):
        raise ParityContractError("legacy match payload lacks an integer source_doc_id")
    payload = cast(dict[str, object], payload_raw)
    source_doc_id = payload.get("source_doc_id")
    if isinstance(source_doc_id, bool) or not isinstance(source_doc_id, int):
        raise ParityContractError("legacy match payload lacks an integer source_doc_id")
    return source_doc_id


def _key_clause(keys: Sequence[_LegacyKeyRow], alias: str) -> tuple[str, tuple[object, ...]]:
    if not keys:
        return "0", ()
    clauses: list[str] = []
    params: list[object] = []
    for key in keys:
        clauses.append(f"({alias}.fact_table=? AND {alias}.fact_row_id=?)")
        params.extend((key.fact_table, key.fact_row_id))
    return " OR ".join(clauses), tuple(params)


def _exact_decimal_text(value: object) -> str:
    if value is None:
        raise ParityContractError("numeric parity value is missing")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ParityContractError("numeric parity value is not an exact decimal") from exc
    if not number.is_finite():
        raise ParityContractError("numeric parity value must be finite")
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _date_period(value: object) -> date:
    if value is None:
        raise ParityContractError("period_end is missing")
    text = str(value)
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParityContractError("period_end is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParityContractError("timestamp period_end must be timezone-aware")
    return parsed.astimezone(UTC).date()


def _currency(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) != 3 or text != text.upper():
        raise ParityContractError("currency must be an uppercase ISO-style code")
    return text


def _required_text(value: object, label: str) -> str:
    if value is None or not str(value).strip():
        raise ParityContractError(f"{label} is missing")
    return str(value)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "FieldDiff",
    "LegacyFactCursor",
    "ParityContractError",
    "ParityDisposition",
    "ParityReport",
    "ParityRequest",
    "ParityRow",
    "ProjectionCoordinate",
    "ProjectionCoordinateReader",
    "run_legacy_canonical_parity",
    "scan_legacy_canonical_parity",
]
