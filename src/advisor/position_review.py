"""Deterministic position facts for on-demand research.

Read portfolio weights, explicit thesis and sizing settings, valuation, tax and
portfolio risk. The pure helpers calculate context from supplied inputs. This
module does not generate a recommendation, enforce coaching or write a memo.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

from allocation.concentration import (
    classify_entry_method,
    classify_zone,
)
from db_paths import require_product_db_path
from identity import DEFAULT_USER_ID

if TYPE_CHECKING:
    from advisor.position_tax import PositionTaxView
    from capture.matcher import RosterIndex
    from compute.thesis_evaluator import ThesisVerdict
    from integrations.portfolio_tracker_client import LiveTransaction

log = logging.getLogger(__name__)

__all__ = [
    "BAND_TOLERANCE",
    "DEFAULT_MOS_BAR",
    "SELL_OVER_UNDER",
    "TRIM_OVER_UNDER",
    "PreAnalysis",
    "RiskContext",
    "assemble_pre_analysis",
    "build_pre_analysis",
    "classify_valuation",
    "classify_weight_band",
    "parse_review_command",
    "render_pre_analysis_chat",
    "render_risk_lines",
    "render_tax_lines",
    "review_reply_text",
    "summarize_verdict",
]

# "at $70" / "above 70" / "over $12.50" -> the price level to review at. Shared
# by the chat command and its pure parser tests.
_AT_PRICE_RX = re.compile(r"(?:at|above|over)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Tunables — the valuation ladder mirrors the ``dcf.valuation.over_under_pct``
# docstring EXACTLY (>0.10 trim, >0.20 sell, < -mos_bar initiation). The guard
# test asserts these against that function so the two can never drift.
# --------------------------------------------------------------------------- #
TRIM_OVER_UNDER = 0.10  # over_under (decimal) above which the ladder says "trim"
SELL_OVER_UNDER = 0.20  # ... "sell"
DEFAULT_MOS_BAR = 0.30  # fallback initiation bar when a holding JSON omits mos_bar
BAND_TOLERANCE = 0.20  # +/- 20% tolerance band around a stated target weight
# NOTE: the old flat 8% single-name concentration bar is gone (PRD §7.2,
# P0.2) — single-name sizing is now read through allocation.concentration's soft
# zones (ordinary/meaningful/concentrated/highly_concentrated/exceptional).
# `concentration_flag` on PreAnalysis now means "zone >= concentrated"
# (weight >= allocation.concentration.TRIM_ASSESSMENT_THRESHOLD_PCT).

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_HOLDINGS_SUBDIR: tuple[str, ...] = ("micro_thesis", "holdings")

# BreachStatus.value -> the coarse label the service reasons over. Read by
# ``.value`` (a str) rather than importing the enum, keeping this core light.
_BREAK_STATUS_LABEL: dict[str, str] = {
    "ok": "intact",
    "warn": "warn",
    "breach": "breach",
    "unresolved": "unresolved",
}


@dataclass(frozen=True)
class RiskContext:
    """Deterministic, zero-LLM read of a position's portfolio-risk footprint —
    the ``/review`` facts. Each field is ``None``/``()`` when that sub-leg had
    nothing to report or failed to compute, and the WHOLE block renders
    nothing (not a placeholder) when every field is empty. Built by
    :func:`build_risk_context`, which independently try/excepts each leg so
    one broken input (e.g. a pre-migration DB missing
    ``business_factor_exposures``) never blocks the others."""

    # Share of TOTAL BOOK RISK this holding carries (0-100), from
    # allocation.book_risk.build_book_risk's Euler risk-contribution split —
    # NOT the same as its portfolio weight (a volatile/correlated name can
    # carry more risk share than dollar share).
    risk_share_pct: float | None = None
    # This holding's correlation to the REST OF THE BOOK (marginal_risk's
    # corr_to_book), from the SAME build_book_risk call above — reused, not a
    # second covariance build.
    corr_to_book: float | None = None
    # Human-readable crowding-cluster membership (portfolio_correlation's
    # connected-components clusters at >=0.70 pairwise correlation), e.g.
    # "co-moves with NU (avg corr 0.75, 22% combined weight)" — None when the
    # ticker isn't in any multi-name cluster, or the read degraded.
    crowding_cluster: str | None = None
    # This ticker's OWN top business-factor loadings (C3
    # business_factor_exposures, is_latest rows only), highest first, capped
    # at 3 — e.g. (("Brazil consumer credit", 0.9), ("LatAm consumer/FX", 0.7)).
    top_factors: tuple[tuple[str, float], ...] = ()
    factor_provenance: str | None = None
    common_drawdown: str | None = None
    # EVENT_SCENARIOS (src/portfolio_montecarlo.py) ids where this ticker is a
    # NAMED member — e.g. ("joint_latam",). Membership only; the modeled
    # book-level stress return is the Risk tab's job, not this one-line read.
    event_scenarios: tuple[str, ...] = ()
    # Human-readable reasons any sub-leg above came back empty (network/DB
    # error, missing table, thin price history) — never surfaced to the
    # owner, logged for debugging only.
    degraded_reasons: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """True when every substantive field is absent — mirrors
        the empty-context contract; the renderer treats this identically
        to ``risk=None``."""
        return (
            self.risk_share_pct is None
            and self.corr_to_book is None
            and self.crowding_cluster is None
            and not self.top_factors
            and self.factor_provenance is None
            and self.common_drawdown is None
            and not self.event_scenarios
        )


@dataclass(frozen=True)
class PreAnalysis:
    """Grounded, LLM-free snapshot of a position — the input to the reasoning
    stage. Every field is a fact read from a store (or ``None`` when that store
    has nothing for the ticker), never an opinion."""

    ticker: str
    # --- sizing --------------------------------------------------------------
    weight_pct: float | None  # percent of book (0-100)
    weight_source: str  # "live" | "materialized" | "unknown"
    market_value_usd: float | None
    unrealized_pnl_usd: float | None
    target_weight_pct: float | None  # from position_sizing_intent (empty in prod)
    target_band: tuple[float, float] | None  # (lo, hi) percent, when a target is set
    weight_vs_band: str  # "above_band" | "in_band" | "below_band" | "no_band"
    conviction_1_5: int | None
    concentration_flag: bool  # zone >= "concentrated" (weight >= TRIM_ASSESSMENT_THRESHOLD_PCT)
    # --- fundamentals --------------------------------------------------------
    thesis_present: bool  # micro_thesis/holdings/<T>.json exists
    verdict_label: str | None  # holdings JSON "verdict" (e.g. "Intact")
    key_driver: str | None
    break_rule_status: str  # "intact" | "warn" | "breach" | "unresolved" | "no_thesis"
    tripped_rules: tuple[str, ...]
    # --- valuation -----------------------------------------------------------
    dcf_gap_pct: float | None  # percent, positive = OVER-valued (recomputed at at_price)
    npv_per_share: float | None
    dcf_live_price: float | None  # the price the stored DCF run used
    dcf_date: str | None
    at_price: float | None  # the level the question asked about ("above $70")
    mos_bar: float | None
    valuation_verdict: str  # "sell" | "trim" | "buy" | "fair" | "n/a"
    # --- owner notes / instrument -------------------------------------------
    has_decision_note: bool
    is_index_instrument: bool  # ETF — a macro/factor sleeve, not a single-name thesis
    # --- tax (deterministic, advisor.position_tax) ---------------------------
    # None only on PreAnalysis objects built before the tax stage (tests, old
    # callers); build_pre_analysis always attaches a view, degraded or not.
    tax: PositionTaxView | None = None
    # --- concentration zones (PRD §7.2, P0.2) --------------------------------
    # allocation.concentration.Zone name, or None when weight_pct is unknown.
    concentration_zone: str | None = None
    # allocation.concentration.ENTRY_INTENTIONAL / ENTRY_APPRECIATION, or None
    # when the entry method couldn't be derived (no transaction history).
    entry_method: str | None = None
    # One-line human text for concentration_zone (PRD §7.2 table), or None.
    zone_treatment: str | None = None
    # --- risk context (C7, deterministic) ------------------------------------
    # None on PreAnalysis objects built before the risk stage (tests, old
    # callers) — treated identically to an empty RiskContext by the
    # renderers. build_pre_analysis always attaches one (degraded-empty/None
    # or not); byte-identical to today's output when every sub-leg degrades.
    risk: RiskContext | None = None


# --------------------------------------------------------------------------- #
# Pure core (no I/O)
# --------------------------------------------------------------------------- #


def _over_under(price: float, fair_value_per_share: float) -> float:
    """(price - fair) / fair as a decimal; positive = over-valued.

    Duplicates the one-line ``dcf.valuation.over_under_pct`` body so the pure
    core carries no heavy import; the guard test pins the two to agree.
    """
    return (price - fair_value_per_share) / fair_value_per_share


def classify_valuation(over_under_dec: float | None, mos_bar: float | None) -> str:
    """Execute the trim/sell ladder on an over/under-valuation decimal.

    ``> SELL_OVER_UNDER`` → ``"sell"``; ``> TRIM_OVER_UNDER`` → ``"trim"``;
    ``< -mos_bar`` (an initiation-grade discount) → ``"buy"``; else ``"fair"``.
    ``None`` (no DCF on file) → ``"n/a"``. Falls back to ``DEFAULT_MOS_BAR`` when
    the holding JSON omits its own bar.
    """
    if over_under_dec is None:
        return "n/a"
    if over_under_dec > SELL_OVER_UNDER:
        return "sell"
    if over_under_dec > TRIM_OVER_UNDER:
        return "trim"
    bar = mos_bar if mos_bar is not None else DEFAULT_MOS_BAR
    if over_under_dec < -bar:
        return "buy"
    return "fair"


def classify_weight_band(
    weight_pct: float | None,
    target_weight_pct: float | None,
    *,
    tolerance: float = BAND_TOLERANCE,
) -> tuple[str, tuple[float, float] | None]:
    """Classify the live weight against a +/-tolerance band around the target.

    Returns ``(label, band)``. With no recorded target the band is ``None`` and
    the label is ``"no_band"`` — the caller leans on ``concentration_flag``
    instead (the prod reality: no target weights recorded yet). A known target
    but unknown live weight also yields ``"no_band"`` (band still returned).
    """
    if target_weight_pct is None:
        return "no_band", None
    lo = target_weight_pct * (1.0 - tolerance)
    hi = target_weight_pct * (1.0 + tolerance)
    band = (lo, hi)
    if weight_pct is None:
        return "no_band", band
    if weight_pct > hi:
        return "above_band", band
    if weight_pct < lo:
        return "below_band", band
    return "in_band", band


def summarize_verdict(verdict: ThesisVerdict | None) -> tuple[str, tuple[str, ...]]:
    """Reduce a ``ThesisVerdict`` to ``(status_label, tripped_rules)``.

    ``None`` (no holdings JSON to evaluate) → ``("no_thesis", ())``. Tripped
    rules are the WARN + BREACH evaluations, most-severe context first as the
    evaluator ordered them (universal tripwires before per-ticker breakers).
    Read via ``.value`` so this stays enum-import-free.
    """
    if verdict is None:
        return "no_thesis", ()
    label = _BREAK_STATUS_LABEL.get(verdict.overall_status.value, "unresolved")
    tripped = tuple(
        f"{ev.rule.rule_id} ({ev.status.value}): {ev.detail}"
        for ev in verdict.rule_evaluations
        if ev.status.value in ("warn", "breach")
    )
    return label, tripped


def _opt_float(payload: dict[str, object], key: str) -> float | None:
    """Read a JSON scalar as float; None for missing / bool / unparseable."""
    v = payload.get(key)
    if isinstance(v, bool):  # bool is an int subclass — reject it as a number
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _opt_str(payload: dict[str, object], key: str) -> str | None:
    v = payload.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def assemble_pre_analysis(
    ticker: str,
    *,
    weight_pct: float | None,
    weight_source: str,
    market_value_usd: float | None,
    unrealized_pnl_usd: float | None,
    target_weight_pct: float | None,
    conviction_1_5: int | None,
    break_rule_status: str,
    tripped_rules: tuple[str, ...],
    holdings_json: dict[str, object] | None,
    dcf_tuple: tuple[float | None, float | None, float | None, str | None] | None,
    has_decision_note: bool,
    is_index_instrument: bool,
    at_price: float | None,
    entry_method: str | None = None,
) -> PreAnalysis:
    """Compose primitives into a :class:`PreAnalysis`. Pure — no I/O.

    ``dcf_tuple`` is ``(fv_gap_pct, npv_per_share, live_price, valuation_date)``
    as returned by ``research_cockpit.latest_dcf_runs``. When ``at_price`` is
    supplied AND a positive NPV is on file, the over/under gap is RECOMPUTED at
    that level so "above $70" is answered against $70, not the stored close.

    ``entry_method`` (:data:`allocation.concentration.ENTRY_INTENTIONAL` /
    ``ENTRY_APPRECIATION`` / ``None``) is the caller's pre-derived read of
    whether the position reached its current size through a recent
    intentional buy or pure price appreciation — ``build_pre_analysis``
    derives it from the tracker's transaction history; this function is pure
    and never fetches it itself.
    """
    thesis_present = holdings_json is not None
    verdict_label = _opt_str(holdings_json, "verdict") if holdings_json is not None else None
    key_driver = _opt_str(holdings_json, "key_driver") if holdings_json is not None else None
    mos_bar = _opt_float(holdings_json, "mos_bar") if holdings_json is not None else None

    fv_gap_pct: float | None = None
    npv: float | None = None
    dcf_price: float | None = None
    dcf_date: str | None = None
    if dcf_tuple is not None:
        fv_gap_pct, npv, dcf_price, dcf_date = dcf_tuple

    over_under_dec: float | None = None
    dcf_gap_pct: float | None = None
    if at_price is not None and npv is not None and npv > 0:
        over_under_dec = _over_under(at_price, npv)
        dcf_gap_pct = over_under_dec * 100.0
    elif fv_gap_pct is not None:
        dcf_gap_pct = fv_gap_pct
        over_under_dec = fv_gap_pct / 100.0
    valuation_verdict = classify_valuation(over_under_dec, mos_bar)

    weight_vs_band, target_band = classify_weight_band(weight_pct, target_weight_pct)
    zone_assessment = classify_zone(weight_pct)
    concentration_flag = zone_assessment is not None and zone_assessment.trim_assessment_required
    concentration_zone = zone_assessment.zone if zone_assessment is not None else None
    zone_treatment = zone_assessment.treatment if zone_assessment is not None else None

    return PreAnalysis(
        ticker=ticker.upper(),
        weight_pct=weight_pct,
        weight_source=weight_source,
        market_value_usd=market_value_usd,
        unrealized_pnl_usd=unrealized_pnl_usd,
        target_weight_pct=target_weight_pct,
        target_band=target_band,
        weight_vs_band=weight_vs_band,
        conviction_1_5=conviction_1_5,
        concentration_flag=concentration_flag,
        thesis_present=thesis_present,
        verdict_label=verdict_label,
        key_driver=key_driver,
        break_rule_status=break_rule_status,
        tripped_rules=tuple(tripped_rules),
        dcf_gap_pct=dcf_gap_pct,
        npv_per_share=npv,
        dcf_live_price=dcf_price,
        dcf_date=dcf_date,
        at_price=at_price,
        mos_bar=mos_bar,
        valuation_verdict=valuation_verdict,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        concentration_zone=concentration_zone,
        entry_method=entry_method,
        zone_treatment=zone_treatment,
    )


# --------------------------------------------------------------------------- #
# I/O orchestrator
# --------------------------------------------------------------------------- #


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively; None on any
    read/parse failure or a non-object payload (so an unencoded name — FLKR —
    degrades cleanly to ``thesis_present=False``)."""
    path = repo_root.joinpath(*_HOLDINGS_SUBDIR) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def render_risk_lines(risk: RiskContext | None) -> list[str]:
    """Markdown bullets for the C7 risk block — shared by the chat reply and
    the chat reply, with the shared renderers'
    shape exactly.

    Empty for ``risk=None`` or an all-empty :class:`RiskContext` — the common
    degrade case (a pre-migration DB, an empty weights cache, or a thin price
    history) MUST render byte-identical to the pre-C7 output — no header, no
    placeholder line."""
    if risk is None or risk.is_empty():
        return []
    lines: list[str] = []
    bits: list[str] = []
    if risk.risk_share_pct is not None:
        bits.append(f"{risk.risk_share_pct:.0f}% of book risk")
    if risk.corr_to_book is not None:
        bits.append(f"corr-to-book {risk.corr_to_book:+.2f}")
    if bits:
        lines.append(f"- Risk: {'; '.join(bits)}")
    if risk.crowding_cluster:
        lines.append(f"- Crowding: {risk.crowding_cluster}")
    if risk.top_factors:
        factors = ", ".join(f"{factor} {loading:.1f}" for factor, loading in risk.top_factors)
        lines.append(f"- Business factors: {factors}")
    if risk.factor_provenance:
        lines.append(f"- Business-factor evidence: {risk.factor_provenance}")
    if risk.common_drawdown:
        lines.append(f"- Common drawdown: {risk.common_drawdown}")
    if risk.event_scenarios:
        lines.append(f"- Event scenarios: {', '.join(risk.event_scenarios)}")
    return lines


