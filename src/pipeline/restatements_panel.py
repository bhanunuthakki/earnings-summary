"""Restatements panel for the Governance theme (P3.5): "was X, now Y".

The restatement detector chains a newer filing's value to the row it
supersedes (``supersedes_id``, 0054); this panel is the read side — every
place a later filing changed an already-reported number, with both values,
the delta, and links into the ``/source/<doc_id>`` viewers for the old and
new documents. Same-value re-reports (a 10-K confirming a 10-Q) are
counted but not listed: the analyst cares about the rows where the number
actually moved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

_PANEL_STYLE = """<style>
.rs-table { width:100%; border-collapse:collapse; font-size:var(--fs-body); }
.rs-table th, .rs-table td {
  padding:6px 10px; border-bottom:1px solid var(--border); text-align:left; }
.rs-table td.num, .rs-table th.num { text-align:right; font-variant-numeric:tabular-nums; }
.rs-was { color:var(--muted); text-decoration:line-through; }
.rs-now { font-weight:600; }
.rs-up { color:var(--ok); }
.rs-down { color:var(--bad); }
.rs-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.55; }
.rs-note code { background:var(--surface); padding:1px 5px; border-radius:4px; }
.rs-doc { font-size:var(--fs-caption); font-family:var(--mono); }
</style>"""

_LIMIT = 50


@dataclass(slots=True)
class RestatementRow:
    ticker: str
    line_item: str
    period_end: str  # YYYY-MM-DD
    fiscal_period_type: str
    old_value: float
    new_value: float
    old_doc_id: int
    new_doc_id: int
    new_doc_type: str | None
    new_accession: str | None
    new_filing_date: str | None


def _new_rows() -> list[RestatementRow]:
    return []


@dataclass(slots=True)
class RestatementOverview:
    chains_total: int = 0
    value_changed: int = 0
    tickers_affected: int = 0
    kpi_chains: int = 0
    rows: list[RestatementRow] = field(default_factory=_new_rows)


def load_restatements(db_path: Path, limit: int = _LIMIT) -> RestatementOverview | None:
    """Aggregate the financial_facts supersede chains; None when the table or
    the supersedes_id column is missing (pre-0054 DBs)."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(financial_facts)")}
        if "supersedes_id" not in cols:
            return None
        ov = RestatementOverview()
        ov.chains_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM financial_facts WHERE supersedes_id IS NOT NULL"
            ).fetchone()[0]
        )
        head = conn.execute(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT new.ticker) AS t "
            "FROM financial_facts new JOIN financial_facts old ON old.id = new.supersedes_id "
            "WHERE CAST(new.value AS REAL) != CAST(old.value AS REAL)"
        ).fetchone()
        ov.value_changed = int(head["n"] or 0)
        ov.tickers_affected = int(head["t"] or 0)
        try:
            ov.kpi_chains = int(
                conn.execute(
                    "SELECT COUNT(*) FROM kpi_facts WHERE supersedes_id IS NOT NULL"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            ov.kpi_chains = 0

        doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        identity = (
            "d.accession_number, d.filing_date"
            if "accession_number" in doc_cols
            else "NULL AS accession_number, NULL AS filing_date"
        )
        for r in conn.execute(
            f"""
            SELECT new.ticker, new.line_item, new.period_end, new.fiscal_period_type,
                   CAST(old.value AS REAL) AS old_value, CAST(new.value AS REAL) AS new_value,
                   old.source_doc_id AS old_doc_id, new.source_doc_id AS new_doc_id,
                   d.doc_type AS new_doc_type, {identity}
            FROM financial_facts new
            JOIN financial_facts old ON old.id = new.supersedes_id
            LEFT JOIN documents d ON d.id = new.source_doc_id
            WHERE CAST(new.value AS REAL) != CAST(old.value AS REAL)
            ORDER BY new.id DESC LIMIT ?
            """,
            (limit,),
        ):
            ov.rows.append(
                RestatementRow(
                    ticker=str(r["ticker"]),
                    line_item=str(r["line_item"]),
                    period_end=str(r["period_end"])[:10],
                    fiscal_period_type=str(r["fiscal_period_type"]),
                    old_value=float(r["old_value"]),
                    new_value=float(r["new_value"]),
                    old_doc_id=int(r["old_doc_id"]),
                    new_doc_id=int(r["new_doc_id"]),
                    new_doc_type=str(r["new_doc_type"]) if r["new_doc_type"] is not None else None,
                    new_accession=(
                        str(r["accession_number"]) if r["accession_number"] is not None else None
                    ),
                    new_filing_date=(
                        str(r["filing_date"]) if r["filing_date"] is not None else None
                    ),
                )
            )
        return ov
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def render_restatements_panel(db_path: Path) -> str:
    """The Restatements tab fragment: was → now across every supersede chain."""
    ov = load_restatements(db_path)
    if ov is None:
        return (
            '<section class="panel"><h2>Restatements</h2>'
            '<p class="muted">No restatement chains in this DB — '
            "run <code>alembic upgrade head</code> (0054 adds supersedes_id).</p></section>"
        )
    if ov.chains_total == 0:
        return (
            _PANEL_STYLE + '<section class="panel"><h2>Restatements</h2>'
            '<p class="sub">"was X, now Y" — every place a later filing changed an '
            "already-reported number.</p>"
            '<div class="rs-note">No supersede chains yet. The restatement detector links '
            "them as later filings re-report existing periods.</div></section>"
        )
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Restatements</h2>',
            '<p class="sub">"was X, now Y" — every place a later filing changed an '
            "already-reported number. Same-value re-reports (a 10-K confirming a 10-Q) "
            "are chained but not listed.</p>",
            _kpi_strip(ov),
            _rows_table(ov),
            "</section>",
        ]
    )


