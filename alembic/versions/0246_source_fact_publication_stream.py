"""Add monotonic source-fact publication replay and resolution watermarks.

Revision ID: 0246_source_fact_publication_stream
Revises: 0245_document_processing_research_snapshots
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0246_source_fact_publication_stream"
down_revision: str | Sequence[str] | None = (
    "0245_document_processing_research_snapshots"
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STREAM_ID = "source-fact-publication-stream-v1"


def _hex(column: str) -> str:
    return (
        f"length({column})=64 AND "
        f"{column} NOT GLOB '*[^0-9a-f]*'"
    )


def _append_only(table: str, label: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_append_only_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{label} is append-only'); END"
    )


def upgrade() -> None:
    bind = op.get_bind()
    required = {
        "source_fact_publications",
        "source_fact_publication_seals",
        "canonical_fact_resolution_snapshot_seals",
    }
    missing = sorted(required - set(sa.inspect(bind).get_table_names()))
    if missing:
        raise RuntimeError(
            "source-fact publication stream requires 0241 and 0244: "
            + ", ".join(missing)
        )

    op.create_table(
        "source_fact_publication_stream_clock",
        sa.Column("singleton_key", sa.Integer(), primary_key=True),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "singleton_key = 1 AND next_sequence > 0",
            name="ck_source_fact_publication_stream_clock",
        ),
    )
    op.execute(
        "INSERT INTO source_fact_publication_stream_clock "
        "(singleton_key,next_sequence) VALUES (1,1)"
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_stream_clock_monotonic "
        "BEFORE UPDATE ON source_fact_publication_stream_clock WHEN "
        "NEW.singleton_key <> 1 OR NEW.next_sequence <= OLD.next_sequence "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication stream clock must advance'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_stream_clock_delete "
        "BEFORE DELETE ON source_fact_publication_stream_clock "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication stream clock cannot be deleted'); END"
    )

    op.create_table(
        "source_fact_publication_stream",
        sa.Column(
            "publication_sequence",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("stream_id", sa.String(64), nullable=False),
        sa.Column(
            "publication_id",
            sa.String(128),
            sa.ForeignKey("source_fact_publications.publication_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "publication_seal_id",
            sa.String(128),
            sa.ForeignKey(
                "source_fact_publication_seals.publication_seal_id"
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column("publication_payload_sha256", sa.String(64), nullable=False),
        sa.Column("member_set_sha256", sa.String(64), nullable=False),
        sa.Column("sequence_basis", sa.String(32), nullable=False),
        sa.Column("sealed_at", sa.DateTime(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("event_version", sa.String(64), nullable=False),
        sa.Column("canonical_event_json", sa.Text(), nullable=False),
        sa.Column("event_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "publication_sequence > 0",
            name="ck_source_fact_publication_stream_sequence",
        ),
        sa.CheckConstraint(
            f"stream_id = '{_STREAM_ID}'",
            name="ck_source_fact_publication_stream_id",
        ),
        sa.CheckConstraint(
            "sequence_basis IN ('legacy_backfill','transactional_publish')",
            name="ck_source_fact_publication_stream_basis",
        ),
        sa.CheckConstraint(
            "event_version = 'source_fact_publication_event.v1'",
            name="ck_source_fact_publication_stream_version",
        ),
        sa.CheckConstraint(
            "assigned_at >= sealed_at",
            name="ck_source_fact_publication_stream_clocks",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_event_json) "
            "AND json_type(canonical_event_json) = 'object'",
            name="ck_source_fact_publication_stream_json",
        ),
        sa.CheckConstraint(
            _hex("publication_payload_sha256")
            + " AND "
            + _hex("member_set_sha256")
            + " AND "
            + _hex("event_sha256"),
            name="ck_source_fact_publication_stream_hashes",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_source_fact_publication_stream_sealed",
        "source_fact_publication_stream",
        ["sealed_at", "publication_sequence"],
    )
    op.execute(
        "CREATE TRIGGER trg_source_fact_publication_stream_exact "
        "BEFORE INSERT ON source_fact_publication_stream WHEN "
        "NEW.publication_sequence >= (SELECT next_sequence "
        "FROM source_fact_publication_stream_clock WHERE singleton_key=1) "
        "OR NEW.publication_sequence <= COALESCE(("
        "SELECT MAX(publication_sequence) "
        "FROM source_fact_publication_stream),0) "
        "OR NOT EXISTS ("
        "SELECT 1 FROM source_fact_publications AS publication "
        "JOIN source_fact_publication_seals AS seal "
        "ON seal.publication_id=publication.publication_id "
        "WHERE publication.publication_id=NEW.publication_id "
        "AND seal.publication_seal_id=NEW.publication_seal_id "
        "AND publication.publication_payload_sha256="
        "NEW.publication_payload_sha256 "
        "AND seal.publication_payload_sha256="
        "NEW.publication_payload_sha256 "
        "AND publication.member_set_sha256=NEW.member_set_sha256 "
        "AND seal.member_set_sha256=NEW.member_set_sha256 "
        "AND seal.sealed_at=NEW.sealed_at) "
        "OR NEW.canonical_event_json <> source_fact_publication_event_v1("
        "NEW.stream_id,NEW.publication_sequence,NEW.publication_id,"
        "NEW.publication_seal_id,NEW.publication_payload_sha256,"
        "NEW.member_set_sha256,NEW.sequence_basis,NEW.sealed_at,"
        "NEW.assigned_at) "
        "OR NEW.event_sha256 <> fact_sha256(NEW.canonical_event_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'source-fact publication stream event mismatch'); END"
    )
    _append_only(
        "source_fact_publication_stream",
        "source-fact publication stream event",
    )

    op.create_table(
        "canonical_fact_resolution_snapshot_watermarks",
        sa.Column(
            "resolution_snapshot_id",
            sa.String(128),
            sa.ForeignKey(
                "canonical_fact_resolution_snapshot_seals."
                "resolution_snapshot_id"
            ),
            primary_key=True,
        ),
        sa.Column("idempotency_key", sa.String(256), nullable=False, unique=True),
        sa.Column("stream_id", sa.String(64), nullable=False),
        sa.Column("publication_high_watermark", sa.Integer(), nullable=False),
        sa.Column("high_watermark_event_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("watermark_version", sa.String(64), nullable=False),
        sa.Column("canonical_watermark_json", sa.Text(), nullable=False),
        sa.Column("watermark_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            f"stream_id = '{_STREAM_ID}' "
            "AND publication_high_watermark >= 0",
            name="ck_canonical_resolution_snapshot_watermark_stream",
        ),
        sa.CheckConstraint(
            "recorded_at >= cutoff_at",
            name="ck_canonical_resolution_snapshot_watermark_clocks",
        ),
        sa.CheckConstraint(
            "watermark_version = "
            "'canonical_resolution_snapshot_watermark.v1'",
            name="ck_canonical_resolution_snapshot_watermark_version",
        ),
        sa.CheckConstraint(
            "json_valid(canonical_watermark_json) "
            "AND json_type(canonical_watermark_json) = 'object'",
            name="ck_canonical_resolution_snapshot_watermark_json",
        ),
        sa.CheckConstraint(
            _hex("high_watermark_event_sha256")
            + " AND "
            + _hex("watermark_sha256"),
            name="ck_canonical_resolution_snapshot_watermark_hashes",
        ),
    )
    op.create_index(
        "ix_canonical_resolution_snapshot_watermark_stream",
        "canonical_fact_resolution_snapshot_watermarks",
        ["stream_id", "publication_high_watermark"],
    )
    op.execute(
        "CREATE TRIGGER trg_canonical_resolution_snapshot_watermark_exact "
        "BEFORE INSERT ON canonical_fact_resolution_snapshot_watermarks WHEN "
        "NOT EXISTS ("
        "SELECT 1 FROM canonical_fact_resolution_snapshot_seals AS snapshot "
        "WHERE snapshot.resolution_snapshot_id=NEW.resolution_snapshot_id "
        "AND snapshot.cutoff_at=NEW.cutoff_at "
        "AND snapshot.recorded_at<=NEW.recorded_at) "
        f"OR (NEW.publication_high_watermark=0 AND "
        f"NEW.high_watermark_event_sha256<>'{'0' * 64}') "
        f"OR (NEW.publication_high_watermark>0 AND "
        f"NEW.high_watermark_event_sha256='{'0' * 64}') "
        "OR (NEW.publication_high_watermark>0 AND NOT EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS event "
        "WHERE event.publication_sequence=NEW.publication_high_watermark "
        "AND event.stream_id=NEW.stream_id "
        "AND event.event_sha256=NEW.high_watermark_event_sha256 "
        "AND julianday(event.sealed_at)<=julianday(NEW.cutoff_at))) "
        "OR EXISTS ("
        "SELECT 1 FROM source_fact_publication_stream AS later "
        "WHERE later.stream_id=NEW.stream_id "
        "AND later.publication_sequence>NEW.publication_high_watermark "
        "AND julianday(later.sealed_at)<=julianday(NEW.cutoff_at)) "
        "OR NEW.canonical_watermark_json <> "
        "canonical_resolution_snapshot_watermark_v1("
        "NEW.resolution_snapshot_id,NEW.stream_id,"
        "NEW.publication_high_watermark,"
        "NEW.high_watermark_event_sha256,NEW.cutoff_at,NEW.recorded_at) "
        "OR NEW.watermark_sha256<>fact_sha256(NEW.canonical_watermark_json) "
        "BEGIN SELECT RAISE(ABORT, "
        "'canonical resolution snapshot watermark mismatch'); END"
    )
    _append_only(
        "canonical_fact_resolution_snapshot_watermarks",
        "canonical resolution snapshot watermark",
    )


def downgrade() -> None:
    for table in (
        "canonical_fact_resolution_snapshot_watermarks",
        "source_fact_publication_stream",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_canonical_resolution_snapshot_watermark_exact"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_source_fact_publication_stream_exact"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_source_fact_publication_stream_clock_monotonic"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_source_fact_publication_stream_clock_delete"
    )
    op.drop_index(
        "ix_canonical_resolution_snapshot_watermark_stream",
        table_name="canonical_fact_resolution_snapshot_watermarks",
    )
    op.drop_table("canonical_fact_resolution_snapshot_watermarks")
    op.drop_index(
        "ix_source_fact_publication_stream_sealed",
        table_name="source_fact_publication_stream",
    )
    op.drop_table("source_fact_publication_stream")
    op.drop_table("source_fact_publication_stream_clock")
