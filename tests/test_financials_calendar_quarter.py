"""§3 Financials quarterly/annual loaders read financial_facts directly and key
every quarter by the *actual* period_end date (calendar quarter), not the
``fiscal_period_type`` label.

Regression guard for the ``metrics``-view ``MAX(period_end)`` cross-labeling
bug: for off-calendar fiscal-year reporters FMP and SEC disagree on the
fiscal-quarter label, so a single mislabeled fact (e.g. a Q4-ending value filed
under 'Q3' in SEC companyfacts, with a higher source_doc_id) used to drag a
whole quarter's period_end — and sometimes its values — into the wrong quarter,
shifting/colliding/dropping displayed quarters (AMAT/MU/TOL/VEEV in prod). The
direct, calendar-quarter-keyed read makes the wrong label inert.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from report.models import SectionStatus
from report.sections._common import quarter_label
from report.sections.financials import (
    _dedupe_by_calendar_quarter,  # pyright: ignore[reportPrivateUsage]
    _load_annual,  # pyright: ignore[reportPrivateUsage]
    _load_quarterly,  # pyright: ignore[reportPrivateUsage]
    build,
)

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
"""

# (period_end, fiscal_period_type, line_item, value, source_doc_id)
_FactRow = tuple[str, str, str, int, int]

# An AMAT-shaped off-calendar reporter (fiscal year ends late October). Each of
# the four FY2016 quarters carries a *distinct* revenue and net_income so a
# mislabel is unambiguous. The four net_income quarters sum to the FY total
# (286+320+505+610 = 1721), so a shift/collision can never reconcile by luck.
_OFFCAL_QUARTERS: list[tuple[str, str, int, int]] = [
    # (period_end, fiscal_label, revenue, net_income)
    ("2016-01-31", "Q1", 2257, 286),
    ("2016-05-01", "Q2", 2450, 320),
    ("2016-07-31", "Q3", 2821, 505),
    ("2016-10-30", "Q4", 3297, 610),
]


def _seed_offcal(conn: sqlite3.Connection, ticker: str = "OFFCAL") -> None:
    """Seed the four FY2016 quarters as legit FMP rows (docs 100..), then add
    THE BUG: a Q4-ending net_income mislabeled 'Q3' in SEC companyfacts with a
    higher source_doc_id (900) — the exact shape that broke AMAT in prod."""
    rows: list[_FactRow] = []
    doc = 100
    for period_end, label, revenue, net_income in _OFFCAL_QUARTERS:
        pe = f"{period_end} 00:00:00"
        # Seed every default-chart-priority line item so build() resolves them
        # via line_items and never queries kpi tables.
        rows.append((pe, label, "revenue", revenue * 1_000_000, doc))
        rows.append((pe, label, "operating_income", net_income * 1_000_000, doc))
        rows.append((pe, label, "net_income", net_income * 1_000_000, doc))
        rows.append((pe, label, "operating_cash_flow", (net_income + 50) * 1_000_000, doc))
        rows.append((pe, label, "free_cash_flow", (net_income + 20) * 1_000_000, doc))
        doc += 1

    # THE MISLABEL: Q4's net_income (610) filed under 'Q3' at the Q4 period_end,
    # winning the source_doc_id tie. Under the old (fiscal_year, fiscal_period_type)
    # grouping this became the (2016, Q3) net_income and dragged that row to the
    # Oct-30 date → it mis-bucketed as cal-Q4 and the July quarter lost its value.
    rows.append(("2016-10-30 00:00:00", "Q3", "net_income", 610 * 1_000_000, 900))

    # An annual FY2016 row for the reconciliation + annual-table assertions.
    rows.append(("2016-10-30 00:00:00", "FY", "revenue", 10825 * 1_000_000, 200))
    rows.append(("2016-10-30 00:00:00", "FY", "net_income", 1721 * 1_000_000, 200))

    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status) VALUES (100, ?, 'fmp', 'fmp_income_statement', "
        " 'f.json', 'a', '2026-01-05 10:00:00', 'ok')",
        (ticker,),
    )
    conn.executemany(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        " value, currency, source_doc_id) VALUES (?, ?, ?, ?, ?, 'USD', ?)",
        [(ticker, pe, fpt, li, str(val), doc_id) for pe, fpt, li, val, doc_id in rows],
    )
    conn.commit()


def _seed_clean(conn: sqlite3.Connection, ticker: str = "CLEAN") -> None:
    """A calendar-year (December) reporter where fiscal_period_type and calendar
    quarter already agree — the fix must be a no-op here."""
    quarters = [
        ("2016-03-31", "Q1", 1000, 100),
        ("2016-06-30", "Q2", 1100, 110),
        ("2016-09-30", "Q3", 1200, 120),
        ("2016-12-31", "Q4", 1300, 130),
    ]
    rows: list[_FactRow] = []
    doc = 100
    for period_end, label, revenue, net_income in quarters:
        pe = f"{period_end} 00:00:00"
        rows.append((pe, label, "revenue", revenue * 1_000_000, doc))
        rows.append((pe, label, "net_income", net_income * 1_000_000, doc))
        doc += 1
    conn.executemany(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        " value, currency, source_doc_id) VALUES (?, ?, ?, ?, ?, 'USD', ?)",
        [(ticker, pe, fpt, li, str(val), doc_id) for pe, fpt, li, val, doc_id in rows],
    )
    conn.commit()


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _make_db(tmp_path: Path) -> Path:
    repo_root = tmp_path
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    db = repo_root / "data" / "portfolio.db"
    conn = _open(db)
    conn.executescript(_DDL)
    conn.close()
    return db


