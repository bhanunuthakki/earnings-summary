"""Delta-only materialization and reads for the latest governed research state.

Historical ledgers remain authoritative.  This module owns the smaller mutable
projection used by routine refreshes and reads, and will only advance it from a
current sealed population cutover and its exactly-bound promoted Ask scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scope_identity import derive_retrieval_scope_id

RETENTION_POLICY_PROPOSAL = "retain-source-history_compact-rebuildable-projection.v1"
DEFAULT_POLICY_VERSION = "latest-governed-state.v1"
_HEX = frozenset("0123456789abcdef")
_MAX_FACT_SEARCH_QUERY_LENGTH = 4_096
_MAX_FACT_SEARCH_TOKEN_COUNT = 32
_MAX_FACT_SEARCH_TOKEN_LENGTH = 128
_MAX_NARRATIVE_SEARCH_QUERY_LENGTH = 4_096
_MAX_NARRATIVE_SEARCH_TOKEN_COUNT = 32
_MAX_NARRATIVE_SEARCH_TOKEN_LENGTH = 128


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_time(value: object) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    aware = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _db_time(value: object) -> str:
    return _canonical_time(value).replace("T", " ").replace("Z", "")


def _datetime(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _optional_text(value: object | None) -> str | None:
    return None if value is None else str(value)


def _bucket(coordinate: str) -> int:
    return int(_digest(coordinate)[:3], 16)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChangeFrontier(_FrozenModel):
    """Exact governed inputs admitted for one refresh."""

    scope_id: str = Field(min_length=1, max_length=256)
    population_run_id: str = Field(min_length=1, max_length=128)
    population_receipt_set_sha256: str
    promotion_id: str = Field(min_length=1, max_length=128)
    fact_generation_id: str = Field(min_length=1, max_length=128)
    fact_projection_seal_sha256: str
    research_snapshot_id: str = Field(min_length=1, max_length=128)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    source_inventory_ids_json: str
    narrative_bundles_json: str
    source_publication_high_watermark: int = Field(ge=0)
    knowledge_cutoff: datetime
    observed_through: datetime

    @field_validator("population_receipt_set_sha256", "fact_projection_seal_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("frontier commitments must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _datetime(self.observed_through) < _datetime(self.knowledge_cutoff):
            raise ValueError("frontier observed_through must not precede knowledge_cutoff")
        for value in (
            self.source_inventory_ids_json,
            self.narrative_bundles_json,
        ):
            parsed = json.loads(value)
            if not isinstance(parsed, list):
                raise ValueError("frontier source coordinates must be JSON arrays")
        return self

    @property
    def commitment_sha256(self) -> str:
        return _digest(self.model_dump(mode="json"))


class CurrentHead(_FrozenModel):
    scope_id: str
    receipt_id: str
    fact_generation_id: str
    input_frontier_sha256: str
    state_commitment_sha256: str
    fact_root_sha256: str
    document_root_sha256: str
    narrative_root_sha256: str
    rollback_receipt_id: str | None
    source_frontier: ChangeFrontier


class DeltaPlan(_FrozenModel):
    refresh_id: str
    idempotency_key: str
    scope_id: str
    prior_head: CurrentHead | None
    frontier: ChangeFrontier
    policy_config_sha256: str
    mode: Literal["no_op", "direct_delta", "checkpoint"]
    fact_change_count: int = Field(ge=0)
    document_change_count: int = Field(ge=0)
    narrative_change_count: int = Field(ge=0)
    resume_cursor: int = Field(ge=0)


class RefreshReceipt(_FrozenModel):
    receipt_id: str
    refresh_id: str
    scope_id: str
    outcome: Literal["no_op", "changed"]
    prior_receipt_id: str | None
    current_state_sha256: str
    terminal_commitment: str
    fact_change_count: int = Field(ge=0)
    document_change_count: int = Field(ge=0)
    narrative_change_count: int = Field(ge=0)
    knowledge_cutoff: datetime
    observed_through: datetime
    operation_recorded_at: datetime


class LatestGovernedRefreshRequest(_FrozenModel):
    scope_id: str = Field(min_length=1, max_length=256)
    operation_recorded_at: datetime
    policy_version: str = Field(default=DEFAULT_POLICY_VERSION, min_length=1, max_length=128)
    max_batch_rows: int = Field(default=1_000, ge=1, le=10_000)
    document_checkpoint: bool = False
    apply: bool = False
    resume_refresh_id: str | None = Field(default=None, max_length=128)
    interrupt_after_batches: int | None = Field(default=None, ge=1)
    expected_terminal_commitment: str | None = None

    @field_validator("expected_terminal_commitment")
    @classmethod
    def _expected_sha(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in _HEX for character in value)
        ):
            raise ValueError("expected terminal commitment must be lowercase SHA-256")
        return value


class LatestGovernedRefreshResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    outcome: Literal["no_op", "changed", "staged"]
    refresh_id: str
    head_id: str | None
    created_count: int = Field(ge=0)
    replayed_count: int = Field(ge=0)
    source_event_count: int = Field(ge=0)
    fact_change_count: int = Field(ge=0)
    document_change_count: int = Field(ge=0)
    narrative_change_count: int = Field(ge=0)
    source_read_count: int = Field(ge=0)
    current_read_count: int = Field(ge=0)
    current_write_count: int = Field(ge=0)
    receipt_write_count: int = Field(ge=0)
    terminal_commitment: str
    resume_cursor: str | None = None


class LatestGovernedReprojectionRequest(_FrozenModel):
    """Forward-only request to reproject an immutable prior receipt."""

    scope_id: str = Field(min_length=1, max_length=256)
    target_receipt_id: str = Field(min_length=1, max_length=128)
    expected_current_receipt_id: str = Field(min_length=1, max_length=128)
    operation_recorded_at: datetime
    policy_version: str = Field(default=DEFAULT_POLICY_VERSION, min_length=1, max_length=128)
    max_batch_rows: int = Field(default=1_000, ge=1, le=10_000)


class LatestGovernedFactHit(_FrozenModel):
    scope_id: str
    canonical_metric_cell_id: str
    canonical_metric_name: str
    canonical_value: str | None
    value_kind: Literal["numeric", "text", "nil"]
    period_start: str | None
    period_end: str
    currency: str | None
    unit_key: str
    commitment_sha256: str
    fact_generation_id: str
    evidence_locator: dict[str, object]
    lineage: dict[str, object]


class LatestGovernedNarrativeHit(_FrozenModel):
    scope_id: str
    expected_document_key: str
    chunk_key: str
    text: str
    score: str
    content_sha256: str
    document_version_id: str
    evidence_node_id: str
    source_chunk_id: str | None
    embedding_artifact_id: str | None
    current_commitment_sha256: str
    evidence_locator: dict[str, object]


class LatestGovernedScopeAudit(_FrozenModel):
    """Exhaustive source-to-current verification for one production scope."""

    scope_id: str
    head_receipt_id: str
    terminal_commitment: str
    fact_count: int = Field(gt=0)
    document_count: int = Field(gt=0)
    narrative_count: int = Field(gt=0)
    fts_count: int = Field(gt=0)
    change_count: int = Field(ge=0)
    finalized_run_count: int = Field(gt=0)
    stage_count: Literal[0] = 0
    fact_canary_coordinate: str
    narrative_canary_coordinate: str
    exhaustive_source_commitment_sha256: str
    current_projection_commitment_sha256: str
    high_risk_sample_commitment_sha256: str


class LatestGovernedCohortAudit(_FrozenModel):
    """Exact, nonempty readiness evidence for one ordered production cohort."""

    schema_version: Literal["latest-governed-cohort-audit/v2"] = "latest-governed-cohort-audit/v2"
    scope_ids: tuple[str, ...]
    scopes: tuple[LatestGovernedScopeAudit, ...]
    table_counts: dict[str, int]
    cohort_commitment_sha256: str


LatestGovernedFinalizationHook = Callable[
    [sqlite3.Connection, LatestGovernedRefreshRequest, ChangeFrontier, RefreshReceipt, CurrentHead],
    None,
]


class LatestGovernedStateError(RuntimeError):
    """Fail-loud materialization or read admission error."""


def refresh_latest_governed_state(
    conn: sqlite3.Connection,
    request: LatestGovernedRefreshRequest,
    *,
    finalization_hook: LatestGovernedFinalizationHook | None = None,
) -> LatestGovernedRefreshResult:
    """Materialize a governed no-op or changed frontier.

    The implementation is completed below the schema contract once migration
    0259 is present.  Keeping this public entry point small prevents refresh
    callers from bypassing frontier admission, staging, CAS publication, or
    immutable receipt creation.
    """

    return GovernedCurrentMaterializer(conn).refresh(
        request,
        finalization_hook=finalization_hook,
    )


def audit_latest_governed_cohort(
    conn: sqlite3.Connection,
    scope_ids: tuple[str, ...],
    *,
    operation_recorded_at: datetime,
    high_risk_sample_size: int = 32,
) -> LatestGovernedCohortAudit:
    """Prove every 0261 plane is terminal, nonempty where durable, and source-exact."""

    if operation_recorded_at.tzinfo is None or operation_recorded_at.utcoffset() is None:
        raise ValueError("operation_recorded_at must include a timezone")
    if high_risk_sample_size < 1 or high_risk_sample_size > 1_000:
        raise ValueError("high_risk_sample_size must be between 1 and 1000")
    ordered = tuple(sorted(scope_ids))
    if not ordered or ordered != scope_ids or len(set(ordered)) != len(ordered):
        raise LatestGovernedStateError(
            "latest governed cohort must be nonempty, unique, and sorted"
        )
    materializer = GovernedCurrentMaterializer(conn)
    audits = tuple(
        materializer.audit_scope(
            scope_id,
            operation_recorded_at=operation_recorded_at,
            high_risk_sample_size=high_risk_sample_size,
        )
        for scope_id in ordered
    )
    head_scope_ids = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT scope_key FROM latest_governed_scope_heads ORDER BY scope_key"
        ).fetchall()
    )
    if head_scope_ids != ordered:
        raise LatestGovernedStateError(
            "latest governed heads differ from the exact production cohort"
        )
    plane_scope_ids = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT scope_key FROM latest_governed_refresh_runs UNION "
            "SELECT scope_key FROM latest_governed_scope_heads UNION "
            "SELECT scope_key FROM latest_governed_fact_entries UNION "
            "SELECT scope_key FROM latest_governed_document_entries UNION "
            "SELECT scope_key FROM latest_governed_narrative_entries "
            "ORDER BY scope_key"
        ).fetchall()
    )
    if plane_scope_ids != ordered:
        raise LatestGovernedStateError(
            "latest governed plane scopes differ from the exact production cohort"
        )
    if int(
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_refresh_runs WHERE status<>'finalized'"
        ).fetchone()[0]
    ):
        raise LatestGovernedStateError("latest governed cohort has unfinished refresh runs")
    orphan_fts = int(
        conn.execute(
            "SELECT COUNT(*) FROM latest_governed_narrative_fts fts "
            "LEFT JOIN latest_governed_narrative_entries entry ON entry.rowid=fts.rowid "
            "WHERE entry.rowid IS NULL"
        ).fetchone()[0]
    )
    if orphan_fts:
        raise LatestGovernedStateError("latest governed FTS contains orphan rows")
    table_names = (
        "latest_governed_refresh_runs",
        "latest_governed_refresh_stage",
        "latest_governed_refresh_receipts",
        "latest_governed_refresh_changes",
        "latest_governed_scope_heads",
        "latest_governed_fact_entries",
        "latest_governed_document_entries",
        "latest_governed_narrative_entries",
        "latest_governed_narrative_fts",
    )
    table_counts = {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])  # nosec B608 -- names are a closed constant tuple
        for name in table_names
    }
    required_nonempty = set(table_names) - {
        "latest_governed_refresh_changes",
        "latest_governed_refresh_stage",
    }
    if table_counts["latest_governed_refresh_stage"] != 0:
        raise LatestGovernedStateError("latest governed stage is not terminally empty")
    if any(table_counts[name] == 0 for name in required_nonempty):
        raise LatestGovernedStateError("latest governed durable planes must all be nonempty")
    if (
        table_counts["latest_governed_scope_heads"] != len(audits)
        or table_counts["latest_governed_fact_entries"] != sum(item.fact_count for item in audits)
        or table_counts["latest_governed_document_entries"]
        != sum(item.document_count for item in audits)
        or table_counts["latest_governed_narrative_entries"]
        != sum(item.narrative_count for item in audits)
        or table_counts["latest_governed_narrative_fts"] != sum(item.fts_count for item in audits)
    ):
        raise LatestGovernedStateError(
            "latest governed global plane counts differ from audited scopes"
        )
    core = {
        "schema_version": "latest-governed-cohort-audit/v2",
        "scope_ids": ordered,
        "scopes": [item.model_dump(mode="json") for item in audits],
        "table_counts": table_counts,
    }
    return LatestGovernedCohortAudit.model_validate(
        core | {"cohort_commitment_sha256": _digest(core)}
    )


def audit_latest_governed_scope(
    conn: sqlite3.Connection,
    scope_id: str,
    *,
    operation_recorded_at: datetime,
    high_risk_sample_size: int = 32,
) -> LatestGovernedScopeAudit:
    """Exhaustively verify one scope without weakening exact cohort admission."""

    if operation_recorded_at.tzinfo is None or operation_recorded_at.utcoffset() is None:
        raise ValueError("operation_recorded_at must include a timezone")
    if high_risk_sample_size < 1 or high_risk_sample_size > 1_000:
        raise ValueError("high_risk_sample_size must be between 1 and 1000")
    return GovernedCurrentMaterializer(conn).audit_scope(
        scope_id,
        operation_recorded_at=operation_recorded_at,
        high_risk_sample_size=high_risk_sample_size,
    )


def current_latest_governed_projection_commitment(
    conn: sqlite3.Connection,
    scope_id: str,
) -> str:
    """Hash one scope's exact mutable 0261 projection without source reads."""

    planes = {
        "refresh_runs": conn.execute(
            "SELECT * FROM latest_governed_refresh_runs WHERE scope_key=? ORDER BY refresh_run_id",
            (scope_id,),
        ).fetchall(),
        "refresh_stage": conn.execute(
            "SELECT stage.* FROM latest_governed_refresh_stage stage "
            "JOIN latest_governed_refresh_runs run "
            "ON run.refresh_run_id=stage.refresh_run_id WHERE run.scope_key=? "
            "ORDER BY stage.refresh_run_id,stage.stage_ordinal",
            (scope_id,),
        ).fetchall(),
        "refresh_receipts": conn.execute(
            "SELECT * FROM latest_governed_refresh_receipts WHERE scope_key=? ORDER BY receipt_id",
            (scope_id,),
        ).fetchall(),
        "refresh_changes": conn.execute(
            "SELECT changes.* FROM latest_governed_refresh_changes changes "
            "JOIN latest_governed_refresh_receipts receipt "
            "ON receipt.receipt_id=changes.receipt_id WHERE receipt.scope_key=? "
            "ORDER BY changes.receipt_id,changes.change_ordinal",
            (scope_id,),
        ).fetchall(),
        "head": conn.execute(
            "SELECT * FROM latest_governed_scope_heads WHERE scope_key=?",
            (scope_id,),
        ).fetchall(),
        "facts": conn.execute(
            "SELECT * FROM latest_governed_fact_entries WHERE scope_key=? "
            "ORDER BY canonical_metric_cell_id",
            (scope_id,),
        ).fetchall(),
        "documents": conn.execute(
            "SELECT * FROM latest_governed_document_entries WHERE scope_key=? "
            "ORDER BY expected_document_key",
            (scope_id,),
        ).fetchall(),
        "narratives": conn.execute(
            "SELECT rowid,* FROM latest_governed_narrative_entries WHERE scope_key=? "
            "ORDER BY expected_document_key,chunk_key",
            (scope_id,),
        ).fetchall(),
        "narrative_fts": conn.execute(
            "SELECT docsize.id FROM latest_governed_narrative_fts_docsize docsize "
            "JOIN latest_governed_narrative_entries entry ON entry.rowid=docsize.id "
            "WHERE entry.scope_key=? ORDER BY docsize.id",
            (scope_id,),
        ).fetchall(),
    }
    return _digest(
        {
            "planes": {name: [list(row) for row in rows] for name, rows in planes.items()},
            "scope_id": scope_id,
            "version": "latest-governed-current-projection/v2",
        }
    )


def reproject_latest_governed_state(
    conn: sqlite3.Connection,
    request: LatestGovernedReprojectionRequest,
) -> LatestGovernedRefreshResult:
    """Create a new receipt/head whose current rows equal a prior receipt."""

    return GovernedCurrentMaterializer(conn).reproject(request)


def search_latest_governed_facts(
    conn: sqlite3.Connection,
    scope_id: str,
    query: str,
    limit: int,
    *,
    include_history: bool = False,
) -> tuple[LatestGovernedFactHit, ...]:
    """Search current facts by exact normalized metric keys."""

    if include_history:
        raise LatestGovernedStateError(
            "historical fact reads require an explicit historical generation API"
        )
    statement = build_latest_governed_fact_search_query(scope_id, query, limit)
    if statement is None:
        return ()
    sql, params = statement
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return tuple(_fact_hit(row) for row in rows)
    finally:
        conn.row_factory = original


def build_latest_governed_fact_search_query(
    scope_id: str,
    query: str,
    limit: int,
) -> tuple[str, tuple[object, ...]] | None:
    """Build the exact indexed SQL used by public current-fact search."""

    if not 1 <= limit <= 1_000:
        raise ValueError("fact search limit must be between 1 and 1000")
    if len(query) > _MAX_FACT_SEARCH_QUERY_LENGTH:
        raise ValueError("fact search query exceeds the 4096 character limit")
    tokens = tuple(sorted(set(re.findall(r"[A-Za-z0-9_-]+", query.casefold()))))
    if not tokens:
        return None
    if len(tokens) > _MAX_FACT_SEARCH_TOKEN_COUNT:
        raise ValueError("fact search query exceeds the 32 token limit")
    if any(len(token) > _MAX_FACT_SEARCH_TOKEN_LENGTH for token in tokens):
        raise ValueError("fact search token exceeds the 128 character limit")
    placeholders = ",".join("?" for _ in tokens)
    sql = (
        "SELECT * FROM latest_governed_fact_entries WHERE scope_key=? "
        f"AND canonical_metric_name IN ({placeholders}) "  # nosec B608 -- placeholders only
        "ORDER BY canonical_metric_name,period_end DESC,"
        "canonical_metric_cell_id LIMIT ?"
    )
    return sql, (scope_id, *tokens, limit)


def search_latest_governed_narrative(
    conn: sqlite3.Connection,
    scope_id: str,
    query: str,
    limit: int,
    *,
    include_history: bool = False,
) -> tuple[LatestGovernedNarrativeHit, ...]:
    """Search the current-only FTS projection and return semantic coordinates."""

    if include_history:
        raise LatestGovernedStateError(
            "historical narrative reads require an explicit historical manifest API"
        )
    if len(query) > _MAX_NARRATIVE_SEARCH_QUERY_LENGTH:
        raise ValueError("narrative search query exceeds the 4096 character limit")
    tokens = tuple(sorted(set(query.split())))
    if len(tokens) > _MAX_NARRATIVE_SEARCH_TOKEN_COUNT:
        raise ValueError("narrative search query exceeds the 32 token limit")
    if any(len(token) > _MAX_NARRATIVE_SEARCH_TOKEN_LENGTH for token in tokens):
        raise ValueError("narrative search token exceeds the 128 character limit")
    expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
    if not expression:
        return ()
    if not 1 <= limit <= 1_000:
        raise ValueError("narrative search limit must be between 1 and 1000")
    original = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT entry.*,bm25(latest_governed_narrative_fts) AS lexical_rank "
            "FROM latest_governed_narrative_fts "
            "JOIN latest_governed_narrative_entries entry "
            "ON entry.rowid=latest_governed_narrative_fts.rowid "
            "WHERE latest_governed_narrative_fts MATCH ? AND entry.scope_key=? "
            "ORDER BY lexical_rank,entry.expected_document_key,entry.chunk_key LIMIT ?",
            (expression, scope_id, limit),
        ).fetchall()
        return tuple(_narrative_hit(row) for row in rows)
    finally:
        conn.row_factory = original


