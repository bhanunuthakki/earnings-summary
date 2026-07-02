"""Deterministic tax awareness for the position-review service. Zero-LLM.

Answers the tax half of "should I trim <TICKER>?": what a proposed trim
*costs* in taxes, whether waiting converts short-term lots to long-term, and
where the position sits across taxable vs sheltered (Roth / 401k) accounts.
Everything here is arithmetic over the tracker feed — no model call anywhere.

Three layers, all pure (I/O stays with the caller):

  * :class:`TaxProfile` — the owner's marginal-rate assumptions. Defaults
    encode the owner-approved profile (2026-07-02 decision, verbatim: "Assume
    450k-500k MAGI MFJ in CA"); a local ``data/tax_profile.json`` (gitignored,
    the ``data/dcf_assumptions`` pattern) overrides any component.
  * :func:`reconstruct_lots` — FIFO tax lots per (ticker, taxable account)
    rebuilt from the tracker's transaction history, VALIDATED against the live
    per-account share counts. Where history can't reconstruct (transfers,
    pre-Plaid basis, truncated window, count mismatch) the result is tagged
    ``approximate`` with reasons — never a silent guess.
  * :func:`build_position_tax_view` — composes the lots (or the honest
    position-level fallback) into the :class:`PositionTaxView` the /review
    pre-analysis renders: ST/LT gain split, estimated tax dollars on a
    proposed trim, "waiting N days saves ~$X", and the account-placement note.

Holding-period convention: a lot is long-term when sold MORE than one year
after acquisition (IRS: the day after the one-year anniversary), see
:func:`long_term_date`. Dates are day-granularity naive dates throughout
(repo naive-UTC convention); ``today`` is injectable for tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from integrations.portfolio_tracker_client import LivePosition, LiveTransaction

__all__ = [
    "DEFAULT_TRIM_FRACTION",
    "SHELTERED_TREATMENTS",
    "TAX_PROFILE_FILENAME",
    "LotReconstruction",
    "PositionTaxView",
    "ReconstructedLot",
    "TaxProfile",
    "TrimTaxEstimate",
    "build_position_tax_view",
    "load_tax_profile",
    "long_term_date",
    "propose_trim_fraction",
    "reconstruct_lots",
]

# With no band signal and no concentration overage, the tax block prices a
# standard one-fifth trim tranche so the estimate is never anchored to nothing.
DEFAULT_TRIM_FRACTION = 0.20
# Account treatments where a trim realizes no taxable gain.
SHELTERED_TREATMENTS: tuple[str, ...] = ("tax_free", "tax_deferred")
TAX_PROFILE_FILENAME = "tax_profile.json"
# Buys within this many days of a loss-realizing sell trigger wash-sale
# disallowance (IRS §1091; spans accounts, including IRAs).
_WASH_SALE_WINDOW_DAYS = 30
# Reconstructed vs live share counts must agree within this absolute tolerance
# (broker fractional-share rounding) or the reconstruction degrades.
_QTY_TOLERANCE = 0.01


# --------------------------------------------------------------------------- #
# Owner tax profile
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaxProfile:
    """Marginal-rate components for gain estimates; all decimals (0.32 = 32%).

    ``magi_assumption`` is rendered in every footnote so estimates stay
    auditable against the assumption they were built on. ``source`` records
    where the numbers came from (``default`` or the override file).
    """

    federal_ordinary_rate: float
    federal_lt_rate: float
    niit_rate: float
    state_rate: float
    magi_assumption: str
    source: str = "default"

    @property
    def st_rate(self) -> float:
        """All-in short-term rate: federal ordinary + NIIT + state."""
        return self.federal_ordinary_rate + self.niit_rate + self.state_rate

    @property
    def lt_rate(self) -> float:
        """All-in long-term rate: federal LT + NIIT + state (CA taxes gains
        as ordinary income, so the state component is identical)."""
        return self.federal_lt_rate + self.niit_rate + self.state_rate

    def footnote(self) -> str:
        return (
            f"Tax estimate assumes {self.magi_assumption}: "
            f"ST {self.st_rate * 100:.1f}% "
            f"(fed {self.federal_ordinary_rate * 100:.0f}% + "
            f"NIIT {self.niit_rate * 100:.1f}% + CA {self.state_rate * 100:.1f}%), "
            f"LT {self.lt_rate * 100:.1f}%. FIFO lots from the tracker feed. "
            f"Override: data/{TAX_PROFILE_FILENAME}."
        )


# Owner-approved profile (2026-07-02): $450-500k MAGI MFJ lands in the 32%
# federal bracket for 2026 (taxable income after the standard deduction sits
# below the ~$512k 35% threshold — a very large trim could straddle it), the
# 15% LT bracket (15% runs to ~$600k MFJ), above the $250k NIIT floor, and in
# California's 9.3% bracket (CA taxes all capital gains as ordinary income).
_DEFAULT_PROFILE = TaxProfile(
    federal_ordinary_rate=0.32,
    federal_lt_rate=0.15,
    niit_rate=0.038,
    state_rate=0.093,
    magi_assumption="2026 MFJ, $450-500k MAGI, CA resident",
    source="default",
)


def _profile_float(payload: Mapping[str, object], key: str, fallback: float) -> float:
    v = payload.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return fallback
    return float(v)


def load_tax_profile(repo_root: Path) -> TaxProfile:
    """The owner tax profile: defaults, overridden by ``data/tax_profile.json``.

    The override file is local-only (``data/`` is gitignored) and partial —
    any subset of the rate keys / ``magi_assumption`` replaces its default.
    Unreadable or malformed JSON degrades to the defaults with the failure
    recorded in ``source`` (never a crash on a hand-edited file).
    """
    path = repo_root / "data" / TAX_PROFILE_FILENAME
    if not path.exists():
        return _DEFAULT_PROFILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TaxProfile(
            federal_ordinary_rate=_DEFAULT_PROFILE.federal_ordinary_rate,
            federal_lt_rate=_DEFAULT_PROFILE.federal_lt_rate,
            niit_rate=_DEFAULT_PROFILE.niit_rate,
            state_rate=_DEFAULT_PROFILE.state_rate,
            magi_assumption=_DEFAULT_PROFILE.magi_assumption,
            source=f"default ({TAX_PROFILE_FILENAME} unreadable)",
        )
    if not isinstance(raw, dict):
        return _DEFAULT_PROFILE
    payload = cast("dict[str, object]", raw)
    magi = payload.get("magi_assumption")
    return TaxProfile(
        federal_ordinary_rate=_profile_float(
            payload, "federal_ordinary_rate", _DEFAULT_PROFILE.federal_ordinary_rate
        ),
        federal_lt_rate=_profile_float(
            payload, "federal_lt_rate", _DEFAULT_PROFILE.federal_lt_rate
        ),
        niit_rate=_profile_float(payload, "niit_rate", _DEFAULT_PROFILE.niit_rate),
        state_rate=_profile_float(payload, "state_rate", _DEFAULT_PROFILE.state_rate),
        magi_assumption=(
            magi.strip()
            if isinstance(magi, str) and magi.strip()
            else _DEFAULT_PROFILE.magi_assumption
        ),
        source=f"data/{TAX_PROFILE_FILENAME}",
    )


# --------------------------------------------------------------------------- #
# Holding-period arithmetic
# --------------------------------------------------------------------------- #


def long_term_date(acquired: date) -> date:
    """First sale date on which a lot acquired on ``acquired`` is long-term.

    IRS convention: a gain is long-term when the holding period exceeds one
    year — the day AFTER the one-year anniversary. Feb 29 anniversaries roll
    to Mar 1.
    """
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # Feb 29 -> Mar 1 of the next year
        anniversary = date(acquired.year + 1, 3, 1)
    return anniversary + timedelta(days=1)


def _parse_day(raw: str) -> date | None:
    try:
        return date.fromisoformat((raw or "")[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# FIFO lot reconstruction
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconstructedLot:
    """One open FIFO lot in a taxable account."""

    acquired: date
    quantity: float
    cost_per_share: float  # buy fees rolled into basis
    account_id: int
    account_name: str


@dataclass(frozen=True)
class LotReconstruction:
    """Open taxable lots for one ticker, or an honest failure.

    ``approximate=True`` means the lots CANNOT be trusted for term-split math
    (the caller falls back to position-level basis); ``reasons`` say why.
    ``recent_buy_dates`` are ticker buys across ALL accounts inside the wash
    window input — kept even when approximate (wash risk is date-only math).
    """

    ticker: str
    lots: tuple[ReconstructedLot, ...]
    approximate: bool
    reasons: tuple[str, ...]
    recent_buy_dates: tuple[date, ...] = ()


def _txn_price(txn: LiveTransaction, qty: float) -> float | None:
    """Per-share price for a fill: the explicit field, else |amount| / qty."""
    if txn.price is not None and txn.price > 0:
        return float(txn.price)
    if txn.amount is not None and qty > 0:
        amount = abs(float(txn.amount))
        if amount > 0:
            return amount / qty
    return None


def reconstruct_lots(
    transactions: Sequence[LiveTransaction],
    *,
    ticker: str,
    taxable_accounts: Mapping[int, str],
    live_quantities: Mapping[int, float],
    history_truncated: bool = False,
    today: date | None = None,
) -> LotReconstruction:
    """Rebuild open FIFO lots for ``ticker`` across the taxable accounts.

    ``taxable_accounts`` maps account_id -> account_name for the accounts whose
    trims are taxable; ``live_quantities`` is the tracker's CURRENT per-account
    share count for the ticker — the ground truth the reconstruction must land
    on. Any of the following degrades the result to ``approximate`` (with a
    reason) rather than guessing: a truncated history window, a share transfer,
    a fill with no derivable price, a sell exceeding the open lots (pre-history
    position), an unparseable fill date, or a final count that misses the live
    count beyond fractional-rounding tolerance.
    """
    ticker = ticker.upper()
    today = today or date.today()
    reasons: list[str] = []
    if history_truncated:
        reasons.append("history window truncated at the tracker's row cap")

    mine = [t for t in transactions if (t.ticker or "").strip().upper() == ticker]
    # Ticker buys in ANY account (wash-sale rules span accounts, IRAs included).
    wash_floor = today - timedelta(days=_WASH_SALE_WINDOW_DAYS)
    recent_buys = tuple(
        sorted(
            d
            for d in (_parse_day(t.date) for t in mine if (t.type or "").strip().lower() == "buy")
            if d is not None and d >= wash_floor
        )
    )

    open_lots: list[ReconstructedLot] = []
    for account_id, account_name in sorted(taxable_accounts.items()):
        fills = [t for t in mine if t.account_id == account_id]
        # Chronological; same-day buys land before sells so intraday
        # round-trips have lots to consume.
        keyed: list[tuple[date, int, LiveTransaction]] = []
        for t in fills:
            kind = (t.type or "").strip().lower()
            if kind not in ("buy", "sell", "transfer"):
                continue  # cash / fee / dividend rows carry no share movement
            day = _parse_day(t.date)
            if day is None:
                reasons.append(f"unparseable fill date {t.date!r} in {account_name}")
                continue
            keyed.append((day, 0 if kind == "buy" else 1, t))
        keyed.sort(key=lambda k: (k[0], k[1]))

        acct_lots: list[ReconstructedLot] = []
        for day, _, t in keyed:
            kind = (t.type or "").strip().lower()
            qty = abs(float(t.quantity or 0.0))
            if kind == "transfer":
                if qty > 0:
                    reasons.append(f"share transfer in {account_name} (no basis carried)")
                continue
            if qty <= 0:
                continue
            if kind == "buy":
                price = _txn_price(t, qty)
                if price is None:
                    reasons.append(f"buy with no derivable price in {account_name}")
                    continue
                basis = price * qty + abs(float(t.fees or 0.0))
                acct_lots.append(
                    ReconstructedLot(
                        acquired=day,
                        quantity=qty,
                        cost_per_share=basis / qty,
                        account_id=account_id,
                        account_name=account_name,
                    )
                )
            else:  # sell — consume FIFO
                remaining = qty
                while remaining > _QTY_TOLERANCE and acct_lots:
                    head = acct_lots[0]
                    if head.quantity <= remaining + 1e-9:
                        remaining -= head.quantity
                        acct_lots.pop(0)
                    else:
                        acct_lots[0] = ReconstructedLot(
                            acquired=head.acquired,
                            quantity=head.quantity - remaining,
                            cost_per_share=head.cost_per_share,
                            account_id=head.account_id,
                            account_name=head.account_name,
                        )
                        remaining = 0.0
                if remaining > _QTY_TOLERANCE:
                    reasons.append(
                        f"sell exceeds reconstructed lots in {account_name} "
                        "(position predates the history window)"
                    )

        live_qty = float(live_quantities.get(account_id, 0.0))
        rebuilt_qty = sum(lot.quantity for lot in acct_lots)
        if abs(rebuilt_qty - live_qty) > _QTY_TOLERANCE:
            reasons.append(
                f"reconstructed {rebuilt_qty:g} vs live {live_qty:g} shares in {account_name}"
            )
        open_lots.extend(acct_lots)

    return LotReconstruction(
        ticker=ticker,
        lots=tuple(open_lots),
        approximate=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),  # dedupe, keep order
        recent_buy_dates=recent_buys,
    )


# --------------------------------------------------------------------------- #
# Trim proposal + tax estimate
# --------------------------------------------------------------------------- #


def propose_trim_fraction(
    *,
    weight_pct: float | None,
    target_band: tuple[float, float] | None,
    weight_vs_band: str,
    concentration_flag: bool,
    concentration_pct: float,
    default_fraction: float = DEFAULT_TRIM_FRACTION,
) -> tuple[float, str]:
    """The deterministic trim size the tax block prices, with its rationale.

    Above the target band -> trim back to the band top (the guard's own
    trim-to-target case). No band but concentration-flagged -> trim back to
    the concentration bar. Otherwise a standard ``default_fraction`` tranche —
    the estimate is illustrative, and says so in the rationale.
    """
    if (
        weight_vs_band == "above_band"
        and target_band is not None
        and weight_pct is not None
        and weight_pct > target_band[1] > 0
    ):
        return (weight_pct - target_band[1]) / weight_pct, (
            f"to top of band ({target_band[1]:.1f}%)"
        )
    if concentration_flag and weight_pct is not None and weight_pct > concentration_pct > 0:
        return (weight_pct - concentration_pct) / weight_pct, (
            f"to the {concentration_pct:.0f}% concentration bar"
        )
    return default_fraction, f"illustrative {default_fraction * 100:.0f}% tranche"


@dataclass(frozen=True)
class TrimTaxEstimate:
    """Tax cost of one proposed trim, executed FIFO in the taxable accounts.

    Exact mode (validated lots): ``st_gain_usd``/``lt_gain_usd`` are the
    realized split and ``tax_low_usd == tax_high_usd``. Approximate mode
    (position-level basis, term unknown): the split is ``None`` and the tax is
    an honest [all-LT, all-ST] range.
    """

    trim_fraction: float
    trim_rationale: str
    trim_shares: float
    trim_usd: float
    taxable_shares_consumed: float
    realized_gain_usd: float
    st_gain_usd: float | None
    lt_gain_usd: float | None
    tax_low_usd: float
    tax_high_usd: float
    days_until_lt: int | None
    wait_savings_usd: float | None
    wash_sale_risk: bool


@dataclass(frozen=True)
class PositionTaxView:
    """The tax block for one position — what /review renders.

    ``available=False`` + ``reason`` when the tracker can't supply a live
    position at all. ``approximate=True`` whenever anything below lot-exact
    math produced the numbers; ``approx_reasons`` say why, and the ST/LT
    embedded split is ``None`` in that mode.
    """

    available: bool
    reason: str | None
    approximate: bool
    approx_reasons: tuple[str, ...]
    eval_price: float | None
    taxable_mv_usd: float | None
    taxable_pct_of_position: float | None
    sheltered_mv_usd: float | None
    sheltered_note: str | None
    st_unrealized_usd: float | None
    lt_unrealized_usd: float | None
    trim: TrimTaxEstimate | None
    footnote: str


def unavailable_tax_view(reason: str) -> PositionTaxView:
    """The degraded view for an offline tracker / unheld name — renders as a
    single honest line, never a crash."""
    return PositionTaxView(
        available=False,
        reason=reason,
        approximate=True,
        approx_reasons=(reason,),
        eval_price=None,
        taxable_mv_usd=None,
        taxable_pct_of_position=None,
        sheltered_mv_usd=None,
        sheltered_note=None,
        st_unrealized_usd=None,
        lt_unrealized_usd=None,
        trim=None,
        footnote="",
    )


def _exact_trim_estimate(
    lots: Sequence[ReconstructedLot],
    *,
    trim_fraction: float,
    trim_rationale: str,
    trim_shares: float,
    eval_price: float,
    profile: TaxProfile,
    today: date,
    recent_buy_dates: Sequence[date],
) -> TrimTaxEstimate:
    """FIFO-consume ``trim_shares`` from validated lots at ``eval_price``."""
    ordered = sorted(lots, key=lambda lot: lot.acquired)
    remaining = trim_shares
    st_gain = 0.0
    lt_gain = 0.0
    consumed = 0.0
    wash_risk = False
    consumed_st: list[tuple[ReconstructedLot, float]] = []  # (lot, qty consumed)
    for lot in ordered:
        if remaining <= 1e-9:
            break
        take = min(lot.quantity, remaining)
        remaining -= take
        consumed += take
        gain = (eval_price - lot.cost_per_share) * take
        if today >= long_term_date(lot.acquired):
            lt_gain += gain
        else:
            st_gain += gain
            consumed_st.append((lot, take))
        if gain < 0 and recent_buy_dates:
            # Realizing a loss with a buy inside the 30-day window: §1091 risk.
            wash_risk = True

    # Waiting until every consumed short-term lot crosses its LT date re-rates
    # the positive ST gain from the ST to the LT stack.
    days_until_lt: int | None = None
    wait_savings: float | None = None
    st_positive = sum(
        (eval_price - lot.cost_per_share) * qty
        for lot, qty in consumed_st
        if eval_price > lot.cost_per_share
    )
    if consumed_st and st_positive > 0:
        days_until_lt = max((long_term_date(lot.acquired) - today).days for lot, _ in consumed_st)
        wait_savings = st_positive * (profile.st_rate - profile.lt_rate)

    # Tax on positive stacks only; net losses reduce the bill toward zero but
    # loss-harvesting mechanics (netting order, carryforward) stay out of scope.
    tax = max(st_gain, 0.0) * profile.st_rate + max(lt_gain, 0.0) * profile.lt_rate
    return TrimTaxEstimate(
        trim_fraction=trim_fraction,
        trim_rationale=trim_rationale,
        trim_shares=trim_shares,
        trim_usd=trim_shares * eval_price,
        taxable_shares_consumed=consumed,
        realized_gain_usd=st_gain + lt_gain,
        st_gain_usd=st_gain,
        lt_gain_usd=lt_gain,
        tax_low_usd=tax,
        tax_high_usd=tax,
        days_until_lt=days_until_lt,
        wait_savings_usd=wait_savings,
        wash_sale_risk=wash_risk,
    )


def _approximate_trim_estimate(
    *,
    trim_fraction: float,
    trim_rationale: str,
    trim_shares: float,
    eval_price: float,
    taxable_shares: float,
    taxable_basis: float,
    profile: TaxProfile,
) -> TrimTaxEstimate:
    """Position-level fallback: average basis, holding period unknown, so the
    tax is an [all-LT, all-ST] range — tagged approximate by the view."""
    consumed = min(trim_shares, taxable_shares)
    gain_embedded = taxable_shares * eval_price - taxable_basis
    realized = gain_embedded * (consumed / taxable_shares) if taxable_shares > 0 else 0.0
    taxable_gain = max(realized, 0.0)
    return TrimTaxEstimate(
        trim_fraction=trim_fraction,
        trim_rationale=trim_rationale,
        trim_shares=trim_shares,
        trim_usd=trim_shares * eval_price,
        taxable_shares_consumed=consumed,
        realized_gain_usd=realized,
        st_gain_usd=None,
        lt_gain_usd=None,
        tax_low_usd=taxable_gain * profile.lt_rate,
        tax_high_usd=taxable_gain * profile.st_rate,
        days_until_lt=None,
        wait_savings_usd=None,
        wash_sale_risk=False,
    )


def build_position_tax_view(
    position: LivePosition,
    history: Sequence[LiveTransaction] | None,
    *,
    profile: TaxProfile,
    at_price: float | None,
    trim_fraction: float,
    trim_rationale: str,
    history_truncated: bool = False,
    today: date | None = None,
) -> PositionTaxView:
    """Compose the tax block for one live position. Pure — callers fetch.

    ``history=None`` means the transaction fetch failed; the view then runs on
    position-level basis (approximate), never raises. ``at_price`` (the
    "/review <T> at $X" level) overrides the implied live price for gain math.
    """
    today = today or date.today()
    ticker = (position.ticker or "?").upper()
    total_qty = float(position.quantity or 0.0)
    if total_qty <= 0:
        return unavailable_tax_view("no live share count for the position")

    # --- account split -------------------------------------------------------
    reasons: list[str] = []
    taxable_accounts: dict[int, str] = {}
    live_qty_by_account: dict[int, float] = {}
    taxable_shares = 0.0
    taxable_mv = 0.0
    taxable_basis = 0.0
    taxable_basis_known = True
    sheltered_mv = 0.0
    sheltered_shares = 0.0
    sheltered_names: set[str] = set()
    for acct in position.accounts:
        if acct.tax_treatment in SHELTERED_TREATMENTS:
            sheltered_mv += acct.market_value or 0.0
            sheltered_shares += acct.quantity
            sheltered_names.add("Roth/HSA" if acct.tax_treatment == "tax_free" else "401k/IRA")
            continue
        if acct.tax_treatment == "unknown":
            reasons.append(f"account {acct.account_name!r} treatment unknown; treated as taxable")
        taxable_accounts[acct.account_id] = acct.account_name
        live_qty_by_account[acct.account_id] = acct.quantity
        taxable_shares += acct.quantity
        taxable_mv += acct.market_value or 0.0
        if acct.cost_basis is None:
            taxable_basis_known = False
        else:
            taxable_basis += acct.cost_basis

    market_value = float(position.market_value or 0.0)
    implied_price = (market_value / total_qty) if market_value > 0 else None
    eval_price = at_price if at_price is not None and at_price > 0 else implied_price

    sheltered_note: str | None = None
    trim_shares = trim_fraction * total_qty
    if sheltered_mv > 0 and market_value > 0:
        where = " + ".join(sorted(sheltered_names))
        covers = sheltered_shares + _QTY_TOLERANCE >= trim_shares
        sheltered_note = (
            f"{100.0 * sheltered_mv / market_value:.0f}% of the position sits in {where} — "
            + (
                "the proposed trim fits there entirely: $0 tax if executed in the sheltered account"
                if covers
                else "trim there first; only the taxable remainder owes tax"
            )
        )

    taxable_pct = (100.0 * taxable_mv / market_value) if market_value > 0 else None
    if eval_price is None:
        return PositionTaxView(
            available=True,
            reason=None,
            approximate=True,
            approx_reasons=(*reasons, "no price available to evaluate gains at"),
            eval_price=None,
            taxable_mv_usd=taxable_mv,
            taxable_pct_of_position=taxable_pct,
            sheltered_mv_usd=sheltered_mv,
            sheltered_note=sheltered_note,
            st_unrealized_usd=None,
            lt_unrealized_usd=None,
            trim=None,
            footnote=profile.footnote(),
        )

    if taxable_shares <= 0:
        # Fully sheltered: the whole tax story is the placement note.
        return PositionTaxView(
            available=True,
            reason=None,
            approximate=bool(reasons),
            approx_reasons=tuple(reasons),
            eval_price=eval_price,
            taxable_mv_usd=0.0,
            taxable_pct_of_position=0.0,
            sheltered_mv_usd=sheltered_mv,
            sheltered_note=sheltered_note
            or "the entire position is sheltered — trims realize no taxable gain",
            st_unrealized_usd=0.0,
            lt_unrealized_usd=0.0,
            trim=None,
            footnote=profile.footnote(),
        )

    # --- lot-exact path -------------------------------------------------------
    recon: LotReconstruction | None = None
    if history is not None:
        recon = reconstruct_lots(
            history,
            ticker=ticker,
            taxable_accounts=taxable_accounts,
            live_quantities=live_qty_by_account,
            history_truncated=history_truncated,
            today=today,
        )
    else:
        reasons.append("transaction history unavailable from the tracker")

    if recon is not None and not recon.approximate and recon.lots:
        st_unreal = sum(
            (eval_price - lot.cost_per_share) * lot.quantity
            for lot in recon.lots
            if today < long_term_date(lot.acquired)
        )
        lt_unreal = sum(
            (eval_price - lot.cost_per_share) * lot.quantity
            for lot in recon.lots
            if today >= long_term_date(lot.acquired)
        )
        trim = _exact_trim_estimate(
            recon.lots,
            trim_fraction=trim_fraction,
            trim_rationale=trim_rationale,
            trim_shares=trim_shares,
            eval_price=eval_price,
            profile=profile,
            today=today,
            recent_buy_dates=recon.recent_buy_dates,
        )
        return PositionTaxView(
            available=True,
            reason=None,
            approximate=bool(reasons),
            approx_reasons=tuple(reasons),
            eval_price=eval_price,
            taxable_mv_usd=taxable_mv,
            taxable_pct_of_position=taxable_pct,
            sheltered_mv_usd=sheltered_mv,
            sheltered_note=sheltered_note,
            st_unrealized_usd=st_unreal,
            lt_unrealized_usd=lt_unreal,
            trim=trim,
            footnote=profile.footnote(),
        )

    # --- honest position-level fallback ---------------------------------------
    if recon is not None:
        reasons.extend(recon.reasons or ("no reconstructable lots in the history window",))
    if not taxable_basis_known:
        if position.cost_basis is not None and market_value > 0:
            taxable_basis = float(position.cost_basis) * (taxable_mv / market_value)
            reasons.append("per-account basis missing; prorated the position-level basis")
        else:
            return PositionTaxView(
                available=True,
                reason=None,
                approximate=True,
                approx_reasons=(*dict.fromkeys(reasons), "no cost basis on file"),
                eval_price=eval_price,
                taxable_mv_usd=taxable_mv,
                taxable_pct_of_position=taxable_pct,
                sheltered_mv_usd=sheltered_mv,
                sheltered_note=sheltered_note,
                st_unrealized_usd=None,
                lt_unrealized_usd=None,
                trim=None,
                footnote=profile.footnote(),
            )
    trim = _approximate_trim_estimate(
        trim_fraction=trim_fraction,
        trim_rationale=trim_rationale,
        trim_shares=trim_shares,
        eval_price=eval_price,
        taxable_shares=taxable_shares,
        taxable_basis=taxable_basis,
        profile=profile,
    )
    return PositionTaxView(
        available=True,
        reason=None,
        approximate=True,
        approx_reasons=tuple(dict.fromkeys(reasons)),
        eval_price=eval_price,
        taxable_mv_usd=taxable_mv,
        taxable_pct_of_position=taxable_pct,
        sheltered_mv_usd=sheltered_mv,
        sheltered_note=sheltered_note,
        st_unrealized_usd=None,
        lt_unrealized_usd=None,
        trim=trim,
        footnote=profile.footnote(),
    )
