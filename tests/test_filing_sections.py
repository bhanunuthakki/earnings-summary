"""Tests for the filing section-partition store (migration 0198).

Weighted toward the degradation paths, because those are what the feature is
actually asked to survive: one source present and the other not, two sources
disagreeing about the same filing, a payload whose shape changed, and a DB
that never ran the migration. A happy-path-only suite here would pass while
the store silently mislabels single-source periods as complete.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import edgar_sections, fmp_sections, ingest, store, taxonomy  # noqa: E402
from filings.models import (  # noqa: E402
    CoverageRecord,
    CoverageStatus,
    FilingForm,
    FilingSection,
    FiscalPeriod,
    HardStopError,
    SectionSource,
    SourceContractError,
    TickerNotResolvableError,
    most_severe_warning,
    normalize_stem,
)

_SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    doc_type VARCHAR(32) NOT NULL,
    file_path VARCHAR(512),
    accession_number VARCHAR(32),
    filing_date VARCHAR(10),
    period_end DATETIME
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    list_type VARCHAR(16),
    instrument_type VARCHAR(16),
    fiscal_year_end VARCHAR(5)
);
CREATE TABLE filing_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    doc_id INTEGER,
    accession_number VARCHAR(32),
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    period_end DATETIME,
    filing_date VARCHAR(10),
    section_key_raw VARCHAR(255) NOT NULL,
    section_stem VARCHAR(255) NOT NULL,
    canonical_id VARCHAR(64),
    title TEXT,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 VARCHAR(64) NOT NULL,
    char_len INTEGER NOT NULL,
    key_truncated INTEGER NOT NULL DEFAULT 0,
    extractor_version VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_sections_key UNIQUE (source, source_ref, section_key_raw, ordinal)
);
CREATE TABLE filing_section_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    source VARCHAR(16) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    source_ref VARCHAR(255),
    status VARCHAR(24) NOT NULL,
    reason_code VARCHAR(64),
    detail TEXT,
    sections_written INTEGER NOT NULL DEFAULT 0,
    extractor_version VARCHAR(32) NOT NULL,
    checked_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_section_coverage_key UNIQUE
        (ticker, source, form, fiscal_year, fiscal_period, extractor_version)
);
"""


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT INTO tracked_companies (ticker, list_type, instrument_type, fiscal_year_end) "
        "VALUES ('META', 'portfolio', 'equity', '12-31')"
    )
    connection.commit()
    return connection


def _section(**overrides: object) -> FilingSection:
    payload: dict[str, object] = {
        "ticker": "META",
        "source": SectionSource.EDGAR_TEXT,
        "source_ref": "0001326801-26-000017",
        "form": FilingForm.FORM_10K,
        "fiscal_period": FiscalPeriod.FY,
        "fiscal_year": 2025,
        "section_key_raw": "Item 1A",
        "text": "Risk factor prose. " * 20,
        "ordinal": 0,
        "extractor_version": "test_v1",
        "canonical_id": "risk_factors",
    }
    payload.update(overrides)
    return FilingSection.build(**payload)  # type: ignore[arg-type]


# --- stem normalization -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Revenue", "revenue"),
        ("Revenue (Tables)", "revenue"),
        ("Revenue - Narrative (Details)", "revenue"),
        ("Income Taxes - Schedule of Provision (Details)", "income taxes - schedule of provision"),
        ("Summary of Significant Accoun_3", "summary of significant accoun"),
        ("Segment and Geographical Info_1", "segment and geographical info"),
    ],
)
def test_normalize_stem_strips_disambiguators_and_qualifiers(raw: str, expected: str) -> None:
    assert normalize_stem(raw) == expected


def test_stem_collapses_the_fmp_n_suffix_family() -> None:
    """The _N suffix is assigned by document order, so it is not stable across
    years — every member of a family must reduce to one stem or cross-period
    joins fail exactly where the disclosure recurs."""
    family = [
        "Summary of Significant Accounti",
        "Summary of Significant Accoun_1",
        "Summary of Significant Accoun_2",
    ]
    stems = {normalize_stem(k) for k in family}
    assert len(stems) == 2, "truncation at differing widths yields at most two stems"


# --- store: writes ----------------------------------------------------------


def test_write_sections_is_idempotent(conn: sqlite3.Connection) -> None:
    assert store.write_sections(conn, [_section()]) == 1
    assert store.write_sections(conn, [_section()]) == 1
    assert conn.execute("SELECT COUNT(*) FROM filing_sections").fetchone()[0] == 1


def test_rewriting_updates_text_in_place(conn: sqlite3.Connection) -> None:
    store.write_sections(conn, [_section()])
    store.write_sections(
        conn, [_section(text="Rewritten prose. " * 20, extractor_version="test_v2")]
    )
    rows = conn.execute("SELECT text, extractor_version FROM filing_sections").fetchall()
    assert len(rows) == 1
    assert rows[0][0].startswith("Rewritten")
    assert rows[0][1] == "test_v2"


def test_reextraction_prunes_superseded_rows(conn: sqlite3.Connection) -> None:
    """One document holds exactly one partition.

    Regression: when the 20-F splitter learned to break Item 3 into sub-items,
    the old whole-``Item 3`` row survived next to the new ``Item 3.D`` and the
    filing was stored as two overlapping partitions of the same prose — which
    would double-count in any diff built on the store.
    """
    store.write_sections(
        conn,
        [
            _section(source_ref="acc-1", section_key_raw="Item 3", ordinal=0),
            _section(source_ref="acc-1", section_key_raw="Item 4", ordinal=1),
        ],
    )
    store.write_sections(
        conn,
        [
            _section(source_ref="acc-1", section_key_raw="Item 3.D", ordinal=0),
            _section(source_ref="acc-1", section_key_raw="Item 4", ordinal=1),
        ],
    )
    keys = {r[0] for r in conn.execute("SELECT section_key_raw FROM filing_sections").fetchall()}
    assert keys == {"Item 3.D", "Item 4"}


def test_pruning_is_scoped_to_the_documents_being_written(conn: sqlite3.Connection) -> None:
    """Re-ingesting one accession must not delete another accession's sections."""
    store.write_sections(conn, [_section(source_ref="acc-2024", section_key_raw="Item 1A")])
    store.write_sections(conn, [_section(source_ref="acc-2025", section_key_raw="Item 1A")])
    refs = {r[0] for r in conn.execute("SELECT source_ref FROM filing_sections").fetchall()}
    assert refs == {"acc-2024", "acc-2025"}


def test_dangling_doc_id_is_a_hard_stop(conn: sqlite3.Connection) -> None:
    with pytest.raises(HardStopError, match="not present in documents"):
        store.write_sections(conn, [_section(doc_id=4242)])


