"""Tests for src/pipeline/sec_xbrl.py — period-span resolution + accession upsert + tag ladders."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from models.facts import FiscalPeriodType
from pipeline.sec_xbrl import (
    CIK_MAP,
    NO_SEC_FILERS,
    TAG_LADDERS,
    _AccessionRecord,
    _enumerate_accessions,
    _infer_fye_month,
    _modal_currency,
    _period_span_months,
    _resolve_fiscal_period_type,
    _same_doc_pick_key,
    insert_facts_from_companyfacts,
    upsert_accession_documents,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24,6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


# Snapshot of the tracked universe (portfolio + evaluation + watchlist,
# prod tracked_companies 2026-07-02). Refresh this set when the book changes;
# the fetch script itself scopes from the live DB, this only guards CIK_MAP.
_TRACKED_UNIVERSE_2026_07 = {
    # portfolio
    "BKNG", "BN", "MELI", "META", "NOW", "NU", "NVO", "RBRK", "UBER", "VEEV", "WIX",
    # evaluation
    "ABNB", "AMZN", "AVGO", "BHP", "CDNS", "CGEH", "CRWV", "DHR", "DLO", "FCX",
    "FIGR", "FNV", "FRVO", "GOOG", "LLY", "MDB", "NBIS", "NSP", "NTDOY", "NTRA",
    "NVDA", "ORCL", "RGEN", "SNOW", "SNPS", "SOFI", "TECH", "TEM", "TMO", "V", "WGS",
    # watchlist
    "AMAT", "AMD", "ASML", "AWK", "BAM", "BEPC", "BIPC", "BRK-B", "CFLT", "CIEN",
    "COHR", "COST", "CRM", "CRWD", "DDOG", "ENB", "EPD", "ESTC", "FTNT", "GTLB",
    "HASI", "HBM", "HDB", "HEI", "IBN", "ISRG", "IVN", "JPM", "KLAC", "KVYO",
    "LITE", "LMND", "MA", "MRVL", "MSFT", "MU", "NEE", "NET", "NVS", "OKTA",
    "PANW", "RIO", "ROP", "SCCO", "SE", "STNE", "TDG", "TECK", "TOL", "TRP",
    "TSM", "TXN", "WMB", "WPM", "XEL", "ZS",
}  # fmt: skip


def test_cik_map_covers_all_tracked_tickers() -> None:
    """Every tracked ticker has a CIK except the documented no-SEC filers."""
    covered = _TRACKED_UNIVERSE_2026_07 - NO_SEC_FILERS
    missing = sorted(covered - set(CIK_MAP))
    assert not missing, f"tracked tickers missing from CIK_MAP: {missing}"


def test_no_sec_filers_never_carry_a_cik() -> None:
    """A NO_SEC_FILERS entry with a CIK is a contradiction — one list must win."""
    overlap = NO_SEC_FILERS & set(CIK_MAP)
    assert not overlap, f"tickers in both NO_SEC_FILERS and CIK_MAP: {sorted(overlap)}"


def test_ciks_are_ten_digit_zero_padded() -> None:
    for ticker, cik in CIK_MAP.items():
        assert len(cik) == 10 and cik.isdigit(), (ticker, cik)


def test_period_span_months_handles_quarterly() -> None:
    """Q3 fact: start=2025-07-01, end=2025-09-30 -> ~3 months."""
    assert _period_span_months("2025-07-01", "2025-09-30") == 3


def test_period_span_months_handles_annual() -> None:
    assert _period_span_months("2024-01-01", "2024-12-31") == 12


def test_period_span_months_handles_ytd_9month() -> None:
    """9-month YTD: start=2025-01-01, end=2025-09-30 -> ~9 months."""
    assert _period_span_months("2025-01-01", "2025-09-30") == 9


def test_resolve_period_skips_9month_aggregation() -> None:
    """9M YTD value at end=Sep 30 -> None (would conflict with Q3 standalone)."""
    fpt = _resolve_fiscal_period_type(fp="Q3", start_date="2025-01-01", end_date="2025-09-30")
    assert fpt is None


def test_resolve_period_returns_q3_for_3month_at_sep30() -> None:
    fpt = _resolve_fiscal_period_type(fp="Q3", start_date="2025-07-01", end_date="2025-09-30")
    assert fpt == FiscalPeriodType.Q3


def test_resolve_period_returns_fy_for_12month() -> None:
    fpt = _resolve_fiscal_period_type(fp="FY", start_date="2024-01-01", end_date="2024-12-31")
    assert fpt == FiscalPeriodType.FY


def test_resolve_period_returns_q4_for_balance_sheet_at_dec31() -> None:
    """Balance-sheet items have only `end`; treat Dec 31 as FY snapshot."""
    fpt = _resolve_fiscal_period_type(fp=None, start_date=None, end_date="2024-12-31")
    assert fpt == FiscalPeriodType.FY


def test_enumerate_accessions_dedupes() -> None:
    """Same accession appearing in multiple tag entries gets a single record."""
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0001-22-001",
                                "form": "10-K",
                                "filed": "2025-02-01",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": 100,
                            },
                            {
                                "accn": "0001-22-001",
                                "form": "10-K",
                                "filed": "2025-02-01",
                                "fy": 2024,
                                "fp": "FY",
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": 100,
                            },
                        ]
                    }
                }
            }
        }
    }
    out = _enumerate_accessions(payload)
    assert len(out) == 1
    assert "0001-22-001" in out


def test_upsert_accession_documents_idempotent(conn: sqlite3.Connection) -> None:
    """Re-running upsert with the same accessions inserts no new documents."""
    accessions = {
        "0001-22-001": _AccessionRecord(
            accession="0001-22-001",
            form="10-K",
            filed="2025-02-01",
            fy=2024,
            fp="FY",
        ),
    }
    first = upsert_accession_documents(
        conn, ticker="X", accessions=accessions, project_root=Path("/tmp")
    )
    second = upsert_accession_documents(
        conn, ticker="X", accessions=accessions, project_root=Path("/tmp")
    )
    assert first == second
    n = conn.execute("SELECT COUNT(*) FROM documents WHERE source_type='sec_xbrl'").fetchone()[0]
    assert n == 1


def test_insert_facts_skips_ytd_aggregations(conn: sqlite3.Connection) -> None:
    """Mixed payload: Q3 standalone (3 month) + 9M YTD; only Q3 gets inserted."""
    accessions = {
        "0001-22-001": _AccessionRecord(
            accession="0001-22-001",
            form="10-Q",
            filed="2025-10-30",
            fy=2025,
            fp="Q3",
        ),
    }
    accn_to_doc = upsert_accession_documents(
        conn, ticker="X", accessions=accessions, project_root=Path("/tmp")
    )
    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            # Q3 standalone (3 months)
                            {
                                "accn": "0001-22-001",
                                "form": "10-Q",
                                "filed": "2025-10-30",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-07-01",
                                "end": "2025-09-30",
                                "val": 4_000_000_000,
                            },
                            # 9-month YTD (skipped)
                            {
                                "accn": "0001-22-001",
                                "form": "10-Q",
                                "filed": "2025-10-30",
                                "fy": 2025,
                                "fp": "Q3",
                                "start": "2025-01-01",
                                "end": "2025-09-30",
                                "val": 11_000_000_000,
                            },
                        ]
                    }
                }
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    assert inserted == 1
    rows = conn.execute(
        "SELECT value, fiscal_period_type FROM financial_facts WHERE ticker='X'"
    ).fetchall()
    assert len(rows) == 1
    assert int(dict(rows[0])["value"]) == 4_000_000_000
    assert dict(rows[0])["fiscal_period_type"] == "Q3"


# ---------------------------------------------------------------------------
# Tag-ladder expansion (full statement coverage)
# ---------------------------------------------------------------------------

_FMP_LINE_ITEMS = {
    # src/compute/income_statement.py + balance_sheet.py + cashflow.py specs —
    # the canonical names FMP writes; every ladder must target one of these.
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "research_and_development",
    "sga",
    "operating_expenses",
    "operating_income",
    "ebit",
    "ebitda",
    "net_income",
    "income_before_tax",
    "income_tax_expense",
    "interest_income",
    "interest_expense",
    "depreciation_and_amortization",
    "eps",
    "eps_diluted",
    "weighted_avg_shares",
    "weighted_avg_shares_diluted",
    "cash_and_equivalents",
    "short_term_investments",
    "cash_and_short_term_investments",
    "net_receivables",
    "accounts_receivable",
    "inventory",
    "other_current_assets",
    "total_current_assets",
    "property_plant_equipment_net",
    "goodwill",
    "intangible_assets",
    "goodwill_and_intangible_assets",
    "long_term_investments",
    "other_non_current_assets",
    "total_non_current_assets",
    "total_assets",
    "accounts_payable",
    "short_term_debt",
    "deferred_revenue",
    "other_current_liabilities",
    "total_current_liabilities",
    "long_term_debt",
    "deferred_revenue_non_current",
    "other_non_current_liabilities",
    "total_non_current_liabilities",
    "total_liabilities",
    "common_stock",
    "retained_earnings",
    "additional_paid_in_capital",
    "total_stockholders_equity",
    "total_equity",
    "total_debt",
    "net_debt",
    "net_income_cf",
    "depreciation_and_amortization_cf",
    "deferred_income_tax",
    "stock_based_compensation",
    "change_in_working_capital",
    "other_non_cash_items",
    "net_cash_from_operating",
    "operating_cash_flow",
    "investments_in_ppe",
    "acquisitions_net",
    "purchases_of_investments",
    "sales_maturities_of_investments",
    "net_cash_from_investing",
    "net_debt_issuance",
    "common_stock_repurchased",
    "net_dividends_paid",
    "common_dividends_paid",
    "net_cash_from_financing",
    "net_change_in_cash",
    "cash_at_end_of_period",
    "capital_expenditure",
    "free_cash_flow",
    "income_taxes_paid",
    "interest_paid",
}


def test_every_ladder_targets_an_fmp_canonical_line_item() -> None:
    """SEC rows must land on the SAME logical keys FMP writes, or the
    tier-aware dedup + source_disagreement validation never see them."""
    for ladder in TAG_LADDERS:
        assert ladder.line_item in _FMP_LINE_ITEMS, ladder.line_item


def test_outflow_ladders_carry_negative_sign() -> None:
    """FMP stores cash outflows negative; GAAP Payments*/Purchase* elements
    are positive payment amounts, so those ladders must flip the sign."""
    by_item = {ladder.line_item: ladder for ladder in TAG_LADDERS}
    for item in (
        "capital_expenditure",
        "investments_in_ppe",
        "common_stock_repurchased",
        "net_dividends_paid",
        "common_dividends_paid",
        "acquisitions_net",
    ):
        assert by_item[item].sign == -1, item
    for item in ("stock_based_compensation", "income_taxes_paid", "interest_paid", "revenue"):
        assert by_item[item].sign == 1, item


def _payload_one_tag(
    namespace: str,
    tag: str,
    entries: list[dict[str, object]],
    unit_code: str = "USD",
) -> dict[str, object]:
    return {"facts": {namespace: {tag: {"units": {unit_code: entries}}}}}


def _q_entry(val: object, *, accn: str = "0001-22-001", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "accn": accn,
        "form": "10-Q",
        "filed": "2025-10-30",
        "fy": 2025,
        "fp": "Q3",
        "start": "2025-07-01",
        "end": "2025-09-30",
        "val": val,
    }
    entry.update(overrides)
    return entry


def _register_accession(conn: sqlite3.Connection, accn: str = "0001-22-001") -> dict[str, int]:
    return upsert_accession_documents(
        conn,
        ticker="X",
        accessions={
            accn: _AccessionRecord(
                accession=accn, form="10-Q", filed="2025-10-30", fy=2025, fp="Q3"
            )
        },
        project_root=Path("/tmp"),
    )


def test_ladder_first_rung_wins_per_period(conn: sqlite3.Connection) -> None:
    """Same period tagged under Revenues AND RevenueFromContract... -> only the
    first rung inserts; a period ONLY in the lower rung still gets covered."""
    accn_to_doc = _register_accession(conn)
    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_q_entry(100)]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            _q_entry(101),  # same period as rung 0 -> skipped
                            _q_entry(
                                55, start="2025-04-01", end="2025-06-30", fp="Q2"
                            ),  # only here -> inserted
                        ]
                    }
                },
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    assert inserted == 2
    rows = {
        (r["fiscal_period_type"]): int(r["value"])
        for r in conn.execute(
            "SELECT fiscal_period_type, value FROM financial_facts WHERE line_item='revenue'"
        ).fetchall()
    }
    assert rows == {"Q3": 100, "Q2": 55}


def test_payments_capex_lands_negative(conn: sqlite3.Connection) -> None:
    accn_to_doc = _register_accession(conn)
    payload = _payload_one_tag(
        "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", [_q_entry(224_000_000)]
    )
    insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    rows = conn.execute(
        "SELECT line_item, value FROM financial_facts ORDER BY line_item"
    ).fetchall()
    # dual-written under both FMP capex names, both negative
    assert [(r["line_item"], int(r["value"])) for r in rows] == [
        ("capital_expenditure", -224_000_000),
        ("investments_in_ppe", -224_000_000),
    ]


def test_eps_parses_per_share_unit(conn: sqlite3.Connection) -> None:
    accn_to_doc = _register_accession(conn)
    payload = _payload_one_tag(
        "us-gaap", "EarningsPerShareBasic", [_q_entry(1.15)], unit_code="USD/shares"
    )
    insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    row = conn.execute("SELECT line_item, value, currency, unit FROM financial_facts").fetchone()
    assert row["line_item"] == "eps"
    assert float(row["value"]) == pytest.approx(1.15)
    assert row["currency"] == "USD"
    assert row["unit"] == "actual"


def test_share_counts_use_count_unit_and_null_currency(conn: sqlite3.Connection) -> None:
    accn_to_doc = _register_accession(conn)
    payload = _payload_one_tag(
        "us-gaap",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        [_q_entry(413_000_000)],
        unit_code="shares",
    )
    insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    row = conn.execute("SELECT line_item, value, currency, unit FROM financial_facts").fetchone()
    assert row["line_item"] == "weighted_avg_shares"
    assert row["currency"] is None
    assert row["unit"] == "count"


def test_stockholders_equity_maps_to_parent_only_line_item(conn: sqlite3.Connection) -> None:
    """StockholdersEquity is parent-only -> total_stockholders_equity; the
    NCI-inclusive tag feeds total_equity (matching FMP's semantics)."""
    accn_to_doc = _register_accession(conn)

    def _instant(val: int) -> dict[str, object]:
        entry = _q_entry(val)
        del entry["start"]  # balance-sheet snapshot: point-in-time, no span
        return entry

    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {"units": {"USD": [_instant(900)]}},
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": {
                    "units": {"USD": [_instant(1000)]}
                },
            }
        }
    }
    insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    rows = {
        r["line_item"]: int(r["value"])
        for r in conn.execute("SELECT line_item, value FROM financial_facts").fetchall()
    }
    assert rows["total_stockholders_equity"] == 900
    assert rows["total_equity"] == 1000


