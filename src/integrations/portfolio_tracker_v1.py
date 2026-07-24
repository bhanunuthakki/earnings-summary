"""Typed client for the portfolio-tracker ``/api/v1`` Portfolio Data Service.

This is the **PRD §8.1 Phase 1** client for the portfolio-intelligence
consolidation program: a Pydantic-modeled, never-raise wrapper around the
sibling ``portfolio-tracker`` repo's versioned, envelope-carrying
``/api/v1/*`` read contract. It exists ALONGSIDE (does not replace)
``integrations.portfolio_tracker_client`` — the legacy client's ad-hoc-JSON
paths (``fetch_live_portfolio``, ``fetch_portfolio_analytics``, etc.) remain
supported until Phase 2 migrates their call sites over one at a time. Do not
delete or edit that module as part of this work.

Contract artifacts (read-only, in the sibling ``portfolio-tracker`` checkout;
never edit them from here):

- ``docs/api/v1-overview.md`` — envelope semantics, data-state (no-data /
  stale / partial), numeric conventions (decimal strings, percent-vs-fraction
  units), pagination, structured errors, and the compatibility policy this
  client's fail-closed major-version gate implements.
- ``docs/api/positions-v1.md`` — the consolidated-positions endpoint detail.
- ``docs/api/openapi.v1.json`` — authoritative schemas; every model below
  mirrors one by name.
- ``docs/api/fixtures/v1/*.json`` — official synthetic fixtures, vendored
  verbatim into this repo at ``tests/fixtures/tracker_v1/`` (see that
  directory's README for provenance + the regeneration command). Every v1
  endpoint has an official fixture as of 2026-07-24 (provider PR #52);
  ``tests/fixtures/tracker_v1/synthetic/`` is kept empty as a fallback
  location for any future endpoint that ships without one.

Design contract, matched to the sibling doc's rules:

- **Decimal, never float.** Every money/quantity JSON string is parsed
  through :func:`_decimal_from_json` into :class:`decimal.Decimal` — a raw
  JSON float arriving in one of these fields is treated as schema drift and
  rejected, never silently coerced.
- **``extra="allow"`` everywhere.** The compatibility policy requires
  consumers tolerate unknown additive fields on a MINOR bump; every model
  here inherits that from :class:`V1Model`.
- **Never raise.** Every fetch method returns a :class:`V1Fetch` — connection
  errors, timeouts, non-200s, JSON parse failures, and Pydantic validation
  failures all come back as ``available=False`` with a reason string, never
  an exception. See :meth:`TrackerV1Client._get_impl` for the single place
  this is enforced, plus an outer safety net in
  :meth:`TrackerV1Client._get`.
- **Fail-closed major-version gate.** Every response carrying the FULL
  :class:`V1Meta` envelope has its ``meta.schema_version`` checked before the
  payload is trusted; a MAJOR mismatch is rejected. Two endpoints are exempt,
  by contract, not by accident: ``PositionsV1Result`` carries no version info
  anywhere (its OpenAPI schema has no ``meta`` at all — see its docstring),
  and ``HealthV1`` — though it does carry a flat top-level
  ``schema_version`` — is the discovery/probe endpoint itself and is
  deliberately not gated (confirmed with the provider session: "envelope"
  here means the full V1Meta shape, which Health lacks). Both still log
  their ``schema_version`` in telemetry for observability; the gate is just
  not enforced against it for those two.
- **No secrets, no payload contents, in logs.** One structured line per
  request goes to the ``tracker_v1`` logger: endpoint path, duration ms, HTTP
  status, schema_version. Never a holdings value, account name, or balance.
  Validation-error summaries use only Pydantic's ``loc``/``type`` — never
  ``input_value`` (which can be the balance itself).

Discovery: base URL from the ``PORTFOLIO_TRACKER_API_URL`` env var (the same
variable name honored by the legacy client), defaulting to
``http://127.0.0.1:8000`` — 127.0.0.1 rather than "localhost" for the same
double-DNS-family latency reason documented in
``integrations.portfolio_tracker_client``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Generic, Literal, TypeVar, cast

import requests
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

logger = logging.getLogger("tracker_v1")

# ---------------------------------------------------------------------------
# Discovery / timeouts
# ---------------------------------------------------------------------------

# Same env var name the legacy portfolio_tracker_client.py honors — one
# override controls both clients.
_ENV_VAR = "PORTFOLIO_TRACKER_API_URL"
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# (connect, read) timeout tuple halves. Connect stays tight regardless of
# endpoint family (a dead loopback tracker should fail fast); read differs
# because analytics/* recomputes TWR/beta/drawdown over a window on request.
_CONNECT_TIMEOUT_SECONDS = 0.5
_READ_TIMEOUT_SECONDS = 4.0
_ANALYTICS_READ_TIMEOUT_SECONDS = 6.0

# Hard cap on cursor-pagination pages, bounding a runaway loop against a
# server that never returns a null next_cursor.
_PAGE_CAP = 50

_SUPPORTED_SCHEMA_MAJOR = 1
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _resolve_base_url(explicit: str | None) -> str:
    """explicit arg -> ``PORTFOLIO_TRACKER_API_URL`` env -> loopback default."""
    base = explicit or os.environ.get(_ENV_VAR) or _DEFAULT_BASE_URL
    return base.rstrip("/")


# ---------------------------------------------------------------------------
# Decimal-string parsing (money / quantity / percent / fraction fields)
# ---------------------------------------------------------------------------


def _decimal_from_json(value: object) -> Decimal:
    """Parse a v1 decimal-string field into :class:`Decimal` — never float.

    The contract (``v1-overview.md`` "Numeric and date conventions") sends
    money/quantity as JSON strings specifically so consumers don't
    float-accumulate. A JSON float arriving here means either schema drift or
    a caller constructing a payload by hand — reject it rather than silently
    losing precision. Scientific-notation zero (``"0E-10"``, seen in the
    official ``transactions.json`` fixture for cash-type rows) parses fine
    through :class:`Decimal` directly; note the OpenAPI ``pattern`` regex on
    these fields technically does NOT match that value (flagged in the PR
    report as a contract quirk) — this parser intentionally does not
    re-enforce that regex, it just defers to ``Decimal``'s own grammar.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"expected a decimal string, got bool {value!r}")
    if isinstance(value, float):
        raise ValueError(
            f"expected a decimal string, got float {value!r} — money/quantity/percent "
            "fields must arrive as JSON strings, never float-accumulated"
        )
    if isinstance(value, str | int):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal string {value!r}: {exc}") from exc
    raise ValueError(f"expected a decimal string, got {type(value).__name__}")


