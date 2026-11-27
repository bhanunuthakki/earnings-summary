"""Purge currently quarantined KPI facts and their dependent capture rows.

Revision ID: 0076_purge_quarantined_kpi_facts
Revises: 0075_admit_google_finance_beta_transcript_provider
Create Date: 2026-11-27

Deletes every ``kpi_facts`` row whose semantic contexts are exclusively
``status = 'quarantined'`` along with those quarantined
``kpi_fact_semantic_contexts`` rows and any matching
``kpi_legacy_disposition_captures`` rows, clearing nullable
``kpi_facts.supersedes_id`` and ``metric_computation_attempts.kpi_fact_id``
references while preserving admitted facts (including any ``revision = 1``
quarantined predecessor superseded by an admitted context) and the append-only
observation/resolution ledger.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence

from sqlalchemy.engine import Connection

from alembic import op

revision = "0076_purge_quarantined_kpi_facts"
down_revision = "0075_admit_google_finance_beta_transcript_provider"
branch_labels = None
depends_on = None

_CHUNK_SIZE = 500
_DELETE_TRIGGERS = (
    "trg_kpi_legacy_disposition_captures_no_delete",
    "trg_kpi_fact_semantic_contexts_no_delete",
    "trg_kpi_facts_observation_delete",
)
_CANDIDATE_FACT_IDS_SQL = """
SELECT DISTINCT sc.kpi_fact_id
FROM kpi_fact_semantic_contexts AS sc
WHERE sc.status = 'quarantined'
  AND NOT EXISTS (
    SELECT 1
    FROM kpi_fact_semantic_contexts AS sc2
    WHERE sc2.kpi_fact_id = sc.kpi_fact_id
      AND sc2.status <> 'quarantined'
  )