def test_infer_fye_month_from_annual_filings() -> None:
    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"fp": "FY", "form": "10-K", "end": "2024-08-29", "val": 1},
                            {"fp": "FY", "form": "10-K", "end": "2023-08-31", "val": 1},
                            {"fp": "Q1", "form": "10-Q", "end": "2024-11-28", "val": 1},
                        ]
                    }
                }
            }
        }
    }
    assert _infer_fye_month(payload) == 8


def test_infer_fye_month_defaults_to_december() -> None:
    assert _infer_fye_month({"facts": {}}) == 12


def test_instant_resolution_uses_fye_month() -> None:
    """MU-style August FYE: a Nov-28 snapshot is fiscal Q1, not the old
    hardcoded Q4; the FYE snapshot itself resolves FY."""
    fpt = _resolve_fiscal_period_type(fp="Q1", start_date=None, end_date="2024-11-28", fye_month=8)
    assert fpt == FiscalPeriodType.Q1
    fpt = _resolve_fiscal_period_type(fp="FY", start_date=None, end_date="2024-08-29", fye_month=8)
    assert fpt == FiscalPeriodType.FY


def test_instant_ignores_filing_fp_for_comparatives() -> None:
    """A 10-Q carries the prior FYE balance sheet as a comparative with the
    FILING's fp (Q3) — the snapshot must still resolve by its own end month."""
    fpt = _resolve_fiscal_period_type(fp="Q3", start_date=None, end_date="2024-12-31", fye_month=12)
    assert fpt == FiscalPeriodType.FY