def _optional_decimal_from_json(value: object) -> Decimal | None:
    if value is None:
        return None
    return _decimal_from_json(value)


# ``Money`` covers dollar amounts and quantities; it is also reused for
# percent-unit fields (0-100, e.g. ``percent_of_portfolio``/``weight_pct``)
# and fraction-unit fields (0-1, ``equity_fraction``) — the contract states
# the unit per field in ``v1-overview.md``, and each field below documents
# its own unit in its docstring/comment since the wire representation
# (decimal string) is identical across all three.
Money = Annotated[Decimal, BeforeValidator(_decimal_from_json)]
MoneyOrNone = Annotated[Decimal | None, BeforeValidator(_optional_decimal_from_json)]

# The ratified detailed five-way tax-treatment enum (SC-1, positions-v1.md).
TaxTreatment = Literal["taxable", "pretax", "roth", "hsa", "unknown"]


# ---------------------------------------------------------------------------
# Shared envelope models
# ---------------------------------------------------------------------------


class V1Model(BaseModel):
    """Shared base for every v1 response model.

    ``extra="allow"``: the compatibility policy (``v1-overview.md``) requires
    consumers tolerate unknown ADDITIVE fields introduced on a MINOR bump —
    rejecting them would break the forward-compatibility the contract
    promises.
    """

    model_config = ConfigDict(extra="allow")


class V1Warning(V1Model):
    """A structured, machine-readable warning attached to a v1 response."""

    code: str
    message: str
    scope: str | None = None


class V1AccountCoverage(V1Model):
    """Which accounts contributed to this response.

    ``lagging_account_ids`` are included accounts whose own latest holdings
    snapshot is older than the response ``as_of`` — the v1 heuristic for a
    one-provider refresh failure.
    """

    included_account_ids: list[int] = Field(default_factory=list[int])
    excluded_account_ids: list[int] = Field(default_factory=list[int])
    lagging_account_ids: list[int] = Field(default_factory=list[int])


class V1Meta(V1Model):
    """The shared v1 response envelope (``v1-overview.md``).

    ``schema_version`` is semver; a MAJOR change means reject (see
    :func:`_check_major_version`). ``as_of`` is the holdings OBSERVATION
    date (``None`` means no data, paired with a ``NO_DATA`` warning) — for
    ``analytics/*`` responses specifically it is NOT the query window end.
    """

    schema_version: str
    generated_at: datetime
    as_of: date | None
    currency: str
    source_providers: list[str] = Field(default_factory=list[str])
    account_coverage: V1AccountCoverage
    last_successful_sync_at: datetime | None
    is_partial: bool
    is_stale: bool
    warnings: list[V1Warning] = Field(default_factory=list[V1Warning])
    methodology: str | None = None
    methodology_version: str | None = None
    links: dict[str, str] = Field(default_factory=dict[str, str])


# ---------------------------------------------------------------------------
# Accounts / positions / portfolio-snapshot
# ---------------------------------------------------------------------------


class AccountV1(V1Model):
    """One normalized account with provenance (SC-1 / SC-2, ``v1-overview.md``)."""

    account_id: int
    canonical_account_id: int | None
    provider: str
    institution: str | None
    name: str
    official_name: str | None
    type: str
    subtype: str | None
    mask: str | None
    active: bool
    included_in_totals: bool
    exclusion_reason: str | None
    tax_treatment: TaxTreatment
    tax_treatment_evidence: str | None
    # Free-form per the OpenAPI schema (plain "string", not an enum) — the
    # doc names high/medium/low but only tax_treatment itself is the ratified
    # fixed enum, so this stays a str rather than a Literal that could
    # hard-fail parsing on a future confidence tier.
    tax_treatment_confidence: str
    value: MoneyOrNone
    value_currency: str
    holdings_as_of: date | None
    last_successful_sync_at: datetime | None
    warnings: list[V1Warning] = Field(default_factory=list[V1Warning])


class AccountsV1Result(V1Model):
    """``GET /api/v1/accounts``."""

    meta: V1Meta
    accounts: list[AccountV1] = Field(default_factory=list[AccountV1])


class PositionLotV1(V1Model):
    """One account's slice of a consolidated position (``positions-v1.md``).

    ``cost_basis_source`` is free-form per OpenAPI (``null`` | ``"manual"`` |
    ``"inferred_acats"`` | ``"inferred_1099"`` per the doc, but typed as
    plain ``str | None`` here rather than Literal — same reasoning as
    ``tax_treatment_confidence`` above).
    """

    account_id: int
    account_name: str
    quantity: Money
    market_value: MoneyOrNone
    cost_basis: MoneyOrNone
    cost_basis_source: str | None
    tax_treatment: TaxTreatment


