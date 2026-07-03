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

import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from advisor.store import STANCES
from identity import DEFAULT_USER_ID

if TYPE_CHECKING:
    from advisor.position_tax import PositionTaxView
    from compute.thesis_evaluator import ThesisVerdict

__all__ = [
    "BAND_TOLERANCE",
    "CONCENTRATION_PCT",
    "CONFIDENCES",
    "DEFAULT_MOS_BAR",
    "POSITION_REVIEW_PURPOSE",
    "SELL_OVER_UNDER",
    "SUGGESTED_EXPRESSIONS",
    "TRIM_OVER_UNDER",
    "PositionReview",
    "PreAnalysis",
    "VerdictOutput",
    "apply_behavioral_guard",
    "assemble_pre_analysis",
    "build_pre_analysis",
    "classify_valuation",
    "classify_weight_band",
    "encode_first_output",
    "graded_sell_record",
    "mechanical_read",
    "parse_review_command",
    "parse_verdict_output",
    "render_pre_analysis_chat",
    "render_pre_analysis_plain",
    "render_tax_lines",
    "review_position",
    "review_reply_text",
    "summarize_verdict",
]

# "at $70" / "above 70" / "over $12.50" -> the price level to review at. Shared
# by every chat-shaped surface that parses "/review <TICKER> [at $X]" (the Ask
# tab command and the Telegram poller) so the at-price behavior never drifts.
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
    # --- tax (deterministic, advisor.position_tax) ---------------------------
    # None only on PreAnalysis objects built before the tax stage (tests, old
    # callers); build_pre_analysis always attaches a view, degraded or not.
    tax: PositionTaxView | None = None


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
        has_stance=has_stance,
        has_decision_note=has_decision_note,
        is_index_instrument=is_index_instrument,
        at_price=at_price,
    )

    # --- tax view: lots from the tracker feed, honest degrade otherwise ------
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
        from integrations.portfolio_tracker_client import (
            TRANSACTION_HISTORY_LIMIT,
            fetch_transaction_history,
        )

        trim_fraction, trim_rationale = propose_trim_fraction(
            weight_pct=pre.weight_pct,
            target_band=pre.target_band,
            weight_vs_band=pre.weight_vs_band,
            concentration_flag=pre.concentration_flag,
            concentration_pct=CONCENTRATION_PCT,
        )
        history = fetch_transaction_history(api_url=api_url)
        tax_view = build_position_tax_view(
            pos,
            history,
            profile=load_tax_profile(repo_root),
            at_price=at_price,
            trim_fraction=trim_fraction,
            trim_rationale=trim_rationale,
            history_truncated=history is not None and len(history) >= TRANSACTION_HISTORY_LIMIT,
        )
    return replace(pre, tax=tax_view)


# --------------------------------------------------------------------------- #
# Slice 2 — governed LLM verdict + deterministic behavioral guardrail
# --------------------------------------------------------------------------- #

POSITION_REVIEW_PURPOSE = "position_review"
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
SUGGESTED_EXPRESSIONS: tuple[str, ...] = (
    "trim-to-target",
    "LEAP-overlay",
    "do-nothing",
    "add-tranche",
    "encode-thesis-first",
)
_VERDICT_KEYS: tuple[str, ...] = (
    "verdict",
    "size",
    "reason",
    "confidence",
    "behavioral_check",
    "suggested_expression",
)

# Fallback when no graded sells/trims exist yet (or the query fails) — the
# guard/prompt must never fabricate a ticker list.
_GENERIC_SELL_PATTERN_LINE = "your sell-winners-too-early pattern"


