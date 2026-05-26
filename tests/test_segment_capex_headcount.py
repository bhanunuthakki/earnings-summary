"""Tests for capex + headcount per-segment extraction and rendering.

Covers four layers of the metric extension:

1. The 10-K segment-note extractor recognizes capex and headcount label
   variants and emits SegmentFact rows with the right Unit + currency policy.
2. The junction writer translates legacy capex/headcount metrics into the
   correct (dim_type, metric) shape.
3. The §4 Segments section builder surfaces the two new buckets when
   segment_facts has matching rows.
4. An AMZN-shaped synthetic fixture round-trips: "Property and equipment
   additions" rows under AWS / International / NA become capex facts with
   per-segment, per-period values.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute.segment_oi_10k import (
    _is_capex_label,
    _is_headcount_label,
    extract_segment_oi_facts,
    extract_segment_oi_from_record,
)
from models.facts import (
    Currency,
    FiscalPeriodType,
    SegmentDimType,
    Unit,
)
from pipeline.segment_junction_writer import (
    segment_fact_to_dimension,
    write_segment_facts_junction,
)
from report.models import SectionStatus
from report.sections.segments import build as build_segments_section

_NBSP = "\xa0"


# ---------------------------------------------------------------------------
# Label matchers
# ---------------------------------------------------------------------------


def test_capex_label_recognizes_common_variants() -> None:
    """All canonical 10-K capex labels match the prefix list."""
    assert _is_capex_label("Capital expenditures")
    assert _is_capex_label("Capital expenditure")  # singular variant
    assert _is_capex_label("Property and equipment additions")  # AMZN convention
    assert _is_capex_label("Purchases of property, plant and equipment")
    assert _is_capex_label("Additions to long-lived assets")
    assert _is_capex_label("Capital additions")


def test_capex_label_rejects_non_capex_property_lines() -> None:
    """Stock-style balance sheet rows must NOT match — they're not flow capex."""
    # "Property and equipment, net" is the net PP&E book value, not capex.
    assert not _is_capex_label("Property and equipment, net")
    assert not _is_capex_label("Total revenues")
    assert not _is_capex_label("Operating income (loss)")


def test_headcount_label_recognizes_common_variants() -> None:
    assert _is_headcount_label("Employees")
    assert _is_headcount_label("Full-time employees")
    assert _is_headcount_label("Headcount")
    assert _is_headcount_label("Associates")
    assert _is_headcount_label("Number of employees")
    assert _is_headcount_label("Total employees")


def test_headcount_label_rejects_employee_benefits() -> None:
    """'Employee benefits' is a comp expense line, not a count."""
    assert not _is_headcount_label("Employee benefits")
    assert not _is_headcount_label("Employee compensation")


# ---------------------------------------------------------------------------
# Extractor — capex
# ---------------------------------------------------------------------------