class PositionV1(V1Model):
    """A position rolled up across every account that holds the security.

    ``percent_of_portfolio`` is PERCENT (0-100): ``market_value /
    total_market_value * 100``; ``None`` when the book total is zero or this
    position has no market value.
    """

    security_id: int
    ticker: str | None
    name: str | None
    quantity: Money
    market_value: MoneyOrNone
    cost_basis: MoneyOrNone
    unrealized_pnl: MoneyOrNone
    percent_of_portfolio: MoneyOrNone
    accounts: list[PositionLotV1] = Field(default_factory=list[PositionLotV1])


class EquityFractionV1(V1Model):
    """SC-3 equity fraction fact (same methodology in ``portfolio-snapshot``
    and ``analytics/positioning``).

    ``equity_fraction`` is a FRACTION in [0, 1] (``unit`` says so
    explicitly) — ``None`` when the denominator is zero, never silently 0.
    """

    equity_value: Money
    denominator_value: Money
    equity_fraction: MoneyOrNone
    unit: str
    cash_equivalent_policy: str
    included_account_ids: list[int] = Field(default_factory=list[int])
    excluded_account_ids: list[int] = Field(default_factory=list[int])
    holdings_as_of: date | None
    is_partial: bool
    is_stale: bool
    warnings: list[V1Warning] = Field(default_factory=list[V1Warning])
    methodology: str
    methodology_version: str


class PortfolioSnapshotV1(V1Model):
    """``GET /api/v1/portfolio-snapshot`` — the bulk consumer read model."""

    meta: V1Meta
    accounts: list[AccountV1] = Field(default_factory=list[AccountV1])
    total_market_value: Money
    by_tax_treatment: dict[str, Money] = Field(default_factory=dict[str, Money])
    positions: list[PositionV1] = Field(default_factory=list[PositionV1])
    equity_fraction: EquityFractionV1


class PositionsV1Result(V1Model):
    """``GET /api/v1/portfolio/positions`` — consolidated positions.

    **Contract quirk**: unlike every other v1 response, this OpenAPI schema
    carries NO ``meta`` envelope at all — no ``schema_version``, no
    ``as_of``, nothing (confirmed against both ``openapi.v1.json`` and the
    ``positions-v1.md`` example payload). The fail-closed major-version gate
    is therefore a structural no-op for this one endpoint: there is nothing
    to check against, not a bypass of the check. ``TrackerV1Client`` still
    returns the standard never-raise :class:`V1Fetch` wrapper; its ``meta``
    field is simply always ``None`` here.
    """

    snapshot_date: date | None
    total_market_value: Money
    positions: list[PositionV1] = Field(default_factory=list[PositionV1])
    by_tax_treatment: dict[str, Money] = Field(default_factory=dict[str, Money])
    notes: list[str] = Field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Transactions / cash flows / position snapshots (cursor-paginated)
# ---------------------------------------------------------------------------


class CashFlowV1(V1Model):
    transaction_id: str
    account_id: int
    account_name: str
    date: date
    name: str | None
    type: str
    subtype: str | None
    amount: Money
    signed_external_amount: Money
    classification: str
    classification_source: str
    currency: str


class CashFlowsV1Result(V1Model):
    """``GET /api/v1/cash-flows`` — keyset-cursor paginated."""

    meta: V1Meta
    start_date: date
    end_date: date
    include_internal: bool
    cash_flows: list[CashFlowV1] = Field(default_factory=list[CashFlowV1])
    net_external_cashflow_in: Money
    next_cursor: str | None


class TransactionV1(V1Model):
    transaction_id: str
    account_id: int
    account_name: str
    security_id: int | None
    ticker: str | None
    date: date
    name: str | None
    quantity: Money
    amount: Money
    price: MoneyOrNone
    fees: MoneyOrNone
    type: str
    subtype: str | None
    currency: str
    override_classification: str | None
    effective_classification: str | None


class TransactionsV1Result(V1Model):
    """``GET /api/v1/transactions`` — keyset-cursor paginated."""

    meta: V1Meta
    start_date: date
    end_date: date
    transactions: list[TransactionV1] = Field(default_factory=list[TransactionV1])
    next_cursor: str | None


class PositionSnapshotV1(V1Model):
    snapshot_date: date
    account_id: int
    account_name: str
    security_id: int
    ticker: str | None
    quantity: Money
    institution_price: MoneyOrNone
    institution_value: MoneyOrNone
    currency: str
    # "broker" | "manual" per v1-overview.md, kept as str (see
    # tax_treatment_confidence's note on non-ratified free-form enums).
    origin: str


class PositionSnapshotsV1Result(V1Model):
    """``GET /api/v1/position-snapshots`` — keyset-cursor paginated."""

    meta: V1Meta
    start_date: date
    end_date: date
    snapshots: list[PositionSnapshotV1] = Field(default_factory=list[PositionSnapshotV1])
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Securities / data-quality
# ---------------------------------------------------------------------------


class SecurityV1(V1Model):
    security_id: int
    ticker: str | None
    name: str | None
    cusip: str | None
    isin: str | None
    type: str | None
    currency: str
    is_cash_equivalent: bool
    asset_type: str
    sector: str | None
    region: str | None
    classification_source: str | None
    classification_updated_at: datetime | None


class SecuritiesV1Result(V1Model):
    meta: V1Meta
    securities: list[SecurityV1] = Field(default_factory=list[SecurityV1])


class DataQualityFindingOut(V1Model):
    """One data-quality issue. ``severity``: ``info`` | ``warning`` | ``error``."""

    category: str
    severity: str
    title: str
    detail: str
    recommended_action: str | None = None
    context: dict[str, str] = Field(default_factory=dict[str, str])


class DataQualityReportOut(V1Model):
    generated_at: datetime
    findings: list[DataQualityFindingOut] = Field(default_factory=list[DataQualityFindingOut])
    summary_counts: dict[str, int] = Field(default_factory=dict[str, int])


