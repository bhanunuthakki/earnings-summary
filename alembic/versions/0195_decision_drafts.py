"""decision_drafts — the typed async parse of a landed capture into a
confirmable Owner Decision (Personal Investment Partner PRD
``docs/design/personal_investment_partner_prd.md`` §9.2/§11.5, P2.1).

Free-form text/voice captures land deterministically through the existing
``capture.ingest`` path (never LLM-gated); a SEPARATE fire-and-forget tap
(``capture.decision_draft.parse_note``, purpose ``decision_draft_parse``)
classifies the landed note's intent and — for anything more consequential
than an ordinary musing — writes one ``decision_drafts`` row the owner
confirms/corrects/dismisses from the mobile Inbox, Telegram, or desktop.
Confirming/correcting is the ONLY path that creates/updates an Owner
Decision (``decisions.decided_by = 'owner'``).

Schema, verbatim per PRD §11.5:
  * ``id``, ``user_id``, ``source_note_id`` (analyst_notes.id, plain INTEGER —
    no REFERENCES, the repo-wide FK-poisoning invariant established by 0086/
    0110/0130/0137/0188's identical treatment of source_artifact_id /
    basis_ref_id / superseded_by_id / advice_artifact_id: code-level
    referential integrity only, so a note purge never orphans a live draft
    row via a cascading FK). ``source_channel`` / ``source_external_id``
    identify the inbound capture (telegram / web / tracker + its native id);
    ``idempotency_key`` is UNIQUE so a reprocessed source (a re-run tap, a
    re-scanned tracker fill) never duplicates a draft.
  * ``original_text`` is NEVER overwritten (PRD: "original text never
    overwritten" / "a parse failure leaves the original capture intact") —
    correction only ever touches the separate ``draft_json`` proposed-fields
    blob.
  * ``transcription_json`` / ``draft_json`` are NULL-or-``json_valid``.
  * ``parse_confidence`` is NULL or in [0,1].
  * ``status`` is closed under the PRD's state machine: ``captured ->
    parsed -> awaiting_confirmation -> confirmed|corrected|dismissed|
    expired|parse_failed``.
  * ``ck_decision_drafts_decision_required`` — a confirmed/corrected row MUST
    carry a ``decision_id`` (the PRD's own constraint: "a confirmed/corrected
    row has a decision_id").
  * ``prompt_version`` / ``model`` / ``llm_call_id`` are the run provenance
    (llm_call_id plain INTEGER, same no-REFERENCES convention as
    ``decision_drafts.decision_id`` and every other cross-table pointer in
    this migration).
  * "ON DELETE SET NULL" per §11.5 is a CODE-LEVEL contract (there is no FK
    to attach a literal ON DELETE clause to, matching every other
    plain-int provenance pointer in this schema) — a note/llm_calls row is
    never hard-deleted by any code path today, so this is a forward
    guarantee, not a currently-exercised cascade.

``v_decision_journal`` (0179, extended by 0188 for the advice-artifact link)
is rebuilt again here to surface the Decision Draft/source-note provenance
the PRD's §11.5 view spec calls for: ``source_draft_id``,
``source_draft_channel``, ``source_note_id`` — a LEFT JOIN keyed on
``decision_drafts.decision_id`` (rn=1 on newest draft per decision, since
nothing prevents — however unlikely — two drafts eventually resolving to the
same decision id). The view SQL otherwise stays byte-identical to 0188's.

``llm_budgets`` seed for ``decision_draft_parse``: $8/month, warn at 80%,
``on_exceed='skip'`` — a per-item degrade (the tap already swallows every
exception; see the poller tap docstring), never a block that would need an
operator to notice before captures keep landing.

Revision ID: 0195_decision_drafts
Revises: 0194_research_triage
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0195_decision_drafts"
down_revision: str | Sequence[str] | None = "0194_research_triage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_PURPOSE = "decision_draft_parse"
_BUDGET_CAP_USD = 8.00

_STATUSES = (
    "captured",
    "parsed",
    "awaiting_confirmation",
    "confirmed",
    "corrected",
    "dismissed",
    "expired",
    "parse_failed",
)
_STATUS_CHECK = "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")"
_DECISION_REQUIRED_CHECK = "status NOT IN ('confirmed', 'corrected') OR decision_id IS NOT NULL"
_CONFIDENCE_CHECK = "parse_confidence IS NULL OR (parse_confidence >= 0 AND parse_confidence <= 1)"

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

# Byte-identical to 0188's _VIEW_SQL, plus a `draft_rank` CTE + the three new
# trailing SELECT columns + LEFT JOIN. Kept as one literal (not a string
# .replace() off an imported prior revision) so this file reads standalone,
# matching every other view-rebuild migration's own convention in this repo.
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
),
draft_ranked AS (
    SELECT
        dd.decision_id AS decision_id,
        dd.id AS draft_id,
        dd.source_channel AS draft_channel,
        dd.source_note_id AS draft_note_id,
        ROW_NUMBER() OVER (
            PARTITION BY dd.decision_id ORDER BY dd.id DESC
        ) AS rn
    FROM decision_drafts dd
    WHERE dd.decision_id IS NOT NULL
),
draft_for_decision AS (
    SELECT decision_id, draft_id, draft_channel, draft_note_id
    FROM draft_ranked WHERE rn = 1
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
    END AS advice_preceded,
    dfd.draft_id AS source_draft_id,
    dfd.draft_channel AS source_draft_channel,
    dfd.draft_note_id AS source_note_id
FROM decisions d
LEFT JOIN linked_memo lm ON lm.decision_id = d.id
LEFT JOIN advice_before ab ON ab.decision_id = d.id
LEFT JOIN coach_ping_before cp ON cp.decision_id = d.id
LEFT JOIN decision_nudges n ON n.decision_id = d.id
LEFT JOIN profile_active pa ON pa.decision_id = d.id
LEFT JOIN stance_ranked st
    ON st.memo_id = COALESCE(lm.memo_id, ab.memo_id) AND st.rn = 1
LEFT JOIN llm_artifacts art ON art.id = d.advice_artifact_id
LEFT JOIN draft_for_decision dfd ON dfd.decision_id = d.id
"""