def graded_sell_record(db_path: Path | str | None) -> str | None:
    """The owner's live graded record on sell/trim recommendations, as one line.

    Reads ``decisions`` for his own rows (``decided_by = 'owner'``) recommending
    a sell or trim that have since been graded (``outcome_label != 'pending'``),
    and renders e.g. ``"Graded record on sells/trims: 5 of 8 wrong (MU, TSM,
    NVDA, AMZN, GOOGL)"`` — the count of ``wrong``-graded calls over the total
    graded, followed by the DISTINCT wrong-graded tickers (alphabetical).

    Returns ``None`` when there is nothing graded yet (a fresh DB, or every
    sell/trim still ``pending``) — the caller omits the line rather than
    printing a hollow "0 of 0". Also ``None``/degraded on any DB error (missing
    file, pre-``decisions`` schema): never fabricate a count or ticker list.
    """
    if db_path is None or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN outcome_label = 'wrong' THEN 1 ELSE 0 END) "
            "FROM decisions WHERE decided_by = 'owner' "
            "AND recommendation_kind IN ('sell', 'trim') AND outcome_label != 'pending'"
        ).fetchone()
        if row is None or not row[0]:
            return None
        total, wrong = int(row[0]), int(row[1] or 0)
        tickers = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT ticker FROM decisions WHERE decided_by = 'owner' "
                "AND recommendation_kind IN ('sell', 'trim') AND outcome_label = 'wrong' "
                "AND ticker IS NOT NULL ORDER BY ticker"
            ).fetchall()
        ]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    names = f" ({', '.join(tickers)})" if tickers else ""
    return f"Graded record on sells/trims: {wrong} of {total} wrong{names}"


def _sell_pattern_phrase(graded_line: str | None) -> str:
    """The parenthetical ticker clause for the guard/prompt copy — the derived
    ``graded_sell_record`` line's ticker list when available, else the generic
    phrase (never a hardcoded name list)."""
    if graded_line is None:
        return _GENERIC_SELL_PATTERN_LINE
    match = re.search(r"\(([^)]+)\)\s*$", graded_line)
    return f"sell-winners-too-early pattern ({match.group(1)})" if match else graded_line


def _behavioral_rules(graded_line: str | None) -> str:
    """The owner's documented decision-making, baked into the prompt as hard
    constraints (from data/ledger_seed/seed.json), with rule 1's evidence
    interpolated from the LIVE graded record rather than a stale hardcode.
    The deterministic guardrail (:func:`apply_behavioral_guard`) is the backstop
    that ENFORCES rule 1 regardless of what the model does."""
    sell_evidence = (
        f"his live graded record: {graded_line}"
        if graded_line is not None
        else "his self-diagnosed sell-winners-too-early flaw"
    )
    return f"""\
Calibrate the verdict to THIS owner's documented decision-making:
1. SELL-WINNERS-TOO-EARLY is his dominant flaw — {sell_evidence}. On an INTACT,
   high-conviction name, "it has run" or "it looks expensive on the tape" is NEVER a
   sufficient reason to trim. The ONLY price-agnostic reason to trim a healthy name is
   SIZING: weight above its target band, or an oversized single-name concentration.
2. LEAP OVERLAY is his prescribed antidote — when he wants less capital at risk on a name he
   still believes in, prefer keep-the-core + a far-OTM long-dated LEAP over an outright trim
   (suggested_expression="LEAP-overlay").
3. CATALYST TEST for adds — only add into weakness when there is a near-term catalyst AND a
   priced-in floor; cheap-without-catalyst is a value trap.
4. INSTRUMENT-SELECTION — express macro/sector themes via an index; hold dry powder in
   T-bills/SGOV; express single-name conviction via LEAPs.
5. KEEP THE VERDICT WITH THE FRAMEWORK, NOT THE FEELING — ground the call in the break-rule
   status and the DCF valuation ladder. If the framework is intact, the name is not
   overvalued, and it is not oversized, the answer is HOLD even if the position feels
   uncomfortable after a big run.
"""


@dataclass(frozen=True)
class VerdictOutput:
    """The reasoned trim/hold/add recommendation. ``verdict`` is validated
    against ``advisor.store.STANCES`` so it persists as a P2.5-scoreable stance.
    ``verdict_source`` records whether the deterministic behavioral guard
    overrode the model ("guard_override") or the name was unencoded
    ("encode_first")."""

    verdict: str  # in STANCES: buy | add | hold | trim | sell
    size: str
    reason: str
    confidence: str  # in CONFIDENCES
    behavioral_check: str
    suggested_expression: str  # in SUGGESTED_EXPRESSIONS
    verdict_source: str = "llm"  # "llm" | "guard_override" | "encode_first"


@dataclass(frozen=True)
class PositionReview:
    """A full review: the grounded pre-analysis, the verdict, and the persisted
    memo id (None when persistence was skipped or no DB was configured)."""

    pre: PreAnalysis
    output: VerdictOutput
    memo_id: int | None


def _req_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"position_review output field {key!r} must be a non-empty string")
    return value.strip()


