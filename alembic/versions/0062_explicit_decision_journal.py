"""Use the explicit decision series without retired coaching/history joins.

No decisions or profile inputs are deleted. Recovery of the previous product
view uses its reviewed predecessor code/database snapshot, not a partial downgrade.
"""

from __future__ import annotations

import hashlib

from alembic import op

revision = "0062_explicit_decision_journal"
down_revision = "0061_bulk_lexical_projection_coverage"
branch_labels = None
depends_on = None

_PREDECESSOR_SHA256 = "75ce0d007b246b9f4f1ab93dbe8c9074f76d68fdafdd7baf44f216da1056f24e"  # pragma: allowlist secret -- schema SQL digest, not a credential
_RETAINED_SQL = "CREATE VIEW v_decision_journal AS\nSELECT\n    d.id AS decision_id,\n    d.ticker AS ticker,\n    d.scope AS scope,\n    d.decided_by AS decided_by,\n    d.recommendation_kind AS recommendation_kind,\n    d.recommendation_value AS recommendation_value,\n    d.conviction AS conviction,\n    d.instrument AS instrument,\n    d.account AS account,\n    d.size_usd AS size_usd,\n    d.size_pct AS size_pct,\n    d.falsifier AS falsifier,\n    d.made_at AS made_at,\n    d.user_action_kind AS user_action_kind,\n    d.user_acted_at AS user_acted_at,\n    d.outcome_label AS outcome_label,\n    d.outcome_pct AS outcome_pct,\n    d.outcome_at AS outcome_at,\n    d.process_quality AS process_quality,\n    d.advice_artifact_id AS advice_artifact_id\nFROM decisions d"


def _normalized(sql: str) -> str:
    return " ".join(
        sql.replace("CREATE VIEW IF NOT EXISTS ", "CREATE VIEW ", 1)
        .strip()
        .removesuffix(";")
        .split()
    )


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_decision_journal'"
    ).fetchone()
    if row is None:
        raise RuntimeError("decision journal view is missing; refusing replacement")
    current = _normalized(str(row[0]))
    if current == _normalized(_RETAINED_SQL):
        return
    if hashlib.sha256(current.encode()).hexdigest() != _PREDECESSOR_SHA256:
        raise RuntimeError("decision journal view differs from its predecessor owner")
    with bind.begin_nested():
        op.execute("DROP VIEW v_decision_journal")
        op.execute(_RETAINED_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Restore the reviewed predecessor code/database snapshot to recover retired product joins"
    )
