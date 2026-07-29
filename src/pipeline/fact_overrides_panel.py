"""Fact-overrides panel — every active company-doc override, auditable.

Read-only surface for the provenance-override layer
(``directives/provenance_override_2026_06.md``): one row per ACTIVE ``fact_overrides``
record — the durable assertions where a company-published document (SEC 8-K / 10-Q /
10-K, press release, IR deck) supersedes FMP for a logical fact. Shows the logical
key, the action, the authoritative value, and the citing source so any override can
be traced back to the filing that justifies it.

Cheap DB read only (no file/xlsx access); degrades to an empty state when the
``fact_overrides`` table is absent (pre-0111 DBs).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import cast

from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg

_PANEL_STYLE = """<style>
.fov-tick { font-weight:600; white-space:nowrap; }
.fov-act-replace { color:var(--ok); font-weight:600; }
.fov-act-drop { color:var(--bad); font-weight:600; }
.fov-act-qualify { color:var(--warn); font-weight:600; }
.fov-muted { color:var(--muted); }
.fov-src { font-family:var(--mono); font-size:var(--fs-caption); color:var(--muted); white-space:nowrap; }
</style>"""


@dataclass(slots=True)
class _OverrideRow:
    ticker: str
    period_end: str
    fiscal_period_type: str
    fact_kind: str
    fact_key: str
    action: str
    value_summary: str
    source_doc_type: str
    source_ref: str
    created_by: str
    created_at: str


def _has_table(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_overrides'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _value_summary(value: object, unit: object, value_json: object) -> str:
    if value_json is not None:
        try:
            parsed = json.loads(str(value_json))
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            n = len(cast("dict[str, object]", parsed))
            return f"{n} segment{'s' if n != 1 else ''}"
    if value is not None:
        unit_s = f" {unit}" if unit else ""
        try:
            return f"{float(str(value)):,.0f}{unit_s}"
        except (TypeError, ValueError):
            return f"{value}{unit_s}"
    return "—"


def collect_rows(db_path: Path) -> list[_OverrideRow]:
    """Active overrides across all tickers (ticker, then newest first). Empty when
    the table is absent or unreadable."""
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn):
            return []
        rows = conn.execute(
            "SELECT ticker, period_end, fiscal_period_type, fact_kind, fact_key, action, "
            "value, unit, value_json, source_doc_type, source_accession, source_exhibit, "
            "created_by, created_at "
            "FROM fact_overrides WHERE status = 'active' "
            "ORDER BY ticker ASC, period_end DESC, fact_kind ASC"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: list[_OverrideRow] = []
    for r in rows:
        acc = r["source_accession"]
        exh = r["source_exhibit"]
        ref = " ".join(p for p in (str(acc) if acc else "", str(exh) if exh else "") if p)
        out.append(
            _OverrideRow(
                ticker=str(r["ticker"]),
                period_end=str(r["period_end"])[:10],
                fiscal_period_type=str(r["fiscal_period_type"]),
                fact_kind=str(r["fact_kind"]),
                fact_key=str(r["fact_key"]),
                action=str(r["action"]),
                value_summary=_value_summary(r["value"], r["unit"], r["value_json"]),
                source_doc_type=str(r["source_doc_type"]),
                source_ref=ref,
                created_by=str(r["created_by"]),
                created_at=str(r["created_at"])[:16],
            )
        )
    return out


def _kpi_strip(rows: list[_OverrideRow]) -> str:
    by_kind = {
        k: sum(1 for r in rows if r.fact_kind == k) for k in ("financial_fact", "segment", "kpi")
    }

    def card(label: str, value: int, sub: str) -> str:
        return (
            f'<div class="kpi-card"><div class="kpi-label">{escape(label)}</div>'
            f'<div class="kpi-value">{value}</div>'
            f'<div class="kpi-sub">{escape(sub)}</div></div>'
        )

    return (
        '<div class="kpi-strip">'
        + card("Active overrides", len(rows), "company-doc figures winning over FMP")
        + card("Segment", by_kind["segment"], "record/cell segment replacements")
        + card("KPI", by_kind["kpi"], "tracked-KPI overrides")
        + card("Line item", by_kind["financial_fact"], "financial_facts overrides")
        + "</div>"
    )


def _row_html(r: _OverrideRow) -> str:
    act_cls = f"fov-act-{r.action}"
    ref = f' <span class="fov-src">{escape(r.source_ref)}</span>' if r.source_ref else ""
    data = (
        lg.data_text(
            f"{r.ticker} {r.period_end} {r.fact_kind} {r.fact_key} "
            f"{r.action} {r.value_summary} {r.source_doc_type} {r.source_ref}"
        )
        + lg.data_text_key("ticker", r.ticker)
        + lg.data_text_key("period", r.period_end)
        + lg.data_text_key("kind", r.fact_kind)
        + lg.data_text_key("key", r.fact_key)
        + lg.data_text_key("action", r.action)
        + lg.data_text_key("by", r.created_by)
    )
    return (
        f"<tr{data}>"
        f'<td class="fov-tick">{escape(r.ticker)}</td>'
        f"<td>{escape(r.period_end)} {escape(r.fiscal_period_type)}</td>"
        f"<td>{escape(r.fact_kind)}</td>"
        f"<td>{escape(r.fact_key)}</td>"
        f'<td><span class="{act_cls}">{escape(r.action)}</span></td>'
        f"<td>{escape(r.value_summary)}</td>"
        f"<td>{escape(r.source_doc_type)}{ref}</td>"
        f'<td class="fov-muted">{escape(r.created_by)}</td>'
        "</tr>"
    )


def render_fact_overrides_panel(db_path: Path) -> str:
    """The Overrides sub-panel: KPI strip + a row per active override."""
    rows = collect_rows(db_path)
    if not rows:
        return (
            '<section class="panel"><h2>Overrides</h2>'
            '<p class="muted">No active company-doc overrides. Record one with '
            "<code>python execution/record_fact_override.py</code> or auto-extract from an "
            "8-K with <code>python execution/extract_8k_overrides.py --apply</code>.</p></section>"
        )
    table_rows = "".join(_row_html(r) for r in rows)
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Overrides</h2>',
            '<p class="sub">Durable company-document overrides that supersede FMP at '
            "read/resolve time (segments, KPIs, line items). Each cites the filing that "
            "justifies it. See <code>directives/provenance_override_2026_06.md</code>.</p>",
            _kpi_strip(rows),
            lg.grid_open(),
            lg.filter_bar(len(rows), noun="overrides", placeholder="Filter by ticker / key…"),
            '<table class="p-table"><thead><tr>'
            + lg.th("Ticker", "ticker", "text", num=False)
            + lg.th("Period", "period", "text", num=False)
            + lg.th("Kind", "kind", "text", num=False)
            + lg.th("Key", "key", "text", num=False)
            + lg.th("Action", "action", "text", num=False)
            + "<th>Value</th><th>Source</th>"
            + lg.th("By", "by", "text", num=False)
            + "</tr></thead><tbody>",
            table_rows,
            "</tbody></table>",
            lg.grid_close(),
            "</section>",
        ]
    )
