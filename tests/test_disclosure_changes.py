"""Tests for the P0 item-level disclosure-change engine (migration 0203).

Weighted toward the failure modes the design doc calls out explicitly
(docs/design/disclosure_change_build_stack.md §P0):

  * a reworded HEADING must not produce a spurious add-plus-remove pair —
    this is the worst failure mode the content-similarity fallback exists to
    prevent, so it gets the most scrutiny here;
  * a genuine removal must actually surface as ``item_removed``, not get
    silently dropped the way a naive forward-only diff would;
  * re-running the whole pipeline is idempotent (unique-key upsert, not
    duplicate accumulation);
  * a ticker with only one period on file emits nothing and does not crash;
  * a missing table is a setup error (``HardStopError``), not a silent empty
    result, unless the caller opts into ``missing_ok``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings import item_diff, section_items, store  # noqa: E402
from filings.item_diff import DisclosureEvent, align_period_pair, diff_ticker_concept  # noqa: E402
from filings.models import (  # noqa: E402
    FilingForm,
    FilingSection,
    FiscalPeriod,
    HardStopError,
    SectionSource,
    sha256_text,
)
from filings.section_items import SectionItem, normalize_heading, split_section  # noqa: E402

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
CREATE TABLE filing_section_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    section_id INTEGER NOT NULL,
    source_ref VARCHAR(255) NOT NULL,
    form VARCHAR(8) NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4) NOT NULL,
    canonical_id VARCHAR(64),
    item_ordinal INTEGER NOT NULL,
    heading TEXT,
    match_key VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    body_sha256 VARCHAR(64) NOT NULL,
    char_len INTEGER NOT NULL,
    extractor_version VARCHAR(32) NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_filing_section_items UNIQUE (section_id, item_ordinal)
);
CREATE TABLE disclosure_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    form VARCHAR(8),
    fiscal_year INTEGER,
    fiscal_period VARCHAR(4),
    prior_fiscal_year INTEGER,
    prior_fiscal_period VARCHAR(4),
    source_ref VARCHAR(255),
    source_doc_id INTEGER,
    canonical_id VARCHAR(64) NOT NULL DEFAULT '',
    subject VARCHAR(255) NOT NULL,
    subject_label TEXT,
    prior_excerpt TEXT,
    current_excerpt TEXT,
    evidence_quote TEXT,
    materiality FLOAT,
    verdict VARCHAR(24) NOT NULL DEFAULT 'unclassified',
    interpretation_md TEXT,
    confidence FLOAT,
    detector_version VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_disclosure_events UNIQUE
        (ticker, event_type, fiscal_year, fiscal_period, canonical_id, subject, detector_version)
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Built from named pieces (never adjacent-literal-concatenated with a `* N`
# in between) so the repeat count applies to exactly the sentence intended,
# not to whatever the parser happens to fold it together with.
_RISK_INTRO = (
    "The following is a summary discussion of the risk factors that could "
    "materially adversely affect our business, financial condition, results "
    "of operations, cash flows, capital resources, or future prospects, and "
    "should be read together with the rest of this report before investing.\n\n"
)
_COMPETITION_HEADING = "We Face Intense Competition For Users And Advertisers"
_COMPETITION_BODY = (
    "We compete with many other companies for the attention of users and advertisers, "
    "and if we fail to compete effectively our revenue and margins could decline "
    "significantly over time. "
) * 3
_IP_HEADING = "We May Not Successfully Protect Our Intellectual Property"
_IP_BODY = (
    "Failure to adequately protect our intellectual property rights could allow "
    "competitors to copy or otherwise obtain our technology without compensation. "
) * 3
_PERSONNEL_HEADING = "Loss Of Key Personnel Could Harm Our Business"
_PERSONNEL_BODY = (
    "The loss of one or more key employees or the inability to attract and retain "
    "qualified personnel could impair our ability to execute our business strategy. "
) * 3

_RISK_TEXT = (
    _RISK_INTRO
    + _COMPETITION_HEADING
    + "\n"
    + _COMPETITION_BODY
    + "\n\n"
    + _IP_HEADING
    + "\n"
    + _IP_BODY
)
# Same competition risk factor, IP risk factor replaced by an unrelated
# personnel risk factor — the two-period fixture for a genuine remove+add.
_RISK_TEXT_V2 = (
    _RISK_INTRO
    + _COMPETITION_HEADING
    + "\n"
    + _COMPETITION_BODY
    + "\n\n"
    + _PERSONNEL_HEADING
    + "\n"
    + _PERSONNEL_BODY
)

_MDNA_INTRO = (
    "This section discusses our results of operations and liquidity position "
    "for the period under review in reasonable detail so that investors can "
    "evaluate our financial performance and condition holistically together "
    "with the accompanying consolidated financial statements.\n\n"
)
_RESULTS_HEADING = "Results of Operations"
_RESULTS_BODY = (
    "Revenue increased year over year driven by growth in our core advertising "
    "business across all major geographic regions. "
) * 3
_LIQUIDITY_HEADING = "Liquidity and Capital Resources"
_LIQUIDITY_BODY = (
    "We believe our existing cash and cash equivalents will be sufficient to meet "
    "our working capital and capital expenditure needs for the next twelve months. "
) * 3

_MDNA_TEXT = (
    _MDNA_INTRO
    + _RESULTS_HEADING
    + "\n"
    + _RESULTS_BODY
    + "\n\n"
    + _LIQUIDITY_HEADING
    + "\n"
    + _LIQUIDITY_BODY
)


def _section(**overrides: object) -> FilingSection:
    payload: dict[str, object] = {
        "ticker": "META",
        "source": SectionSource.EDGAR_TEXT,
        "source_ref": "0001326801-26-000017",
        "form": FilingForm.FORM_10K,
        "fiscal_period": FiscalPeriod.FY,
        "fiscal_year": 2025,
        "section_key_raw": "Item 1A",
        "text": _RISK_TEXT,
        "ordinal": 0,
        "extractor_version": "test_v1",
        "canonical_id": "risk_factors",
    }
    payload.update(overrides)
    return FilingSection.build(**payload)  # type: ignore[arg-type]


def _write_and_fetch(conn: sqlite3.Connection, *sections: FilingSection) -> list[FilingSection]:
    """Persist sections and read them back WITH ids populated (mirrors real usage:
    section_items.to_section_items requires a persisted, DB-rehydrated section)."""
    store.write_sections(conn, list(sections))
    return store.section_timeline(conn, sections[0].ticker, canonical_id=sections[0].canonical_id)


def _item(**overrides: object) -> SectionItem:
    body = str(overrides.get("body", "Body text about a specific risk. " * 6))
    payload: dict[str, object] = {
        "ticker": "META",
        "section_id": 1,
        "source_ref": "acc-1",
        "doc_id": None,
        "form": FilingForm.FORM_10K,
        "fiscal_year": 2025,
        "fiscal_period": FiscalPeriod.FY,
        "canonical_id": "risk_factors",
        "item_ordinal": 0,
        "heading": "A Risk Factor Heading",
        "match_key": "a risk factor heading",
        "body": body,
        "body_sha256": sha256_text(body),
        "char_len": len(body),
        "extractor_version": "test_v1",
    }
    payload.update(overrides)
    return SectionItem(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_heading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Risks Related to Our Business", "risks related to our business"),
        ("1. Risks Related to Our Business", "risks related to our business"),
        ("(a) Competition Risk", "competition risk"),
        ("• Intellectual Property Risk", "intellectual property risk"),
        ("iii. Regulatory Risk", "regulatory risk"),
    ],
)
def test_normalize_heading_strips_enumerators_and_punctuation(raw: str, expected: str) -> None:
    assert normalize_heading(raw) == expected


# ---------------------------------------------------------------------------
# splitting
# ---------------------------------------------------------------------------


def test_split_risk_factors_reuses_the_shared_heuristic() -> None:
    section = _section(text=_RISK_TEXT)
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert result.ok
    assert len(result.items) == 2
    headings = {h for h, _ in result.items}
    assert "We Face Intense Competition For Users And Advertisers" in headings


def test_split_mdna_uses_its_own_subheading_pass() -> None:
    section = _section(
        text=_MDNA_TEXT, canonical_id="mdna", section_key_raw="Item 7", form=FilingForm.FORM_10K
    )
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert result.ok
    assert len(result.items) >= 2
    headings = {h for h, _ in result.items}
    assert "Liquidity and Capital Resources" in headings


def test_split_section_with_no_detectable_items_falls_back_to_whole_section() -> None:
    """Mirrors edgar_sections.split_freeform's honest fallback: a section that
    yields no sub-items still emits ONE item, not zero."""
    flat_text = "Just one long undifferentiated paragraph with no headings at all. " * 10
    section = _section(text=flat_text, canonical_id="mdna", section_key_raw="Item 7")
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert len(result.items) == 1
    assert "whole_section_fallback" in result.warnings


def test_to_section_items_requires_a_persisted_section() -> None:
    section = _section()  # id is None — never written/read back
    result = split_section(section)
    with pytest.raises(HardStopError):
        section_items.to_section_items(section, result)


def test_mdna_split_does_not_mistake_a_table_row_for_a_subheading() -> None:
    """Regression, found spot-checking real META data: an MD&A financial
    table row like "Interest expense(1,165)(715)(446)(63)%(60)%" reads as two
    "words" (one capitalized label, one digit-leading blob) to the shared
    heading heuristic — enough to pass its >=40%-capitalized test and get
    misread as a sub-heading, fragmenting the table into spurious items that
    then churn every period purely because the FIGURES change."""
    text = (
        _MDNA_INTRO
        + _RESULTS_HEADING
        + "\n"
        + _RESULTS_BODY
        + "\n"
        + "Interest expense(1,165)(715)(446)(63)%(60)%\n"
        + "Foreign currency exchange gains (losses), net352 (690)(366)151 %(89)%\n"
        + _RESULTS_BODY
    )
    section = _section(text=text, canonical_id="mdna", section_key_raw="Item 7")
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert result.ok
    headings = {h for h, _ in result.items}
    assert "Interest expense(1,165)(715)(446)(63)%(60)%" not in headings
    assert len(result.items) == 1
    assert result.items[0][0] == _RESULTS_HEADING


#: A real TOC entry, once its HTML table's title/page-number cells flatten
#: with no separating whitespace, glues into one token ("Item1ARiskFactors9")
#: — too few "words" to look like a heading itself, so it reads as BODY text
#: under the "Table of Contents" line above it, easily clearing the 60-char
#: floor.
_TOC_BODY = (
    "Item1ARiskFactors9\n"
    "Item1BUnresolvedStaffComments25\n"
    "Item2Properties26\n"
    "Item3LegalProceedings26\n"
)


def test_split_risk_factors_rejects_a_table_of_contents_heading() -> None:
    """Regression for the disclosure_events collision bug: "Table of
    Contents" reads as a heading to the shared heuristic (short, capitalized,
    header-shaped) same as any real risk-factor title, and its own garbled
    entries clear the body-length floor — producing a spurious "table of
    contents" item that then collided with the identical item MD&A's split
    produced for the same filing, before migration 0203 added canonical_id
    to disclosure_events' row identity."""
    text = (
        "Table of Contents\n" + _TOC_BODY + "\n" + _COMPETITION_HEADING + "\n" + _COMPETITION_BODY
    )
    section = _section(text=text)
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert result.ok
    headings = {h for h, _ in result.items}
    assert "Table of Contents" not in headings


