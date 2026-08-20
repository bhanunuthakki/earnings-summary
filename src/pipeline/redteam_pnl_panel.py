"""Decision P&L (Red Team) panel — monthly_red_team.md Phase 3, PR7.

Renders ``redteam.decision_pnl``'s scored responses + the three yearly
scorecard numbers, mounted beside the existing "Coach P&L" section on the
Portfolio -> Decisions page (``pipeline.allocation_decisions_panel``). Kit-
composed (design_language / tests/test_ui_controls.py): status/direction tags
ride ``.k-chip``, the scored value rides ``.k-pill``; this module contributes
layout-only CSS.

Honesty is the point here too: an item with no resolvable price reads "not
scorable" rather than vanishing, and the three scorecard numbers show their
own "no data yet, here's what's missing" state rather than a fabricated
placeholder (see ``redteam.decision_pnl.build_yearly_scorecard``).
"""

from __future__ import annotations

from html import escape

from pipeline.analysis_styles import REDTEAM_PNL_CSS
from redteam.decision_pnl import DecisionPnlReport, DecisionPnlRow, ScorecardNumber, YearlyScorecard
from ui.controls import panel_toolbar, ticker_label

# ---------------------------------------------------------------------------
# Self-registered style module (design_language / tests/test_ui_controls.py):
# layout + tone only, every color/size/radius rides a token. Registered as
# "pipeline/redteam_pnl_panel.py" in tests/test_ui_controls.py's REGISTERED set.
# ---------------------------------------------------------------------------
_DIRECTION_TONE: dict[str, str] = {"refuted": "ok", "accepted": "accent", "deferred": "warn"}


def _status_chip(status: str) -> str:
    tone = _DIRECTION_TONE.get(status, "")
    cls = f"k-chip k-chip-{tone}".strip() if tone else "k-chip"
    return f'<span class="{cls}">{escape(status.upper())}</span>'


def _scored_pill(row: DecisionPnlRow) -> str:
    if row.scored_pct is None:
        if row.status == "deferred" and row.price_move_pct is not None:
            return f'<span class="k-pill">{row.price_move_pct:+.1%} (informational)</span>'
        return '<span class="k-pill">not scorable</span>'
    tone = "ok" if row.scored_pct >= 0 else "bad"
    return f'<span class="k-pill k-pill-{tone}">{row.scored_pct:+.1%}</span>'


def _row_html(row: DecisionPnlRow) -> str:
    subject = ticker_label(row.ticker) if row.ticker else '<span class="rtp-note">Cross-book</span>'
    head = (
        f'<div class="rtp-row">{subject}{_status_chip(row.status)}{_scored_pill(row)}'
        f'<span class="rtp-note">responded {escape(row.responded_at[:10])}</span></div>'
    )
    note = f'<p class="rtp-note">{escape(row.note)}</p>'
    return f"{head}{note}"


def _scorecard_number_html(n: ScorecardNumber) -> str:
    cls = "k-pill" if n.available else "k-pill k-pill-warn"
    pill = f'<span class="{cls}">{escape(n.value_text)}</span>'
    return (
        f'<div class="rtp-sc-row"><span class="rtp-sc-label">{escape(n.label)}</span>{pill}'
        f'<p class="rtp-sc-detail">{escape(n.detail)}</p></div>'
    )


def render_redteam_pnl_section(
    report: DecisionPnlReport | None, scorecard: YearlyScorecard | None
) -> str:
    """The Decision P&L section — always renders (never ""), mirroring the
    scorecard panel's REQ-6 posture: a missing report/scorecard is itself an
    honest state, not a silent absence."""
    toolbar = panel_toolbar("Decision P&L (Red Team)")
    head = (
        '<section class="panel rtp">'
        f"{toolbar}"
        '<p class="sub">Every REFUTE/ACCEPT/DEFER from the monthly Red Team, scored '
        f"{'' if report is None else report.min_quarters} quarters later against what the "
        "price actually did — a simple, legible price-move read, not a full weight-adjusted "
        "counterfactual (see <code>redteam.decision_pnl</code> for the exact arithmetic).</p>"
    )
    if report is None:
        body = '<p class="rtp-note">Decision P&amp;L unavailable — see logs.</p>'
    elif not report.rows:
        body = (
            f'<p class="rtp-due">0 responses due for scoring '
            f"({report.n_not_yet_due} responded but not yet {report.min_quarters} quarters "
            "old).</p>"
        )
    else:
        due_line = (
            f'<p class="rtp-due">{report.n_due} response(s) due for scoring'
            + (f" &middot; {report.n_not_yet_due} not yet due" if report.n_not_yet_due else "")
            + (
                f" &middot; {report.n_unscorable} unscorable (no price on file)"
                if report.n_unscorable
                else ""
            )
            + "</p>"
        )
        body = (
            due_line
            + '<div class="rtp-rows">'
            + "".join(_row_html(r) for r in report.rows)
            + "</div>"
        )
    sc_html = ""
    if scorecard is not None:
        sc_html = (
            '<div class="rtp-scorecard">'
            + _scorecard_number_html(scorecard.brier_trend)
            + _scorecard_number_html(scorecard.cut_discipline_hit_rate)
            + _scorecard_number_html(scorecard.rule_execution_fidelity)
            + "</div>"
        )
    return f"{head}{body}{sc_html}</section>"


__all__ = ["REDTEAM_PNL_CSS", "render_redteam_pnl_section"]
