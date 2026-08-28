"""Derived-KPI lineage + cell-level disagreement surfacing (S2 PR3):
kpi_facts.computed_from (alembic 0087) written by the derivers and the
restatement detector, tier enrichment at persist, the popover's
derived-from / issue / extracted_by rows, and the issue display formatter."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import cast

import pytest

from compute.fmp_derived_kpis import (
    KPI_OPERATING_MARGIN_GAAP,
    KPI_REVENUE_YOY_USD,
    KPI_ROE,
    DerivedKpiRow,
    KpiSeriesPoint,
    QuarterlyFacts,
    TransformKind,
    compute_yoy_transform,
    derive_for_facts,
    persist_derived_kpis,
)
from models.facts import FiscalPeriodType, Unit
from pipeline.confidence import (
    _ParsedIssue,  # pyright: ignore[reportPrivateUsage]
    display_issues_for_fact,
    load_unresolved_issues,
)
from pipeline.restatement_detector import insert_kpi_with_restatement_detection
from report.models import CellSource
from report.sections.financials import (
    _kpi_cell_sources_for,  # pyright: ignore[reportPrivateUsage]
)
from tests.kpi_semantic_support import admit_all_kpi_facts
from ui.source_chip import source_chip_html
from viewspec.engine import _cell_source  # pyright: ignore[reportPrivateUsage]

# ----------------------------------------------------------------------------
# migration sanity — 0087 chains onto the current head
# ----------------------------------------------------------------------------


def test_migration_0087_chain() -> None:
    import importlib.util
    from pathlib import Path

    mig = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions_archived"
        / "0087_kpi_computed_from.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0087", mig)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0087_kpi_computed_from"
    assert mod.down_revision == "0086_decision_conditions"


# ----------------------------------------------------------------------------
# derivers emit computed_from
# ----------------------------------------------------------------------------


def _qf(
    period: str,
    fpt: FiscalPeriodType,
    revenue: str,
    doc_id: int,
    *,
    equity: str | None = None,
) -> QuarterlyFacts:
    return QuarterlyFacts(
        ticker="TST",
        period_end=datetime.fromisoformat(period),
        fiscal_period_type=fpt,
        revenue=Decimal(revenue),
        operating_income=Decimal("20"),
        net_income=Decimal("10"),
        gross_profit=Decimal("50"),
        source_doc_id=doc_id,
        total_stockholders_equity=Decimal(equity) if equity is not None else None,
    )


def test_derive_for_facts_emits_lineage() -> None:
    facts = [
        _qf("2024-12-31", FiscalPeriodType.Q4, "100", 7),
        _qf("2025-12-31", FiscalPeriodType.Q4, "120", 9),
    ]
    rows = derive_for_facts(facts)
    by_key = {(r.name, r.period_end.year): r for r in rows}

    margin = by_key[(KPI_OPERATING_MARGIN_GAAP, 2025)]
    assert margin.computed_from is not None
    payload = json.loads(margin.computed_from)
    assert payload["display"] == "operating_income ÷ revenue (%)"
    inputs = cast("list[dict[str, object]]", payload["inputs"])
    assert [i["item"] for i in inputs] == ["operating_income", "revenue"]
    assert all(i["ref"] == "financial_fact" for i in inputs)
    assert all(i["period_end"] == "2025-12-31" for i in inputs)
    assert all(i["doc_id"] == 9 for i in inputs)

    yoy = by_key[(KPI_REVENUE_YOY_USD, 2025)]
    assert yoy.computed_from is not None
    ypayload = json.loads(yoy.computed_from)
    yinputs = cast("list[dict[str, object]]", ypayload["inputs"])
    # Two-period derivation: current quarter + same quarter 1y prior, each
    # carrying ITS OWN source document.
    assert [(i["period_end"], i["doc_id"]) for i in yinputs] == [
        ("2025-12-31", 9),
        ("2024-12-31", 7),
    ]


def test_derive_roe_lineage_lists_ttm_window() -> None:
    facts = [
        _qf("2025-03-31", FiscalPeriodType.Q1, "100", 1),
        _qf("2025-06-30", FiscalPeriodType.Q2, "100", 2),
        _qf("2025-09-30", FiscalPeriodType.Q3, "100", 3),
        _qf("2025-12-31", FiscalPeriodType.Q4, "100", 4, equity="400"),
    ]
    rows = [r for r in derive_for_facts(facts) if r.name == KPI_ROE]
    assert len(rows) == 1
    assert rows[0].computed_from is not None
    payload = json.loads(rows[0].computed_from)
    inputs = cast("list[dict[str, object]]", payload["inputs"])
    items = [i["item"] for i in inputs]
    assert items.count("net_income") == 4
    assert items[-1] == "total_stockholders_equity"


def test_compute_yoy_transform_lineage_and_legacy_none() -> None:
    points = [
        KpiSeriesPoint(datetime(2024, 12, 31), FiscalPeriodType.Q4, Decimal("10"), 5),
        KpiSeriesPoint(datetime(2025, 12, 31), FiscalPeriodType.Q4, Decimal("12"), 6),
    ]
    with_label = compute_yoy_transform(
        points,
        kind=TransformKind.YOY_CHANGE_BPS,
        name="X YoY (bps)",
        unit=Unit.BPS,
        base_label="Risk-adjusted NIM",
    )
    assert len(with_label) == 1
    assert with_label[0].computed_from is not None
    payload = json.loads(with_label[0].computed_from)
    assert payload["display"] == "Risk-adjusted NIM - same quarter 1y prior (bps)"
    inputs = cast("list[dict[str, object]]", payload["inputs"])
    assert [(i["ref"], i["doc_id"]) for i in inputs] == [("kpi_fact", 6), ("kpi_fact", 5)]

    legacy = compute_yoy_transform(
        points, kind=TransformKind.YOY_CHANGE_BPS, name="X", unit=Unit.BPS
    )
    assert legacy[0].computed_from is None


# ----------------------------------------------------------------------------
# persistence: detector tail column + tier enrichment
# ----------------------------------------------------------------------------

_PERSIST_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, doc_type TEXT,
    file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
    raw_bytes_size INTEGER DEFAULT 0, source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT, filing_date TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'percent', primary_source TEXT, fallback_source TEXT,
    ir_url TEXT, threshold_tier TEXT, threshold_low REAL, threshold_high REAL,
    notes TEXT, UNIQUE(ticker, name)
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
    value NUMERIC NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0, extracted_by TEXT, supersedes_id INTEGER,
    locator TEXT, source_excerpt TEXT, computed_from TEXT
);
"""


