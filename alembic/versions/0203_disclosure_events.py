"""filing_section_items + disclosure_events — the substrate for item-level
change detection (docs/design/disclosure_change_signals.md).

Why two tables, and why item-level at all: the literature is consistent that
ADDITIONS AND REMOVALS OF INDIVIDUAL DISCLOSURE ITEMS carry information that a
document-level or section-level similarity score misses (Lyle, Riedl & Siano,
*The Accounting Review* 98(6), 2023 — both directions of individual
risk-factor change move the variance risk premium; Campbell et al., *RAST* 19,
2014; Kravet & Muslu, *RAST* 18, 2013). Total length is the WEAKEST version of
this measure and is actively misleading on its own, because median 10-K length
roughly doubled 1996-2013 for reasons that have nothing to do with any
individual firm (Dyer, Lang & Stice-Lawrence, *JAE* 64, 2017).

So ``filing_sections`` (migration 0198) is not granular enough to carry the
signal. ``filing_section_items`` splits a stored section into the atomic units
a filer actually adds and drops — one risk factor, one MD&A sub-heading — and
``disclosure_events`` records what changed about them between periods.

``disclosure_events`` is deliberately ONE table across detectors rather than a
table per detector. A discontinued XBRL metric, a removed risk factor and a
dropped section are the same kind of finding from the reader's point of view
("this issuer stopped saying something"), they need the same triage/verdict
workflow, and a single table means a surface or query never has to union three
schemas that drift apart.

``verdict`` exists because "stopped reporting" is NOT reliably bearish. Firms
that stopped quarterly EPS guidance saw -4.8% CAR (Chen, Matsumoto & Rajgopal,
*JAE* 51, 2011), but ~40% of COVID-era guidance stoppers never restarted and
subsequently performed WELL (Call, Melessa & Volant, 2024) — they were only
"stuck" issuing guidance by the penalty for stopping. The column forces a
detector to commit to concealment vs. maturity vs. mechanical rather than
letting the event type imply a direction it has not earned.

No DB-level foreign keys, per the repo's FK-poisoning invariant: ``section_id``
and ``source_doc_id`` are code-validated INTEGER pointers, matching
``filing_sections.doc_id`` (0198) and ``segment_quarterly_coverage`` (0168).

Revision ID: 0203_disclosure_events
Revises: 0202_prompt_ab_arms
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0203_disclosure_events"
down_revision: str | Sequence[str] | None = "0202_prompt_ab_arms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS = "filing_section_items"
_EVENTS = "disclosure_events"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if _ITEMS not in existing:
        op.create_table(
            _ITEMS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            # Code-validated pointer to filing_sections.id — NOT a DB FK.
            sa.Column("section_id", sa.Integer(), nullable=False),
            sa.Column("source_ref", sa.String(length=255), nullable=False),
            sa.Column("form", sa.String(length=8), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=True),
            sa.Column("fiscal_period", sa.String(length=4), nullable=False),
            # The parent section's cross-form concept ('risk_factors', 'mdna').
            sa.Column("canonical_id", sa.String(length=64), nullable=True),
            sa.Column("item_ordinal", sa.Integer(), nullable=False),
            sa.Column("heading", sa.Text(), nullable=True),
            # Normalized heading used as the primary cross-period match key;
            # content similarity is the fallback when headings are reworded.
            sa.Column("match_key", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("body_sha256", sa.String(length=64), nullable=False),
            sa.Column("char_len", sa.Integer(), nullable=False),
            sa.Column("extractor_version", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("section_id", "item_ordinal", name="uq_filing_section_items"),
        )
        op.create_index(
            "ix_filing_section_items_lookup",
            _ITEMS,
            ["ticker", "canonical_id", "fiscal_year", "fiscal_period"],
        )
        op.create_index("ix_filing_section_items_match", _ITEMS, ["ticker", "match_key"])
        op.create_index("ix_filing_section_items_section", _ITEMS, ["section_id"])

    if _EVENTS not in existing:
        op.create_table(
            _EVENTS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            # 'item_added' | 'item_removed' | 'item_reworded' |
            # 'section_added' | 'section_removed' |
            # 'metric_discontinued' | 'metric_relabeled' | 'metric_resumed' |
            # 'metric_standard_transition' (P1, src/filings/metric_lifecycle.py:
            # a tag stopped industry-wide within the same window as >= N other
            # cached tickers -- an ASU/ASC-driven transition, not a company
            # decision; distinct from 'metric_relabeled' because no successor
            # tag is claimed) |
            # P4 transcript longitudinal tracking (docs/design/
            # disclosure_change_build_stack.md P4, src/transcripts/):
            # 'qa_nonanswer_rate_shift'    — deviation of a call's Q&A
            #     non-answer rate from the ~11% baseline (Gow/Larcker/
            #     Zakolyukina, JAR 2021) or from the ticker's own prior call.
            # 'transcript_topic_disappeared' — a KPI mentioned on the last
            #     2+ calls, absent this one. NOVEL/UNVALIDATED — the
            #     literature has no published validation for this specific
            #     measure (nearest analog: guidance withdrawal).
            # 'transcript_tone_shift_abnormal' — a named management
            #     speaker's tone moved beyond what the same-quarter earnings
            #     surprise would predict (Huang/Teoh/Zhang ABTONE-style
            #     proxy — see transcripts.longitudinal.tone_shift_is_abnormal
            #     for exactly what this is NOT: a fitted OLS residual).
            # 'executive_speaker_change' — a management speaker (by name)
            #     appeared/disappeared vs the prior call. NOVEL/UNVALIDATED.
            # 'analyst_roster_change' — material (<25% overlap) turnover in
            #     the set of analysts asking questions vs the prior call.
            #     NOVEL/UNVALIDATED.
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("form", sa.String(length=8), nullable=True),
            sa.Column("fiscal_year", sa.Integer(), nullable=True),
            sa.Column("fiscal_period", sa.String(length=4), nullable=True),
            sa.Column("prior_fiscal_year", sa.Integer(), nullable=True),
            sa.Column("prior_fiscal_period", sa.String(length=4), nullable=True),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            sa.Column("source_doc_id", sa.Integer(), nullable=True),
            # Part of the row identity below, so it is NOT NULL with an empty
            # sentinel rather than nullable: SQLite treats two NULLs in a
            # UNIQUE index as distinct, so a nullable member of the key would
            # silently stop deduplicating exactly the rows that don't carry a
            # section concept (every metric-lifecycle event). Same trap, and
            # same reasoning, as filing_section_coverage's NULL-safe upsert.
            sa.Column("canonical_id", sa.String(length=64), nullable=False, server_default=""),
            # What changed: a risk-factor heading, an XBRL tag, a section stem.
            sa.Column("subject", sa.String(length=255), nullable=False),
            sa.Column("subject_label", sa.Text(), nullable=True),
            sa.Column("prior_excerpt", sa.Text(), nullable=True),
            sa.Column("current_excerpt", sa.Text(), nullable=True),
            # Verbatim quote backing the finding — the receipts bar for any
            # surface that asks the owner to act on it.
            sa.Column("evidence_quote", sa.Text(), nullable=True),
            sa.Column("materiality", sa.Float(), nullable=True),
            # 'concealment' | 'maturity' | 'mechanical' | 'noise' |
            # 'unclassified'. See the module docstring: the event type alone
            # must never imply a direction.
            sa.Column(
                "verdict", sa.String(length=24), nullable=False, server_default="unclassified"
            ),
            sa.Column("interpretation_md", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("detector_version", sa.String(length=32), nullable=False),
            # 'new' | 'reviewed' | 'dismissed' — owner-facing triage state.
            sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            # ``canonical_id`` is part of row identity: the same heading can
            # legitimately appear as an item in two different sections (a
            # phrase occurring in both Risk Factors and MD&A), and those are
            # two distinct findings. Omitting it made them collide and
            # silently overwrite each other — measured at ~32% of computed
            # item events lost before this was caught.
            sa.UniqueConstraint(
                "ticker",
                "event_type",
                "fiscal_year",
                "fiscal_period",
                "canonical_id",
                "subject",
                "detector_version",
                name="uq_disclosure_events",
            ),
        )
        op.create_index(
            "ix_disclosure_events_lookup", _EVENTS, ["ticker", "fiscal_year", "fiscal_period"]
        )
        op.create_index("ix_disclosure_events_type", _EVENTS, ["event_type", "verdict"])
        op.create_index("ix_disclosure_events_status", _EVENTS, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if _EVENTS in existing:
        op.drop_index("ix_disclosure_events_status", table_name=_EVENTS)
        op.drop_index("ix_disclosure_events_type", table_name=_EVENTS)
        op.drop_index("ix_disclosure_events_lookup", table_name=_EVENTS)
        op.drop_table(_EVENTS)
    if _ITEMS in existing:
        op.drop_index("ix_filing_section_items_section", table_name=_ITEMS)
        op.drop_index("ix_filing_section_items_match", table_name=_ITEMS)
        op.drop_index("ix_filing_section_items_lookup", table_name=_ITEMS)
        op.drop_table(_ITEMS)
