"""Position tab: accounts, totals, transactions, open/closed decisions.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import PortfolioPositionSection
from report.renderers.workspace_sections._shared import _empty_panel, _esc, _fmt_usd, _panel_head

__all__ = [
    "_position_stat",
    "_position_tab",
]


def _position_tab(body: StringIO, pp: PortfolioPositionSection | None) -> None:
    body.write('<div class="tab-body">')
    if pp is None or not pp.held:
        _empty_panel(
            body,
            "Position",
            "The portfolio tracker shows no position in this name.",
            reason="not held",
        )
        body.write("</div>")
        return
    body.write('<div class="row-split"><div>')
    body.write('<div class="eyebrow">Your position</div>')
    body.write(
        '<h2 class="section-title">'
        f"{pp.total_quantity:.0f} shares across {len(pp.accounts)} account"
        f"{'s' if len(pp.accounts) != 1 else ''}."
        "</h2>"
    )
    # (Tracker snapshot date rides in the Accounts panel's as-of slot — P4.1.)
    body.write("</div></div>")

    body.write('<div class="position-stats">')
    _position_stat(body, "Cost basis", _fmt_usd(pp.total_cost_basis))
    _position_stat(body, "Market value", _fmt_usd(pp.total_market_value))
    pnl_tone = ""
    if pp.total_unrealized_pnl is not None:
        pnl_tone = "pos" if pp.total_unrealized_pnl >= 0 else "neg"
    _position_stat(body, "Unrealized P&L", _fmt_usd(pp.total_unrealized_pnl), tone=pnl_tone)
    if pp.total_unrealized_pct is not None:
        _position_stat(
            body,
            "Return",
            f"{pp.total_unrealized_pct * 100:+.1f}%",
            tone=pnl_tone,
        )
    body.write("</div>")

    if pp.accounts:
        body.write(
            _panel_head(
                "Accounts",
                sub=f"{len(pp.accounts)} rows",
                as_of=pp.position_as_of.isoformat() if pp.position_as_of is not None else None,
            )
            + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Account</th>"
            '<th class="num">Shares</th>'
            '<th class="num">Cost basis</th>'
            '<th class="num">Cost source</th>'
            '<th class="num">Market value</th>'
            '<th class="num">P&L</th>'
            '<th class="num">Return</th></tr></thead><tbody>'
        )
        for a in pp.accounts:
            pnl_cls = ""
            if a.unrealized_pnl is not None:
                pnl_cls = " pos" if a.unrealized_pnl >= 0 else " neg"
            body.write(f"<tr><td>{_esc(a.account_name)}</td>")
            body.write(f'<td class="num">{a.quantity:.0f}</td>')
            body.write(f'<td class="num">{_fmt_usd(a.cost_basis)}</td>')
            # Cost-basis source — 'broker' (None means broker), 'manual',
            # 'inferred_acats', etc. Surfaces data-quality at a glance.
            src = a.cost_basis_source or "broker"
            body.write(f'<td class="num muted xsmall">{_esc(src)}</td>')
            body.write(f'<td class="num">{_fmt_usd(a.market_value)}</td>')
            body.write(f'<td class="num{pnl_cls}">{_fmt_usd(a.unrealized_pnl)}</td>')
            pct = f"{a.unrealized_pct * 100:+.1f}%" if a.unrealized_pct is not None else "—"
            body.write(f'<td class="num{pnl_cls}">{_esc(pct)}</td></tr>')
        body.write("</tbody></table></div></div>")

    # Recent transactions panel — last N broker activity rows on this ticker.
    # Goes ABOVE the decisions section so the activity context is visible
    # before the analyst's own decision log.
    if pp.recent_transactions:
        body.write(
            _panel_head(
                "Recent transactions",
                sub=f"{len(pp.recent_transactions)} most-recent",
            )
            + '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
            "<th>Date</th><th>Account</th><th>Type</th>"
            '<th class="num">Shares</th><th class="num">Amount</th>'
            "</tr></thead><tbody>"
        )
        for t in pp.recent_transactions:
            body.write(
                f"<tr><td>{t.date.isoformat()}</td>"
                f"<td>{_esc(t.account_name)}</td>"
                f"<td>{_esc(t.type)}</td>"
                f'<td class="num">{t.quantity:+.0f}</td>'
                f'<td class="num">{_fmt_usd(t.amount)}</td></tr>'
            )
        body.write("</tbody></table></div></div>")

    if pp.open_decisions or pp.closed_decisions:
        body.write('<div class="grid-2col">')
        for title, decisions in (
            ("Open decisions", pp.open_decisions),
            ("Closed decisions", pp.closed_decisions),
        ):
            if not decisions:
                continue  # P4.2 hide-don't-stub: skip the empty half
            body.write(_panel_head(title, sub=f"{len(decisions)} logged"))
            for d in decisions:
                body.write('<div class="decision-card"><div class="decision-head">')
                body.write(f'<span class="decision-date">{d.decision_date.isoformat()}</span>')
                body.write(f'<span class="decision-action">{_esc(d.action)}</span>')
                if d.confidence:
                    body.write(f'<span class="decision-confidence">{_esc(d.confidence)}</span>')
                if d.outcome_status and d.outcome_status not in ("open", "None"):
                    body.write(
                        f'<span class="decision-outcome {_esc(d.outcome_status)}">'
                        f"{_esc(d.outcome_status)}</span>"
                    )
                if d.linked_brief_path:
                    # Link directly to the brief that backed this decision.
                    as_uri = d.linked_brief_path.replace("\\", "/")
                    if not as_uri.startswith(("file://", "http://", "https://")):
                        as_uri = "file:///" + as_uri.lstrip("/")
                    body.write(
                        f'<a class="decision-brief-link" href="{_esc(as_uri)}" '
                        'target="_blank" rel="noopener">brief ↗</a>'
                    )
                body.write("</div>")
                body.write(f'<p class="decision-thesis">{_esc(d.thesis)}</p>')
                # Closed-decision outcome notes/date — high-signal post-mortem.
                if d.outcome_status not in (None, "open") and (d.outcome_date or d.outcome_notes):
                    body.write('<div class="decision-outcome-block xsmall muted">')
                    if d.outcome_date is not None:
                        body.write(
                            f"<span><strong>Closed {d.outcome_date.isoformat()}.</strong> </span>"
                        )
                    if d.outcome_notes:
                        body.write(f"<span>{_esc(d.outcome_notes)}</span>")
                    body.write("</div>")
                body.write("</div>")
            body.write("</div>")
        body.write("</div>")

    body.write("</div>")


def _position_stat(body: StringIO, label: str, value: str, *, tone: str = "") -> None:
    tone_cls = f" {tone}" if tone else ""
    body.write('<div class="position-stat">')
    body.write(f'<div class="position-stat-label">{_esc(label)}</div>')
    body.write(f'<div class="position-stat-value{tone_cls}">{_esc(value)}</div>')
    body.write("</div>")