def _coerce_choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    """Normalize a soft enum field to a member of ``allowed`` (default on miss).
    Softer than ``verdict`` — a bad confidence/expression degrades to a safe
    default rather than raising, because it never changes the stance itself."""
    norm = value.strip() if isinstance(value, str) else ""
    return norm if norm in allowed else default


def parse_verdict_output(payload: dict[str, object]) -> VerdictOutput:
    """Validate the LLM's JSON into a :class:`VerdictOutput`.

    ``verdict`` MUST be in ``STANCES`` (raises otherwise — an out-of-vocabulary
    stance isn't scoreable and must fail loudly, not silently degrade). The soft
    fields coerce to safe defaults.
    """
    verdict = _req_str(payload, "verdict").lower()
    if verdict not in STANCES:
        raise ValueError(f"position_review verdict {verdict!r} not in {STANCES}")
    return VerdictOutput(
        verdict=verdict,
        size=_req_str(payload, "size"),
        reason=_req_str(payload, "reason"),
        confidence=_coerce_choice(payload.get("confidence"), CONFIDENCES, "low"),
        behavioral_check=_req_str(payload, "behavioral_check"),
        suggested_expression=_coerce_choice(
            payload.get("suggested_expression"), SUGGESTED_EXPRESSIONS, "do-nothing"
        ),
    )


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


def apply_behavioral_guard(
    pre: PreAnalysis, out: VerdictOutput, *, graded_line: str | None = None
) -> VerdictOutput:
    """Enforce the sell-winners-too-early rule deterministically.

    A ``trim``/``sell`` on a thesis that is intact-or-warn, NOT over-valued, and
    NOT oversized (not above its band, and not a flagged single-name
    concentration) is a price-only trim on a healthy name — the owner's signature
    mistake. Override it to ``hold`` and say why. Legitimate trims survive
    untouched: a breached thesis, an over-valued name (ladder trim/sell), an
    above-band weight, or an oversized concentration all justify trimming.

    ``graded_line`` is the caller-supplied output of :func:`graded_sell_record`
    (built ONCE per review, not re-queried here) — the override text names the
    live wrong-graded tickers when available and falls back to a generic
    "sell-winners-too-early pattern" phrase (no hardcoded names) otherwise.

    When a tax view is on the pre-analysis, an override cites the trim's tax
    cost as supporting rationale — the tax never *drives* the verdict (that
    stays framework-only), it corroborates the hold.
    """
    if out.verdict not in ("trim", "sell"):
        return out
    thesis_healthy = pre.break_rule_status in ("intact", "warn")
    not_overvalued = pre.valuation_verdict in ("fair", "buy", "n/a")
    sizing_justified = pre.weight_vs_band == "above_band" or (
        pre.weight_vs_band == "no_band" and pre.concentration_flag
    )
    if thesis_healthy and not_overvalued and not sizing_justified:
        cost = _tax_cost_phrase(pre.tax)
        tax_support = f" Trimming now would also cost {cost} in taxes." if cost else ""
        pattern = _sell_pattern_phrase(graded_line)
        return replace(
            out,
            verdict="hold",
            reason=f"[behavioral guard] Overrode a price-only {out.verdict} on an intact, "
            f"non-oversized, non-overvalued name.{tax_support} {out.reason}",
            behavioral_check=(
                f"This is the {pattern}: trimming a healthy, non-oversized name that the "
                "framework says is not expensive. Hold the core; if you want less capital "
                f"at risk, use a LEAP overlay. {out.behavioral_check}"
            ),
            suggested_expression="do-nothing",
            verdict_source="guard_override",
        )
    return out


def encode_first_output(pre: PreAnalysis) -> VerdictOutput:
    """Deterministic degraded verdict for a name with no encoded thesis (FLKR):
    there is no framework to ground a call, so recommend encoding one rather than
    fabricating a fundamentals verdict."""
    index_note = (
        "This is an index instrument — a macro/factor sleeve, not a single-name thesis. "
        if pre.is_index_instrument
        else ""
    )
    return VerdictOutput(
        verdict="hold",
        size="none",
        reason=(
            f"No encoded thesis for {pre.ticker} (no holdings JSON, no DCF, no break-rules), "
            f"so there is no framework to ground a trim/add call. {index_note}"
            "Your own note tags it low-conviction."
        ),
        confidence="low",
        behavioral_check=(
            "Can't check this against your framework because the conviction isn't encoded. "
            "If you hold it on a real single-name thesis, encode it so the next review is "
            "grounded — and don't trim on price alone (the sell-winners-too-early reflex)."
        ),
        suggested_expression="encode-thesis-first",
        verdict_source="encode_first",
    )


