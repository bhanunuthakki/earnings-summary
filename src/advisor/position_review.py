"""Deterministic pre-analysis for the position-review service (Slice 1).

The position-review service answers "should I trim / hold / add <TICKER> at
these levels?" by composing five inputs the owner cares about — portfolio
weight, dollar value, company fundamentals + break-rules, valuation, and his
own convictions. This module is the *grounded, LLM-free* first stage: it fetches
those facts and packs them into a :class:`PreAnalysis` so the later reasoning
stage (Slice 2) argues over numbers, not vibes.

Design split (mirrors ``pipeline.allocation_decisions_panel``'s pure ``score_row``
vs I/O ``build_sizing_audit_rows``):

  * ``build_pre_analysis`` — the I/O orchestrator: reads the live tracker, the
    materialized weight cache, the thesis evaluator, the DCF runs, the sizing
    intents, the instrument registry, and the conviction stores.
  * ``assemble_pre_analysis`` + the ``classify_*`` / ``summarize_verdict``
    helpers — a PURE core over primitives, unit-testable with no DB or network.

Two determinations are pure Python and never touch an LLM:

  1. the **valuation verdict** — the ``dcf.valuation.over_under_pct`` trim/sell
     ladder, executed here against the holding's ``mos_bar`` (see
     ``classify_valuation``); and
  2. **weight-vs-band** — a target-weight tolerance band, falling back to a
     single-name concentration flag when no target is recorded (which is the
     prod reality today: ``position_sizing_intent`` is empty).

Heavy dependencies (the DCF cockpit, thesis evaluator, tracker client, stores)
are imported lazily inside ``build_pre_analysis`` so importing this module for
the pure helpers stays cheap and side-effect-free (``db_paths`` docstring: a
top-level ``db`` import triggers ``init_db``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from identity import DEFAULT_USER_ID

if TYPE_CHECKING:
    from compute.thesis_evaluator import ThesisVerdict

__all__ = [
    "BAND_TOLERANCE",
    "CONCENTRATION_PCT",
    "DEFAULT_MOS_BAR",
    "SELL_OVER_UNDER",
    "TRIM_OVER_UNDER",
    "PreAnalysis",
    "assemble_pre_analysis",
    "build_pre_analysis",
    "classify_valuation",
    "classify_weight_band",
    "summarize_verdict",
]

# --------------------------------------------------------------------------- #
# Tunables — the valuation ladder mirrors the ``dcf.valuation.over_under_pct``
# docstring EXACTLY (>0.10 trim, >0.20 sell, < -mos_bar initiation). The guard
# test asserts these against that function so the two can never drift.
# --------------------------------------------------------------------------- #
TRIM_OVER_UNDER = 0.10  # over_under (decimal) above which the ladder says "trim"
SELL_OVER_UNDER = 0.20  # ... "sell"
DEFAULT_MOS_BAR = 0.30  # fallback initiation bar when a holding JSON omits mos_bar
BAND_TOLERANCE = 0.20  # +/- 20% tolerance band around a stated target weight
CONCENTRATION_PCT = 8.0  # single-name weight (%) at/above which we flag concentration

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
    concentration_flag: bool  # single-name weight >= CONCENTRATION_PCT
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
    # --- convictions / instrument -------------------------------------------
    conviction_encoded: bool  # thesis_present AND (a current stance OR a decision note)
    has_stance: bool
    has_decision_note: bool
    is_index_instrument: bool  # ETF — a macro/factor sleeve, not a single-name thesis


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
    has_stance: bool,
    has_decision_note: bool,
    is_index_instrument: bool,
    at_price: float | None,
) -> PreAnalysis:
    """Compose primitives into a :class:`PreAnalysis`. Pure — no I/O.

    ``dcf_tuple`` is ``(fv_gap_pct, npv_per_share, live_price, valuation_date)``
    as returned by ``research_cockpit.latest_dcf_runs``. When ``at_price`` is
    supplied AND a positive NPV is on file, the over/under gap is RECOMPUTED at
    that level so "above $70" is answered against $70, not the stored close.
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
    concentration_flag = weight_pct is not None and weight_pct >= CONCENTRATION_PCT
    conviction_encoded = thesis_present and (has_stance or has_decision_note)

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
        conviction_encoded=conviction_encoded,
        has_stance=has_stance,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
    )


# --------------------------------------------------------------------------- #
# I/O orchestrator
# --------------------------------------------------------------------------- #


def _load_holdings_json(repo_root: Path, ticker: str) -> dict[str, object] | None:
    """Read ``micro_thesis/holdings/<TICKER>.json`` defensively; None on any
    read/parse failure or a non-object payload (so an unencoded name — FLKR —
    degrades cleanly to ``thesis_present=False``)."""
    import json

    path = repo_root.joinpath(*_HOLDINGS_SUBDIR) / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


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

    ticker = ticker.upper()
    holdings_dir = repo_root.joinpath(*_HOLDINGS_SUBDIR)
    holdings_json = _load_holdings_json(repo_root, ticker)

    # --- weight + dollars: prefer the live tracker, fall back to the cache ---
    weight_pct: float | None = None
    weight_source = "unknown"
    market_value_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    live = fetch_live_portfolio(api_url=api_url)
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
    from synthesis.insights import list_insights
    from user_state.notes import list_notes

    has_stance = bool(
        list_insights(kind="stance", scope_key=ticker, status="current", db_path=db_path)
    )
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

    return assemble_pre_analysis(
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
        has_stance=has_stance,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        at_price=at_price,
    )
