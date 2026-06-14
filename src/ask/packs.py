"""Portfolio evidence packs — the ask engine's reach into the analyst's own
book (fund-grade build S4).

Document grounding (``ask.grounding``) answers "what do the filings say";
these packs answer "what do I hold, at what conviction, against what
valuation, and what did I already decide" — the six portfolio stores a
trim/add/size question needs:

  holdings     — live positions from the tracker API (weights, cost basis,
                 unrealized P&L, and per-account tax treatment so tax-loss-
                 harvest / "which lot do I sell" is answerable); the tracker
                 owns all the math
  conviction   — stated conviction + target weights (position_sizing_intent)
  dcf          — latest fair value vs live price per ticker (dcf_runs)
  decisions    — the recommendation ledger + outcomes (decisions)
  calibration  — the analyst's OWN track record: hit-rate overall and by
                 conviction cohort, reversal win/loss, days-to-outcome, and —
                 when the question names a name with a live call — that
                 cohort's documented accuracy (decision_calibration, the
                 keystone "confront me at the decision moment" loop)
  journal      — open analyst notes (analyst_notes)
  performance  — window TWR vs benchmarks + per-name alpha + concentration
                 (tracker analytics API)

Each selected pack becomes ONE raw evidence item (``ask.grounding`` numbers
it into the [n] citation system), so a pack's whole story is citeable with a
single marker and the marker count stays small. Pack text is
absence-explicit: a focus ticker with no position / no DCF run / no stated
conviction gets a "none on file" line — silence is what breeds confident
hallucination, and an explicit negative is itself citeable evidence.

``focus_tickers`` scopes the per-ticker lines to the names the question is
about; empty means portfolio-wide (top rows by weight/upside, capped).

Failure contract (matches grounding): every loader is best-effort and
read-only over possibly-missing tables — sqlite3 errors and empty stores
contribute nothing. The tracker-backed packs are the exception: when
SELECTED but unreachable they emit an explicit "tracker offline" item
instead of vanishing, so the model knows live data is missing rather than
guessing around it.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from calibration_guard import rate_phrase
from decision_calibration import bucket_for_conviction, build_calibration, omission_clause
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import (
    TAX_BUCKETS,
    LivePortfolio,
    LivePosition,
    TaxLot,
    fetch_live_portfolio,
    fetch_portfolio_analytics,
)

log = logging.getLogger(__name__)

# Schema vocabulary of position_sizing_intent.intent_kind (the sizing-audit
# panel writes/reads the same two — pipeline.allocation_decisions_panel).
_CONVICTION_KIND = "conviction"
_TARGET_WEIGHT_KIND = "target_weight_pct"

# A DCF upside above this is more likely a mis-modeled run (currency/unit
# artifacts on sweep-built watchlist DCFs) than a free double — same ceiling
# and reasoning as advisor.context.IMPLAUSIBLE_UPSIDE_PCT.
_IMPLAUSIBLE_UPSIDE_PCT = 100.0

# Per-ticker lines when the question names tickers / portfolio-wide row caps.
_MAX_FOCUS_TICKERS = 8
_MAX_BOOK_ROWS = 10
_MAX_LEDGER_ROWS = 6
_NOTE_BODY_CHARS = 140
_NARRATIVE_CHARS = 80


@dataclass(frozen=True, slots=True)
class PackSpec:
    """One pack's identity: citation chip text, home surface, token budget
    (as a char cap on the item text), and its line in the router catalog."""

    key: str
    label: str  # citation chip text
    href: str  # in-app home surface for the chip (shell hash deep-link)
    char_budget: int
    router_hint: str  # one catalog line in the router prompt


PACKS: tuple[PackSpec, ...] = (
    PackSpec(
        "holdings",
        "Tracker · live holdings",
        "/#portfolio",
        1100,
        "live position sizes (weights, market value, cost basis, unrealized "
        "P&L) AND each name's per-account tax treatment (taxable / tax-deferred "
        '/ tax-free) — for trim/add/size/exposure/cost-basis/"do I own" AND '
        'tax-loss-harvesting / "which lot or account do I sell to minimize tax" '
        "questions",
    ),
    PackSpec(
        "conviction",
        "Sizing intents · conviction",
        "/#decisions_record",
        700,
        "the analyst's own stated conviction (n/5) and target weights per name",
    ),
    PackSpec(
        "dcf",
        "DCF runs · fair value",
        "/#decisions_record",
        700,
        "latest model fair value vs live price (upside/downside %) per name",
    ),
    PackSpec(
        "decisions",
        "Decision ledger",
        "/#decisions_record",
        800,
        "past add/trim/hold/sell recommendations, whether the analyst acted, and graded outcomes",
    ),
    PackSpec(
        "calibration",
        "Decision calibration · track record",
        "/#decisions_record",
        800,
        "the analyst's OWN forecasting track record: hit-rate overall and by "
        'stated conviction, reversal win/loss, days-to-outcome — for "am I '
        'getting better", "how accurate are my calls", "can I trust my '
        'high-conviction read", and conviction-driven trim/add where the '
        "analyst's documented accuracy should temper the call",
    ),
    PackSpec(
        "journal",
        "Analyst journal · open notes",
        "/#journal",
        800,
        "the analyst's open questions, watch-items, assumptions and decisions on file",
    ),
    PackSpec(
        "performance",
        "Tracker · performance",
        "/#portfolio",
        700,
        "portfolio returns vs SPY/QQQ benchmarks, per-position dollar alpha, "
        'concentration — "how am I doing / what\'s working" questions',
    ),
)

PACK_KEYS: tuple[str, ...] = tuple(p.key for p in PACKS)


def load_packs(
    keys: Iterable[str],
    *,
    db_path: Path,
    focus_tickers: list[str],
) -> list[dict[str, object]]:
    """Load the selected packs as grounding-shaped raw evidence items.

    Packs come back in canonical PACKS order regardless of selection order
    (deterministic citation numbering). Unknown keys are ignored; a loader
    failure drops only its own pack.
    """
    wanted = set(keys) & set(PACK_KEYS)
    focus = [t.strip().upper() for t in focus_tickers if t.strip()][:_MAX_FOCUS_TICKERS]
    out: list[dict[str, object]] = []
    for spec in PACKS:
        if spec.key not in wanted:
            continue
        try:
            item = _LOADERS[spec.key](spec, db_path, focus)
        except Exception:
            log.warning({"event": "ask_pack_load_failed", "pack": spec.key}, exc_info=True)
            item = None
        if item is not None:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _item(spec: PackSpec, text: str, *, label_suffix: str = "") -> dict[str, object]:
    return {
        "kind": spec.key,
        "label": spec.label + label_suffix,
        "text": text,
        "doc_id": None,
        "href": spec.href,
        "source_url": None,
    }


def _offline_item(spec: PackSpec, what: str, error: str | None) -> dict[str, object]:
    reason = error or "no response"
    return _item(
        spec,
        f"{what} unavailable — portfolio tracker offline ({reason}). "
        f"{what} are NOT known this turn; say so if asked rather than estimating.",
        label_suffix=" (offline)",
    )


def _rows(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    """Best-effort rows; [] on missing DB / table / column."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        return cast("list[sqlite3.Row]", conn.execute(sql, params).fetchall())
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _join_capped(lines: list[str], budget: int) -> str:
    """Join with "; " while staying under the pack's char budget; the cut is
    announced ("(+N more)") so truncation is never silent."""
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        if out and used + len(line) > budget:
            out.append(f"(+{len(lines) - i} more)")
            break
        out.append(line)
        used += len(line) + 2
    return "; ".join(out)