def _convictions_block(repo_root: Path, ticker: str, db_path: Path | str | None) -> str:
    """Assemble the owner's captured convictions for ``ticker`` (synthesized
    stance + recent decisions/musings), spotlighted as untrusted input before it
    reaches the prompt (stance text chains LLM-synthesized content)."""
    from llm.untrusted import spotlight
    from synthesis.insights import list_insights
    from user_state.notes import list_notes

    parts: list[str] = []
    stances = list_insights(kind="stance", scope_key=ticker, status="current", db_path=db_path)
    if stances:
        parts.append(f"STANCE (synthesized): {stances[0].body_md}")
    for note in list_notes(ticker=ticker, kind="decision", db_path=db_path)[:3]:
        tail = ""
        if note.context:
            conv = note.context.get("conviction")
            fals = note.context.get("falsifier")
            bits = [
                b
                for b in (
                    f"conviction={conv}" if conv else "",
                    f"falsifier: {fals}" if fals else "",
                )
                if b
            ]
            tail = f" [{'; '.join(bits)}]" if bits else ""
        parts.append(f"DECISION: {note.body}{tail}")
    for note in list_notes(ticker=ticker, kind="musing", db_path=db_path)[:5]:
        parts.append(f"MUSING: {note.body}")
    raw = "\n\n".join(parts)
    return spotlight(raw, source="the owner's own captured convictions (the Ledger)")


def _build_verdict_prompt(
    pre: PreAnalysis, convictions_block: str, *, graded_line: str | None = None
) -> str:
    facts = (
        f"Ticker: {pre.ticker}\n"
        f"Weight: {pre.weight_pct}% of book (source: {pre.weight_source}); "
        f"market value {pre.market_value_usd}; unrealized P&L {pre.unrealized_pnl_usd}\n"
        f"Target band: {pre.target_band} -> {pre.weight_vs_band}; "
        f"concentration_flag={pre.concentration_flag} (single name >= {CONCENTRATION_PCT}%)\n"
        f"Break-rule status: {pre.break_rule_status}; "
        f"tripped/watch: {list(pre.tripped_rules) or 'none'}\n"
        f"Thesis verdict label: {pre.verdict_label}; key driver: {pre.key_driver}\n"
        f"Valuation: DCF fair value {pre.npv_per_share}, asked-at price {pre.at_price}, "
        f"over/under {pre.dcf_gap_pct}% (+ = over-valued), mos_bar {pre.mos_bar}; "
        f"ladder verdict: {pre.valuation_verdict}\n"
        f"Conviction encoded: {pre.conviction_encoded}\n"
    )
    return (
        "You are the owner's position-review analyst. Using the GROUNDED FACTS "
        "(deterministic, computed from the platform) and the owner's OWN convictions, "
        "return a single trim/hold/add/sell verdict for this ONE position.\n\n"
        f"{_behavioral_rules(graded_line)}\n"
        f"## GROUNDED FACTS\n{facts}\n"
        f"## THE OWNER'S CONVICTIONS ON THIS NAME\n{convictions_block or '(none on file)'}\n\n"
        "## OUTPUT — return ONLY a JSON object with exactly these keys:\n"
        f'  "verdict": one of {list(STANCES)} (his exact vocabulary)\n'
        '  "size": short phrase, e.g. "trim ~2pp to top of band" or "none"\n'
        '  "reason": the SPECIFIC driver — which break-rule/threshold, which sizing band, '
        "or which DCF ladder step\n"
        f'  "confidence": one of {list(CONFIDENCES)}\n'
        '  "behavioral_check": one sentence naming which of HIS documented patterns this '
        "decision risks repeating\n"
        f'  "suggested_expression": one of {list(SUGGESTED_EXPRESSIONS)}\n'
        "No markdown fences, no prose outside the JSON object.\n"
    )


