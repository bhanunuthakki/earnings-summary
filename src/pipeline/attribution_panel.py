"""Attribution-narrative renderers (S15 PR2) — holding page + Portfolio tab.

Pure HTML over :mod:`attribution`'s deterministic skeletons. Two surfaces:

* ``render_attribution_section(db_path, ticker)`` — the Holding tab's Ops
  drawer: one panel section with the position's narrative. Fetches the
  tracker's ``/position-alpha`` itself (``only={"position_alpha"}`` — the
  decisions panel's exact pattern) and degrades to an events-only narrative
  when the tracker is offline; never raises.
* ``render_book_attribution_section(attributions)`` — the Portfolio →
  Performance tab: one card per research position (open lifecycle row),
  biggest |alpha| first, each carrying its narrative. Pure assembly — the
  panel passes the PositionAlpha it already fetched.

Styling rides the token system + control kit only (no raw hex; enrolled in
the test_ui_controls hex guard via the shared test sweep).
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from attribution import PositionAttribution, build_position_attribution
from identity import DEFAULT_USER_ID
from integrations.portfolio_tracker_client import PositionAlphaRow, fetch_portfolio_analytics

_ATTRIB_STYLE = """<style>
.atr-card { margin-bottom:var(--sp-2); }
.atr-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:4px; }
.atr-ticker { font-family:var(--mono); font-weight:600; }
.atr-alpha { font-family:var(--mono); font-variant-numeric:tabular-nums;
  font-size:var(--fs-caption); }
.atr-window { color:var(--muted); font-size:var(--fs-micro); font-family:var(--mono); }
.atr-narrative { font-size:var(--fs-body); line-height:1.55; color:var(--fg-soft); margin:0; }
.atr-sub { color:var(--muted); font-size:var(--fs-caption); margin:2px 0 10px; }
</style>"""


def _alpha_chip(a: PositionAttribution) -> str:
    if a.alpha_usd is None:
        return '<span class="atr-alpha" style="color:var(--muted)">&alpha; &mdash;</span>'
    tone = "k-num-pos" if a.alpha_usd >= 0 else "k-num-neg"
    sign = "+" if a.alpha_usd >= 0 else "-"
    return f'<span class="atr-alpha {tone}">&alpha; {sign}${abs(a.alpha_usd):,.0f}</span>'


def _card(a: PositionAttribution, *, show_ticker: bool) -> str:
    window = (
        f'<span class="atr-window">{escape(a.window_start or "?")} &rarr; '
        f"{escape(a.window_end or '?')}</span>"
    )
    ticker = (
        f'<a class="atr-ticker" href="/ticker/{escape(a.ticker)}">{escape(a.ticker)}</a>'
        if show_ticker
        else ""
    )
    return (
        '<div class="k-well atr-card">'
        f'<div class="atr-head">{ticker}{_alpha_chip(a)}{window}</div>'
        f'<p class="atr-narrative">{escape(a.narrative)}</p>'
        "</div>"
    )


def render_attribution_section(
    db_path: Path,
    ticker: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    api_url: str | None = None,
) -> str:
    """The Holding tab's attribution panel for one name. Tracker offline →
    the narrative leads with "no tracker alpha" and still carries the entry
    context + window events."""
    t = ticker.upper()
    analytics = fetch_portfolio_analytics(api_url=api_url, only={"position_alpha"})
    alpha = analytics.position_alpha
    alpha_row: PositionAlphaRow | None = None
    window_start = window_end = None
    if alpha is not None:
        window_start, window_end = alpha.start_date, alpha.end_date
        alpha_row = next((r for r in alpha.rows if (r.ticker or "").upper() == t), None)
    attribution = build_position_attribution(
        t,
        db_path=db_path,
        alpha_row=alpha_row,
        window_start=window_start,
        window_end=window_end,
        user_id=user_id,
    )
    return (
        f"{_ATTRIB_STYLE}"
        '<section class="panel"><h2>Attribution</h2>'
        '<p class="atr-sub">What drove this position\'s window performance — tracker '
        "dollar alpha vs SPY, the entry posture, and the thesis/alert/decision events "
        "inside the window. Deterministic: every clause traces to a row.</p>"
        f"{_card(attribution, show_ticker=False)}"
        "</section>"
    )


def render_book_attribution_section(attributions: list[PositionAttribution]) -> str:
    """The Portfolio tab's attribution block — pure assembly, empty string
    when there are no research positions to attribute (the section hides)."""
    if not attributions:
        return ""
    cards = "".join(_card(a, show_ticker=True) for a in attributions)
    return (
        f"{_ATTRIB_STYLE}"
        '<section class="panel"><h2>Attribution narratives</h2>'
        '<p class="atr-sub">Per research position (open lifecycle row): window dollar '
        "alpha vs the SPY counterfactual + the thesis events behind it, biggest moves "
        "first. Tracker-only holdings (index funds, cash) have no thesis to attribute "
        "and are not listed.</p>"
        f"{cards}"
        "</section>"
    )