class DataQualityV1Result(V1Model):
    meta: V1Meta
    report: DataQualityReportOut


# ---------------------------------------------------------------------------
# Analytics: positioning
# ---------------------------------------------------------------------------


class PositioningBucketOut(V1Model):
    """One slice of a breakdown (asset type / sector / region / account
    type). ``weight_pct`` is PERCENT (0-100)."""

    label: str
    value: Money
    weight_pct: Money
    count: int


class ConcentrationOut(V1Model):
    """``hhi`` is on the 0-10,000 scale; ``effective_holdings`` reads as
    "behaves like ~N equal positions"."""

    num_positions: int
    top1_weight_pct: MoneyOrNone
    top5_weight_pct: MoneyOrNone
    top10_weight_pct: MoneyOrNone
    hhi: float | None
    effective_holdings: float | None


class PositionCorrelationRow(V1Model):
    """Per-ticker correlation + beta to each benchmark over the window. Each
    ``correlation_*``/``beta_*`` is ``None`` when the name lacks enough
    overlapping price history (see ``sample_size``)."""

    security_id: int
    ticker: str | None
    name: str | None
    value: Money
    weight_pct: Money
    sample_size: int
    correlation_spy: float | None
    beta_spy: float | None
    correlation_qqq: float | None
    beta_qqq: float | None
    correlation_policy: float | None
    beta_policy: float | None


class PositioningOut(V1Model):
    """Everything the Holdings positioning section renders in one payload."""

    snapshot_date: date
    start_date: date
    end_date: date
    total_value: Money
    by_asset_type: list[PositioningBucketOut] = Field(default_factory=list[PositioningBucketOut])
    by_sector: list[PositioningBucketOut] = Field(default_factory=list[PositioningBucketOut])
    by_region: list[PositioningBucketOut] = Field(default_factory=list[PositioningBucketOut])
    by_account_type: list[PositioningBucketOut] = Field(default_factory=list[PositioningBucketOut])
    concentration: ConcentrationOut
    correlations: list[PositionCorrelationRow] = Field(default_factory=list[PositionCorrelationRow])
    weighted_avg_correlation_spy: float | None
    has_policy: bool
    notes: list[str] = Field(default_factory=list[str])


class PositioningV1Result(V1Model):
    """``GET /api/v1/analytics/positioning``."""

    meta: V1Meta
    positioning: PositioningOut
    equity_fraction: EquityFractionV1


# ---------------------------------------------------------------------------
# Analytics: performance (Modified-Dietz TWR)
# ---------------------------------------------------------------------------


class PerformancePoint(V1Model):
    date: date
    portfolio_value: Money
    portfolio_return_pct: Money
    spy_return_pct: MoneyOrNone
    qqq_return_pct: MoneyOrNone
    policy_return_pct: MoneyOrNone
    spy_equivalent_value: MoneyOrNone
    qqq_equivalent_value: MoneyOrNone
    policy_equivalent_value: MoneyOrNone


class PerformanceSeries(V1Model):
    start_date: date
    end_date: date
    base_value: Money
    net_external_cashflow_in: Money = Decimal("0")
    backfill_start_unreliable: bool = False
    earliest_observed_date: date | None = None
    points: list[PerformancePoint] = Field(default_factory=list[PerformancePoint])


class PerformanceV1Result(V1Model):
    """``GET /api/v1/analytics/performance`` — Modified-Dietz TWR vs
    cashflow-matched SPY/QQQ/policy counterfactuals."""

    meta: V1Meta
    series: PerformanceSeries


# ---------------------------------------------------------------------------
# Analytics: position performance (per-ticker dollar alpha)
# ---------------------------------------------------------------------------


class PositionAlphaTimePoint(V1Model):
    date: date
    portfolio_value: Money
    spy_counterfactual_value: Money
    qqq_counterfactual_value: Money
    policy_counterfactual_value: Money
    position_cashflow: Money = Decimal("0")


class PositionAlphaRow(V1Model):
    ticker: str
    name: str | None
    value_at_start: Money
    bought_in_window: Money
    sold_in_window: Money
    value_at_end: Money
    actual_pl: Money
    spy_counterfactual_pl: Money
    qqq_counterfactual_pl: Money
    policy_counterfactual_pl: Money
    alpha: Money
    alpha_vs_qqq: Money
    alpha_vs_policy: Money
    incomplete: bool


class PositionAlphaResult(V1Model):
    start_date: date
    end_date: date
    has_policy: bool = False
    rows: list[PositionAlphaRow] = Field(default_factory=list[PositionAlphaRow])
    series: list[PositionAlphaTimePoint] = Field(default_factory=list[PositionAlphaTimePoint])
    total_actual_pl: Money
    total_spy_pl: Money
    total_qqq_pl: Money
    total_policy_pl: Money
    total_alpha: Money
    total_alpha_vs_qqq: Money
    total_alpha_vs_policy: Money
    v_start: Money = Decimal("0")
    v_end: Money = Decimal("0")


class PositionPerformanceV1Result(V1Model):
    """``GET /api/v1/analytics/position-performance``."""

    meta: V1Meta
    result: PositionAlphaResult


# ---------------------------------------------------------------------------
# Analytics: risk (beta/alpha/drawdown)
# ---------------------------------------------------------------------------


class BetaResult(V1Model):
    """Regression + risk-adjusted stats vs one benchmark. Per OpenAPI these
    are plain JSON numbers (not decimal strings) — kept as ``float``."""

    benchmark: str
    start_date: date
    end_date: date
    sample_size: int
    risk_free_annual: float
    beta: float | None
    alpha_annualized_pct: float | None
    r_squared: float | None
    correlation: float | None
    alpha_t_stat: float | None
    alpha_std_error_annualized_pct: float | None
    alpha_significant: bool | None
    sharpe: float | None
    sortino: float | None
    information_ratio: float | None
    portfolio_volatility_annualized: float | None
    benchmark_volatility_annualized: float | None
    tracking_error_annualized: float | None
    notes: list[str] = Field(default_factory=list[str])