def _render_memo_body(pre: PreAnalysis, out: VerdictOutput) -> str:
    lines = [
        f"**{pre.ticker}** — position-review verdict: **{out.verdict}** "
        f"({out.confidence} confidence)",
        "",
        f"- Size: {out.size}",
        f"- Reason: {out.reason}",
        f"- Suggested expression: {out.suggested_expression}",
        f"- Behavioral check: {out.behavioral_check}",
        "",
        f"Grounded facts — weight {pre.weight_pct}% ({pre.weight_source}); "
        f"break-rules {pre.break_rule_status}; valuation {pre.valuation_verdict} "
        f"(over/under {pre.dcf_gap_pct}%); concentration_flag={pre.concentration_flag}.",
    ]
    # The tax block persists with the memo whichever way the verdict went, so
    # P2.5 grading sees the cost the decision was made against.
    tax_lines = render_tax_lines(pre.tax)
    if tax_lines:
        lines.append("")
        lines.extend(tax_lines)
    if out.verdict_source != "llm":
        lines.append(f"\n_verdict source: {out.verdict_source}_")
    lines.append(f"\nSTANCE: {out.verdict}")
    return "\n".join(lines)


def _tax_context(tax: PositionTaxView | None) -> dict[str, object] | None:
    """JSON-safe tax summary for the memo ``context`` (grading + audits)."""
    if tax is None or not tax.available:
        return None
    trim = tax.trim
    out: dict[str, object] = {
        "approximate": tax.approximate,
        "taxable_pct_of_position": tax.taxable_pct_of_position,
        "st_unrealized_usd": tax.st_unrealized_usd,
        "lt_unrealized_usd": tax.lt_unrealized_usd,
    }
    if trim is not None:
        out.update(
            trim_rationale=trim.trim_rationale,
            trim_usd=trim.trim_usd,
            est_tax_low_usd=trim.tax_low_usd,
            est_tax_high_usd=trim.tax_high_usd,
            days_until_lt=trim.days_until_lt,
            wait_savings_usd=trim.wait_savings_usd,
            wash_sale_risk=trim.wash_sale_risk,
        )
    return out


def _persist_review(
    pre: PreAnalysis, out: VerdictOutput, *, user_id: str, db_path: Path | str | None
) -> int | None:
    from advisor.memos import DEFAULT_HORIZON_DAYS, persist_memo
    from db_paths import resolve_db_path

    resolved = resolve_db_path(db_path)
    if resolved is None:
        return None
    context: dict[str, object] = {
        "weight_pct": pre.weight_pct,
        "dcf_gap_pct": pre.dcf_gap_pct,
        "break_rule_status": pre.break_rule_status,
        "valuation_verdict": pre.valuation_verdict,
        "confidence": out.confidence,
        "suggested_expression": out.suggested_expression,
        "verdict_source": out.verdict_source,
        "at_price": pre.at_price,
    }
    tax_context = _tax_context(pre.tax)
    if tax_context is not None:
        context["tax"] = tax_context
    result = persist_memo(
        db_path=resolved,
        user_id=user_id,
        kind=POSITION_REVIEW_PURPOSE,
        ticker=pre.ticker,
        counter_ticker=None,
        title=f"Position review: {pre.ticker} -> {out.verdict}",
        body_md=_render_memo_body(pre, out),
        context=context,
        write_ledger=True,
        stance=out.verdict,
        horizon_days=DEFAULT_HORIZON_DAYS,
    )
    return result.memo_id


def review_position(
    repo_root: Path,
    ticker: str,
    *,
    at_price: float | None = None,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
    db_path: Path | str | None = None,
    persist: bool = True,
) -> PositionReview:
    """End-to-end position review: grounded pre-analysis -> governed LLM verdict
    -> deterministic behavioral guard -> persisted memo.

    A name with no encoded thesis short-circuits to a deterministic low-confidence
    "encode a thesis first" verdict WITHOUT an LLM call — there is nothing to
    ground a fundamentals call on. Otherwise the verdict is produced by the
    governed ``position_review`` LLM purpose (model-picked, cost-logged,
    schema-validated) and then passed through the behavioral guard.
    """
    pre = build_pre_analysis(
        repo_root, ticker, at_price=at_price, user_id=user_id, api_url=api_url, db_path=db_path
    )
    if not pre.thesis_present:
        out = encode_first_output(pre)
    else:
        from db_paths import resolve_db_path
        from llm.structured import call_llm_structured

        # Built ONCE per review and threaded into both the prompt and the guard
        # override, so the model's rationale and the deterministic backstop
        # always cite the same live count (never a stale hardcode, never a
        # query race between the two call sites).
        graded_line = graded_sell_record(resolve_db_path(db_path))
        prompt = _build_verdict_prompt(
            pre, _convictions_block(repo_root, pre.ticker, db_path), graded_line=graded_line
        )
        # db_path= keeps the call's DB-backed layers (llm_calls cost ledger,
        # budget, pins) on the caller's DB — without it a library invocation
        # with an explicit db_path and no db.set_db_path bootstrap logged its
        # cost row against db.DB_PATH ("no such table: llm_calls").
        payload = call_llm_structured(
            prompt,
            purpose=POSITION_REVIEW_PURPOSE,
            ticker=pre.ticker,
            required_keys=_VERDICT_KEYS,
            db_path=db_path,
        )
        out = apply_behavioral_guard(
            pre, parse_verdict_output(cast("dict[str, object]", payload)), graded_line=graded_line
        )

    memo_id = _persist_review(pre, out, user_id=user_id, db_path=db_path) if persist else None
    return PositionReview(pre=pre, output=out, memo_id=memo_id)


