"""Validation / data-quality panel for the command-center shell (P3.4).

Surfaces the ``validation_issues`` table — the data-quality ledger every
write path appends to (the validation engine's range / magnitude-jump /
source-disagreement sweeps, plus persist-time unit-mismatch and
plausible-range flags from kpi_persistence) — which previously rendered
only per-ticker inside workspace reports (§Provenance). One Governance tab
answers "is the data clean?" across the whole book: an open-issues KPI
strip, a per-rule breakdown, and the latest open issues in detail.

Reads the table directly (same degrade-to-empty-state contract as the
sibling panels): no rows is a real state, not an error — the engine runs
on demand via ``python execution/run_validation_engine.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from ui import living_grid as lg
from ui.controls import prov_row, prov_severity_tick

_PANEL_STYLE = """<style>
.vi-raw { font-family:var(--mono); font-size:var(--fs-caption); word-break:break-all; }
.vi-note { margin-top:14px; line-height:1.55; }
.vi-note code { background:var(--surface); padding:1px 5px; border-radius:var(--radius); }
</style>"""

_DETAIL_LIMIT = 50


def _new_rule_rows() -> list[tuple[str, str, int]]:
    return []


def _new_detail_rows() -> list[tuple[str, str, str, str, str, str, int]]:
    return []


@dataclass(slots=True)
class ValidationOverview:
    """Aggregates the panel renders — split out for tests."""

    open_total: int = 0
    open_halt: int = 0
    open_warn: int = 0
    tickers_affected: int = 0
    resolved_total: int = 0
    # Open cross-source disagreements still awaiting a human call after the
    # reconciler auto-resolved the near-agreements (pipeline.reader_tier_audit).
    # Surfaced as its own KPI card because it's the residual data-quality debt the
    # EDGAR backfill left — the number to watch trend down.
    open_source_disagreements: int = 0
    last_raised_at: str | None = None
    # (rule, severity, count) for open issues, largest first.
    by_rule: list[tuple[str, str, int]] = field(default_factory=_new_rule_rows)
    # Latest open issues: (ticker, severity, rule, raw_value, expected, raised_at, id).
    # ``id`` is appended LAST so positional consumers (tests) keep their indices.
    detail: list[tuple[str, str, str, str, str, str, int]] = field(default_factory=_new_detail_rows)


def load_validation_overview(db_path: Path) -> ValidationOverview | None:
    """Aggregate validation_issues for the panel; None when the table is absent."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        ov = ValidationOverview()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  SUM(CASE WHEN severity = 'halt' THEN 1 ELSE 0 END) AS halt_n, "
            "  SUM(CASE WHEN severity = 'warn' THEN 1 ELSE 0 END) AS warn_n, "
            "  COUNT(DISTINCT ticker) AS tickers, "
            "  MAX(raised_at) AS last_raised "
            "FROM validation_issues WHERE resolved_at IS NULL"
        ).fetchone()
        ov.open_total = int(row["total"] or 0)
        ov.open_halt = int(row["halt_n"] or 0)
        ov.open_warn = int(row["warn_n"] or 0)
        ov.tickers_affected = int(row["tickers"] or 0)
        ov.last_raised_at = str(row["last_raised"]) if row["last_raised"] is not None else None
        ov.resolved_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM validation_issues WHERE resolved_at IS NOT NULL"
            ).fetchone()[0]
        )
        ov.by_rule = [
            (str(r["rule"]), str(r["severity"]), int(r["n"]))
            for r in conn.execute(
                "SELECT rule, severity, COUNT(*) AS n FROM validation_issues "
                "WHERE resolved_at IS NULL GROUP BY rule, severity ORDER BY n DESC"
            )
        ]
        # Residual open cross-source disagreements — summed across severities from
        # the per-rule breakdown we already loaded (no extra query).
        ov.open_source_disagreements = sum(
            n for rule, _sev, n in ov.by_rule if rule == "source_disagreement"
        )
        ov.detail = [
            (
                str(r["ticker"] or "—"),
                str(r["severity"]),
                str(r["rule"]),
                str(r["raw_value"] or ""),
                str(r["expected"] or ""),
                str(r["raised_at"] or "")[:19],
                int(r["id"]),
            )
            for r in conn.execute(
                "SELECT id, ticker, severity, rule, raw_value, expected, raised_at "
                "FROM validation_issues WHERE resolved_at IS NULL "
                "ORDER BY CASE severity WHEN 'halt' THEN 0 ELSE 1 END, raised_at DESC "
                "LIMIT ?",
                (_DETAIL_LIMIT,),
            )
        ]
        return ov
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def render_validation_panel(db_path: Path) -> str:
    """The Validation tab fragment: data-quality state across the whole book."""
    ov = load_validation_overview(db_path)
    if ov is None:
        return (
            '<section class="panel"><h2>Validation</h2>'
            '<p class="muted">No <code>validation_issues</code> table in this DB — '
            "run <code>alembic upgrade head</code>.</p></section>"
        )
    if ov.open_total == 0:
        resolved = (
            f" {ov.resolved_total:,} previously raised issue(s) are resolved."
            if ov.resolved_total
            else ""
        )
        return (
            _PANEL_STYLE + '<section class="panel"><h2>Validation</h2>'
            '<p class="sub">Data-quality issues raised by the validation engine and the '
            "persist-time sanity checks (plausible ranges, magnitude jumps, source "
            "disagreement, unit mismatches).</p>"
            f'<div class="vi-note k-well">No open issues.{escape(resolved)} Sweep the book with '
            "<code>python execution/run_validation_engine.py</code> after big ingests "
            "to keep this honest.</div></section>"
        )
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Validation</h2>',
            '<p class="sub">Open data-quality issues across every fact table — range '
            "violations, magnitude jumps, cross-source disagreement, unit mismatches. "
            "<strong>halt</strong> severity means a value is wildly implausible and "
            "should be fixed before it feeds analysis.</p>",
            _kpi_strip(ov),
            _rule_table(ov),
            _detail_section(ov),
            "</section>",
            _RESOLVE_SCRIPT,
        ]
    )