def build_risk_context(
    ticker: str, db_path: Path | str | None, repo_root: Path
) -> RiskContext | None:
    """Assemble the C7 risk block for ``ticker`` (§ plan: "risk-aware
    /review"). Every sub-leg below is independently try/excepted into
    ``degraded_reasons`` — a broken/absent ``business_factor_exposures``
    table (pre-migration substrate: ``sqlite3.OperationalError``), an offline
    price cache, or a weights-cache miss each degrade THEIR OWN leg only, and
    never propagate to the others or raise out of this function.

    Returns ``None`` (leg absent — ``/review`` renders exactly as it did
    before this block existed) when EVERY leg above came back empty. This is
    the plan's explicit degrade test: an empty/absent
    ``business_factor_exposures`` table combined with an empty weights cache
    yields ``None`` here, and ``render_risk_lines(None)`` / the verdict
    prompt's risk block both render nothing.
    """
    ticker = ticker.upper()
    degraded: list[str] = []

    # --- leg 1+2: book risk share + corr-to-book (one build_book_risk call,
    # allocation.book_risk — the SAME assembly the next-dollar model and the
    # L7 risk-budget allocator share, so this never disagrees with the Risk
    # tab about "the book's risk") ---------------------------------------
    risk_share_pct: float | None = None
    corr_to_book: float | None = None
    weights: dict[str, float] = {}
    try:
        from portfolio_weights import read_materialized_weights

        weights = read_materialized_weights(repo_root)
        if not weights:
            degraded.append("materialized weights cache empty or unavailable")
    except Exception as exc:  # never let a cache-read glitch break /review
        log.debug({"event": "risk_context_weights_failed", "ticker": ticker, "error": str(exc)})
        degraded.append(f"weights cache read failed: {type(exc).__name__}")

    if weights:
        try:
            from allocation.book_risk import build_book_risk

            book = build_book_risk(repo_root, list(weights), weights)
            if book.hidden_reason is not None:
                degraded.append(f"book risk unavailable: {book.hidden_reason}")
            elif ticker not in book.risk_share:
                degraded.append(f"{ticker} not in the priced book-risk matrix")
            else:
                risk_share_pct = book.risk_share[ticker] * 100.0
                corr_to_book = book.corr_to_book.get(ticker)
        except Exception as exc:
            log.debug(
                {"event": "risk_context_book_risk_failed", "ticker": ticker, "error": str(exc)}
            )
            degraded.append(f"book-risk leg failed: {type(exc).__name__}")

    # --- leg 3: crowding-cluster membership (portfolio_correlation's
    # connected-components clusters — Portfolio Risk v2's crowding read) -----
    crowding_cluster: str | None = None
    if weights:
        try:
            from portfolio_correlation import build_holdings_correlation_from_disk

            corr_read = build_holdings_correlation_from_disk(repo_root, list(weights), weights)
            if corr_read is None:
                degraded.append("crowding-cluster read unavailable (thin/absent price history)")
            else:
                cluster = next((c for c in corr_read.clusters if ticker in c.tickers), None)
                if cluster is not None:
                    others = ", ".join(t for t in cluster.tickers if t != ticker)
                    crowding_cluster = (
                        f"co-moves with {others} (avg corr {cluster.avg_corr:.2f}, "
                        f"{cluster.combined_weight_pct:.0f}% combined weight)"
                    )
        except Exception as exc:
            log.debug(
                {"event": "risk_context_crowding_failed", "ticker": ticker, "error": str(exc)}
            )
            degraded.append(f"crowding-cluster leg failed: {type(exc).__name__}")

    # Same holdings/input admission as the Risk page; no raw-history fallback.
    top_factors: tuple[tuple[str, float], ...] = ()
    factor_provenance: str | None = None
    try:
        from risk_factors import book_factor_vector

        factors = book_factor_vector(db_path, repo_root)
        top_factors = tuple(
            sorted(factors.ticker_loadings.get(ticker, {}).items(), key=lambda pair: -pair[1])[:3]
        )
        factor_provenance = (
            f"{factors.availability}; coverage {factors.coverage_pct:.0f}%; "
            f"excluded {', '.join(factors.excluded_tickers) or 'none'}; "
            f"source {factors.source_as_of or 'unavailable'}; effective {factors.effective_as_of or 'unavailable'}; "
            f"input {factors.input_sha or 'unavailable'}; registry {factors.registry_version}"
        )
        if factors.freshness_reasons:
            degraded.extend(factors.freshness_reasons)
    except Exception as exc:
        degraded.append(f"business-factor leg failed: {type(exc).__name__}")

    common_drawdown: str | None = None
    try:
        from research.qualitative_stress import read_common_drawdown, review_summary

        common_drawdown = review_summary(read_common_drawdown(db_path, repo_root))
    except Exception as exc:
        common_drawdown = f"unavailable: {type(exc).__name__}; no qualitative rating admitted"

    # --- leg 5: event-scenario membership (C5, src.portfolio_montecarlo) ----
    event_scenarios: tuple[str, ...] = ()
    try:
        from portfolio_montecarlo import EVENT_SCENARIOS

        event_scenarios = tuple(s.id for s in EVENT_SCENARIOS if ticker in s.named_tickers)
    except Exception as exc:
        log.debug({"event": "risk_context_scenarios_failed", "ticker": ticker, "error": str(exc)})
        degraded.append(f"event-scenario leg failed: {type(exc).__name__}")

    risk = RiskContext(
        risk_share_pct=risk_share_pct,
        corr_to_book=corr_to_book,
        crowding_cluster=crowding_cluster,
        top_factors=top_factors,
        factor_provenance=factor_provenance,
        common_drawdown=common_drawdown,
        event_scenarios=event_scenarios,
        degraded_reasons=tuple(degraded),
    )
    return None if risk.is_empty() else risk