def test_split_mdna_rejects_a_table_of_contents_heading() -> None:
    """Same guard, exercised through _split_mdna_items's own pass rather than
    split_risk_factors — both reuse _looks_like_risk_heading, but they are
    separate functions and a fix to one does not guarantee the other."""
    text = "Table of Contents\n" + _TOC_BODY + "\n" + _RESULTS_HEADING + "\n" + _RESULTS_BODY
    section = _section(text=text, canonical_id="mdna", section_key_raw="Item 7")
    section = section.model_copy(update={"id": 1})
    result = split_section(section)
    assert result.ok
    headings = {h for h, _ in result.items}
    assert "Table of Contents" not in headings


# ---------------------------------------------------------------------------
# alignment — the core classification logic
# ---------------------------------------------------------------------------


def test_identical_bodies_emit_nothing() -> None:
    body = "Identical risk factor prose that never changed. " * 5
    prior = [_item(body=body, match_key="stable heading", heading="Stable Heading")]
    current = [_item(body=body, match_key="stable heading", heading="Stable Heading")]
    assert align_period_pair(prior, current) == []


def test_same_match_key_different_body_is_reworded() -> None:
    prior = [_item(body="Old body text about risk. " * 5, match_key="same heading")]
    current = [
        _item(body="New body text about a different angle on risk. " * 5, match_key="same heading")
    ]
    events = align_period_pair(prior, current)
    assert len(events) == 1
    assert events[0].event_type == "item_reworded"
    assert events[0].verdict == "unclassified"