def test_valid_doc_id_passes_validation(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO documents (id, ticker, doc_type) VALUES (7, 'META', 'sec_10k')")
    assert store.write_sections(conn, [_section(doc_id=7)]) == 1


def test_missing_table_raises_unless_missing_ok() -> None:
    bare = sqlite3.connect(":memory:")
    with pytest.raises(HardStopError, match="alembic upgrade head"):
        store.get_sections(bare, "META")
    assert store.get_sections(bare, "META", missing_ok=True) == []
    assert store.period_availability(bare, "META", missing_ok=True) == []


def test_implausible_fiscal_year_rejected() -> None:
    with pytest.raises(ValueError, match="implausible fiscal_year"):
        _section(fiscal_year=12025)


# --- store: coverage + availability ----------------------------------------


def _coverage(**overrides: object) -> CoverageRecord:
    payload: dict[str, object] = {
        "ticker": "META",
        "source": SectionSource.FMP_RFILE,
        "form": FilingForm.FORM_10K,
        "fiscal_year": 2025,
        "fiscal_period": FiscalPeriod.FY,
        "status": CoverageStatus.OK,
        "extractor_version": "test_v1",
    }
    payload.update(overrides)
    return CoverageRecord(**payload)  # type: ignore[arg-type]


def test_coverage_upsert_is_null_safe_on_fiscal_year(conn: sqlite3.Connection) -> None:
    """A NULL fiscal_year must still dedupe: SQLite's UNIQUE index treats NULLs
    as distinct, which would otherwise pile up duplicate verdicts for exactly
    the periods whose year we failed to resolve."""
    store.record_coverage(conn, _coverage(fiscal_year=None, status=CoverageStatus.SOURCE_MISSING))
    store.record_coverage(conn, _coverage(fiscal_year=None, status=CoverageStatus.FETCH_FAILED))
    rows = conn.execute("SELECT status FROM filing_section_coverage").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "fetch_failed"


def test_single_source_period_is_labeled(conn: sqlite3.Connection) -> None:
    store.write_sections(conn, [_section(source=SectionSource.FMP_RFILE, source_ref="a.json")])
    store.record_coverage(conn, _coverage())
    store.record_coverage(
        conn,
        _coverage(
            source=SectionSource.EDGAR_TEXT,
            status=CoverageStatus.FETCH_FAILED,
            reason_code="primary_document_fetch_failed",
        ),
    )
    [period] = store.period_availability(conn, "META")
    assert period.is_single_source
    assert period.sources_present == {SectionSource.FMP_RFILE}
    assert period.absent_sources == {"edgar_text": "primary_document_fetch_failed"}


def test_two_source_period_is_not_single_source(conn: sqlite3.Connection) -> None:
    store.write_sections(
        conn,
        [
            _section(source=SectionSource.FMP_RFILE, source_ref="a.json"),
            _section(source=SectionSource.EDGAR_TEXT, source_ref="0001-25-000001"),
        ],
    )
    [period] = store.period_availability(conn, "META")
    assert not period.is_single_source
    assert period.section_counts == {"fmp_rfile": 1, "edgar_text": 1}


def test_coverage_claiming_ok_with_no_rows_reads_as_absent(conn: sqlite3.Connection) -> None:
    """Trust the rows, not the claim: a coverage row can go stale (a later
    truncate, a failed transaction), and a period with no stored sections is
    absent no matter what the verdict says."""
    store.write_sections(conn, [_section(source=SectionSource.FMP_RFILE, source_ref="a.json")])
    store.record_coverage(conn, _coverage(source=SectionSource.EDGAR_TEXT, sections_written=99))
    [period] = store.period_availability(conn, "META")
    assert period.sources_present == {SectionSource.FMP_RFILE}
    assert "edgar_text" in period.absent_sources


def test_mismatch_reason_surfaces_in_availability(conn: sqlite3.Connection) -> None:
    store.write_sections(conn, [_section(source=SectionSource.FMP_RFILE, source_ref="a.json")])
    store.record_coverage(conn, _coverage(reason_code="regime_mismatch_resolved_to_declared"))
    [period] = store.period_availability(conn, "META")
    assert any("regime_mismatch" in m for m in period.mismatches)


# --- store: timeline --------------------------------------------------------


def test_section_timeline_is_oldest_first(conn: sqlite3.Connection) -> None:
    for year in (2023, 2025, 2024):
        store.write_sections(conn, [_section(fiscal_year=year, source_ref=f"acc-{year}")])
    timeline = store.section_timeline(conn, "META", canonical_id="risk_factors")
    assert [s.fiscal_year for s in timeline] == [2023, 2024, 2025]


def test_section_timeline_requires_exactly_one_selector(conn: sqlite3.Connection) -> None:
    with pytest.raises(HardStopError, match="exactly one"):
        store.section_timeline(conn, "META")
    with pytest.raises(HardStopError, match="exactly one"):
        store.section_timeline(conn, "META", canonical_id="risk_factors", section_stem="revenue")


def test_timeline_spans_forms_via_concept(conn: sqlite3.Connection) -> None:
    """A company that moves 10-K -> 20-F keeps one risk-factors timeline: the
    concept is the join key, not the form-specific item number."""
    store.write_sections(
        conn,
        [
            _section(
                fiscal_year=2024,
                source_ref="acc-2024",
                form=FilingForm.FORM_10K,
                section_key_raw="Item 1A",
            ),
            _section(
                fiscal_year=2025,
                source_ref="acc-2025",
                form=FilingForm.FORM_20F,
                section_key_raw="Item 3.D",
            ),
        ],
    )
    timeline = store.section_timeline(conn, "META", canonical_id="risk_factors")
    assert [(s.fiscal_year, s.form.value) for s in timeline] == [(2024, "10-K"), (2025, "20-F")]


# --- EDGAR splitting --------------------------------------------------------


def _fake_10k() -> str:
    filler = "\n".join("Body prose about the business and its operations." for _ in range(60))
    return "\n".join(
        [
            "TABLE OF CONTENTS",
            "Item 1. Business.......................3",
            "Item 1A. Risk Factors..................9",
            "Item 7. Management's Discussion........40",
            "Item 8. Financial Statements...........60",
            "",
            "Item 1. Business",
            filler,
            "Item 1A. Risk Factors",
            filler,
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            filler,
            "Item 8. Financial Statements and Supplementary Data",
            filler,
        ]
    )


def test_split_10k_finds_body_not_table_of_contents() -> None:
    result = edgar_sections.split_10k(_fake_10k())
    concepts = [s.concept for s in result.slices]
    assert "risk_factors" in concepts
    assert "mdna" in concepts
    risk = next(s for s in result.slices if s.concept == "risk_factors")
    assert "Body prose" in risk.text
    assert "..." not in risk.text[:200], "a TOC line leaked into the body slice"


def test_split_10k_warns_when_core_sections_missing() -> None:
    text = "Item 1. Business\n" + ("Prose. " * 200) + "\nItem 2. Properties\n" + ("Prose. " * 200)
    result = edgar_sections.split_10k(text)
    assert "missing_risk_factors" in result.warnings
    assert "missing_mdna" in result.warnings


def _fake_10k_dotless_toc() -> str:
    """A table of contents with NO dotted leaders at all -- each item title and
    its page number are separate lines/block elements, the real production
    shape for NU's 20-F and FCX's 10-K (``docs/design`` / PR description).
    ``_TOC_LEADER`` alone would accept every one of these as a real header.
    """
    filler = "\n".join("Body prose about the business and its operations." for _ in range(60))
    return "\n".join(
        [
            "TABLE OF CONTENTS",
            "",
            "Item 1. Business",
            "3",
            "",
            "Item 1A. Risk Factors",
            "9",
            "",
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            "40",
            "",
            "Item 8. Financial Statements and Supplementary Data",
            "60",
            "",
            "Item 1. Business",
            filler,
            "Item 1A. Risk Factors",
            filler,
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            filler,
            "Item 8. Financial Statements and Supplementary Data",
            filler,
        ]
    )


def test_split_10k_rejects_dotless_toc_without_leaders() -> None:
    """Regression for the diagnosed defect: NU's 20-F and FCX's 10-K render
    their TOC as one title per line and one bare page number per line, with no
    dots anywhere, so the old ``_TOC_LEADER``-only filter let the chain latch
    onto it (NU's Item 2 slice opened with TOC page numbers; FCX's Item 1B
    ballooned to ~298K chars of everything between the TOC and Item 1C's real
    body). The chain must still find the real body here, not the TOC."""
    result = edgar_sections.split_10k(_fake_10k_dotless_toc())
    concepts = [s.concept for s in result.slices]
    assert "risk_factors" in concepts
    assert "mdna" in concepts
    risk = next(s for s in result.slices if s.concept == "risk_factors")
    assert "Body prose" in risk.text
    first_line = risk.text.strip().splitlines()[0]
    assert not first_line.strip().isdigit(), "a bare TOC page number leaked into the body slice"


# --- new integrity checks: trivial-section-oversized + mid-sentence share ---


def test_trivial_section_oversized_flags_implausibly_large_concept() -> None:
    """unresolved_staff_comments / mine_safety / offer_statistics are almost
    always one line of "Not applicable." -- a slice this size under one of
    those labels means a boundary swallowed a neighboring item's disclosure,
    which is exactly what happened to FCX (~298K chars) and NU (~87-106K)."""
    prose = "Disclosure prose that fills out the body. " * 200
    oversized = [
        edgar_sections.SectionSlice(
            key="Item 1B", text=prose, ordinal=0, concept="unresolved_staff_comments"
        )
    ]
    assert edgar_sections._trivial_section_oversized(oversized)

    trivial = [
        edgar_sections.SectionSlice(
            key="Item 1B", text="Not applicable.", ordinal=0, concept="unresolved_staff_comments"
        )
    ]
    assert not edgar_sections._trivial_section_oversized(trivial)

    # A concept OUTSIDE the near-always-trivial set is never flagged, no
    # matter how large -- a genuinely long MD&A is not a defect.
    large_mdna = [edgar_sections.SectionSlice(key="Item 7", text=prose, ordinal=0, concept="mdna")]
    assert not edgar_sections._trivial_section_oversized(large_mdna)


def test_mid_sentence_share_flags_a_filing_wide_pattern_not_one_slice() -> None:
    """A single lowercase-opening slice ("10b5-1 Trading Plans...") happens in
    clean filings too, so only a SHARE across the filing's own slices should
    fire -- not one occurrence. Real case: NU's 20-F, after the TOC fix, still
    has several slices open mid-sentence because its body reorders items
    relative to their canonical numbers."""
    clean = [
        edgar_sections.SectionSlice(
            key=f"Item {i}", text="Prose that starts properly. " * 10, ordinal=i, concept=f"c{i}"
        )
        for i in range(6)
    ]
    assert not edgar_sections._mid_sentence_share_suspect(clean)

    # One slice with a legitimate lowercase cross-reference is still fine.
    one_off = list(clean)
    one_off[0] = edgar_sections.SectionSlice(
        key="Item 0", text="10b5-1 Trading Plans discussed further below.", ordinal=0, concept="c0"
    )
    assert not edgar_sections._mid_sentence_share_suspect(one_off)

    # Multiple cuts landing mid-sentence in the SAME filing is the pattern.
    shifted = list(clean)
    shifted[0] = edgar_sections.SectionSlice(
        key="Item 0", text="comments\n\nNot applicable.", ordinal=0, concept="c0"
    )
    shifted[1] = edgar_sections.SectionSlice(
        key="Item 1",
        text="staff information that continues from the prior cut.",
        ordinal=1,
        concept="c1",
    )
    assert edgar_sections._mid_sentence_share_suspect(shifted)


def test_enumerator_openings_are_not_mid_sentence() -> None:
    """ "(a) Evaluation of disclosure controls..." is a completely normal,
    correctly-cut section start -- legal drafting enumerators must be
    stripped before the case of the first real word is inspected."""
    assert not edgar_sections._starts_mid_sentence("(a) Evaluation of disclosure controls.")
    assert not edgar_sections._starts_mid_sentence("(a)(1). Financial Statements.")
    assert edgar_sections._starts_mid_sentence("comments\n\nNot applicable.")


def test_new_integrity_warnings_are_silent_on_clean_fixtures() -> None:
    """The existing clean 10-K/20-F fixtures must not gain new false-positive
    warnings just because the two new integrity checks were wired in."""
    result_10k = edgar_sections.split_10k(_fake_10k())
    assert "trivial_section_oversized" not in result_10k.warnings
    assert "slice_starts_mid_sentence" not in result_10k.warnings

    filler = "\n".join("Foreign private issuer disclosure prose." for _ in range(40))
    text = "\n".join(
        [
            "Item 3. Key Information",
            "3.A Selected Financial Data",
            filler,
            "3.B Capitalization and Indebtedness",
            filler,
            "3.D Risk Factors",
            filler,
            "Item 5. Operating and Financial Review and Prospects",
            filler,
        ]
    )
    result_20f = edgar_sections.split_20f(text)
    assert "trivial_section_oversized" not in result_20f.warnings
    assert "slice_starts_mid_sentence" not in result_20f.warnings


def test_split_10q_is_part_aware() -> None:
    filler = "\n".join("Quarterly prose that fills the section body." for _ in range(40))
    text = "\n".join(
        [
            "PART I",
            "Item 1. Financial Statements",
            filler,
            "Item 2. Management's Discussion and Analysis of Financial Condition",
            filler,
            "PART II",
            "Item 1. Legal Proceedings",
            filler,
            "Item 1A. Risk Factors",
            filler,
        ]
    )
    result = edgar_sections.split_10q(text)
    by_key = {s.key: s.concept for s in result.slices}
    assert by_key.get("Part I Item 1") == "financial_statements"
    assert by_key.get("Part II Item 1") == "legal_proceedings"
    assert by_key.get("Part II Item 1A") == "risk_factors"


def test_split_10q_flags_unresolved_part_boundary() -> None:
    text = "Item 1. Financial Statements\n" + ("Prose. " * 200)
    result = edgar_sections.split_10q(text)
    assert "part_boundary_unresolved" in result.warnings


def test_split_20f_subsplits_item_3_into_risk_factors() -> None:
    filler = "\n".join("Foreign private issuer disclosure prose." for _ in range(40))
    text = "\n".join(
        [
            "Item 3. Key Information",
            "3.A Selected Financial Data",
            filler,
            "3.B Capitalization and Indebtedness",
            filler,
            "3.D Risk Factors",
            filler,
            "Item 5. Operating and Financial Review and Prospects",
            filler,
        ]
    )
    result = edgar_sections.split_20f(text)
    by_concept = {s.concept: s.key for s in result.slices}
    assert by_concept.get("risk_factors") == "Item 3.D"
    assert by_concept.get("mdna") == "Item 5"
    assert "Item 3" not in [s.key for s in result.slices], "parent must not duplicate its sub-items"


def test_split_20f_subsplits_on_the_real_bare_letter_convention() -> None:
    """Real 20-Fs write "D. Risk Factors" with no "3." prefix, and 3.A/3.B are
    routinely one line of "Not applicable" — so the risk-factors sub-item must
    survive as the ONLY located sub-item. Regression: an earlier rule required
    two surviving sub-items and silently dropped every FPI risk-factors
    section on the book (WIX FY2023-25 all fell back to a coarse Item 3)."""
    risk_prose = "\n".join("Risk factor disclosure prose for the issuer." for _ in range(80))
    text = "\n".join(
        [
            "ITEM 3. KEY INFORMATION",
            "A. Selected Financial Data",
            "Not applicable.",
            "B. Capitalization and Indebtedness",
            "Not applicable.",
            "D. Risk Factors",
            risk_prose,
            "ITEM 4. INFORMATION ON THE COMPANY",
            "\n".join("Company information prose." for _ in range(40)),
        ]
    )
    result = edgar_sections.split_20f(text)
    by_concept = {s.concept: s.key for s in result.slices}
    assert by_concept.get("risk_factors") == "Item 3.D"
    assert "Item 3" not in [s.key for s in result.slices]


def test_split_20f_preamble_is_kept_not_dropped() -> None:
    """Text between the parent header and the first sub-item is real
    disclosure; it is emitted as its own slice rather than silently lost."""
    lead = "\n".join("Introductory discussion of results before the sub-items." for _ in range(20))
    body = "\n".join("Operating results prose." for _ in range(60))
    text = "\n".join(
        [
            "Item 5. Operating and Financial Review and Prospects",
            lead,
            "A. Operating Results",
            body,
            "B. Liquidity and Capital Resources",
            body,
        ]
    )
    result = edgar_sections.split_20f(text)
    keys = [s.key for s in result.slices]
    assert "Item 5 (preamble)" in keys
    assert "Item 5.A" in keys and "Item 5.B" in keys


def test_split_20f_keeps_parent_when_subsplit_explains_too_little() -> None:
    """A sub-item that accounts for a sliver of its parent is an unreliable
    read of the structure — the parent stays whole and the run is flagged."""
    text = "\n".join(
        [
            "Item 3. Key Information",
            "\n".join(
                "Substantial key-information prose that dominates the item." for _ in range(200)
            ),
            "B. Capitalization and Indebtedness",
            "Short note about capitalization that is barely long enough to keep.  " * 3,
        ]
    )
    result = edgar_sections.split_20f(text)
    assert "Item 3" in [s.key for s in result.slices]
    assert any("subsplit_incomplete" in w for w in result.warnings)


# --- header matching: prose, contents blocks, and filer-specific labels -----
#
# Every case below is a real defect found by re-partitioning the 125 cached
# EDGAR filings under data/sec_text and comparing against the stored rows.


def _wrapped(paragraph: str, width: int = 135) -> str:
    """Hard-wrap prose the way NU's stripped 20-F text arrives.

    Line width is the whole point: the header matcher's only prose defense used
    to be a 200-character line-length limit, which is inert on a document whose
    every line is 135 characters. Nothing else in the corpus wraps this way,
    which is why one issuer carried a silently wrong partition.
    """
    words, lines, current = paragraph.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return "\n".join(lines)


def test_cross_reference_in_wrapped_prose_is_not_a_header() -> None:
    """An item citation inside a sentence must not become a header candidate.

    NU's 20-F cites '"Item 3. Key Information—D. Risk Factors"' dozens of times
    in body prose. Hard-wrapped, each citation sits on a short line and passed
    the length check, so the chain scorer built its partition out of footnote
    references — including one that started its line with the opening quote.
    """
    citing = _wrapped(
        "The risks described in this section should be read together with the discussion under "
        '"Item 5. Operating and Financial Review and Prospects" and the disclosures set out in '
        '"Item 3. Key Information-D. Risk Factors" and elsewhere in this annual report, each of '
        "which may materially affect the results we report for any future period."
    )
    for spec in taxonomy.FORM_20F_ITEMS:
        assert taxonomy._candidate_positions(citing, spec) == [], (
            f"{spec.key} matched a cross-reference in prose"
        )

    # The same title as an actual heading still matches — the test above must
    # not be passing merely because the patterns stopped working.
    heading = "Item 5. Operating and Financial Review and Prospects\nBody prose follows here."
    item5 = next(s for s in taxonomy.FORM_20F_ITEMS if s.key == "Item 5")
    assert taxonomy._candidate_positions(heading, item5)


def test_body_starts_after_a_header_that_wraps_onto_a_second_line() -> None:
    """A slice must not open on the tail of its own heading.

    NU's 20-F prints "Item 4A. Unresolved" / "staff comments" across two
    physical lines, and the title pattern stops at "staff" — so the body began
    mid-word on " comments". The same arithmetic bites any filing whose title
    pattern matches a prefix of the real heading ("Market for" inside "Market
    for Registrant's Common Equity…"). Independently confirmed against
    data/sec_text/NU_20f_000129281426002166.txt.
    """
    text = "Item 4A. Unresolved\nstaff comments\n\nNot applicable.\n"
    spec = next(s for s in taxonomy.FORM_20F_ITEMS if s.key == "Item 4A")
    positions = taxonomy._candidate_positions(text, spec)
    assert positions, "the wrapped heading must still be found"
    _, body_start, _ = positions[0]
    assert text[body_start:].strip() == "Not applicable."


def test_contents_block_does_not_consume_item_numbers() -> None:
    """A table of contents must not spend the item numbers the body needs.

    Contents rows sit a few hundred characters apart, so each earns its
    per-item bonus while contributing no gap: the highest-scoring chain walked
    the whole TOC and only then entered the body. Harmless-looking, except the
    item numbers were now taken, so real body items became unreachable.
    """
    items = [
        ("Item 1", "Business"),
        ("Item 1A", "Risk Factors"),
        ("Item 1B", "Unresolved Staff Comments"),
        ("Item 1C", "Cybersecurity"),
        ("Item 2", "Properties"),
        ("Item 3", "Legal Proceedings"),
        ("Item 5", "Market for Registrant's Common Equity"),
        ("Item 7", "Management's Discussion and Analysis of Financial Condition"),
        ("Item 7A", "Quantitative and Qualitative Disclosures About Market Risk"),
        ("Item 8", "Financial Statements and Supplementary Data"),
        ("Item 9A", "Controls and Procedures"),
        ("Item 10", "Directors, Executive Officers and Corporate Governance"),
        ("Item 11", "Executive Compensation"),
        ("Item 12", "Security Ownership of Certain Beneficial Owners"),
        ("Item 15", "Exhibits"),
    ]
    body: list[str] = []
    for key, title in items:
        body.append(f"{key}. {title}")
        body.extend(f"Body prose for {key} about the registrant." for _ in range(40))
    text = "\n".join(["TABLE OF CONTENTS", *[f"{k}. {t}" for k, t in items], "", *body])
    result = edgar_sections.split_10k(text)
    by_key = {s.key: s for s in result.slices}
    assert set(by_key) == {k for k, _ in items}
    for key, slice_ in by_key.items():
        # Each slice must hold ITS OWN body, which a contents row cannot: a row
        # taken as a header slices to the next row, a few dozen characters away.
        assert f"Body prose for {key} " in slice_.text, f"{key} did not get its own body"
        assert "TABLE OF CONTENTS" not in slice_.text


def test_a_contents_block_is_removed_whole_or_not_at_all() -> None:
    """Thinning a contents block is worse than leaving it intact.

    `_drop_contents_block` identifies the block by its density, so anything
    that removes SOME of its rows first destroys the signal that finds the
    rest — and the survivors go on to consume item numbers exactly as before.
    This is not hypothetical: #1009's per-row lookahead test stripped 29 of
    NU's 32 contents rows and left the final two (a lookahead cannot see the
    block's tail, which has no following entry to look ahead at), which cost
    all three NU years their risk-factors section.

    Pinned by simulating the thinning directly: drop all but the last two
    contents rows, and the partition must still not hand a body item's number
    to a leftover row.
    """
    items = [f"Item {n}" for n in ("1", "1A", "7", "8", "9A", "10", "11", "12", "15")]
    titles = {
        "Item 1": "Business",
        "Item 1A": "Risk Factors",
        "Item 7": "Management's Discussion and Analysis of Financial Condition",
        "Item 8": "Financial Statements and Supplementary Data",
        "Item 9A": "Controls and Procedures",
        "Item 10": "Directors, Executive Officers and Corporate Governance",
        "Item 11": "Executive Compensation",
        "Item 12": "Security Ownership of Certain Beneficial Owners",
        "Item 15": "Exhibits",
    }
    body: list[str] = []
    for key in items:
        body.append(f"{key}. {titles[key]}")
        body.extend(f"Body prose for {key} about the registrant." for _ in range(40))

    thinned_contents = [f"{k}. {titles[k]}" for k in items[-2:]]
    text = "\n".join(["TABLE OF CONTENTS", *thinned_contents, "", *body])
    result = edgar_sections.split_10k(text)
    by_key = {s.key: s for s in result.slices}
    for key in ("Item 1A", "Item 7", "Item 12", "Item 15"):
        assert key in by_key, f"{key} was lost to a leftover contents row"
        assert f"Body prose for {key} " in by_key[key].text


def test_running_header_repeat_is_dropped_when_isolated() -> None:
    """An item's own title, reprinted verbatim as a running page header well
    inside that item's real body with nothing else in the taxonomy located in
    between, is the running-header defect (NVO's 20-F: "ITEM 3 KEY
    INFORMATION" appears once as the true heading, then again a page into the
    D. Risk Factors prose it introduces) — only the isolated, later repeat is
    dropped, opt-in via ``drop_running_header_repeats``."""
    pool = [
        (100, 0, 110, "ITEM 2 OFFER STATISTICS"),
        (500, 1, 510, "ITEM 3 KEY INFORMATION"),  # the real header
        (900, 1, 910, "ITEM 3 KEY INFORMATION"),  # running-header repeat
        (1200, 2, 1210, "ITEM 4 INFORMATION ON THE COMPANY"),
    ]
    out = taxonomy._drop_running_header_repeats(pool)
    assert out == [pool[0], pool[1], pool[3]], "only the isolated later repeat should be dropped"


def test_running_header_repeat_kept_when_interleaved_with_another_item() -> None:
    """A repeat that has ANOTHER item's own candidate sitting between it and
    the earlier occurrence is not the running-header defect — it is (at
    least potentially) a small index/TOC pair, and ``_drop_contents_block``'s
    density signal is what has to arbitrate those, not this filter.

    Pinned directly on the real defect this guards against: a 10-Q's Part
    I/Part II one-paragraph index prints "PART I" and "PART II" a few hundred
    characters apart, then the REAL "PART I" header, then (far later) the
    real "PART II" header — Part I's own index-vs-real pair has Part II's
    index entry sitting between them, so it must be left alone here. Treating
    it as an isolated repeat instead (an earlier version of this fix) dropped
    the real Part I header and broke the AMZN/MELI 10-Q boundary."""
    pool = [
        (100, 0, 110, "PART I. FINANCIAL INFORMATION"),  # index entry
        (150, 1, 160, "PART II. OTHER INFORMATION"),  # index entry -- sits BETWEEN the Part I pair
        (900, 0, 910, "PART I. FINANCIAL INFORMATION"),  # the real header
        (90000, 1, 90010, "PART II. OTHER INFORMATION"),  # the real header, far later
    ]
    out = taxonomy._drop_running_header_repeats(pool)
    assert out == pool, "an interleaved pair must survive this filter untouched"


def test_split_20f_recovers_risk_factors_despite_running_header_repeat() -> None:
    """End-to-end regression pin for the NVO defect: Item 3's D. Risk Factors
    sub-item must still be located even though "ITEM 3 KEY INFORMATION"
    reprints as a running header inside the risk-factors prose itself."""
    risk_prose_p1 = "\n".join(
        f"Risk factor prose sentence {n} about the issuer." for n in range(30)
    )
    risk_prose_p2 = "\n".join(f"More risk factor prose sentence {n}." for n in range(30))
    text = "\n".join(
        [
            "ITEM 1 IDENTITY OF DIRECTORS, SENIOR MANAGEMENT AND ADVISORS",
            "Not applicable.",
            "ITEM 2 OFFER STATISTICS AND EXPECTED TIMETABLE",
            "Not applicable.",
            "ITEM 3 KEY INFORMATION",
            "A. [RESERVED]",
            "B. CAPITALIZATION AND INDEBTEDNESS",
            "Not applicable.",
            "C. REASONS FOR THE OFFER AND USE OF PROCEEDS",
            "Not applicable.",
            "D. RISK FACTORS",
            risk_prose_p1,
            # The running-header repeat: the same title, verbatim, on its own
            # line, well inside the risk-factors prose it introduced.
            "ITEM 3 KEY INFORMATION",
            risk_prose_p2,
            "ITEM 4 INFORMATION ON THE COMPANY",
            "\n".join(f"Company information prose sentence {n}." for n in range(40)),
        ]
    )
    result = edgar_sections.split_20f(text)
    by_concept = {s.concept: s for s in result.slices}
    assert "risk_factors" in by_concept, "the running-header repeat must not swallow Item 3.D"
    assert "Risk factor prose sentence 0" in by_concept["risk_factors"].text
    assert "company_information" in by_concept
    assert "Company information prose sentence 0" in by_concept["company_information"].text


def test_thin_restated_10k_contents_block_is_stripped() -> None:
    """A partial-restatement 10-K/A that only touches a handful of items still
    prints a dense contents page for those items — RGEN's FY2023 10-K/A
    (Items 1, 1A, 7, 7A, 8, 9A, 15, each suffixed "(Restated)"). The document's
    OWN taxonomy footprint (7 items) never reaches the absolute
    ``_TOC_MIN_ITEMS`` floor an ordinary, full 10-K would, so the business
    section must not inherit the contents page as its own body."""
    restated_items = [
        ("Item 1", "Business (Restated)"),
        ("Item 1A", "Risk Factors (Restated)"),
        ("Item 7", "Management's Discussion and Analysis of Financial Condition (Restated)"),
        ("Item 7A", "Quantitative and Qualitative Disclosures About Market Risk (Restated)"),
        ("Item 8", "Financial Statements and Supplementary Data (Restated)"),
        ("Item 9A", "Controls and Procedures (Restated)"),
        ("Item 15", "Exhibits and Financial Statement Schedules (Restated)"),
    ]
    body: list[str] = []
    for key, title in restated_items:
        body.append(f"ITEM {key.split()[-1]}. {title.upper()}")
        body.extend(f"Restated body prose for {key} about the registrant." for _ in range(40))
    contents = [f"{key}.\n\n{title}\n\n{n + 1}" for n, (key, title) in enumerate(restated_items)]
    text = "\n".join(["Table of Contents", *contents, "", *body])
    result = edgar_sections.split_10k(text)
    by_key = {s.key: s for s in result.slices}
    assert "Item 1" in by_key, "the business section must still be located"
    assert "Restated body prose for Item 1 " in by_key["Item 1"].text
    assert "Table of Contents" not in by_key["Item 1"].text
    assert "Restated (Restated)" not in by_key["Item 1"].text


def test_small_taxonomy_pass_never_treats_its_own_index_as_a_contents_block() -> None:
    """A taxonomy that is small BY DESIGN (a 10-Q's 2-entry Part I/Part II
    pass) must keep relying purely on chain scoring to resolve a TOC-vs-real
    pair, exactly as before this fix — the adaptive, duplicated-item-based
    branch is scoped to a LARGE taxonomy used by a thin document (RGEN's
    restated 10-K/A), not to a taxonomy that is inherently tiny. Regression
    pin: an earlier version of this fix keyed the gate on how many DISTINCT
    items were found rather than on the taxonomy's own size, and it broke
    exactly this shape on real AMZN/MELI 10-Qs."""
    part1 = edgar_sections._PART1_SPEC
    part2 = edgar_sections._PART2_SPEC
    pool = [
        (100, 0, 110, "PART I. FINANCIAL INFORMATION"),  # one-paragraph index
        (150, 1, 160, "PART II. OTHER INFORMATION"),  # one-paragraph index
        (900, 0, 910, "PART I. FINANCIAL INFORMATION"),  # the real header
        (90000, 1, 90010, "PART II. OTHER INFORMATION"),  # the real header
    ]
    out = taxonomy._drop_contents_block(pool, taxonomy_size=len((part1, part2)))
    assert out == pool, "a 2-item taxonomy must never engage the thin-filing branch"


def test_items_printed_out_of_order_are_still_partitioned() -> None:
    """Filers reorder items, and the taxonomy's order must not overrule them.

    NU's 20-F prints Item 4, then Item 4A, then Item 3, then Item 1. Requiring
    taxonomy order made Items 3 and 1 unreachable once Item 4 was taken, so the
    entire risk-factors disclosure was absorbed into the preceding slice — a
    swallowed item, not merely a missing one.
    """
    company = "\n".join("Company information prose." for _ in range(60))
    risks = "\n".join("Risk factor disclosure prose for the issuer." for _ in range(60))
    directors = "\n".join("Directors and senior management prose." for _ in range(60))
    text = "\n".join(
        [
            "Item 4. Information on the Company",
            company,
            "Item 4A. Unresolved Staff Comments",
            "Not applicable.",
            "Item 3. Key Information",
            "D. Risk Factors",
            risks,
            "Item 1. Identity of Directors, Senior Management and Advisers",
            directors,
        ]
    )
    result = edgar_sections.split_20f(text)
    by_concept = {s.concept: s for s in result.slices}
    assert by_concept["risk_factors"].key == "Item 3.D"
    assert "Risk factor disclosure" in by_concept["risk_factors"].text
    assert "Company information prose" not in by_concept["risk_factors"].text
    assert "Company information prose" in by_concept["company_information"].text
    assert "Risk factor disclosure" not in by_concept["company_information"].text
    assert [s.key for s in result.slices] == [
        "Item 4",
        "Item 3.D",
        "Item 1",
    ], "slices must come back in document order, not taxonomy order"


def test_no_item_is_emitted_twice() -> None:
    """Dropping the order constraint lets a chain revisit an item; it must not.

    Two slices under one key would hand the store rival texts for one
    disclosure. The guard falls back to the strictly-ordered chain, which is
    unique by construction — and it is a live path, firing on DASH, NVO, RGEN
    and VEEV in the cached corpus.
    """
    filler = "\n".join("Disclosure prose that fills out the body." for _ in range(40))
    text = "\n".join(
        [
            "Item 1. Business",
            filler,
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            filler,
            "Item 15. Exhibits",
            filler,
            # An exhibit index that names Item 1 again, after Item 15.
            "Item 1. Business",
            filler,
        ]
    )
    keys = [s.key for s in edgar_sections.split_10k(text).slices]
    assert len(keys) == len(set(keys)), f"an item was emitted twice: {keys}"


def test_market_risk_and_financial_statements_title_variants() -> None:
    """ServiceNow writes "Qualitative and Quantitative" and "Consolidated
    Financial Statements". Neither matched, so Items 7A and 8 were never
    located — and a missing boundary is worse than a missing section: MD&A ran
    straight through both, ending 120KB later inside the segment footnote.
    """
    filler = "\n".join("Managements discussion prose about results." for _ in range(60))
    statements = "\n".join("(19) Segment and Geographic Information detail." for _ in range(60))
    text = "\n".join(
        [
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            filler,
            "Item 7A. Qualitative and Quantitative Disclosures About Market Risk",
            "\n".join("Market risk prose." for _ in range(40)),
            "Item 8. Consolidated Financial Statements and Supplementary Data",
            statements,
        ]
    )
    by_concept = {s.concept: s for s in edgar_sections.split_10k(text).slices}
    assert set(by_concept) >= {"mdna", "market_risk", "financial_statements"}
    assert "Segment and Geographic" not in by_concept["mdna"].text, "MD&A ran into Item 8"


def test_two_items_answered_under_one_plural_heading() -> None:
    """FCX answers Items 7 and 7A together and heads the section "Items 7. and
    7A. Management's Discussion and Analysis…". Neither the plural nor the
    second item number was tolerated, so the title keyword was unreachable and
    the heading matched nothing at all — FCX had no `mdna` section for any of
    its three cached fiscal years, while `missing_mdna` was the only hint."""
    body = "\n".join("Managements discussion prose about results." for _ in range(60))
    statements = "\n".join("Report of independent accountants detail." for _ in range(60))
    text = "\n".join(
        [
            "Items 1. and 2. Business and Properties",
            "\n".join("Business and properties prose." for _ in range(40)),
            "Item 1A. Risk Factors",
            "\n".join("Risk disclosure prose." for _ in range(40)),
            "Items 7. and 7A. Management's Discussion and Analysis of Financial Condition",
            body,
            "Item 8. Financial Statements and Supplementary Data",
            statements,
        ]
    )
    by_concept = {s.concept: s for s in edgar_sections.split_10k(text).slices}
    assert "mdna" in by_concept, "the plural combined heading was not matched"
    assert "Managements discussion" in by_concept["mdna"].text
    assert "Report of independent" not in by_concept["mdna"].text, "MD&A ran into Item 8"
    assert "business" in by_concept, "the plural Items 1. and 2. heading was not matched"

    # The ordinary singular form must be unaffected by the plural tolerance.
    plain = "\n".join(
        [
            "Item 7. Management's Discussion and Analysis of Financial Condition",
            body,
            "Item 8. Financial Statements and Supplementary Data",
            statements,
        ]
    )
    assert "mdna" in {s.concept for s in edgar_sections.split_10k(plain).slices}


def test_subitem_letter_may_be_the_wrong_one() -> None:
    """NU prints its risk factors as "A.Risk Factors" under Item 3 while its own
    contents page and every cross-reference call the section 3.D. Keying on the
    SEC's letter found no risk-factors sub-item at all and left Item 3 whole."""
    risks = "\n".join("Risk factor disclosure prose for the issuer." for _ in range(80))
    text = "\n".join(
        [
            "Item 3. Key Information",
            "A.[Reserved.]",
            "B.Capitalization and Indebtedness",
            "Not applicable.",
            "A.Risk Factors",
            risks,
            "Item 4. Information on the Company",
            "\n".join("Company information prose." for _ in range(40)),
        ]
    )
    by_concept = {s.concept: s.key for s in edgar_sections.split_20f(text).slices}
    assert by_concept.get("risk_factors") == "Item 3.D", (
        "the concept follows the disclosure, and section_key_raw keeps the SEC's label"
    )


def test_subitem_title_words_in_a_sentence_are_not_a_subitem_header() -> None:
    """Tolerating a mislabeled letter must not become tolerating NO letter.

    Without a letter, ``risk\\s+factors?`` matches the opening words of an
    ordinary sentence — "Risk factor disclosure prose…" — so every line of a
    risk section became a rival 3.D header and the slice collapsed to the last
    one. The letter must be present and stand alone; which letter it is, is the
    part filers get wrong.
    """
    body = "\n".join("Risk factor disclosure prose for the issuer." for _ in range(40))
    risk_spec = next(s for s in taxonomy.FORM_20F_ITEM3_SUBITEMS if s.key == "Item 3.D")
    assert taxonomy._candidate_positions(body, risk_spec) == []
    assert taxonomy._candidate_positions("D. Risk Factors\n" + body, risk_spec)
    assert taxonomy._candidate_positions("A.Risk Factors\n" + body, risk_spec)


def test_split_freeform_falls_back_to_whole_document() -> None:
    result = edgar_sections.split_freeform("just one long run of prose without headings. " * 40)
    assert len(result.slices) == 1
    assert "no_headings_detected" in result.warnings


def test_split_document_rejects_40f_as_unsupported() -> None:
    result = edgar_sections.split_document(FilingForm.FORM_40F, "Some MJDS text. " * 100)
    assert result.slices == []
    assert result.warnings == ["unsupported_form"]


# --- FMP parsing ------------------------------------------------------------


def _write_payload(tmp_path: Path, name: str, payload: dict[str, object]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parse_payload_reads_declared_form_over_filename(tmp_path: Path) -> None:
    """NU's payloads are named form_10k but declare 20-F; the declaration wins."""
    path = _write_payload(
        tmp_path,
        "NU_form_10k_2022.json",
        {
            "symbol": "NU",
            "period": "FY",
            "year": "2022",
            "Cover": [
                {"Document Type": ["20-F"]},
                {"Document Period End Date": ["Dec. 31,  2022"]},
                {"Document Fiscal Year Focus": ["2022"]},
            ],
            "Segment information": [{"Revenue": ["1,234", "1,000"]}],
        },
    )
    parsed = fmp_sections.parse_payload(path)
    assert parsed.meta.declared_form is FilingForm.FORM_20F
    assert parsed.meta.period_end == datetime(2022, 12, 31)
    hint = fmp_sections.parse_filename(path)
    assert hint is not None and hint.form is FilingForm.FORM_10K


def test_parse_payload_raises_on_shape_drift(tmp_path: Path) -> None:
    path = tmp_path / "META_form_10k_2025.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(SourceContractError, match="expected a JSON object"):
        fmp_sections.parse_payload(path)


def test_parse_payload_raises_on_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "META_form_10k_2025.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SourceContractError, match="invalid JSON"):
        fmp_sections.parse_payload(path)


def test_parse_filename_ignores_non_filing_payloads(tmp_path: Path) -> None:
    assert fmp_sections.parse_filename(tmp_path / "META_income_statement_annual.json") is None
    assert fmp_sections.parse_filename(tmp_path / "META_form_10q_2025_Q3.json") is not None


# --- ingest: fiscal-period arithmetic --------------------------------------


@pytest.mark.parametrize(
    ("period_end", "fye_month", "expected"),
    [
        (datetime(2025, 3, 31), 12, FiscalPeriod.Q1),
        (datetime(2025, 6, 30), 12, FiscalPeriod.Q2),
        (datetime(2025, 9, 30), 12, FiscalPeriod.Q3),
        (datetime(2025, 12, 31), 12, FiscalPeriod.Q4),
        # January FYE (RBRK): the fiscal year starts in February, so an April
        # period-end is Q1, not Q2.
        (datetime(2025, 4, 30), 1, FiscalPeriod.Q1),
        (datetime(2026, 1, 31), 1, FiscalPeriod.Q4),
    ],
)
def test_fiscal_period_for(period_end: datetime, fye_month: int, expected: FiscalPeriod) -> None:
    assert ingest.fiscal_period_for(period_end, fye_month, 31) == expected


def test_fiscal_year_labels_by_year_of_fiscal_end() -> None:
    assert ingest.fiscal_year_for(datetime(2025, 12, 31), 12, 31) == 2025
    # RBRK's FY ending Jan 2026 is FY2026, and an April-2025 period-end sits
    # inside it.
    assert ingest.fiscal_year_for(datetime(2025, 4, 30), 1, 31) == 2026


@pytest.mark.parametrize(
    ("period_end", "fye_month", "fye_day", "expected"),
    [
        # AVGO: stored FYE 11-02; actual closes wobble a day either side.
        (datetime(2025, 11, 2), 11, 2, 2025),
        (datetime(2024, 11, 3), 11, 2, 2024),  # one day past the cutoff
        (datetime(2023, 10, 29), 11, 2, 2023),  # a few days before it
        # LITE: stored FYE 06-28; actual closes drift as late as Jul 3.
        (datetime(2024, 6, 29), 6, 28, 2024),
        (datetime(2021, 7, 3), 6, 28, 2021),
        # NVDA: stored FYE 01-25; actual closes drift as late as Jan 31.
        (datetime(2025, 1, 26), 1, 25, 2025),
        (datetime(2021, 1, 31), 1, 25, 2021),
    ],
)
def test_fiscal_year_for_tolerates_52_53_week_wobble(
    period_end: datetime, fye_month: int, fye_day: int, expected: int
) -> None:
    """A 52/53-week fiscal calendar's actual close drifts a few days either
    side of the stored ``(fye_month, fye_day)`` reference every year. A bare
    day-of-year comparison bumped a one-day overshoot into the WRONG fiscal
    year (AVGO's real FY2024 10-K, period end 2024-11-03, landed in FY2025) —
    a genuine cross-source bug, confirmed against FMP's own ``year`` field for
    the same filing agreeing with the tolerant answer, not the strict one."""
    assert ingest.fiscal_year_for(period_end, fye_month, fye_day) == expected


@pytest.mark.parametrize(
    ("period_end", "fye_month", "fye_day", "expected"),
    [
        # A genuine quarterly period-end sits months away from the FYE and
        # must keep taking the plain day-of-year comparison unaffected by the
        # wobble tolerance -- these would ALL be misclassified by a "closest
        # calendar-year FYE" search across adjacent years, which is why the
        # fix is a small tolerance band around the cutoff, not that.
        (datetime(2024, 3, 31), 12, 31, 2024),
        (datetime(2020, 1, 15), 12, 31, 2020),
        (datetime(2019, 12, 31), 12, 31, 2019),
        (datetime(2024, 9, 27), 12, 31, 2024),  # DHR-shaped: Q3, Dec FYE
    ],
)
def test_fiscal_year_for_quarterly_periods_are_unaffected_by_wobble_tolerance(
    period_end: datetime, fye_month: int, fye_day: int, expected: int
) -> None:
    assert ingest.fiscal_year_for(period_end, fye_month, fye_day) == expected


def test_unknown_fiscal_year_end_is_reported_not_assumed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO tracked_companies (ticker, list_type) VALUES ('RBRK', 'portfolio')")
    month, day, known = ingest.fiscal_year_end_for(conn, "RBRK")
    assert (month, day) == (12, 31)
    assert known is False, "an assumed FYE must be flagged so quarter labels aren't trusted blindly"


# --- ingest: FMP lane end to end -------------------------------------------


def test_ingest_fmp_records_source_missing_when_no_payloads(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    report = ingest.ingest_fmp(conn, "NU", tmp_path, fmp_dir=tmp_path)
    assert report.sections_written == 0
    [coverage] = store.get_coverage(conn, "NU")
    assert coverage.status is CoverageStatus.SOURCE_MISSING
    assert coverage.reason_code == "no_cached_payloads"


def test_ingest_fmp_writes_sections_and_coverage(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _write_payload(
        tmp_path,
        "META_form_10k_2025.json",
        {
            "symbol": "META",
            "period": "FY",
            "year": "2025",
            "Cover page": [
                {"Document Type": ["10-K"]},
                {"Document Period End Date": ["Dec. 31,  2025"]},
            ],
            "Revenue": [{"Advertising": ["1", "2"]}, {"Other": ["3", "4"]}],
            "Revenue (Tables)": [{"Disaggregation": ["5", "6"]}],
        },
    )
    report = ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    assert report.documents_ingested == 1
    assert report.sections_written == 3
    sections = store.get_sections(conn, "META")
    stems = {s.section_stem for s in sections}
    assert "revenue" in stems, "Revenue and Revenue (Tables) must share a stem"
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.status is CoverageStatus.OK


def test_ingest_fmp_flags_regime_mismatch_but_keeps_sections(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _write_payload(
        tmp_path,
        "NU_form_10k_2022.json",
        {
            "symbol": "NU",
            "period": "FY",
            "year": "2022",
            "Cover": [
                {"Document Type": ["20-F"]},
                {"Document Period End Date": ["Dec. 31,  2022"]},
            ],
            "Segment information": [{"Revenue": ["1", "2"]}],
        },
    )
    report = ingest.ingest_fmp(conn, "NU", tmp_path, fmp_dir=tmp_path)
    # Cover + Segment information: the cover page is itself a stored section,
    # since changes to it (auditor, shares outstanding, filer status) are signal.
    assert report.sections_written == 2
    assert report.mismatches, "the declared/filename disagreement must be reported"
    sections = store.get_sections(conn, "NU")
    assert all(s.form is FilingForm.FORM_20F for s in sections), "the filing's own declaration wins"
    [coverage] = store.get_coverage(conn, "NU")
    assert coverage.reason_code == "regime_mismatch_resolved_to_declared"


def test_ingest_fmp_withholds_sections_when_declared_form_is_unsupported(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A declared, RECOGNIZED-but-unsupported Document Type (an 8-K) must not
    fall back to trusting the filename the way a genuinely undeclared cover
    section does — DHR's cached "form_10k_2025" payload declares
    ``Document type: 8-K`` and ``Document Period End Date: Jan. 28, 2026``, a
    full month past the real Dec 31 2025 close, and prior to this fix that
    date was silently accepted as the 10-K's own period end."""
    _write_payload(
        tmp_path,
        "DHR_form_10k_2025.json",
        {
            "symbol": "DHR",
            "period": "FY",
            "year": "2025",
            "Cover page": [
                {"Document type": ["8-K"]},
                {"Document Period End Date": ["Jan. 28,  2026"]},
            ],
            "Some section": [{"Line item": ["1"]}],
        },
    )
    report = ingest.ingest_fmp(conn, "DHR", tmp_path, fmp_dir=tmp_path)
    assert report.sections_written == 0
    assert store.get_sections(conn, "DHR") == []
    [coverage] = store.get_coverage(conn, "DHR")
    assert coverage.status is CoverageStatus.REGIME_MISMATCH
    assert coverage.reason_code == "declared_form_unsupported"


def test_ingest_fmp_withdraws_stale_rows_once_form_is_recognized_as_unsupported(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A row written by an earlier, less careful ingest must not survive a
    re-run that now correctly identifies the payload as an unsupported form —
    otherwise ``reconcile_sources`` keeps flagging a cross-source disagreement
    against data this pipeline no longer trusts at all (measured on prod: DHR
    FY2025 kept disagreeing with EDGAR's 2025-12-31 every run until the stale
    ``fmp_rfile`` row itself was withdrawn, not merely stopped from growing)."""
    path = _write_payload(
        tmp_path,
        "DHR_form_10k_2025.json",
        {
            "symbol": "DHR",
            "period": "FY",
            "year": "2025",
            "Cover page": [{"Document Type": ["10-K"]}],
            "Some section": [{"Line item": ["1"]}],
        },
    )
    ingest.ingest_fmp(conn, "DHR", tmp_path, fmp_dir=tmp_path)
    assert store.get_sections(conn, "DHR"), "the well-formed payload must have written rows first"

    # The same payload now declares an 8-K, as if a subsequent fetch replaced
    # the cache with the wrong document under the same filename.
    path.write_text(
        json.dumps(
            {
                "symbol": "DHR",
                "period": "FY",
                "year": "2025",
                "Cover page": [
                    {"Document type": ["8-K"]},
                    {"Document Period End Date": ["Jan. 28,  2026"]},
                ],
                "Some section": [{"Line item": ["1"]}],
            }
        ),
        encoding="utf-8",
    )
    ingest.ingest_fmp(conn, "DHR", tmp_path, fmp_dir=tmp_path)
    assert store.get_sections(conn, "DHR") == [], (
        "the stale rows must be withdrawn, not just frozen"
    )


def test_ingest_fmp_withholds_sections_on_year_mismatch(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _write_payload(
        tmp_path,
        "META_form_10k_2025.json",
        {
            "symbol": "META",
            "period": "FY",
            "year": "2019",
            "Cover page": [{"Document Type": ["10-K"]}, {"Document Fiscal Year Focus": ["2019"]}],
            "Revenue": [{"Advertising": ["1"]}],
        },
    )
    report = ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    assert report.sections_written == 0
    assert store.get_sections(conn, "META") == []
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.status is CoverageStatus.PERIOD_MISMATCH


def test_ingest_fmp_withholds_sections_on_symbol_mismatch(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _write_payload(
        tmp_path,
        "META_form_10k_2025.json",
        {
            "symbol": "GOOGL",
            "period": "FY",
            "year": "2025",
            "Cover page": [{"Document Type": ["10-K"]}],
            "Revenue": [{"Advertising": ["1"]}],
        },
    )
    report = ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    assert report.sections_written == 0
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.reason_code == "symbol_mismatch"


def test_ingest_fmp_records_schema_drift_and_dumps(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    (tmp_path / "META_form_10k_2025.json").write_text("{broken", encoding="utf-8")
    report = ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    assert report.failures
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.status is CoverageStatus.SCHEMA_DRIFT
    dumps = list((tmp_path / ".tmp" / "filing_sections" / "schema_drift").glob("*.json"))
    assert dumps, "a drift incident must leave an artifact for inspection"


def test_ingest_fmp_is_rerunnable(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _write_payload(
        tmp_path,
        "META_form_10k_2025.json",
        {
            "symbol": "META",
            "period": "FY",
            "year": "2025",
            "Cover page": [{"Document Type": ["10-K"]}],
            "Revenue": [{"Advertising": ["1"]}],
        },
    )
    ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    first = conn.execute("SELECT COUNT(*) FROM filing_sections").fetchone()[0]
    ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM filing_sections").fetchone()[0] == first
    assert conn.execute("SELECT COUNT(*) FROM filing_section_coverage").fetchone()[0] == 1


# --- ingest: local exhibits + reconciliation -------------------------------


def test_ingest_local_exhibits_skips_synthetic_pointers(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    conn.execute(
        "INSERT INTO documents (ticker, doc_type, file_path, accession_number, period_end) "
        "VALUES ('NU', 'sec_6k', 'data/historical/sec/NU_companyfacts.json#accn=x', '0001-25-000001', "
        "'2025-03-31 00:00:00')"
    )
    report = ingest.ingest_local_exhibits(conn, "NU", tmp_path)
    assert report.sections_written == 0
    [coverage] = store.get_coverage(conn, "NU")
    assert coverage.reason_code == "synthetic_companfacts_pointer".replace(
        "companfacts", "companyfacts"
    )


def test_ingest_local_exhibits_partitions_real_html(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    exhibit = tmp_path / "data" / "historical" / "sec" / "NU_6k_2025-05-13.html"
    exhibit.parent.mkdir(parents=True, exist_ok=True)
    body = "<p>" + ("Quarterly results commentary prose. " * 30) + "</p>"
    exhibit.write_text(
        f"<html><body><p>RESULTS OF OPERATIONS</p>{body}<p>SEGMENT REVIEW</p>{body}</body></html>",
        encoding="utf-8",
    )
    conn.execute(
        "INSERT INTO documents (ticker, doc_type, file_path, accession_number, filing_date, period_end) "
        "VALUES ('NU', 'sec_6k', 'data/historical/sec/NU_6k_2025-05-13.html', '0001-25-000002', "
        "'2025-05-13', '2025-03-31 00:00:00')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, list_type, fiscal_year_end) VALUES ('NU', 'portfolio', '12-31')"
    )
    report = ingest.ingest_local_exhibits(conn, "NU", tmp_path)
    assert report.sections_written >= 1
    sections = store.get_sections(conn, "NU")
    assert all(s.form is FilingForm.FORM_6K for s in sections)
    assert sections[0].fiscal_period is FiscalPeriod.Q1
    assert all(s.canonical_id is None for s in sections), "6-K has no mandated taxonomy to claim"


def test_reconcile_flags_cross_source_period_disagreement(conn: sqlite3.Connection) -> None:
    store.write_sections(
        conn,
        [
            _section(
                source=SectionSource.FMP_RFILE,
                source_ref="a.json",
                period_end=datetime(2025, 12, 31),
            ),
            _section(
                source=SectionSource.EDGAR_TEXT,
                source_ref="acc-1",
                period_end=datetime(2024, 12, 31),
            ),
        ],
    )
    store.record_coverage(conn, _coverage())
    findings = ingest.reconcile_sources(conn, "META")
    assert findings and "period_end disagreement" in findings[0]
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.reason_code is not None
    assert "cross_source_period_mismatch" in coverage.reason_code


def test_reconcile_is_quiet_when_sources_agree(conn: sqlite3.Connection) -> None:
    store.write_sections(
        conn,
        [
            _section(
                source=SectionSource.FMP_RFILE,
                source_ref="a.json",
                period_end=datetime(2025, 12, 31),
            ),
            _section(
                source=SectionSource.EDGAR_TEXT,
                source_ref="acc-1",
                period_end=datetime(2025, 12, 31),
            ),
        ],
    )
    assert ingest.reconcile_sources(conn, "META") == []


def test_shifted_slices_are_flagged_as_suspect() -> None:
    """A partition whose slices each swallow the NEXT item's header is
    misaligned — every slice attributes one item's prose to another.

    Real case: NVO's 20-F repeats "ITEM 3 KEY INFORMATION" as a running header,
    so the header chain latched onto the wrong occurrences and its Item 2 slice
    contained Item 3's heading. Detection cannot fix the cut, but a flagged gap
    is far better for a language diff than a clean-looking mislabel.

    Exercised against the detector directly: the chain scorer routes around
    duplicated headers in synthetic text, so the shifted state has to be built
    explicitly rather than provoked.
    """
    prose = "Disclosure prose that fills out the body. " * 20
    shifted = [
        edgar_sections.SectionSlice(
            key="Item 1", text=prose + "\nItem 1A. Risk Factors", ordinal=0
        ),
        edgar_sections.SectionSlice(key="Item 1A", text=prose + "\nItem 2. Properties", ordinal=1),
        edgar_sections.SectionSlice(
            key="Item 2", text=prose + "\nItem 3. Legal Proceedings", ordinal=2
        ),
        edgar_sections.SectionSlice(key="Item 3", text=prose, ordinal=3),
    ]
    assert edgar_sections._boundaries_look_suspect(shifted, taxonomy.FORM_10K_ITEMS)

    aligned = [
        edgar_sections.SectionSlice(key="Item 1", text=prose, ordinal=0),
        edgar_sections.SectionSlice(key="Item 1A", text=prose, ordinal=1),
        edgar_sections.SectionSlice(key="Item 2", text=prose, ordinal=2),
        edgar_sections.SectionSlice(key="Item 3", text=prose, ordinal=3),
    ]
    assert not edgar_sections._boundaries_look_suspect(aligned, taxonomy.FORM_10K_ITEMS)


def test_clean_partition_is_not_flagged_suspect() -> None:
    """The check must not fire on a normal filing, or the signal is useless."""
    result = edgar_sections.split_10k(_fake_10k())
    assert "slice_boundaries_suspect" not in result.warnings


def test_partial_coverage_always_names_a_reason(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """ "Degraded, reason unknown" is the state this store exists to prevent:
    a PARTIAL verdict must carry a queryable reason_code, not only free text."""
    _write_payload(
        tmp_path,
        "META_form_10k_2025.json",
        {
            "symbol": "META",
            "period": "FY",
            "year": "2025",
            # No cover section -> a `no_cover_section` note, hence PARTIAL.
            "Revenue": [{"Advertising": ["1", "2"]}],
        },
    )
    ingest.ingest_fmp(conn, "META", tmp_path, fmp_dir=tmp_path)
    [coverage] = store.get_coverage(conn, "META")
    assert coverage.status is CoverageStatus.PARTIAL
    assert coverage.reason_code, "a PARTIAL verdict with no reason_code is not actionable"


def test_reason_code_names_the_most_severe_warning() -> None:
    """Only one warning fits the queryable reason_code column, so it must be
    the one a consumer would act on first.

    Regression: NVO's 20-F emits subsplit_incomplete_item_3 (incomplete),
    slice_boundaries_suspect (WRONG — one item's prose filed under another)
    and missing_risk_factors. Taking the first-appended warning surfaced the
    least severe of the three, so `WHERE reason_code='slice_boundaries_suspect'`
    silently missed the only filings that have the problem.
    """
    assert (
        most_severe_warning(
            ["subsplit_incomplete_item_3", "slice_boundaries_suspect", "missing_risk_factors"]
        )
        == "slice_boundaries_suspect"
    )
    assert most_severe_warning(["missing_mdna", "missing_risk_factors"]) == "missing_risk_factors"
    assert most_severe_warning([]) is None


def test_unranked_warnings_still_surface() -> None:
    """A warning nobody thought to rank must not vanish from reason_code."""
    assert most_severe_warning(["empty_sections:4"]) == "empty_sections:4"
    assert most_severe_warning(["subsplit_incomplete_item_5"]) == "subsplit_incomplete_item_5"


def test_new_integrity_warnings_rank_above_incomplete_and_missing() -> None:
    """The two new WRONG-class warnings (a boundary swallowed a neighboring
    item's disclosure) must outrank the merely-incomplete/missing ones, same
    as ``slice_boundaries_suspect`` -- and defer to it when both fire
    together, since it is the more specific of the two same-class signals.
    Real case: NU's 20-F fires both trivial_section_oversized AND
    slice_starts_mid_sentence at once (Item 4A absorbing Item 3's content)."""
    assert (
        most_severe_warning(["missing_risk_factors", "trivial_section_oversized"])
        == "trivial_section_oversized"
    )
    assert (
        most_severe_warning(["missing_mdna", "slice_starts_mid_sentence"])
        == "slice_starts_mid_sentence"
    )
    assert (
        most_severe_warning(
            ["slice_starts_mid_sentence", "trivial_section_oversized", "part_boundary_unresolved"]
        )
        == "trivial_section_oversized"
    )
    assert (
        most_severe_warning(["trivial_section_oversized", "slice_boundaries_suspect"])
        == "slice_boundaries_suspect"
    )


def test_unresolvable_ticker_degrades_instead_of_aborting_the_batch(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ADR with no direct SEC presence is a coverage fact, not a broken run.

    Regression: NTDOY raised HardStopError from CIK resolution, which aborted a
    38-ticker eval backfill on reaching it — the 20 tickers after it in the
    alphabet never ran.
    """

    def _no_cik(*_args: object, **_kwargs: object) -> list[object]:
        raise TickerNotResolvableError("no CIK for ticker NTDOY")

    monkeypatch.setattr(ingest.edgar_fetch, "list_filings", _no_cik)
    report = ingest.ingest_edgar(conn, "NTDOY", tmp_path)
    assert report.sections_written == 0
    [coverage] = store.get_coverage(conn, "NTDOY")
    assert coverage.status is CoverageStatus.SOURCE_MISSING
    assert coverage.reason_code == "no_cik"