def _persist_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_PERSIST_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_quality_tier) VALUES "
        " (9, 'TST', 'fmp', 'fmp_income_statement', 'f.json', 'a', "
        "  '2026-01-01 00:00:00', 'ok', 'fmp_normalized')"
    )
    return conn


def test_detector_writes_computed_from_when_column_exists() -> None:
    conn = _persist_conn()
    new_id, _ = insert_kpi_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        kpi_definition_id=1,
        value=Decimal("20"),
        unit="percent",
        source_doc_id=9,
        extracted_by="fmp_derived",
        computed_from='{"display":"x","inputs":[]}',
    )
    assert new_id is not None
    stored = conn.execute("SELECT computed_from FROM kpi_facts WHERE id = ?", (new_id,)).fetchone()
    assert stored[0] == '{"display":"x","inputs":[]}'
    conn.close()


def test_detector_drops_computed_from_on_legacy_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_PERSIST_DDL.replace(", computed_from TEXT", ""))
    new_id, _ = insert_kpi_with_restatement_detection(
        conn,
        ticker="TST",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type="Q4",
        kpi_definition_id=1,
        value=Decimal("20"),
        unit="percent",
        source_doc_id=9,
        computed_from='{"display":"x","inputs":[]}',
    )
    assert new_id is not None  # silently dropped, insert still lands
    conn.close()