class UnderwaterPoint(V1Model):
    date: date
    drawdown_pct: Money


class DrawdownResult(V1Model):
    """Peak-to-trough pain over the TWR series. ``calmar`` is
    ``annualized_return_pct / |max_drawdown_pct|``."""

    start_date: date
    end_date: date
    max_drawdown_pct: MoneyOrNone
    peak_date: date | None
    trough_date: date | None
    recovery_date: date | None
    days_to_recovery: int | None
    current_drawdown_pct: MoneyOrNone
    annualized_return_pct: MoneyOrNone
    calmar: MoneyOrNone
    underwater: list[UnderwaterPoint] = Field(default_factory=list[UnderwaterPoint])


class RiskV1Result(V1Model):
    """``GET /api/v1/analytics/risk`` — beta/alpha/drawdown in one read."""

    meta: V1Meta
    beta: BetaResult
    drawdown: DrawdownResult


# ---------------------------------------------------------------------------
# Analytics: exit quality
# ---------------------------------------------------------------------------


class ExitQualityRow(V1Model):
    ticker: str
    name: str | None
    sold_shares: Money
    sold_proceeds: Money
    avg_sell_price: MoneyOrNone
    price_now: MoneyOrNone
    value_if_held: Money
    regret_vs_hold: Money
    spy_value_if_reinvested: Money
    exit_alpha_vs_spy: Money
    still_held: bool
    incomplete: bool


class ExitQualityResult(V1Model):
    start_date: date
    end_date: date
    rows: list[ExitQualityRow] = Field(default_factory=list[ExitQualityRow])
    total_sold_proceeds: Money
    total_value_if_held: Money
    total_regret_vs_hold: Money
    total_exit_alpha_vs_spy: Money


class ExitQualityV1Result(V1Model):
    """``GET /api/v1/analytics/exit-quality``."""

    meta: V1Meta
    result: ExitQualityResult


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class ProviderHealthV1(V1Model):
    """Per-provider link health — counts and times only, never balances."""

    name: str
    configured: bool
    items_linked: int
    items_active: int
    last_successful_sync_at: datetime | None


class HealthV1(V1Model):
    """``GET /api/v1/health`` — service, database, migration, provider, and
    contract health. Contains no holdings, balances, or account details.

    Flat shape (NOT the shared :class:`V1Meta` envelope): ``schema_version``
    lives at the top level here, not nested under a ``meta`` key. Per the
    provider-session contract clarification, this endpoint is EXEMPT from the
    client's major-version fail-closed gate — "envelope-carrying" for that
    gate means the full V1Meta shape (account_coverage, warnings, staleness,
    etc.), which Health doesn't have. It's also the discovery/probe endpoint
    itself, so gating it on version would be circular. Its ``schema_version``
    is still parsed and still shows up in telemetry — just not enforced.
    """

    status: str
    schema_version: str
    generated_at: datetime
    database_ok: bool
    migration_version: str | None
    providers: list[ProviderHealthV1] = Field(default_factory=list[ProviderHealthV1])
    active_account_count: int
    latest_snapshot_date: date | None
    is_stale: bool
    links: dict[str, str] = Field(default_factory=dict[str, str])


# ---------------------------------------------------------------------------
# Never-raise fetch wrapper
# ---------------------------------------------------------------------------

ResultT = TypeVar("ResultT")


@dataclass
class V1Fetch(Generic[ResultT]):
    """Never-raise result wrapper for every v1 fetch.

    ``available=False`` covers connection errors, timeouts, non-200s, JSON
    parse failures, schema validation failures, and a rejected MAJOR
    version — the reason lives in ``error``; nothing here ever raises to the
    caller. ``meta`` is the parsed envelope (when the response carries one)
    on a SUCCESSFUL fetch — it stays ``None`` on failure and for the one
    endpoint with no envelope at all (``PositionsV1Result``).
    """

    available: bool
    endpoint: str
    data: ResultT | None = None
    error: str | None = None
    meta: V1Meta | None = None


def _extract_meta(obj: BaseModel) -> V1Meta | None:
    """Pull ``.meta`` off a parsed response model, if it has one (most do;
    ``PositionsV1Result`` and ``HealthV1`` don't — see their docstrings)."""
    meta = getattr(obj, "meta", None)
    if isinstance(meta, V1Meta):
        return meta
    return None


def _extract_schema_version(payload: Mapping[str, object]) -> str | None:
    """Pull ``schema_version`` from either the flat (``HealthV1``) or nested
    (``meta.schema_version``) envelope shape. ``None`` for the one v1
    response with no version info anywhere (``PositionsV1Result``)."""
    direct = payload.get("schema_version")
    if isinstance(direct, str):
        return direct
    meta = payload.get("meta")
    if isinstance(meta, dict):
        nested = cast("dict[str, object]", meta).get("schema_version")
        if isinstance(nested, str):
            return nested
    return None


def _check_major_version(schema_version: str) -> str | None:
    """``None`` when compatible; else a compatibility error naming both
    versions (fail-closed per the ``v1-overview.md`` compatibility policy:
    "consumers must reject a MAJOR they don't support")."""
    match = _SEMVER_RE.match(schema_version)
    if not match:
        return f"unparseable_schema_version: server sent {schema_version!r}"
    major = int(match.group(1))
    if major != _SUPPORTED_SCHEMA_MAJOR:
        return (
            f"incompatible_schema_version: server={schema_version} "
            f"client_supports_major={_SUPPORTED_SCHEMA_MAJOR}"
        )
    return None


