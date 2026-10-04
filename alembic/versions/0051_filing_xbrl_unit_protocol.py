"""Admit v2 measure units through the existing exact filing-XBRL seal.

Revision ID: 0051_filing_xbrl_unit_protocol
Revises: 0050_kpi_fact_supersedes_lookup_index
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "0051_filing_xbrl_unit_protocol"
down_revision = "0050_kpi_fact_supersedes_lookup_index"
branch_labels = None
depends_on = None

_TRIGGER = "trg_filing_xbrl_input_seal_exact"
_V1_PROTOCOL = "artifact.bridge_protocol_version='filing-xbrl-bridge.v1'"
# Frozen migration contract. Application consumers must preserve its proof.
_V2_PROTOCOL = (
    "(CASE WHEN artifact.bridge_protocol_version='filing-xbrl-bridge.v1' THEN 1 "
    "WHEN artifact.bridge_protocol_version='filing-xbrl-bridge.v2' "
    "AND json_valid(artifact.canonical_manifest_json) THEN COALESCE((json_extract(artifact.canonical_manifest_json,'$.bridge_protocol_version')="
    "artifact.bridge_protocol_version "
    "AND json_type(artifact.canonical_manifest_json,'$.execution.runtime_members')='array' "
    "AND json_type(artifact.canonical_manifest_json,'$.build_provenance.unit_source_sha256')='text' "
    "AND length(json_extract(artifact.canonical_manifest_json,"
    "'$.build_provenance.unit_source_sha256'))=64 "
    "AND json_extract(artifact.canonical_manifest_json,'$.build_provenance.unit_source_sha256') "
    "NOT GLOB '*[^0-9a-f]*' "
    "AND (SELECT COUNT(*) FROM json_each(artifact.canonical_manifest_json,"
    "'$.execution.runtime_members') unit_member "
    "WHERE json_extract(CASE WHEN unit_member.type='object' THEN unit_member.value ELSE '{}' END,'$.relative_path')='earnings_summary_xbrl_units.py')=1 "
    "AND EXISTS (SELECT 1 FROM json_each(artifact.canonical_manifest_json,"
    "'$.execution.runtime_members') unit_member "
    "WHERE json_extract(CASE WHEN unit_member.type='object' THEN unit_member.value ELSE '{}' END,'$.relative_path')='earnings_summary_xbrl_units.py' "
    "AND json_extract(CASE WHEN unit_member.type='object' THEN unit_member.value ELSE '{}' END,'$.blob_sha256')="
    "json_extract(artifact.canonical_manifest_json,'$.build_provenance.unit_source_sha256') "
    "AND json_type(CASE WHEN unit_member.type='object' THEN unit_member.value ELSE '{}' END,'$.byte_size')='integer' "
    "AND json_extract(CASE WHEN unit_member.type='object' THEN unit_member.value ELSE '{}' END,'$.byte_size')>=0)),0) ELSE 0 END)"
)


def _rewrite(*, upgrade_protocol: bool) -> None:
    bind = op.get_bind()
    if (
        not upgrade_protocol
        and bind.execute(
            text(
                "SELECT 1 FROM filing_xbrl_processor_artifacts "
                "WHERE bridge_protocol_version='filing-xbrl-bridge.v2' LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError("cannot remove the v2 contract while v2 artifacts are retained")
    row = bind.execute(
        text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
        {"name": _TRIGGER},
    ).scalar_one_or_none()
    if not isinstance(row, str):
        raise RuntimeError("required filing-XBRL input seal trigger is absent")
    target = _V1_PROTOCOL if upgrade_protocol else _V2_PROTOCOL
    replacement = _V2_PROTOCOL if upgrade_protocol else _V1_PROTOCOL
    if row.count(target) != 1:
        raise RuntimeError("filing-XBRL input seal protocol guard differs from its predecessor")
    rewritten = row.replace(target, replacement, 1)
    bind.exec_driver_sql("DROP TRIGGER trg_filing_xbrl_input_seal_exact")
    bind.exec_driver_sql(rewritten)


def upgrade() -> None:
    _rewrite(upgrade_protocol=True)


def downgrade() -> None:
    _rewrite(upgrade_protocol=False)