def build_pre_analysis(
    repo_root: Path,
    ticker: str,
    *,
    at_price: float | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    db_path: Path | str | None = None,
) -> PreAnalysis:
    """Fetch every input and assemble the :class:`PreAnalysis` for ``ticker``.

    Degrades gracefully: an offline tracker falls back to the materialized
    weight cache; a name with no holdings JSON (an unencoded conviction like
    FLKR) yields ``thesis_present=False`` / ``break_rule_status="no_thesis"``
    rather than raising. Heavy modules are imported here, not at module top.
    """
    from integrations.portfolio_tracker_client import fetch_live_portfolio
    from portfolio_weights import read_materialized_weights

    db_path = require_product_db_path(
        _PROJECT_ROOT, explicit=Path(db_path) if db_path is not None else None
    )
    ticker = ticker.upper()
    holdings_dir = repo_root.joinpath(*_HOLDINGS_SUBDIR)
    holdings_json = _load_holdings_json(repo_root, ticker)

    # --- weight + dollars: prefer the live tracker, fall back to the cache ---
    weight_pct: float | None = None
    weight_source = "unknown"
    market_value_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    live = fetch_live_portfolio(api_url=api_url)
    pos = None
    if live.available:
        pos = next((p for p in live.positions if (p.ticker or "").upper() == ticker), None)
        if pos is not None:
            weight_pct = pos.percent_of_portfolio
            weight_source = "live"
            market_value_usd = pos.market_value
            unrealized_pnl_usd = pos.unrealized_pnl
    if weight_pct is None:
        frac = read_materialized_weights(repo_root).get(ticker)
        if frac is not None:
            weight_pct = frac * 100.0
            weight_source = "materialized"

    # --- stated sizing posture (empty in prod today) -------------------------
    from user_state.sizing import latest_intent

    target_row = latest_intent(
        user_id=user_id, ticker=ticker, intent_kind="target_weight_pct", db_path=db_path
    )
    conv_row = latest_intent(
        user_id=user_id, ticker=ticker, intent_kind="conviction", db_path=db_path
    )
    target_weight_pct = target_row.intent_value if target_row is not None else None
    conviction_1_5 = (
        int(conv_row.intent_value)
        if conv_row is not None and conv_row.intent_value is not None
        else None
    )

    # --- convictions on file -------------------------------------------------
    from user_state.notes import list_notes

    has_decision_note = bool(list_notes(ticker=ticker, kind="decision", db_path=db_path))

    # --- conn-backed reads: break-rules, DCF, instrument kind ----------------
    from compute.thesis_evaluator import evaluate_ticker_thesis
    from instrument_store import get_instrument_kind
    from models.companies import InstrumentType
    from pipeline.research_cockpit import latest_dcf_runs
    from user_state._db import open_conn

    conn = open_conn(db_path)
    try:
        verdict: ThesisVerdict | None
        try:
            verdict = evaluate_ticker_thesis(conn, ticker=ticker, holdings_dir=holdings_dir)
        except FileNotFoundError:
            verdict = None  # no holdings JSON — the FLKR case
        dcf_tuple = latest_dcf_runs(conn).get(ticker)
        instrument_kind = get_instrument_kind(conn, ticker)
    finally:
        conn.close()

    break_rule_status, tripped_rules = summarize_verdict(verdict)
    is_index_instrument = instrument_kind is InstrumentType.ETF

    # --- transaction history: fetched ONCE (only when the tracker holds a
    # live position — otherwise there is no tax leg to price either) and
    # reused for BOTH the entry-method zone classification below AND the
    # FIFO tax-lot reconstruction further down — never a second network
    # round-trip over the same window (PRD §7.2, P0.2 seam).
    history: list[LiveTransaction] | None = None
    entry_method: str | None = None
    if pos is not None:
        from integrations.portfolio_tracker_client import fetch_transaction_history

        history = fetch_transaction_history(api_url=api_url)
        if history is not None:
            buy_dates = [
                t.date
                for t in history
                if (t.ticker or "").strip().upper() == ticker
                and (
                    "buy" in (t.type or "").strip().lower()
                    or "buy" in (t.subtype or "").strip().lower()
                )
            ]
            entry_method = classify_entry_method(buy_dates, as_of=date.today())

    pre = assemble_pre_analysis(
        ticker,
        weight_pct=weight_pct,
        weight_source=weight_source,
        market_value_usd=market_value_usd,
        unrealized_pnl_usd=unrealized_pnl_usd,
        target_weight_pct=target_weight_pct,
        conviction_1_5=conviction_1_5,
        break_rule_status=break_rule_status,
        tripped_rules=tripped_rules,
        holdings_json=holdings_json,
        dcf_tuple=dcf_tuple,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        at_price=at_price,
        entry_method=entry_method,
    )

    # --- tax view: lots from the SAME tracker history fetched above, honest
    # degrade otherwise ---------------------------------------------------
    from advisor.position_tax import (
        build_position_tax_view,
        load_tax_profile,
        propose_trim_fraction,
        unavailable_tax_view,
    )

    if pos is None:
        # live.error carries a full urllib3 pool dump — keep only the exception
        # type so the chat line stays one line.
        error_kind = (live.error or "unreachable").split(":", 1)[0]
        reason = (
            f"tracker offline ({error_kind})"
            if not live.available
            else "ticker not held in the live book"
        )
        tax_view = unavailable_tax_view(reason)
    else:
        from integrations.portfolio_tracker_client import TRANSACTION_HISTORY_LIMIT

        trim_fraction, trim_rationale = propose_trim_fraction(
            weight_pct=pre.weight_pct,
            target_band=pre.target_band,
            weight_vs_band=pre.weight_vs_band,
            zone=pre.concentration_zone,
        )
        tax_view = build_position_tax_view(
            pos,
            history,
            profile=load_tax_profile(repo_root),
            at_price=at_price,
            trim_fraction=trim_fraction,
            trim_rationale=trim_rationale,
            history_truncated=history is not None and len(history) >= TRANSACTION_HISTORY_LIMIT,
        )
    # --- risk context (C7): a separate I/O leg, wrapped defensively even
    # though build_risk_context already guards every sub-leg internally —
    # matching this function's paranoid style for every cross-cutting block
    # so a bug in
    # a NEW leg can never break /review.
    try:
        risk_ctx = build_risk_context(ticker, db_path, repo_root)
    except Exception as exc:
        log.debug({"event": "risk_context_build_failed", "ticker": ticker, "error": str(exc)})
        risk_ctx = None

    return replace(pre, tax=tax_view, risk=risk_ctx)