def test_persist_enriches_input_tiers() -> None:
    conn = _persist_conn()
    row = DerivedKpiRow(
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        name=KPI_OPERATING_MARGIN_GAAP,
        value=Decimal("20"),
        unit=Unit.PERCENT,
        source_doc_id=9,
        computed_from=json.dumps(
            {
                "display": "operating_income ÷ revenue (%)",
                "inputs": [
                    {
                        "ref": "financial_fact",
                        "item": "operating_income",
                        "period_end": "2025-12-31",
                        "doc_id": 9,
                    }
                ],
            }
        ),
    )
    inserted = persist_derived_kpis(conn, ticker="TST", rows=[row])
    assert inserted == 1
    stored = conn.execute("SELECT computed_from FROM kpi_facts").fetchone()[0]
    payload = json.loads(stored)
    assert payload["inputs"][0]["tier"] == "fmp_normalized"
    conn.close()


# ----------------------------------------------------------------------------
# issue display strings
# ----------------------------------------------------------------------------


def _signals(rows: list[tuple[str, str, str]]) -> dict[str, list[_ParsedIssue]]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE validation_issues (id INTEGER PRIMARY KEY, run_id TEXT, "
        "source_doc_id INTEGER, ticker TEXT, severity TEXT, rule TEXT, raw_value TEXT, "
        "expected TEXT, raised_at TIMESTAMP, resolved_at TIMESTAMP);"
    )
    conn.executemany(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, raised_at) "
        "VALUES ('r', 'TST', ?, ?, ?, '2026-06-01')",
        rows,
    )
    conn.commit()
    out = load_unresolved_issues(conn)
    conn.close()
    return out


def test_disagreement_reads_from_displayed_cells_perspective() -> None:
    signals = _signals(
        [
            (
                "warn",
                "source_disagreement",
                "revenue @ 2025-12-31: fmp=99000000 vs sec_xbrl=99990000 (0.99%)",
            )
        ]
    )
    # Displayed cell is the FMP row → the OTHER side (SEC) is quoted.
    shown = display_issues_for_fact(
        signals,
        ticker="TST",
        item="revenue",
        period_end="2025-12-31",
        value=99000000.0,
        displayed_tier="fmp_normalized",
    )
    assert shown == ["⚠ SEC says $100M, 0.99% delta"]
    # Displayed cell is the SEC row → FMP is quoted.
    other = display_issues_for_fact(
        signals,
        ticker="TST",
        item="revenue",
        period_end="2025-12-31",
        value=99990000.0,
        displayed_tier="sec_official",
    )
    assert other == ["⚠ FMP says $99M, 0.99% delta"]
    # No displayed tier → both sides named.
    both = display_issues_for_fact(
        signals,
        ticker="TST",
        item="revenue",
        period_end="2025-12-31",
        value=99000000.0,
    )
    assert both == ["⚠ FMP $99M vs SEC $100M (0.99% delta)"]


def test_unknown_rule_falls_back_to_raw_reason() -> None:
    # Manual-override reasons land in raw_value per data_provenance.md §3 —
    # the generic fallback surfaces them verbatim.
    signals = _signals([("warn", "manual_override", "revenue=5 — corrected per 10-K errata")])
    shown = display_issues_for_fact(
        signals,
        ticker="TST",
        item="revenue",
        period_end="2025-12-31",
        value=5.0,
    )
    assert shown == ["⚠ manual override: revenue=5 — corrected per 10-K errata"]


# ----------------------------------------------------------------------------
# popover rendering
# ----------------------------------------------------------------------------


def test_popover_renders_lineage_issues_and_extracted_by() -> None:
    src = CellSource(
        source="fmp_normalized",
        confidence=0.94,
        extracted_by="fmp_derived",
        computed_from=json.dumps(
            {
                "display": "operating_income ÷ revenue (%)",
                "inputs": [
                    {
                        "ref": "financial_fact",
                        "item": "operating_income",
                        "period_end": "2025-12-31",
                        "doc_id": 45,
                        "tier": "fmp_normalized",
                    },
                    {
                        "ref": "financial_fact",
                        "item": "revenue",
                        "period_end": "2025-12-31",
                    },
                ],
            }
        ),
        issues=["⚠ SEC says $100M, 0.99% delta"],
    )
    out = source_chip_html(src)
    assert "derived from: operating_income ÷ revenue (%)" in out
    # Input with doc_id + tier → tier-colored mini-chip linking /source.
    assert '<a class="src-chip src-fmp-normalized" href="/source/45"' in out
    assert "operating_income · 2025-12-31" in out
    # Input without doc_id/tier → plain span fallback chip.
    assert '<span class="src-chip">SRC</span> revenue · 2025-12-31' in out
    assert '<div class="src-pop-row src-pop-warn">⚠ SEC says $100M, 0.99% delta</div>' in out
    assert "via fmp_derived" in out


