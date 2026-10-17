"""report-financials-hoist structural query budget (audit performance-serving
F6 / VAL-SERVE-013).

The financials section's chart-priority loop used to re-derive the constant
semantic-SQL triple (canonical fact relation + semantic admission + series
identity), the schema guards, and the resolver lookups once per KPI, and to
issue TWO identity-anchored queries per KPI — the series query plus a
separately ranked sources query over the same definition. This suite pins the
hoisted/merged shape through the public section entries only:

* the resolved output is unchanged: same priorities order, same values, same
  per-period chip provenance (``build_legacy_shadow`` end-to-end and the public
  ``kpi_series_for`` reader directly);
* the production ``build`` entry keeps its degraded-projection contract on a
  fixture without the §2 binding schema;
* at most ONE heavy series-plus-sources statement per KPI, and the standalone
  sources statement shape never appears in the build path;
* a total statement budget at most 60 percent of the recorded pre-change count
  for this same fixture (see ``_PRE_CHANGE_STATEMENT_COUNT``).

Output equivalence for the frozen bench tickers is captured separately under
``.tmp/contract/serving/report-financials-hoist/`` (before/after serialized
section JSON, byte-identical for all five tickers).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from report.models import FinancialsSection, SectionStatus
from report.sections.financials import build as build_financials_section
from report.sections.financials import build_legacy_shadow, kpi_series_for

# The identity-anchored ranked statement over the canonical fact relation:
# the pre-merge series/sources queries and the post-merge single query all
# have this prefix shape.
_HEAVY_RX = re.compile(r"WITH\s+ranked\s+AS\s*\(", re.IGNORECASE | re.DOTALL)
_SERIES_PARTITION_RX = re.compile(
    r"ROW_NUMBER\(\)\s+OVER\s*\(\s*PARTITION BY kf\.period_end", re.DOTALL
)
# The standalone sources reader's select list — must NOT appear in the report
# build path anymore (its chip rows ride along the merged query instead).
_STANDALONE_SOURCES_RX = re.compile(
    r"WITH\s+ranked\s+AS\s*\(\s*SELECT kf\.period_end,\s*kf\.locator", re.DOTALL
)

_CHART_PRIORITIES = ["ARPAC (USD)", "Margin (GAAP)", "Bookings"]
_QUARTER_LABELS = ["2025 Q3", "2025 Q4"]
_QUARTER_LABELS_FULL = ["2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4"]
_AS_OF = datetime(2026, 3, 1, tzinfo=UTC)

# Statement count measured for this exact fixture through the same public
# entry (``build_legacy_shadow``) with the PRE-change reader (git 0d9571fb,
# before the context hoist + series/sources merge): the loop re-derived the
# semantic-SQL triple, schema guards and resolver lookups per KPI and issued a
# series query plus a sources query per resolved KPI (6 heavy statements and 3
# standalone-sources statements for 3 KPIs, 156 statements total). The hoisted
# and merged reader must stay at or below 60 percent of that count
# (VAL-SERVE-013); it measured 64 statements and 3 heavy statements (one per
# KPI, zero standalone-sources statements) on the same fixture.
_PRE_CHANGE_STATEMENT_COUNT = 156
_POST_CHANGE_STATEMENT_CEILING = int(_PRE_CHANGE_STATEMENT_COUNT * 0.60)


_DDL = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    unit VARCHAR,
    reporting_cadence VARCHAR
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    period_end VARCHAR NOT NULL,
    value NUMERIC,
    unit VARCHAR,
    currency VARCHAR,
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
CREATE TABLE fact_overrides (
    ticker TEXT, fact_kind TEXT, fact_key TEXT, action TEXT, status TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    currency TEXT,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC,
    source_doc_id INTEGER NOT NULL DEFAULT 1
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    _seed(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
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
    conn.executemany(
        "INSERT INTO kpi_definitions (id, ticker, name, unit, reporting_cadence) "
        "VALUES (?, 'TST', ?, ?, ?)",
        [
            (9, "ARPAC (USD)", "actual", "quarterly"),
            (10, "Margin (GAAP)", "percent", "quarterly"),
            (11, "Bookings", "actual", "annual"),
        ],
    )
    kpi_rows: list[tuple[str, float, str, int, int, float]] = [
        # ARPAC (USD): Q2/Q3 from the LLM brief, Q4 reported by both the LLM
        # brief (doc 1) and the IR spreadsheet (doc 2) — the tier/issuer winner
        # is the IR row (11.2), exactly the chip provenance pin below.
        ("2025-06-30", 10.1, "Q2", 9, 1, 0.7),
        ("2025-09-30", 10.9, "Q3", 9, 1, 0.7),
        ("2025-12-31", 11.0, "Q4", 9, 1, 0.7),
        ("2025-12-31", 11.2, "Q4", 9, 2, 0.94),
        # Margin (GAAP): two quarters, different documents.
        ("2025-06-30", 22.0, "Q2", 10, 1, 0.8),
        ("2025-09-30", 23.5, "Q3", 10, 2, 0.9),
        # Bookings: annual cadence on the fiscal-year axis.
        ("2023-12-31", 100.0, "FY", 11, 2, 0.9),
        ("2024-12-31", 130.0, "FY", 11, 2, 0.9),
        ("2025-12-31", 160.0, "FY", 11, 2, 0.93),
    ]
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, value, unit, currency, "
        " kpi_definition_id, fiscal_period_type, source_doc_id, confidence) "
        "VALUES ('TST', ?, ?, 'actual', 'USD', ?, ?, ?, ?)",
        [(r[0], r[1], r[3], r[2], r[4], r[5]) for r in kpi_rows],
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id, metric_name_as_reported, reported_period_end) "
        "SELECT kf.id, kd.name, substr(kf.period_end, 1, 10) "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id"
    )
    # §3 financial_facts: three quarters of revenue so the legacy-shadow
    # entry derives a real quarter axis ("2025 Q2".."2025 Q4") and the
    # quarterly KPI series have labels to align against.
    financial_rows: list[tuple[str, str, str, str, float, int]] = [
        ("2025-06-30", "USD", "Q2", "revenue", 500.0, 2),
        ("2025-09-30", "USD", "Q3", "revenue", 520.0, 2),
        ("2025-12-31", "USD", "Q4", "revenue", 540.0, 2),
    ]
    conn.executemany(
        "INSERT INTO financial_facts "
        "(ticker, period_end, currency, fiscal_period_type, line_item, value, source_doc_id) "
        "VALUES ('TST', ?, ?, ?, ?, ?, ?)",
        financial_rows,
    )
    conn.commit()


def _repo(tmp_path: Path) -> Path:
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True, exist_ok=True)
    (holdings / "TST.json").write_text(
        json.dumps({"ticker": "TST", "chart_priorities": _CHART_PRIORITIES}),
        encoding="utf-8",
    )
    return tmp_path


class _Trace:
    """Minimal sqlite3 statement trace: ordered SQL text."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, sql: str) -> None:
        self.statements.append(sql)

    @property
    def heavy_series_statements(self) -> list[str]:
        return [
            sql
            for sql in self.statements
            if _HEAVY_RX.search(sql) and _SERIES_PARTITION_RX.search(sql)
        ]

    @property
    def standalone_sources_statements(self) -> list[str]:
        return [sql for sql in self.statements if _STANDALONE_SOURCES_RX.search(sql)]