# --------------------------------------------------------------------------- #
# Tax and chat rendering
# --------------------------------------------------------------------------- #


def _tax_cost_phrase(tax: PositionTaxView | None) -> str | None:
    """Compact dollar phrase for the proposed trim's tax cost — ``~$6,200`` or
    ``~$1,200-$2,000 (term unknown)`` — or None when there is nothing to say
    (no view, no trim estimate, or a zero/negative bill)."""
    if tax is None or not tax.available or tax.trim is None:
        return None
    trim = tax.trim
    if trim.tax_high_usd <= 0:
        return None
    if abs(trim.tax_high_usd - trim.tax_low_usd) < 0.5:
        return f"~${trim.tax_low_usd:,.0f}"
    return f"~${trim.tax_low_usd:,.0f}-${trim.tax_high_usd:,.0f} (term unknown)"


def render_tax_lines(tax: PositionTaxView | None) -> list[str]:
    """Markdown bullets for the tax block — shared by the chat reply, the CLI
    summary, and the persisted memo body. Empty for a pre-tax-stage
    :class:`PreAnalysis` (``tax=None``); a single honest line when the tracker
    couldn't supply a view; the dense block otherwise.

    Web chat, the CLI and persisted memos use the same Markdown block.
    """
    if tax is None:
        return []
    if not tax.available:
        return [f"- Tax: unavailable ({tax.reason})"]
    approx = " (approx)" if tax.approximate else ""
    pct = f"{tax.taxable_pct_of_position:.0f}%" if tax.taxable_pct_of_position is not None else "—"
    if tax.st_unrealized_usd is not None and tax.lt_unrealized_usd is not None:
        embedded = (
            f"embedded gain ST ${tax.st_unrealized_usd:,.0f} / LT ${tax.lt_unrealized_usd:,.0f}"
        )
    else:
        embedded = "term split unknown"
    lines = [f"- Tax{approx}: taxable {pct} of position · {embedded}"]
    trim = tax.trim
    if trim is not None:
        cost = _tax_cost_phrase(tax) or "$0"
        seg = f"- Proposed trim ({trim.trim_rationale}): ~${trim.trim_usd:,.0f} → est. tax {cost}"
        if (
            trim.days_until_lt is not None
            and trim.wait_savings_usd is not None
            and trim.wait_savings_usd > 0
        ):
            seg += f" · waiting {trim.days_until_lt}d (ST→LT) saves ~${trim.wait_savings_usd:,.0f}"
        if trim.wash_sale_risk:
            seg += " · wash-sale risk (buy within 30d of a loss lot)"
        lines.append(seg)
    if tax.sheltered_note:
        lines.append(f"- Placement: {tax.sheltered_note}")
    if tax.approximate and tax.approx_reasons:
        lines.append(f"- Tax approx because: {'; '.join(tax.approx_reasons[:2])}")
    if tax.footnote:
        lines.append(f"- _{tax.footnote}_")
    return lines


