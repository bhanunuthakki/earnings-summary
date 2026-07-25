"""filing_sections + filing_section_coverage — the durable section-partition
store behind longitudinal reporting-language tracking (docs/design/
filing_longitudinal_language.md §3, Phase 0).

Two tables, because the audit found the platform has TWO complementary and
independently-available partitions of the same filings and no place to put
either:

  * ``edgar_text`` — narrative Items split out of the EDGAR primary document
    (10-K Items 1..16, 10-Q Part I/II items, 20-F Items 1..19, free-form 6-K
    exhibit headings). Exhaustive; carries MD&A and Risk Factors, which FMP
    structurally does not.
  * ``fmp_rfile`` — the filer's own XBRL R-file sections out of the cached
    ``financial-reports-json`` payloads (statements + footnotes + Tables /
    Details blocks). Pre-partitioned by the filer, but keys truncate at ~31
    chars with order-dependent ``_N`` suffixes, so cross-period joins must run
    on the normalized stem, never the raw key — hence ``section_stem`` being a
    stored, indexed column rather than a query-time expression.

``filing_sections`` holds the partitions; ``filing_section_coverage`` records
one row per (ticker, source, form, fiscal period) attempt INCLUDING the
failures, so "we have no FY2025 EDGAR sections for NU" is a queryable fact
with a reason code rather than an absence indistinguishable from "never
looked". This is the same never-a-silent-skip bar migration 0168 set for
``segment_quarterly_coverage``, and it is what makes single-source periods
safe to consume: a reader can always tell whether the other source was
missing, blocked, or simply never attempted.

Deliberately NOT database-level foreign keys: ``doc_id`` is a plain INTEGER
pointer to ``documents.id``, code-validated at write time, per this repo's
FK-poisoning invariant (reference_platform_invariants.md) and the precedent
set by ``segment_quarterly_coverage.source_doc_id`` (0168) and
``segment_dimensions.supersedes_id`` (0165).

Row identity:
  * ``filing_sections`` — UNIQUE (source, source_ref, section_key_raw, ordinal).
    ``source_ref`` is the accession number for ``edgar_text`` and the
    repo-relative cached-file path for ``fmp_rfile``: the natural per-document
    key in each partition. ``ordinal`` participates because a filing legitimately
    repeats a raw key (FMP's ``_N`` disambiguators collide once truncated).
  * ``filing_section_coverage`` — UNIQUE (ticker, source, form, fiscal_year,
    fiscal_period, extractor_version). One verdict per period per source per
    extractor version; re-running upgrades the verdict in place.

Both tables are brand-new ``op.create_table`` (no ``batch_alter_table``), so
the 0057-trigger-drop hazard 0165/0166 worked around does not apply.

Revision ID: 0198_filing_sections
Revises: 0197_decision_drafts
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0198_filing_sections"
down_revision: str | Sequence[str] | None = "0197_decision_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SECTIONS = "filing_sections"
_COVERAGE = "filing_section_coverage"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if _SECTIONS not in existing:
        op.create_table(
            _SECTIONS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            # 'edgar_text' | 'fmp_rfile'
            sa.Column("source", sa.String(length=16), nullable=False),
            # Accession number (edgar_text) or repo-relative file path (fmp_rfile).
            sa.Column("source_ref", sa.String(length=255), nullable=False),
            # Code-validated pointer to documents.id — NOT a DB-level FK.
            sa.Column("doc_id", sa.Integer(), nullable=True),
            sa.Column("accession_number", sa.String(length=32), nullable=True),
            # '10-K' | '10-Q' | '20-F' | '40-F' | '6-K' | 'S-1'
            sa.Column("form", sa.String(length=8), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=True),
            # 'FY' | 'Q1' | 'Q2' | 'Q3' | 'Q4'
            sa.Column("fiscal_period", sa.String(length=4), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=True),
            sa.Column("filing_date", sa.String(length=10), nullable=True),
            # The section heading exactly as the source presented it.
            sa.Column("section_key_raw", sa.String(length=255), nullable=False),
            # Normalized for cross-period matching: lowercased, _N disambiguator
            # and (Tables)/(Details) qualifiers stripped, whitespace collapsed.
            sa.Column("section_stem", sa.String(length=255), nullable=False),
            # Taxonomy id when the section resolves to a mandated item
            # ('item_1a', 'item_7', '20f_item_3d', ...); NULL for free-form
            # 6-K headings and unmapped FMP note sections.
            sa.Column("canonical_id", sa.String(length=64), nullable=True),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("text_sha256", sa.String(length=64), nullable=False),
            sa.Column("char_len", sa.Integer(), nullable=False),
            # 1 when the source key hit FMP's ~31-char cap (so a stem match is
            # prefix-only and a consumer should weight content similarity higher).
            sa.Column("key_truncated", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("extractor_version", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "source",
                "source_ref",
                "section_key_raw",
                "ordinal",
                name="uq_filing_sections_key",
            ),
        )
        op.create_index(
            "ix_filing_sections_ticker_period",
            _SECTIONS,
            ["ticker", "form", "fiscal_year", "fiscal_period"],
        )
        # The cross-period alignment lookup: "every period's version of this section".
        op.create_index(
            "ix_filing_sections_stem",
            _SECTIONS,
            ["ticker", "section_stem"],
        )
        op.create_index(
            "ix_filing_sections_canonical",
            _SECTIONS,
            ["ticker", "canonical_id"],
        )
        op.create_index("ix_filing_sections_doc", _SECTIONS, ["doc_id"])

    if _COVERAGE not in existing:
        op.create_table(
            _COVERAGE,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ticker", sa.String(length=16), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column("form", sa.String(length=8), nullable=False),
            sa.Column("fiscal_year", sa.Integer(), nullable=True),
            sa.Column("fiscal_period", sa.String(length=4), nullable=False),
            sa.Column("source_ref", sa.String(length=255), nullable=True),
            # 'ok' | 'partial' | 'source_missing' | 'not_attempted' |
            # 'fetch_failed' | 'auth_blocked' | 'parse_failed' | 'schema_drift' |
            # 'no_sections_found' | 'regime_mismatch' | 'period_mismatch' |
            # 'image_only' | 'unsupported_form'
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("reason_code", sa.String(length=64), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("sections_written", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("extractor_version", sa.String(length=32), nullable=False),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ticker",
                "source",
                "form",
                "fiscal_year",
                "fiscal_period",
                "extractor_version",
                name="uq_filing_section_coverage_key",
            ),
        )
        op.create_index(
            "ix_filing_section_coverage_lookup",
            _COVERAGE,
            ["ticker", "fiscal_year", "fiscal_period"],
        )
        op.create_index("ix_filing_section_coverage_status", _COVERAGE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())
    if _COVERAGE in existing:
        op.drop_index("ix_filing_section_coverage_status", table_name=_COVERAGE)
        op.drop_index("ix_filing_section_coverage_lookup", table_name=_COVERAGE)
        op.drop_table(_COVERAGE)
    if _SECTIONS in existing:
        op.drop_index("ix_filing_sections_doc", table_name=_SECTIONS)
        op.drop_index("ix_filing_sections_canonical", table_name=_SECTIONS)
        op.drop_index("ix_filing_sections_stem", table_name=_SECTIONS)
        op.drop_index("ix_filing_sections_ticker_period", table_name=_SECTIONS)
        op.drop_table(_SECTIONS)