def _money(v: float | None) -> str:
    if v is None:
        return "?"
    a = abs(v)
    if a >= 1e6:
        return f"${v / 1e6:,.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:,.1f}K"
    return f"${v:,.0f}"


def _signed_money(v: float) -> str:
    return ("+" if v >= 0 else "-") + _money(abs(v))


def _f(v: object) -> float | None:
    try:
        return float(cast("float", v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _day(v: object) -> str:
    return str(v or "")[:10] or "?"


# ---------------------------------------------------------------------------
# holdings — live tracker positions (+ per-account tax treatment)
# ---------------------------------------------------------------------------

# Display names for the tracker's tax-treatment buckets (TAX_BUCKETS order).
# "untagged" reads better than the raw "unknown" when an account's Plaid
# type/subtype didn't classify.
_TAX_LABELS: dict[str, str] = {
    "taxable": "taxable",
    "tax_deferred": "tax-deferred",
    "tax_free": "tax-free",
    "unknown": "untagged",
}


def _tax_split(lots: list[TaxLot]) -> list[tuple[str, float]]:
    """A position's lot market values summed by tax treatment, in display
    order, dropping empty buckets. ``[]`` when no lot carries a value — the
    tracker didn't surface per-account data for this name. Values are dollars
    (the client already coerced any Decimal-as-string market value to float)."""
    by: dict[str, float] = {}
    for lot in lots:
        if lot.market_value:
            by[lot.tax_treatment] = by.get(lot.tax_treatment, 0.0) + lot.market_value
    return [(b, by[b]) for b in TAX_BUCKETS if by.get(b)]


def _position_line(ticker: str, p: LivePosition) -> str:
    w = f"{p.percent_of_portfolio:.1f}%" if p.percent_of_portfolio is not None else "?%"
    bits = [w, _money(p.market_value)]
    if p.cost_basis is not None:
        bits.append(f"cost {_money(p.cost_basis)}")
    if p.unrealized_pnl is not None:
        bits.append(f"P&L {_signed_money(p.unrealized_pnl)}")
    split = _tax_split(p.accounts)
    if split:
        bits.append("held: " + ", ".join(f"{_money(v)} {_TAX_LABELS[b]}" for b, v in split))
    return f"{ticker} {' · '.join(bits)}"


def _tax_context(live: LivePortfolio) -> str:
    """Trailing tax block for the holdings item: the book-wide split by tax
    treatment plus the rules that make "which lot do I sell" answerable.

    Empty string when the tracker carries no per-account tax data (older
    builds / nothing classifiable), so the holdings pack is byte-for-byte
    unchanged when tax lots aren't available — and the tracker-offline path
    omits it for free (the whole item is the offline marker)."""
    present = [(b, live.by_tax_treatment[b]) for b in TAX_BUCKETS if live.by_tax_treatment.get(b)]
    if not present:
        return ""
    book = ", ".join(f"{_money(v)} {_TAX_LABELS[b]}" for b, v in present)
    return (
        f" | Book by tax treatment — {book}. Tax-loss harvesting only realizes a "
        "deduction in TAXABLE lots; selling appreciated shares from "
        "tax-free/tax-deferred lots triggers no current capital-gains tax. "
        "Per-lot cost basis and holding period (short- vs long-term) are NOT in "
        "this data — don't infer them."
    )


def _holdings_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    live = fetch_live_portfolio()
    if not live.available:
        return _offline_item(spec, "Live holdings (weights, cost basis, tax lots)", live.error)
    by_ticker = {(p.ticker or "").upper(): p for p in live.positions if p.ticker}
    if focus:
        lines = [
            _position_line(t, by_ticker[t]) if t in by_ticker else f"{t}: no current position"
            for t in focus
        ]
    else:
        top = sorted(live.positions, key=lambda p: -(p.market_value or 0.0))[:_MAX_BOOK_ROWS]
        lines = [_position_line((p.ticker or "?").upper(), p) for p in top]
        rest = len(live.positions) - len(top)
        if rest > 0:
            lines.append(f"(+{rest} smaller positions)")
    # Reserve the tax block's room so the per-position lines never crowd it out;
    # _join_capped overshoots by at most the header, comfortably inside the
    # eval's +120 budget slack.
    tax = _tax_context(live)
    header = f"Live portfolio (tracker, total {_money(live.total_market_value)}): "
    body = _join_capped(lines, max(0, spec.char_budget - len(tax)))
    return _item(spec, header + body + tax)


# ---------------------------------------------------------------------------
# conviction — stated sizing posture (position_sizing_intent)
# ---------------------------------------------------------------------------


def _conviction_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    rows = _rows(
        db_path,
        "SELECT ticker, intent_kind, intent_value, narrative, created_at "
        "FROM position_sizing_intent WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (DEFAULT_USER_ID,),
    )
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in rows:  # newest-first → first per (ticker, kind) wins
        latest.setdefault((str(r["ticker"]).upper(), str(r["intent_kind"])), r)
    if not latest:
        return None
    tickers = focus or sorted({t for t, _ in latest})[:_MAX_FOCUS_TICKERS]
    lines: list[str] = []
    for t in tickers:
        conv = latest.get((t, _CONVICTION_KIND))
        target = latest.get((t, _TARGET_WEIGHT_KIND))
        if conv is None and target is None:
            lines.append(f"{t}: no stated conviction or target weight")
            continue
        bits: list[str] = []
        narrative: str | None = None
        if conv is not None:
            v = _f(conv["intent_value"])
            bits.append(
                f"conviction {v:g}/5 (since {_day(conv['created_at'])})"
                if v is not None
                else f"conviction stated {_day(conv['created_at'])}"
            )
            narrative = cast("str | None", conv["narrative"])
        if target is not None:
            v = _f(target["intent_value"])
            if v is not None:
                bits.append(f"target {v:g}%")
            narrative = narrative or cast("str | None", target["narrative"])
        line = f"{t}: {' · '.join(bits)}"
        if narrative and narrative.strip():
            flat = " ".join(narrative.split())[:_NARRATIVE_CHARS]
            line += f' — "{flat}"'
        lines.append(line)
    return _item(spec, "Stated sizing posture: " + _join_capped(lines, spec.char_budget))


# ---------------------------------------------------------------------------
# dcf — latest fair value vs live price (dcf_runs)
# ---------------------------------------------------------------------------


def _latest_dcf_by_ticker(db_path: Path) -> dict[str, tuple[float, float, str]]:
    """ticker -> (fair_value, live_price, run_date), latest usable run.
    Upside is recomputed downstream from the row's own two fields — the
    convention-proof read (over_under_pct is never read; its sign convention
    differed across builders)."""
    rows = _rows(
        db_path,
        "SELECT ticker, valuation_date, npv_per_share, live_price "
        "FROM dcf_runs ORDER BY ticker, created_at DESC, id DESC",
    )
    out: dict[str, tuple[float, float, str]] = {}
    for r in rows:
        t = str(r["ticker"]).upper()
        if t in out:
            continue
        fv = _f(r["npv_per_share"])
        px = _f(r["live_price"])
        if fv is None or px is None or fv <= 0 or px <= 0:
            continue
        out[t] = (fv, px, _day(r["valuation_date"]))
    return out


def _dcf_line(ticker: str, fv: float, px: float, run_date: str) -> str:
    upside = (fv / px - 1.0) * 100.0
    line = f"{ticker}: fair ${fv:,.2f} vs live ${px:,.2f} → {upside:+.0f}% upside (run {run_date})"
    if upside > _IMPLAUSIBLE_UPSIDE_PCT:
        line += " — suspect (>+100%, likely a mis-modeled run, not an opportunity)"
    return line


def _dcf_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    runs = _latest_dcf_by_ticker(db_path)
    if not runs:
        return None
    if focus:
        lines = [_dcf_line(t, *runs[t]) if t in runs else f"{t}: no DCF run on file" for t in focus]
    else:
        port = {
            str(r["ticker"]).upper()
            for r in _rows(
                db_path,
                "SELECT ticker FROM tracked_companies "
                "WHERE archived_at IS NULL AND list_type = 'portfolio'",
            )
        }
        have = sorted(
            (t for t in port if t in runs),
            key=lambda t: -(runs[t][0] / runs[t][1] - 1.0),
        )[:_MAX_BOOK_ROWS]
        if not have:
            return None
        lines = [_dcf_line(t, *runs[t]) for t in have]
        missing = len(port) - len(have)
        if missing > 0:
            lines.append(f"({missing} portfolio name(s) lack a DCF run)")
    return _item(spec, "Model fair values (DCF): " + _join_capped(lines, spec.char_budget))


# ---------------------------------------------------------------------------
# decisions — the recommendation ledger + outcomes
# ---------------------------------------------------------------------------


def _decisions_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    sql = (
        "SELECT made_at, ticker, recommendation_kind, recommendation_value, conviction, "
        "user_action_kind, outcome_label, outcome_pct FROM decisions"
    )
    params: tuple[object, ...] = ()
    if focus:
        sql += f" WHERE UPPER(ticker) IN ({','.join('?' * len(focus))})"
        params = tuple(focus)
    sql += f" ORDER BY made_at DESC, id DESC LIMIT {_MAX_LEDGER_ROWS}"
    rows = _rows(db_path, sql, params)
    if not rows:
        return None
    lines: list[str] = []
    for r in rows:
        kind = str(r["recommendation_kind"])
        size = _f(r["recommendation_value"])
        head = f"{_day(r['made_at'])} {str(r['ticker']).upper()} {kind}"
        if size is not None:
            head += f" {size:g}%"
        conviction = cast("str | None", r["conviction"])
        if conviction:
            head += f" (conviction {conviction})"
        acted = cast("str | None", r["user_action_kind"]) or "no action recorded"
        outcome = cast("str | None", r["outcome_label"]) or "pending"
        pct = _f(r["outcome_pct"])
        if pct is not None:
            outcome += f" ({pct:+.0f}%)"
        lines.append(f"{head} — acted: {acted}; outcome: {outcome}")
    return _item(spec, "Decision ledger (newest first): " + _join_capped(lines, spec.char_budget))


# ---------------------------------------------------------------------------
# calibration — the analyst's own track record (decision_calibration)
# ---------------------------------------------------------------------------

# Per-name cohort callouts (the "confront me at the decision moment" lines) are
# capped so a wide focus can't blow the budget before the portfolio-wide stats.
_MAX_CALIBRATION_FOCUS = 3


def _focus_conviction_cohort(db_path: Path, ticker: str) -> tuple[str, str] | None:
    """The conviction cohort a focus ticker's CURRENT stance belongs to, plus a
    human descriptor — so the pack can put "your {bucket}-conviction calls have
    graded X%" right where a fresh call on this name is being weighed.

    A pending (ungraded) decision is the strongest signal of a live call;
    failing that, the latest stated sizing conviction. None when neither exists
    (no live stance, so no cohort to confront with)."""
    pend = _rows(
        db_path,
        "SELECT conviction, recommendation_kind FROM decisions "
        "WHERE UPPER(ticker) = ? AND (outcome_label IS NULL OR outcome_label = 'pending') "
        "ORDER BY made_at DESC, id DESC LIMIT 1",
        (ticker,),
    )
    if pend:
        bucket = bucket_for_conviction(pend[0]["conviction"])
        return bucket, f"a pending {bucket}-conviction {pend[0]['recommendation_kind']} call"
    stated = _rows(
        db_path,
        "SELECT intent_value FROM position_sizing_intent "
        "WHERE user_id = ? AND UPPER(ticker) = ? AND intent_kind = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (DEFAULT_USER_ID, ticker, _CONVICTION_KIND),
    )
    if stated:
        v = _f(stated[0]["intent_value"])
        if v is not None:
            bucket = bucket_for_conviction(v)
            return bucket, f"a stated conviction of {v:g}/5 ({bucket})"
    return None


def _calibration_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    stats = build_calibration(db_path=db_path)
    if stats is None or stats.total == 0:
        return None  # no ledger / empty ledger — nothing to calibrate against
    lines: list[str] = [
        rate_phrase("overall", stats.overall_hit_rate, stats.graded)
        if stats.graded
        else f"{stats.total} calls logged, none graded yet (n=0)"
    ]
    conv = [
        rate_phrase(f"{b.conviction}-conviction", b.hit_rate, b.graded)
        for b in stats.by_conviction
        if b.graded
    ]
    if conv:
        lines.append("by conviction — " + ", ".join(conv))
    if stats.reversals:
        v, c = stats.reversals_vindicated, stats.reversals_cost
        unresolved = len(stats.reversals) - v - c
        parts: list[str] = []
        if v:
            parts.append(f"{v} vindicated (you were right to override)")
        if c:
            parts.append(f"{c} cost (the override hurt)")
        if unresolved:
            parts.append(f"{unresolved} still open")
        lines.append("reversals — " + (", ".join(parts) or f"{len(stats.reversals)} logged"))
    if stats.time_to_outcome:
        lines.append(
            "median days to outcome — "
            + ", ".join(f"{t.kind} {round(t.median_days)}d" for t in stats.time_to_outcome[:3])
        )
    om_clause = omission_clause(stats.omissions)
    if om_clause is not None:
        # The omission side (L11): names you PASSED on, graded by what they did
        # next — so a fresh pass is weighed against your record of passing.
        lines.append("omissions — " + om_clause)
    for t in focus[:_MAX_CALIBRATION_FOCUS]:
        cohort = _focus_conviction_cohort(db_path, t)
        if cohort is None:
            continue
        bucket, descriptor = cohort
        cb = next((b for b in stats.by_conviction if b.conviction == bucket), None)
        if cb is not None and cb.graded:
            lines.append(
                f"{t} carries {descriptor} — "
                + rate_phrase(f"your {bucket}-conviction calls", cb.hit_rate, cb.graded)
            )
        else:
            lines.append(
                f"{t} carries {descriptor} — but no graded {bucket}-conviction "
                "calls yet to calibrate against (n=0)"
            )
    return _item(
        spec,
        "Your decision calibration (graded outcomes from your own ledger — weigh a fresh "
        "call against this track record): " + _join_capped(lines, spec.char_budget),
    )


# ---------------------------------------------------------------------------
# journal — open analyst notes
# ---------------------------------------------------------------------------


def _journal_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    sql = (
        "SELECT ticker, kind, body, created_at FROM analyst_notes "
        "WHERE user_id = ? AND status = 'open'"
    )
    params: list[object] = [DEFAULT_USER_ID]
    if focus:
        # Portfolio-level notes (NULL ticker) always qualify alongside focus names.
        sql += f" AND (ticker IS NULL OR UPPER(ticker) IN ({','.join('?' * len(focus))}))"
        params.extend(focus)
    sql += f" ORDER BY created_at DESC, id DESC LIMIT {_MAX_LEDGER_ROWS}"
    rows = _rows(db_path, sql, tuple(params))
    if not rows:
        return None
    lines: list[str] = []
    for r in rows:
        scope = str(r["ticker"]).upper() if r["ticker"] else "portfolio"
        body = " ".join(str(r["body"]).split())
        if len(body) > _NOTE_BODY_CHARS:
            body = body[: _NOTE_BODY_CHARS - 1].rstrip() + "…"
        lines.append(f"[{scope} · {r['kind']}] {body} (since {_day(r['created_at'])})")
    return _item(
        spec, "Open notes from the analyst's journal: " + _join_capped(lines, spec.char_budget)
    )


# ---------------------------------------------------------------------------
# performance — TWR vs benchmarks, per-name alpha, concentration
# ---------------------------------------------------------------------------


def _performance_item(spec: PackSpec, db_path: Path, focus: list[str]) -> dict[str, object] | None:
    analytics = fetch_portfolio_analytics(only={"performance", "position_alpha", "positioning"})
    if not analytics.available:
        return _offline_item(spec, "Performance vs benchmarks (TWR, alpha)", None)
    lines: list[str] = []
    perf = analytics.performance
    if perf is not None and perf.points:
        last = perf.points[-1]

        def pct(v: float | None) -> str:
            return f"{v:+.1f}%" if v is not None else "?"

        lines.append(
            f"Window TWR {perf.start_date or '?'} → {perf.end_date or '?'}: "
            f"portfolio {pct(last.portfolio_return_pct)} vs SPY {pct(last.spy_return_pct)} "
            f"vs QQQ {pct(last.qqq_return_pct)}"
        )
    alpha = analytics.position_alpha
    if alpha is not None and alpha.rows:
        rows = [r for r in alpha.rows if r.ticker and r.alpha is not None]
        if focus:
            rows = [r for r in rows if (r.ticker or "").upper() in set(focus)]
        else:
            rows = sorted(rows, key=lambda r: -abs(r.alpha or 0.0))[:3]
        for r in rows:
            lines.append(f"{(r.ticker or '?').upper()} alpha {_signed_money(r.alpha or 0.0)}")
    pos = analytics.positioning
    if pos is not None and pos.concentration is not None:
        c = pos.concentration

        def num(v: float | None, suffix: str = "") -> str:
            return f"{v:.1f}{suffix}" if v is not None else "?"

        lines.append(
            f"Concentration: top1 {num(c.top1_weight_pct, '%')} · "
            f"top5 {num(c.top5_weight_pct, '%')} · "
            f"effective holdings {num(c.effective_holdings)}"
        )
    if not lines:
        return None
    return _item(spec, "Performance (tracker window): " + _join_capped(lines, spec.char_budget))


_LOADERS: dict[str, Callable[[PackSpec, Path, list[str]], dict[str, object] | None]] = {
    "holdings": _holdings_item,
    "conviction": _conviction_item,
    "dcf": _dcf_item,
    "decisions": _decisions_item,
    "calibration": _calibration_item,
    "journal": _journal_item,
    "performance": _performance_item,
}


__all__ = ["PACKS", "PACK_KEYS", "PackSpec", "load_packs"]