def _render_zone(pre: PreAnalysis) -> str:
    """The concentration-zone read for the /review renders (PRD §7.2, P0.2) —
    e.g. ``"concentrated (13.4%)"``. ``"n/a"`` when no weight is known."""
    if pre.concentration_zone and pre.weight_pct is not None:
        return f"{pre.concentration_zone} ({pre.weight_pct:.1f}%)"
    return pre.concentration_zone or "n/a"


def render_pre_analysis_chat(pre: PreAnalysis) -> str:
    """Render position facts, tax context and portfolio risk without coaching."""
    wt = f"{pre.weight_pct:.1f}%" if pre.weight_pct is not None else "—"
    conc = _render_zone(pre)
    fv = f"${pre.npv_per_share:,.2f}" if pre.npv_per_share is not None else "—"
    asked = f"${pre.at_price:,.2f}" if pre.at_price is not None else "—"
    gap = f"{pre.dcf_gap_pct:+.1f}%" if pre.dcf_gap_pct is not None else "—"
    watch = f" · watch: {'; '.join(pre.tripped_rules)}" if pre.tripped_rules else ""
    return "\n".join(
        [
            f"**{pre.ticker}** — position review (deterministic read; weight source: "
            f"{pre.weight_source})",
            f"- Sizing: {wt} of book · concentration: {conc} · band: {pre.weight_vs_band}",
            f"- Fundamentals: thesis {pre.verdict_label or '—'} · "
            f"break-rules {pre.break_rule_status}{watch}",
            f"- Valuation: fair {fv} vs asked {asked} → {gap} (+ = over-valued) · "
            f"ladder: {pre.valuation_verdict}",
            *render_tax_lines(pre.tax),
            *render_risk_lines(pre.risk),
        ]
    )