def _fact_hit(row: sqlite3.Row) -> LatestGovernedFactHit:
    evidence = json.loads(str(row["source_evidence_json"]))
    if not isinstance(evidence, dict):
        raise LatestGovernedStateError("latest fact provenance payload is invalid")
    lineage = cast(dict[str, object], evidence)
    raw_locator = lineage.get("evidence_locator_json")
    decoded_locator: object = json.loads(str(raw_locator)) if raw_locator is not None else {}
    if not isinstance(decoded_locator, dict):
        raise LatestGovernedStateError("latest fact evidence locator is invalid")
    locator_value = cast(dict[str, object], decoded_locator)
    return LatestGovernedFactHit(
        scope_id=str(row["scope_key"]),
        canonical_metric_cell_id=str(row["canonical_metric_cell_id"]),
        canonical_metric_name=str(row["canonical_metric_name"]),
        canonical_value=_optional_text(row["canonical_value"]),
        value_kind=cast(Literal["numeric", "text", "nil"], str(row["value_kind"])),
        period_start=_optional_text(row["period_start"]),
        period_end="" if row["period_end"] is None else str(row["period_end"]),
        currency=_optional_text(row["currency"]),
        unit_key="" if row["unit_key"] is None else str(row["unit_key"]),
        commitment_sha256=str(row["current_commitment_sha256"]),
        fact_generation_id=str(row["fact_generation_id"]),
        evidence_locator=locator_value,
        lineage=lineage,
    )


def _narrative_hit(row: sqlite3.Row) -> LatestGovernedNarrativeHit:
    rank = abs(Decimal(str(row["lexical_rank"])))
    locator: dict[str, object] = {
        "document_version_id": str(row["document_version_id"]),
        "evidence_node_id": str(row["evidence_node_id"]),
        "source_chunk_id": _optional_text(row["source_chunk_id"]),
    }
    return LatestGovernedNarrativeHit(
        scope_id=str(row["scope_key"]),
        expected_document_key=str(row["expected_document_key"]),
        chunk_key=str(row["chunk_key"]),
        text=str(row["text"]),
        score=str(Decimal(1) / (Decimal(1) + rank)),
        content_sha256=str(row["content_sha256"]),
        document_version_id=str(row["document_version_id"]),
        evidence_node_id=str(row["evidence_node_id"]),
        source_chunk_id=_optional_text(row["source_chunk_id"]),
        embedding_artifact_id=_optional_text(row["embedding_artifact_id"]),
        current_commitment_sha256=str(row["current_commitment_sha256"]),
        evidence_locator=locator,
    )


