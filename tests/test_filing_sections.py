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