def _resolve_review_target(body: str, roster: RosterIndex | None) -> str:
    """Resolve the free-text name/symbol after ``/review`` to a canonical ticker.

    Prefers a roster name/alias match — ``Rubrik`` → ``RBRK``, ``Nubank`` →
    ``NU``, multi-word ``Novo Nordisk`` → ``NVO`` — via the same deterministic
    ``capture.matcher`` lanes the capture pipeline uses (so the two never drift).
    Falls back to the first token treated as a literal symbol (uppercased,
    leading ``$`` stripped, then canonicalized through ``capture.matcher``'s
    index builder for symbol aliases like ``GOOGL`` → ``GOOG``) when the roster
    has no name match — so a typed ticker still resolves and an un-rostered name
    still reviews as typed.
    """
    from capture.matcher import build_roster_index, match_ticker

    if roster is not None:
        matched = match_ticker(body, roster).ticker
        if matched is not None:
            return matched
    # Fall back to the first token as a literal symbol, canonicalized (GOOGL ->
    # GOOG) through the same typed index builder the roster uses — this reuses
    # capture.matcher's symbol-alias boundary instead of touching the untyped
    # alias_manager directly.
    tokens = body.split()
    raw = (tokens[0] if tokens else body).upper().lstrip("$")
    canonical = build_roster_index(symbols=[raw]).symbol_to_ticker
    return next(iter(canonical.values()), raw)