def test_extractor_emits_capex_facts_per_segment() -> None:
    """A capex section emits one fact per (segment, period) with metric='capex'."""
    record: dict[str, object] = {
        "Segment Information - Reconciliation of Property and Equipment Additions": [
            {
                "Segment Information - Reconciliation of Property and Equipment "
                "Additions (Details) - USD ($) $ in Millions": ["12 Months Ended"]
            },
            {"items": ["Dec. 31, 2024", "Dec. 31, 2023"]},
            # Consolidated total row before any segment header — silently dropped.
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Property and equipment additions": [85_752, 48_344]},
            {"Operating Segments | North America": [_NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Property and equipment additions": [24_348, 17_529]},
            {"Operating Segments | International": [_NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Property and equipment additions": [6_643, 4_144]},
            {"Operating Segments | AWS": [_NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP]},
            {"Property and equipment additions": [53_267, 24_843]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=42, ticker="AMZN")
    capex_facts = [f for f in facts if f.metric == "capex"]
    by_key = {(f.segment_name, f.period_end): f for f in capex_facts}

    assert ("North America", datetime(2024, 12, 31)) in by_key
    assert ("International", datetime(2024, 12, 31)) in by_key
    assert ("AWS", datetime(2024, 12, 31)) in by_key

    # Section title carries "$ in Millions" → values × 1_000_000.
    aws_2024 = by_key[("AWS", datetime(2024, 12, 31))]
    assert aws_2024.value == Decimal("53267000000")
    assert aws_2024.unit == Unit.ACTUAL
    assert aws_2024.fiscal_period_type == FiscalPeriodType.FY
    assert aws_2024.source_doc_id == 42

    # Prior-year column also extracts.
    na_2023 = by_key[("North America", datetime(2023, 12, 31))]
    assert na_2023.value == Decimal("17529000000")

    # Consolidated total ($85,752M) appeared BEFORE any segment header — must
    # not have leaked into the emitted facts.
    assert not any(f.value == Decimal("85752000000") for f in capex_facts)


def test_extractor_handles_capital_expenditures_label() -> None:
    """A filer using 'Capital expenditures' instead of AMZN's label still emits capex."""
    record: dict[str, object] = {
        "Segment Capital Investment": [
            {"Segment Capital Investment - $ in Millions": ["12 Months Ended"]},
            {"items": ["Dec. 31, 2024"]},
            {"Cloud Services": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Capital expenditures": [12_000]},
            {"Consumer Hardware": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Capital expenditures": [3_500]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=1, ticker="X")
    capex = {f.segment_name: f.value for f in facts if f.metric == "capex"}
    assert capex == {
        "Cloud Services": Decimal("12000000000"),
        "Consumer Hardware": Decimal("3500000000"),
    }


# ---------------------------------------------------------------------------
# Extractor — headcount
# ---------------------------------------------------------------------------


def test_extractor_emits_headcount_facts_as_count_unit() -> None:
    """Headcount rows emit Unit.COUNT and are NOT multiplied by the section scale.

    A section labeled "$ in Millions" must not turn 150,000 employees into
    150B people — headcount ignores the section scale by design.
    """
    record: dict[str, object] = {
        "Segment Human Capital": [
            {"Segment Human Capital (Details) - $ in Millions": ["12 Months Ended"]},
            {"items": ["Dec. 31, 2024"]},
            {"Cloud Services": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Employees": [150_000]},
            {"Retail": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Employees": [80_000]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=1, ticker="X")
    headcount = [f for f in facts if f.metric == "headcount"]
    assert len(headcount) == 2
    by_seg = {f.segment_name: f for f in headcount}
    assert by_seg["Cloud Services"].value == Decimal("150000")
    assert by_seg["Cloud Services"].unit == Unit.COUNT
    assert by_seg["Cloud Services"].currency is None
    assert by_seg["Retail"].value == Decimal("80000")


def test_extractor_emits_capex_and_headcount_together_under_same_segment() -> None:
    """A section that mixes both metrics emits both facts per segment."""
    record: dict[str, object] = {
        "Segment Mix": [
            {"Segment Mix - $ in Thousands": ["12 Months Ended"]},
            {"items": ["Dec. 31, 2024"]},
            {"AWS": [_NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP]},
            {"Capital expenditures": [50_000]},
            {"Employees": [120_000]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=1, ticker="AMZN")
    by_metric = {f.metric: f for f in facts if f.segment_name == "AWS"}
    assert "capex" in by_metric
    assert "headcount" in by_metric
    # Capex scaled by Thousands (×1000)
    assert by_metric["capex"].value == Decimal("50000000")
    # Headcount NOT scaled
    assert by_metric["headcount"].value == Decimal("120000")
    assert by_metric["headcount"].unit == Unit.COUNT


# ---------------------------------------------------------------------------
# Junction writer mapping
# ---------------------------------------------------------------------------


def test_segment_fact_to_dimension_maps_capex() -> None:
    """capex → BUSINESS_UNIT + metric='capex' (no _by_segment suffix on the wire)."""
    d = segment_fact_to_dimension("AWS", "capex", Decimal("53_267_000_000"))
    assert d.dim_type == SegmentDimType.BUSINESS_UNIT
    assert d.metric == "capex"
    assert d.dim_name == "AWS"
    assert d.value == Decimal("53267000000")


def test_segment_fact_to_dimension_maps_headcount() -> None:
    d = segment_fact_to_dimension("Cloud Services", "headcount", Decimal("150000"))
    assert d.dim_type == SegmentDimType.BUSINESS_UNIT
    assert d.metric == "headcount"
    assert d.dim_name == "Cloud Services"


# ---------------------------------------------------------------------------
# Junction writer round-trip — value + unit preserved
# ---------------------------------------------------------------------------


def _create_junction_schema(conn: sqlite3.Connection) -> None:
    """Schema mirroring migrations 0004 + 0055 + 0057 for in-memory junction tests.

    Includes the per-dim `unit` column added by 0057 so the writer can store
    headcount (Unit.COUNT) alongside ACTUAL-unit dims under one period
    anchor."""
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL,
            source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
        );
        CREATE TABLE segment_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type VARCHAR(8) NOT NULL,
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            currency VARCHAR(8),
            unit VARCHAR(16) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_segment_periods_provenance UNIQUE
              (ticker, period_end, fiscal_period_type, source_doc_id)
        );
        CREATE TABLE segment_dimensions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL REFERENCES segment_periods(id),
            dim_type VARCHAR(16) NOT NULL,
            dim_name VARCHAR(128) NOT NULL,
            value NUMERIC(20, 4) NOT NULL,
            metric VARCHAR(32) NOT NULL,
            unit VARCHAR(16),
            segment_entity_id INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (1, 'AMZN', 'fmp', 'fmp_10k_json', 'x.json', '0', "
        " CURRENT_TIMESTAMP, 'ok', 1)"
    )
    conn.commit()


@pytest.fixture
def junction_conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_junction_schema(c)
    return c


def test_junction_writer_round_trips_capex_actual(
    junction_conn: sqlite3.Connection,
) -> None:
    """A capex SegmentDimension survives a write round-trip with Unit.ACTUAL."""
    capex_dim = segment_fact_to_dimension("AWS", "capex", Decimal("53_267_000_000"))
    write_segment_facts_junction(
        junction_conn,
        ticker="AMZN",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.FY,
        source_doc_id=1,
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        dimensions=[capex_dim],
    )
    junction_conn.commit()
    row = junction_conn.execute(
        "SELECT sd.metric, sd.dim_type, sd.dim_name, sd.value, sp.unit "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sd.period_id = sp.id"
    ).fetchone()
    assert row["metric"] == "capex"
    assert row["dim_type"] == "business_unit"
    assert row["dim_name"] == "AWS"
    assert str(row["value"]) == "53267000000"
    assert row["unit"] == "actual"


def test_junction_writer_round_trips_headcount_count(
    junction_conn: sqlite3.Connection,
) -> None:
    """A headcount SegmentDimension survives a write round-trip with Unit.COUNT.

    Headcount uses a different period anchor (Unit.COUNT, currency=None) from
    capex, so each is written under its own segment_periods row.
    """
    hc_dim = segment_fact_to_dimension("AWS", "headcount", Decimal("120000"))
    write_segment_facts_junction(
        junction_conn,
        ticker="AMZN",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.FY,
        source_doc_id=1,
        currency=None,
        unit=Unit.COUNT,
        dimensions=[hc_dim],
    )
    junction_conn.commit()
    row = junction_conn.execute(
        "SELECT sd.metric, sd.value, sp.unit, sp.currency "
        "FROM segment_dimensions sd JOIN segment_periods sp ON sd.period_id = sp.id"
    ).fetchone()
    assert row["metric"] == "headcount"
    assert str(row["value"]) == "120000"
    assert row["unit"] == "count"
    assert row["currency"] is None


# ---------------------------------------------------------------------------
# Renderer — capex + headcount buckets populate from segment_facts
# ---------------------------------------------------------------------------


def _seed_renderer_repo(tmp_path: Path) -> Path:
    """Build a tmp repo_root with portfolio.db + junction schema seeded.

    Bootstraps the post-0057 schema (segment_periods + segment_dimensions
    with per-dim `unit`), then seeds one period anchor per (ticker, quarter,
    source_doc) tuple plus capex + headcount dim cells under each. Returns
    the repo_root path for build().
    """
    (tmp_path / "data").mkdir()
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _create_junction_schema(conn)
        # _create_junction_schema seeds a placeholder document with id=1 —
        # reuse it as the source for every fact row here.
        period_ids: dict[tuple[str, str], int] = {}
        for q, pe in (
            ("Q1", "2024-03-31"),
            ("Q2", "2024-06-30"),
            ("Q3", "2024-09-30"),
            ("Q4", "2024-12-31"),
        ):
            cur = conn.execute(
                "INSERT INTO segment_periods "
                "(ticker, period_end, fiscal_period_type, source_doc_id, "
                " currency, unit) VALUES ('AMZN', ?, ?, 1, 'USD', 'actual')",
                (pe, q),
            )
            assert cur.lastrowid is not None
            period_ids[(q, pe)] = cur.lastrowid

        dim_rows: list[tuple[int, str, str, str, str, str | None]] = []
        for segment in ("AWS", "North America", "International"):
            for q, pe, val in [
                ("Q1", "2024-03-31", 10_000_000_000),
                ("Q2", "2024-06-30", 11_000_000_000),
                ("Q3", "2024-09-30", 12_000_000_000),
                ("Q4", "2024-12-31", 13_000_000_000),
            ]:
                # ACTUAL-unit capex matches the period's unit → unit=NULL
                # (inherit).
                dim_rows.append(
                    (period_ids[(q, pe)], "business_unit", segment, str(val), "capex", None)
                )
            for q, pe, val in [
                ("Q1", "2024-03-31", 150_000),
                ("Q2", "2024-06-30", 152_000),
                ("Q3", "2024-09-30", 155_000),
                ("Q4", "2024-12-31", 158_000),
            ]:
                # COUNT headcount differs from the period's ACTUAL → store
                # the override on the dim row.
                dim_rows.append(
                    (period_ids[(q, pe)], "business_unit", segment, str(val), "headcount", "count")
                )
        conn.executemany(
            "INSERT INTO segment_dimensions "
            "(period_id, dim_type, dim_name, value, metric, unit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            dim_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def test_renderer_surfaces_capex_by_segment(tmp_path: Path) -> None:
    """When segment_facts has capex rows, SegmentsSection.capex_by_segment fills in."""
    repo_root = _seed_renderer_repo(tmp_path)
    section = build_segments_section("AMZN", repo_root)
    assert section.status == SectionStatus.OK
    seg_names = {s.segment_name for s in section.capex_by_segment}
    assert seg_names == {"AWS", "North America", "International"}
    # Raw value $13B should display as 13_000 (USD millions)
    aws = next(s for s in section.capex_by_segment if s.segment_name == "AWS")
    assert aws.values[-1] == 13_000.0
    assert aws.unit == "USD millions"


def test_renderer_surfaces_headcount_by_segment_with_count_unit(
    tmp_path: Path,
) -> None:
    """Headcount renders as raw counts (no /1M scaling) with unit='employees'."""
    repo_root = _seed_renderer_repo(tmp_path)
    section = build_segments_section("AMZN", repo_root)
    hc_aws = next(s for s in section.headcount_by_segment if s.segment_name == "AWS")
    # 158_000 employees — raw integer, NOT divided by 1M.
    assert hc_aws.values[-1] == 158_000.0
    assert hc_aws.unit == "employees"


def test_renderer_empty_when_no_capex_or_headcount_rows(tmp_path: Path) -> None:
    """A ticker whose junction has only revenue rows still builds, with
    capex_by_segment and headcount_by_segment as empty lists."""
    (tmp_path / "data").mkdir()
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _create_junction_schema(conn)
        cur = conn.execute(
            "INSERT INTO segment_periods "
            "(ticker, period_end, fiscal_period_type, source_doc_id, currency, unit) "
            "VALUES ('GOOG', '2024-12-31', 'Q4', 1, 'USD', 'actual')"
        )
        period_id = cur.lastrowid
        assert period_id is not None
        conn.executemany(
            "INSERT INTO segment_dimensions "
            "(period_id, dim_type, dim_name, value, metric) "
            "VALUES (?, 'product', ?, ?, 'revenue')",
            [
                (period_id, "Search", "50000000000"),
                (period_id, "Cloud", "10000000000"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    section = build_segments_section("GOOG", tmp_path)
    assert section.capex_by_segment == []
    assert section.headcount_by_segment == []
    assert len(section.revenue_by_product) == 2


# ---------------------------------------------------------------------------
# AMZN fixture — end-to-end extraction shape parity
# ---------------------------------------------------------------------------


def test_extract_segment_oi_facts_writes_capex_to_junction(tmp_path: Path) -> None:
    """End-to-end: extract_segment_oi_facts populates segment_dimensions with
    capex rows for AMZN's primary segments under one period anchor.

    Matches acceptance criterion #2 (post-0057 rewording): running the CLI
    extractor against an AMZN 10-K JSON populates the junction. The legacy
    segment_facts table no longer exists.
    """
    (tmp_path / "data" / "historical" / "fmp").mkdir(parents=True)
    json_path = tmp_path / "data" / "historical" / "fmp" / "AMZN_form_10k_2024.json"
    import json
    json_path.write_text(
        json.dumps({
            "Segment Information - Reconci_3": [
                {
                    "Segment Information - Reconciliation of Property and Equipment "
                    "Additions - $ in Millions": ["12 Months Ended"]
                },
                {"items": ["Dec. 31, 2024"]},
                {"Operating Segments | North America": [_NBSP]},
                {"Segment Reporting Information [Line Items]": [_NBSP]},
                {"Property and equipment additions": [24_348]},
                {"Operating Segments | International": [_NBSP]},
                {"Segment Reporting Information [Line Items]": [_NBSP]},
                {"Property and equipment additions": [6_643]},
                {"Operating Segments | AWS": [_NBSP]},
                {"Segment Reporting Information [Line Items]": [_NBSP]},
                {"Property and equipment additions": [53_267]},
            ]
        }),
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _create_junction_schema(conn)
        # Replace the placeholder document with an AMZN 10-K row pointing at
        # the fixture JSON.
        conn.execute("DELETE FROM documents")
        conn.execute(
            "INSERT INTO documents "
            "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
            " fetch_status, raw_bytes_size, source_quality_tier) "
            "VALUES (7, 'AMZN', 'fmp', 'fmp_10k_json', ?, '0', CURRENT_TIMESTAMP, "
            " 'ok', 1, 'fmp_normalized')",
            (str(json_path.relative_to(tmp_path)),),
        )
        conn.commit()

        inserted = extract_segment_oi_facts(conn, document_id=7, project_root=tmp_path)
        assert inserted > 0

        # Junction populated — same three segments under dim_type=business_unit.
        junction_capex = conn.execute(
            "SELECT sd.dim_name, sd.value FROM segment_dimensions sd "
            "JOIN segment_periods sp ON sd.period_id = sp.id "
            "WHERE sp.ticker='AMZN' AND sd.metric='capex' ORDER BY sd.dim_name"
        ).fetchall()
        junction_segments = {r["dim_name"] for r in junction_capex}
        assert {"AWS", "International", "North America"}.issubset(junction_segments)

        # AWS value matches: $53,267M × 1M = $53.267B.
        aws_junction = next(r for r in junction_capex if r["dim_name"] == "AWS")
        assert str(aws_junction["value"]) == "53267000000"
    finally:
        conn.close()


def test_amzn_shaped_fixture_produces_capex_for_aws_intl_na() -> None:
    """A fixture mirroring AMZN's actual 10-K capex section emits the expected
    per-segment capex for AWS, North America, International — matching the
    acceptance criteria in the task spec.
    """
    # Values match AMZN's actual 2024 10-K segment-capex section (in $M).
    record: dict[str, object] = {
        "Segment Information - Reconci_3": [
            {
                "Segment Information - Reconciliation of Property and Equipment "
                "Additions from Segments to Consolidated (Details) - USD ($) "
                "$ in Millions": ["12 Months Ended"]
            },
            {"items": ["Dec. 31, 2024", "Dec. 31, 2023", "Dec. 31, 2022"]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP, _NBSP]},
            {"Property and equipment additions": [85_752, 48_344, 60_836]},
            {"Operating Segments | North America": [_NBSP, _NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP, _NBSP]},
            {"Property and equipment additions": [24_348, 17_529, 23_682]},
            {"Operating Segments | International": [_NBSP, _NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP, _NBSP]},
            {"Property and equipment additions": [6_643, 4_144, 6_711]},
            {"Operating Segments | AWS": [_NBSP, _NBSP, _NBSP]},
            {"Segment Reporting Information [Line Items]": [_NBSP, _NBSP, _NBSP]},
            {"Property and equipment additions": [53_267, 24_843, 27_755]},
        ]
    }
    facts = extract_segment_oi_from_record(record, source_doc_id=99, ticker="AMZN")
    capex = {
        (f.segment_name, f.period_end.year): f
        for f in facts
        if f.metric == "capex"
    }
    # All three primary segments present for 2024.
    assert ("AWS", 2024) in capex
    assert ("North America", 2024) in capex
    assert ("International", 2024) in capex
    # Non-zero, in raw dollars (×1M from the "$ in Millions" section title).
    assert capex[("AWS", 2024)].value == Decimal("53267000000")
    assert capex[("North America", 2024)].value == Decimal("24348000000")
    assert capex[("International", 2024)].value == Decimal("6643000000")
    # Prior-year columns also extracted (acceptance: non-zero rows across years).
    assert capex[("AWS", 2022)].value == Decimal("27755000000")
