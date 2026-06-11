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

_PANEL_STYLE = """<style>
.vi-table { width:100%; border-collapse:collapse; font-size:var(--fs-body); }
.vi-table th, .vi-table td {
  padding:6px 10px; border-bottom:1px solid var(--border); text-align:left; }
.vi-table td.num, .vi-table th.num { text-align:right; font-variant-numeric:tabular-nums; }
.vi-sev-halt { color:var(--bad); font-weight:600; }
.vi-sev-warn { color:var(--warn); }
.vi-raw { font-family:var(--mono); font-size:var(--fs-caption); word-break:break-all; }
.vi-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.55; }
.vi-note code { background:var(--surface); padding:1px 5px; border-radius:4px; }
</style>"""

_DETAIL_LIMIT = 50


def _new_rule_rows() -> list[tuple[str, str, int]]:
    return []


def _new_detail_rows() -> list[tuple[str, str, str, str, str, str]]:
    return []


@dataclass(slots=True)
class ValidationOverview:
    """Aggregates the panel renders — split out for tests."""

    open_total: int = 0
    open_halt: int = 0
    open_warn: int = 0
    tickers_affected: int = 0
    resolved_total: int = 0
    last_raised_at: str | None = None
    # (rule, severity, count) for open issues, largest first.
    by_rule: list[tuple[str, str, int]] = field(default_factory=_new_rule_rows)
    # Latest open issues: (ticker, severity, rule, raw_value, expected, raised_at).
    detail: list[tuple[str, str, str, str, str, str]] = field(default_factory=_new_detail_rows)


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
        ov.detail = [
            (
                str(r["ticker"] or "—"),
                str(r["severity"]),
                str(r["rule"]),
                str(r["raw_value"] or ""),
                str(r["expected"] or ""),
                str(r["raised_at"] or "")[:19],
            )
            for r in conn.execute(
                "SELECT ticker, severity, rule, raw_value, expected, raised_at "
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
            f'<div class="vi-note">No open issues.{escape(resolved)} Sweep the book with '
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
            _detail_table(ov),
            "</section>",
        ]
    )


def _kpi_strip(ov: ValidationOverview) -> str:
    halt_tone = "tone-bad" if ov.open_halt else "tone-good"
    warn_tone = "tone-warn" if ov.open_warn else "tone-good"
    last = ov.last_raised_at[:10] if ov.last_raised_at else "—"
    cards = [
        f'<div class="kpi-card {halt_tone}"><div class="kpi-label">Open · halt</div>'
        f'<div class="kpi-value">{ov.open_halt:,}</div>'
        '<div class="kpi-sub">fix before trusting</div></div>',
        f'<div class="kpi-card {warn_tone}"><div class="kpi-label">Open · warn</div>'
        f'<div class="kpi-value">{ov.open_warn:,}</div>'
        '<div class="kpi-sub">review when convenient</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Tickers affected</div>'
        f'<div class="kpi-value">{ov.tickers_affected:,}</div>'
        f'<div class="kpi-sub">last raised {escape(last)}</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Resolved</div>'
        f'<div class="kpi-value">{ov.resolved_total:,}</div>'
        '<div class="kpi-sub">all-time</div></div>',
    ]
    return '<div class="kpi-strip">' + "".join(cards) + "</div>"


def _rule_table(ov: ValidationOverview) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(rule)}</td>"
        f'<td class="vi-sev-{escape(sev)}">{escape(sev)}</td>'
        f'<td class="num">{n:,}</td>'
        "</tr>"
        for rule, sev, n in ov.by_rule
    )
    return (
        "<h3>Open issues by rule</h3>"
        '<table class="vi-table"><thead><tr>'
        '<th>Rule</th><th>Severity</th><th class="num">Open</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _detail_table(ov: ValidationOverview) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(t)}</td>"
        f'<td class="vi-sev-{escape(sev)}">{escape(sev)}</td>'
        f"<td>{escape(rule)}</td>"
        f'<td class="vi-raw">{escape(raw[:160])}</td>'
        f'<td class="vi-raw">{escape(exp[:120])}</td>'
        f'<td class="num">{escape(raised[:10])}</td>'
        "</tr>"
        for t, sev, rule, raw, exp, raised in ov.detail
    )
    capped = (
        f'<div class="vi-note">Showing the latest {_DETAIL_LIMIT} of '
        f"{ov.open_total:,} open issues (halt first).</div>"
        if ov.open_total > _DETAIL_LIMIT
        else ""
    )
    return (
        "<h3>Latest open issues</h3>"
        '<table class="vi-table"><thead><tr>'
        "<th>Ticker</th><th>Severity</th><th>Rule</th><th>Value</th><th>Expected</th>"
        '<th class="num">Raised</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>{capped}"
    )