def _traced_legacy_shadow(
    conn: sqlite3.Connection, repo_root: Path
) -> tuple[FinancialsSection, _Trace]:
    trace = _Trace()
    conn.set_trace_callback(trace)
    try:
        section = build_legacy_shadow("TST", repo_root, conn=conn)
    finally:
        conn.set_trace_callback(None)
    return section, trace


def _traced_build(conn: sqlite3.Connection, repo_root: Path) -> tuple[FinancialsSection, _Trace]:
    trace = _Trace()
    conn.set_trace_callback(trace)
    try:
        section = build_financials_section("TST", repo_root, conn=conn, as_of=_AS_OF)
    finally:
        conn.set_trace_callback(None)
    return section, trace


# ---------------------------------------------------------------------------
# resolution output: unchanged priorities order, values, and chips (parity pins)
# ---------------------------------------------------------------------------


def test_kpi_series_reader_values_and_chips_unchanged() -> None:
    """The public ``kpi_series_for`` reader keeps the exact values/chips contract.

    ``kpi_series_for`` is the module's public reader alias; the ARPAC Q4 chip
    must describe the IR-spreadsheet row that wins the same-tier pick, and the
    label axes pad with None exactly as before the hoist/merge.
    """
    conn = _conn()
    try:
        arpac = kpi_series_for(
            conn, "TST", "ARPAC (USD)", list(_QUARTER_LABELS), list(_QUARTER_LABELS_FULL)
        )
        assert arpac is not None
        assert arpac.name == "ARPAC (USD)"
        assert arpac.quarters == _QUARTER_LABELS
        assert arpac.values == [10.9, 11.2]
        assert arpac.levels_full == [None, 10.1, 10.9, 11.2]
        q4_source = arpac.sources_full[-1]
        assert q4_source is not None
        assert q4_source.source == "fmp_normalized"
        assert q4_source.source_url == "https://ir.example/x.xlsx"
        assert q4_source.confidence == 0.94
        margin = kpi_series_for(
            conn, "TST", "Margin (GAAP)", list(_QUARTER_LABELS), list(_QUARTER_LABELS_FULL)
        )
        assert margin is not None
        assert margin.values == [23.5, None]
        assert margin.levels_full == [None, 22.0, 23.5, None]
    finally:
        conn.close()


