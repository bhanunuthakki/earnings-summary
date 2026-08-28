"""KPI chip universality (fund-grade build S2, PR2): KpiSeries /
AnnualKpiSeries carry per-period provenance + confidence, the §3 KPI
heatmap panels render the chip anatomy, and the ViewSpec fragment keeps
parity (its kpi cells carry the same chip with the scored %)."""

from __future__ import annotations

import sqlite3
from io import StringIO

from report.models import AnnualKpiSeries, CellSource, FinancialsSection, KpiSeries, SectionStatus
from report.renderers.workspace_html import (
    _annual_kpi_series_yoy_panel,  # pyright: ignore[reportPrivateUsage]
    _kpi_series_yoy_panel,  # pyright: ignore[reportPrivateUsage]
)
from report.sections.financials import (
    _align_annual_kpis,  # pyright: ignore[reportPrivateUsage]
    _annual_kpi_raw_for,  # pyright: ignore[reportPrivateUsage]
    _kpi_series_for,  # pyright: ignore[reportPrivateUsage]
)
from viewspec.engine import ViewCell, ViewResult, ViewRow
from viewspec.render import render_view_fragment
from viewspec.spec import MetricRef, ViewSpec

# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------

_DDL = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    unit VARCHAR
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    period_end VARCHAR NOT NULL,
    value NUMERIC,
    unit VARCHAR,
    kpi_definition_id INTEGER NOT NULL,
    fiscal_period_type VARCHAR NOT NULL,
    source_doc_id INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 1.0,
    locator TEXT
);
CREATE TABLE kpi_fact_semantic_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kpi_fact_id INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    supersedes_context_id INTEGER,
    metric_name_as_reported TEXT NOT NULL,
    reported_period_end TEXT,
    period_role TEXT NOT NULL DEFAULT 'current',
    publication_lane TEXT NOT NULL DEFAULT 'current_actual',
    accounting_basis TEXT NOT NULL DEFAULT 'management',
    consolidation_scope TEXT NOT NULL DEFAULT 'consolidated',
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    unit_scale TEXT NOT NULL DEFAULT 'none',
    source_row_label TEXT,
    source_column_header TEXT,
    status TEXT NOT NULL DEFAULT 'admitted',
    reason_code TEXT
);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
"""

_Q_LABELS = ["2025 Q3", "2025 Q4"]
_Q_LABELS_FULL = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4"]


def _seed(conn: sqlite3.Connection, *, with_documents: bool = True) -> None:
    if with_documents:
        conn.executemany(
            "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
            " fetched_at, fetch_status, source_url, source_quality_tier) "
            "VALUES (?, 'TST', ?, ?, ?, ?, ?, 'ok', ?, ?)",
            [
                (
                    1,
                    "llm_extracted",
                    "fmp_10q_json",
                    "b.json",
                    "a",
                    "2026-01-05 10:00:00",
                    None,
                    "llm_extracted",
                ),
                (
                    2,
                    "ir_doc",
                    "ir_supplement",
                    "ir.xlsx",
                    "b",
                    "2026-02-01 10:00:00",
                    "https://ir.example/x.xlsx",
                    "fmp_normalized",
                ),
            ],
        )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (9, 'TST', 'ARPAC (USD)', 'actual')"
    )
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
        " fiscal_period_type, source_doc_id, confidence) VALUES ('TST', ?, ?, 'actual', 9, ?, ?, ?)",
        [
            ("2025-06-30", 10.1, "Q2", 1, 0.7),
            ("2025-09-30", 10.9, "Q3", 1, 0.7),
            # Q4 reported by both the LLM brief (doc 1) and the IR spreadsheet
            # (doc 2) — the MAX(source_doc_id) winner is doc 2 and the chip
            # must describe IT, not the superseded LLM row.
            ("2025-12-31", 11.0, "Q4", 1, 0.7),
            ("2025-12-31", 11.2, "Q4", 2, 0.94),
        ],
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id,metric_name_as_reported,reported_period_end) "
        "SELECT id,'ARPAC (USD)',substr(period_end,1,10) FROM kpi_facts"
    )
    conn.commit()


def _conn(*, with_documents: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ddl = _DDL if with_documents else _DDL.split("CREATE TABLE documents")[0]
    conn.executescript(ddl)
    _seed(conn, with_documents=with_documents)
    return conn


# ----------------------------------------------------------------------------
# section builder: KpiSeries gains sources_full / confidence_full
# ----------------------------------------------------------------------------


def test_kpi_series_carries_sources_and_confidence() -> None:
    conn = _conn()
    series = _kpi_series_for(conn, "TST", "ARPAC", _Q_LABELS, _Q_LABELS_FULL)
    conn.close()
    assert series is not None
    assert series.name == "ARPAC (USD)"
    # sources_full aligns to quarter_labels_full: Q1 has no fact → None.
    assert len(series.sources_full) == len(_Q_LABELS_FULL)
    assert series.sources_full[0] is None
    q3 = series.sources_full[2]
    assert q3 is not None and q3.source == "llm_extracted" and q3.doc_id == 1
    # Q4: the IR-spreadsheet row (higher source_doc_id) wins value AND chip.
    assert series.levels_full[3] == 11.2
    q4 = series.sources_full[3]
    assert q4 is not None
    assert q4.source == "fmp_normalized"
    assert q4.doc_id == 2
    assert q4.source_url == "https://ir.example/x.xlsx"
    assert q4.confidence == 0.94
    # confidence_full mirrors sources_full[i].confidence.
    assert series.confidence_full == [None, 0.7, 0.7, 0.94]


def test_kpi_series_degrades_without_documents_table() -> None:
    conn = _conn(with_documents=False)
    series = _kpi_series_for(conn, "TST", "ARPAC", _Q_LABELS, _Q_LABELS_FULL)
    conn.close()
    assert series is not None  # values still load
    assert series.values == [10.9, 11.2]
    assert series.sources_full == []  # chips silently absent
    assert series.confidence_full == []


def test_annual_kpi_series_carries_sources() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_quality_tier) "
        "VALUES (4, 'TST', 'sec_xbrl', 'sec_20f', 's.json', 'd', '2026-03-01 10:00:00', "
        " 'ok', 'sec_official')"
    )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (3, 'TST', 'CAR (%)', 'percent')"
    )
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
        " fiscal_period_type, source_doc_id, confidence) VALUES ('TST', ?, ?, 'percent', 3, 'FY', 4, 1.0)",
        [("2024-12-31", 18.0), ("2025-12-31", 17.1)],
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id,metric_name_as_reported,reported_period_end,accounting_basis) "
        "SELECT id,'CAR (%)',substr(period_end,1,10),'gaap' FROM kpi_facts"
    )
    conn.commit()
    raw = _annual_kpi_raw_for(conn, "TST", "CAR")
    conn.close()
    assert raw is not None
    series, years = _align_annual_kpis([raw])
    assert years == [2024, 2025]
    s = series[0]
    assert s.values == [18.0, 17.1]
    assert len(s.sources_full) == 2
    y25 = s.sources_full[1]
    assert y25 is not None and y25.source == "sec_official" and y25.doc_id == 4
    assert s.confidence_full == [1.0, 1.0]


# ----------------------------------------------------------------------------
# §3 renderer panels: chip anatomy on the KPI heatmaps
# ----------------------------------------------------------------------------


def _fin_with_kpi_series(series: KpiSeries) -> FinancialsSection:
    return FinancialsSection(
        status=SectionStatus.OK,
        quarter_labels=_Q_LABELS,
        quarter_labels_full=_Q_LABELS_FULL,
        kpi_chart_series=[series],
    )


def test_kpi_panel_emits_cell_titles_and_label_chip() -> None:
    series = KpiSeries(
        name="ARPAC (USD)",
        unit="",
        quarters=_Q_LABELS,
        values=[10.9, 11.2],
        levels_full=[10.1, 10.4, 10.9, 11.2],
        sources_full=[
            None,
            None,
            CellSource(source="llm_extracted", fetched_at="2026-01-05 10:00:00", confidence=0.7),
            CellSource(
                source="fmp_normalized",
                fetched_at="2026-02-01 10:00:00",
                source_url="https://ir.example/x.xlsx",
                confidence=0.94,
            ),
        ],
        confidence_full=[None, None, 0.7, 0.94],
    )
    body = StringIO()
    _kpi_series_yoy_panel(body, _fin_with_kpi_series(series))
    out = body.getvalue()
    # Per-cell hover carries tier + conf %; row label carries the clickable chip
    # for the latest sourced quarter (the 0.94 IR-spreadsheet row, NOT lowconf).
    assert 'title="llm_extracted · fetched 2026-01-05 · conf 70%"' in out
    assert 'title="fmp_normalized · fetched 2026-02-01 · conf 94%"' in out
    assert 'class="src-chip src-fmp-normalized"' in out
    assert "confidence 94%" in out  # popover row
    assert "https://ir.example/x.xlsx" in out


def test_kpi_panel_without_sources_renders_plain() -> None:
    series = KpiSeries(
        name="ARPAC (USD)",
        unit="",
        quarters=_Q_LABELS,
        values=[10.9, 11.2],
        levels_full=[10.1, 10.4, 10.9, 11.2],
    )
    body = StringIO()
    _kpi_series_yoy_panel(body, _fin_with_kpi_series(series))
    out = body.getvalue()
    assert "Tracked KPIs" in out
    assert "src-chip" not in out


def test_annual_kpi_panel_emits_chips() -> None:
    fin = FinancialsSection(
        status=SectionStatus.OK,
        annual_kpi_years=[2024, 2025],
        annual_kpi_chart_series=[
            AnnualKpiSeries(
                name="CAR (%)",
                unit="%",
                years=[2024, 2025],
                values=[18.0, 17.1],
                sources_full=[
                    None,
                    CellSource(
                        source="sec_official", fetched_at="2026-03-01 10:00:00", confidence=1.0
                    ),
                ],
                confidence_full=[None, 1.0],
            )
        ],
    )
    body = StringIO()
    _annual_kpi_series_yoy_panel(body, fin)
    out = body.getvalue()
    assert 'title="sec_official · fetched 2026-03-01 · conf 100%"' in out
    assert 'class="src-chip src-sec-official"' in out


# ----------------------------------------------------------------------------
# ViewSpec parity: kpi cells render the same chip with the scored %
# ----------------------------------------------------------------------------


def test_viewspec_fragment_kpi_cell_chip_carries_confidence() -> None:
    spec = ViewSpec(
        tickers=("TST",),
        metrics=(MetricRef(domain="kpi", key="ARPAC (USD)"),),
        transform="level",
        cadence="quarter",
    )
    low = CellSource(source="llm_extracted", fetched_at="2026-01-05 10:00:00", confidence=0.7)
    row = ViewRow(
        ticker="TST",
        metric=spec.metrics[0],
        label="TST · ARPAC (USD)",
        unit="USD",
        cells=[ViewCell(value=11.2, raw=11.2, source=low)],
    )
    result = ViewResult(spec=spec, period_labels=["Q4'25"], rows=[row], warnings=[])
    html_out = render_view_fragment(result, include_chart=False)
    assert "confidence 70% · below threshold" in html_out
    assert "src-lowconf" in html_out
    assert "conf 70%" in html_out  # hover title