_TABLE_BODY_2024 = (
    "Year Ended December 31, (in millions)20242023 Depreciation and amortization$591 $504 "
    "17.3 % Interest expense$(1,295)$(897)44.3 % Total revenues2.5 %2.4 % Other countries44.1 "
    "26.1 Total consolidated101.5 %73.2 %"
)
_TABLE_BODY_2025 = (
    "Year Ended December 31, (in millions)20252024 Depreciation and amortization$655 $591 "
    "10.8 % Interest expense$(1,402)$(1,295)8.3 % Total revenues2.7 %2.5 % Other countries51.3 "
    "44.1 Total consolidated118.9 %101.5 %"
)


def test_tabular_items_emit_no_event_at_all() -> None:
    """Top-of-funnel gate: extracted table content never becomes an event.

    Two periods of the SAME financial table differ every quarter purely because
    the figures moved — which is why these defeated body-level dedupe and grew
    to ~36% of all events. Numeric change is the facts/XBRL pipeline's job; this
    detector diffs narrative. Nothing here is a disclosure change.
    """
    prior = [_item(body=_TABLE_BODY_2024, match_key="results of operations")]
    current = [_item(body=_TABLE_BODY_2025, match_key="results of operations")]
    assert align_period_pair(prior, current) == []


def test_tabular_item_is_suppressed_on_add_and_on_remove() -> None:
    """The gate applies to every emit path, not just rewording."""
    table = [_item(body=_TABLE_BODY_2025, match_key="a table that appeared")]
    assert align_period_pair([], table) == [], "added table must not emit"
    assert align_period_pair(table, []) == [], "removed table must not emit"