def _kpi_strip(ov: RestatementOverview) -> str:
    cards = [
        '<div class="kpi-card"><div class="kpi-label">Value changed</div>'
        f'<div class="kpi-value">{ov.value_changed:,}</div>'
        '<div class="kpi-sub">was ≠ now</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Chains total</div>'
        f'<div class="kpi-value">{ov.chains_total:,}</div>'
        '<div class="kpi-sub">incl. same-value re-reports</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Tickers</div>'
        f'<div class="kpi-value">{ov.tickers_affected:,}</div>'
        '<div class="kpi-sub">with changed values</div></div>',
        '<div class="kpi-card"><div class="kpi-label">KPI chains</div>'
        f'<div class="kpi-value">{ov.kpi_chains:,}</div>'
        '<div class="kpi-sub">kpi_facts side</div></div>',
    ]
    return '<div class="kpi-strip">' + "".join(cards) + "</div>"


def _fmt(v: float) -> str:
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:,.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.1f}M"
    if abs(v) >= 1_000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _rows_table(ov: RestatementOverview) -> str:
    body: list[str] = []
    for r in ov.rows:
        delta_cls = "rs-up" if r.new_value > r.old_value else "rs-down"
        pct = (
            f"{(r.new_value / r.old_value - 1) * 100:+.1f}%"
            if r.old_value not in (0, 0.0)
            else "n/m"
        )
        doc_bits = r.new_doc_type or ""
        if r.new_accession:
            doc_bits += f" · {r.new_accession}"
            if r.new_filing_date:
                doc_bits += f" · filed {r.new_filing_date}"
        body.append(
            "<tr>"
            f"<td>{escape(r.ticker)}</td>"
            f"<td>{escape(r.line_item)}</td>"
            f'<td class="num">{escape(r.period_end)} {escape(r.fiscal_period_type)}</td>'
            f'<td class="num rs-was">{_fmt(r.old_value)}</td>'
            f'<td class="num rs-now">{_fmt(r.new_value)}</td>'
            f'<td class="num {delta_cls}">{escape(pct)}</td>'
            f'<td class="rs-doc">{escape(doc_bits.strip(" ·"))}</td>'
            f'<td class="rs-doc"><a href="/source/{r.old_doc_id}">was ↗</a> · '
            f'<a href="/source/{r.new_doc_id}">now ↗</a></td>'
            "</tr>"
        )
    capped = (
        f'<div class="rs-note">Showing the latest {len(ov.rows)} of '
        f"{ov.value_changed:,} value-changed restatements.</div>"
        if ov.value_changed > len(ov.rows)
        else ""
    )
    return (
        '<table class="rs-table"><thead><tr>'
        "<th>Ticker</th><th>Line item</th>"
        '<th class="num">Period</th><th class="num">Was</th><th class="num">Now</th>'
        '<th class="num">Δ</th><th>New filing</th><th>Open</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>{capped}"
    )