def test_fye_instant_dual_writes_fy_and_q4(conn: sqlite3.Connection) -> None:
    """The FYE balance-sheet snapshot lands under both FY and Q4 — FMP writes
    it from both its annual and quarterly endpoints."""
    accn_to_doc = _register_accession(conn)
    entry: dict[str, object] = {
        "accn": "0001-22-001",
        "form": "10-K",
        "filed": "2025-02-01",
        "fy": 2024,
        "fp": "FY",
        "end": "2024-12-31",
        "val": 5_000,
    }
    payload = _payload_one_tag("us-gaap", "Assets", [entry])
    inserted = insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    assert inserted == 2
    fpts = {
        r["fiscal_period_type"]
        for r in conn.execute(
            "SELECT fiscal_period_type FROM financial_facts WHERE line_item='total_assets'"
        ).fetchall()
    }
    assert fpts == {"FY", "Q4"}


def test_modal_currency_prefers_fuller_series() -> None:
    """TSM-style dual tagging: the local currency carries the longer history
    and must win over sparse USD convenience translations."""
    units: dict[str, object] = {
        "TWD": [{"val": 1}, {"val": 2}, {"val": 3}],
        "USD": [{"val": 1}],
    }
    assert _modal_currency(units, "monetary") == "TWD"
    # tie breaks alphabetically -> deterministic
    units_tie: dict[str, object] = {"USD": [{"val": 1}], "TWD": [{"val": 2}]}
    assert _modal_currency(units_tie, "monetary") == "TWD"


