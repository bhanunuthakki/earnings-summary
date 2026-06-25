"""Per-ticker scoped equivalents of the global ``metrics`` / ``ratios`` SQL views.

The global views (``alembic/versions/0012_metrics_and_ratios_views.py``, last
redefined in ``0075_locator_schema.py``) run a ``ROW_NUMBER()`` window over
*all* of ``financial_facts``. ``SELECT * FROM metrics WHERE ticker = ?`` cannot
push the ``ticker`` predicate inside that window, so the cost grows with the
TOTAL number of facts in the DB rather than the one ticker being rendered — the
documented "metrics/ratios views are slow" cliff, and it sits on the brief-build
render path (``report.sections.evaluation_snapshot`` and
``execution/build_diligence.py``).

These helpers inline the identical dedup + pivot, but with ``WHERE ticker = ?``
applied *before* the partition, so the window only sorts that one ticker's slice
— served by the ticker-led UNIQUE index ``uq_financial_facts_provenance`` from
``0011_facts_unique_indexes.py``. The result rows are column-identical to the
views, so a caller that previously ran ``SELECT * FROM metrics WHERE ticker = ?``
can swap to :func:`metrics_rows` with no shape change.

No extra index is needed: the window function forces a sort step regardless of
indexing, and the existing ticker-led unique index already serves the
``WHERE ticker = ?`` seek — the asymptotic fix is the ticker-scoping itself.

INVARIANT: the column lists below MUST stay identical to the view DDL in the
latest migration that (re)defines them. ``tests/test_metrics_view_parity.py``
guards this — it asserts these helpers return the same rows as the real
migration-built views for a seeded multi-ticker DB, so a future migration that
changes a view fails the test until this file is updated in lockstep.
"""

from __future__ import annotations

import sqlite3

# The per-ticker dedup CTE: identical to the view's, plus ``WHERE ticker = ?``
# inside the scan so the ROW_NUMBER partition only covers this ticker's facts.
_DEDUP_CTE = """dedup AS (
    SELECT
        ticker,
        CAST(substr(period_end, 1, 4) AS INTEGER) AS fiscal_year,
        fiscal_period_type,
        line_item,
        value,
        currency,
        period_end,
        ROW_NUMBER() OVER (
            PARTITION BY
                ticker,
                CAST(substr(period_end, 1, 4) AS INTEGER),
                fiscal_period_type,
                line_item
            ORDER BY source_doc_id DESC, period_end DESC
        ) AS rn
    FROM financial_facts
    WHERE ticker = ?
)"""

# The pivot — column-for-column identical to the ``metrics`` view DDL.
_METRICS_SELECT = """SELECT
    ticker,
    fiscal_year,
    fiscal_period_type,
    MAX(period_end) AS period_end,
    MAX(currency) AS currency,
    MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
    MAX(CASE WHEN line_item = 'cost_of_revenue' THEN value END) AS cost_of_revenue,
    MAX(CASE WHEN line_item = 'gross_profit' THEN value END) AS gross_profit,
    MAX(CASE WHEN line_item = 'research_and_development' THEN value END) AS rd,
    MAX(CASE WHEN line_item = 'sga' THEN value END) AS sga,
    MAX(CASE WHEN line_item = 'operating_expenses' THEN value END) AS operating_expenses,
    MAX(CASE WHEN line_item = 'operating_income' THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'ebit' THEN value END) AS ebit,
    MAX(CASE WHEN line_item = 'ebitda' THEN value END) AS ebitda,
    MAX(CASE WHEN line_item = 'net_income' THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'eps' THEN value END) AS eps,
    MAX(CASE WHEN line_item = 'eps_diluted' THEN value END) AS eps_diluted,
    MAX(CASE WHEN line_item = 'weighted_avg_shares_diluted' THEN value END)
        AS weighted_avg_shares_diluted,
    MAX(CASE WHEN line_item = 'total_assets' THEN value END) AS total_assets,
    MAX(CASE WHEN line_item = 'total_current_assets' THEN value END) AS total_current_assets,
    MAX(CASE WHEN line_item = 'cash_and_equivalents' THEN value END) AS cash_and_equivalents,
    MAX(CASE WHEN line_item = 'total_liabilities' THEN value END) AS total_liabilities,
    MAX(CASE WHEN line_item = 'total_current_liabilities' THEN value END)
        AS total_current_liabilities,
    MAX(CASE WHEN line_item = 'total_stockholders_equity' THEN value END) AS total_equity,
    MAX(CASE WHEN line_item = 'total_debt' THEN value END) AS total_debt,
    MAX(CASE WHEN line_item = 'long_term_debt' THEN value END) AS long_term_debt,
    MAX(CASE WHEN line_item = 'net_debt' THEN value END) AS net_debt,
    MAX(CASE WHEN line_item = 'operating_cash_flow' THEN value END) AS operating_cash_flow,
    MAX(CASE WHEN line_item = 'capital_expenditure' THEN value END) AS capex,
    MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
    MAX(CASE WHEN line_item = 'common_dividends_paid' THEN value END) AS dividends_paid,
    MAX(CASE WHEN line_item = 'common_stock_repurchased' THEN value END) AS stock_repurchased,
    MAX(CASE WHEN line_item = 'rpo' THEN value END) AS rpo,
    MAX(CASE WHEN line_item = 'contract_liabilities' THEN value END) AS contract_liabilities,
    MAX(CASE WHEN line_item = 'operating_lease_liability' THEN value END)
        AS operating_lease_liability,
    MAX(CASE WHEN line_item = 'operating_lease_rou_asset' THEN value END)
        AS operating_lease_rou_asset
FROM dedup
WHERE rn = 1
GROUP BY ticker, fiscal_year, fiscal_period_type"""