def test_legacy_shadow_resolves_every_priority_end_to_end(tmp_path: Path) -> None:
    """``build_legacy_shadow`` — the public section entry with a real quarter
    axis — resolves all three chart-priority KPIs with unchanged ordering,
    values, chip provenance, and the annual fiscal-year alignment.
    """
    conn = _conn()
    try:
        section, _trace = _traced_legacy_shadow(conn, _repo(tmp_path))
        assert section.status is SectionStatus.PARTIAL
        assert section.chart_priorities == ["ARPAC (USD)", "Margin (GAAP)", "Bookings"]
        assert [series.name for series in section.kpi_chart_series] == [
            "ARPAC (USD)",
            "Margin (GAAP)",
        ]
        arpac = section.kpi_chart_series[0]
        assert arpac.quarters == section.quarter_labels == ["2025 Q2", "2025 Q3", "2025 Q4"]
        assert arpac.values == [10.1, 10.9, 11.2]
        assert arpac.levels_full == [10.1, 10.9, 11.2]
        q4_source = arpac.sources_full[-1]
        assert q4_source is not None
        assert q4_source.source == "fmp_normalized"
        assert q4_source.source_url == "https://ir.example/x.xlsx"
        assert q4_source.confidence == 0.94
        margin = section.kpi_chart_series[1]
        assert margin.values == [22.0, 23.5, None]
        assert [series.name for series in section.annual_kpi_chart_series] == ["Bookings"]
        bookings = section.annual_kpi_chart_series[0]
        assert section.annual_kpi_years == [2023, 2024, 2025]
        assert bookings.values == [100.0, 130.0, 160.0]
        latest_source = bookings.sources_full[-1]
        assert latest_source is not None
        assert latest_source.source == "fmp_normalized"
        assert latest_source.confidence == 0.93
    finally:
        conn.close()


def test_production_build_keeps_degraded_projection_contract(tmp_path: Path) -> None:
    """``build`` — the production §2 entry — fails closed to the missing-data
    section on a fixture without the canonical binding schema, and still
    resolves the annual chart-priority KPI on the fiscal-year axis while the
    quarterly KPIs stay dropped without a quarter axis (the pre-change
    behavior for this degraded shape).
    """
    conn = _conn()
    try:
        section, trace = _traced_build(conn, _repo(tmp_path))
        assert section.status is SectionStatus.MISSING_DATA
        assert section.missing is not None
        assert section.missing.detail is not None
        assert "canonical_financial_table_unavailable" in section.missing.detail
        assert section.quarter_labels == []
        assert section.kpi_chart_series == []
        # Annual-cadence KPIs key off period_end years, not the §2 quarter
        # axis — they still resolve on the degraded projection.
        assert section.chart_priorities == ["Bookings"]
        assert [series.name for series in section.annual_kpi_chart_series] == ["Bookings"]
        assert section.annual_kpi_chart_series[0].values == [100.0, 130.0, 160.0]
        # Bounded per KPI even through the degraded production entry: the
        # quarterly KPIs still pay their one merged statement before the
        # empty-axis drop, the annual KPI pays its one — and the standalone
        # sources shape never appears.
        assert len(trace.heavy_series_statements) == len(_CHART_PRIORITIES) == 3
        assert trace.standalone_sources_statements == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# structural query budget (VAL-SERVE-013)
# ---------------------------------------------------------------------------


def test_one_heavy_series_statement_per_kpi(tmp_path: Path) -> None:
    conn = _conn()
    try:
        _section, trace = _traced_legacy_shadow(conn, _repo(tmp_path))
        heavy = trace.heavy_series_statements
        # Three chart-priority KPIs (two quarterly + one annual) → exactly one
        # identity-anchored series-plus-sources statement per KPI. Pre-merge
        # the build issued two per KPI (series + separately ranked sources).
        assert len(heavy) == len(_CHART_PRIORITIES) == 3
        # The standalone sources statement shape must not appear in the build
        # path at all — its chip rows ride along the merged query.
        assert trace.standalone_sources_statements == []
    finally:
        conn.close()


def test_statement_count_within_query_budget(tmp_path: Path) -> None:
    conn = _conn()
    try:
        _section, trace = _traced_legacy_shadow(conn, _repo(tmp_path))
        total = len(trace.statements)
        # VAL-SERVE-013: the post-change count must be at most 60 percent of
        # the pre-change count recorded for this same fixture through the same
        # public entry (_PRE_CHANGE_STATEMENT_COUNT, measured at git 0d9571fb).
        assert total <= _POST_CHANGE_STATEMENT_CEILING, (
            f"statement budget blown: {total} > "
            f"{_POST_CHANGE_STATEMENT_CEILING} (60% of {_PRE_CHANGE_STATEMENT_COUNT})"
        )
    finally:
        conn.close()
