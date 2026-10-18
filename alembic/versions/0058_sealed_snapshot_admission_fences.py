"""Reject new admissions that would change a retained native snapshot selection.

Old records and seals retain their clocks and bytes. The guards follow both the
stored text predicates and SQLite's second-precision datetime predicates. This
deliberately blocks same-second late admissions; it does not invent a new clock
or authorize retroactive population. Exact retained rows can still be replayed.
"""

from __future__ import annotations

from alembic import op

revision = "0058_sealed_snapshot_admission_fences"
down_revision = "0057_companyfacts_extractor_identity"
branch_labels = None
depends_on = None

# Every record producer in v_ontology_snapshot_expected_members, including the
# separate canonical-cell seal. Dimensions already reject writes after sealing.
_ONTOLOGY_CLOCKS = {
    "canonical_metrics": ("effective_at", "knowledge_at", "recorded_at"),
    "canonical_axes": ("effective_at", "knowledge_at", "recorded_at"),
    "canonical_members": ("effective_at", "knowledge_at", "recorded_at"),
    "source_taxonomy_components": ("effective_at", "knowledge_at", "recorded_at"),
    "source_observation_taxonomy_assertions": ("knowledge_at", "recorded_at"),
    "canonical_metric_definition_revisions": ("effective_at", "knowledge_at", "recorded_at"),
    "source_dimension_mapping_revisions": ("effective_at", "knowledge_at", "recorded_at"),
    "metric_mapping_revisions": ("effective_at", "knowledge_at", "recorded_at"),
    "canonical_metric_cells": ("effective_at", "knowledge_at", "recorded_at"),
    "canonical_metric_cell_seals": ("sealed_at",),
    "fact_cell_canonical_binding_revisions": ("effective_at", "knowledge_at", "recorded_at"),
}


def _exact_replay(table: str) -> str:
    # The table names are the closed migration-owned set above, plus resolutions.
    columns = op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    comparison = " AND ".join(f"retained.{row[1]} IS NEW.{row[1]}" for row in columns)
    return f"EXISTS (SELECT 1 FROM {table} retained WHERE {comparison})"


def _create(sql: str) -> None:
    """Resume matching partial DDL without accepting a different same-name guard."""
    _create_word, kind, name, *_rest = sql.split()
    existing = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind.lower(), name)
        )
        .fetchone()
    )
    if existing is not None:
        if " ".join(str(existing[0]).strip().removesuffix(";").split()) != " ".join(sql.split()):
            raise RuntimeError(f"sealed admission migration object conflicts: {name}")
        return
    op.execute(sql)


def upgrade() -> None:
    _create(
        "CREATE INDEX ix_ontology_snapshot_admission_cutoff "
        "ON ontology_snapshot_headers(cutoff_at,ontology_snapshot_id)"
    )
    _create(
        "CREATE INDEX ix_ontology_snapshot_admission_second "
        "ON ontology_snapshot_headers(datetime(cutoff_at),ontology_snapshot_id)"
    )
    _create(
        "CREATE INDEX ix_resolution_snapshot_admission_entity "
        "ON canonical_fact_resolution_snapshot_scope_members"
        "(reporting_entity_id,resolution_snapshot_id)"
    )
    for table, clocks in _ONTOLOGY_CLOCKS.items():
        exact = " AND ".join(f"NEW.{clock}<=header.cutoff_at" for clock in clocks)
        seconds = " AND ".join(
            f"datetime(NEW.{clock})<=datetime(header.cutoff_at)" for clock in clocks
        )
        # Global ontology membership has no issuer filter. Conservatively guard
        # cells before their seal as well, rather than leave a partial new cell.
        _create(
            f"CREATE TRIGGER trg_{table}_sealed_admission "
            f"BEFORE INSERT ON {table} "
            f"WHEN NOT ({_exact_replay(table)}) AND EXISTS ("
            "SELECT 1 FROM ontology_snapshot_headers header "
            "JOIN ontology_snapshot_seals seal "
            "ON seal.ontology_snapshot_id=header.ontology_snapshot_id "
            f"WHERE ({exact}) OR ({seconds})) "
            "BEGIN SELECT RAISE(ABORT,'sealed_ontology_admission_conflict'); END"
        )
    table = "canonical_fact_resolution_revisions"
    _create(
        f"CREATE TRIGGER trg_{table}_sealed_admission "
        f"BEFORE INSERT ON {table} "
        f"WHEN NOT ({_exact_replay(table)}) AND EXISTS ("
        "SELECT 1 FROM canonical_fact_resolution_snapshot_seals seal "
        "JOIN canonical_fact_resolution_snapshot_scope_members member "
        "ON member.resolution_snapshot_id=seal.resolution_snapshot_id "
        "JOIN canonical_metric_cells cell "
        "ON cell.reporting_entity_id=member.reporting_entity_id "
        "WHERE cell.canonical_metric_cell_id=NEW.canonical_metric_cell_id AND ("
        "(NEW.knowledge_at<=seal.cutoff_at AND NEW.recorded_at<=seal.recorded_at) OR "
        "(datetime(NEW.knowledge_at)<=datetime(seal.cutoff_at) AND "
        "datetime(NEW.recorded_at)<=datetime(seal.recorded_at)))) "
        "BEGIN SELECT RAISE(ABORT,'sealed_resolution_admission_conflict'); END"
    )


def downgrade() -> None:
    for table in (*_ONTOLOGY_CLOCKS, "canonical_fact_resolution_revisions"):
        op.execute(f"DROP TRIGGER trg_{table}_sealed_admission")
    op.execute("DROP INDEX ix_resolution_snapshot_admission_entity")
    op.execute("DROP INDEX ix_ontology_snapshot_admission_second")
    op.execute("DROP INDEX ix_ontology_snapshot_admission_cutoff")
