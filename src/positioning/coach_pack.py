"""The positioning coach's context pack — ask-engine reuse, no new chat stack.

The coach is one more conversational surface of the unified ask engine
(``ask.engine.respond_turn``): a :class:`~ask.context.ContextPack` whose
system context makes the LLM an investment-coach/wealth-advisor for the
OWNER'S POSITIONING specifically —

  * socratic push-back: interrogate horizon, risk capacity, life
    circumstances; challenge inconsistencies between what the owner says and
    what the book shows (tone precedent: ``advisor/socratic.py``);
  * grounding: the ACTIVE saved positioning (or "current book is the default
    target"), live weights, the risk snapshot, tracker sector positioning,
    style-factor loadings, crowding clusters — every number quoted from data,
    never invented;
  * the CLOSED sector vocabulary (tracker ``by_sector`` labels) so anything
    the coach discusses can later be encoded against the taxonomy the fit
    math actually compares;
  * a hard rule: the coach NEVER claims anything was saved. Persistence
    happens only through the explicit propose→approve flow
    (``positioning/encode.py``), where the owner's edits win.

Conversation history rides ``ask_sessions``/``ask_turns`` with
``scope='positioning'`` so coach threads never mix into the Ask tab's list.
Billing/routing runs under the ``positioning_coach_turn`` purpose via the
pack's ``narrative_purpose`` seam.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ask.context import ContextPack
from candidate_fit_cache import assemble_book_context
from positioning.store import latest_intent
from positioning.target import TargetContext, resolve_target_context

COACH_PURPOSE = "positioning_coach_turn"
SESSION_SCOPE = "positioning"

_INSTRUCTIONS = """You are the owner's investment coach and wealth advisor for
PORTFOLIO POSITIONING — the standing conversation about what shape their book
should take (growth/value tilt, sector weights, volatility posture,
international/small-cap/EM/cash sleeves, position-size limits), grounded in
their life circumstances and horizon.

How to behave:
- PUSH BACK socratically. When the owner states a view, interrogate it before
  accepting it: what horizon? what would make it wrong? how does it square
  with their life circumstances (liquidity needs, income stability, upcoming
  expenses)? Point out inconsistencies between what they say and what the
  BOOK STATE below shows — e.g. "you say you want less growth crowding, but
  you also asked about adding another mega-cap grower".
- GROUND every claim in the numbers below. Quote them. If a number you need
  is absent (tracker offline, no snapshot), SAY it is unavailable — never
  estimate or invent it.
- Talk in the book's own vocabulary: growth tilt = QQQ-beta minus SPY-beta;
  sector names from the SECTOR VOCABULARY list verbatim; weights as % of the
  book; sleeves limited to intl / small_cap / em / cash.
