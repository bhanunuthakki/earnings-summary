"""decisions.advice_artifact_id — P0.4a Incremental Dollar Recommendation
decision linkage (PRD ``docs/design/personal_investment_partner_prd.md``
§11.5).

PRD §11.5 calls for a decision-level ``source_artifact_id`` pointing at the
governed advice artifact that preceded it. ``decisions.source_artifact_id``
is ALREADY CLAIMED (migration 0046) for a distinct, older provenance chain —
the lens/insight artifact (``lens:five_min_reread`` and friends) that an
advisor-extracted recommendation was parsed out of
(``src/decision_extractor.py``). Reusing that column for the P0.4a advice
artifact would conflate two different provenance chains under one FK-shaped
name. This migration instead adds a SECOND, purpose-built column —
``advice_artifact_id`` — so the PRD's "source_artifact_id" concept is
realized without a collision. Plain nullable ``sa.Integer()``, NO
``sa.ForeignKey`` / REFERENCES / ondelete (the repo-wide FK-poisoning
invariant — see 0086/0110/0130/0137's identical treatment of
``source_artifact_id`` / ``basis_ref_id`` / ``superseded_by_id``); code-level
referential integrity only (``allocation.actions.act_on_recommendation``
checks the artifact row exists before writing the FK-shaped value).

Added via plain ``op.add_column`` (mirrors 0137's ``basis_*`` columns) — a
nullable column with no CHECK and no default requiring a rewrite does not
need SQLite's batch-table-rebuild dance, so no partial-index restore is
needed here.

Recommendation-kind vocabulary (PRD §7.4's ``pass`` | ``watch`` | ``promote``
research dispositions): verified there is NO DB-level CHECK constraint on
``decisions.recommendation_kind`` (it is a plain ``VARCHAR(32)``) — nothing
to widen. The one place kind values drive behavior,
``decision_extractor.reconcile_decision_actions`` /
``decision_extractor._KIND_DIRECTION``, already degrades safely: `.get(kind)`
returns ``None`` for any kind absent from ``_KIND_DIRECTION`` (which only maps
``add``/``initiate``/``trim``/``sell``), so ``pass``/``watch``/``promote``
(and this PR's ``allocate``/``hold``) fall through to the HOLD/AVOID branch
(fill-vs-no-fill inference) rather than ever being mis-mapped to a buy/sell
direction. Confirmed by inspection; no code change required.

``v_decision_journal`` (0179) is rebuilt (its guarded ``_recreate_view``
pattern, verbatim) to surface the new advice link: ``advice_artifact_id``,
``advice_purpose``, ``advice_created_at`` (LEFT JOIN ``llm_artifacts``), and
``advice_preceded`` (1 when the artifact predates the decision, else 0; NULL
when no advice artifact is linked). ``llm_artifacts`` (0035) joins the
existing ``_SOURCE_TABLES`` guard set.

``llm_budgets`` seed for ``incremental_dollar_recommendation``: $10/month,
warn at 80%, ``on_exceed='block'`` — PRD §10.6: a budget-exhausted governed
recommendation is EXPLICIT UNAVAILABILITY (the generator catches
``LLMBudgetExceeded`` itself and returns a labeled deterministic fallback with
``degraded_reasons``), never a silent swap to a cheaper model. Mirrors 0132's
idempotent ``ON CONFLICT DO NOTHING`` seed pattern.

Revision ID: 0188_decisions_advice_artifact
Revises: 0187_wealth_context_snapshot_history
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0188_decisions_advice_artifact"
down_revision: str | Sequence[str] | None = "0187_wealth_context_snapshot_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "incremental_dollar_recommendation"
_BUDGET_CAP = 10.00

_SOURCE_TABLES: tuple[str, ...] = (
    "decisions",
    "advisor_memos",
    "stance_scores",
    "coach_pings",
    "decision_nudges",
    "owner_profile_facts",
    "llm_artifacts",
)

_ADVICE_LOOKBACK_DAYS = 30

_VIEW_SQL = f"""
CREATE VIEW v_decision_journal AS
WITH linked_memo AS (
    SELECT
        d.id AS decision_id,
        m.id AS memo_id,
        m.kind AS memo_kind,
        m.context_json AS memo_context_json,
        m.created_at AS memo_created_at
    FROM decisions d
    JOIN advisor_memos m ON m.id = d.source_memo_id
),
advice_before_ranked AS (
    SELECT
        d.id AS decision_id,
        m.id AS memo_id,
        m.kind AS memo_kind,
        m.context_json AS memo_context_json,
        m.created_at AS memo_created_at,
        ROW_NUMBER() OVER (
            PARTITION BY d.id ORDER BY m.created_at DESC, m.id DESC
        ) AS rn
    FROM decisions d
    JOIN advisor_memos m
        ON m.kind IN ('position_review', 'socratic')
       AND d.ticker IS NOT NULL
       AND UPPER(m.ticker) = UPPER(d.ticker)
       AND m.created_at <= d.made_at
       AND julianday(d.made_at) - julianday(m.created_at) <= {_ADVICE_LOOKBACK_DAYS}
       AND COALESCE(json_extract(m.context_json, '$.source'), '') != 'agent'
),
advice_before AS (
    SELECT decision_id, memo_id, memo_kind, memo_context_json, memo_created_at
    FROM advice_before_ranked WHERE rn = 1
),
coach_ping_ranked AS (
    SELECT
        d.id AS decision_id,
        p.class_ AS ping_class,
        p.status AS ping_status,
        p.created_at AS ping_created_at,
        ROW_NUMBER() OVER (
            PARTITION BY d.id ORDER BY p.created_at DESC, p.id DESC
        ) AS rn
    FROM decisions d
    JOIN coach_pings p
        ON d.ticker IS NOT NULL
       AND p.ticker IS NOT NULL
       AND UPPER(p.ticker) = UPPER(d.ticker)
       AND p.created_at <= d.made_at
       AND julianday(d.made_at) - julianday(p.created_at) <= {_ADVICE_LOOKBACK_DAYS}
),
coach_ping_before AS (
    SELECT decision_id, ping_class, ping_status, ping_created_at
    FROM coach_ping_ranked WHERE rn = 1
),
profile_active AS (
    SELECT
        d.id AS decision_id,
        COUNT(*) AS active_fact_count,
        MAX(f.affirmed_at) AS last_affirmed_at
    FROM decisions d
    JOIN owner_profile_facts f
        ON f.status = 'affirmed'
       AND f.created_at <= d.made_at
       AND (f.superseded_at IS NULL OR f.superseded_at > d.made_at)
    GROUP BY d.id
),
stance_ranked AS (
    SELECT
        s.memo_id,
        s.verdict AS stance_verdict,
        s.horizon_days AS stance_horizon_days,
        s.end_date AS stance_end_date,
        ROW_NUMBER() OVER (
            PARTITION BY s.memo_id ORDER BY s.created_at DESC, s.id DESC
        ) AS rn
    FROM stance_scores s
)
SELECT
    d.id AS decision_id,
    d.ticker AS ticker,
    d.scope AS scope,
    d.decided_by AS decided_by,
    d.recommendation_kind AS recommendation_kind,
    d.recommendation_value AS recommendation_value,
    d.conviction AS conviction,
    d.instrument AS instrument,
    d.account AS account,
    d.size_usd AS size_usd,
    d.size_pct AS size_pct,
    d.falsifier AS falsifier,
    d.made_at AS made_at,
    d.user_action_kind AS user_action_kind,
    d.user_acted_at AS user_acted_at,
    d.outcome_label AS outcome_label,
    d.outcome_pct AS outcome_pct,
    d.outcome_at AS outcome_at,
    d.process_quality AS process_quality,
    lm.memo_id AS linked_memo_id,
    lm.memo_kind AS linked_memo_kind,
    json_extract(lm.memo_context_json, '$.verdict_source') AS linked_memo_verdict_source,
    ab.memo_id AS advice_before_memo_id,
    ab.memo_kind AS advice_before_memo_kind,
    ab.memo_created_at AS advice_before_memo_at,
    json_extract(ab.memo_context_json, '$.verdict_source') AS advice_before_verdict_source,
    CASE
        WHEN json_extract(lm.memo_context_json, '$.verdict_source') = 'guard_override'
          OR json_extract(ab.memo_context_json, '$.verdict_source') = 'guard_override'
        THEN 1 ELSE 0
    END AS guard_override_flag,
    CASE
        WHEN lm.memo_context_json IS NULL AND ab.memo_context_json IS NULL THEN NULL
        WHEN json_extract(COALESCE(lm.memo_context_json, ab.memo_context_json),
                           '$.owner_attested_change') = 1
        THEN 1 ELSE 0
    END AS owner_attested_change,
    cp.ping_class AS coach_ping_class,
    cp.ping_status AS coach_ping_status,
    cp.ping_created_at AS coach_ping_at,
    n.id AS decision_nudge_id,
    n.status AS decision_nudge_status,
    n.sent_at AS decision_nudge_sent_at,
    pa.active_fact_count AS owner_profile_active_fact_count,
    pa.last_affirmed_at AS owner_profile_last_affirmed_at,
    st.stance_verdict AS stance_verdict,
    st.stance_horizon_days AS stance_horizon_days,
    st.stance_end_date AS stance_end_date,
    d.advice_artifact_id AS advice_artifact_id,
    art.purpose AS advice_purpose,
    art.generated_at AS advice_created_at,
    CASE
        WHEN d.advice_artifact_id IS NULL THEN NULL
        WHEN art.generated_at IS NOT NULL AND art.generated_at < d.made_at THEN 1
        ELSE 0
    END AS advice_preceded