def test_narrative_prose_still_emits_after_the_tabular_gate() -> None:
    """The gate must not swallow real narrative drift — including prose that
    legitimately cites figures, which is exactly the content the detector is
    for. A regression here is silent signal loss."""
    prior = [
        _item(
            body=(
                "Our Brazil credit portfolio grew during fiscal 2024 and delinquency "
                "formation in the youngest vintages remained within our underwriting "
                "tolerance across the period we reviewed."
            ),
            match_key="credit quality",
        )
    ]
    current = [
        _item(
            body=(
                "Our Brazil credit portfolio contracted during fiscal 2025 and delinquency "
                "formation in the youngest vintages exceeded our underwriting tolerance, "
                "prompting a tightening of origination standards."
            ),
            match_key="credit quality",
        )
    ]
    events = align_period_pair(prior, current)
    assert len(events) == 1
    assert events[0].event_type == "item_reworded"


def test_reworded_heading_does_not_produce_spurious_add_and_remove() -> None:
    """THE failure mode this engine exists to prevent: a heading edit alone
    must not look like one item vanishing and an unrelated one appearing."""
    shared_body = (
        "We compete with many other companies for the attention of users and "
        "advertisers, and if we fail to compete effectively our revenue and "
        "margins could decline significantly over time. " * 4
    )
    prior = [
        _item(
            heading="We Face Intense Competition For Users And Advertisers",
            match_key=normalize_heading("We Face Intense Competition For Users And Advertisers"),
            body=shared_body,
        )
    ]
    current = [
        _item(
            heading="Our Business Faces Significant Competitive Pressure",
            match_key=normalize_heading("Our Business Faces Significant Competitive Pressure"),
            # Body picks up one extra sentence but keeps almost all of its
            # vocabulary — the realistic shape of a heading-only rewording.
            body=shared_body + " This risk has intensified in recent periods.",
        )
    ]
    events = align_period_pair(prior, current)
    assert len(events) == 1, f"expected exactly one reworded event, got {events}"
    assert events[0].event_type == "item_reworded"
    assert events[0].evidence_quote