# --------------------------------------------------------------------------- #
# Slice 3 — deterministic chat rendering (the instant, no-LLM /review surface)
# --------------------------------------------------------------------------- #


def mechanical_read(pre: PreAnalysis) -> str:
    """A one-line deterministic trim/hold read mirroring the behavioral guard —
    no LLM. This is what the instant ``/review`` command shows; the LLM-calibrated
    narrative comes from :func:`review_position`."""
    if not pre.thesis_present:
        kind = "an index / macro sleeve" if pre.is_index_instrument else "unencoded"
        return f"No encoded thesis ({kind}) — encode one first; don't trim on price alone."
    if pre.break_rule_status == "breach":
        return "Thesis BREACHED — reassess; a legitimate trim/exit candidate."
    if pre.valuation_verdict in ("trim", "sell"):
        return f"Over-valued (ladder: {pre.valuation_verdict}) — valuation supports trimming."
    sizing_oversized = pre.weight_vs_band == "above_band" or (
        pre.weight_vs_band == "no_band" and pre.concentration_flag
    )
    if sizing_oversized:
        return (
            "Framework intact but the position is OVERSIZED — trim-to-target is the sizing "
            "case (keep the core; a LEAP keeps the upside)."
        )
    return (
        "HOLD — framework intact, not over-valued, not oversized. Trimming here would be "
        "price-only (your sell-winners-too-early trap)."
    )


def render_tax_lines(tax: PositionTaxView | None) -> list[str]:
    """Markdown bullets for the tax block — shared by the chat reply, the CLI
    summary, and the persisted memo body. Empty for a pre-tax-stage
    :class:`PreAnalysis` (``tax=None``); a single honest line when the tracker
    couldn't supply a view; the dense block otherwise."""
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


def render_pre_analysis_chat(pre: PreAnalysis, *, db_path: Path | str | None = None) -> str:
    """Compact Markdown reply for the ``/review`` chat command — the grounded
    facts plus the mechanical read, the tax block, the live graded-sells base
    rate (when any sells/trims are graded yet), and a pointer to the full
    calibrated verdict. ``db_path`` is optional — omitted, the base-rate line
    simply doesn't render (same degrade as a fresh/ungraded DB)."""
    wt = f"{pre.weight_pct:.1f}%" if pre.weight_pct is not None else "—"
    conc = f"FLAGGED (>= {CONCENTRATION_PCT:.0f}% single name)" if pre.concentration_flag else "no"
    fv = f"${pre.npv_per_share:,.2f}" if pre.npv_per_share is not None else "—"
    asked = f"${pre.at_price:,.2f}" if pre.at_price is not None else "—"
    gap = f"{pre.dcf_gap_pct:+.1f}%" if pre.dcf_gap_pct is not None else "—"
    watch = f" · watch: {'; '.join(pre.tripped_rules)}" if pre.tripped_rules else ""
    at_flag = f" --at-price {pre.at_price:g}" if pre.at_price is not None else ""
    graded_line = graded_sell_record(db_path)
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
            f"- Mechanical read: {mechanical_read(pre)}",
            *([f"- {graded_line}"] if graded_line is not None else []),
            f"- Full calibrated verdict (LLM + behavioral guard): "
            f"`python execution/review_position.py {pre.ticker}{at_flag}`",
        ]
    )


