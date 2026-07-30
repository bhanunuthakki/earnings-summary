"""Build an additive live cutover candidate without replacing either authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from sqlite3 import Connection

from pydantic import BaseModel, ConfigDict, Field

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

_GOVERNED_PREFIXES = (
    "alembic_",
    "ask_retrieval_",
    "canonical_fact_",
    "document_processing_",
    "document_semantic_",
    "embedding_",
    "evidence_",
    "expected_document",
    "fact_",
    "filing_xbrl_",
    "financial_fact_",
    "heterogeneous_retrieval_",
    "image_ocr_",
    "issuer_",
    "kpi_fact_",
    "legacy_document_evidence_",
    "legacy_fact_evidence_",
    "legacy_issuer_",
    "metric_",
    "observation_resolution_",
    "ocr_",
    "ontology_",
    "pdf_table_extraction_",
    "population_",
    "provenance_",
    "recorded_subject_binding_",
    "reported_observations",
    "reporting_entit",
    "research_snapshot_",
    "retrieval_",
    "search_",
    "securit",
    "source_coverage_",
    "source_dimension_",
    "source_fact_",
    "source_inventory_",
    "source_obligation_",
    "source_observation_taxonomy_",
    "source_taxonomy_",
)


class LiveCutoverMergeError(RuntimeError):
    """The candidate cannot be planned or applied without weakening authority."""


class TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    type: str
    notnull: int
    default: object
    pk: int


class MergeTablePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table: str
    strategy: str
    primary_key: tuple[str, ...]
    live_row_count: int = Field(ge=0)
    governed_row_count: int = Field(ge=0)
    added_row_count: int = Field(ge=0)
    changed_row_count: int = Field(ge=0)
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveCutoverMergePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_name: str
    policy_version: str
    live_database: str
    governed_database: str
    alembic_revision: str
    tables: tuple[MergeTablePlan, ...]
    governed_table_count: int = Field(ge=0)
    operational_table_count: int = Field(ge=0)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AppliedMergeTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table: str
    changed_rows: int = Field(ge=0)
    destination_row_count: int = Field(ge=0)
    live_rows_not_preserved: int = Field(ge=0)


class LiveCutoverMergeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plan: LiveCutoverMergePlan
    destination_database: str
    applied_tables: tuple[AppliedMergeTable, ...]
    quick_check: str
    foreign_key_violations: int = Field(ge=0)
    destination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def plan_live_cutover_merge(
    live_database: Path,
    governed_database: Path,
) -> LiveCutoverMergePlan:
    """Plan exact live-authoritative deltas over one governed substrate."""
    live_path = live_database.resolve()
    governed_path = governed_database.resolve()
    if live_path == governed_path:
        raise LiveCutoverMergeError("live and governed databases must be distinct")
    live = connect_sqlite(live_path, role=SQLiteConnectionRole.READ_ONLY)
    governed = connect_sqlite(governed_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        _require_healthy("live", live)
        _require_healthy("governed", governed)
        revision = _require_same_revision(live, governed)
        live_tables = _table_names(live)
        governed_tables = _table_names(governed)
        plans: list[MergeTablePlan] = []
        governed_count = 0
        operational_count = 0
        for table in sorted(live_tables & governed_tables):
            if _is_governed_table(table):
                governed_count += 1
                continue
            operational_count += 1
            live_count = _row_count(live, table)
            if live_count == 0:
                continue
            live_schema = _table_schema(live, table)
            governed_schema = _table_schema(governed, table)
            if live_schema != governed_schema:
                raise LiveCutoverMergeError(f"schema mismatch for operational table {table}")
            primary_key = tuple(
                column.name
                for column in sorted(live_schema, key=lambda column: column.pk)
                if column.pk
            )
            added_count, changed_count = _delta_counts(
                live,
                governed,
                table=table,
                columns=tuple(column.name for column in live_schema),
                primary_key=primary_key,
            )
            if added_count == 0 and changed_count == 0:
                continue
            plans.append(
                MergeTablePlan(
                    table=table,
                    strategy="upsert_live" if primary_key else "append_exact",
                    primary_key=primary_key,
                    live_row_count=live_count,
                    governed_row_count=_row_count(governed, table),
                    added_row_count=added_count,
                    changed_row_count=changed_count,
                    schema_sha256=_canonical_sha(
                        [column.model_dump(mode="json") for column in live_schema]
                    ),
                )
            )
        commitment_payload = {
            "policy_name": "additive_live_operational_authority_merge",
            "policy_version": "1",
            "live_database": str(live_path),
            "governed_database": str(governed_path),
            "alembic_revision": revision,
            "tables": [plan.model_dump(mode="json") for plan in plans],
            "governed_table_count": governed_count,
            "operational_table_count": operational_count,
        }
        return LiveCutoverMergePlan(
            policy_name="additive_live_operational_authority_merge",
            policy_version="1",
            live_database=str(live_path),
            governed_database=str(governed_path),
            alembic_revision=revision,
            tables=tuple(plans),
            governed_table_count=governed_count,
            operational_table_count=operational_count,
            plan_sha256=_canonical_sha(commitment_payload),
        )
    finally:
        governed.close()
        live.close()


def apply_live_cutover_merge(
    live_database: Path,
    governed_database: Path,
    destination_database: Path,
    *,
    expected_plan_sha256: str,
) -> LiveCutoverMergeReceipt:
    """Copy the governed DB and atomically merge the committed live deltas."""
    destination = destination_database.resolve()
    source_paths = {live_database.resolve(), governed_database.resolve()}
    if destination in source_paths:
        raise LiveCutoverMergeError("destination must not replace either source database")
    if destination.exists():
        raise LiveCutoverMergeError("destination already exists")
    plan = plan_live_cutover_merge(live_database, governed_database)
    if plan.plan_sha256 != expected_plan_sha256:
        raise LiveCutoverMergeError(
            "plan commitment mismatch; rerun dry-run and review the new authority delta"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_database(governed_database.resolve(), destination)
    applied: list[AppliedMergeTable] = []
    connection = connect_sqlite(
        destination,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    live_connection = connect_sqlite(
        live_database.resolve(),
        role=SQLiteConnectionRole.READ_ONLY,
    )
    try:
        with connection:
            for table_plan in plan.tables:
                _stage_live_table(connection, live_connection, table_plan)
                before = connection.total_changes
                _merge_table(connection, table_plan)
                changed_rows = connection.total_changes - before
                missing = _live_rows_not_preserved(connection, table_plan)
                connection.execute("DROP TABLE temp._live_authority_rows")
                if missing:
                    raise LiveCutoverMergeError(
                        f"{table_plan.table} failed live-authority preservation: {missing}"
                    )
                applied.append(
                    AppliedMergeTable(
                        table=table_plan.table,
                        changed_rows=changed_rows,
                        destination_row_count=_row_count(connection, table_plan.table),
                        live_rows_not_preserved=missing,
                    )
                )
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if quick_check != "ok" or foreign_key_violations:
            raise LiveCutoverMergeError(
                "candidate integrity failed after additive merge: "
                f"quick_check={quick_check}, foreign_keys={foreign_key_violations}"
            )
    except Exception:
        live_connection.close()
        connection.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        live_connection.close()
        connection.close()
    return LiveCutoverMergeReceipt(
        plan=plan,
        destination_database=str(destination),
        applied_tables=tuple(applied),
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        destination_sha256=_file_sha256(destination),
    )


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = connect_sqlite(source_path, role=SQLiteConnectionRole.READ_ONLY)
    destination = connect_sqlite(
        destination_path,
        role=SQLiteConnectionRole.SNAPSHOT_DESTINATION,
        schema_preflight=False,
    )
    try:
        source.backup(destination)
    except Exception:
        destination.close()
        source.close()
        destination_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()


def _merge_table(connection: Connection, plan: MergeTablePlan) -> None:
    columns = tuple(
        row["name"] for row in connection.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    column_sql = ", ".join(_quote(column) for column in columns)
    source_columns = ", ".join(f"src.{_quote(column)}" for column in columns)
    equality = _row_equality("dst", "src", columns)
    if plan.primary_key:
        pk_sql = ", ".join(_quote(column) for column in plan.primary_key)
        updates = ", ".join(
            f"{_quote(column)} = excluded.{_quote(column)}"
            for column in columns
            if column not in plan.primary_key
        )
        # The generated tokens contain quoted, schema-derived identifiers only.
        conflict_clause = (
            f"DO UPDATE SET {updates}" if updates else "DO NOTHING"  # nosec B608
        )
        connection.execute(
            f"INSERT INTO main.{_quote(plan.table)} ({column_sql}) "  # nosec B608
            f"SELECT {source_columns} FROM temp._live_authority_rows AS src "
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
            f") ON CONFLICT ({pk_sql}) {conflict_clause}"
        )
        return
    connection.execute(
        f"INSERT INTO main.{_quote(plan.table)} ({column_sql}) "  # nosec B608
        f"SELECT {source_columns} FROM temp._live_authority_rows AS src "
        f"WHERE NOT EXISTS ("
        f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
        f")"
    )


def _live_rows_not_preserved(connection: Connection, plan: MergeTablePlan) -> int:
    columns = tuple(
        row["name"] for row in connection.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    equality = _row_equality("dst", "src", columns)
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM temp._live_authority_rows AS src "  # nosec B608
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(plan.table)} AS dst WHERE {equality}"
            f")"
        ).fetchone()[0]
    )


def _stage_live_table(
    destination: Connection,
    live: Connection,
    plan: MergeTablePlan,
) -> None:
    destination.execute("DROP TABLE IF EXISTS temp._live_authority_rows")
    destination.execute(
        f"CREATE TEMP TABLE _live_authority_rows AS "  # nosec B608
        f"SELECT * FROM main.{_quote(plan.table)} WHERE 0"
    )
    columns = tuple(
        str(row["name"]) for row in live.execute(f"PRAGMA table_info({_quote(plan.table)})")
    )
    placeholders = ", ".join("?" for _ in columns)
    # The placeholder count comes only from the inspected schema.
    insert_sql = f"INSERT INTO temp._live_authority_rows VALUES ({placeholders})"  # nosec B608
    # The table identifier is schema-derived and double-quoted.
    cursor = live.execute(
        f"SELECT * FROM {_quote(plan.table)}"  # nosec B608
    )
    while rows := cursor.fetchmany(1_000):
        destination.executemany(insert_sql, (tuple(row) for row in rows))


def _delta_counts(
    live: Connection,
    governed: Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
) -> tuple[int, int]:
    governed.execute(
        "ATTACH DATABASE ? AS live_delta",
        (Path(_database_path(live)).resolve().as_uri() + "?mode=ro",),
    )
    try:
        if primary_key:
            key_match = _row_equality("dst", "src", primary_key)
            row_match = _row_equality("dst", "src", columns)
            added = governed.execute(
                f"SELECT COUNT(*) FROM live_delta.{_quote(table)} AS src "  # nosec B608
                f"WHERE NOT EXISTS ("
                f"SELECT 1 FROM main.{_quote(table)} AS dst WHERE {key_match}"
                f")"
            ).fetchone()[0]
            changed = governed.execute(
                f"SELECT COUNT(*) FROM live_delta.{_quote(table)} AS src "  # nosec B608
                f"WHERE EXISTS ("
                f"SELECT 1 FROM main.{_quote(table)} AS dst "
                f"WHERE {key_match} AND NOT ({row_match})"
                f")"
            ).fetchone()[0]
            return int(added), int(changed)
        row_match = _row_equality("dst", "src", columns)
        added = governed.execute(
            f"SELECT COUNT(*) FROM live_delta.{_quote(table)} AS src "  # nosec B608
            f"WHERE NOT EXISTS ("
            f"SELECT 1 FROM main.{_quote(table)} AS dst WHERE {row_match}"
            f")"
        ).fetchone()[0]
        return int(added), 0
    finally:
        governed.execute("DETACH DATABASE live_delta")


def _database_path(connection: Connection) -> str:
    rows = connection.execute("PRAGMA database_list").fetchall()
    return str(next(row[2] for row in rows if row[1] == "main"))


def _require_healthy(label: str, connection: Connection) -> None:
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        raise LiveCutoverMergeError(f"{label} quick_check failed: {quick}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise LiveCutoverMergeError(f"{label} has {len(violations)} foreign-key violation(s)")


def _require_same_revision(live: Connection, governed: Connection) -> str:
    live_revision = _revision(live)
    governed_revision = _revision(governed)
    if live_revision != governed_revision:
        raise LiveCutoverMergeError(
            f"Alembic revision mismatch: live={live_revision}, governed={governed_revision}"
        )
    return live_revision


def _revision(connection: Connection) -> str:
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise LiveCutoverMergeError("database must have exactly one Alembic revision")
    return str(rows[0][0])


def _table_names(connection: Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_schema(connection: Connection, table: str) -> tuple[TableColumn, ...]:
    return tuple(
        TableColumn(
            name=str(row["name"]),
            type=str(row["type"]),
            notnull=int(row["notnull"]),
            default=row["dflt_value"],
            pk=int(row["pk"]),
        )
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    )


def _row_count(connection: Connection, table: str) -> int:
    # The table identifier is schema-derived and double-quoted.
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_quote(table)}"  # nosec B608
        ).fetchone()[0]
    )


def _is_governed_table(table: str) -> bool:
    return table.startswith(_GOVERNED_PREFIXES)


def _row_equality(left: str, right: str, columns: Iterable[str]) -> str:
    return " AND ".join(
        f"{left}.{_quote(column)} IS {right}.{_quote(column)}" for column in columns
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _canonical_sha(value: object) -> str:
    payload = json.dumps(
        value,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