def test_genuinely_unrelated_items_still_produce_add_and_remove() -> None:
    """The similarity fallback must not OVER-collapse: two items about
    genuinely different risks, even sharing generic boilerplate language,
    should still land as an independent remove + add."""
    prior = [
        _item(
            heading="Currency Fluctuation Risk",
            match_key=normalize_heading("Currency Fluctuation Risk"),
            body=(
                "Adverse movements in foreign exchange rates could adversely "
                "affect our business, financial condition and results of "
                "operations in ways we cannot fully hedge against."
            ),
        )
    ]
    current = [
        _item(
            heading="Cybersecurity Incident Risk",
            match_key=normalize_heading("Cybersecurity Incident Risk"),
            body=(
                "A significant data breach or cybersecurity incident could "
                "adversely affect our business, financial condition and "
                "results of operations and damage our reputation with users."
            ),
        )
    ]
    events = align_period_pair(prior, current)
    types = sorted(e.event_type for e in events)
    assert types == ["item_added", "item_removed"]


def test_genuine_removal_is_detected() -> None:
    prior = [
        _item(heading="Old Standalone Risk", match_key="old standalone risk", body="Body A. " * 10),
        _item(
            heading="Surviving Risk",
            match_key="surviving risk",
            body="Body B stays the same. " * 10,
            item_ordinal=1,
        ),
    ]
    current = [
        _item(
            heading="Surviving Risk",
            match_key="surviving risk",
            body="Body B stays the same. " * 10,
            item_ordinal=1,
        ),
    ]
    events = align_period_pair(prior, current)
    assert len(events) == 1
    assert events[0].event_type == "item_removed"
    assert events[0].subject_label == "Old Standalone Risk"
    assert events[0].evidence_quote


def test_genuine_addition_is_detected() -> None:
    prior = [_item(heading="Existing Risk", match_key="existing risk", body="Body A. " * 10)]
    current = [
        _item(heading="Existing Risk", match_key="existing risk", body="Body A. " * 10),
        _item(
            heading="Brand New Risk Never Seen Before",
            match_key="brand new risk never seen before",
            body="A completely new disclosure about a risk not previously reported. " * 5,
            item_ordinal=1,
        ),
    ]
    events = align_period_pair(prior, current)
    assert len(events) == 1
    assert events[0].event_type == "item_added"
    assert events[0].subject_label == "Brand New Risk Never Seen Before"


def test_single_period_emits_no_events(conn: sqlite3.Connection) -> None:
    _write_and_fetch(conn, _section())
    items, events = diff_ticker_concept(conn, "META", "risk_factors")
    assert events == []
    assert len(items) == 2  # the two risk factors split out of the lone period


def test_ticker_with_no_sections_emits_nothing_and_does_not_crash(conn: sqlite3.Connection) -> None:
    items, events = diff_ticker_concept(conn, "NOPE", "risk_factors")
    assert items == []
    assert events == []


# ---------------------------------------------------------------------------
# end-to-end pipeline + idempotency
# ---------------------------------------------------------------------------


def _two_period_fixture(conn: sqlite3.Connection) -> None:
    """Competition risk factor is untouched across periods (unchanged, emits
    nothing); the IP risk factor is dropped and an unrelated personnel risk
    factor appears — a genuine remove + a genuine add, not a reword."""
    store.write_sections(
        conn,
        [
            _section(source_ref="acc-2024", fiscal_year=2024, text=_RISK_TEXT),
            _section(source_ref="acc-2025", fiscal_year=2025, text=_RISK_TEXT_V2),
        ],
    )