def test_popover_tolerates_malformed_lineage() -> None:
    src = CellSource(source="fmp_normalized", computed_from="{not json")
    out = source_chip_html(src)
    assert "derived from" not in out
    plain = source_chip_html(CellSource(source="fmp_normalized"))
    assert "via " not in plain
    assert "src-pop-warn" not in plain


# ----------------------------------------------------------------------------
# §3 KPI cell sources carry the new fields end-to-end
# ----------------------------------------------------------------------------


def test_kpi_cell_sources_carry_lineage_and_issues() -> None:
    conn = _persist_conn()
    conn.executescript(
        "CREATE TABLE validation_issues (id INTEGER PRIMARY KEY, run_id TEXT, "
        "source_doc_id INTEGER, ticker TEXT, severity TEXT, rule TEXT, raw_value TEXT, "
        "expected TEXT, raised_at TIMESTAMP, resolved_at TIMESTAMP);"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) "
        "VALUES (3, 'TST', 'Operating Margin (GAAP)', 'percent')"
    )
    conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        " value, unit, source_doc_id, confidence, extracted_by, computed_from) "
        "VALUES ('TST', '2025-12-31', 'Q4', 3, 20.0, 'percent', 9, 0.94, 'fmp_derived', "
        ' \'{"display":"operating_income ÷ revenue (%)","inputs":[]}\')'
    )
    conn.execute(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, raised_at) "
        "VALUES ('r', 'TST', 'warn', 'plausible_range', 'Operating Margin (GAAP)=20.0 percent', "
        " '2026-06-01')"
    )
    admit_all_kpi_facts(conn)
    conn.commit()
    out = _kpi_cell_sources_for(conn, "TST", "Operating Margin (GAAP)", ("Q1", "Q2", "Q3", "Q4"))
    conn.close()
    cell = out["2025-12-31"]
    assert cell.extracted_by == "fmp_derived"
    assert cell.computed_from is not None and "operating_income" in cell.computed_from
    assert cell.issues == ["⚠ plausible range: Operating Margin (GAAP)=20.0 percent"]


def test_viewspec_cell_source_maps_lineage_fields() -> None:
    src = _cell_source(
        {
            "source": "fmp_normalized",
            "extracted_by": "fmp_derived",
            "computed_from": '{"display":"x","inputs":[]}',
        }
    )
    assert src.extracted_by == "fmp_derived"
    assert src.computed_from == '{"display":"x","inputs":[]}'


def test_score_path_unaffected_by_lineage_fields() -> None:
    # Sanity: PR1 chip anatomy unchanged for plain extracted facts.
    plain = CellSource(source="sec_official", confidence=1.0)
    out = source_chip_html(plain)
    assert "confidence 100%" in out
    assert "derived from" not in out


def test_display_issue_value_format_edges() -> None:
    signals = _signals(
        [("warn", "source_disagreement", "nim @ 2025-12-31: fmp=9.5 vs sec_xbrl=9.6 (1.05%)")]
    )
    shown = display_issues_for_fact(
        signals,
        ticker="TST",
        item="nim",
        period_end="2025-12-31",
        value=9.5,
        displayed_tier="fmp_normalized",
    )
    # Small KPI values render as plain numbers, not $-scaled.
    assert shown == ["⚠ SEC says 9.6, 1.05% delta"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("99990000", "$100M"),
        ("1500000000", "$1.5B"),
        ("250000", "$250K"),
        ("9.5", "9.5"),
    ],
)
def test_fmt_compact_value(raw: str, expected: str) -> None:
    from pipeline.confidence import _fmt_compact_value  # pyright: ignore[reportPrivateUsage]

    assert _fmt_compact_value(raw) == expected