def _error_reason_from_body(status: int, body: object) -> str:
    """Structured-error-shape extraction (``v1-overview.md`` "Errors and
    retries"): ``{"error": {code, message, request_id, resource, retryable,
    recovery}}``. Falls back to a bare status code when the body doesn't
    match — never includes the raw body verbatim."""
    if isinstance(body, dict):
        err = cast("dict[str, object]", body).get("error")
        if isinstance(err, dict):
            err_d = cast("dict[str, object]", err)
            code = err_d.get("code")
            message = err_d.get("message")
            retryable = err_d.get("retryable")
            bits = [f"http_{status}"]
            if isinstance(code, str):
                bits.append(code)
            if isinstance(message, str):
                bits.append(message)
            if retryable is not None:
                bits.append(f"retryable={retryable}")
            return " ".join(bits)
    return f"http_{status}"


def _validation_error_summary(exc: ValidationError) -> str:
    """A short, PAYLOAD-FREE summary of a Pydantic validation failure — field
    path + error type only. ``exc.errors()`` includes the offending
    ``input_value`` by default, which can be an account name or a balance;
    never let that reach a log line or an error string."""
    parts: list[str] = []
    for err in exc.errors(include_url=False)[:5]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc or '<root>'}: {err['type']}")
    return "; ".join(parts) or "validation_error"


def _query_params(**kwargs: str | int | float | bool | None) -> dict[str, str]:
    """Build a query-param dict, dropping ``None`` values and rendering
    ``bool`` as the lowercase JSON-ish ``"true"``/``"false"`` FastAPI expects
    for boolean query params."""
    out: dict[str, str] = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        out[key] = "true" if value is True else "false" if value is False else str(value)
    return out


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

ModelT = TypeVar("ModelT", bound=BaseModel)
ItemT = TypeVar("ItemT")