def render_pre_analysis_plain(pre: PreAnalysis, *, db_path: Path | str | None = None) -> str:
    """Plain-ASCII reply for the ``/review`` command on Telegram — the SAME
    grounded facts as :func:`render_pre_analysis_chat` (including the live
    graded-sells base rate), with no ``**``/backtick Markdown markers
    (Telegram's ``send_message`` never sets ``parse_mode``, so a
    Markdown-shaped body arrives as a raw text wall — literal asterisks and
    backticks). Also drops the CLI pointer line: ``python execution/...`` is a
    broken instruction in a chat channel with no shell. Ends instead with a
    pointer to the calibrated review surface in the web app."""
    wt = f"{pre.weight_pct:.1f}%" if pre.weight_pct is not None else "-"
    conc = f"FLAGGED (>= {CONCENTRATION_PCT:.0f}% single name)" if pre.concentration_flag else "no"
    fv = f"${pre.npv_per_share:,.2f}" if pre.npv_per_share is not None else "-"
    asked = f"${pre.at_price:,.2f}" if pre.at_price is not None else "-"
    gap = f"{pre.dcf_gap_pct:+.1f}%" if pre.dcf_gap_pct is not None else "-"
    watch = f" - watch: {'; '.join(pre.tripped_rules)}" if pre.tripped_rules else ""
    graded_line = graded_sell_record(db_path)
    return "\n".join(
        [
            f"{pre.ticker} - position review (deterministic read; weight source: "
            f"{pre.weight_source})",
            f"- Sizing: {wt} of book - concentration: {conc} - band: {pre.weight_vs_band}",
            f"- Fundamentals: thesis {pre.verdict_label or '-'} - "
            f"break-rules {pre.break_rule_status}{watch}",
            f"- Valuation: fair {fv} vs asked {asked} -> {gap} (+ = over-valued) - "
            f"ladder: {pre.valuation_verdict}",
            *render_tax_lines(pre.tax),
            f"- Mechanical read: {mechanical_read(pre)}",
            *([f"- {graded_line}"] if graded_line is not None else []),
            "- Full calibrated review: from the desk (Holding -> Review).",
        ]
    )


def parse_review_command(text: str) -> tuple[str, float | None] | None:
    """Parse ``/review <TICKER> [at $PRICE]`` into ``(ticker, at_price)``.

    Returns ``None`` when no ticker follows the command (the usage-message
    case). The ONE parser for this command shape — shared by
    ``review_reply_text`` (the instant Slice-1 reply), ``ask.commands``'s
    background Slice-2 kickoff, and the Telegram poller — so the at-price
    regex and the ticker-extraction rule (uppercase, strip a leading ``$``)
    can never drift between callers.
    """
    parts = text.split()
    if len(parts) < 2:
        return None
    ticker = parts[1].upper().lstrip("$")
    match = _AT_PRICE_RX.search(text)
    at_price = float(match.group(1)) if match else None
    return ticker, at_price


def review_reply_text(repo_root: Path, text: str, *, plain: bool = False) -> str:
    """``/review <TICKER> [at $PRICE]`` end-to-end: parse the ticker + at-price
    out of ``text`` and return the rendered instant pre-analysis reply.

    The ONE place that renders this command shape — the Ask-tab chat command
    (``ask.commands._review_command``) and the Telegram poller both call this
    instead of each re-implementing the parsing, so the at-price regex and the
    usage/error copy can never drift between the two surfaces. LLM-free and
    fast: ``build_pre_analysis`` never imports a model client.

    ``plain=True`` (the Telegram callers) renders via
    :func:`render_pre_analysis_plain` — no Markdown, since ``send_message``
    never sets ``parse_mode`` there. The web chat (Ask tab) keeps the default
    Markdown variant, which its renderer displays properly.
    """
    if plain:
        usage = "Usage: /review <TICKER> [at $PRICE] - e.g. /review RBRK or /review FLKR at $70."
    else:
        usage = (
            "Usage: /review <TICKER> [at $PRICE] — e.g. `/review RBRK` or `/review FLKR at $70`."
        )
    parsed = parse_review_command(text)
    if parsed is None:
        return usage
    ticker, at_price = parsed
    db_path = repo_root / "data" / "portfolio.db"
    try:
        pre = build_pre_analysis(repo_root, ticker, at_price=at_price, db_path=db_path)
    except Exception as exc:
        return f"Couldn't build a review for {ticker}: {type(exc).__name__}: {exc}"
    if plain:
        return render_pre_analysis_plain(pre, db_path=db_path)
    return render_pre_analysis_chat(pre, db_path=db_path)
