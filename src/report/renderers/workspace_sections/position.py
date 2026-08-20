"""Position tab: accounts, totals, transactions, open/closed decisions.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO

from report.models import PortfolioPositionSection, SectionStatus
from report.renderers.workspace_data import StandingRulesPanel
from report.renderers.workspace_sections._shared import _empty_panel, _esc, _fmt_usd, _panel_head
from ui import living_grid as lg

__all__ = [
    "_position_coaching",
    "_position_stat",
    "_position_tab",
    "_standing_rules_block",
]


# The research server the report deep-links into (report opens via file://,
# so the doorway must be absolute) — same hardcoded base the chat drawer /
# sources tab use (workspace_sections/boot.py, workspace_sections/sources.py).
# The Socratic think-through page is the deterministic per-ticker decision
# surface every other "review this name" doorway in the workspace already
# points at (chat drawer's "think it through", the console's Memos panel).
def _position_tab(
    body: StringIO,
    pp: PortfolioPositionSection | None,
    *,
    ticker: str = "",
    position_review_count: int = 0,
    graded_sell_line: str | None = None,
    standing_rules: StandingRulesPanel | None = None,
    you_said_html: str | None = None,
) -> None:
    body.write('<div class="tab-body">')
    has_history = pp is not None and bool(
        pp.recent_transactions or pp.open_decisions or pp.closed_decisions
    )
    if pp is not None and pp.missing is not None and not pp.held:
        _empty_panel(
            body,
            "Position",
            f"Broker data unavailable: {pp.missing.detail or 'Portfolio tracker source is unavailable.'}",
            reason="source unavailable",
        )
        _source_warnings(body, pp)
        if has_history:
            body.write('<div class="eyebrow">Historical position evidence</div>')
            body.write(
                '<h2 class="section-title">Source unavailable; preserving canonical history.</h2>'
            )
            if pp.history_error:
                body.write(
                    f'<p class="muted xsmall">History evidence: {_esc(pp.history_error)}</p>'
                )
            _history_only(body, pp)
        if standing_rules is not None and bool(standing_rules.rows):
            _standing_rules_block(body, standing_rules)
        body.write("</div>")
        return
    held = pp is not None and pp.held
    has_standing_rules = standing_rules is not None and bool(standing_rules.rows)
    if not held and not has_standing_rules and not has_history:
        _empty_panel(
            body,
            "Position",
            (
                "No current position was observed, but canonical transaction/open/closed "
                "history is unavailable."
                if pp is not None and pp.status is SectionStatus.PARTIAL
                else "The portfolio tracker shows no position in this name."
            ),
            reason=(
                "history unavailable"
                if pp is not None and pp.status is SectionStatus.PARTIAL
                else "not held"
            ),
        )
        if pp is not None:
            _source_warnings(body, pp)
        body.write("</div>")
        return

    if not held and has_history and pp is not None:
        body.write('<div class="eyebrow">Historical position evidence</div>')
        body.write(
            '<h2 class="section-title">No current broker position; preserving canonical history.</h2>'
        )
        if pp.history_error:
            body.write(f'<p class="muted xsmall">History evidence: {_esc(pp.history_error)}</p>')
        _source_warnings(body, pp)
        _history_only(body, pp)
        _standing_rules_block(body, standing_rules)
        body.write("</div>")
        return

    if pp is not None and pp.held:
        body.write('<div class="row-split"><div>')
        body.write('<div class="eyebrow">Your position</div>')
        if pp.source_is_stale:
            body.write('<div class="k-pill k-pill-warn">Source evidence stale</div>')
        _source_warnings(body, pp)
        if pp.history_state != "available":
            body.write(
                '<div class="k-pill k-pill-warn">Transaction/decision history unavailable or partial</div>'
            )
        body.write(
            '<h2 class="section-title">'
            f"{pp.total_quantity:g} shares across {len(pp.accounts)} account"
            f"{'s' if len(pp.accounts) != 1 else ''}."
            "</h2>"
        )
        # (Tracker snapshot date rides in the Accounts panel's as-of slot — P4.1.)
        body.write("</div></div>")

        # "You said" strip (owner-ratified design review, 2026-08-02): the
        # owner's own last decision on this name, right below the position
        # header — pre-computed by the caller (pipeline.you_said), a pure
        # string here like graded_sell_line.
        if you_said_html:
            body.write(f'<div class="position-yousaid">{you_said_html}</div>')

        _position_coaching(body, ticker, position_review_count, graded_sell_line)

        if pp.history_state != "available" and pp.history_error:
            body.write(f'<p class="muted xsmall">History evidence: {_esc(pp.history_error)}</p>')

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
            )
            body.write(lg.grid_open())
            body.write(lg.filter_bar(len(pp.accounts), noun="accounts"))
            body.write(
                '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
                + lg.th("Account", "account", "text", num=False)
                + lg.th("Shares", "shares", "num")
                + lg.th("Cost basis", "cost", "num")
                + lg.th("Cost source", "source", "text")
                + lg.th("Market value", "mv", "num")
                + lg.th("P&L", "pnl", "num")
                + lg.th("Return", "ret", "num")
                + "</tr></thead><tbody>"
            )
            for a in pp.accounts:
                pnl_cls = ""
                if a.unrealized_pnl is not None:
                    pnl_cls = " pos" if a.unrealized_pnl >= 0 else " neg"
                src = a.cost_basis_source or "broker"
                data = (
                    lg.data_text(f"{a.account_name} {src}")
                    + lg.data_text_key("account", a.account_name)
                    + lg.data_num("shares", a.quantity)
                    + lg.data_num("cost", a.cost_basis)
                    + lg.data_text_key("source", src)
                    + lg.data_num("mv", a.market_value)
                    + lg.data_num("pnl", a.unrealized_pnl)
                    + lg.data_num("ret", a.unrealized_pct)
                )
                body.write(f"<tr{data}><td>{_esc(a.account_name)}</td>")
                body.write(f'<td class="num">{a.quantity:.0f}</td>')
                body.write(f'<td class="num">{_fmt_usd(a.cost_basis)}</td>')
                # Cost-basis source — 'broker' (None means broker), 'manual',
                # 'inferred_acats', etc. Surfaces data-quality at a glance.
                body.write(f'<td class="num muted xsmall">{_esc(src)}</td>')
                body.write(f'<td class="num">{_fmt_usd(a.market_value)}</td>')
                body.write(f'<td class="num{pnl_cls}">{_fmt_usd(a.unrealized_pnl)}</td>')
                pct = f"{a.unrealized_pct * 100:+.1f}%" if a.unrealized_pct is not None else "—"
                body.write(f'<td class="num{pnl_cls}">{_esc(pct)}</td></tr>')
            body.write("</tbody></table></div>")
            body.write(lg.grid_close())
            body.write("</div>")

        # Recent transactions panel — last N broker activity rows on this ticker.
        # Goes ABOVE the decisions section so the activity context is visible
        # before the analyst's own decision log.
        if pp.recent_transactions:
            body.write(
                _panel_head(
                    "Recent transactions",
                    sub=f"{len(pp.recent_transactions)} most-recent",
                )
            )
            body.write(lg.grid_open())
            body.write(lg.filter_bar(len(pp.recent_transactions), noun="transactions"))
            body.write(
                '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr>'
                + lg.th("Date", "date", "text", num=False)
                + lg.th("Account", "account", "text", num=False)
                + lg.th("Type", "type", "text", num=False)
                + lg.th("Shares", "shares", "num")
                + lg.th("Amount", "amount", "num")
                + "</tr></thead><tbody>"
            )
            for t in pp.recent_transactions:
                iso = t.date.isoformat()
                data = (
                    lg.data_text(f"{iso} {t.account_name} {t.type}")
                    + lg.data_text_key("date", iso)
                    + lg.data_text_key("account", t.account_name)
                    + lg.data_text_key("type", t.type)
                    + lg.data_num("shares", t.quantity)
                    + lg.data_num("amount", t.amount)
                )
                body.write(
                    f"<tr{data}><td>{iso}</td>"
                    f"<td>{_esc(t.account_name)}</td>"
                    f"<td>{_esc(t.type)}</td>"
                    f'<td class="num">{t.quantity:+.0f}</td>'
                    f'<td class="num">{_fmt_usd(t.amount)}</td></tr>'
                )
            body.write("</tbody></table></div>")
            body.write(lg.grid_close())
            body.write("</div>")

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
                    if d.outcome_status not in (None, "open") and (
                        d.outcome_date or d.outcome_notes
                    ):
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
    else:
        # No broker holding on file, but standing rules exist (a sizing
        # intent recorded before an entry, or lingering after an exit) — skip
        # the position-specific stats (they don't apply) and go straight to
        # the rules so they're never invisible just because the tracker shows
        # zero shares.
        body.write('<div class="eyebrow">Standing rules</div>')
        body.write(
            '<h2 class="section-title">No broker position on file for '
            f"{_esc(ticker.upper())} &mdash; sizing intents below.</h2>"
        )

    _standing_rules_block(body, standing_rules)
    body.write("</div>")


def _position_coaching(
    body: StringIO,
    ticker: str,
    position_review_count: int,
    graded_sell_line: str | None,
) -> None:
    """Position-tab coaching lines (REQ-3/REQ-6): the behavioral-guard status,
    the graded-sells base rate when that ledger exists on this branch, and a
    doorway into the deterministic review surface for this name.

    The guard phrase is derived from ``position_review_count`` so it can never
    contradict the count rendered beside it: the behavioral guard runs *inside*
    a position review, so with zero reviews it has genuinely never fired on
    this name (the honest-zero starvation wording), and with N reviews it was
    consulted on each of them."""
    body.write('<div class="position-coaching">')
    reviews_word = "review" if position_review_count == 1 else "reviews"
    if position_review_count == 0:
        # Honest-zero starvation: no review has ever run the guard on this name.
        guard_line = "Guard: never run on this name &middot; 0 position reviews"
    else:
        guard_line = f"Guard: consulted during {position_review_count} position {reviews_word}"
    body.write(f'<p class="coaching-line muted xsmall">{guard_line}</p>')
    if graded_sell_line:
        body.write(f'<p class="coaching-line muted xsmall">{_esc(graded_sell_line)}</p>')
    if ticker:
        body.write(
            f'<a class="k-btn k-btn-quiet k-btn-sm" '
            f'href="/socratic/{_esc(ticker.upper())}" '
            f'data-server-path="/socratic/{_esc(ticker.upper())}" '
            'target="_blank" rel="noopener">Review this position &rarr;</a>'
        )
    body.write("</div>")


def _source_warnings(body: StringIO, pp: PortfolioPositionSection) -> None:
    if not pp.source_warnings:
        return
    body.write(
        '<div class="k-well position-source-warnings"><div class="eyebrow">Source warnings</div><ul>'
    )
    for warning in pp.source_warnings:
        code = _esc(str(warning.get("code") or "WARNING"))
        message = str(warning.get("message") or "Source warning")
        body.write(f'<li><span class="k-chip k-chip-warn">{code}</span> {_esc(message)}</li>')
    body.write("</ul></div>")


def _history_only(body: StringIO, pp: PortfolioPositionSection) -> None:
    if pp.recent_transactions:
        body.write(_panel_head("Recent transactions", sub=f"{len(pp.recent_transactions)} logged"))
        body.write(
            '<div class="table-scroll"><table class="tbl tbl-nowrap"><thead><tr><th>Date</th><th>Account</th><th>Type</th><th>Shares</th><th>Amount</th></tr></thead><tbody>'
        )
        for transaction in pp.recent_transactions:
            body.write(
                f"<tr><td>{transaction.date.isoformat()}</td><td>{_esc(transaction.account_name)}</td>"
                f'<td>{_esc(transaction.type)}</td><td class="num">{transaction.quantity:+.0f}</td>'
                f'<td class="num">{_fmt_usd(transaction.amount)}</td></tr>'
            )
        body.write("</tbody></table></div>")
    for title, decisions in (
        ("Open decisions", pp.open_decisions),
        ("Closed decisions", pp.closed_decisions),
    ):
        if not decisions:
            continue
        body.write(_panel_head(title, sub=f"{len(decisions)} logged"))
        for decision in decisions:
            body.write(
                f'<div class="decision-card"><div class="decision-head"><span class="decision-date">{decision.decision_date.isoformat()}</span>'
                f'<span class="decision-action">{_esc(decision.action)}</span></div><p class="decision-thesis">{_esc(decision.thesis)}</p></div>'
            )


def _position_stat(body: StringIO, label: str, value: str, *, tone: str = "") -> None:
    tone_cls = f" {tone}" if tone else ""
    body.write('<div class="position-stat">')
    body.write(f'<div class="position-stat-label">{_esc(label)}</div>')
    body.write(f'<div class="position-stat-value{tone_cls}">{_esc(value)}</div>')
    body.write("</div>")


def _fmt_intent_value(intent_kind: str, value: float | None) -> str:
    """Best-effort unit inference from the ``intent_kind`` suffix — the schema
    carries no separate units column, so ``..._pct`` reads as a percent and any
    kind naming ``price`` reads as a dollar; anything else (e.g. the free-text
    ``sizing_note`` kind, which never carries ``intent_value``) reads plain."""
    if value is None:
        return "—"
    if intent_kind.endswith("pct"):
        return f"{value:.1f}%"
    if "price" in intent_kind:
        return f"${value:,.2f}"
    return f"{value:g}"


def _standing_rules_coverage_line(standing_rules: StandingRulesPanel) -> str:
    """The dense downside/add-rung coverage read, reusing ``position_guard``'s
    own downside/add-rung pass/fail — never re-derived. ``""`` when neither
    check is available (never rendered — the caller checks truthiness)."""
    parts: list[str] = []
    if standing_rules.downside_passed is not None:
        mark = "✓" if standing_rules.downside_passed else "✗"
        parts.append(f"downside rule {mark}")
    if standing_rules.add_passed is not None:
        if standing_rules.add_passed:
            mark = "✓ (draft)" if standing_rules.add_is_draft else "✓"
        else:
            mark = "—"
        parts.append(f"add-rung {mark}")
    if not parts:
        return ""
    return f'<span class="panel-sub">{_esc(" · ".join(parts))}</span>'


def _standing_rules_block(body: StringIO, standing_rules: StandingRulesPanel | None) -> None:
    """The Position-tab 'Standing rules' block (Monthly Red Team PR10 —
    surface wiring): every ``position_sizing_intent`` row on file for this
    name — the FLKR exit ladder, RBRK/BKNG target weights, DRAFT add-rungs —
    rendered as one dense row each, plus a coverage read. Hidden when there
    are no rows on file (hide-don't-stub)."""
    if standing_rules is None or not standing_rules.rows:
        return
    coverage_html = _standing_rules_coverage_line(standing_rules)
    body.write(
        _panel_head(
            "Standing rules",
            sub_html=coverage_html or None,
            sub="every sizing intent on file for this name" if not coverage_html else None,
        )
    )
    body.write('<div class="table-scroll"><table class="tbl"><thead><tr>')
    body.write("<th>Kind</th><th>Value</th><th>Narrative</th><th>Updated</th></tr></thead><tbody>")
    for row in standing_rules.rows:
        kind_label = row.intent_kind.upper().replace("_", " ")
        draft_chip = ' <span class="k-chip">DRAFT</span>' if row.is_draft else ""
        body.write(
            f'<tr><td><span class="k-chip k-chip-mono">{_esc(kind_label)}</span>{draft_chip}</td>'
        )
        body.write(f"<td>{_esc(_fmt_intent_value(row.intent_kind, row.intent_value))}</td>")
        body.write(f"<td>{_esc(row.narrative)}</td>")
        body.write(f'<td class="muted xsmall">{row.updated_at.date().isoformat()}</td></tr>')
    body.write("</tbody></table></div>")
    body.write("</div>")
