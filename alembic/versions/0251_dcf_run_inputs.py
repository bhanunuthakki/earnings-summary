"""Add normalized, immutable DCF input provenance.

Revision ID: 0251_dcf_run_inputs
Revises: 0250_immutable_transcript_versions
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0251_dcf_run_inputs"
down_revision: str | Sequence[str] | None = "0250_immutable_transcript_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dcf_run_inputs"
_UPDATE_TRIGGER = "trg_dcf_run_inputs_immutable"
_DELETE_TRIGGER = "trg_dcf_run_inputs_no_delete"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_rows(detail: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    raw_sources = detail.get("sources")
    if isinstance(raw_sources, list):
        for raw_source in raw_sources:
            if not isinstance(raw_source, Mapping):
                continue
            role = _text(raw_source.get("role"))
            locator = next(
                (
                    value
                    for key in ("path", "locator", "url", "source")
                    if (value := _text(raw_source.get(key))) is not None
                ),
                None,
            )
            if role is None or locator is None:
                continue
            byte_size = raw_source.get("bytes")
            rows.append(
                {
                    "role": role,
                    "locator": locator,
                    "sha256": _text(raw_source.get("sha256")),
                    "byte_size": byte_size if isinstance(byte_size, int) else None,
                    "observed_at": _text(raw_source.get("observed_at")),
                    "detail_json": json.dumps(
                        dict(raw_source),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )

    raw_market = detail.get("market_price")
    if isinstance(raw_market, Mapping) and any(
        raw_market.get(key) is not None for key in ("price", "observed_at", "source")
    ):
        rows.append(
            {
                "role": "market_price",
                "locator": _text(raw_market.get("source")) or "live_market_price",
                "sha256": None,
                "byte_size": None,
                "observed_at": _text(raw_market.get("observed_at")),
                "detail_json": json.dumps(
                    dict(raw_market),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
    return rows


def _backfill(bind: sa.Connection) -> None:
    columns = {str(column["name"]) for column in sa.inspect(bind).get_columns("dcf_runs")}
    if "provenance_json" not in columns:
        return
    existing = bind.execute(
        sa.text(
            "SELECT id, provenance_json FROM dcf_runs "
            "WHERE provenance_json IS NOT NULL AND TRIM(provenance_json) <> ''"
        )
    )
    insert = sa.text(
        "INSERT OR IGNORE INTO dcf_run_inputs "
        "(dcf_run_id, role, locator, sha256, byte_size, observed_at, detail_json) "
        "VALUES (:dcf_run_id, :role, :locator, :sha256, :byte_size, :observed_at, :detail_json)"
    )
    for run_id, raw_detail in existing:
        try:
            detail = json.loads(str(raw_detail))
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        for row in _normalized_rows(detail):
            bind.execute(insert, {"dcf_run_id": int(run_id), **row})


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "dcf_runs" not in tables:
        return
    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "dcf_run_id",
                sa.Integer(),
                sa.ForeignKey("dcf_runs.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("role", sa.String(length=64), nullable=False),
            sa.Column("locator", sa.Text(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=True),
            sa.Column("byte_size", sa.Integer(), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=False),
            sa.Column(
                "recorded_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint("TRIM(role) <> ''", name="ck_dcf_run_inputs_role"),
            sa.CheckConstraint("TRIM(locator) <> ''", name="ck_dcf_run_inputs_locator"),
            sa.CheckConstraint(
                "sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')",
                name="ck_dcf_run_inputs_sha256",
            ),
            sa.CheckConstraint(
                "byte_size IS NULL OR byte_size >= 0",
                name="ck_dcf_run_inputs_byte_size",
            ),
            sa.UniqueConstraint(
                "dcf_run_id",
                "role",
                "locator",
                name="uq_dcf_run_inputs_identity",
            ),
        )
        op.create_index(
            "ix_dcf_run_inputs_run_role",
            _TABLE,
            ["dcf_run_id", "role"],
        )

    _backfill(bind)
    op.execute(
        f"CREATE TRIGGER IF NOT EXISTS {_UPDATE_TRIGGER} "
        f"BEFORE UPDATE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'DCF input provenance is immutable'); END"
    )
    op.execute(
        f"CREATE TRIGGER IF NOT EXISTS {_DELETE_TRIGGER} "
        f"BEFORE DELETE ON {_TABLE} "
        "BEGIN SELECT RAISE(ABORT, 'DCF input provenance cannot be deleted'); END"
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE not in tables:
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_DELETE_TRIGGER}")
    op.drop_index("ix_dcf_run_inputs_run_role", table_name=_TABLE)
    op.drop_table(_TABLE)