def _kpi_strip(ov: ValidationOverview) -> str:
    halt_tone = "tone-bad" if ov.open_halt else "tone-good"
    warn_tone = "tone-warn" if ov.open_warn else "tone-good"
    last = ov.last_raised_at[:10] if ov.last_raised_at else "—"
    cards = [
        f'<div class="kpi-card {halt_tone}"><div class="kpi-label">Open · halt</div>'
        f'<div class="kpi-value" data-vi-count="halt">{ov.open_halt:,}</div>'
        '<div class="kpi-sub">fix before trusting</div></div>',
        f'<div class="kpi-card {warn_tone}"><div class="kpi-label">Open · warn</div>'
        f'<div class="kpi-value" data-vi-count="warn">{ov.open_warn:,}</div>'
        '<div class="kpi-sub">review when convenient</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Open · source disagreement</div>'
        f'<div class="kpi-value">{ov.open_source_disagreements:,}</div>'
        '<div class="kpi-sub">cross-source, awaiting review</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Tickers affected</div>'
        f'<div class="kpi-value">{ov.tickers_affected:,}</div>'
        f'<div class="kpi-sub">last raised {escape(last)}</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Resolved</div>'
        f'<div class="kpi-value" data-vi-count="resolved">{ov.resolved_total:,}</div>'
        '<div class="kpi-sub">all-time</div></div>',
    ]
    return '<div class="kpi-strip">' + "".join(cards) + "</div>"


def _rule_table(ov: ValidationOverview) -> str:
    rows = "".join(
        f"<tr{lg.data_text(rule + ' ' + sev)}"
        f"{lg.data_text_key('rule', rule)}{lg.data_text_key('sev', sev)}{lg.data_num('open', n)}>"
        f"<td>{escape(rule)}</td>"
        f"<td>{prov_severity_tick(sev)}</td>"
        f'<td class="num">{n:,}</td>'
        "</tr>"
        for rule, sev, n in ov.by_rule
    )
    return (
        "<h3>Open issues by rule</h3>"
        + lg.grid_open()
        + lg.filter_bar(len(ov.by_rule), noun="rules")
        + '<table class="p-table"><thead><tr>'
        + lg.th("Rule", "rule", "text", num=False)
        + lg.th("Severity", "sev", "text", num=False)
        + lg.th("Open", "open", "num")
        + f"</tr></thead><tbody>{rows}</tbody></table>"
        + lg.grid_close()
    )