def test_ifrs_filer_maps_parent_profit_to_net_income(conn: sqlite3.Connection) -> None:
    """IFRS: ProfitLossAttributableToOwnersOfParent outranks total ProfitLoss
    (FMP's netIncome is parent-attributable)."""
    accn_to_doc = _register_accession(conn)
    payload: dict[str, object] = {
        "facts": {
            "ifrs-full": {
                "ProfitLoss": {"units": {"USD": [_q_entry(110)]}},
                "ProfitLossAttributableToOwnersOfParent": {"units": {"USD": [_q_entry(100)]}},
            }
        }
    }
    insert_facts_from_companyfacts(
        conn, ticker="X", payload=payload, accession_to_doc_id=accn_to_doc
    )
    rows = conn.execute("SELECT value FROM financial_facts WHERE line_item='net_income'").fetchall()
    assert [int(r["value"]) for r in rows] == [100]


# ---------------------------------------------------------------------------
# Multi-frame same-document collision (LITE net_income @ 2016-07-02)
# ---------------------------------------------------------------------------


def test_same_doc_pick_key_prefers_latest_start() -> None:
    """The tighter period (latest `start`) wins; a dated start beats an instant
    (None), and `frame`/value only break a genuine `start` tie."""
    own = {"start": "2015-08-02", "end": "2016-07-02", "val": 21_000_000}
    recast = {"start": "2015-06-28", "end": "2016-07-02", "val": 9_300_000}
    assert _same_doc_pick_key(own, Decimal(21_000_000)) > _same_doc_pick_key(
        recast, Decimal(9_300_000)
    )
    # instant (no start) sorts below any dated duration
    instant = {"end": "2016-07-02", "val": 5}
    assert _same_doc_pick_key(recast, Decimal(9_300_000)) > _same_doc_pick_key(instant, Decimal(5))
    # equal start -> a framed entry outranks an unframed one
    framed = {"start": "2015-08-02", "frame": "CY2016", "val": 1}
    unframed = {"start": "2015-08-02", "val": 1}
    assert _same_doc_pick_key(framed, Decimal(1)) > _same_doc_pick_key(unframed, Decimal(1))