class GovernedCurrentMaterializer:
    """Own frontier admission, changed-only staging, and atomic head publication."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.execute("PRAGMA foreign_keys=ON")

    def audit_scope(
        self,
        scope_id: str,
        *,
        operation_recorded_at: datetime,
        high_risk_sample_size: int,
    ) -> LatestGovernedScopeAudit:
        """Verify one current scope against a fresh exhaustive source reconstruction."""

        frontier = self._frontier(scope_id)
        if _datetime(operation_recorded_at) < _datetime(frontier.observed_through):
            raise LatestGovernedStateError(
                "audit operation_recorded_at precedes the admitted population frontier"
            )
        head = self._head(scope_id)
        if head is None or head.source_frontier != frontier:
            raise LatestGovernedStateError(
                "latest governed head is missing or does not bind the current frontier"
            )
        if self._receipt_head(scope_id=scope_id, receipt_id=head.receipt_id) != head:
            raise LatestGovernedStateError("latest governed head receipt is invalid")
        stage_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_stage stage "
                "JOIN latest_governed_refresh_runs run "
                "ON run.refresh_run_id=stage.refresh_run_id WHERE run.scope_key=?",
                (scope_id,),
            ).fetchone()[0]
        )
        if stage_count:
            raise LatestGovernedStateError("latest governed scope has staged changes")
        unfinished = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_runs "
                "WHERE scope_key=? AND status<>'finalized'",
                (scope_id,),
            ).fetchone()[0]
        )
        if unfinished:
            raise LatestGovernedStateError("latest governed scope has unfinished refresh runs")

        source_changes = self._baseline_source_changes(frontier)
        history = self._snapshot_at_receipt(
            scope_id=scope_id,
            receipt_id=head.receipt_id,
        )
        expected: dict[str, dict[str, str]] = {
            "fact": {},
            "document": {},
            "narrative": {},
        }
        for change in source_changes:
            if change["change_kind"] != "upsert" or change["current_commitment_sha256"] is None:
                raise LatestGovernedStateError(
                    "exhaustive latest governed source reconstruction emitted a tombstone"
                )
            expected[str(change["entity_kind"])][str(change["coordinate_key"])] = str(
                change["current_commitment_sha256"]
            )
            history_change = history.get(
                (str(change["entity_kind"]), str(change["coordinate_key"]))
            )
            if history_change is None:
                raise LatestGovernedStateError(
                    "latest governed current coordinate is absent from receipt history"
                )
            self._verify_current_payload(
                scope_id,
                frontier,
                change,
                history_change=history_change,
            )
        if set(history) != {
            (kind, coordinate) for kind, rows in expected.items() for coordinate in rows
        }:
            raise LatestGovernedStateError(
                "latest governed receipt history differs from governed sources"
            )
        current = {
            "fact": {
                str(row[0]): str(row[1])
                for row in self._conn.execute(
                    "SELECT canonical_metric_cell_id,current_commitment_sha256 "
                    "FROM latest_governed_fact_entries WHERE scope_key=?",
                    (scope_id,),
                )
            },
            "document": {
                str(row[0]): str(row[1])
                for row in self._conn.execute(
                    "SELECT expected_document_key,current_commitment_sha256 "
                    "FROM latest_governed_document_entries WHERE scope_key=?",
                    (scope_id,),
                )
            },
            "narrative": {
                f"{row[0]}\x00{row[1]}": str(row[2])
                for row in self._conn.execute(
                    "SELECT expected_document_key,chunk_key,current_commitment_sha256 "
                    "FROM latest_governed_narrative_entries WHERE scope_key=?",
                    (scope_id,),
                )
            },
        }
        if current != expected:
            raise LatestGovernedStateError(
                "latest governed current rows differ from exhaustive governed sources"
            )
        counts = {kind: len(rows) for kind, rows in current.items()}
        if any(value == 0 for value in counts.values()):
            raise LatestGovernedStateError(
                "latest governed facts, documents, and narratives must all be nonempty"
            )
        self._verify_head_projection(
            scope_id=scope_id,
            frontier=frontier,
            head=head,
            counts=counts,
        )
        narrative_rowids = {
            int(row[0])
            for row in self._conn.execute(
                "SELECT rowid FROM latest_governed_narrative_entries WHERE scope_key=?",
                (scope_id,),
            )
        }
        fts_rowids = {
            int(row[0])
            for row in self._conn.execute(
                "SELECT fts.rowid FROM latest_governed_narrative_fts fts "
                "JOIN latest_governed_narrative_entries entry ON entry.rowid=fts.rowid "
                "WHERE entry.scope_key=?",
                (scope_id,),
            )
        }
        if fts_rowids != narrative_rowids:
            raise LatestGovernedStateError("latest governed FTS rows differ from narratives")
        change_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_changes changes "
                "JOIN latest_governed_refresh_receipts receipt "
                "ON receipt.receipt_id=changes.receipt_id WHERE receipt.scope_key=?",
                (scope_id,),
            ).fetchone()[0]
        )
        finalized_run_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM latest_governed_refresh_runs "
                "WHERE scope_key=? AND status='finalized'",
                (scope_id,),
            ).fetchone()[0]
        )
        fact_coordinate, fact_name = self._conn.execute(
            "SELECT canonical_metric_cell_id,canonical_metric_name "
            "FROM latest_governed_fact_entries WHERE scope_key=? "
            "ORDER BY canonical_metric_cell_id LIMIT 1",
            (scope_id,),
        ).fetchone()
        if not any(
            hit.canonical_metric_cell_id == str(fact_coordinate)
            for hit in search_latest_governed_facts(self._conn, scope_id, str(fact_name), 5)
        ):
            raise LatestGovernedStateError("latest governed fact retrieval canary failed")
        narrative_row = self._conn.execute(
            "SELECT expected_document_key,chunk_key,text "
            "FROM latest_governed_narrative_entries WHERE scope_key=? "
            "ORDER BY expected_document_key,chunk_key LIMIT 1",
            (scope_id,),
        ).fetchone()
        narrative_tokens = re.findall(r"[A-Za-z0-9]{2,}", str(narrative_row[2]))
        if not narrative_tokens:
            raise LatestGovernedStateError("latest governed narrative canary has no safe token")
        narrative_coordinate = f"{narrative_row[0]}\x00{narrative_row[1]}"
        if not any(
            f"{hit.expected_document_key}\x00{hit.chunk_key}" == narrative_coordinate
            for hit in search_latest_governed_narrative(
                self._conn,
                scope_id,
                narrative_tokens[0],
                5,
            )
        ):
            raise LatestGovernedStateError("latest governed narrative retrieval canary failed")
        ordered_source = [
            {
                "coordinate": coordinate,
                "entity_kind": kind,
                "sha256": sha256,
            }
            for kind in ("fact", "document", "narrative")
            for coordinate, sha256 in sorted(expected[kind].items())
        ]
        high_risk_sample = sorted(
            ordered_source,
            key=lambda item: (_digest(item["coordinate"]), item["entity_kind"]),
        )[:high_risk_sample_size]
        for sample in high_risk_sample:
            if sample["entity_kind"] != "narrative":
                continue
            document_key, chunk_key = str(sample["coordinate"]).split("\x00", 1)
            text_row = self._conn.execute(
                "SELECT text FROM latest_governed_narrative_entries "
                "WHERE scope_key=? AND expected_document_key=? AND chunk_key=?",
                (scope_id, document_key, chunk_key),
            ).fetchone()
            tokens = [] if text_row is None else re.findall(r"[A-Za-z0-9]{2,}", str(text_row[0]))
            if not tokens or not any(
                hit.expected_document_key == document_key and hit.chunk_key == chunk_key
                for hit in search_latest_governed_narrative(
                    self._conn,
                    scope_id,
                    tokens[0],
                    max(5, min(100, counts["narrative"])),
                )
            ):
                raise LatestGovernedStateError(
                    "latest governed sampled FTS content integrity check failed"
                )
        return LatestGovernedScopeAudit(
            scope_id=scope_id,
            head_receipt_id=head.receipt_id,
            terminal_commitment=head.state_commitment_sha256,
            fact_count=counts["fact"],
            document_count=counts["document"],
            narrative_count=counts["narrative"],
            fts_count=len(fts_rowids),
            change_count=change_count,
            finalized_run_count=finalized_run_count,
            stage_count=0,
            fact_canary_coordinate=str(fact_coordinate),
            narrative_canary_coordinate=narrative_coordinate,
            exhaustive_source_commitment_sha256=_digest(ordered_source),
            current_projection_commitment_sha256=(
                current_latest_governed_projection_commitment(self._conn, scope_id)
            ),
            high_risk_sample_commitment_sha256=_digest(high_risk_sample),
        )

    def _verify_current_payload(
        self,
        scope_id: str,
        frontier: ChangeFrontier,
        change: dict[str, object],
        *,
        history_change: dict[str, object],
    ) -> None:
        payload = cast(dict[str, object], change["payload"])
        expected_row = cast(dict[str, object], payload["row"])
        evidence = cast(dict[str, object], payload["source_evidence"])
        kind = str(change["entity_kind"])
        coordinate = str(change["coordinate_key"])
        original = self._conn.row_factory
        self._conn.row_factory = sqlite3.Row
        try:
            if kind == "fact":
                row = self._conn.execute(
                    "SELECT * FROM latest_governed_fact_entries "
                    "WHERE scope_key=? AND canonical_metric_cell_id=?",
                    (scope_id, coordinate),
                ).fetchone()
            elif kind == "document":
                row = self._conn.execute(
                    "SELECT * FROM latest_governed_document_entries "
                    "WHERE scope_key=? AND expected_document_key=?",
                    (scope_id, coordinate),
                ).fetchone()
            else:
                document_key, chunk_key = coordinate.split("\x00", 1)
                row = self._conn.execute(
                    "SELECT * FROM latest_governed_narrative_entries "
                    "WHERE scope_key=? AND expected_document_key=? AND chunk_key=?",
                    (scope_id, document_key, chunk_key),
                ).fetchone()
        finally:
            self._conn.row_factory = original
        if row is None:
            raise LatestGovernedStateError("latest governed current payload is missing")
        actual = dict(row)
        evidence_json = _canonical_json(evidence)
        raw_history_payload = history_change.get("canonical_payload")
        if not isinstance(raw_history_payload, dict):
            raise LatestGovernedStateError(
                "latest governed persisted payload differs from governed source evidence: history"
            )
        history_payload = cast(dict[str, object], raw_history_payload)
        if (
            history_payload.get("row") != payload["row"]
            or history_payload.get("selection_reason") != payload["selection_reason"]
            or history_change.get("current_commitment_sha256")
            != change["current_commitment_sha256"]
        ):
            raise LatestGovernedStateError(
                "latest governed persisted payload differs from governed source evidence: history"
            )
        expected_common: dict[str, object] = {
            "scope_key": scope_id,
            "digest_bucket": _bucket(coordinate),
            "refresh_receipt_id": history_change["refresh_receipt_id"],
            "selection_reason": payload["selection_reason"],
            "prior_commitment_sha256": history_change["prior_commitment_sha256"],
            "current_commitment_sha256": change["current_commitment_sha256"],
        }
        if kind in {"fact", "document"}:
            expected_common["source_evidence_json"] = evidence_json
            expected_common["source_evidence_sha256"] = _digest(evidence_json)
        if kind == "fact":
            expected_common["fact_generation_id"] = frontier.fact_generation_id
        expected_payload = {**expected_row, **expected_common}
        mismatches = tuple(
            key for key, value in expected_payload.items() if actual.get(key) != value
        )
        clock_mismatches = tuple(
            key
            for key in ("knowledge_cutoff", "observed_through", "updated_at")
            if _datetime(actual.get(key)) != _datetime(history_change[key])
        )
        if mismatches or clock_mismatches:
            raise LatestGovernedStateError(
                "latest governed persisted payload differs from governed source evidence: "
                + ",".join((*mismatches, *clock_mismatches))
            )

    def _verify_head_projection(
        self,
        *,
        scope_id: str,
        frontier: ChangeFrontier,
        head: CurrentHead,
        counts: dict[str, int],
    ) -> None:
        row = self._conn.execute(
            "SELECT population_run_id,promotion_id,fact_generation_id,source_heads_json,"
            "source_heads_sha256,state_sha256,fact_root_sha256,document_root_sha256,"
            "narrative_root_sha256,fact_count,document_count,narrative_count,"
            "knowledge_cutoff,observed_through,updated_at "
            "FROM latest_governed_scope_heads WHERE scope_key=?",
            (scope_id,),
        ).fetchone()
        sealed = self._conn.execute(
            "SELECT sealed_at FROM latest_governed_refresh_receipts WHERE receipt_id=?",
            (head.receipt_id,),
        ).fetchone()
        if row is None or sealed is None:
            raise LatestGovernedStateError("latest governed head projection is incomplete")
        expected = (
            frontier.population_run_id,
            frontier.promotion_id,
            frontier.fact_generation_id,
            _canonical_json(frontier.model_dump(mode="json")),
            frontier.commitment_sha256,
            head.state_commitment_sha256,
            head.fact_root_sha256,
            head.document_root_sha256,
            head.narrative_root_sha256,
            counts["fact"],
            counts["document"],
            counts["narrative"],
        )
        clocks_exact = (
            _datetime(row[12]) == _datetime(frontier.knowledge_cutoff)
            and _datetime(row[13]) == _datetime(frontier.observed_through)
            and _datetime(row[14]) == _datetime(sealed[0])
        )
        if tuple(row[:12]) != expected or not clocks_exact:
            raise LatestGovernedStateError(
                "latest governed head projection differs from its exact source and receipt"
            )

    def refresh(
        self,
        request: LatestGovernedRefreshRequest,
        *,
        finalization_hook: LatestGovernedFinalizationHook | None = None,
    ) -> LatestGovernedRefreshResult:
        frontier = self._frontier(request.scope_id)
        if _datetime(request.operation_recorded_at) < _datetime(frontier.observed_through):
            raise LatestGovernedStateError(
                "operation_recorded_at must not precede the admitted population frontier"
            )
        prior = self._head(request.scope_id)
        policy_sha = _digest(
            {
                "document_checkpoint": request.document_checkpoint,
                "policy_version": request.policy_version,
                "retention_policy": RETENTION_POLICY_PROPOSAL,
            }
        )
        idempotency = _digest(
            {
                "scope_id": request.scope_id,
                "prior_head": (
                    None
                    if prior is None
                    else {
                        "receipt_id": prior.receipt_id,
                        "state_commitment_sha256": prior.state_commitment_sha256,
                    }
                ),
                "frontier": frontier.commitment_sha256,
                "policy_config_sha256": policy_sha,
            }
        )
        refresh_id = f"latest-run:{idempotency[:40]}"
        replay = self._replayed_result(refresh_id, request.apply)
        if replay is not None:
            if finalization_hook is not None:
                self._finalize_replayed_hook(
                    request=request,
                    frontier=frontier,
                    replay=replay,
                    finalization_hook=finalization_hook,
                )
            return replay
        if request.resume_refresh_id is not None and request.resume_refresh_id != refresh_id:
            raise LatestGovernedStateError(
                "resume refresh does not match the current governed plan"
            )

        if prior is not None and prior.input_frontier_sha256 == frontier.commitment_sha256:
            if not request.apply:
                return self._planned_result(
                    refresh_id=refresh_id,
                    prior=prior,
                    outcome="no_op",
                    terminal=prior.state_commitment_sha256,
                )
            return self._write_noop_receipt(
                request=request,
                frontier=frontier,
                prior=prior,
                policy_sha=policy_sha,
                refresh_id=refresh_id,
                idempotency=idempotency,
                finalization_hook=finalization_hook,
            )

        changes, _fact_mode, source_reads, current_reads = self._plan_changes(
            frontier=frontier,
            prior=prior,
            document_checkpoint=request.document_checkpoint,
        )
        source_event_count = _source_event_count(frontier=frontier, prior=prior)
        fact_count = sum(item["entity_kind"] == "fact" for item in changes)
        document_count = sum(item["entity_kind"] == "document" for item in changes)
        narrative_count = sum(item["entity_kind"] == "narrative" for item in changes)
        if not request.apply:
            terminal = _transition_commitment(
                prior=prior,
                frontier=frontier,
                policy_sha=policy_sha,
                changes=changes,
            )["state"]
            return LatestGovernedRefreshResult(
                mode="dry_run",
                outcome="changed",
                refresh_id=refresh_id,
                head_id=None if prior is None else prior.receipt_id,
                created_count=0,
                replayed_count=0,
                source_event_count=source_event_count,
                fact_change_count=fact_count,
                document_change_count=document_count,
                narrative_change_count=narrative_count,
                source_read_count=source_reads,
                current_read_count=current_reads,
                current_write_count=0,
                receipt_write_count=0,
                terminal_commitment=terminal,
            )

        self._ensure_run(
            refresh_id=refresh_id,
            idempotency=idempotency,
            request=request,
            frontier=frontier,
            policy_sha=policy_sha,
            prior=prior,
        )
        staged_created, staged_replayed, batches, cursor = self._stage_changes(
            refresh_id=refresh_id,
            changes=changes,
            request=request,
        )
        if (
            request.interrupt_after_batches is not None
            and batches >= request.interrupt_after_batches
            and cursor < len(changes)
        ):
            return LatestGovernedRefreshResult(
                mode="apply",
                outcome="staged",
                refresh_id=refresh_id,
                head_id=None if prior is None else prior.receipt_id,
                created_count=staged_created,
                replayed_count=staged_replayed,
                source_event_count=source_event_count,
                fact_change_count=fact_count,
                document_change_count=document_count,
                narrative_change_count=narrative_count,
                source_read_count=source_reads,
                current_read_count=current_reads,
                current_write_count=0,
                receipt_write_count=0,
                terminal_commitment=(
                    prior.state_commitment_sha256 if prior is not None else "0" * 64
                ),
                resume_cursor=str(cursor),
            )
        receipt, current_writes, finalized_replay = self._finalize(
            request=request,
            frontier=frontier,
            prior=prior,
            policy_sha=policy_sha,
            refresh_id=refresh_id,
            idempotency=idempotency,
            planned_changes=changes,
            finalization_hook=finalization_hook,
        )
        return LatestGovernedRefreshResult(
            mode="apply",
            outcome="changed",
            refresh_id=refresh_id,
            head_id=receipt.receipt_id,
            created_count=staged_created + current_writes + 1,
            replayed_count=staged_replayed + finalized_replay,
            source_event_count=source_event_count,
            fact_change_count=receipt.fact_change_count,
            document_change_count=receipt.document_change_count,
            narrative_change_count=receipt.narrative_change_count,
            source_read_count=source_reads,
            current_read_count=current_reads,
            current_write_count=current_writes,
            receipt_write_count=1,
            terminal_commitment=receipt.terminal_commitment,
        )

    def reproject(
        self,
        request: LatestGovernedReprojectionRequest,
    ) -> LatestGovernedRefreshResult:
        """Advance history while restoring the exact projection at a prior receipt."""

        prior = self._head(request.scope_id)
        if prior is None:
            raise LatestGovernedStateError("reprojection requires an existing current head")
        if prior.receipt_id != request.expected_current_receipt_id:
            raise LatestGovernedStateError(
                "reprojection expected current receipt does not match the locked head"
            )
        target = self._receipt_head(
            scope_id=request.scope_id,
            receipt_id=request.target_receipt_id,
        )
        if target.receipt_id == prior.receipt_id:
            raise LatestGovernedStateError("reprojection target must be a prior immutable receipt")
        self._require_ancestor(
            scope_id=request.scope_id,
            descendant_receipt_id=prior.receipt_id,
            ancestor_receipt_id=target.receipt_id,
        )
        if _datetime(request.operation_recorded_at) < _datetime(
            prior.source_frontier.observed_through
        ):
            raise LatestGovernedStateError(
                "operation_recorded_at must not precede the current governed frontier"
            )
        target_snapshot = self._snapshot_at_receipt(
            scope_id=request.scope_id,
            receipt_id=target.receipt_id,
        )
        changes, current_reads = self._reprojection_changes(
            scope_id=request.scope_id,
            target_snapshot=target_snapshot,
        )
        policy_sha = _digest(
            {
                "policy_version": request.policy_version,
                "reprojection_target_receipt_id": target.receipt_id,
                "retention_policy": RETENTION_POLICY_PROPOSAL,
            }
        )
        idempotency = _digest(
            {
                "scope_id": request.scope_id,
                "prior_head": prior.state_commitment_sha256,
                "reprojection_target_receipt_id": target.receipt_id,
                "policy_config_sha256": policy_sha,
            }
        )
        refresh_id = f"latest-run:{idempotency[:40]}"
        replay = self._replayed_result(refresh_id, True)
        if replay is not None:
            return replay
        refresh_request = LatestGovernedRefreshRequest(
            scope_id=request.scope_id,
            operation_recorded_at=request.operation_recorded_at,
            policy_version=request.policy_version,
            max_batch_rows=request.max_batch_rows,
            apply=True,
        )
        self._ensure_run(
            refresh_id=refresh_id,
            idempotency=idempotency,
            request=refresh_request,
            frontier=target.source_frontier,
            policy_sha=policy_sha,
            prior=prior,
        )
        staged_created, staged_replayed, _batches, _cursor = self._stage_changes(
            refresh_id=refresh_id,
            changes=changes,
            request=refresh_request,
        )
        receipt, current_writes, finalized_replay = self._finalize(
            request=refresh_request,
            frontier=target.source_frontier,
            prior=prior,
            policy_sha=policy_sha,
            refresh_id=refresh_id,
            idempotency=idempotency,
            planned_changes=changes,
            projection_target=target,
            rollback_target_receipt_id=target.receipt_id,
        )
        counts = {
            kind: sum(item["entity_kind"] == kind for item in changes)
            for kind in ("fact", "document", "narrative")
        }
        return LatestGovernedRefreshResult(
            mode="apply",
            outcome="changed",
            refresh_id=refresh_id,
            head_id=receipt.receipt_id,
            created_count=staged_created + current_writes + 1,
            replayed_count=staged_replayed + finalized_replay,
            source_event_count=0,
            fact_change_count=counts["fact"],
            document_change_count=counts["document"],
            narrative_change_count=counts["narrative"],
            source_read_count=0,
            current_read_count=current_reads,
            current_write_count=current_writes,
            receipt_write_count=0 if finalized_replay else 1,
            terminal_commitment=receipt.terminal_commitment,
        )

    def _frontier(self, scope_id: str) -> ChangeFrontier:
        population = self._conn.execute(
            "SELECT population_run_id,receipt_set_sha256,knowledge_cutoff,"
            "observed_through FROM v_population_cutover_current"
        ).fetchall()
        if len(population) != 1:
            raise LatestGovernedStateError(
                "exactly one current sealed population cutover is required"
            )
        promotion = self._conn.execute(
            "SELECT promotion_id,scope_key,status,research_snapshot_id,"
            "fact_generation_id,fact_projection_seal_sha256,"
            "source_inventory_set_json,narrative_bundles_json,cutoff_at,"
            "population_run_id,population_receipt_set_sha256,"
            "population_observed_through,issuer_id,reporting_entity_id,"
            "source_scope_key,source_scope_revision_id "
            "FROM v_ask_retrieval_scope_current WHERE scope_key=?",
            (scope_id,),
        ).fetchall()
        if len(promotion) != 1 or str(promotion[0][2]) != "promoted":
            raise LatestGovernedStateError("one current promoted Ask retrieval scope is required")
        population_row, promoted = population[0], promotion[0]
        expected = (
            str(population_row[0]),
            str(population_row[1]),
            _canonical_time(population_row[2]),
            _canonical_time(population_row[3]),
        )
        actual = (
            str(promoted[9]),
            str(promoted[10]),
            _canonical_time(promoted[8]),
            _canonical_time(promoted[11]),
        )
        if actual != expected:
            raise LatestGovernedStateError(
                "current Ask promotion is not exactly bound to the current population cutover"
            )
        seal = self._conn.execute(
            "SELECT projection_seal_sha256 FROM canonical_fact_projection_seals "
            "WHERE generation_id=?",
            (promoted[4],),
        ).fetchall()
        if len(seal) != 1 or str(seal[0][0]) != str(promoted[5]):
            raise LatestGovernedStateError(
                "current Ask promotion fact projection seal is missing or different"
            )
        issuer_id = str(promoted[12])
        reporting_entity_id = str(promoted[13])
        source_scope_key = str(promoted[14])
        source_scope_revision_id = str(promoted[15])
        if scope_id != derive_retrieval_scope_id(
            source_scope_key=source_scope_key,
            issuer_id=issuer_id,
        ):
            raise LatestGovernedStateError(
                "current Ask promotion scope ID does not match its source composite identity"
            )
        source_scopes = self._conn.execute(
            "SELECT scope_revision_id FROM v_issuer_reporting_scope_current "
            "WHERE scope_key=? AND issuer_id=? AND inclusion_state='core'",
            (source_scope_key, issuer_id),
        ).fetchall()
        if len(source_scopes) != 1 or str(source_scopes[0][0]) != source_scope_revision_id:
            raise LatestGovernedStateError(
                "current Ask promotion does not bind the exact current source scope revision"
            )
        try:
            raw_inventory_ids: object = json.loads(str(promoted[6]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise LatestGovernedStateError(
                "current Ask promotion source inventories are malformed"
            ) from exc
        if not isinstance(raw_inventory_ids, list):
            raise LatestGovernedStateError("current Ask promotion source inventories are malformed")
        raw_inventory_list = cast(list[object], raw_inventory_ids)
        if any(
            not isinstance(inventory_id, str) or not inventory_id
            for inventory_id in raw_inventory_list
        ):
            raise LatestGovernedStateError("current Ask promotion source inventories are malformed")
        inventory_ids = tuple(cast(str, inventory_id) for inventory_id in raw_inventory_list)
        source_inventory_ids_json = _canonical_json(list(inventory_ids))
        if (
            not inventory_ids
            or list(inventory_ids) != sorted(set(inventory_ids))
            or source_inventory_ids_json != str(promoted[6])
        ):
            raise LatestGovernedStateError("current Ask promotion source inventories are malformed")
        inventory_rows = self._conn.execute(
            "SELECT inventory.snapshot_id,inventory.issuer_id,inventory.outcome,"
            "seal.completion_status "
            "FROM source_inventory_snapshots inventory "
            "JOIN source_inventory_snapshot_seals seal "
            "ON seal.snapshot_id=inventory.snapshot_id "
            "WHERE inventory.snapshot_id IN ("
            "SELECT CAST(value AS TEXT) FROM json_each(?)) "
            "ORDER BY inventory.snapshot_id",
            (source_inventory_ids_json,),
        ).fetchall()
        if (
            len(inventory_rows) != len(inventory_ids)
            or tuple(str(row[0]) for row in inventory_rows) != inventory_ids
        ):
            raise LatestGovernedStateError(
                "current Ask promotion source inventory is missing or unsealed"
            )
        if any(str(row[1]) != issuer_id for row in inventory_rows):
            raise LatestGovernedStateError(
                "current Ask promotion source inventory does not bind to its issuer"
            )
        if any(
            str(row[2]) not in {"succeeded", "partial"} or str(row[3]) != "complete"
            for row in inventory_rows
        ):
            raise LatestGovernedStateError(
                "current Ask promotion source inventory is not sealed complete"
            )
        universe = self._conn.execute(
            "SELECT issuer_id,reporting_entity_ids_json "
            "FROM research_snapshot_universe_commitments "
            "WHERE research_snapshot_id=?",
            (promoted[3],),
        ).fetchall()
        if len(universe) != 1:
            raise LatestGovernedStateError(
                "current research snapshot has no unambiguous universe binding"
            )
        if str(universe[0][0]) != issuer_id:
            raise LatestGovernedStateError(
                "current Ask promotion issuer does not match its research universe"
            )
        try:
            raw_reporting_entity_ids: object = json.loads(str(universe[0][1]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise LatestGovernedStateError(
                "current research universe reporting entities are malformed"
            ) from exc
        if not isinstance(raw_reporting_entity_ids, list):
            raise LatestGovernedStateError(
                "current research universe reporting entities are malformed"
            )
        raw_reporting_entity_list = cast(list[object], raw_reporting_entity_ids)
        if any(
            not isinstance(entity_id, str) or not entity_id
            for entity_id in raw_reporting_entity_list
        ):
            raise LatestGovernedStateError(
                "current research universe reporting entities are malformed"
            )
        reporting_entity_ids = tuple(
            cast(str, entity_id) for entity_id in raw_reporting_entity_list
        )
        if (
            not reporting_entity_ids
            or list(reporting_entity_ids) != sorted(set(reporting_entity_ids))
            or _canonical_json(list(reporting_entity_ids)) != str(universe[0][1])
        ):
            raise LatestGovernedStateError(
                "current research universe reporting entities are malformed"
            )
        if reporting_entity_id not in reporting_entity_ids:
            raise LatestGovernedStateError(
                "current Ask promotion reporting entity is outside its research universe"
            )
        bound_entities = self._conn.execute(
            "SELECT reporting_entity_id,issuer_id FROM reporting_entities "
            "WHERE reporting_entity_id=?",
            (reporting_entity_id,),
        ).fetchall()
        if len(bound_entities) != 1 or (
            str(bound_entities[0][0]),
            str(bound_entities[0][1]),
        ) != (reporting_entity_id, issuer_id):
            raise LatestGovernedStateError(
                "current Ask promotion reporting entity does not bind to its issuer"
            )
        try:
            watermark = int(
                self._conn.execute(
                    "SELECT COALESCE(MAX(publication_sequence),0) "
                    "FROM source_fact_publication_stream "
                    "WHERE datetime(sealed_at)<=datetime(?) "
                    "AND datetime(assigned_at)<=datetime(?)",
                    (population_row[2], population_row[3]),
                ).fetchone()[0]
            )
        except sqlite3.OperationalError as exc:
            raise LatestGovernedStateError(
                "source fact publication frontier is unavailable"
            ) from exc
        bundles = _canonical_json(json.loads(str(promoted[7])))
        return ChangeFrontier(
            scope_id=scope_id,
            population_run_id=expected[0],
            population_receipt_set_sha256=expected[1],
            promotion_id=str(promoted[0]),
            fact_generation_id=str(promoted[4]),
            fact_projection_seal_sha256=str(promoted[5]),
            research_snapshot_id=str(promoted[3]),
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            source_inventory_ids_json=source_inventory_ids_json,
            narrative_bundles_json=bundles,
            source_publication_high_watermark=watermark,
            knowledge_cutoff=_datetime(population_row[2]),
            observed_through=_datetime(population_row[3]),
        )

    def _head(self, scope_id: str) -> CurrentHead | None:
        row = self._conn.execute(
            "SELECT head.refresh_receipt_id,head.fact_generation_id,"
            "head.source_heads_sha256,head.state_sha256,head.fact_root_sha256,"
            "head.document_root_sha256,head.narrative_root_sha256,"
            "receipt.prior_receipt_id,head.source_heads_json "
            "FROM latest_governed_scope_heads head "
            "JOIN latest_governed_refresh_receipts receipt "
            "ON receipt.receipt_id=head.refresh_receipt_id "
            "WHERE head.scope_key=?",
            (scope_id,),
        ).fetchone()
        if row is None:
            return None
        source_frontier = ChangeFrontier.model_validate(_json_object(row[8]))
        if source_frontier.commitment_sha256 != str(row[2]):
            raise LatestGovernedStateError("latest governed source frontier is tampered")
        return CurrentHead(
            scope_id=scope_id,
            receipt_id=str(row[0]),
            fact_generation_id=str(row[1]),
            input_frontier_sha256=str(row[2]),
            state_commitment_sha256=str(row[3]),
            fact_root_sha256=str(row[4]),
            document_root_sha256=str(row[5]),
            narrative_root_sha256=str(row[6]),
            rollback_receipt_id=_optional_text(row[7]),
            source_frontier=source_frontier,
        )

    def _receipt_head(self, *, scope_id: str, receipt_id: str) -> CurrentHead:
        row = self._conn.execute(
            "SELECT receipt_id,scope_key,prior_receipt_id,current_state_sha256,"
            "fact_root_sha256,document_root_sha256,narrative_root_sha256,"
            "fact_generation_id,input_head_sha256,canonical_receipt_json,"
            "receipt_sha256 FROM latest_governed_refresh_receipts "
            "WHERE receipt_id=? AND scope_key=?",
            (receipt_id, scope_id),
        ).fetchone()
        if row is None:
            raise LatestGovernedStateError(
                "reprojection target receipt is missing from the immutable history"
            )
        payload_json = str(row[9])
        payload = _json_object(payload_json)
        raw_frontier = payload.get("frontier")
        if not isinstance(raw_frontier, dict):
            raise LatestGovernedStateError(
                "reprojection target receipt predates restorable frontier snapshots"
            )
        frontier = ChangeFrontier.model_validate(raw_frontier)
        expected = (
            receipt_id,
            scope_id,
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
        )
        actual = (
            payload.get("receipt_id"),
            payload.get("scope_id"),
            payload.get("current_state_sha256"),
            cast(dict[str, object], payload.get("roots", {})).get("fact"),
            cast(dict[str, object], payload.get("roots", {})).get("document"),
            cast(dict[str, object], payload.get("roots", {})).get("narrative"),
            cast(dict[str, object], payload.get("baseline", {})).get("fact_generation_id"),
            payload.get("frontier_sha256"),
        )
        if (
            actual != expected
            or _digest(payload_json) != str(row[10])
            or frontier.commitment_sha256 != str(row[8])
        ):
            raise LatestGovernedStateError(
                "reprojection target receipt identity or commitment is invalid"
            )
        return CurrentHead(
            scope_id=scope_id,
            receipt_id=receipt_id,
            fact_generation_id=str(row[7]),
            input_frontier_sha256=str(row[8]),
            state_commitment_sha256=str(row[3]),
            fact_root_sha256=str(row[4]),
            document_root_sha256=str(row[5]),
            narrative_root_sha256=str(row[6]),
            rollback_receipt_id=_optional_text(row[2]),
            source_frontier=frontier,
        )

    def _require_ancestor(
        self,
        *,
        scope_id: str,
        descendant_receipt_id: str,
        ancestor_receipt_id: str,
    ) -> None:
        cursor: str | None = descendant_receipt_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor in visited:
                raise LatestGovernedStateError("governed receipt history contains a cycle")
            visited.add(cursor)
            if cursor == ancestor_receipt_id:
                return
            row = self._conn.execute(
                "SELECT prior_receipt_id FROM latest_governed_refresh_receipts "
                "WHERE receipt_id=? AND scope_key=?",
                (cursor, scope_id),
            ).fetchone()
            if row is None:
                raise LatestGovernedStateError(
                    "governed receipt history is incomplete or crosses scope"
                )
            cursor = _optional_text(row[0])
        raise LatestGovernedStateError("reprojection target is not an ancestor of the current head")

    def _snapshot_at_receipt(
        self,
        *,
        scope_id: str,
        receipt_id: str,
    ) -> dict[tuple[str, str], dict[str, object]]:
        chain: list[str] = []
        cursor: str | None = receipt_id
        visited: set[str] = set()
        while cursor is not None:
            if cursor in visited:
                raise LatestGovernedStateError("governed receipt history contains a cycle")
            visited.add(cursor)
            row = self._conn.execute(
                "SELECT prior_receipt_id FROM latest_governed_refresh_receipts "
                "WHERE receipt_id=? AND scope_key=?",
                (cursor, scope_id),
            ).fetchone()
            if row is None:
                raise LatestGovernedStateError(
                    "governed receipt history is incomplete or crosses scope"
                )
            chain.append(cursor)
            cursor = _optional_text(row[0])
        snapshot: dict[tuple[str, str], dict[str, object]] = {}
        for chain_receipt_id in reversed(chain):
            receipt = self._conn.execute(
                "SELECT change_count,canonical_change_set_json,change_set_sha256,"
                "fact_generation_id,prior_receipt_id,current_state_sha256,"
                "fact_root_sha256,document_root_sha256,narrative_root_sha256,"
                "canonical_receipt_json,knowledge_cutoff,observed_through,sealed_at "
                "FROM latest_governed_refresh_receipts WHERE receipt_id=?",
                (chain_receipt_id,),
            ).fetchone()
            if receipt is None:
                raise LatestGovernedStateError("governed receipt history disappeared")
            change_set_json = str(receipt[1])
            decoded_hashes: object = json.loads(change_set_json)
            if not isinstance(decoded_hashes, list) or _digest(change_set_json) != str(receipt[2]):
                raise LatestGovernedStateError("governed receipt change-set commitment is invalid")
            raw_hashes = cast(list[object], decoded_hashes)
            receipt_payload = _json_object(receipt[9])
            raw_audit = receipt_payload.get("change_audit")
            if not isinstance(raw_audit, dict):
                raise LatestGovernedStateError("governed receipt change-audit contract is missing")
            audit = cast(dict[str, object], raw_audit)
            rows = self._conn.execute(
                "SELECT change_ordinal,entity_kind,change_kind,coordinate_key,"
                "prior_commitment_sha256,current_commitment_sha256,"
                "canonical_change_json,change_sha256,selection_reason,"
                "source_evidence_json,source_evidence_sha256 "
                "FROM latest_governed_refresh_changes WHERE receipt_id=? "
                "ORDER BY change_ordinal",
                (chain_receipt_id,),
            ).fetchall()
            if audit.get("mode") == "baseline_digest_buckets.v1":
                if receipt[4] is not None or rows:
                    raise LatestGovernedStateError(
                        "baseline receipt contains coordinate audit history"
                    )
                raw_frontier = receipt_payload.get("frontier")
                if not isinstance(raw_frontier, dict):
                    raise LatestGovernedStateError("baseline receipt source frontier is missing")
                frontier = ChangeFrontier.model_validate(raw_frontier)
                changes = self._baseline_source_changes(frontier)
                expected_change_set, expected_mode = _change_audit(
                    prior=None,
                    changes=changes,
                )
                policy_sha = str(receipt_payload.get("policy_config_sha256"))
                transitions = _transition_commitment(
                    prior=None,
                    frontier=frontier,
                    policy_sha=policy_sha,
                    changes=changes,
                )
                audit_change_count = audit.get("change_count")
                audit_bucket_count = audit.get("bucket_count")
                if (
                    not isinstance(audit_change_count, int)
                    or isinstance(audit_change_count, bool)
                    or not isinstance(audit_bucket_count, int)
                    or isinstance(audit_bucket_count, bool)
                ):
                    raise LatestGovernedStateError(
                        "baseline receipt change-audit counts are invalid"
                    )
                if (
                    expected_mode != audit.get("mode")
                    or raw_hashes != expected_change_set
                    or len(changes) != int(receipt[0])
                    or audit_change_count != len(changes)
                    or audit_bucket_count != len(expected_change_set)
                    or len(expected_change_set) > 4_096
                    or (
                        transitions["state"],
                        transitions["fact"],
                        transitions["document"],
                        transitions["narrative"],
                    )
                    != (str(receipt[5]), str(receipt[6]), str(receipt[7]), str(receipt[8]))
                ):
                    raise LatestGovernedStateError(
                        "baseline receipt source reprojection commitment is invalid"
                    )
                for change in changes:
                    if change["change_kind"] != "upsert":
                        continue
                    key = (
                        str(change["entity_kind"]),
                        str(change["coordinate_key"]),
                    )
                    snapshot[key] = {
                        "change_kind": change["change_kind"],
                        "coordinate_key": change["coordinate_key"],
                        "current_commitment_sha256": change["current_commitment_sha256"],
                        "entity_kind": change["entity_kind"],
                        "prior_commitment_sha256": None,
                        "canonical_payload": change["payload"],
                        "refresh_receipt_id": chain_receipt_id,
                        "knowledge_cutoff": receipt[10],
                        "observed_through": receipt[11],
                        "updated_at": receipt[12],
                    }
                continue
            if audit.get("mode") != "coordinate_changes.v1":
                raise LatestGovernedStateError("governed receipt change-audit mode is invalid")
            if len(rows) != int(receipt[0]) or len(rows) != len(raw_hashes):
                raise LatestGovernedStateError("governed receipt change history is incomplete")
            for ordinal, (row, raw_transition_sha) in enumerate(zip(rows, raw_hashes, strict=True)):
                if not isinstance(raw_transition_sha, str):
                    raise LatestGovernedStateError(
                        "governed receipt transition commitment is invalid"
                    )
                change_json = str(row[6])
                change = _json_object(change_json)
                expected_identity = (
                    ordinal,
                    change.get("entity_kind"),
                    change.get("change_kind"),
                    change.get("coordinate_key"),
                    change.get("prior_commitment_sha256"),
                    change.get("current_commitment_sha256"),
                )
                actual_identity = (
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    row[4],
                    row[5],
                )
                if (
                    actual_identity != expected_identity
                    or _digest(change_json) != str(row[7])
                    or raw_transition_sha != _transition_change_digest(change)
                ):
                    raise LatestGovernedStateError(
                        "governed receipt change identity or commitment is invalid"
                    )
                key = (str(row[1]), str(row[3]))
                if str(row[2]) == "delete":
                    snapshot.pop(key, None)
                else:
                    evidence_json = str(row[9])
                    if _digest(evidence_json) != str(row[10]):
                        raise LatestGovernedStateError(
                            "governed receipt evidence commitment is invalid"
                        )
                    evidence = _json_object(evidence_json)
                    payload, source_commitment = self._reprojection_source_payload(
                        entity_kind=str(row[1]),
                        coordinate=str(row[3]),
                        selection_reason=str(row[8]),
                        evidence=evidence,
                        fact_generation_id=str(receipt[3]),
                    )
                    if source_commitment != _optional_text(row[5]):
                        raise LatestGovernedStateError(
                            "governed receipt source coordinate commitment changed"
                        )
                    snapshot[key] = {
                        **change,
                        "canonical_payload": payload,
                        "refresh_receipt_id": chain_receipt_id,
                        "knowledge_cutoff": receipt[10],
                        "observed_through": receipt[11],
                        "updated_at": receipt[12],
                    }
        return snapshot

    def _reprojection_source_payload(
        self,
        *,
        entity_kind: str,
        coordinate: str,
        selection_reason: str,
        evidence: dict[str, object],
        fact_generation_id: str,
    ) -> tuple[dict[str, object], str]:
        if entity_kind == "fact":
            generation_id = str(evidence.get("fact_generation_id", fact_generation_id))
            original = self._conn.row_factory
            self._conn.row_factory = sqlite3.Row
            try:
                rows = self._conn.execute(
                    "SELECT * FROM canonical_fact_projection_entries "
                    "WHERE generation_id=? AND canonical_metric_cell_id=? "
                    "AND change_kind='upsert'",
                    (generation_id, coordinate),
                ).fetchall()
            finally:
                self._conn.row_factory = original
            if len(rows) != 1:
                raise LatestGovernedStateError(
                    "immutable fact source coordinate is missing or ambiguous"
                )
            source = rows[0]
            payload: dict[str, object] = {
                "row": {
                    "canonical_metric_cell_id": coordinate,
                    "canonical_resolution_revision_id": source["canonical_resolution_revision_id"],
                    "selected_observation_id": source["selected_observation_id"],
                    "canonical_metric_name": source["canonical_metric_name"],
                    "period_kind": source["period_kind"],
                    "period_start": source["period_start"],
                    "period_end": source["period_end"],
                    "unit_key": source["unit_key"],
                    "currency": source["currency"],
                    "value_kind": source["value_kind"],
                    "canonical_value": source["canonical_value"],
                    "canonical_search_text": source["canonical_search_text"],
                },
                "selection_reason": selection_reason,
                "source_evidence": evidence,
            }
            return payload, str(source["entry_sha256"])
        if entity_kind == "document":
            rows = self._conn.execute(
                "SELECT expected.expected_document_id,membership.document_version_id,"
                "expected.source_kind,expected.document_type,expected.period_start,"
                "expected.period_end,document.blob_sha256 "
                "FROM search_corpus_document_memberships membership "
                "JOIN expected_documents expected "
                "ON expected.expected_document_key=membership.expected_document_key "
                "JOIN evidence_document_versions document "
                "ON document.document_version_id=membership.document_version_id "
                "WHERE membership.manifest_id=? "
                "AND membership.expected_document_key=? "
                "AND expected.snapshot_id=? "
                "AND membership.membership_status='included'",
                (
                    evidence.get("corpus_manifest_id"),
                    coordinate,
                    evidence.get("source_inventory_snapshot_id"),
                ),
            ).fetchall()
            if len(rows) != 1:
                raise LatestGovernedStateError(
                    "immutable document source coordinate is missing or ambiguous"
                )
            source = rows[0]
            document_row: dict[str, object] = {
                "expected_document_id": source[0],
                "document_version_id": source[1],
                "source_kind": source[2],
                "document_type": source[3],
                "period_start": source[4],
                "period_end": source[5],
            }
            payload = {
                "row": document_row,
                "selection_reason": selection_reason,
                "source_evidence": evidence,
            }
            return payload, _digest(
                {
                    "document_blob_sha256": source[6],
                    "row": document_row,
                }
            )
        if entity_kind != "narrative":
            raise LatestGovernedStateError("governed receipt entity kind is invalid")
        expected_document_key, chunk_key = coordinate.split("\x00", 1)
        rows = self._conn.execute(
            "SELECT chunk.chunk_id,chunk.evidence_node_id,chunk.text,"
            "chunk.content_sha256,chunk.chunker_config_sha256,"
            "run.document_version_id "
            "FROM search_chunks chunk "
            "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=node.extraction_run_id "
            "WHERE chunk.manifest_id=? AND chunk.chunk_id=? "
            "AND chunk.chunk_key=? AND chunk.evidence_node_id=? "
            "AND run.document_version_id=?",
            (
                evidence.get("corpus_manifest_id"),
                evidence.get("source_chunk_id"),
                chunk_key,
                evidence.get("evidence_node_id"),
                evidence.get("document_version_id"),
            ),
        ).fetchall()
        if len(rows) != 1:
            raise LatestGovernedStateError(
                "immutable narrative source coordinate is missing or ambiguous"
            )
        source = rows[0]
        narrative_row: dict[str, object] = {
            "expected_document_key": expected_document_key,
            "chunk_key": chunk_key,
            "document_version_id": source[5],
            "evidence_node_id": source[1],
            "source_chunk_id": source[0],
            "embedding_artifact_id": evidence.get("embedding_artifact_id"),
            "text": source[2],
            "content_sha256": source[3],
            "chunker_config_sha256": source[4],
        }
        payload = {
            "row": narrative_row,
            "selection_reason": selection_reason,
            "source_evidence": evidence,
        }
        return payload, _digest(
            {
                "content_sha256": source[3],
                "chunker_config_sha256": source[4],
                "document_version_id": source[5],
                "embedding_artifact_id": evidence.get("embedding_artifact_id"),
                "evidence_node_id": source[1],
            }
        )

    def _reprojection_changes(
        self,
        *,
        scope_id: str,
        target_snapshot: dict[tuple[str, str], dict[str, object]],
    ) -> tuple[list[dict[str, object]], int]:
        current: dict[tuple[str, str], str] = {}
        for coordinate, commitment in self._conn.execute(
            "SELECT canonical_metric_cell_id,current_commitment_sha256 "
            "FROM latest_governed_fact_entries WHERE scope_key=?",
            (scope_id,),
        ):
            current[("fact", str(coordinate))] = str(commitment)
        for coordinate, commitment in self._conn.execute(
            "SELECT expected_document_key,current_commitment_sha256 "
            "FROM latest_governed_document_entries WHERE scope_key=?",
            (scope_id,),
        ):
            current[("document", str(coordinate))] = str(commitment)
        for document_key, chunk_key, commitment in self._conn.execute(
            "SELECT expected_document_key,chunk_key,current_commitment_sha256 "
            "FROM latest_governed_narrative_entries WHERE scope_key=?",
            (scope_id,),
        ):
            current[("narrative", f"{document_key}\x00{chunk_key}")] = str(commitment)
        changes: list[dict[str, object]] = []
        for key in sorted(set(current) | set(target_snapshot)):
            entity_kind, coordinate = key
            prior_sha = current.get(key)
            target_change = target_snapshot.get(key)
            target_sha = (
                None
                if target_change is None
                else _optional_text(target_change.get("current_commitment_sha256"))
            )
            if prior_sha == target_sha:
                continue
            if target_change is None:
                row: dict[str, object]
                if entity_kind == "narrative":
                    document_key, chunk_key = coordinate.split("\x00", 1)
                    row = {
                        "expected_document_key": document_key,
                        "chunk_key": chunk_key,
                    }
                elif entity_kind == "document":
                    row = {"expected_document_key": coordinate}
                else:
                    row = {"canonical_metric_cell_id": coordinate}
                changes.append(
                    _stage_change(
                        entity_kind=cast(Literal["fact", "document", "narrative"], entity_kind),
                        change_kind="delete",
                        coordinate=coordinate,
                        prior_sha=prior_sha,
                        current_sha=None,
                        row=row,
                        reason="forward_reprojection_remove",
                        evidence={"reprojection_target": "prior_immutable_receipt"},
                    )
                )
                continue
            raw_payload = target_change.get("canonical_payload")
            if not isinstance(raw_payload, dict):
                raise LatestGovernedStateError("reprojection target canonical payload is invalid")
            payload = cast(dict[str, object], raw_payload)
            raw_row = payload.get("row")
            raw_evidence = payload.get("source_evidence")
            if not isinstance(raw_row, dict) or not isinstance(raw_evidence, dict):
                raise LatestGovernedStateError("reprojection target row or provenance is invalid")
            target_row = cast(dict[str, object], raw_row)
            evidence = cast(dict[str, object], raw_evidence)
            changes.append(
                _stage_change(
                    entity_kind=cast(Literal["fact", "document", "narrative"], entity_kind),
                    change_kind="upsert",
                    coordinate=coordinate,
                    prior_sha=prior_sha,
                    current_sha=target_sha,
                    row=target_row,
                    reason=str(payload.get("selection_reason")),
                    evidence=evidence,
                )
            )
        changes.sort(key=lambda item: (str(item["entity_kind"]), str(item["coordinate_key"])))
        for ordinal, item in enumerate(changes):
            item["stage_ordinal"] = ordinal
        return changes, len(current)

    def _plan_changes(
        self,
        *,
        frontier: ChangeFrontier,
        prior: CurrentHead | None,
        document_checkpoint: bool = False,
    ) -> tuple[list[dict[str, object]], str, int, int]:
        identity_rollover = prior is not None and (
            prior.source_frontier.issuer_id != frontier.issuer_id
            or prior.source_frontier.reporting_entity_id != frontier.reporting_entity_id
        )
        effective_document_checkpoint = document_checkpoint or identity_rollover
        fact_rows, fact_mode = self._fact_source_rows(frontier, prior)
        fact_changes, fact_current_reads = self._fact_changes(
            frontier.scope_id,
            frontier,
            fact_rows,
            checkpoint=fact_mode == "checkpoint",
        )
        documents_unchanged = (
            prior is not None
            and not identity_rollover
            and prior.source_frontier.source_inventory_ids_json
            == frontier.source_inventory_ids_json
            and prior.source_frontier.narrative_bundles_json == frontier.narrative_bundles_json
        )
        if documents_unchanged:
            document_changes: list[dict[str, object]] = []
            narrative_changes: list[dict[str, object]] = []
            document_source_reads = 0
            document_current_reads = 0
        else:
            (
                document_changes,
                narrative_changes,
                document_source_reads,
                document_current_reads,
            ) = self._document_and_narrative_changes(
                frontier,
                prior=prior,
                checkpoint=prior is None,
                document_checkpoint=effective_document_checkpoint,
            )
        changes = sorted(
            [*fact_changes, *document_changes, *narrative_changes],
            key=lambda item: (str(item["entity_kind"]), str(item["coordinate_key"])),
        )
        for ordinal, item in enumerate(changes):
            item["stage_ordinal"] = ordinal
        return (
            changes,
            fact_mode,
            len(fact_rows) + document_source_reads,
            fact_current_reads + document_current_reads,
        )

    def _baseline_source_changes(
        self,
        frontier: ChangeFrontier,
    ) -> list[dict[str, object]]:
        fact_rows, _fact_mode = self._fact_source_rows(frontier, None)
        fact_changes, _fact_reads = self._fact_changes(
            frontier.scope_id,
            frontier,
            fact_rows,
            checkpoint=True,
            ignore_current=True,
        )
        (
            document_changes,
            narrative_changes,
            _document_source_reads,
            _document_current_reads,
        ) = self._document_and_narrative_changes(
            frontier,
            prior=None,
            checkpoint=True,
            ignore_current=True,
        )
        changes = sorted(
            [*fact_changes, *document_changes, *narrative_changes],
            key=lambda item: (str(item["entity_kind"]), str(item["coordinate_key"])),
        )
        for ordinal, item in enumerate(changes):
            item["stage_ordinal"] = ordinal
        return changes

    def _fact_source_rows(
        self,
        frontier: ChangeFrontier,
        prior: CurrentHead | None,
    ) -> tuple[list[sqlite3.Row], Literal["direct_delta", "checkpoint"]]:
        original = self._conn.row_factory
        self._conn.row_factory = sqlite3.Row
        try:
            generation = self._conn.execute(
                "SELECT generation_kind,parent_generation_id "
                "FROM canonical_fact_projection_generations WHERE generation_id=?",
                (frontier.fact_generation_id,),
            ).fetchone()
            if generation is None:
                raise LatestGovernedStateError("frontier fact generation is missing")
            direct = (
                prior is not None
                and str(generation["generation_kind"]) == "delta"
                and str(generation["parent_generation_id"]) == prior.fact_generation_id
                and prior.source_frontier.issuer_id == frontier.issuer_id
                and prior.source_frontier.reporting_entity_id == frontier.reporting_entity_id
            )
            if direct or str(generation["generation_kind"]) == "checkpoint":
                rows = self._conn.execute(
                    "SELECT entry.* FROM canonical_fact_projection_entries entry "
                    "JOIN canonical_metric_cells cell "
                    "ON cell.canonical_metric_cell_id="
                    "entry.canonical_metric_cell_id "
                    "WHERE entry.generation_id=? "
                    "AND cell.reporting_entity_id IN ("
                    "SELECT CAST(value AS TEXT) FROM json_each(?)) "
                    "ORDER BY entry.entry_ordinal",
                    (
                        frontier.fact_generation_id,
                        _canonical_json([frontier.reporting_entity_id]),
                    ),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    _EFFECTIVE_FACT_SQL,
                    (
                        frontier.fact_generation_id,
                        _canonical_json([frontier.reporting_entity_id]),
                    ),
                ).fetchall()
            return list(rows), "direct_delta" if direct else "checkpoint"
        finally:
            self._conn.row_factory = original

    def _fact_changes(
        self,
        scope_id: str,
        frontier: ChangeFrontier,
        rows: list[sqlite3.Row],
        *,
        checkpoint: bool,
        ignore_current: bool = False,
    ) -> tuple[list[dict[str, object]], int]:
        coordinates = tuple(sorted({str(row["canonical_metric_cell_id"]) for row in rows}))
        if ignore_current:
            current_rows = ()
            current_read_count = 0
        elif checkpoint:
            current_rows = self._conn.execute(
                "SELECT canonical_metric_cell_id,current_commitment_sha256 "
                "FROM latest_governed_fact_entries WHERE scope_key=?",
                (scope_id,),
            )
            current_read_count = 0
        else:
            fetched: list[tuple[object, ...]] = []
            for offset in range(0, len(coordinates), 500):
                batch = coordinates[offset : offset + 500]
                if not batch:
                    continue
                placeholders = ",".join("?" for _ in batch)
                fetched.extend(
                    self._conn.execute(
                        "SELECT canonical_metric_cell_id,current_commitment_sha256 "
                        "FROM latest_governed_fact_entries "
                        f"WHERE scope_key=? AND canonical_metric_cell_id IN ({placeholders})",  # nosec B608 -- placeholders are generated from a bounded tuple; values are bound
                        (scope_id, *batch),
                    ).fetchall()
                )
            current_rows = fetched
            current_read_count = len(coordinates)
        current = {str(row[0]): str(row[1]) for row in current_rows}
        changes: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            coordinate = str(row["canonical_metric_cell_id"])
            seen.add(coordinate)
            prior_sha = current.get(coordinate)
            if str(row["change_kind"]) == "delete":
                if prior_sha is not None:
                    changes.append(
                        _stage_change(
                            entity_kind="fact",
                            change_kind="delete",
                            coordinate=coordinate,
                            prior_sha=prior_sha,
                            current_sha=None,
                            row={"canonical_metric_cell_id": coordinate},
                            reason="governed_projection_tombstone",
                            evidence={
                                "fact_generation_id": frontier.fact_generation_id,
                                "selection_outcome": "unresolved_or_retired",
                            },
                        )
                    )
                continue
            current_sha = str(row["entry_sha256"])
            if prior_sha == current_sha:
                continue
            evidence = {
                key: row[key]
                for key in (
                    "binding_commitment_sha256",
                    "binding_revision_id",
                    "evidence_document_version_id",
                    "evidence_locator_json",
                    "evidence_locator_sha256",
                    "evidence_node_id",
                    "mapping_commitment_sha256",
                    "mapping_revision_id",
                    "metric_definition_commitment_sha256",
                    "metric_definition_revision_id",
                    "selected_observation_id",
                    "source_fact_cell_id",
                    "source_publication_id",
                    "source_publication_member_id",
                    "source_publication_seal_id",
                )
                if key in row
            }
            evidence["fact_generation_id"] = frontier.fact_generation_id
            changes.append(
                _stage_change(
                    entity_kind="fact",
                    change_kind="upsert",
                    coordinate=coordinate,
                    prior_sha=prior_sha,
                    current_sha=current_sha,
                    row={
                        "canonical_metric_cell_id": coordinate,
                        "canonical_resolution_revision_id": row["canonical_resolution_revision_id"],
                        "selected_observation_id": row["selected_observation_id"],
                        "canonical_metric_name": row["canonical_metric_name"],
                        "period_kind": row["period_kind"],
                        "period_start": row["period_start"],
                        "period_end": row["period_end"],
                        "unit_key": row["unit_key"],
                        "currency": row["currency"],
                        "value_kind": row["value_kind"],
                        "canonical_value": row["canonical_value"],
                        "canonical_search_text": row["canonical_search_text"],
                    },
                    reason="selected_by_governed_canonical_resolution",
                    evidence=evidence,
                )
            )
        if checkpoint:
            for coordinate, prior_sha in sorted(current.items()):
                if coordinate not in seen:
                    changes.append(
                        _stage_change(
                            entity_kind="fact",
                            change_kind="delete",
                            coordinate=coordinate,
                            prior_sha=prior_sha,
                            current_sha=None,
                            row={"canonical_metric_cell_id": coordinate},
                            reason="absent_from_governed_checkpoint",
                            evidence={"fact_generation_id": frontier.fact_generation_id},
                        )
                    )
        return changes, len(current) if checkpoint else current_read_count

    def _document_and_narrative_changes(
        self,
        frontier: ChangeFrontier,
        *,
        prior: CurrentHead | None,
        checkpoint: bool,
        document_checkpoint: bool = False,
        ignore_current: bool = False,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], int, int]:
        bundles = cast(list[object], json.loads(frontier.narrative_bundles_json))
        manifest_coordinates: dict[str, dict[str, object]] = {}
        for raw in bundles:
            if not isinstance(raw, dict):
                raise LatestGovernedStateError("narrative bundle is not an object")
            bundle = cast(dict[str, object], raw)
            manifest_id = str(bundle["corpus_manifest_id"])
            if manifest_id in manifest_coordinates:
                raise LatestGovernedStateError("narrative manifest coordinate is duplicated")
            manifest_coordinates[manifest_id] = bundle
        inventory_ids = tuple(str(item) for item in json.loads(frontier.source_inventory_ids_json))
        if not inventory_ids:
            raise LatestGovernedStateError("current promotion has no source inventories")
        prior_manifest_coordinates: dict[str, dict[str, object]] = {}
        prior_inventory_ids: tuple[str, ...] = ()
        if prior is not None:
            prior_inventory_ids = tuple(
                str(item) for item in json.loads(prior.source_frontier.source_inventory_ids_json)
            )
            inventory_changed = tuple(sorted(prior_inventory_ids)) != tuple(sorted(inventory_ids))
            if inventory_changed and not document_checkpoint:
                raise LatestGovernedStateError(
                    "source inventory set changed; explicit document checkpoint required"
                )
            for raw in cast(
                list[object],
                json.loads(prior.source_frontier.narrative_bundles_json),
            ):
                if not isinstance(raw, dict):
                    raise LatestGovernedStateError("prior narrative bundle is not an object")
                bundle = cast(dict[str, object], raw)
                prior_manifest_coordinates[str(bundle["corpus_manifest_id"])] = bundle
        if document_checkpoint:
            self._validate_active_membership(
                manifest_ids=tuple(sorted(manifest_coordinates)),
                inventory_ids=tuple(sorted(inventory_ids)),
                label="current",
            )
            self._validate_active_membership(
                manifest_ids=tuple(sorted(prior_manifest_coordinates)),
                inventory_ids=tuple(sorted(prior_inventory_ids)),
                label="prior",
            )
        changed_manifest_ids = sorted(
            manifest_id
            for manifest_id, bundle in manifest_coordinates.items()
            if prior_manifest_coordinates.get(manifest_id) != bundle
        )
        retired_or_changed_manifest_ids = sorted(
            manifest_id
            for manifest_id, bundle in prior_manifest_coordinates.items()
            if manifest_coordinates.get(manifest_id) != bundle
        )
        if checkpoint or document_checkpoint:
            changed_manifest_ids = sorted(manifest_coordinates)
        if document_checkpoint:
            retired_or_changed_manifest_ids = sorted(prior_manifest_coordinates)
        manifest_ids_json = _canonical_json(sorted(manifest_coordinates))
        changed_manifest_ids_json = _canonical_json(changed_manifest_ids)
        retired_manifest_ids_json = _canonical_json(retired_or_changed_manifest_ids)
        inventory_ids_json = _canonical_json(sorted(inventory_ids))
        candidate_rows = self._conn.execute(
            """
            WITH changed_keys AS (
              SELECT DISTINCT membership.expected_document_key
              FROM json_each(?) changed_manifest
              JOIN search_corpus_document_memberships membership
                ON membership.manifest_id=
                   CAST(changed_manifest.value AS TEXT)
              JOIN expected_documents expected
                ON expected.expected_document_key=
                   membership.expected_document_key
               AND expected.snapshot_id IN (
                 SELECT CAST(value AS TEXT) FROM json_each(?)
               )
              WHERE membership.membership_status='included'
                AND membership.document_version_id IS NOT NULL
            ),
            candidate AS (
              SELECT
                membership.expected_document_key,
                membership.document_version_id,
                membership.reason,
                document.blob_sha256,
                expected.expected_document_id,
                expected.source_kind,
                expected.document_type,
                expected.period_start,
                expected.period_end,
                expected.snapshot_id,
                membership.manifest_id,
                current.current_commitment_sha256,
                current.document_version_id AS current_document_version_id,
                current.expected_document_id AS current_expected_document_id,
                current.source_kind AS current_source_kind,
                current.document_type AS current_document_type,
                current.period_start AS current_period_start,
                current.period_end AS current_period_end,
                current.source_evidence_json AS current_source_evidence_json,
                COUNT(*) OVER (
                  PARTITION BY membership.manifest_id,
                               membership.expected_document_key
                ) AS expected_match_count
              FROM json_each(?) manifest
              JOIN search_corpus_document_memberships membership
                ON membership.manifest_id=CAST(manifest.value AS TEXT)
              JOIN changed_keys changed
                ON changed.expected_document_key=
                   membership.expected_document_key
              JOIN evidence_document_versions document
                ON document.document_version_id=membership.document_version_id
              JOIN expected_documents expected
                ON expected.expected_document_key=membership.expected_document_key
               AND expected.snapshot_id IN (
                 SELECT CAST(value AS TEXT) FROM json_each(?)
               )
              LEFT JOIN latest_governed_document_entries current
                ON current.scope_key=?
               AND current.expected_document_key=membership.expected_document_key
              WHERE membership.membership_status='included'
                AND membership.document_version_id IS NOT NULL
            ),
            changed_coordinates AS (
              SELECT DISTINCT expected_document_key
              FROM candidate
              WHERE ?=1
                 OR expected_match_count<>1
                 OR current_commitment_sha256 IS NULL
                 OR current_document_version_id IS NOT document_version_id
                 OR current_expected_document_id IS NOT expected_document_id
                 OR current_source_kind IS NOT source_kind
                 OR current_document_type IS NOT document_type
                 OR current_period_start IS NOT period_start
                 OR current_period_end IS NOT period_end
                 OR json_extract(
                      current_source_evidence_json,
                      '$.document_blob_sha256'
                    ) IS NOT blob_sha256
            )
            SELECT candidate.*
            FROM candidate
            JOIN changed_coordinates USING(expected_document_key)
            ORDER BY expected_document_key,manifest_id,snapshot_id
            """,
            (
                changed_manifest_ids_json,
                inventory_ids_json,
                manifest_ids_json,
                inventory_ids_json,
                frontier.scope_id,
                int(checkpoint),
            ),
        ).fetchall()
        deleted_rows = self._conn.execute(
            """
            WITH affected_prior_keys AS (
              SELECT DISTINCT membership.expected_document_key
              FROM json_each(?) prior_manifest
              JOIN search_corpus_document_memberships membership
                ON membership.manifest_id=
                   CAST(prior_manifest.value AS TEXT)
            ),
            candidate_keys AS (
              SELECT DISTINCT membership.expected_document_key
              FROM affected_prior_keys affected
              JOIN search_corpus_document_memberships membership
                ON membership.expected_document_key=
                   affected.expected_document_key
               AND membership.manifest_id IN (
                 SELECT CAST(value AS TEXT) FROM json_each(?)
               )
              JOIN expected_documents expected
                ON expected.expected_document_key=membership.expected_document_key
               AND expected.snapshot_id IN (
                 SELECT CAST(value AS TEXT) FROM json_each(?)
               )
              WHERE membership.membership_status='included'
                AND membership.document_version_id IS NOT NULL
            )
            SELECT current.expected_document_key,
                   current.current_commitment_sha256
            FROM affected_prior_keys affected
            JOIN latest_governed_document_entries current
              ON current.expected_document_key=affected.expected_document_key
             AND current.scope_key=?
            WHERE NOT EXISTS (
                SELECT 1 FROM candidate_keys candidate
                WHERE candidate.expected_document_key=
                      current.expected_document_key
              )
            ORDER BY current.expected_document_key
            """,
            (
                retired_manifest_ids_json,
                manifest_ids_json,
                inventory_ids_json,
                frontier.scope_id,
            ),
        ).fetchall()
        documents: dict[str, dict[str, object]] = {}
        source_reads = len(candidate_rows)
        current_document_reads: set[str] = set()
        for candidate_row in candidate_rows:
            key = str(candidate_row[0])
            if int(candidate_row[19]) != 1:
                raise LatestGovernedStateError(
                    "current document coordinate is missing or ambiguous: " + key
                )
            manifest_id = str(candidate_row[10])
            bundle = manifest_coordinates[manifest_id]
            doc_payload = {
                "expected_document_id": candidate_row[4],
                "document_version_id": candidate_row[1],
                "source_kind": candidate_row[5],
                "document_type": candidate_row[6],
                "period_start": candidate_row[7],
                "period_end": candidate_row[8],
            }
            evidence = {
                "corpus_manifest_id": manifest_id,
                "document_blob_sha256": candidate_row[3],
                "membership_reason": candidate_row[2],
                "source_inventory_snapshot_id": candidate_row[9],
            }
            commitment = _digest(
                {
                    "document_blob_sha256": candidate_row[3],
                    "row": doc_payload,
                }
            )
            existing = documents.get(key)
            candidate: dict[str, object] = {
                "row": doc_payload,
                "evidence": evidence,
                "commitment": commitment,
                "bundle": bundle,
                "manifest_id": manifest_id,
                "prior_commitment": candidate_row[11],
            }
            if existing is not None and existing["commitment"] != commitment:
                raise LatestGovernedStateError(
                    "conflicting current governed documents for coordinate: " + key
                )
            documents[key] = candidate
            if candidate_row[11] is not None and not ignore_current:
                current_document_reads.add(key)

        deleted_documents = {str(row[0]): str(row[1]) for row in deleted_rows}
        if ignore_current:
            deleted_documents = {}
        affected_keys = tuple(sorted({*documents, *deleted_documents}))
        current_narrative_rows: list[tuple[object, ...]] = []
        for offset in range(0, 0 if ignore_current else len(affected_keys), 500):
            key_batch = affected_keys[offset : offset + 500]
            if not key_batch:
                continue
            current_narrative_rows.extend(
                self._conn.execute(
                    "SELECT expected_document_key,chunk_key,"
                    "current_commitment_sha256 "
                    "FROM latest_governed_narrative_entries "
                    "WHERE scope_key=? AND expected_document_key IN "
                    "(SELECT CAST(value AS TEXT) FROM json_each(?))",
                    (frontier.scope_id, _canonical_json(key_batch)),
                ).fetchall()
            )
        current_narrative = {
            (str(row[0]), str(row[1])): str(row[2]) for row in current_narrative_rows
        }
        document_changes: list[dict[str, object]] = []
        narrative_changes: list[dict[str, object]] = []
        for key, prior_sha in sorted(deleted_documents.items()):
            for (document_key, chunk_key), chunk_sha in sorted(current_narrative.items()):
                if document_key == key:
                    narrative_changes.append(
                        _stage_change(
                            entity_kind="narrative",
                            change_kind="delete",
                            coordinate=f"{key}\x00{chunk_key}",
                            prior_sha=chunk_sha,
                            current_sha=None,
                            row={
                                "expected_document_key": key,
                                "chunk_key": chunk_key,
                            },
                            reason="current_document_removed",
                            evidence={"expected_document_key": key},
                        )
                    )
            document_changes.append(
                _stage_change(
                    entity_kind="document",
                    change_kind="delete",
                    coordinate=key,
                    prior_sha=prior_sha,
                    current_sha=None,
                    row={"expected_document_key": key},
                    reason="absent_from_current_governed_manifests",
                    evidence={"research_snapshot_id": frontier.research_snapshot_id},
                )
            )
        for key, candidate in sorted(documents.items()):
            prior_sha = None if ignore_current else _optional_text(candidate["prior_commitment"])
            commitment = str(candidate["commitment"])
            if prior_sha != commitment:
                document_changes.append(
                    _stage_change(
                        entity_kind="document",
                        change_kind="upsert",
                        coordinate=key,
                        prior_sha=prior_sha,
                        current_sha=commitment,
                        row=cast(dict[str, object], candidate["row"]),
                        reason="selected_current_governed_document",
                        evidence=cast(dict[str, object], candidate["evidence"]),
                    )
                )
            chunks = self._document_chunks(
                key=key,
                document_version_id=str(
                    cast(dict[str, object], candidate["row"])["document_version_id"]
                ),
                manifest_id=str(candidate["manifest_id"]),
                bundle=cast(dict[str, object], candidate["bundle"]),
            )
            source_reads += len(chunks)
            next_keys = {(key, str(chunk["chunk_key"])) for chunk in chunks}
            for (document_key, chunk_key), chunk_sha in sorted(current_narrative.items()):
                if document_key == key and (document_key, chunk_key) not in next_keys:
                    narrative_changes.append(
                        _stage_change(
                            entity_kind="narrative",
                            change_kind="delete",
                            coordinate=f"{key}\x00{chunk_key}",
                            prior_sha=chunk_sha,
                            current_sha=None,
                            row={"expected_document_key": key, "chunk_key": chunk_key},
                            reason="absent_from_current_governed_document",
                            evidence={
                                "document_version_id": cast(dict[str, object], candidate["row"])[
                                    "document_version_id"
                                ]
                            },
                        )
                    )
            for chunk in chunks:
                chunk_key = str(chunk["chunk_key"])
                chunk_sha = str(chunk.pop("current_commitment_sha256"))
                prior_chunk_sha = current_narrative.get((key, chunk_key))
                if prior_chunk_sha == chunk_sha:
                    continue
                narrative_changes.append(
                    _stage_change(
                        entity_kind="narrative",
                        change_kind="upsert",
                        coordinate=f"{key}\x00{chunk_key}",
                        prior_sha=prior_chunk_sha,
                        current_sha=chunk_sha,
                        row=chunk,
                        reason="selected_chunk_from_current_governed_document",
                        evidence={
                            "corpus_manifest_id": candidate["manifest_id"],
                            "document_version_id": chunk["document_version_id"],
                            "embedding_artifact_id": chunk["embedding_artifact_id"],
                            "evidence_node_id": chunk["evidence_node_id"],
                            "research_snapshot_id": frontier.research_snapshot_id,
                            "source_chunk_id": chunk["source_chunk_id"],
                            "vector_index_run_id": cast(dict[str, object], candidate["bundle"]).get(
                                "vector_index_run_id"
                            ),
                        },
                    )
                )
        return (
            document_changes,
            narrative_changes,
            source_reads,
            len(current_document_reads) + len(deleted_documents) + len(current_narrative),
        )

    def _validate_active_membership(
        self,
        *,
        manifest_ids: tuple[str, ...],
        inventory_ids: tuple[str, ...],
        label: str,
    ) -> None:
        if not manifest_ids:
            return
        rows = self._conn.execute(
            "SELECT membership.expected_document_key,membership.manifest_id,"
            "membership.document_version_id,expected.expected_document_id,"
            "expected.source_kind,expected.document_type,expected.period_start,"
            "expected.period_end,COUNT(expected.expected_document_id) OVER ("
            "PARTITION BY membership.manifest_id,"
            "membership.expected_document_key) AS expected_match_count "
            "FROM search_corpus_document_memberships membership "
            "LEFT JOIN expected_documents expected "
            "ON expected.expected_document_key=membership.expected_document_key "
            "AND expected.snapshot_id IN ("
            "SELECT CAST(value AS TEXT) FROM json_each(?)) "
            "WHERE membership.manifest_id IN ("
            "SELECT CAST(value AS TEXT) FROM json_each(?)) "
            "AND membership.membership_status='included' "
            "AND membership.document_version_id IS NOT NULL "
            "ORDER BY membership.expected_document_key,membership.manifest_id",
            (
                _canonical_json(inventory_ids),
                _canonical_json(manifest_ids),
            ),
        ).fetchall()
        coordinates: dict[str, tuple[object, ...]] = {}
        for row in rows:
            key = str(row[0])
            if int(row[8]) != 1:
                raise LatestGovernedStateError(
                    f"{label} active document coordinate is missing or ambiguous: {key}"
                )
            identity = tuple(row[2:8])
            existing = coordinates.get(key)
            if existing is not None and existing != identity:
                raise LatestGovernedStateError(
                    f"{label} active document coordinate conflicts: {key}"
                )
            coordinates[key] = identity

    def _document_chunks(
        self,
        *,
        key: str,
        document_version_id: str,
        manifest_id: str,
        bundle: dict[str, object],
    ) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT chunk.chunk_key,chunk.chunk_id,chunk.evidence_node_id,"
            "chunk.text,chunk.content_sha256,chunk.chunker_config_sha256,"
            "embedding.embedding_artifact_id "
            "FROM search_chunks chunk "
            "JOIN evidence_nodes node ON node.node_id=chunk.evidence_node_id "
            "JOIN evidence_extraction_runs run "
            "ON run.extraction_run_id=node.extraction_run_id "
            "LEFT JOIN search_embedding_artifacts embedding "
            "ON embedding.index_run_id=? AND embedding.chunk_id=chunk.chunk_id "
            "AND embedding.outcome='succeeded' "
            "WHERE chunk.manifest_id=? AND run.document_version_id=? "
            "ORDER BY chunk.chunk_key,chunk.chunk_id",
            (bundle.get("vector_index_run_id"), manifest_id, document_version_id),
        ).fetchall()
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            chunk_key = str(row[0])
            if chunk_key in seen:
                raise LatestGovernedStateError(
                    f"current narrative chunk coordinate is ambiguous: {key}/{chunk_key}"
                )
            seen.add(chunk_key)
            payload: dict[str, object] = {
                "expected_document_key": key,
                "chunk_key": chunk_key,
                "document_version_id": document_version_id,
                "evidence_node_id": row[2],
                "source_chunk_id": row[1],
                "embedding_artifact_id": row[6],
                "text": row[3],
                "content_sha256": row[4],
                "chunker_config_sha256": row[5],
            }
            payload["current_commitment_sha256"] = _digest(
                {
                    "content_sha256": row[4],
                    "chunker_config_sha256": row[5],
                    "document_version_id": document_version_id,
                    "embedding_artifact_id": row[6],
                    "evidence_node_id": row[2],
                }
            )
            result.append(payload)
        return result

    def _ensure_run(
        self,
        *,
        refresh_id: str,
        idempotency: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        policy_sha: str,
        prior: CurrentHead | None,
    ) -> None:
        cursor_json = _canonical_json({"next_stage_ordinal": 0})
        expected = (
            refresh_id,
            idempotency,
            request.scope_id,
            frontier.population_run_id,
            frontier.population_receipt_set_sha256,
            frontier.promotion_id,
            frontier.fact_generation_id,
            frontier.commitment_sha256,
            policy_sha,
            _db_time(frontier.knowledge_cutoff),
            _db_time(frontier.observed_through),
        )
        existing = self._conn.execute(
            "SELECT refresh_run_id,idempotency_key,scope_key,"
            "baseline_population_run_id,baseline_population_receipt_sha256,"
            "baseline_promotion_id,baseline_fact_generation_id,input_head_sha256,"
            "policy_config_sha256,knowledge_cutoff,observed_through "
            "FROM latest_governed_refresh_runs "
            "WHERE refresh_run_id=? OR idempotency_key=?",
            (refresh_id, idempotency),
        ).fetchone()
        if existing is not None:
            actual = tuple(existing)
            normalized = (*actual[:9], _db_time(actual[9]), _db_time(actual[10]))
            if normalized != expected:
                raise LatestGovernedStateError("refresh run idempotency conflict")
            return
        self._conn.execute(
            "INSERT INTO latest_governed_refresh_runs ("
            "refresh_run_id,idempotency_key,scope_key,status,"
            "baseline_population_run_id,baseline_population_receipt_sha256,"
            "baseline_promotion_id,baseline_fact_generation_id,input_head_sha256,"
            "policy_config_sha256,knowledge_cutoff,observed_through,"
            "resume_cursor_json,resume_cursor_sha256,staged_change_count,"
            "applied_change_count,planned_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            (
                refresh_id,
                idempotency,
                request.scope_id,
                "planned",
                frontier.population_run_id,
                frontier.population_receipt_set_sha256,
                frontier.promotion_id,
                frontier.fact_generation_id,
                frontier.commitment_sha256,
                policy_sha,
                _db_time(frontier.knowledge_cutoff),
                _db_time(frontier.observed_through),
                cursor_json,
                _digest(cursor_json),
                0,
                0,
                _db_time(request.operation_recorded_at),
                _db_time(request.operation_recorded_at),
            ),
        )
        self._conn.commit()
        loaded = self._conn.execute(
            "SELECT refresh_run_id,idempotency_key,scope_key,"
            "baseline_population_run_id,baseline_population_receipt_sha256,"
            "baseline_promotion_id,baseline_fact_generation_id,input_head_sha256,"
            "policy_config_sha256,knowledge_cutoff,observed_through "
            "FROM latest_governed_refresh_runs "
            "WHERE refresh_run_id=? OR idempotency_key=?",
            (refresh_id, idempotency),
        ).fetchall()
        if len(loaded) != 1:
            raise LatestGovernedStateError(
                "refresh run insert-or-load did not resolve one identity"
            )
        actual = tuple(loaded[0])
        normalized = (*actual[:9], _db_time(actual[9]), _db_time(actual[10]))
        if normalized != expected:
            raise LatestGovernedStateError("refresh run idempotency conflict")

    def _stage_changes(
        self,
        *,
        refresh_id: str,
        changes: list[dict[str, object]],
        request: LatestGovernedRefreshRequest,
    ) -> tuple[int, int, int, int]:
        existing = {
            (str(row[1]), str(row[3])): tuple(row)
            for row in self._conn.execute(
                "SELECT stage_ordinal,entity_kind,change_kind,coordinate_key,"
                "digest_bucket,prior_commitment_sha256,current_commitment_sha256,"
                "canonical_payload_json,payload_sha256,stage_status "
                "FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                (refresh_id,),
            )
        }
        created = 0
        replayed = 0
        batches = 0
        cursor = 0
        for offset in range(0, len(changes), request.max_batch_rows):
            batch = changes[offset : offset + request.max_batch_rows]
            for item in batch:
                payload_json = _canonical_json(item["payload"])
                payload_sha = _digest(payload_json)
                identity = (str(item["entity_kind"]), str(item["coordinate_key"]))
                expected = (
                    int(str(item["stage_ordinal"])),
                    str(item["entity_kind"]),
                    str(item["change_kind"]),
                    str(item["coordinate_key"]),
                    int(str(item["digest_bucket"])),
                    item["prior_commitment_sha256"],
                    item["current_commitment_sha256"],
                    payload_json,
                    payload_sha,
                    "staged",
                )
                prior = existing.get(identity)
                if prior is not None:
                    if prior != expected:
                        raise LatestGovernedStateError(
                            "staged refresh coordinate idempotency conflict"
                        )
                    replayed += 1
                    cursor = max(cursor, int(str(item["stage_ordinal"])) + 1)
                    continue
                inserted = self._conn.execute(
                    "INSERT INTO latest_governed_refresh_stage ("
                    "refresh_run_id,stage_ordinal,entity_kind,change_kind,"
                    "coordinate_key,digest_bucket,prior_commitment_sha256,"
                    "current_commitment_sha256,canonical_payload_json,payload_sha256,"
                    "stage_status,staged_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        refresh_id,
                        item["stage_ordinal"],
                        item["entity_kind"],
                        item["change_kind"],
                        item["coordinate_key"],
                        item["digest_bucket"],
                        item["prior_commitment_sha256"],
                        item["current_commitment_sha256"],
                        payload_json,
                        payload_sha,
                        "staged",
                        _db_time(request.operation_recorded_at),
                    ),
                )
                loaded = self._conn.execute(
                    "SELECT stage_ordinal,entity_kind,change_kind,coordinate_key,"
                    "digest_bucket,prior_commitment_sha256,current_commitment_sha256,"
                    "canonical_payload_json,payload_sha256,stage_status "
                    "FROM latest_governed_refresh_stage "
                    "WHERE refresh_run_id=? AND entity_kind=? AND coordinate_key=?",
                    (refresh_id, *identity),
                ).fetchall()
                if len(loaded) != 1 or tuple(loaded[0]) != expected:
                    raise LatestGovernedStateError("staged refresh coordinate idempotency conflict")
                existing[identity] = expected
                if inserted.rowcount == 1:
                    created += 1
                else:
                    replayed += 1
            cursor = min(len(changes), offset + len(batch))
            cursor_json = _canonical_json({"next_stage_ordinal": cursor})
            self._conn.execute(
                "UPDATE latest_governed_refresh_runs SET status=?,"
                "resume_cursor_json=?,resume_cursor_sha256=?,"
                "staged_change_count=(SELECT COUNT(*) "
                "FROM latest_governed_refresh_stage WHERE refresh_run_id=?),"
                "updated_at=? WHERE refresh_run_id=?",
                (
                    "ready" if cursor == len(changes) else "staging",
                    cursor_json,
                    _digest(cursor_json),
                    refresh_id,
                    _db_time(request.operation_recorded_at),
                    refresh_id,
                ),
            )
            self._conn.commit()
            batches += 1
            if (
                request.interrupt_after_batches is not None
                and batches >= request.interrupt_after_batches
                and cursor < len(changes)
            ):
                break
        if not changes:
            cursor_json = _canonical_json({"next_stage_ordinal": 0})
            self._conn.execute(
                "UPDATE latest_governed_refresh_runs SET status='ready',"
                "resume_cursor_json=?,resume_cursor_sha256=?,updated_at=? "
                "WHERE refresh_run_id=?",
                (
                    cursor_json,
                    _digest(cursor_json),
                    _db_time(request.operation_recorded_at),
                    refresh_id,
                ),
            )
            self._conn.commit()
        return created, replayed, batches, cursor

    def _write_noop_receipt(
        self,
        *,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        prior: CurrentHead,
        policy_sha: str,
        refresh_id: str,
        idempotency: str,
        finalization_hook: LatestGovernedFinalizationHook | None = None,
    ) -> LatestGovernedRefreshResult:
        self._ensure_run(
            refresh_id=refresh_id,
            idempotency=idempotency,
            request=request,
            frontier=frontier,
            policy_sha=policy_sha,
            prior=prior,
        )
        receipt_id = f"latest-receipt:{idempotency[:40]}"
        payload = _receipt_payload(
            receipt_id=receipt_id,
            refresh_id=refresh_id,
            request=request,
            frontier=frontier,
            prior=prior,
            current_state_sha=prior.state_commitment_sha256,
            roots={
                "fact": prior.fact_root_sha256,
                "document": prior.document_root_sha256,
                "narrative": prior.narrative_root_sha256,
            },
            change_set=[],
            change_audit_mode="coordinate_changes.v1",
            counts={"fact": 0, "document": 0, "narrative": 0},
            policy_sha=policy_sha,
        )
        receipt_sha = _digest(payload)
        final_receipt = RefreshReceipt(
            receipt_id=receipt_id,
            refresh_id=refresh_id,
            scope_id=request.scope_id,
            outcome="no_op",
            prior_receipt_id=prior.receipt_id,
            current_state_sha256=prior.state_commitment_sha256,
            terminal_commitment=receipt_sha,
            fact_change_count=0,
            document_change_count=0,
            narrative_change_count=0,
            knowledge_cutoff=frontier.knowledge_cutoff,
            observed_through=frontier.observed_through,
            operation_recorded_at=request.operation_recorded_at,
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._frontier(request.scope_id) != frontier:
                raise LatestGovernedStateError(
                    "latest governed frontier changed before no-op finalization"
                )
            locked = self._head(request.scope_id)
            if locked != prior:
                raise LatestGovernedStateError("latest governed head changed before no-op receipt")
            if (
                request.expected_terminal_commitment is not None
                and request.expected_terminal_commitment != prior.state_commitment_sha256
            ):
                raise LatestGovernedStateError(
                    "latest governed no-op terminal differs from admission"
                )
            replayed_receipt_sha = self._locked_noop_receipt_sha(
                refresh_id=refresh_id,
                receipt_id=receipt_id,
                scope_id=request.scope_id,
                prior=prior,
            )
            if replayed_receipt_sha is not None:
                replayed_receipt = final_receipt.model_copy(
                    update={"terminal_commitment": replayed_receipt_sha}
                )
                if finalization_hook is not None:
                    finalization_hook(
                        self._conn,
                        request,
                        frontier,
                        replayed_receipt,
                        prior,
                    )
                self._conn.commit()
                return self._noop_result(
                    refresh_id=refresh_id,
                    head_receipt_id=prior.receipt_id,
                    receipt_sha=replayed_receipt_sha,
                )
            self._insert_receipt(
                receipt_id=receipt_id,
                receipt_idempotency=f"latest-receipt-key:{idempotency}",
                refresh_id=refresh_id,
                request=request,
                frontier=frontier,
                prior=prior,
                state_sha=prior.state_commitment_sha256,
                roots={
                    "fact": prior.fact_root_sha256,
                    "document": prior.document_root_sha256,
                    "narrative": prior.narrative_root_sha256,
                },
                change_set=[],
                counts={"fact": 0, "document": 0, "narrative": 0},
                payload=payload,
                receipt_sha=receipt_sha,
            )
            self._conn.execute(
                "UPDATE latest_governed_refresh_runs SET status='finalized',"
                "applied_change_count=0,updated_at=? WHERE refresh_run_id=?",
                (_db_time(request.operation_recorded_at), refresh_id),
            )
            self._conn.execute(
                "DELETE FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                (refresh_id,),
            )
            if finalization_hook is not None:
                finalization_hook(self._conn, request, frontier, final_receipt, prior)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self._noop_result(
            refresh_id=refresh_id,
            head_receipt_id=prior.receipt_id,
            receipt_sha=receipt_sha,
        )

    def _noop_result(
        self,
        *,
        refresh_id: str,
        head_receipt_id: str,
        receipt_sha: str,
    ) -> LatestGovernedRefreshResult:
        return LatestGovernedRefreshResult(
            mode="apply",
            outcome="no_op",
            refresh_id=refresh_id,
            head_id=head_receipt_id,
            created_count=1,
            replayed_count=0,
            source_event_count=0,
            fact_change_count=0,
            document_change_count=0,
            narrative_change_count=0,
            source_read_count=0,
            current_read_count=1,
            current_write_count=0,
            receipt_write_count=1,
            terminal_commitment=receipt_sha,
        )

    def _locked_noop_receipt_sha(
        self,
        *,
        refresh_id: str,
        receipt_id: str,
        scope_id: str,
        prior: CurrentHead,
    ) -> str | None:
        row = self._conn.execute(
            "SELECT receipt_id,scope_key,prior_receipt_id,prior_state_sha256,"
            "current_state_sha256,fact_root_sha256,document_root_sha256,"
            "narrative_root_sha256,change_count,fact_change_count,"
            "document_change_count,narrative_change_count,canonical_change_set_json,"
            "change_set_sha256,canonical_receipt_json,receipt_sha256 "
            "FROM latest_governed_refresh_receipts WHERE refresh_run_id=?",
            (refresh_id,),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row[14])
        payload = _json_object(payload_json)
        empty_change_set = _canonical_json([])
        expected = (
            receipt_id,
            scope_id,
            prior.receipt_id,
            prior.state_commitment_sha256,
            prior.state_commitment_sha256,
            prior.fact_root_sha256,
            prior.document_root_sha256,
            prior.narrative_root_sha256,
            0,
            0,
            0,
            0,
            empty_change_set,
            _digest(empty_change_set),
        )
        if (
            tuple(row[:14]) != expected
            or payload.get("receipt_id") != receipt_id
            or payload.get("refresh_run_id") != refresh_id
            or payload.get("scope_id") != scope_id
            or payload.get("prior_receipt_id") != prior.receipt_id
            or payload.get("current_state_sha256") != prior.state_commitment_sha256
            or _digest(payload_json) != str(row[15])
        ):
            raise LatestGovernedStateError("no-op receipt insert-or-load identity conflict")
        return str(row[15])

    def _finalize(
        self,
        *,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        prior: CurrentHead | None,
        policy_sha: str,
        refresh_id: str,
        idempotency: str,
        planned_changes: list[dict[str, object]],
        projection_target: CurrentHead | None = None,
        rollback_target_receipt_id: str | None = None,
        finalization_hook: LatestGovernedFinalizationHook | None = None,
    ) -> tuple[RefreshReceipt, int, int]:
        receipt_id = f"latest-receipt:{idempotency[:40]}"
        current_writes = 0
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if projection_target is None and self._frontier(request.scope_id) != frontier:
                raise LatestGovernedStateError(
                    "latest governed frontier changed before finalization"
                )
            replay = self._receipt_for_refresh(
                refresh_id=refresh_id,
                receipt_id=receipt_id,
                scope_id=request.scope_id,
            )
            if replay is not None:
                self._conn.execute(
                    "DELETE FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                    (refresh_id,),
                )
                replay_head = self._head(request.scope_id)
                if replay_head is None:
                    raise LatestGovernedStateError("replayed refresh has no current head")
                if finalization_hook is not None:
                    finalization_hook(
                        self._conn,
                        request,
                        frontier,
                        replay,
                        replay_head,
                    )
                self._conn.commit()
                return replay, 0, 1
            if self._head(request.scope_id) != prior:
                raise LatestGovernedStateError(
                    "latest governed head changed before CAS publication"
                )
            self._validate_locked_run(
                refresh_id=refresh_id,
                idempotency=idempotency,
                request=request,
                frontier=frontier,
                policy_sha=policy_sha,
            )
            staged = self._validated_locked_stage(
                refresh_id=refresh_id,
                planned_changes=planned_changes,
            )
            counts = {
                kind: sum(str(row["entity_kind"]) == kind for row in staged)
                for kind in ("fact", "document", "narrative")
            }
            transitions = _transition_commitment(
                prior=prior,
                frontier=frontier,
                policy_sha=policy_sha,
                changes=planned_changes,
            )
            if projection_target is not None:
                transitions = {
                    "state": projection_target.state_commitment_sha256,
                    "fact": projection_target.fact_root_sha256,
                    "document": projection_target.document_root_sha256,
                    "narrative": projection_target.narrative_root_sha256,
                }
            if (
                request.expected_terminal_commitment is not None
                and request.expected_terminal_commitment != transitions["state"]
            ):
                raise LatestGovernedStateError(
                    "latest governed changed terminal differs from admission"
                )
            change_set, change_audit_mode = _change_audit(
                prior=prior,
                changes=planned_changes,
            )
            payload = _receipt_payload(
                receipt_id=receipt_id,
                refresh_id=refresh_id,
                request=request,
                frontier=frontier,
                prior=prior,
                current_state_sha=transitions["state"],
                roots=transitions,
                change_set=change_set,
                change_audit_mode=change_audit_mode,
                counts=counts,
                policy_sha=policy_sha,
                rollback_target_receipt_id=rollback_target_receipt_id,
            )
            receipt_sha = _digest(payload)
            self._insert_receipt(
                receipt_id=receipt_id,
                receipt_idempotency=f"latest-receipt-key:{idempotency}",
                refresh_id=refresh_id,
                request=request,
                frontier=frontier,
                prior=prior,
                state_sha=transitions["state"],
                roots=transitions,
                change_set=change_set,
                counts=counts,
                payload=payload,
                receipt_sha=receipt_sha,
            )
            if prior is not None:
                self._insert_change_receipts(
                    receipt_id=receipt_id,
                    request=request,
                    frontier=frontier,
                    staged=staged,
                )
            current_writes = self._apply_staged_current_rows(
                receipt_id=receipt_id,
                request=request,
                frontier=frontier,
                staged=staged,
            )
            self._before_head_advance()
            source_heads_json = _canonical_json(frontier.model_dump(mode="json"))
            head_values = (
                receipt_id,
                frontier.population_run_id,
                frontier.promotion_id,
                frontier.fact_generation_id,
                source_heads_json,
                frontier.commitment_sha256,
                transitions["state"],
                transitions["fact"],
                transitions["document"],
                transitions["narrative"],
                self._count("latest_governed_fact_entries", request.scope_id),
                self._count("latest_governed_document_entries", request.scope_id),
                self._count("latest_governed_narrative_entries", request.scope_id),
                _db_time(frontier.knowledge_cutoff),
                _db_time(frontier.observed_through),
                _db_time(request.operation_recorded_at),
            )
            self._conn.execute(
                "INSERT INTO latest_governed_scope_heads ("
                "scope_key,refresh_receipt_id,population_run_id,promotion_id,"
                "fact_generation_id,source_heads_json,source_heads_sha256,state_sha256,"
                "fact_root_sha256,document_root_sha256,narrative_root_sha256,"
                "fact_count,document_count,narrative_count,knowledge_cutoff,"
                "observed_through,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_key) DO UPDATE SET "
                "refresh_receipt_id=excluded.refresh_receipt_id,"
                "population_run_id=excluded.population_run_id,"
                "promotion_id=excluded.promotion_id,"
                "fact_generation_id=excluded.fact_generation_id,"
                "source_heads_json=excluded.source_heads_json,"
                "source_heads_sha256=excluded.source_heads_sha256,"
                "state_sha256=excluded.state_sha256,"
                "fact_root_sha256=excluded.fact_root_sha256,"
                "document_root_sha256=excluded.document_root_sha256,"
                "narrative_root_sha256=excluded.narrative_root_sha256,"
                "fact_count=excluded.fact_count,"
                "document_count=excluded.document_count,"
                "narrative_count=excluded.narrative_count,"
                "knowledge_cutoff=excluded.knowledge_cutoff,"
                "observed_through=excluded.observed_through,"
                "updated_at=excluded.updated_at",
                (request.scope_id, *head_values),
            )
            self._conn.execute(
                "UPDATE latest_governed_refresh_runs SET status='finalized',"
                "applied_change_count=staged_change_count,updated_at=? "
                "WHERE refresh_run_id=?",
                (_db_time(request.operation_recorded_at), refresh_id),
            )
            self._conn.execute(
                "DELETE FROM latest_governed_refresh_stage WHERE refresh_run_id=?",
                (refresh_id,),
            )
            final_receipt = RefreshReceipt(
                receipt_id=receipt_id,
                refresh_id=refresh_id,
                scope_id=request.scope_id,
                outcome="changed",
                prior_receipt_id=None if prior is None else prior.receipt_id,
                current_state_sha256=transitions["state"],
                terminal_commitment=receipt_sha,
                fact_change_count=counts["fact"],
                document_change_count=counts["document"],
                narrative_change_count=counts["narrative"],
                knowledge_cutoff=frontier.knowledge_cutoff,
                observed_through=frontier.observed_through,
                operation_recorded_at=request.operation_recorded_at,
            )
            if finalization_hook is not None:
                final_head = self._head(request.scope_id)
                if final_head is None:
                    raise LatestGovernedStateError("finalized refresh has no current head")
                finalization_hook(
                    self._conn,
                    request,
                    frontier,
                    final_receipt,
                    final_head,
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return (final_receipt, current_writes, 0)

    def _validate_locked_run(
        self,
        *,
        refresh_id: str,
        idempotency: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        policy_sha: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT refresh_run_id,idempotency_key,scope_key,"
            "baseline_population_run_id,baseline_population_receipt_sha256,"
            "baseline_promotion_id,baseline_fact_generation_id,input_head_sha256,"
            "policy_config_sha256,knowledge_cutoff,observed_through,status,"
            "resume_cursor_json,resume_cursor_sha256,staged_change_count "
            "FROM latest_governed_refresh_runs WHERE refresh_run_id=?",
            (refresh_id,),
        ).fetchone()
        if row is None:
            raise LatestGovernedStateError("governed refresh run disappeared before finalization")
        expected_identity = (
            refresh_id,
            idempotency,
            request.scope_id,
            frontier.population_run_id,
            frontier.population_receipt_set_sha256,
            frontier.promotion_id,
            frontier.fact_generation_id,
            frontier.commitment_sha256,
            policy_sha,
            _db_time(frontier.knowledge_cutoff),
            _db_time(frontier.observed_through),
        )
        actual_identity = (*tuple(row[:9]), _db_time(row[9]), _db_time(row[10]))
        if actual_identity != expected_identity:
            raise LatestGovernedStateError("refresh run idempotency conflict")
        cursor_json = str(row[12])
        if str(row[11]) != "ready" or _digest(cursor_json) != str(row[13]):
            raise LatestGovernedStateError("refresh run is not completely and canonically staged")
        cursor = _json_object(cursor_json).get("next_stage_ordinal")
        if cursor != int(row[14]):
            raise LatestGovernedStateError(
                "refresh run cursor does not bind the staged change count"
            )

    def _validated_locked_stage(
        self,
        *,
        refresh_id: str,
        planned_changes: list[dict[str, object]],
    ) -> list[sqlite3.Row]:
        original = self._conn.row_factory
        self._conn.row_factory = sqlite3.Row
        try:
            staged = self._conn.execute(
                "SELECT * FROM latest_governed_refresh_stage "
                "WHERE refresh_run_id=? ORDER BY stage_ordinal",
                (refresh_id,),
            ).fetchall()
        finally:
            self._conn.row_factory = original
        if len(staged) != len(planned_changes):
            raise LatestGovernedStateError("staged refresh does not match the deterministic plan")
        for row, item in zip(staged, planned_changes, strict=True):
            payload_json = _canonical_json(item["payload"])
            expected = (
                int(str(item["stage_ordinal"])),
                str(item["entity_kind"]),
                str(item["change_kind"]),
                str(item["coordinate_key"]),
                int(str(item["digest_bucket"])),
                item["prior_commitment_sha256"],
                item["current_commitment_sha256"],
                payload_json,
                _digest(payload_json),
                "staged",
            )
            actual = (
                int(row["stage_ordinal"]),
                str(row["entity_kind"]),
                str(row["change_kind"]),
                str(row["coordinate_key"]),
                int(row["digest_bucket"]),
                row["prior_commitment_sha256"],
                row["current_commitment_sha256"],
                str(row["canonical_payload_json"]),
                str(row["payload_sha256"]),
                str(row["stage_status"]),
            )
            if _digest(str(row["canonical_payload_json"])) != str(row["payload_sha256"]):
                raise LatestGovernedStateError(
                    "staged refresh canonical payload commitment is invalid"
                )
            if actual != expected:
                raise LatestGovernedStateError(
                    "staged refresh does not match the deterministic plan"
                )
        return list(staged)

    def _receipt_for_refresh(
        self,
        *,
        refresh_id: str,
        receipt_id: str,
        scope_id: str,
    ) -> RefreshReceipt | None:
        row = self._conn.execute(
            "SELECT receipt_id,refresh_run_id,scope_key,prior_receipt_id,"
            "current_state_sha256,receipt_sha256,fact_change_count,"
            "document_change_count,narrative_change_count,knowledge_cutoff,"
            "observed_through,sealed_at,canonical_receipt_json "
            "FROM latest_governed_refresh_receipts WHERE refresh_run_id=?",
            (refresh_id,),
        ).fetchone()
        if row is None:
            return None
        payload_json = str(row[12])
        payload = _json_object(payload_json)
        if (
            str(row[0]) != receipt_id
            or str(row[1]) != refresh_id
            or str(row[2]) != scope_id
            or payload.get("receipt_id") != receipt_id
            or payload.get("refresh_run_id") != refresh_id
            or payload.get("scope_id") != scope_id
            or payload.get("current_state_sha256") != str(row[4])
            or _digest(payload_json) != str(row[5])
        ):
            raise LatestGovernedStateError(
                "finalized refresh receipt identity or commitment is invalid"
            )
        head = self._conn.execute(
            "SELECT refresh_receipt_id,state_sha256 FROM latest_governed_scope_heads "
            "WHERE scope_key=?",
            (scope_id,),
        ).fetchone()
        change_count = sum(int(row[index]) for index in (6, 7, 8))
        expected_head_receipt_id = receipt_id if change_count else _optional_text(row[3])
        if (
            head is None
            or expected_head_receipt_id is None
            or (str(head[0]), str(head[1])) != (expected_head_receipt_id, str(row[4]))
        ):
            raise LatestGovernedStateError(
                "finalized refresh receipt is not the current governed head"
            )
        return RefreshReceipt(
            receipt_id=receipt_id,
            refresh_id=refresh_id,
            scope_id=scope_id,
            outcome="changed" if change_count else "no_op",
            prior_receipt_id=_optional_text(row[3]),
            current_state_sha256=str(row[4]),
            terminal_commitment=str(row[5]),
            fact_change_count=int(row[6]),
            document_change_count=int(row[7]),
            narrative_change_count=int(row[8]),
            knowledge_cutoff=_datetime(row[9]),
            observed_through=_datetime(row[10]),
            operation_recorded_at=_datetime(row[11]),
        )

    def _before_head_advance(self) -> None:
        """Test seam immediately before the atomic current-head CAS."""

    def _count(self, table: str, scope_id: str) -> int:
        if table not in {
            "latest_governed_fact_entries",
            "latest_governed_document_entries",
            "latest_governed_narrative_entries",
        }:
            raise ValueError("current-state count table is not allowed")
        return int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE scope_key=?",  # nosec B608 -- closed set above
                (scope_id,),
            ).fetchone()[0]
        )

    def _insert_receipt(
        self,
        *,
        receipt_id: str,
        receipt_idempotency: str,
        refresh_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        prior: CurrentHead | None,
        state_sha: str,
        roots: dict[str, str],
        change_set: list[object],
        counts: dict[str, int],
        payload: dict[str, object],
        receipt_sha: str,
    ) -> None:
        change_json = _canonical_json(change_set)
        self._conn.execute(
            "INSERT INTO latest_governed_refresh_receipts ("
            "receipt_id,idempotency_key,refresh_run_id,scope_key,prior_receipt_id,"
            "baseline_population_run_id,baseline_population_receipt_sha256,"
            "baseline_promotion_id,fact_generation_id,input_head_sha256,"
            "prior_state_sha256,current_state_sha256,fact_root_sha256,"
            "document_root_sha256,narrative_root_sha256,change_count,"
            "fact_change_count,document_change_count,narrative_change_count,"
            "canonical_change_set_json,change_set_sha256,canonical_receipt_json,"
            "receipt_sha256,knowledge_cutoff,observed_through,sealed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                receipt_idempotency,
                refresh_id,
                request.scope_id,
                None if prior is None else prior.receipt_id,
                frontier.population_run_id,
                frontier.population_receipt_set_sha256,
                frontier.promotion_id,
                frontier.fact_generation_id,
                frontier.commitment_sha256,
                None if prior is None else prior.state_commitment_sha256,
                state_sha,
                roots["fact"],
                roots["document"],
                roots["narrative"],
                sum(counts.values()),
                counts["fact"],
                counts["document"],
                counts["narrative"],
                change_json,
                _digest(change_json),
                _canonical_json(payload),
                receipt_sha,
                _db_time(frontier.knowledge_cutoff),
                _db_time(frontier.observed_through),
                _db_time(request.operation_recorded_at),
            ),
        )

    def _insert_change_receipts(
        self,
        *,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        staged: list[sqlite3.Row],
    ) -> None:
        rows: list[tuple[object, ...]] = []
        for ordinal, stage in enumerate(staged):
            payload = _json_object(stage["canonical_payload_json"])
            reason = str(payload["selection_reason"])
            evidence = cast(dict[str, object], payload["source_evidence"])
            evidence_json = _canonical_json(evidence)
            change = {
                "change_kind": stage["change_kind"],
                "coordinate_key": stage["coordinate_key"],
                "current_commitment_sha256": stage["current_commitment_sha256"],
                "entity_kind": stage["entity_kind"],
                "prior_commitment_sha256": stage["prior_commitment_sha256"],
                "selection_reason": reason,
                "source_evidence_sha256": _digest(evidence_json),
            }
            change_json = _canonical_json(change)
            identity = _digest({"receipt_id": receipt_id, "ordinal": ordinal, "change": change})
            rows.append(
                (
                    f"latest-change:{identity[:40]}",
                    f"latest-change-key:{identity}",
                    receipt_id,
                    ordinal,
                    stage["entity_kind"],
                    stage["change_kind"],
                    stage["coordinate_key"],
                    stage["digest_bucket"],
                    stage["prior_commitment_sha256"],
                    stage["current_commitment_sha256"],
                    reason,
                    evidence_json,
                    _digest(evidence_json),
                    change_json,
                    _digest(change_json),
                    _db_time(frontier.knowledge_cutoff),
                    _db_time(frontier.observed_through),
                    _db_time(request.operation_recorded_at),
                )
            )
        if rows:
            self._conn.executemany(
                "INSERT INTO latest_governed_refresh_changes ("
                "change_id,idempotency_key,receipt_id,change_ordinal,entity_kind,"
                "change_kind,coordinate_key,digest_bucket,prior_commitment_sha256,"
                "current_commitment_sha256,selection_reason,source_evidence_json,"
                "source_evidence_sha256,canonical_change_json,change_sha256,"
                "knowledge_cutoff,observed_through,recorded_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def _apply_staged_current_rows(
        self,
        *,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        staged: list[sqlite3.Row],
    ) -> int:
        writes = 0
        decoded = [(row, _json_object(row["canonical_payload_json"])) for row in staged]
        for row, payload in decoded:
            if row["entity_kind"] == "fact":
                writes += self._apply_fact(receipt_id, request, frontier, row, payload)
        writes += self._apply_narrative_batch(
            receipt_id=receipt_id,
            request=request,
            frontier=frontier,
            staged=staged,
            change_kind="delete",
        )
        for row, payload in decoded:
            if row["entity_kind"] == "document" and row["change_kind"] == "delete":
                writes += self._apply_document(receipt_id, request, frontier, row, payload)
        for row, payload in decoded:
            if row["entity_kind"] == "document" and row["change_kind"] == "upsert":
                writes += self._apply_document(receipt_id, request, frontier, row, payload)
        writes += self._apply_narrative_batch(
            receipt_id=receipt_id,
            request=request,
            frontier=frontier,
            staged=staged,
            change_kind="upsert",
        )
        return writes

    def _apply_narrative_batch(
        self,
        *,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        staged: list[sqlite3.Row],
        change_kind: Literal["upsert", "delete"],
    ) -> int:
        expected = sum(
            row["entity_kind"] == "narrative" and row["change_kind"] == change_kind
            for row in staged
        )
        if expected == 0:
            return 0
        refresh_id = str(staged[0]["refresh_run_id"])
        if change_kind == "delete":
            self._conn.execute(
                "DELETE FROM latest_governed_narrative_entries AS current "
                "WHERE current.scope_key=? AND EXISTS ("
                "SELECT 1 FROM latest_governed_refresh_stage stage "
                "WHERE stage.refresh_run_id=? AND stage.entity_kind='narrative' "
                "AND stage.change_kind='delete' "
                "AND json_extract(stage.canonical_payload_json,"
                "'$.row.expected_document_key')=current.expected_document_key "
                "AND json_extract(stage.canonical_payload_json,"
                "'$.row.chunk_key')=current.chunk_key)",
                (request.scope_id, refresh_id),
            )
            return expected
        timestamp = _db_time(request.operation_recorded_at)
        self._conn.execute(
            "INSERT INTO latest_governed_narrative_entries ("
            "scope_key,expected_document_key,chunk_key,digest_bucket,"
            "refresh_receipt_id,document_version_id,evidence_node_id,source_chunk_id,"
            "embedding_artifact_id,text,content_sha256,chunker_config_sha256,"
            "selection_reason,prior_commitment_sha256,current_commitment_sha256,"
            "knowledge_cutoff,observed_through,updated_at) "
            "SELECT ?,"
            "json_extract(stage.canonical_payload_json,'$.row.expected_document_key'),"
            "json_extract(stage.canonical_payload_json,'$.row.chunk_key'),"
            "stage.digest_bucket,?,"
            "json_extract(stage.canonical_payload_json,'$.row.document_version_id'),"
            "json_extract(stage.canonical_payload_json,'$.row.evidence_node_id'),"
            "json_extract(stage.canonical_payload_json,'$.row.source_chunk_id'),"
            "json_extract(stage.canonical_payload_json,'$.row.embedding_artifact_id'),"
            "json_extract(stage.canonical_payload_json,'$.row.text'),"
            "json_extract(stage.canonical_payload_json,'$.row.content_sha256'),"
            "json_extract(stage.canonical_payload_json,'$.row.chunker_config_sha256'),"
            "json_extract(stage.canonical_payload_json,'$.selection_reason'),"
            "stage.prior_commitment_sha256,stage.current_commitment_sha256,?,?,? "
            "FROM latest_governed_refresh_stage stage "
            "WHERE stage.refresh_run_id=? AND stage.entity_kind='narrative' "
            "AND stage.change_kind='upsert' "
            "ON CONFLICT(scope_key,expected_document_key,chunk_key) DO UPDATE SET "
            "digest_bucket=excluded.digest_bucket,"
            "refresh_receipt_id=excluded.refresh_receipt_id,"
            "document_version_id=excluded.document_version_id,"
            "evidence_node_id=excluded.evidence_node_id,"
            "source_chunk_id=excluded.source_chunk_id,"
            "embedding_artifact_id=excluded.embedding_artifact_id,text=excluded.text,"
            "content_sha256=excluded.content_sha256,"
            "chunker_config_sha256=excluded.chunker_config_sha256,"
            "selection_reason=excluded.selection_reason,"
            "prior_commitment_sha256=excluded.prior_commitment_sha256,"
            "current_commitment_sha256=excluded.current_commitment_sha256,"
            "knowledge_cutoff=excluded.knowledge_cutoff,"
            "observed_through=excluded.observed_through,updated_at=excluded.updated_at",
            (
                request.scope_id,
                receipt_id,
                _db_time(frontier.knowledge_cutoff),
                _db_time(frontier.observed_through),
                timestamp,
                refresh_id,
            ),
        )
        return expected

    def _apply_fact(
        self,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        stage: sqlite3.Row,
        payload: dict[str, object],
    ) -> int:
        row = cast(dict[str, object], payload["row"])
        if stage["change_kind"] == "delete":
            cursor = self._conn.execute(
                "DELETE FROM latest_governed_fact_entries "
                "WHERE scope_key=? AND canonical_metric_cell_id=?",
                (request.scope_id, row["canonical_metric_cell_id"]),
            )
            return max(0, cursor.rowcount)
        evidence = cast(dict[str, object], payload["source_evidence"])
        evidence_json = _canonical_json(evidence)
        values = (
            int(stage["digest_bucket"]),
            receipt_id,
            frontier.fact_generation_id,
            row["canonical_resolution_revision_id"],
            row["selected_observation_id"],
            row["canonical_metric_name"],
            row["period_kind"],
            row["period_start"],
            row["period_end"],
            row["unit_key"],
            row["currency"],
            row["value_kind"],
            row["canonical_value"],
            row["canonical_search_text"],
            payload["selection_reason"],
            evidence_json,
            _digest(evidence_json),
            stage["prior_commitment_sha256"],
            stage["current_commitment_sha256"],
            _db_time(frontier.knowledge_cutoff),
            _db_time(frontier.observed_through),
            _db_time(request.operation_recorded_at),
        )
        self._conn.execute(
            "INSERT INTO latest_governed_fact_entries ("
            "scope_key,canonical_metric_cell_id,digest_bucket,refresh_receipt_id,"
            "fact_generation_id,canonical_resolution_revision_id,"
            "selected_observation_id,canonical_metric_name,period_kind,period_start,"
            "period_end,unit_key,currency,value_kind,canonical_value,"
            "canonical_search_text,selection_reason,source_evidence_json,"
            "source_evidence_sha256,prior_commitment_sha256,"
            "current_commitment_sha256,knowledge_cutoff,observed_through,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_key,canonical_metric_cell_id) DO UPDATE SET "
            "digest_bucket=excluded.digest_bucket,"
            "refresh_receipt_id=excluded.refresh_receipt_id,"
            "fact_generation_id=excluded.fact_generation_id,"
            "canonical_resolution_revision_id=excluded.canonical_resolution_revision_id,"
            "selected_observation_id=excluded.selected_observation_id,"
            "canonical_metric_name=excluded.canonical_metric_name,"
            "period_kind=excluded.period_kind,period_start=excluded.period_start,"
            "period_end=excluded.period_end,unit_key=excluded.unit_key,"
            "currency=excluded.currency,value_kind=excluded.value_kind,"
            "canonical_value=excluded.canonical_value,"
            "canonical_search_text=excluded.canonical_search_text,"
            "selection_reason=excluded.selection_reason,"
            "source_evidence_json=excluded.source_evidence_json,"
            "source_evidence_sha256=excluded.source_evidence_sha256,"
            "prior_commitment_sha256=excluded.prior_commitment_sha256,"
            "current_commitment_sha256=excluded.current_commitment_sha256,"
            "knowledge_cutoff=excluded.knowledge_cutoff,"
            "observed_through=excluded.observed_through,updated_at=excluded.updated_at",
            (request.scope_id, row["canonical_metric_cell_id"], *values),
        )
        return 1

    def _apply_document(
        self,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        stage: sqlite3.Row,
        payload: dict[str, object],
    ) -> int:
        row = cast(dict[str, object], payload["row"])
        if stage["change_kind"] == "delete":
            cursor = self._conn.execute(
                "DELETE FROM latest_governed_document_entries "
                "WHERE scope_key=? AND expected_document_key=?",
                (request.scope_id, row["expected_document_key"]),
            )
            return max(0, cursor.rowcount)
        evidence = cast(dict[str, object], payload["source_evidence"])
        evidence_json = _canonical_json(evidence)
        values = (
            int(stage["digest_bucket"]),
            receipt_id,
            row["expected_document_id"],
            row["document_version_id"],
            row["source_kind"],
            row["document_type"],
            row["period_start"],
            row["period_end"],
            payload["selection_reason"],
            evidence_json,
            _digest(evidence_json),
            stage["prior_commitment_sha256"],
            stage["current_commitment_sha256"],
            _db_time(frontier.knowledge_cutoff),
            _db_time(frontier.observed_through),
            _db_time(request.operation_recorded_at),
        )
        self._conn.execute(
            "INSERT INTO latest_governed_document_entries ("
            "scope_key,expected_document_key,digest_bucket,refresh_receipt_id,"
            "expected_document_id,document_version_id,source_kind,document_type,"
            "period_start,period_end,selection_reason,source_evidence_json,"
            "source_evidence_sha256,prior_commitment_sha256,"
            "current_commitment_sha256,knowledge_cutoff,observed_through,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_key,expected_document_key) DO UPDATE SET "
            "digest_bucket=excluded.digest_bucket,"
            "refresh_receipt_id=excluded.refresh_receipt_id,"
            "expected_document_id=excluded.expected_document_id,"
            "document_version_id=excluded.document_version_id,"
            "source_kind=excluded.source_kind,document_type=excluded.document_type,"
            "period_start=excluded.period_start,period_end=excluded.period_end,"
            "selection_reason=excluded.selection_reason,"
            "source_evidence_json=excluded.source_evidence_json,"
            "source_evidence_sha256=excluded.source_evidence_sha256,"
            "prior_commitment_sha256=excluded.prior_commitment_sha256,"
            "current_commitment_sha256=excluded.current_commitment_sha256,"
            "knowledge_cutoff=excluded.knowledge_cutoff,"
            "observed_through=excluded.observed_through,updated_at=excluded.updated_at",
            (request.scope_id, stage["coordinate_key"], *values),
        )
        return 1

    def _apply_narrative(
        self,
        receipt_id: str,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        stage: sqlite3.Row,
        payload: dict[str, object],
    ) -> int:
        row = cast(dict[str, object], payload["row"])
        if stage["change_kind"] == "delete":
            cursor = self._conn.execute(
                "DELETE FROM latest_governed_narrative_entries "
                "WHERE scope_key=? AND expected_document_key=? AND chunk_key=?",
                (
                    request.scope_id,
                    row["expected_document_key"],
                    row["chunk_key"],
                ),
            )
            return max(0, cursor.rowcount)
        values = (
            int(stage["digest_bucket"]),
            receipt_id,
            row["document_version_id"],
            row["evidence_node_id"],
            row["source_chunk_id"],
            row["embedding_artifact_id"],
            row["text"],
            row["content_sha256"],
            row["chunker_config_sha256"],
            payload["selection_reason"],
            stage["prior_commitment_sha256"],
            stage["current_commitment_sha256"],
            _db_time(frontier.knowledge_cutoff),
            _db_time(frontier.observed_through),
            _db_time(request.operation_recorded_at),
        )
        self._conn.execute(
            "INSERT INTO latest_governed_narrative_entries ("
            "scope_key,expected_document_key,chunk_key,digest_bucket,"
            "refresh_receipt_id,document_version_id,evidence_node_id,source_chunk_id,"
            "embedding_artifact_id,text,content_sha256,chunker_config_sha256,"
            "selection_reason,prior_commitment_sha256,current_commitment_sha256,"
            "knowledge_cutoff,observed_through,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope_key,expected_document_key,chunk_key) DO UPDATE SET "
            "digest_bucket=excluded.digest_bucket,"
            "refresh_receipt_id=excluded.refresh_receipt_id,"
            "document_version_id=excluded.document_version_id,"
            "evidence_node_id=excluded.evidence_node_id,"
            "source_chunk_id=excluded.source_chunk_id,"
            "embedding_artifact_id=excluded.embedding_artifact_id,text=excluded.text,"
            "content_sha256=excluded.content_sha256,"
            "chunker_config_sha256=excluded.chunker_config_sha256,"
            "selection_reason=excluded.selection_reason,"
            "prior_commitment_sha256=excluded.prior_commitment_sha256,"
            "current_commitment_sha256=excluded.current_commitment_sha256,"
            "knowledge_cutoff=excluded.knowledge_cutoff,"
            "observed_through=excluded.observed_through,updated_at=excluded.updated_at",
            (
                request.scope_id,
                row["expected_document_key"],
                row["chunk_key"],
                *values,
            ),
        )
        return 1

    def _replayed_result(
        self,
        refresh_id: str,
        apply: bool,
    ) -> LatestGovernedRefreshResult | None:
        row = self._conn.execute(
            "SELECT receipt.receipt_id,receipt.receipt_sha256,receipt.change_count,"
            "receipt.fact_change_count,receipt.document_change_count,"
            "receipt.narrative_change_count,receipt.scope_key,"
            "receipt.prior_receipt_id "
            "FROM latest_governed_refresh_runs run "
            "JOIN latest_governed_refresh_receipts receipt "
            "ON receipt.refresh_run_id=run.refresh_run_id "
            "WHERE run.refresh_run_id=? AND run.status='finalized'",
            (refresh_id,),
        ).fetchone()
        if row is None:
            return None
        change_count = int(row[2])
        if change_count == 0:
            head = self._head(str(row[6]))
            if head is None or head.receipt_id != str(row[7]):
                raise LatestGovernedStateError(
                    "replayed no-op receipt is not bound to the unchanged current head"
                )
            if not apply:
                return self._planned_result(
                    refresh_id=refresh_id,
                    prior=head,
                    outcome="no_op",
                    terminal=head.state_commitment_sha256,
                )
            return self._noop_result(
                refresh_id=refresh_id,
                head_receipt_id=head.receipt_id,
                receipt_sha=str(row[1]),
            )
        return LatestGovernedRefreshResult(
            mode="apply" if apply else "dry_run",
            outcome="no_op" if change_count == 0 else "changed",
            refresh_id=refresh_id,
            head_id=str(row[0]),
            created_count=0,
            replayed_count=1,
            source_event_count=0,
            fact_change_count=int(row[3]),
            document_change_count=int(row[4]),
            narrative_change_count=int(row[5]),
            source_read_count=0,
            current_read_count=0,
            current_write_count=0,
            receipt_write_count=0,
            terminal_commitment=str(row[1]),
        )

    def _finalize_replayed_hook(
        self,
        *,
        request: LatestGovernedRefreshRequest,
        frontier: ChangeFrontier,
        replay: LatestGovernedRefreshResult,
        finalization_hook: LatestGovernedFinalizationHook,
    ) -> None:
        """Attach an outer atomic receipt to an already-finalized refresh replay."""

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._frontier(request.scope_id) != frontier:
                raise LatestGovernedStateError(
                    "latest governed frontier changed before replay finalization"
                )
            row = self._conn.execute(
                "SELECT receipt_id FROM latest_governed_refresh_receipts WHERE refresh_run_id=?",
                (replay.refresh_id,),
            ).fetchone()
            if row is None:
                raise LatestGovernedStateError("replayed refresh receipt disappeared")
            receipt = self._receipt_for_refresh(
                refresh_id=replay.refresh_id,
                receipt_id=str(row[0]),
                scope_id=request.scope_id,
            )
            head = self._head(request.scope_id)
            if receipt is None or head is None:
                raise LatestGovernedStateError("replayed refresh has no current head")
            if (
                request.expected_terminal_commitment is not None
                and request.expected_terminal_commitment != head.state_commitment_sha256
            ):
                raise LatestGovernedStateError(
                    "latest governed replay terminal differs from admission"
                )
            finalization_hook(self._conn, request, frontier, receipt, head)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _planned_result(
        self,
        *,
        refresh_id: str,
        prior: CurrentHead,
        outcome: Literal["no_op", "changed"],
        terminal: str,
    ) -> LatestGovernedRefreshResult:
        return LatestGovernedRefreshResult(
            mode="dry_run",
            outcome=outcome,
            refresh_id=refresh_id,
            head_id=prior.receipt_id,
            created_count=0,
            replayed_count=0,
            source_event_count=0,
            fact_change_count=0,
            document_change_count=0,
            narrative_change_count=0,
            source_read_count=0,
            current_read_count=1,
            current_write_count=0,
            receipt_write_count=0,
            terminal_commitment=terminal,
        )


def _stage_change(
    *,
    entity_kind: Literal["fact", "document", "narrative"],
    change_kind: Literal["upsert", "delete"],
    coordinate: str,
    prior_sha: str | None,
    current_sha: str | None,
    row: dict[str, object],
    reason: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    if change_kind == "upsert" and current_sha is None:
        raise ValueError("upsert stage requires a current commitment")
    if change_kind == "delete" and current_sha is not None:
        raise ValueError("delete stage cannot retain a current commitment")
    return {
        "stage_ordinal": 0,
        "entity_kind": entity_kind,
        "change_kind": change_kind,
        "coordinate_key": coordinate,
        "digest_bucket": _bucket(coordinate),
        "prior_commitment_sha256": prior_sha,
        "current_commitment_sha256": current_sha,
        "payload": {
            "row": row,
            "selection_reason": reason,
            "source_evidence": evidence,
        },
    }


def _source_event_count(
    *,
    frontier: ChangeFrontier,
    prior: CurrentHead | None,
) -> int:
    previous = 0 if prior is None else prior.source_frontier.source_publication_high_watermark
    current = frontier.source_publication_high_watermark
    if current < previous:
        raise LatestGovernedStateError("source publication high-watermark regressed")
    return current - previous


def _transition_commitment(
    *,
    prior: CurrentHead | None,
    frontier: ChangeFrontier,
    policy_sha: str,
    changes: list[dict[str, object]],
) -> dict[str, str]:
    zero = "0" * 64
    prior_roots = {
        "fact": zero if prior is None else prior.fact_root_sha256,
        "document": zero if prior is None else prior.document_root_sha256,
        "narrative": zero if prior is None else prior.narrative_root_sha256,
    }
    roots: dict[str, str] = {}
    for kind in ("fact", "document", "narrative"):
        vector = [
            {
                "change_kind": item["change_kind"],
                "coordinate_key": item["coordinate_key"],
                "current_commitment_sha256": item["current_commitment_sha256"],
                "prior_commitment_sha256": item["prior_commitment_sha256"],
            }
            for item in changes
            if item["entity_kind"] == kind
        ]
        roots[kind] = (
            prior_roots[kind]
            if not vector
            else _digest(
                {
                    "prior_root_sha256": prior_roots[kind],
                    "ordered_changes": vector,
                    "root_version": "latest-governed-transition-root.v1",
                }
            )
        )
    roots["state"] = _digest(
        {
            "document_root_sha256": roots["document"],
            "fact_root_sha256": roots["fact"],
            "frontier_sha256": frontier.commitment_sha256,
            "narrative_root_sha256": roots["narrative"],
            "policy_config_sha256": policy_sha,
            "state_version": "latest-governed-state.v1",
        }
    )
    return roots


def _transition_change_digest(item: dict[str, object]) -> str:
    return _digest(
        {
            "entity_kind": item["entity_kind"],
            "change_kind": item["change_kind"],
            "coordinate_key": item["coordinate_key"],
            "prior_commitment_sha256": item["prior_commitment_sha256"],
            "current_commitment_sha256": item["current_commitment_sha256"],
        }
    )


def _change_audit(
    *,
    prior: CurrentHead | None,
    changes: list[dict[str, object]],
) -> tuple[list[object], str]:
    if prior is not None:
        return (
            [_transition_change_digest(item) for item in changes],
            "coordinate_changes.v1",
        )
    buckets: dict[int, list[str]] = {}
    for item in changes:
        bucket = int(str(item["digest_bucket"]))
        if bucket != _bucket(str(item["coordinate_key"])):
            raise LatestGovernedStateError(
                "baseline change digest bucket does not match its coordinate"
            )
        buckets.setdefault(bucket, []).append(_transition_change_digest(item))
    commitments: list[object] = [
        {
            "change_count": len(change_hashes),
            "commitment_sha256": _digest(
                {
                    "digest_bucket": bucket,
                    "ordered_change_sha256s": change_hashes,
                    "version": "latest-governed-baseline-bucket.v1",
                }
            ),
            "digest_bucket": bucket,
        }
        for bucket, change_hashes in sorted(buckets.items())
    ]
    return commitments, "baseline_digest_buckets.v1"


def _receipt_payload(
    *,
    receipt_id: str,
    refresh_id: str,
    request: LatestGovernedRefreshRequest,
    frontier: ChangeFrontier,
    prior: CurrentHead | None,
    current_state_sha: str,
    roots: dict[str, str],
    change_set: list[object],
    change_audit_mode: str,
    counts: dict[str, int],
    policy_sha: str,
    rollback_target_receipt_id: str | None = None,
) -> dict[str, object]:
    return {
        "baseline": {
            "fact_generation_id": frontier.fact_generation_id,
            "fact_projection_seal_sha256": frontier.fact_projection_seal_sha256,
            "population_receipt_set_sha256": frontier.population_receipt_set_sha256,
            "population_run_id": frontier.population_run_id,
            "promotion_id": frontier.promotion_id,
            "research_snapshot_id": frontier.research_snapshot_id,
        },
        "change_counts": counts,
        "change_audit": {
            "bucket_count": (
                len(change_set) if change_audit_mode == "baseline_digest_buckets.v1" else 0
            ),
            "change_count": sum(counts.values()),
            "mode": change_audit_mode,
        },
        "change_set_sha256": _digest(change_set),
        "clocks": {
            "knowledge_cutoff": _canonical_time(frontier.knowledge_cutoff),
            "observed_through": _canonical_time(frontier.observed_through),
            "operation_recorded_at": _canonical_time(request.operation_recorded_at),
        },
        "current_state_sha256": current_state_sha,
        "frontier": frontier.model_dump(mode="json"),
        "frontier_sha256": frontier.commitment_sha256,
        "policy_config_sha256": policy_sha,
        "prior_receipt_id": None if prior is None else prior.receipt_id,
        "prior_state_sha256": None if prior is None else prior.state_commitment_sha256,
        "receipt_id": receipt_id,
        "refresh_run_id": refresh_id,
        "retention_policy": RETENTION_POLICY_PROPOSAL,
        "rollback_receipt_id": (None if prior is None else prior.receipt_id),
        "rollback_target_receipt_id": rollback_target_receipt_id,
        "roots": {
            "document": roots["document"],
            "fact": roots["fact"],
            "narrative": roots["narrative"],
        },
        "scope_id": request.scope_id,
        "version": "latest-governed-refresh-receipt.v1",
    }


def _json_object(value: object) -> dict[str, object]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise LatestGovernedStateError("latest governed JSON payload is not an object")
    return cast(dict[str, object], decoded)


_EFFECTIVE_FACT_SQL = """
WITH RECURSIVE lineage(generation_id,parent_generation_id,depth) AS (
 SELECT generation_id,parent_generation_id,0
 FROM canonical_fact_projection_generations WHERE generation_id=?
 UNION ALL
 SELECT parent.generation_id,parent.parent_generation_id,lineage.depth+1
 FROM canonical_fact_projection_generations parent
 JOIN lineage ON parent.generation_id=lineage.parent_generation_id
 WHERE lineage.depth<32
),
ranked AS (
 SELECT entry.*,lineage.depth,
 row_number() OVER (
   PARTITION BY entry.canonical_metric_cell_id ORDER BY lineage.depth ASC
 ) AS state_rank
 FROM lineage
 JOIN canonical_fact_projection_entries entry
 ON entry.generation_id=lineage.generation_id
 JOIN canonical_metric_cells cell
 ON cell.canonical_metric_cell_id=entry.canonical_metric_cell_id
 WHERE cell.reporting_entity_id IN (
   SELECT CAST(value AS TEXT) FROM json_each(?)
 )
)
SELECT * FROM ranked WHERE state_rank=1 ORDER BY canonical_metric_cell_id
"""