def parse_review_command(
    text: str, *, roster: RosterIndex | None = None
) -> tuple[str, float | None] | None:
    """Parse a name or ticker and optional price using the supplied roster."""
    parts = text.split()
    if len(parts) < 2:
        return None
    match = _AT_PRICE_RX.search(text)
    at_price = float(match.group(1)) if match else None
    # The name/symbol portion is everything after the command word, minus the
    # trailing "at $X" clause — so a multi-word name ("Novo Nordisk") survives
    # while the price level is still stripped out before resolution.
    body = _AT_PRICE_RX.sub("", text.split(None, 1)[1]).strip()
    ticker = _resolve_review_target(body, roster)
    return ticker, at_price


def _load_review_roster(db_path: Path) -> RosterIndex | None:
    """Best-effort roster for ``/review`` name→ticker resolution; ``None`` on any
    failure (the parser then falls back to the token-as-symbol behavior). Loads
    ``tracked_companies`` merged with the distinctive-alias seed, so a name like
    ``Rubrik`` resolves even when the DB is absent (the seed carries it)."""
    try:
        from capture.matcher import load_roster

        return load_roster(db_path)
    except Exception:
        return None


def review_reply_text(repo_root: Path, text: str, *, db_path: Path | str | None = None) -> str:
    """Render position facts without generation or background work."""
    usage = "Usage: /review <NAME|TICKER> [at $PRICE] — e.g. `/review Rubrik at $70`."
    if len(text.split()) < 2:
        return usage
    try:
        database = require_product_db_path(
            _PROJECT_ROOT, explicit=Path(db_path) if db_path is not None else None
        )
        parsed = parse_review_command(text, roster=_load_review_roster(database))
        if parsed is None:
            return usage
        ticker, at_price = parsed
        pre = build_pre_analysis(repo_root, ticker, at_price=at_price, db_path=database)
    except Exception as exc:
        return f"Couldn't build position facts: {type(exc).__name__}"
    return render_pre_analysis_chat(pre)