def _register_10k(conn: sqlite3.Connection, accn: str) -> dict[str, int]:
    return upsert_accession_documents(
        conn,
        ticker="LITE",
        accessions={
            accn: _AccessionRecord(
                accession=accn, form="10-K", filed="2017-08-29", fy=2017, fp="FY"
            )
        },
        project_root=Path("/tmp"),
    )


def _lite_multiframe_payload(recast_first: bool) -> dict[str, object]:
    """One 10-K accession reporting net_income @ 2016-07-02 under two duration
    contexts that both resolve FY: Lumentum's own first fiscal year (start
    2015-08-02 -> 21.0M) and a longer recast span (start 2015-06-28 -> 9.3M).
    Both collapse onto the same (source_doc_id, period_end, FY, net_income)
    5-tuple, so the extractor must keep exactly one deterministic winner."""
    accn = "0001633978-17-000095"
    own: dict[str, object] = {
        "accn": accn,
        "form": "10-K",
        "filed": "2017-08-29",
        "fy": 2017,
        "fp": "FY",
        "start": "2015-08-02",
        "end": "2016-07-02",
        "val": 21_000_000,
    }
    recast: dict[str, object] = {
        "accn": accn,
        "form": "10-K",
        "filed": "2017-08-29",
        "fy": 2017,
        "fp": "FY",
        "start": "2015-06-28",
        "end": "2016-07-02",
        "val": 9_300_000,
    }
    entries = [recast, own] if recast_first else [own, recast]
    return _payload_one_tag("us-gaap", "NetIncomeLoss", entries)