FROM decisions d
LEFT JOIN linked_memo lm ON lm.decision_id = d.id
LEFT JOIN advice_before ab ON ab.decision_id = d.id
LEFT JOIN coach_ping_before cp ON cp.decision_id = d.id
LEFT JOIN decision_nudges n ON n.decision_id = d.id
LEFT JOIN profile_active pa ON pa.decision_id = d.id
LEFT JOIN stance_ranked st
    ON st.memo_id = COALESCE(lm.memo_id, ab.memo_id) AND st.rn = 1
LEFT JOIN llm_artifacts art ON art.id = d.advice_artifact_id
"""


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _columns(insp: sa.Inspector, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _recreate_view(insp: sa.Inspector) -> None:
    op.execute("DROP VIEW IF EXISTS v_decision_journal")
    tables = set(insp.get_table_names())
    if all(t in tables for t in _SOURCE_TABLES):
        op.execute(_VIEW_SQL)


def _seed_budget(bind: sa.Connection) -> None:
    insp = sa.inspect(bind)
    if "llm_budgets" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0188 — P0.4a governed Incremental Dollar Recommendation; "
        "block mode (PRD §10.6: budget-exhausted is explicit unavailability, "
        "surfaced via a labeled deterministic fallback, never a silent provider swap)"
    )
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 1, 'block', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    else:  # pre-0066 shape (hand-built fixture DBs)
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 1, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    bind.execute(
        sa.text(sql), {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP, "now": now, "notes": notes}
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if _has_table(insp, "decisions"):
        have = _columns(insp, "decisions")
        if "advice_artifact_id" not in have:
            op.add_column("decisions", sa.Column("advice_artifact_id", sa.Integer(), nullable=True))
        insp = sa.inspect(bind)  # refresh after add_column
        _recreate_view(insp)

    _seed_budget(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # EVERY view referencing `decisions` must be dropped BEFORE any
    # batch_alter_table("decisions") — SQLite's batch rebuild renames the
    # table mid-flight, and a live view referencing it makes that rename fail
    # ("no such table: main.decisions"). v_decision_journal is rebuilt below;
    # v_decision_freshness (0137) is captured verbatim and restored after the
    # batch (found live by #959's merge-head CI, 2026-07-23).
    freshness_sql_row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='view' AND name='v_decision_freshness'")
    ).fetchone()
    op.execute("DROP VIEW IF EXISTS v_decision_journal")
    op.execute("DROP VIEW IF EXISTS v_decision_freshness")

    if _has_table(insp, "decisions"):
        have = _columns(insp, "decisions")
        if "advice_artifact_id" in have:
            linked = bind.execute(
                sa.text("SELECT COUNT(*) FROM decisions WHERE advice_artifact_id IS NOT NULL")
            ).scalar()
            if linked:
                raise RuntimeError(
                    f"cannot downgrade 0188: {linked} decisions row(s) carry an "
                    "advice_artifact_id link — it has no representation in the prior schema"
                )
            with op.batch_alter_table("decisions") as batch:
                batch.drop_column("advice_artifact_id")

        # Restore v_decision_freshness verbatim (its SQL doesn't reference
        # the dropped column — it only needed to be out of the way during
        # the batch rename).
        if freshness_sql_row is not None and freshness_sql_row[0]:
            op.execute(str(freshness_sql_row[0]))

        # Restore the pre-0188 v_decision_journal (0179's shape, minus the
        # advice columns) when its source tables are present.
        insp = sa.inspect(bind)
        prior_sources = (
            "decisions",
            "advisor_memos",
            "stance_scores",
            "coach_pings",
            "decision_nudges",
            "owner_profile_facts",
        )
        tables = set(insp.get_table_names())
        if all(t in tables for t in prior_sources):
            op.execute(_PRIOR_VIEW_SQL)

    if "llm_budgets" in set(insp.get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_PURPOSE},
        )


# 0179's original view body, kept verbatim here so downgrade restores exactly
# what upgrade found (avoids importing another revision module).
_PRIOR_VIEW_SQL = _VIEW_SQL.replace(
    """,
    d.advice_artifact_id AS advice_artifact_id,
    art.purpose AS advice_purpose,
    art.generated_at AS advice_created_at,
    CASE
        WHEN d.advice_artifact_id IS NULL THEN NULL
        WHEN art.generated_at IS NOT NULL AND art.generated_at < d.made_at THEN 1
        ELSE 0
    END AS advice_preceded
FROM decisions d""",
    """
FROM decisions d""",
).replace(
    "LEFT JOIN llm_artifacts art ON art.id = d.advice_artifact_id\n",
    "",
)