- Iterate toward CONCRETE targets. When the owner's view firms up, restate it
  as specific numbers ("target growth tilt ~0.0 from +0.25 today; Technology
  toward 30% from 45%; build an intl small-cap value sleeve toward 15%") so
  the encode step has something exact to propose.
- You NEVER save anything. When the conversation has converged, tell the
  owner to click "Propose targets from this conversation" — the proposal is
  shown as an editable form and only their explicit approval persists it.
  Do not claim any target is set, saved, or active.
- No stances on individual holdings here (buy/trim/sell of a specific name
  belongs to the per-holding Socratic think-through) — this conversation is
  about the book's SHAPE."""


def _fmt(v: float | None, spec: str = "+.2f") -> str:
    return format(v, spec) if v is not None else "unavailable"


def _pct(v: float | None) -> str:
    return f"{v * 100.0:.0f}%" if v is not None else "unavailable"


def _grounding_block(repo_root: Path, db_path: Path) -> str:
    """The live book state + active positioning, absence-explicit throughout
    (the ``ask/packs.py`` failure contract: say 'offline', never guess)."""
    book = assemble_book_context(repo_root, db_path=db_path)
    target = resolve_target_context(db_path, book)

    lines: list[str] = ["BOOK STATE (live where possible; every number is real):"]
    lines.append(f"- book Sharpe: {_fmt(book.sharpe)}")
    lines.append(f"- risk-free rate used: {_fmt(book.risk_free_annual, '.3f')}")
    lines.append(f"- growth tilt (QQQ beta - SPY beta): {_fmt(book.growth_tilt)}")
    if book.weights:
        top = sorted(book.weights.items(), key=lambda kv: kv[1], reverse=True)[:12]
        lines.append(
            "- held weights: "
            + ", ".join(f"{t} {_pct(w)}" for t, w in top)
            + (f" (+{len(book.weights) - len(top)} more)" if len(book.weights) > len(top) else "")
        )
    else:
        lines.append("- held weights: unavailable (weights cache empty)")
    if book.sector_weights:
        sect = sorted(book.sector_weights.items(), key=lambda kv: kv[1], reverse=True)
        lines.append("- sector weights: " + ", ".join(f"{s} {_pct(w)}" for s, w in sect))
    else:
        lines.append("- sector weights: unavailable (tracker offline)")
    for reason in book.degraded:
        lines.append(f"- DEGRADED: {reason}")

    lines.append("")
    lines.append(_style_and_crowding_block(repo_root))
    lines.append("")
    lines.append(_active_positioning_block(db_path, target))
    lines.append("")
    vocabulary = sorted(book.sector_weights) or [
        "(tracker offline — sector vocabulary unavailable)"
    ]
    lines.append("SECTOR VOCABULARY (use these labels verbatim): " + ", ".join(vocabulary))
    lines.append("SLEEVE VOCABULARY: intl, small_cap, em, cash")
    return "\n".join(lines)


def _style_and_crowding_block(repo_root: Path) -> str:
    """Style-factor loadings + crowding clusters from the offline price caches;
    absence-explicit when the proxy/price files are thin."""
    lines: list[str] = ["STYLE & CROWDING (offline price-cache reads):"]
    from portfolio_weights import read_materialized_weights

    weights = read_materialized_weights(repo_root)
    try:
        from portfolio_style_factors import build_style_rollup_from_disk

        rollup = (
            build_style_rollup_from_disk(repo_root, sorted(weights), weights) if weights else None
        )
    except OSError:
        rollup = None
    if rollup is not None and rollup.legs:
        for leg in rollup.legs:
            if leg.book_beta is not None:
                lines.append(
                    f"- style {leg.label} ({leg.spread_label}): book beta {leg.book_beta:+.2f}"
                )
    else:
        lines.append("- style loadings: unavailable (thin price/proxy history)")

    clusters_line_added = False
    try:
        from allocation.price_history import (
            build_aligned_returns,
            daily_log_returns,
            load_daily_closes,
        )
        from portfolio_correlation import correlation_matrix, find_crowding_clusters

        returns = {
            t: r for t in weights if (r := daily_log_returns(load_daily_closes(t, repo_root)))
        }
        aligned = build_aligned_returns(returns) if len(returns) >= 2 else None
        corr = correlation_matrix(aligned.matrix) if aligned is not None else None
        if aligned is not None and corr is not None:
            for c in find_crowding_clusters(aligned.tickers, corr, weights):
                lines.append(
                    f"- crowding cluster (corr ≥ 0.70): {', '.join(c.tickers)} "
                    f"— {c.combined_weight_pct:.0f}% of book, avg corr {c.avg_corr:+.2f}"
                )
                clusters_line_added = True
    except OSError:
        pass
    if not clusters_line_added:
        lines.append("- crowding clusters: none detected (or insufficient history)")
    return "\n".join(lines)


def _active_positioning_block(db_path: Path, target: TargetContext) -> str:
    lines: list[str] = ["ACTIVE POSITIONING:"]
    if target.source == "book_default":
        lines.append(
            "- no saved positioning — the current book is the default target "
            "(every gap reads zero until the owner sets intent)"
        )
        return "\n".join(lines)
    lines.append(
        f"- saved intent v{target.intent_id} (created {target.intent_created_at}); "
        f"the owner's words: {target.narrative or '(none recorded)'}"
    )
    lines.append(
        f"- target growth tilt: {_fmt(target.growth_tilt)} (band ±{target.growth_tilt_band:.2f})"
    )
    for sector, band in sorted(target.sector_bands.items()):
        lines.append(
            f"- target {sector}: {_pct(target.sector_weights.get(sector))} (band ±{band * 100.0:.0f}pp)"
        )
    for sleeve, frac in sorted(target.sleeves.items()):
        lines.append(f"- target sleeve {sleeve}: {_pct(frac)}")
    if target.vol_posture:
        lines.append(f"- vol posture: {target.vol_posture}")
    if target.max_position_weight is not None:
        lines.append(f"- max position weight: {_pct(target.max_position_weight)}")
    return "\n".join(lines)


def build_positioning_pack(repo_root: Path, db_path: Path) -> ContextPack:
    """The coach's ContextPack: portfolio scope (routing), positioning
    instructions + grounding as the narrative system context, history via the
    caller-supplied session id (``persist=False`` — ask_turns is the thread
    store, same as the Ask tab), billed under ``positioning_coach_turn``."""
    system_context = _INSTRUCTIONS + "\n\n" + _grounding_block(repo_root, db_path)
    return ContextPack(
        scope="portfolio",
        default_tickers=[],
        system_context=system_context,
        persist=False,
        narrative_purpose=COACH_PURPOSE,
    )


def coach_session_narrative(db_path: Path) -> str | None:
    """The active intent's narrative for surfaces that only need the words."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        intent = latest_intent(conn)
    finally:
        conn.close()
    return intent.narrative if intent is not None else None