@pytest.mark.parametrize("recast_first", [False, True])
def test_multiframe_same_accession_collapses_deterministically(
    conn: sqlite3.Connection, recast_first: bool
) -> None:
    """Two frames of one accession sharing a 5-tuple write exactly ONE row, and
    the winner (own fiscal year, 21.0M) is independent of payload order — no
    write-then-correct churn, no iteration-order dependence. Reproduces the
    LITE flapping (rows 915064/915065/915066, source_doc_ids 10073/10074/10078)
    that INSERT-OR-IGNORE-then-same-doc-correction produced before the dedup."""
    accn = "0001633978-17-000095"
    accn_to_doc = _register_10k(conn, accn)
    payload = _lite_multiframe_payload(recast_first=recast_first)
    inserted = insert_facts_from_companyfacts(
        conn, ticker="LITE", payload=payload, accession_to_doc_id=accn_to_doc
    )
    assert inserted == 1
    rows = conn.execute(
        "SELECT value, source_doc_id FROM financial_facts "
        "WHERE ticker='LITE' AND line_item='net_income' AND fiscal_period_type='FY'"
    ).fetchall()
    assert len(rows) == 1  # collapsed, not one row per frame
    assert int(rows[0]["value"]) == 21_000_000  # latest-start winner, both orders
    assert int(rows[0]["source_doc_id"]) == accn_to_doc[accn]
