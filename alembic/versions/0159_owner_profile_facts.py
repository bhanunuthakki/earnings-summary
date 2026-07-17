"""owner_profile_facts — the canonical, versioned owner-context substrate
(tenet-2 Phase 1, docs/design/tenet2_advisory_program.md §3.2/§7 decision 2).

Household/appetite/behavioral facts about the owner (tax-bucket balances, cash
buffer, glide posture, dated life events, human-capital bucket caps, ...) live
scattered across two sibling repos' gitignored files today (wealthplan's
``plan.local.json``, the tracker's ``CIO_CONTEXT.local.md``) — earnings-summary
itself has no owner-profile object. This table becomes that canonical home;
the sibling files remain their own operational inputs, imported one-way
(``execution/import_owner_capacity.py``), never synced back.

Append-and-supersede, modeled EXACTLY on the ``positioning_intents`` 0145
precedent (``model_provenance/versioning.py``): every save is a new row; the
prior latest row for the same ``(user_id, category, key)`` flips
``is_latest=0`` with ``superseded_at``/``superseded_by_id`` stamped (no
REFERENCES — the repo-wide FK-poisoning invariant). A partial unique index
keeps exactly one authoritative row per fact.

Gated assertion (§7.1, owner ruling 2026-07-17): an imported or derived fact
always lands ``status='proposed'`` — nothing becomes ``'affirmed'`` without an
explicit owner action (the packet-walk ratification card). ``owner``-provenance
rows (the owner typing something directly) may land straight at ``'affirmed'``
via the store, same as ``positioning_intents``/Tenets treat owner-authored rows
as immediately authoritative.

Revision ID: 0159_owner_profile_facts
Revises: 0152_v_thesis_status_stub_substring
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0159_owner_profile_facts"
down_revision: str | Sequence[str] | None = "0152_v_thesis_status_stub_substring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "owner_profile_facts"
_INDEX = "ux_owner_profile_facts_latest"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if _TABLE not in insp.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Text(), nullable=False, server_default="bhanu"),
            # 'capacity' (Tier A, imported) | 'appetite' (Tier B, owner-authored,
            # references positioning_intents where a fact is already representable
            # there) | 'behavioral' (Tier C, derived from graded decisions).
            sa.Column("category", sa.Text(), nullable=False),
            # Stable slug within (user_id, category) — e.g.
            # 'tax_bucket_balances', 'human_capital.big_tech_ads',
            # 'life_event.baby_2031_01_01'. The versioning key alongside category.
            sa.Column("key", sa.Text(), nullable=False),
            # The typed payload (a JSON object), validated by a Pydantic model at
            # the producer boundary (the importer or the ratification route)
            # before every write — never hand-built dict->str.
            sa.Column("value_json", sa.Text(), nullable=False),
            # Plain-English restatement of value_json — what the packet-walk card
            # and any future anchor slot quote back at the owner.
            sa.Column("narrative", sa.Text(), nullable=False),
            sa.Column(
                "provenance",
                sa.Text(),
                nullable=False,
                # 'wealthplan_import' | 'cio_context_import' = a snapshot from a
                # sibling repo's file; 'owner' = the owner stated it directly
                # (may land 'affirmed' immediately); 'derived' = distilled from
                # graded decisions (Tier C, Phase 4) — always lands 'proposed'.
            ),
            sa.Column("status", sa.Text(), nullable=False, server_default="proposed"),
            sa.Column("affirmed_at", sa.Text(), nullable=True),
            # Per-category review cadence (§3.3): capacity ~quarterly (90),
            # appetite on a pledge/drawdown trigger (no fixed days -> NULL),
            # behavioral after each grading batch (NULL). NULL = no automatic
            # staleness reminder for this fact; the owner-ratified value stands
            # until a new import/derivation supersedes it.
            sa.Column("review_horizon_days", sa.Integer(), nullable=True),
            # Free-text: source file + as-of marker (e.g.
            # "wealthplan/data/plan.local.json as_of=2026-06-21") — never a
            # value, just where this snapshot came from, for the federation
            # provenance trail.
            sa.Column("source_detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),  # naive-UTC ISO
            sa.Column("is_latest", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("superseded_at", sa.Text(), nullable=True),
            sa.Column("superseded_by_id", sa.Integer(), nullable=True),
            sa.CheckConstraint(
                "category IN ('capacity','appetite','behavioral')",
                name="ck_owner_profile_facts_category",
            ),
            sa.CheckConstraint(
                "provenance IN ('wealthplan_import','cio_context_import','owner','derived')",
                name="ck_owner_profile_facts_provenance",
            ),
            sa.CheckConstraint(
                "status IN ('proposed','affirmed','rejected')",
                name="ck_owner_profile_facts_status",
            ),
        )
        op.execute(
            f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE}(user_id, category, key) WHERE is_latest = 1"
        )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