# The ratios projection — column-for-column identical to the ``ratios`` view DDL,
# reading from the ``metrics`` CTE built above.
_RATIOS_SELECT = """SELECT
    ticker,
    fiscal_year,
    fiscal_period_type,
    period_end,
    currency,
    revenue,
    gross_profit,
    operating_income,
    net_income,
    free_cash_flow,
    total_assets,
    total_equity,
    total_debt,
    capex,
    operating_cash_flow,
    rpo,
    CAST(gross_profit AS REAL) / NULLIF(revenue, 0) AS gross_margin,
    CAST(operating_income AS REAL) / NULLIF(revenue, 0) AS operating_margin,
    CAST(net_income AS REAL) / NULLIF(revenue, 0) AS net_margin,
    CAST(free_cash_flow AS REAL) / NULLIF(revenue, 0) AS fcf_margin,
    CAST(net_income AS REAL) / NULLIF(total_assets, 0) AS roa,
    CAST(net_income AS REAL) / NULLIF(total_equity, 0) AS roe,
    CAST(operating_income AS REAL) / NULLIF(total_assets, 0) AS roa_op,
    CAST(capex AS REAL) / NULLIF(revenue, 0) AS capex_intensity
FROM metrics"""

_METRICS_SQL = f"WITH {_DEDUP_CTE}\n{_METRICS_SELECT}"
_RATIOS_SQL = f"WITH {_DEDUP_CTE},\nmetrics AS (\n{_METRICS_SELECT}\n)\n{_RATIOS_SELECT}"


def _fetch_dicts(conn: sqlite3.Connection, sql: str, ticker: str) -> list[dict[str, object]]:
    """Run a ticker-scoped query and return plain dicts, independent of whatever
    ``row_factory`` the caller's connection happens to use."""
    cur = conn.execute(sql, (ticker,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def metrics_rows(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Every ``metrics``-view row for one ticker, via a ticker-scoped dedup
    window (cost scales with this ticker's facts, not the whole table)."""
    return _fetch_dicts(conn, _METRICS_SQL, ticker)


def ratios_rows(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Every ``ratios``-view row for one ticker (see :func:`metrics_rows`)."""
    return _fetch_dicts(conn, _RATIOS_SQL, ticker)


def _rows_for(conn: sqlite3.Connection, ticker: str, view: str) -> list[dict[str, object]]:
    if view == "metrics":
        return metrics_rows(conn, ticker)
    if view == "ratios":
        return ratios_rows(conn, ticker)
    raise ValueError(f"unknown view {view!r}; expected 'metrics' or 'ratios'")


def annual_rows(
    conn: sqlite3.Connection, ticker: str, view: str, n: int
) -> list[dict[str, object]]:
    """The N most-recent ``FY`` rows from the ticker-scoped ``metrics`` /
    ``ratios`` result, oldest-first — the ticker-scoped equivalent of
    ``SELECT * FROM {view} WHERE ticker = ? AND fiscal_period_type = 'FY'
    ORDER BY period_end DESC LIMIT n`` (then reversed to oldest-first)."""
    rows = [r for r in _rows_for(conn, ticker, view) if r.get("fiscal_period_type") == "FY"]
    rows.sort(key=lambda r: str(r.get("period_end") or ""), reverse=True)
    rows = rows[:n]
    rows.reverse()
    return rows


def ttm_row(conn: sqlite3.Connection, ticker: str, view: str) -> dict[str, object] | None:
    """The most-recent ``TTM`` row from the ticker-scoped result, or None — the
    ticker-scoped equivalent of ``SELECT * FROM {view} WHERE ticker = ? AND
    fiscal_period_type = 'TTM' ORDER BY period_end DESC LIMIT 1``."""
    rows = [r for r in _rows_for(conn, ticker, view) if r.get("fiscal_period_type") == "TTM"]
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("period_end") or ""), reverse=True)
    return rows[0]
