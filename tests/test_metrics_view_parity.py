"""Parity guard for ``report.metrics_view`` against the real ``metrics`` /
``ratios`` SQL views.

``report.metrics_view`` inlines the views' dedup + pivot with ``WHERE ticker=?``
*inside* the ROW_NUMBER partition (so cost scales with one ticker's facts, not
the whole table). This test extracts the ACTUAL view DDL from the
highest-numbered migration that defines the views (today ``0075_locator_schema``;
the extraction auto-follows any future redefinition), builds both the views and a
seeded multi-ticker ``financial_facts``, and asserts the helpers return identical
rows to ``SELECT * FROM {view} WHERE ticker=?``. If a migration changes a view,
this fails until ``report.metrics_view`` is updated in lockstep — the invariant
the module's docstring promises.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from report import metrics_view

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_FINANCIAL_FACTS_DDL = """
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value NUMERIC NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL
);
"""

# (ticker, period_end, fiscal_period_type, line_item, value, source_doc_id)
_FACTS: list[tuple[str, str, str, str, float, int]] = [
    # AAA FY2024: a newer source_doc_id (2) supersedes the older (1) for revenue —
    # exercises the dedup ROW_NUMBER (must pick the higher source_doc_id).
    ("AAA", "2024-12-31", "FY", "revenue", 1000.0, 1),
    ("AAA", "2024-12-31", "FY", "revenue", 1100.0, 2),
    ("AAA", "2024-12-31", "FY", "gross_profit", 600.0, 2),
    ("AAA", "2024-12-31", "FY", "operating_income", 300.0, 2),
    ("AAA", "2024-12-31", "FY", "net_income", 220.0, 2),
    ("AAA", "2024-12-31", "FY", "total_assets", 5000.0, 2),
    ("AAA", "2024-12-31", "FY", "total_stockholders_equity", 2500.0, 2),
    ("AAA", "2024-12-31", "FY", "free_cash_flow", 180.0, 2),
    ("AAA", "2024-12-31", "FY", "capital_expenditure", 150.0, 2),
    ("AAA", "2023-12-31", "FY", "revenue", 900.0, 1),
    ("AAA", "2025-09-30", "TTM", "revenue", 1250.0, 3),
    ("AAA", "2025-09-30", "TTM", "net_income", 260.0, 3),
    # BBB: overlapping years with AAA in the global view — isolation check.
    ("BBB", "2024-12-31", "FY", "revenue", 5000.0, 1),
    ("BBB", "2024-12-31", "FY", "operating_income", 800.0, 1),
    ("BBB", "2024-12-31", "FY", "net_income", 0.0, 1),  # NULLIF→NULL ratio path
    ("BBB", "2025-06-30", "Q2", "revenue", 1300.0, 4),
    # CCC: a third ticker so any cross-ticker leak would change row counts.
    ("CCC", "2024-12-31", "FY", "revenue", 10.0, 1),
]


def _latest_view_ddls() -> tuple[str, str]:
    """(metrics_ddl, ratios_ddl) extracted from the highest-numbered migration
    that (re)defines both views — so this guard tracks the production view even
    after a future redefinition, with no migration-chain bootstrap."""
    versions = PROJECT_ROOT / "alembic" / "versions"
    chosen: Path | None = None
    for path in sorted(versions.glob("*.py")):  # 4-digit zero-padded → lexical == numeric
        text = path.read_text(encoding="utf-8")
        if (
            "CREATE VIEW IF NOT EXISTS metrics AS" in text
            and "CREATE VIEW IF NOT EXISTS ratios AS" in text
        ):
            chosen = path
    assert chosen is not None, "no migration defines the metrics/ratios views"
    text = chosen.read_text(encoding="utf-8")

    def _extract(view: str) -> str:
        m = re.search(rf'(CREATE VIEW IF NOT EXISTS {view} AS.*?)"""', text, re.S)
        assert m is not None, f"could not extract {view} DDL from {chosen.name}"
        return m.group(1)

    return _extract("metrics"), _extract("ratios")


def _db_with_views(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    metrics_ddl, ratios_ddl = _latest_view_ddls()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_FINANCIAL_FACTS_DDL)
    conn.executescript(metrics_ddl)
    conn.executescript(ratios_ddl)
    conn.executemany(
        "INSERT INTO financial_facts"
        " (ticker, period_end, fiscal_period_type, line_item, value, currency, unit, source_doc_id)"
        " VALUES (?, ?, ?, ?, ?, 'USD', 'actual', ?)",
        _FACTS,
    )
    conn.commit()
    conn.close()
    return db_path


def _key(row: dict[str, object]) -> tuple[object, object]:
    return (str(row.get("fiscal_period_type")), str(row.get("fiscal_year")))


def test_metrics_view_helpers_match_the_real_views(tmp_path: Path) -> None:
    db_path = _db_with_views(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    seen_rows = 0
    for ticker in ("AAA", "BBB", "CCC"):
        for view, helper in (
            ("metrics", metrics_view.metrics_rows),
            ("ratios", metrics_view.ratios_rows),
        ):
            view_rows = sorted(
                (
                    dict(r)
                    for r in conn.execute(f"SELECT * FROM {view} WHERE ticker = ?", (ticker,))
                ),
                key=_key,
            )
            helper_rows = sorted(helper(conn, ticker), key=_key)
            assert helper_rows == view_rows, f"{ticker}/{view} drift vs the real view"
            seen_rows += len(helper_rows)

    conn.close()
    assert seen_rows > 0  # guard against a vacuous pass


def test_annual_and_ttm_helpers_scope_and_order(tmp_path: Path) -> None:
    db_path = _db_with_views(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    annual = metrics_view.annual_rows(conn, "AAA", "metrics", n=4)
    # FY rows only, oldest-first; the deduped revenue is the newer (1100, not 1000).
    assert [r["fiscal_year"] for r in annual] == [2023, 2024]
    assert annual[-1]["revenue"] == 1100.0

    ttm = metrics_view.ttm_row(conn, "AAA", "metrics")
    assert ttm is not None
    assert ttm["fiscal_period_type"] == "TTM"
    assert ttm["revenue"] == 1250.0

    # BBB has no TTM row → None.
    assert metrics_view.ttm_row(conn, "BBB", "metrics") is None
    conn.close()
