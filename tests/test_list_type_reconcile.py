"""Unit tests for list_type_reconcile — the holdings → list_type reconciler.

The rule (user directive 2026-06-15): operating companies held > $threshold are
``portfolio``; portfolio names not held demote to ``evaluation``; ETFs/funds/cash
are never promoted; pinned names stay in portfolio regardless of holdings.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from integrations.portfolio_tracker_v1 import (
    AccountV1,
    PortfolioSnapshotV1,
    SecuritiesV1Result,
    SecurityV1,
    V1AccountCoverage,
    V1Fetch,
    V1Meta,
)
from list_type_reconcile import (
    PinsUnreadableError,
    TrackerHoldings,
    apply_reclassification,
    compute_reclassification,
    holdings_from_tracker_v1,
    load_pins,
)

USER = "bhanu"
AS_OF = date(2026, 6, 14)
GENERATED_AT = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)


def _es_db() -> sqlite3.Connection:
    """A minimal tracked_companies table (only the columns the reconciler reads)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tracked_companies (
            user_id TEXT, ticker TEXT, name TEXT, list_type TEXT,
            ir_url TEXT, archived_at TIMESTAMP,
            brief_dirty BOOLEAN DEFAULT 0
        )
        """
    )
    return conn


def _track(
    conn: sqlite3.Connection, ticker: str, list_type: str, *, archived: bool = False
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies(user_id, ticker, name, list_type, ir_url, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            USER,
            ticker,
            f"{ticker} Inc",
            list_type,
            f"https://ir.{ticker}.com",
            "2020-01-01" if archived else None,
        ),
    )
    conn.commit()


def _meta(
    *,
    as_of: date | None = AS_OF,
    is_partial: bool = False,
    is_stale: bool = False,
    lagging_account_ids: list[int] | None = None,
    included_account_ids: list[int] | None = None,
) -> V1Meta:
    return V1Meta(
        schema_version="1.0.0",
        generated_at=GENERATED_AT,
        as_of=as_of,
        currency="USD",
        account_coverage=V1AccountCoverage(
            included_account_ids=included_account_ids or [1],
            lagging_account_ids=lagging_account_ids or [],
        ),
        last_successful_sync_at=GENERATED_AT,
        is_partial=is_partial,
        is_stale=is_stale,
    )


def _account(
    account_id: int = 1,
    *,
    holdings_as_of: date | None = AS_OF,
    last_successful_sync_at: datetime | None = GENERATED_AT,
    value: Decimal | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> AccountV1:
    return AccountV1.model_validate(
        {
            "account_id": account_id,
            "canonical_account_id": None,
            "provider": "test",
            "institution": None,
            "name": f"Account {account_id}",
            "official_name": None,
            "type": "investment",
            "subtype": None,
            "mask": "0000",
            "active": True,
            "included_in_totals": True,
            "exclusion_reason": None,
            "tax_treatment": "taxable",
            "tax_treatment_evidence": None,
            "tax_treatment_confidence": "high",
            "value": None if value is None else str(value),
            "value_currency": "USD",
            "holdings_as_of": None if holdings_as_of is None else holdings_as_of.isoformat(),
            "last_successful_sync_at": (
                None if last_successful_sync_at is None else last_successful_sync_at.isoformat()
            ),
            "warnings": warnings or [],
        }
    )


def _security(security_id: int, ticker: str, sec_type: str, *, cash_eq: bool = False) -> SecurityV1:
    return SecurityV1(
        security_id=security_id,
        ticker=ticker,
        name=f"{ticker} Inc",
        cusip=None,
        isin=None,
        type=sec_type,
        currency="USD",
        is_cash_equivalent=cash_eq,
        asset_type="equity" if sec_type in {"cs", "equity", "ad", "adr"} else "other",
        sector=None,
        region=None,
        classification_source="test",
        classification_updated_at=None,
    )


def _fetches(
    holdings: tuple[tuple[int, str, str, float], ...] | list[tuple[int, str, str, float]],
    *,
    meta: V1Meta | None = None,
    unknown_security: bool = False,
    accounts: list[AccountV1] | None = None,
    position_market_values: dict[int, Decimal | None] | None = None,
    lot_market_values: dict[int, tuple[Decimal | None, ...]] | None = None,
    lot_quantities: dict[int, tuple[Decimal, ...]] | None = None,
) -> tuple[V1Fetch[PortfolioSnapshotV1], V1Fetch[SecuritiesV1Result]]:
    """Build the real typed API result models from compact test holdings."""
    meta = meta or _meta()
    position_market_values = position_market_values or {}
    lot_market_values = lot_market_values or {}
    lot_quantities = lot_quantities or {}
    positions = [
        {
            "security_id": security_id,
            "ticker": ticker,
            "name": f"{ticker} Inc",
            "quantity": (
                sum(lot_quantities.get(security_id, (Decimal("10"),)))
                if security_id in lot_quantities
                else Decimal("10")
            ),
            "market_value": (
                None
                if (position_value := position_market_values.get(security_id, Decimal(str(value))))
                is None
                else str(position_value)
            ),
            "cost_basis": None,
            "unrealized_pnl": None,
            "percent_of_portfolio": None,
            "accounts": [
                {
                    "account_id": lot_index + 1,
                    "account_name": f"Account {lot_index + 1}",
                    "quantity": str(lot_quantity),
                    "market_value": None if lot_value is None else str(lot_value),
                    "cost_basis": None,
                    "cost_basis_source": None,
                    "tax_treatment": "taxable",
                }
                for lot_index, (lot_value, lot_quantity) in enumerate(
                    zip(
                        lot_market_values.get(security_id, (position_value,)),
                        lot_quantities.get(
                            security_id,
                            (Decimal("10"),)
                            * len(lot_market_values.get(security_id, (position_value,))),
                        ),
                    )
                )
            ],
        }
        for security_id, ticker, _sec_type, value in holdings
    ]
    snapshot = PortfolioSnapshotV1.model_validate(
        {
            "meta": meta.model_dump(mode="json"),
            "accounts": [
                account.model_dump(mode="json")
                for account in (
                    accounts
                    if accounts is not None
                    else [
                        _account(account_id)
                        for account_id in meta.account_coverage.included_account_ids
                    ]
                )
            ],
            "total_market_value": str(sum(value for *_prefix, value in holdings)),
            "positions": positions,
            "equity_fraction": {
                "equity_value": "1",
                "denominator_value": "1",
                "equity_fraction": "1",
                "unit": "fraction",
                "cash_equivalent_policy": "exclude",
                "included_account_ids": meta.account_coverage.included_account_ids,
                "holdings_as_of": AS_OF.isoformat()
                if meta.as_of is None
                else meta.as_of.isoformat(),
                "is_partial": False,
                "is_stale": False,
                "methodology": "test",
                "methodology_version": "1",
            },
        }
    )
    securities = [
        _security(security_id, ticker, sec_type)
        for security_id, ticker, sec_type, _value in holdings
        if not unknown_security or security_id != 999
    ]
    securities_result = SecuritiesV1Result(meta=meta, securities=securities)
    return (
        V1Fetch[PortfolioSnapshotV1](available=True, endpoint="portfolio-snapshot", data=snapshot),
        V1Fetch[SecuritiesV1Result](available=True, endpoint="securities", data=securities_result),
    )


def _tracker_holdings(
    *holdings: tuple[int, str, str, float],
    meta: V1Meta | None = None,
    unknown_security: bool = False,
    accounts: list[AccountV1] | None = None,
    position_market_values: dict[int, Decimal | None] | None = None,
    lot_market_values: dict[int, tuple[Decimal | None, ...]] | None = None,
    lot_quantities: dict[int, tuple[Decimal, ...]] | None = None,
) -> TrackerHoldings:
    return holdings_from_tracker_v1(
        *_fetches(
            holdings,
            meta=meta,
            unknown_security=unknown_security,
            accounts=accounts,
            position_market_values=position_market_values,
            lot_market_values=lot_market_values,
            lot_quantities=lot_quantities,
        )
    )


# ---------------------------------------------------------------------------


def test_promotes_held_equity_demotes_unheld_portfolio() -> None:
    es = _es_db()
    holdings = _tracker_holdings(
        (1, "BKNG", "equity", 6237.0),
        (2, "UBER", "equity", 14935.0),
        (3, "MELI", "cs", 88258.0),
    )
    _track(es, "BKNG", "evaluation")
    _track(es, "UBER", "evaluation")
    _track(es, "MELI", "portfolio")
    _track(es, "AMZN", "portfolio")
    _track(es, "GOOG", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert [promotion[0] for promotion in plan.promotions] == ["BKNG", "UBER"]
    assert all(promotion[1] == "evaluation" for promotion in plan.promotions)
    assert [demotion[0] for demotion in plan.demotions] == ["AMZN", "GOOG"]
    assert plan.unchanged_portfolio == ["MELI"]
    assert plan.has_changes


def test_sold_security_absent_from_snapshot_demotes() -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "UBER", "equity", 14935.0))
    _track(es, "UBER", "portfolio")
    _track(es, "BKNG", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert [demotion[0] for demotion in plan.demotions] == ["BKNG"]
    assert plan.unavailable_reason is None


def test_multi_account_rollup_counts_toward_operating_value() -> None:
    es = _es_db()
    snapshot_fetch, securities_fetch = _fetches(
        [(1, "BKNG", "equity", 150.0), (2, "UBER", "equity", 150.0)]
    )
    _track(es, "BKNG", "evaluation")
    holdings = holdings_from_tracker_v1(snapshot_fetch, securities_fetch)

    assert holdings.operating == {"BKNG": 150.0, "UBER": 150.0}
    assert holdings.all_held == {"BKNG": 150.0, "UBER": 150.0}


def test_multi_account_lots_sum_held_value() -> None:
    snapshot_fetch, securities_fetch = _fetches(
        [(1, "MELI", "cs", 150.0)],
        meta=_meta(included_account_ids=[1, 2]),
        lot_market_values={1: (Decimal("100"), Decimal("50"))},
        lot_quantities={1: (Decimal("4"), Decimal("6"))},
    )
    assert snapshot_fetch.data is not None
    position = snapshot_fetch.data.positions[0]

    assert [(lot.account_id, lot.quantity) for lot in position.accounts] == [(1, 4), (2, 6)]
    holdings = holdings_from_tracker_v1(snapshot_fetch, securities_fetch)

    assert holdings.all_held == {"MELI": 150.0}
    assert holdings.operating == {"MELI": 150.0}


def test_short_lots_do_not_offset_held_long_lots() -> None:
    holdings = _tracker_holdings(
        *[(1, "MELI", "cs", 50.0)],
        meta=_meta(included_account_ids=[1, 2]),
        lot_market_values={1: (Decimal("200"), Decimal("-150"))},
        lot_quantities={1: (Decimal("2"), Decimal("-1"))},
    )

    assert holdings.all_held == {"MELI": 200.0}
    assert holdings.operating == {"MELI": 200.0}


def test_only_short_lots_are_not_held() -> None:
    es = _es_db()
    holdings = _tracker_holdings(
        (1, "MELI", "cs", -200.0),
        (2, "UBER", "equity", 14935.0),
        lot_market_values={1: (Decimal("-200"),)},
        lot_quantities={1: (Decimal("-2"),)},
    )
    _track(es, "MELI", "portfolio")
    _track(es, "UBER", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.all_held == {"UBER": 14935.0}
    assert [demotion[0] for demotion in plan.demotions] == ["MELI"]


def test_book_without_long_lots_is_unavailable_never_mass_demotes() -> None:
    es = _es_db()
    holdings = _tracker_holdings(
        (1, "MELI", "cs", -200.0),
        lot_market_values={1: (Decimal("-200"),)},
        lot_quantities={1: (Decimal("-2"),)},
    )
    _track(es, "MELI", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.unavailable_reason == "portfolio snapshot has zero held positions"
    assert plan.holdings_unavailable
    assert not plan.has_changes


def test_cash_equivalent_equity_type_is_never_promoted() -> None:
    es = _es_db()
    snapshot_fetch, _securities_fetch = _fetches([(1, "MELI", "cs", 88_000.0)])
    assert snapshot_fetch.data is not None
    securities_result = SecuritiesV1Result(
        meta=snapshot_fetch.data.meta,
        securities=[_security(1, "MELI", "cs", cash_eq=True)],
    )
    cash_security_fetch = V1Fetch[SecuritiesV1Result](
        available=True,
        endpoint="securities",
        data=securities_result,
    )
    holdings = holdings_from_tracker_v1(snapshot_fetch, cash_security_fetch)
    _track(es, "MELI", "evaluation")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.all_held == {"MELI": 88_000.0}
    assert holdings.operating == {}
    assert plan.promotions == []


def test_etfs_funds_cash_never_promoted() -> None:
    es = _es_db()
    holdings = _tracker_holdings(
        (1, "FLKR", "et", 49626.0),
        (2, "VTI", "et", 136754.0),
        (3, "SPY", "etf", 19034.0),
        (4, "FDRXX", "oef", 5000.0),
        (5, "CUR:USD", "cash", 5000.0),
    )
    _track(es, "FLKR", "evaluation")
    _track(es, "VTI", "watchlist")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert plan.promotions == []
    assert plan.untracked_held == []
    assert set(holdings.all_held) == {"FLKR", "VTI", "SPY", "FDRXX", "CUR:USD"}
    assert not plan.has_changes


def test_mixed_security_types_do_not_pollute_operating_company_value() -> None:
    holdings = _tracker_holdings(
        (1, "MIX", "cs", 200.0),
        (2, "MIX", "etf", 50_000.0),
        (3, "MIX", "cash", 25_000.0),
    )

    assert holdings.operating == {"MIX": 200.0}
    assert holdings.all_held == {"MIX": 75_200.0}


def test_mixed_etf_row_does_not_suppress_operating_company() -> None:
    holdings = _tracker_holdings(
        (1, "DUPE", "cs", 5_000.0),
        (2, "DUPE", "etf", 5_000.0),
    )

    assert holdings.operating == {"DUPE": 5_000.0}


def test_threshold_excludes_tiny_positions() -> None:
    es = _es_db()
    holdings = _tracker_holdings(
        (1, "GOOGL", "equity", 14.91),
        (2, "BKNG", "equity", 150.0),
    )
    _track(es, "GOOGL", "evaluation")
    _track(es, "BKNG", "evaluation")

    plan = compute_reclassification(es_conn=es, holdings=holdings, min_value=100.0, user_id=USER)
    assert [promotion[0] for promotion in plan.promotions] == ["BKNG"]

    plan2 = compute_reclassification(es_conn=es, holdings=holdings, min_value=10.0, user_id=USER)
    assert [promotion[0] for promotion in plan2.promotions] == ["BKNG", "GOOGL"]


def test_pinned_name_kept_in_portfolio() -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "MELI", "cs", 88000.0))
    _track(es, "MELI", "portfolio")
    _track(es, "META", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, pins={"META"}, user_id=USER)
    assert plan.demotions == []
    assert [pin[0] for pin in plan.pinned_kept] == ["META"]
    assert not plan.has_changes

    plan2 = compute_reclassification(es_conn=es, holdings=holdings, pins=set(), user_id=USER)
    assert [demotion[0] for demotion in plan2.demotions] == ["META"]


def test_untracked_held_equity_reported_not_promoted() -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "FLKRCO", "cs", 49626.0))

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert [holding[0] for holding in plan.untracked_held] == ["FLKRCO"]
    assert plan.promotions == []


def test_archived_rows_ignored() -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "BKNG", "equity", 6237.0))
    _track(es, "BKNG", "evaluation", archived=True)

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert plan.promotions == []
    assert [holding[0] for holding in plan.untracked_held] == ["BKNG"]


@pytest.mark.parametrize(
    "meta",
    [
        _meta(as_of=None),
        _meta(is_partial=True),
        _meta(is_stale=True),
        _meta(lagging_account_ids=[2]),
    ],
)
def test_incomplete_snapshot_is_unavailable(meta: V1Meta) -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "MELI", "cs", 88_000.0), meta=meta)
    _track(es, "MELI", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.unavailable_reason is not None
    assert plan.holdings_unavailable is True
    assert plan.unavailable_reason == holdings.unavailable_reason
    assert plan.demotions == []
    assert plan.promotions == []


def test_missing_position_market_value_is_unavailable() -> None:
    holdings = _tracker_holdings(
        (1, "MELI", "cs", 88_000.0),
        position_market_values={1: None},
        lot_market_values={1: (Decimal("50"),)},
    )

    assert holdings.unavailable_reason == "position MELI has no market value"


def test_unvalued_account_lot_is_unavailable() -> None:
    holdings = _tracker_holdings(
        (1, "MELI", "cs", 88_000.0),
        lot_market_values={1: (Decimal("44_000"), None)},
    )

    assert holdings.unavailable_reason == "position MELI has an unvalued account lot"


def test_missing_pins_file_returns_empty() -> None:
    assert load_pins(Path("/nonexistent-checkout")) == {}


@pytest.mark.parametrize(
    "payload",
    [
        '{"pinned_portfolio": ["META"]}',
        '{"pinned_portfolio": [{"reason": "kept"}]}',
        '{"pinned_portfolio": [{"ticker": "   "}]}',
    ],
)
def test_malformed_pin_entries_raise(payload: str, tmp_path: Path) -> None:
    pins_path = tmp_path / "data" / "portfolio_pins.json"
    pins_path.parent.mkdir()
    pins_path.write_text(payload, encoding="utf-8")

    with pytest.raises(PinsUnreadableError):
        load_pins(tmp_path)


def test_unreadable_pins_path_raises(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_pins.json").mkdir()
    with pytest.raises(PinsUnreadableError):
        load_pins(tmp_path)


@pytest.mark.parametrize(
    ("account", "expected_reason"),
    [
        (_account(holdings_as_of=None), None),
        (
            _account(holdings_as_of=None, last_successful_sync_at=None),
            "account 1 has unknown holdings coverage",
        ),
        (
            _account(
                holdings_as_of=None,
                warnings=[{"code": "PROVIDER", "message": "account warning"}],
            ),
            "account 1 has unknown holdings coverage",
        ),
        (
            _account(holdings_as_of=None, value=Decimal("12")),
            "account 1 has unknown holdings coverage",
        ),
    ],
)
def test_null_dated_account_coverage(account: AccountV1, expected_reason: str | None) -> None:
    holdings = _tracker_holdings((1, "MELI", "cs", 88_000.0), accounts=[account])

    assert holdings.unavailable_reason == expected_reason


def test_included_account_missing_from_snapshot_accounts_is_unavailable() -> None:
    holdings = _tracker_holdings(
        (1, "MELI", "cs", 88_000.0),
        meta=_meta(included_account_ids=[1, 2]),
        accounts=[_account(1)],
    )

    assert holdings.unavailable_reason == "included account 2 missing from snapshot accounts"


def test_account_holdings_date_mismatch_is_unavailable() -> None:
    holdings = _tracker_holdings(
        (1, "MELI", "cs", 88_000.0),
        accounts=[_account(holdings_as_of=AS_OF.replace(day=13))],
    )

    assert holdings.unavailable_reason == "account 1 holdings date differs from snapshot"


def test_previous_calendar_day_snapshot_is_usable() -> None:
    previous_day = AS_OF.replace(day=13)
    holdings = _tracker_holdings(
        (1, "MELI", "cs", 88_000.0),
        meta=_meta(as_of=previous_day),
        accounts=[_account(holdings_as_of=previous_day)],
    )

    assert holdings.unavailable_reason is None
    assert holdings.all_held == {"MELI": 88_000.0}


def test_unavailable_fetch_is_unavailable() -> None:
    es = _es_db()
    snapshot_fetch = V1Fetch[PortfolioSnapshotV1](
        available=False, endpoint="portfolio-snapshot", error="api down"
    )
    securities_fetch = V1Fetch[SecuritiesV1Result](available=True, endpoint="securities")
    holdings = holdings_from_tracker_v1(snapshot_fetch, securities_fetch)
    _track(es, "MELI", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.unavailable_reason == "api down"
    assert plan.holdings_unavailable is True
    assert plan.unavailable_reason == "api down"
    assert plan.demotions == []


def test_unknown_security_id_is_unavailable() -> None:
    holdings = _tracker_holdings((999, "MELI", "cs", 88_000.0), unknown_security=True)

    assert holdings.unavailable_reason == "security 999 missing from securities catalog"


def test_zero_positions_is_safe_unavailable() -> None:
    es = _es_db()
    holdings = _tracker_holdings()
    _track(es, "MELI", "portfolio")
    _track(es, "NU", "portfolio")
    _track(es, "BKNG", "evaluation")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)

    assert holdings.unavailable_reason == "portfolio snapshot has zero held positions"
    assert plan.holdings_unavailable is True
    assert plan.demotions == []
    assert plan.promotions == []
    assert not plan.has_changes


def test_apply_writes_only_list_type_and_is_idempotent() -> None:
    es = _es_db()
    holdings = _tracker_holdings((1, "BKNG", "equity", 6237.0))
    _track(es, "BKNG", "evaluation")
    _track(es, "AMZN", "portfolio")

    plan = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)
    tally = apply_reclassification(es_conn=es, plan=plan, user_id=USER)
    assert tally == {"promoted": 1, "demoted": 1}

    rows = {
        row["ticker"]: (row["list_type"], row["ir_url"])
        for row in es.execute("SELECT ticker, list_type, ir_url FROM tracked_companies")
    }
    assert rows["BKNG"][0] == "portfolio"
    assert rows["AMZN"][0] == "evaluation"
    assert rows["BKNG"][1] == "https://ir.BKNG.com"

    plan2 = compute_reclassification(es_conn=es, holdings=holdings, user_id=USER)
    assert not plan2.has_changes


def test_load_pins_roundtrip(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "portfolio_pins.json").write_text(
        json.dumps(
            {
                "pinned_portfolio": [
                    {"ticker": "META", "reason": "conviction, no live position"},
                    {"ticker": "abc"},
                ]
            }
        ),
        encoding="utf-8",
    )
    pins = load_pins(tmp_path)
    assert pins == {"META": "conviction, no live position", "ABC": ""}


def test_load_pins_absent(tmp_path: Path) -> None:
    assert load_pins(tmp_path) == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json{",
        "[]",
        "{}",
        '{"pinned_portfolio": {}}',
        "[1]",
        '[{"reason": "x"}]',
        '[{"ticker": ""}]',
    ],
)
def test_load_pins_malformed_raises(tmp_path: Path, payload: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = data / "portfolio_pins.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(PinsUnreadableError, match=str(path)):
        load_pins(tmp_path)


def test_load_pins_unreadable_raises(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    path = data / "portfolio_pins.json"
    path.mkdir()

    with pytest.raises(PinsUnreadableError, match="is unreadable"):
        load_pins(tmp_path)


def test_load_pins_permission_error_raises_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pins store the process cannot read fails closed — a permission error
    is never degraded to an empty (unpinned) store."""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "portfolio_pins.json"
    path.write_text(
        '{"pinned_portfolio": [{"ticker": "META", "reason": "held"}]}', encoding="utf-8"
    )
    original_read_text = Path.read_text

    def deny_pins(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("permission denied for test")
        return original_read_text(self)

    monkeypatch.setattr(Path, "read_text", deny_pins)

    with pytest.raises(PinsUnreadableError, match=f"pins file {path} is unreadable"):
        load_pins(tmp_path)