def test_diff_ticker_concept_detects_a_genuine_removal_and_addition(
    conn: sqlite3.Connection,
) -> None:
    _two_period_fixture(conn)
    items, events = diff_ticker_concept(conn, "META", "risk_factors")
    assert items  # both periods' items are returned for persistence
    types = sorted(e.event_type for e in events)
    # The competition risk factor is untouched (unchanged, emits nothing);
    # the IP risk factor is replaced by an unrelated key-personnel risk factor.
    assert "item_removed" in types
    assert "item_added" in types
    for e in events:
        assert e.evidence_quote
        assert e.verdict == "unclassified"


def test_rerunning_the_pipeline_is_idempotent(conn: sqlite3.Connection) -> None:
    _two_period_fixture(conn)
    items1, events1 = diff_ticker_concept(conn, "META", "risk_factors")
    section_items.write_items(conn, items1)
    item_diff.write_events(conn, events1)
    conn.commit()

    items2, events2 = diff_ticker_concept(conn, "META", "risk_factors")
    section_items.write_items(conn, items2)
    item_diff.write_events(conn, events2)
    conn.commit()

    item_count = conn.execute("SELECT COUNT(*) FROM filing_section_items").fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM disclosure_events").fetchone()[0]
    assert item_count == len(items1)
    assert event_count == len(events1)


def _event(**overrides: object) -> DisclosureEvent:
    payload: dict[str, object] = {
        "ticker": "META",
        "event_type": "item_added",
        "form": FilingForm.FORM_10K,
        "fiscal_year": 2025,
        "fiscal_period": FiscalPeriod.FY,
        "prior_fiscal_year": None,
        "prior_fiscal_period": None,
        "source_ref": "acc-1",
        "source_doc_id": None,
        "canonical_id": "risk_factors",
        "subject": "table of contents",
        "subject_label": "Table of Contents",
        "prior_excerpt": None,
        "current_excerpt": "some excerpt",
        "evidence_quote": "some excerpt",
    }
    payload.update(overrides)
    return DisclosureEvent(**payload)  # type: ignore[arg-type]


def test_write_events_keeps_the_same_subject_distinct_across_sections(
    conn: sqlite3.Connection,
) -> None:
    """Regression for the data-loss bug fixed by migration 0203: the same
    normalized heading ("table of contents", or any other phrase) can appear
    as an item in BOTH the risk-factors and MD&A sections of the same
    filing. Those are two distinct findings and must land as two distinct
    rows, not collide under a unique key that omits canonical_id."""
    risk_event = _event(canonical_id="risk_factors")
    mdna_event = _event(canonical_id="mdna")
    written = item_diff.write_events(conn, [risk_event, mdna_event])
    conn.commit()

    assert written == 2
    rows = conn.execute(
        "SELECT canonical_id FROM disclosure_events WHERE subject = 'table of contents' "
        "ORDER BY canonical_id"
    ).fetchall()
    assert [r[0] for r in rows] == ["mdna", "risk_factors"]