class TrackerV1Client:
    """Typed client for the portfolio-tracker ``/api/v1`` Portfolio Data
    Service. See the module docstring for the full contract."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        connect_timeout: float = _CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = _READ_TIMEOUT_SECONDS,
        analytics_read_timeout: float = _ANALYTICS_READ_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = _resolve_base_url(base_url)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._analytics_read_timeout = analytics_read_timeout
        self._session = session or requests.Session()

    # -- telemetry -----------------------------------------------------

    def _log_request(
        self,
        *,
        endpoint: str,
        duration_ms: float,
        status: int | None,
        schema_version: str | None,
    ) -> None:
        """One structured line per request — endpoint / duration / status /
        schema_version ONLY. Never a payload field, header, or body."""
        logger.info(
            "tracker_v1 request endpoint=%s duration_ms=%.1f status=%s schema_version=%s",
            endpoint,
            duration_ms,
            status if status is not None else "no_response",
            schema_version or "n/a",
        )

    # -- core fetch ------------------------------------------------------

    def _get_impl(
        self,
        endpoint: str,
        model: type[ModelT],
        *,
        params: Mapping[str, str] | None,
        timeout: float,
        check_version: bool,
    ) -> V1Fetch[ModelT]:
        url = f"{self.base_url}{endpoint}"
        start = time.monotonic()
        try:
            resp = self._session.get(url, params=params, timeout=(self._connect_timeout, timeout))
        except requests.Timeout as exc:
            self._log_request(
                endpoint=endpoint,
                duration_ms=(time.monotonic() - start) * 1000,
                status=None,
                schema_version=None,
            )
            return V1Fetch(
                available=False, endpoint=endpoint, error=f"timeout: {type(exc).__name__}"
            )
        except requests.ConnectionError as exc:
            self._log_request(
                endpoint=endpoint,
                duration_ms=(time.monotonic() - start) * 1000,
                status=None,
                schema_version=None,
            )
            return V1Fetch(
                available=False, endpoint=endpoint, error=f"connection_error: {type(exc).__name__}"
            )
        except requests.RequestException as exc:
            self._log_request(
                endpoint=endpoint,
                duration_ms=(time.monotonic() - start) * 1000,
                status=None,
                schema_version=None,
            )
            return V1Fetch(
                available=False, endpoint=endpoint, error=f"request_error: {type(exc).__name__}"
            )

        duration_ms = (time.monotonic() - start) * 1000

        try:
            body = resp.json()
        except ValueError:
            self._log_request(
                endpoint=endpoint,
                duration_ms=duration_ms,
                status=resp.status_code,
                schema_version=None,
            )
            return V1Fetch(available=False, endpoint=endpoint, error="json_decode_error")

        if resp.status_code != 200:
            reason = _error_reason_from_body(resp.status_code, body)
            self._log_request(
                endpoint=endpoint,
                duration_ms=duration_ms,
                status=resp.status_code,
                schema_version=None,
            )
            return V1Fetch(available=False, endpoint=endpoint, error=reason)

        if not isinstance(body, dict):
            self._log_request(
                endpoint=endpoint,
                duration_ms=duration_ms,
                status=resp.status_code,
                schema_version=None,
            )
            return V1Fetch(
                available=False,
                endpoint=endpoint,
                error=f"unexpected_shape: expected a JSON object, got {type(body).__name__}",
            )

        payload = cast("dict[str, object]", body)
        schema_version = _extract_schema_version(payload)

        if check_version and schema_version is not None:
            compat_error = _check_major_version(schema_version)
            if compat_error is not None:
                self._log_request(
                    endpoint=endpoint,
                    duration_ms=duration_ms,
                    status=resp.status_code,
                    schema_version=schema_version,
                )
                return V1Fetch(available=False, endpoint=endpoint, error=compat_error)

        self._log_request(
            endpoint=endpoint,
            duration_ms=duration_ms,
            status=resp.status_code,
            schema_version=schema_version,
        )

        try:
            parsed = model.model_validate(payload)
        except ValidationError as exc:
            return V1Fetch(
                available=False,
                endpoint=endpoint,
                error=f"schema_validation_error: {_validation_error_summary(exc)}",
            )

        return V1Fetch(available=True, endpoint=endpoint, data=parsed, meta=_extract_meta(parsed))

    def _get(
        self,
        endpoint: str,
        model: type[ModelT],
        *,
        params: Mapping[str, str] | None = None,
        timeout: float,
        check_version: bool = True,
    ) -> V1Fetch[ModelT]:
        """Outer never-raise safety net around :meth:`_get_impl` — any
        exception :meth:`_get_impl` didn't anticipate still degrades instead
        of propagating. The reason deliberately omits ``str(exc)``: an
        unexpected exception's message could itself embed payload content
        (e.g. a KeyError repr of a response dict)."""
        try:
            return self._get_impl(
                endpoint, model, params=params, timeout=timeout, check_version=check_version
            )
        except Exception as exc:  # never-raise contract
            logger.info(
                "tracker_v1 request endpoint=%s duration_ms=%.1f status=%s schema_version=%s",
                endpoint,
                0.0,
                "error",
                "n/a",
            )
            return V1Fetch(
                available=False, endpoint=endpoint, error=f"unexpected_error: {type(exc).__name__}"
            )

    # -- cursor pagination -------------------------------------------------

    def _paginate(
        self,
        endpoint: str,
        model: type[ModelT],
        *,
        params: Mapping[str, str],
        timeout: float,
        items: Callable[[ModelT], list[ItemT]],
        next_cursor: Callable[[ModelT], str | None],
        page_cap: int = _PAGE_CAP,
    ) -> V1Fetch[list[ItemT]]:
        """Follow ``next_cursor`` until null, concatenating each page's
        items. A malformed-cursor structured error on any page (surfaced by
        :meth:`_get` as ``available=False`` with the ``INVALID_CURSOR`` code
        in ``error``) aborts the whole fetch rather than returning a
        partially-collected list — matching the "never merge a partial read"
        spirit of the snapshot rules, applied to pagination."""
        collected: list[ItemT] = []
        cursor: str | None = None
        last_meta: V1Meta | None = None
        pages = 0
        while True:
            page_params = dict(params)
            if cursor is not None:
                page_params["cursor"] = cursor
            fetch = self._get(endpoint, model, params=page_params, timeout=timeout)
            if not fetch.available or fetch.data is None:
                return V1Fetch(
                    available=False, endpoint=endpoint, error=fetch.error, meta=last_meta
                )
            collected.extend(items(fetch.data))
            last_meta = fetch.meta or last_meta
            pages += 1
            cursor = next_cursor(fetch.data)
            if cursor is None:
                return V1Fetch(available=True, endpoint=endpoint, data=collected, meta=last_meta)
            if pages >= page_cap:
                return V1Fetch(
                    available=False,
                    endpoint=endpoint,
                    error=(
                        f"pagination_page_cap_exceeded: stopped after {page_cap} pages "
                        "with next_cursor still set"
                    ),
                    meta=last_meta,
                )

    # -- health / probe ----------------------------------------------------

    def get_health(self) -> V1Fetch[HealthV1]:
        """``GET /api/v1/health``. Exempt from the major-version gate — see
        :class:`HealthV1`'s docstring."""
        return self._get(
            "/api/v1/health", HealthV1, timeout=self._read_timeout, check_version=False
        )

    def probe_v1(self) -> V1Fetch[HealthV1]:
        """Health-check probe before a decision-grade read (``v1-overview.md``
        "Discovery": "Probe GET /api/v1/health before decision-grade reads")."""
        return self.get_health()

    # -- accounts / snapshot / positions -----------------------------------

    def get_accounts(self) -> V1Fetch[AccountsV1Result]:
        return self._get("/api/v1/accounts", AccountsV1Result, timeout=self._read_timeout)

    def get_portfolio_snapshot(self) -> V1Fetch[PortfolioSnapshotV1]:
        return self._get(
            "/api/v1/portfolio-snapshot", PortfolioSnapshotV1, timeout=self._read_timeout
        )

    def get_positions(self) -> V1Fetch[PositionsV1Result]:
        """No envelope on this endpoint — see :class:`PositionsV1Result`."""
        return self._get(
            "/api/v1/portfolio/positions", PositionsV1Result, timeout=self._read_timeout
        )

    def get_securities(self) -> V1Fetch[SecuritiesV1Result]:
        return self._get("/api/v1/securities", SecuritiesV1Result, timeout=self._read_timeout)

    def get_data_quality(self) -> V1Fetch[DataQualityV1Result]:
        return self._get("/api/v1/data-quality", DataQualityV1Result, timeout=self._read_timeout)

    # -- transactions (cursor-paginated) -----------------------------------

    def get_transactions_page(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> V1Fetch[TransactionsV1Result]:
        """One page. Default window trailing 730 days per the contract."""
        params = _query_params(
            start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
        )
        return self._get(
            "/api/v1/transactions", TransactionsV1Result, params=params, timeout=self._read_timeout
        )

    def get_all_transactions(
        self, *, start_date: str | None = None, end_date: str | None = None, limit: int = 500
    ) -> V1Fetch[list[TransactionV1]]:
        """Follows ``next_cursor`` until null (page cap :data:`_PAGE_CAP`)."""
        params = _query_params(start_date=start_date, end_date=end_date, limit=limit)
        return self._paginate(
            "/api/v1/transactions",
            TransactionsV1Result,
            params=params,
            timeout=self._read_timeout,
            items=lambda r: r.transactions,
            next_cursor=lambda r: r.next_cursor,
        )

    # -- cash flows (cursor-paginated) -------------------------------------

    def get_cash_flows_page(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        include_internal: bool = False,
        limit: int = 500,
        cursor: str | None = None,
    ) -> V1Fetch[CashFlowsV1Result]:
        params = _query_params(
            start_date=start_date,
            end_date=end_date,
            include_internal=include_internal,
            limit=limit,
            cursor=cursor,
        )
        return self._get(
            "/api/v1/cash-flows", CashFlowsV1Result, params=params, timeout=self._read_timeout
        )

    def get_all_cash_flows(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        include_internal: bool = False,
        limit: int = 500,
    ) -> V1Fetch[list[CashFlowV1]]:
        params = _query_params(
            start_date=start_date, end_date=end_date, include_internal=include_internal, limit=limit
        )
        return self._paginate(
            "/api/v1/cash-flows",
            CashFlowsV1Result,
            params=params,
            timeout=self._read_timeout,
            items=lambda r: r.cash_flows,
            next_cursor=lambda r: r.next_cursor,
        )

    # -- position snapshots (cursor-paginated) -----------------------------

    def get_position_snapshots_page(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> V1Fetch[PositionSnapshotsV1Result]:
        """One page. Default window trailing 90 days per the contract."""
        params = _query_params(
            start_date=start_date, end_date=end_date, limit=limit, cursor=cursor
        )
        return self._get(
            "/api/v1/position-snapshots",
            PositionSnapshotsV1Result,
            params=params,
            timeout=self._read_timeout,
        )

    def get_all_position_snapshots(
        self, *, start_date: str | None = None, end_date: str | None = None, limit: int = 500
    ) -> V1Fetch[list[PositionSnapshotV1]]:
        params = _query_params(start_date=start_date, end_date=end_date, limit=limit)
        return self._paginate(
            "/api/v1/position-snapshots",
            PositionSnapshotsV1Result,
            params=params,
            timeout=self._read_timeout,
            items=lambda r: r.snapshots,
            next_cursor=lambda r: r.next_cursor,
        )

    # -- analytics ----------------------------------------------------------

    def get_positioning(
        self, *, start_date: str | None = None, end_date: str | None = None
    ) -> V1Fetch[PositioningV1Result]:
        params = _query_params(start_date=start_date, end_date=end_date)
        return self._get(
            "/api/v1/analytics/positioning",
            PositioningV1Result,
            params=params,
            timeout=self._analytics_read_timeout,
        )

    def get_performance(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        include_backfill: bool = False,
        reserve_amount: float | None = None,
        exclude_index_etfs: bool = False,
    ) -> V1Fetch[PerformanceV1Result]:
        params = _query_params(
            start_date=start_date,
            end_date=end_date,
            include_backfill=include_backfill,
            reserve_amount=reserve_amount,
            exclude_index_etfs=exclude_index_etfs,
        )
        return self._get(
            "/api/v1/analytics/performance",
            PerformanceV1Result,
            params=params,
            timeout=self._analytics_read_timeout,
        )

    def get_position_performance(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        exclude_broad_index: bool = False,
    ) -> V1Fetch[PositionPerformanceV1Result]:
        params = _query_params(
            start_date=start_date, end_date=end_date, exclude_broad_index=exclude_broad_index
        )
        return self._get(
            "/api/v1/analytics/position-performance",
            PositionPerformanceV1Result,
            params=params,
            timeout=self._analytics_read_timeout,
        )

    def get_risk(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        benchmark: str = "SPY",
        risk_free_annual: float | None = None,
        exclude_index_etfs: bool = False,
        reserve_amount: float | None = None,
    ) -> V1Fetch[RiskV1Result]:
        params = _query_params(
            start_date=start_date,
            end_date=end_date,
            benchmark=benchmark,
            risk_free_annual=risk_free_annual,
            exclude_index_etfs=exclude_index_etfs,
            reserve_amount=reserve_amount,
        )
        return self._get(
            "/api/v1/analytics/risk", RiskV1Result, params=params, timeout=self._analytics_read_timeout
        )

    def get_exit_quality(
        self, *, start_date: str | None = None, end_date: str | None = None
    ) -> V1Fetch[ExitQualityV1Result]:
        params = _query_params(start_date=start_date, end_date=end_date)
        return self._get(
            "/api/v1/analytics/exit-quality",
            ExitQualityV1Result,
            params=params,
            timeout=self._analytics_read_timeout,
        )


__all__ = [
    "AccountV1",
    "AccountsV1Result",
    "BetaResult",
    "CashFlowV1",
    "CashFlowsV1Result",
    "ConcentrationOut",
    "DataQualityFindingOut",
    "DataQualityReportOut",
    "DataQualityV1Result",
    "DrawdownResult",
    "EquityFractionV1",
    "ExitQualityResult",
    "ExitQualityRow",
    "ExitQualityV1Result",
    "HealthV1",
    "Money",
    "MoneyOrNone",
    "PerformancePoint",
    "PerformanceSeries",
    "PerformanceV1Result",
    "PortfolioSnapshotV1",
    "PositionAlphaResult",
    "PositionAlphaRow",
    "PositionAlphaTimePoint",
    "PositionCorrelationRow",
    "PositionLotV1",
    "PositionPerformanceV1Result",
    "PositionSnapshotV1",
    "PositionSnapshotsV1Result",
    "PositionV1",
    "PositioningBucketOut",
    "PositioningOut",
    "PositioningV1Result",
    "PositionsV1Result",
    "ProviderHealthV1",
    "RiskV1Result",
    "SecuritiesV1Result",
    "SecurityV1",
    "TaxTreatment",
    "TrackerV1Client",
    "TransactionV1",
    "TransactionsV1Result",
    "UnderwaterPoint",
    "V1AccountCoverage",
    "V1Fetch",
    "V1Meta",
    "V1Model",
    "V1Warning",
]