def _detail_section(ov: ValidationOverview) -> str:
    """The latest open issues as actionable provenance rows (design_language
    §10; Law "provenance is actionable"): each issue is one :func:`prov_row` —
    a severity tick + ``rule · ticker`` label + the offending-value→expected
    note + a relative raised stamp — with an inline **Resolve** action that
    POSTs ``/actions/resolve-issue`` for that row's id and drops the row."""
    rows = "".join(
        prov_row(
            f"{rule} · {ticker}",
            severity=sev,
            stamp=raised or None,
            stamp_prefix="raised ",
            note=_raw_expected_note(raw, exp),
            actions=_resolve_button(issue_id, sev),
        )
        for ticker, sev, rule, raw, exp, raised, issue_id in ov.detail
    )
    capped = (
        f'<div class="vi-note k-well">Showing the latest {_DETAIL_LIMIT} of '
        f"{ov.open_total:,} open issues (halt first).</div>"
        if ov.open_total > _DETAIL_LIMIT
        else ""
    )
    return f"<h3>Latest open issues</h3>{rows}{capped}"


def _raw_expected_note(raw: str, exp: str) -> str | None:
    """The offending value → what was expected, as one plain-text aside (prov_row
    escapes it). ``None`` when neither side carries text."""
    parts = [p for p in (raw[:160], exp[:120]) if p]
    return " → ".join(parts) or None


def _resolve_button(issue_id: int, severity: str) -> str:
    """The per-row Resolve control. Built by hand (NOT ``prov_action``'s button
    mode) so it carries ``data-resolve-issue`` rather than ``data-prov-post`` —
    the latter is hijacked by ``pipeline.peeks``' global listener, which expects
    a streaming-job response and would try to stream this synchronous route.
    ``data-severity`` lets the listener decrement the matching open-count KPI on
    success."""
    return (
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        f'data-resolve-issue="{int(issue_id)}" '
        f'data-severity="{escape(severity, quote=True)}" '
        'title="Mark this issue resolved">Resolve</button>'
    )


# One guarded document-level listener (re-injected fragments never double-wire —
# the command-center shell re-executes a panel's <script> on every load, and the
# Provenance console composes this panel). A click on a Resolve button POSTs the
# SYNCHRONOUS /actions/resolve-issue route (NOT the streaming data-prov-post
# path) and, on success, decrements the matching open-count KPI, bumps Resolved,
# and fades + removes the row. Distinct attribute (data-resolve-issue) keeps it
# clear of peeks.py's button[data-prov-post] streaming listener.
_RESOLVE_SCRIPT = """<script>
(function () {
  if (window.__ccResolveWired) return;
  window.__ccResolveWired = true;
  function bump(sev, delta) {
    var el = document.querySelector('[data-vi-count="' + sev + '"]');
    if (!el) return;
    var n = parseInt(el.textContent.replace(/[^0-9-]/g, ''), 10);
    if (isNaN(n)) return;
    el.textContent = (n + delta).toLocaleString();
  }
  document.addEventListener('click', function (ev) {
    var btn = ev.target && ev.target.closest
      ? ev.target.closest('button[data-resolve-issue]') : null;
    if (!btn || btn.disabled) return;
    var id = parseInt(btn.getAttribute('data-resolve-issue'), 10);
    if (isNaN(id)) return;
    var sev = btn.getAttribute('data-severity') || '';
    btn.disabled = true;
    btn.textContent = 'Resolving\\u2026';
    fetch('/actions/resolve-issue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue_id: id })
    }).then(function (resp) {
      if (!resp.ok) {
        btn.disabled = false;
        btn.textContent = 'Retry resolve';
        btn.setAttribute('aria-invalid', 'true');
        return;
      }
      if (sev === 'halt' || sev === 'warn') bump(sev, -1);
      bump('resolved', 1);
      var wrap = btn.closest('.k-prov');
      if (!wrap) return;
      wrap.style.transition = 'opacity 0.25s';
      wrap.style.opacity = '0';
      setTimeout(function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); }, 280);
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = 'Retry resolve';
    });
  });
})();
</script>"""