def test_write_events_still_dedupes_the_same_subject_within_one_section(
    conn: sqlite3.Connection,
) -> None:
    """The canonical_id fix must not turn the upsert into an always-insert:
    re-running write_events for the SAME section still updates the existing
    row rather than accumulating a duplicate."""
    first = item_diff.write_events(conn, [_event(canonical_id="risk_factors")])
    conn.commit()
    second = item_diff.write_events(
        conn, [_event(canonical_id="risk_factors", current_excerpt="updated excerpt")]
    )
    conn.commit()

    assert first == 1
    assert second == 1
    rows = conn.execute(
        "SELECT current_excerpt FROM disclosure_events WHERE subject = 'table of contents'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "updated excerpt"


def test_write_events_coerces_a_null_canonical_id_to_the_empty_sentinel(
    conn: sqlite3.Connection,
) -> None:
    """disclosure_events.canonical_id is NOT NULL DEFAULT '' (migration
    0203) precisely because SQLite treats two NULLs in a UNIQUE index as
    distinct — a raw None here would silently stop deduplicating every event
    for a concept this module has no canonical_id for."""
    event = _event(canonical_id=None, subject="a metric lifecycle change")
    item_diff.write_events(conn, [event])
    conn.commit()
    item_diff.write_events(conn, [event])
    conn.commit()

    rows = conn.execute(
        "SELECT canonical_id FROM disclosure_events WHERE subject = 'a metric lifecycle change'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == ""


def test_write_items_prunes_ordinals_a_re_split_no_longer_produces(
    conn: sqlite3.Connection,
) -> None:
    """A splitter change (e.g. a heuristic fix) that yields FEWER items for a
    section must not leave the excess higher-ordinal rows behind — mirrors
    store._prune_superseded's reasoning for filing_sections."""
    [section] = _write_and_fetch(conn, _section())
    section_items.write_items(
        conn,
        [
            section_items.SectionItem(
                ticker="META",
                section_id=section.id,  # type: ignore[arg-type]
                source_ref=section.source_ref,
                doc_id=None,
                form=section.form,
                fiscal_year=section.fiscal_year,
                fiscal_period=section.fiscal_period,
                canonical_id=section.canonical_id,
                item_ordinal=ordinal,
                heading=f"Heading {ordinal}",
                match_key=f"heading {ordinal}",
                body=f"Body {ordinal}. " * 10,
                body_sha256=sha256_text(f"Body {ordinal}. " * 10),
                char_len=len(f"Body {ordinal}. " * 10),
            )
            for ordinal in range(5)
        ],
    )
    assert conn.execute("SELECT COUNT(*) FROM filing_section_items").fetchone()[0] == 5

    # Re-split the SAME section and it now yields only 2 items (ordinals 0-1).
    section_items.write_items(
        conn,
        [
            section_items.SectionItem(
                ticker="META",
                section_id=section.id,  # type: ignore[arg-type]
                source_ref=section.source_ref,
                doc_id=None,
                form=section.form,
                fiscal_year=section.fiscal_year,
                fiscal_period=section.fiscal_period,
                canonical_id=section.canonical_id,
                item_ordinal=ordinal,
                heading=f"New Heading {ordinal}",
                match_key=f"new heading {ordinal}",
                body=f"New body {ordinal}. " * 10,
                body_sha256=sha256_text(f"New body {ordinal}. " * 10),
                char_len=len(f"New body {ordinal}. " * 10),
            )
            for ordinal in range(2)
        ],
    )
    remaining = conn.execute(
        "SELECT item_ordinal FROM filing_section_items ORDER BY item_ordinal"
    ).fetchall()
    assert [r[0] for r in remaining] == [0, 1]


# ---------------------------------------------------------------------------
# missing-table hard stops
# ---------------------------------------------------------------------------


def test_get_items_raises_hard_stop_when_table_missing() -> None:
    bare = sqlite3.connect(":memory:")
    with pytest.raises(HardStopError):
        section_items.get_items(bare, "META")


def test_get_items_degrades_when_missing_ok() -> None:
    bare = sqlite3.connect(":memory:")
    assert section_items.get_items(bare, "META", missing_ok=True) == []


def test_get_events_raises_hard_stop_when_table_missing() -> None:
    bare = sqlite3.connect(":memory:")
    with pytest.raises(HardStopError):
        item_diff.get_events(bare, "META")


def test_get_events_degrades_when_missing_ok() -> None:
    bare = sqlite3.connect(":memory:")
    assert item_diff.get_events(bare, "META", missing_ok=True) == []


def test_write_items_raises_hard_stop_when_table_missing() -> None:
    bare = sqlite3.connect(":memory:")
    with pytest.raises(HardStopError):
        section_items.write_items(bare, [_item()])


def test_write_events_raises_hard_stop_when_table_missing() -> None:
    bare = sqlite3.connect(":memory:")
    event = DisclosureEvent(
        ticker="META",
        event_type="item_added",
        form=FilingForm.FORM_10K,
        fiscal_year=2025,
        fiscal_period=FiscalPeriod.FY,
        prior_fiscal_year=None,
        prior_fiscal_period=None,
        source_ref="acc-1",
        source_doc_id=None,
        canonical_id="risk_factors",
        subject="new risk",
        subject_label="New Risk",
        prior_excerpt=None,
        current_excerpt="New risk excerpt",
        evidence_quote="New risk excerpt",
    )
    with pytest.raises(HardStopError):
        item_diff.write_events(bare, [event])