ORDER BY sc.kpi_fact_id
"""


def _chunks(values: Sequence[int], size: int = _CHUNK_SIZE) -> Iterator[tuple[int, ...]]:
    for idx in range(0, len(values), size):
        yield tuple(values[idx : idx + size])


def _placeholders(values: Sequence[int]) -> str:
    return ",".join("?" for _ in values)


def _object_exists(connection: Connection, obj_type: str, name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ? LIMIT 1",
        (obj_type, name),
    ).fetchone()
    return row is not None


def _load_context_revisions(
    connection: Connection,
    fact_ids: Sequence[int],
) -> tuple[list[int], dict[int, list[int]]]:
    ctx_ids: list[int] = []
    by_revision: dict[int, list[int]] = defaultdict(list)
    for chunk in _chunks(fact_ids):
        ph = _placeholders(chunk)
        rows = connection.exec_driver_sql(
            f"SELECT id, revision FROM kpi_fact_semantic_contexts "  # nosec B608 -- bound integer placeholder list
            f"WHERE kpi_fact_id IN ({ph}) ORDER BY revision DESC, id",
            chunk,
        ).fetchall()
        for row in rows:
            ctx_id = int(row[0])
            rev = int(row[1])
            ctx_ids.append(ctx_id)
            by_revision[rev].append(ctx_id)
    return ctx_ids, by_revision


def _assert_no_retained_references(
    connection: Connection,
    fact_ids: Sequence[int],
    ctx_ids: Sequence[int],
) -> None:
    ctx_id_set = set(ctx_ids)
    for chunk in _chunks(ctx_ids):
        ph = _placeholders(chunk)
        rows = connection.exec_driver_sql(
            f"SELECT id FROM kpi_fact_semantic_contexts "  # nosec B608 -- bound integer placeholder list
            f"WHERE supersedes_context_id IN ({ph})",
            chunk,
        ).fetchall()
        if any(int(row[0]) not in ctx_id_set for row in rows):
            raise RuntimeError(
                "quarantined KPI purge aborted: retained semantic context supersedes purged context"
            )

    if _object_exists(connection, "table", "report_kpi_reference_resolution_revisions"):
        for chunk in _chunks(fact_ids):
            ph = _placeholders(chunk)
            if (
                connection.exec_driver_sql(
                    f"SELECT 1 FROM report_kpi_reference_resolution_revisions "  # nosec B608 -- bound integer placeholder list
                    f"WHERE evidence_fact_id IN ({ph}) LIMIT 1",
                    chunk,
                ).fetchone()
                is not None
            ):
                raise RuntimeError(
                    "quarantined KPI purge aborted: referenced by report_kpi_reference_resolution_revisions.evidence_fact_id"
                )
        for chunk in _chunks(ctx_ids):
            ph = _placeholders(chunk)
            if (
                connection.exec_driver_sql(
                    f"SELECT 1 FROM report_kpi_reference_resolution_revisions "  # nosec B608 -- bound integer placeholder list
                    f"WHERE evidence_context_id IN ({ph}) LIMIT 1",
                    chunk,
                ).fetchone()
                is not None
            ):
                raise RuntimeError(
                    "quarantined KPI purge aborted: referenced by report_kpi_reference_resolution_revisions.evidence_context_id"
                )

    if _object_exists(connection, "table", "fact_selection_decisions"):
        for chunk in _chunks(fact_ids):
            ph = _placeholders(chunk)
            if (
                connection.exec_driver_sql(
                    f"SELECT 1 FROM fact_selection_decisions "  # nosec B608 -- bound integer placeholder list
                    f"WHERE target_table = 'kpi_facts' AND target_row_id IN ({ph}) LIMIT 1",
                    chunk,
                ).fetchone()
                is not None
            ):
                raise RuntimeError(
                    "quarantined KPI purge aborted: referenced by fact_selection_decisions"
                )

    if _object_exists(connection, "view", "v_legacy_fact_evidence_matches_accepted_current"):
        for chunk in _chunks(fact_ids):
            ph = _placeholders(chunk)
            if (
                connection.exec_driver_sql(
                    f"SELECT 1 FROM v_legacy_fact_evidence_matches_accepted_current "  # nosec B608 -- bound integer placeholder list
                    f"WHERE fact_table = 'kpi_facts' AND fact_row_id IN ({ph}) LIMIT 1",
                    chunk,
                ).fetchone()
                is not None
            ):
                raise RuntimeError(
                    "quarantined KPI purge aborted: referenced by accepted legacy fact evidence match"
                )


def _capture_triggers(connection: Connection) -> list[tuple[str, str]]:
    captured: list[tuple[str, str]] = []
    for name in _DELETE_TRIGGERS:
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is not None and row[0] is not None:
            captured.append((name, str(row[0])))
    return captured


def upgrade() -> None:
    connection = op.get_bind()
    if not (
        _object_exists(connection, "table", "kpi_fact_semantic_contexts")
        and _object_exists(connection, "table", "kpi_facts")
    ):
        return

    fact_ids = [
        int(row[0]) for row in connection.exec_driver_sql(_CANDIDATE_FACT_IDS_SQL).fetchall()
    ]
    if not fact_ids:
        return

    ctx_ids, ctx_ids_by_rev = _load_context_revisions(connection, fact_ids)
    _assert_no_retained_references(connection, fact_ids, ctx_ids)

    captured_triggers = _capture_triggers(connection)
    has_captures = _object_exists(connection, "table", "kpi_legacy_disposition_captures")
    has_attempts = _object_exists(connection, "table", "metric_computation_attempts")

    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("quarantined KPI purge requires the standard Alembic connection")

    connection.exec_driver_sql("SAVEPOINT purge_quarantined_kpi_facts")
    try:
        for trigger_name, _ in captured_triggers:
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{trigger_name}"')

        if has_captures:
            for chunk in _chunks(fact_ids):
                ph = _placeholders(chunk)
                connection.exec_driver_sql(
                    f"DELETE FROM kpi_legacy_disposition_captures WHERE kpi_fact_id IN ({ph})",  # nosec B608 -- bound integer placeholder list
                    chunk,
                )
            for chunk in _chunks(ctx_ids):
                ph = _placeholders(chunk)
                connection.exec_driver_sql(
                    f"DELETE FROM kpi_legacy_disposition_captures "  # nosec B608 -- bound integer placeholder list
                    f"WHERE quarantine_context_id IN ({ph})",
                    chunk,
                )

        for rev in sorted(ctx_ids_by_rev, reverse=True):
            for chunk in _chunks(ctx_ids_by_rev[rev]):
                ph = _placeholders(chunk)
                connection.exec_driver_sql(
                    f"DELETE FROM kpi_fact_semantic_contexts WHERE id IN ({ph})",  # nosec B608 -- bound integer placeholder list
                    chunk,
                )

        # Delete purged kpi_facts BEFORE clearing surviving rows' supersedes_id to
        # NULL: a surviving replacement on the same (ticker, period_end,
        # fiscal_period_type, kpi_definition_id, source_doc_id) would otherwise
        # collide with the quarantined predecessor on uq_kpi_facts_provenance.
        for chunk in _chunks(fact_ids):
            ph = _placeholders(chunk)
            connection.exec_driver_sql(
                f"DELETE FROM kpi_facts WHERE id IN ({ph})",  # nosec B608 -- bound integer placeholder list
                chunk,
            )

        for chunk in _chunks(fact_ids):
            ph = _placeholders(chunk)
            connection.exec_driver_sql(
                f"UPDATE kpi_facts SET supersedes_id = NULL WHERE supersedes_id IN ({ph})",  # nosec B608 -- bound integer placeholder list
                chunk,
            )

        if has_attempts:
            for chunk in _chunks(fact_ids):
                ph = _placeholders(chunk)
                connection.exec_driver_sql(
                    f"UPDATE metric_computation_attempts SET kpi_fact_id = NULL "  # nosec B608 -- bound integer placeholder list
                    f"WHERE kpi_fact_id IN ({ph})",
                    chunk,
                )

        for _, trigger_sql in captured_triggers:
            connection.exec_driver_sql(trigger_sql)

        fk_tables = ["kpi_facts", "kpi_fact_semantic_contexts"]
        if has_captures:
            fk_tables.append("kpi_legacy_disposition_captures")
        for tbl in fk_tables:
            if (
                connection.exec_driver_sql(f"PRAGMA foreign_key_check({tbl})").fetchone()
                is not None
            ):
                raise RuntimeError(f"quarantined KPI purge failed foreign-key check on {tbl}")

        connection.exec_driver_sql("RELEASE SAVEPOINT purge_quarantined_kpi_facts")
    except Exception:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT purge_quarantined_kpi_facts")
        connection.exec_driver_sql("RELEASE SAVEPOINT purge_quarantined_kpi_facts")
        raise


def downgrade() -> None:
    # Schema is unchanged; purged rows are restored from a predecessor DB snapshot.
    pass