def _has_table(insp: sa.Inspector, name: str) -> bool:
    return name in insp.get_table_names()


def _recreate_view(insp: sa.Inspector) -> None:
    op.execute("DROP VIEW IF EXISTS v_decision_journal")
    tables = set(insp.get_table_names())
    if all(t in tables for t in _SOURCE_TABLES) and "decision_drafts" in tables:
        op.execute(_VIEW_SQL)


def _seed_budget(bind: sa.Connection) -> None:
    insp = sa.inspect(bind)
    if "llm_budgets" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("llm_budgets")}
    now = datetime.now(UTC).isoformat()
    notes = (
        "seeded by migration 0195 — P2.1 Decision Draft parse (async tap off the "
        "landed capture); skip mode — a blown cap must degrade the tap to a plain "
        "landed note, never block ordinary capture (PRD §9.2)."
    )
    if "on_exceed" in cols:
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 on_exceed, created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, 'skip', :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    else:  # pre-0066 shape (hand-built fixture DBs)
        sql = """
            INSERT INTO llm_budgets
                (purpose, monthly_cap_usd, warn_threshold_pct, hard_block,
                 created_at, updated_at, notes)
            VALUES (:purpose, :cap, 0.80, 0, :now, :now, :notes)
            ON CONFLICT(purpose) DO NOTHING
            """
    bind.execute(
        sa.text(sql),
        {"purpose": _BUDGET_PURPOSE, "cap": _BUDGET_CAP_USD, "now": now, "notes": notes},
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if "decision_drafts" not in set(insp.get_table_names()):
        op.create_table(
            "decision_drafts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Text(), nullable=False, server_default=sa.text("'bhanu'")),
            sa.Column("source_note_id", sa.Integer(), nullable=True),
            sa.Column("source_channel", sa.Text(), nullable=False),
            sa.Column("source_external_id", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.Text(), nullable=False),
            sa.Column("original_text", sa.Text(), nullable=False),
            sa.Column("transcription_json", sa.Text(), nullable=True),
            sa.Column("draft_json", sa.Text(), nullable=True),
            sa.Column("parse_confidence", sa.Float(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("prompt_version", sa.Text(), nullable=True),
            sa.Column("model", sa.Text(), nullable=True),
            sa.Column("llm_call_id", sa.Integer(), nullable=True),
            sa.Column("decision_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.Column("confirmed_at", sa.Text(), nullable=True),
            sa.Column("dismissed_at", sa.Text(), nullable=True),
            sa.CheckConstraint(_STATUS_CHECK, name="ck_decision_drafts_status"),
            sa.CheckConstraint(
                "transcription_json IS NULL OR json_valid(transcription_json)",
                name="ck_decision_drafts_transcription_json_valid",
            ),
            sa.CheckConstraint(
                "draft_json IS NULL OR json_valid(draft_json)",
                name="ck_decision_drafts_draft_json_valid",
            ),
            sa.CheckConstraint(_CONFIDENCE_CHECK, name="ck_decision_drafts_confidence_range"),
            sa.CheckConstraint(
                _DECISION_REQUIRED_CHECK, name="ck_decision_drafts_decision_required"
            ),
            sa.UniqueConstraint("idempotency_key", name="uq_decision_drafts_idempotency_key"),
        )
        op.create_index("ix_decision_drafts_source_note", "decision_drafts", ["source_note_id"])
        op.create_index("ix_decision_drafts_status", "decision_drafts", ["status"])
        op.create_index("ix_decision_drafts_decision", "decision_drafts", ["decision_id"])

    insp = sa.inspect(bind)  # refresh after create_table
    _recreate_view(insp)
    _seed_budget(bind)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # v_decision_journal references decisions AND decision_drafts — drop it
    # before any batch rebuild / the table drop (0188's documented gotcha:
    # a live view referencing a table blocks SQLite's rename-based rebuild).
    op.execute("DROP VIEW IF EXISTS v_decision_journal")

    if _has_table(insp, "decision_drafts"):
        # Guard on `decisions` too: mid-chain-stamped test fixtures (0069/
        # 0074/0119 round-trips) never created it, and the linked-count JOIN
        # would raise "no such table: decisions" (found in P2.1's full-suite
        # run). No decisions table => nothing can be linked.
        linked = (
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM decisions d "
                    "JOIN decision_drafts dd ON dd.decision_id = d.id"
                )
            ).scalar()
            if _has_table(insp, "decisions")
            else 0
        )
        if linked:
            raise RuntimeError(
                f"cannot downgrade 0195: {linked} decisions row(s) are linked from a "
                "decision_drafts row — they have no representation in the prior schema"
            )
        op.drop_table("decision_drafts")

    # Restore the pre-0195 (0188) v_decision_journal shape when its source
    # tables are present — verbatim, not derived, so downgrade is exact.
    insp = sa.inspect(bind)
    prior_sources = (
        "decisions",
        "advisor_memos",
        "stance_scores",
        "coach_pings",
        "decision_nudges",
        "owner_profile_facts",
        "llm_artifacts",
    )
    tables = set(insp.get_table_names())
    if all(t in tables for t in prior_sources):
        op.execute(_PRIOR_VIEW_SQL)

    if "llm_budgets" in set(insp.get_table_names()):
        bind.execute(
            sa.text("DELETE FROM llm_budgets WHERE purpose = :purpose"),
            {"purpose": _BUDGET_PURPOSE},
        )


# 0188's view body, kept verbatim here (minus this migration's draft_ranked
# CTE + trailing three columns) so downgrade restores exactly what upgrade
# found, without importing another revision module.
_PRIOR_VIEW_SQL = (
    _VIEW_SQL.replace(
        """,
draft_ranked AS (
    SELECT
        dd.decision_id AS decision_id,
        dd.id AS draft_id,
        dd.source_channel AS draft_channel,
        dd.source_note_id AS draft_note_id,
        ROW_NUMBER() OVER (
            PARTITION BY dd.decision_id ORDER BY dd.id DESC
        ) AS rn
    FROM decision_drafts dd
    WHERE dd.decision_id IS NOT NULL
),
draft_for_decision AS (
    SELECT decision_id, draft_id, draft_channel, draft_note_id
    FROM draft_ranked WHERE rn = 1
)
SELECT""",
        """
SELECT""",
    )
    .replace(
        """,
    dfd.draft_id AS source_draft_id,
    dfd.draft_channel AS source_draft_channel,
    dfd.draft_note_id AS source_note_id
FROM decisions d""",
        """
FROM decisions d""",
    )
    .replace(
        "LEFT JOIN draft_for_decision dfd ON dfd.decision_id = d.id\n",
        "",
    )
)
