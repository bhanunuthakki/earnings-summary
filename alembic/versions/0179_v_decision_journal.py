"""v_decision_journal -- the unified decision-journal read (tenet-2 Phase 5,
docs/design/tenet2_advisory_program.md §5.1).

One row per ``decisions`` moment, joining -- NULL-tolerant throughout -- the
advice that existed *before* the decision, the owner's disposition, and the
graded outcome. Read-only: no new write paths, no new tables. Every join is a
LEFT JOIN off ``decisions`` so a decision with zero advice machinery around it
(most historical rows predate Phases 1-4) still surfaces with honest NULLs
rather than being silently dropped or fabricated a false footprint.

Column groups:

* **the decision** -- ``decision_id`` (``decisions.id``), ``ticker``, ``scope``,
  ``decided_by``, ``recommendation_kind``, ``recommendation_value``,
  ``conviction``, ``instrument``, ``account``, ``size_usd``, ``size_pct``,
  ``falsifier``, ``made_at``.
* **advice present before** -- two DISTINCT signals, never collapsed into one:
  - ``linked_memo_id``/``linked_memo_kind``/``linked_memo_verdict_source`` --
    the memo that directly PRODUCED this decision via ``decisions.source_memo_id``
    (exact provenance, any memo kind).
  - ``advice_before_memo_id``/``advice_before_memo_kind``/``advice_before_memo_at``
    -- the most recent ``position_review``/``socratic`` memo for the same
    ticker in the ``_ADVICE_LOOKBACK_DAYS``-day window strictly before
    ``made_at`` (proximity, not causation -- may be the same memo as the
    linked one, or a different/no memo). Memos with
    ``context_json.source = 'agent'`` (verification/CI runs -- see
    ``advisor.position_review.AGENT_SOURCE``) are excluded from this signal so
    an automated run never counts as "advice delivered".
  - ``guard_override_flag`` -- 1 when either memo above carries
    ``context_json.verdict_source = 'guard_override'``.
  - ``owner_attested_change`` -- the owner's explicit attestation
    (``context_json.owner_attested_change``) off whichever memo is present;
    NULL (not 0) when no memo is linked at all -- "no attestation" and
    "nothing to attest" are different facts.
  - ``coach_ping_class``/``coach_ping_status``/``coach_ping_at`` -- the most
    recent ``coach_pings`` row for the same ticker in the same lookback window
    before ``made_at``.
  - ``decision_nudge_id``/``decision_nudge_status``/``decision_nudge_sent_at``
    -- ``decision_nudges`` is 1:1 with a specific decision (UNIQUE
    ``decision_id``), so this is a direct join, not a window.
  - ``owner_profile_active_fact_count``/``owner_profile_last_affirmed_at`` --
    point-in-time read over ``owner_profile_facts``: how many AFFIRMED facts
    were active (``created_at <= made_at`` and not yet superseded) at decision
    time, and the latest of their affirmations. There is no single "profile
    version" integer (0159 is append-and-supersede, not version-numbered), so
    this is the honest point-in-time substitute.
* **disposition** -- ``user_action_kind``, ``user_acted_at``.
* **outcome** -- ``outcome_label``, ``outcome_pct``, ``outcome_at``,
  ``process_quality``, plus the linked/advice-before memo's latest
  ``stance_scores`` verdict (``stance_verdict``, ``stance_horizon_days``,
  ``stance_end_date``) when that memo has been graded.

``_ADVICE_LOOKBACK_DAYS = 30`` mirrors the Coach P&L's own
``_COACH_CHANGE_WINDOW_DAYS`` (``pipeline.allocation_decisions_panel``) --
the one existing "how long does advice stay live" constant in the codebase;
reused here rather than inventing a second window.

Guard pattern (0143/0152, verbatim): SQLite validates every view in the schema
on any ``batch_alter_table`` elsewhere in the chain, so the view is created
only when ALL six source tables exist (``DROP VIEW IF EXISTS`` first keeps
upgrade idempotent); a DB stamped before 0159 (owner_profile_facts) or 0149
(decision_nudges) simply doesn't get the view yet, which is correct -- there's
nothing for it to read.

Revision ID: 0179_v_decision_journal
Revises: 0178_comp_set_metrics_locator
Create Date: 2026-07-18

Note (alembic renumbering-at-rebase, tenet2_advisory_program.md §6.4):
repointed at rebase (2026-07-18) — the parallel comparable-sets/metrics-engine
wave landed ``0178_comp_set_metrics_locator`` (also chained off 0174) before
this PR merged. This program (tenet-2 Phase 5, wave E) keeps its claimed
number 0179; ``down_revision`` now repoints to the actual head (0178).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0179_v_decision_journal"
down_revision: str | Sequence[str] | None = "0178_comp_set_metrics_locator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_TABLES: tuple[str, ...] = (
    "decisions",
    "advisor_memos",
    "stance_scores",
    "coach_pings",
    "decision_nudges",
    "owner_profile_facts",
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
    st.stance_end_date AS stance_end_date
FROM decisions d
LEFT JOIN linked_memo lm ON lm.decision_id = d.id
LEFT JOIN advice_before ab ON ab.decision_id = d.id
LEFT JOIN coach_ping_before cp ON cp.decision_id = d.id
LEFT JOIN decision_nudges n ON n.decision_id = d.id
LEFT JOIN profile_active pa ON pa.decision_id = d.id
LEFT JOIN stance_ranked st
    ON st.memo_id = COALESCE(lm.memo_id, ab.memo_id) AND st.rn = 1
"""


def _recreate_view() -> None:
    op.execute("DROP VIEW IF EXISTS v_decision_journal")
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if all(t in tables for t in _SOURCE_TABLES):
        op.execute(_VIEW_SQL)


def upgrade() -> None:
    _recreate_view()


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_decision_journal")