def _disp_quarters(conn: sqlite3.Connection, ticker: str) -> dict[str, dict[str, object]]:
    rows = _load_quarterly(conn, ticker)
    return {
        quarter_label(cast("str", r["period_end"])): r for r in _dedupe_by_calendar_quarter(rows)
    }


def _num(row: dict[str, object], col: str) -> float:
    return float(str(row[col]))


# --------------------------------------------------------------------------- #
# loader-level
# --------------------------------------------------------------------------- #


def test_load_quarterly_returns_one_row_per_calendar_quarter(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = _open(db)
    _seed_offcal(conn)
    disp = _disp_quarters(conn, "OFFCAL")
    conn.close()

    # All four calendar quarters present and on their true dates — none dropped,
    # none shifted forward a quarter.
    assert set(disp) == {"2016 Q1", "2016 Q2", "2016 Q3", "2016 Q4"}
    assert str(disp["2016 Q3"]["period_end"])[:10] == "2016-07-31"
    assert str(disp["2016 Q4"]["period_end"])[:10] == "2016-10-30"


def test_mislabeled_fact_does_not_leak_into_wrong_quarter(tmp_path: Path) -> None:
    """The Q3-labeled, Oct-30-dated net_income (higher source_doc_id) must land
    in calendar Q4, NOT calendar Q3 — the core of the metrics-view bug."""
    db = _make_db(tmp_path)
    conn = _open(db)
    _seed_offcal(conn)
    disp = _disp_quarters(conn, "OFFCAL")
    conn.close()

    # Revenue is keyed by date regardless of label.
    assert _num(disp["2016 Q3"], "revenue") == 2821e6
    assert _num(disp["2016 Q4"], "revenue") == 3297e6
    # The July quarter keeps its own net_income (505), NOT the Oct value (610)
    # that the mislabel would have injected under fiscal_period_type grouping.
    assert _num(disp["2016 Q3"], "net_income") == 505e6
    assert _num(disp["2016 Q4"], "net_income") == 610e6


def test_clean_reporter_unaffected(tmp_path: Path) -> None:
    """For a December reporter (labels already match calendar quarters) the
    direct read reproduces the obvious series — the fix is a no-op."""
    db = _make_db(tmp_path)
    conn = _open(db)
    _seed_clean(conn)
    disp = _disp_quarters(conn, "CLEAN")
    conn.close()

    assert set(disp) == {"2016 Q1", "2016 Q2", "2016 Q3", "2016 Q4"}
    assert _num(disp["2016 Q1"], "revenue") == 1000e6
    assert _num(disp["2016 Q4"], "revenue") == 1300e6
    assert _num(disp["2016 Q2"], "net_income") == 110e6


def test_load_annual_one_row_per_fiscal_year(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = _open(db)
    _seed_offcal(conn)
    annual = _load_annual(conn, "OFFCAL")
    conn.close()

    years = [int(str(r["period_end"])[:4]) for r in annual]
    assert years == [2016]  # exactly one row for the one fiscal year seeded
    assert _num(annual[0], "revenue") == 10825e6
    assert _num(annual[0], "net_income") == 1721e6


# --------------------------------------------------------------------------- #
# section-level (full build)
# --------------------------------------------------------------------------- #


def _series(section_line_items: object, name: str) -> dict[str, float | None]:
    """{quarter_label: value} for the named quarterly line item."""
    from report.models import QuarterlyLineItem

    assert isinstance(section_line_items, list)
    for li in section_line_items:  # pyright: ignore[reportUnknownVariableType]
        assert isinstance(li, QuarterlyLineItem)
        if li.line_item == name:
            return dict(zip(li.quarters, li.values, strict=True))
    raise AssertionError(f"line item {name!r} not rendered")


def test_build_quarterly_series_unshifted_and_reconciles_to_annual(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = _open(db)
    _seed_offcal(conn)
    conn.close()

    section = build("OFFCAL", tmp_path)

    assert section.status == SectionStatus.OK
    # No dropped/duplicated quarters in the displayed labels.
    for label in ("2016 Q1", "2016 Q2", "2016 Q3", "2016 Q4"):
        assert label in section.quarter_labels
    assert len(section.quarter_labels) == len(set(section.quarter_labels))

    revenue = _series(section.line_items, "Revenue")
    assert revenue["2016 Q2"] == 2450.0  # present, not dropped
    assert revenue["2016 Q3"] == 2821.0  # not shifted forward
    assert revenue["2016 Q4"] == 3297.0

    # The decisive invariant: the four displayed quarterly net incomes sum to
    # the annual net income. Under the old shift/collision they could not.
    net_income = _series(section.line_items, "Net income")
    quarterly_sum = sum(v for q, v in net_income.items() if q.startswith("2016") and v is not None)
    annual_ni = next(
        item.values[item.years.index(2016)]
        for item in section.annual_line_items
        if item.line_item == "Net income"
    )
    assert annual_ni == 1721.0
    assert quarterly_sum == annual_ni


def test_build_missing_facts_table_reports_missing(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "data").mkdir(parents=True, exist_ok=True)
    # Empty DB: no financial_facts table.
    sqlite3.connect(repo_root / "data" / "portfolio.db").close()

    section = build("OFFCAL", repo_root)
    assert section.status == SectionStatus.MISSING_DATA
