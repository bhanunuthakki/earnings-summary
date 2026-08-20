"""Per-ticker section-coverage panel for the Governance theme (P4.2).

The hide-don't-stub policy (master build P4.2) removed the in-report
"cold ticker" stubs: a report panel with no data now simply doesn't render.
This panel is the other half of that contract — the one place that answers
"which sections are filled for which names, and where are the gaps?" so the
absence of a panel in a report is never silent.

Two substrates, both read-only:

* the latest ``output/research/<TICKER>/{date}_sections.json`` sidecar per
  ticker (written by ``build_artifacts`` on every build) for the
  spec-carried sections (earnings, say-do, financials, news, bear, …);
* direct best-effort row counts over the render-time panel tables
  (macro_sensitivities, strategic_targets, customer_concentrations,
  lease_commitments, management_commitments, decisions) — these never ride
  on the spec, so the sidecar can't see them.

Business-model-suppressed sections (``suppressed_sections_for_ticker``)
render as "n/a" rather than "missing": a bank without a lease ladder is
correct, not cold.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import cast

from identity import DEFAULT_USER_ID
from industry_classifier import (
    SECTION_CUSTOMER_CONCENTRATION,
    SECTION_LEASE_LADDER,
    SECTION_STRATEGIC_TARGETS,
    suppressed_sections_for_ticker,
)
from pipeline.operations_styles import SECTION_COVERAGE_STYLE as _PANEL_STYLE
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui import living_grid as lg

# Column order of the matrix: (key, short label). Sidecar-fed sections first,
# then the DB-probed render-time panels.
SECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("earnings", "Earnings"),
    ("saydo", "Say·Do"),
    ("financials", "Financials"),
    ("thesis", "Thesis KPIs"),
    ("news", "News"),
    ("bear", "Bear case"),
    ("company", "Company"),
    ("transcripts", "Transcripts"),
    ("saydo_verdicts", "Verdicts"),
    ("decisions", "Decisions"),
    ("macro", "Macro β"),
    ("strategic_targets", "Targets"),
    ("customer_concentration", "Cust. conc."),
    ("lease_ladder", "Leases"),
)

# DB-probed columns: column key -> (table, suppression key or None).
_DB_PROBES: dict[str, tuple[str, str | None]] = {
    "saydo_verdicts": ("management_commitments", None),
    "decisions": ("decisions", None),
    "macro": ("macro_sensitivities", None),
    "strategic_targets": ("strategic_targets", SECTION_STRATEGIC_TARGETS),
    "customer_concentration": ("customer_concentrations", SECTION_CUSTOMER_CONCENTRATION),
    "lease_ladder": ("lease_commitments", SECTION_LEASE_LADDER),
}

# Cell states.
FILLED = "filled"
EMPTY = "empty"
SUPPRESSED = "n/a"
NO_REPORT = "no report"


def _new_cells() -> dict[str, str]:
    return {}


@dataclass(slots=True)
class TickerCoverage:
    """One ticker's row in the coverage matrix."""

    ticker: str
    list_type: str
    report_date: str | None = None  # latest sections.json date, None = never built
    cells: dict[str, str] = field(default_factory=_new_cells)

    @property
    def filled_count(self) -> int:
        return sum(1 for v in self.cells.values() if v == FILLED)

    @property
    def gap_count(self) -> int:
        return sum(1 for v in self.cells.values() if v == EMPTY)


def _latest_sections_payload(
    reports_dir: Path, ticker: str
) -> tuple[str | None, dict[str, object] | None]:
    """(report_date, parsed payload) for the ticker's newest sections sidecar."""
    tdir = reports_dir / ticker
    if not tdir.is_dir():
        return None, None
    candidates = sorted(tdir.glob("*_sections.json"))
    if not candidates:
        return None, None
    latest = candidates[-1]
    try:
        payload: object = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return latest.name.split("_")[0], cast("dict[str, object]", payload)


def _tab(payload: dict[str, object], name: str) -> dict[str, object]:
    tabs = payload.get("tabs")
    if isinstance(tabs, dict):
        tab = cast("dict[str, object]", tabs).get(name)
        if isinstance(tab, dict):
            return cast("dict[str, object]", tab)
    return {}


def _nonempty_list(tab: dict[str, object], *keys: str) -> bool:
    return any(isinstance(tab.get(k), list) and cast("list[object]", tab[k]) for k in keys)


def _sidecar_cells(payload: dict[str, object]) -> dict[str, str]:
    """Filled/empty per spec-carried section, judged on CONTENT (not just the
    status enum — a section can be status=ok with nothing in it)."""
    earnings = _tab(payload, "earnings")
    saydo = _tab(payload, "saydo")
    fins = _tab(payload, "financials")
    thesis = _tab(payload, "thesis_details")
    news = _tab(payload, "news")
    bear = _tab(payload, "nuance")
    company = _tab(payload, "company_description")
    transcripts = _tab(payload, "transcripts")
    news_md = news.get("content_md")
    return {
        "earnings": FILLED
        if _nonempty_list(earnings, "full_quarters", "digest_quarters")
        else EMPTY,
        "saydo": FILLED if _nonempty_list(saydo, "cards") else EMPTY,
        "financials": FILLED if _nonempty_list(fins, "line_items") else EMPTY,
        "thesis": FILLED if _nonempty_list(thesis, "kpi_ledger") else EMPTY,
        "news": FILLED if isinstance(news_md, str) and news_md.strip() else EMPTY,
        "bear": FILLED
        if (_nonempty_list(bear, "failure_modes") or bear.get("most_underweighted"))
        else EMPTY,
        "company": FILLED if company.get("elevator_pitch") else EMPTY,
        "transcripts": FILLED if _nonempty_list(transcripts, "quarters") else EMPTY,
    }


def _db_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """ticker -> row count for one panel table; {} when the table is absent."""
    try:
        rows = conn.execute(f"SELECT ticker, COUNT(*) AS n FROM {table} GROUP BY ticker").fetchall()
    except sqlite3.Error:
        return {}
    return {str(r[0]).upper(): int(r[1]) for r in rows}


def load_section_coverage(
    *,
    db_path: Path,
    repo_root: Path,
    user_id: str = DEFAULT_USER_ID,
) -> list[TickerCoverage]:
    """The coverage matrix for every first-class tracked name.

    First-class = portfolio / watchlist / evaluation (index members are a
    screening universe; no reports are built for them). Sorted: portfolio
    first, then watchlist, then evaluation; most gaps first within a list.
    """
    if not db_path.exists():
        return []
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        return []
    try:
        try:
            tracked = conn.execute(
                "SELECT ticker, list_type FROM tracked_companies "
                "WHERE archived_at IS NULL AND user_id = ? "
                "AND list_type IN ('portfolio', 'watchlist', 'evaluation') "
                "ORDER BY ticker",
                (user_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        probe_counts = {col: _db_counts(conn, table) for col, (table, _skey) in _DB_PROBES.items()}
    finally:
        conn.close()

    reports_dir = Path(repo_root) / "output" / "research"
    out: list[TickerCoverage] = []
    for raw_ticker, list_type in tracked:
        ticker = str(raw_ticker).upper()
        row = TickerCoverage(ticker=ticker, list_type=str(list_type))
        report_date, payload = _latest_sections_payload(reports_dir, ticker)
        row.report_date = report_date
        if payload is not None:
            row.cells.update(_sidecar_cells(payload))
        else:
            for key, _label in SECTION_COLUMNS:
                if key not in _DB_PROBES:
                    row.cells[key] = NO_REPORT
        suppressed = suppressed_sections_for_ticker(ticker, Path(repo_root))
        for col, (_table, skey) in _DB_PROBES.items():
            if skey is not None and skey in suppressed:
                row.cells[col] = SUPPRESSED
            else:
                row.cells[col] = FILLED if probe_counts[col].get(ticker, 0) > 0 else EMPTY
        out.append(row)

    list_rank = {"portfolio": 0, "watchlist": 1, "evaluation": 2}
    out.sort(key=lambda r: (list_rank.get(r.list_type, 9), -r.gap_count, r.ticker))
    return out


_CELL_GLYPH: dict[str, tuple[str, str, str]] = {
    # state -> (glyph, css class, hover)
    FILLED: ("&#9679;", "sc-filled", "populated"),
    EMPTY: ("&#9675;", "sc-empty", "no data"),
    SUPPRESSED: ("&mdash;", "sc-na", "not applicable to this business model"),
    NO_REPORT: ("&middot;", "sc-noreport", "no report built yet"),
}


def render_section_coverage_panel(
    db_path: Path,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    """The Governance → Coverage tab fragment."""
    rows = load_section_coverage(db_path=db_path, repo_root=repo_root, user_id=user_id)
    parts: list[str] = [_PANEL_STYLE, '<section class="panel"><h2>Section coverage</h2>']
    if not rows:
        parts.append(
            '<p class="sc-note">No tracked names found — the coverage matrix '
            "fills in once companies are onboarded and reports are built.</p></section>"
        )
        return "".join(parts)

    built = sum(1 for r in rows if r.report_date is not None)
    total_gaps = sum(r.gap_count for r in rows)
    fully = sum(1 for r in rows if r.report_date is not None and r.gap_count == 0)
    parts.append(
        '<div class="kpi-strip">'
        '<div class="kpi-card"><div class="kpi-label">Tracked names</div>'
        f'<div class="kpi-value">{len(rows)}</div></div>'
        '<div class="kpi-card"><div class="kpi-label">Built report</div>'
        f'<div class="kpi-value">{built}</div></div>'
        '<div class="kpi-card"><div class="kpi-label">Fully covered</div>'
        f'<div class="kpi-value">{fully}</div></div>'
        '<div class="kpi-card"><div class="kpi-label">Section gaps</div>'
        f'<div class="kpi-value">{total_gaps}</div></div>'
        "</div>"
    )

    parts.append(lg.grid_open())
    parts.append(lg.filter_bar(len(rows), noun="names", placeholder="Filter by ticker…"))
    parts.append('<div class="sc-scroll"><table class="p-table sc-matrix"><thead><tr>')
    parts.append(
        lg.th("Ticker", "ticker", "text", num=False)
        + lg.th("List", "list", "text", num=False)
        + lg.th("Built", "built", "text", num=False)
    )
    for _key, label in SECTION_COLUMNS:
        parts.append(f"<th>{escape(label)}</th>")
    parts.append(lg.th("Gaps", "gaps", "num") + "</tr></thead><tbody>")
    for r in rows:
        data = (
            lg.data_text(f"{r.ticker} {r.list_type}")
            + lg.data_text_key("ticker", r.ticker)
            + lg.data_text_key("list", r.list_type)
            + lg.data_text_key("built", r.report_date or "")
            + lg.data_num("gaps", r.gap_count)
        )
        parts.append(f"<tr{data}>")
        parts.append(f'<td class="sc-tkr">{escape(r.ticker)}</td>')
        parts.append(f'<td class="sc-list">{escape(r.list_type)}</td>')
        built_cell = escape(r.report_date) if r.report_date else "&mdash;"
        parts.append(f'<td class="sc-list">{built_cell}</td>')
        for key, label in SECTION_COLUMNS:
            state = r.cells.get(key, EMPTY)
            glyph, cls, hover = _CELL_GLYPH[state]
            parts.append(
                f'<td class="{cls}" title="{escape(r.ticker)} · {escape(label)}: '
                f'{escape(hover)}">{glyph}</td>'
            )
        parts.append(f'<td class="sc-gaps">{r.gap_count}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    parts.append(lg.grid_close())

    parts.append(
        '<p class="sc-note">&#9679; populated &nbsp; &#9675; no data &nbsp; '
        "&mdash; not applicable to the business model &nbsp; &middot; no report "
        "built yet. Reports hide empty sections rather than stubbing them "
        "(P4.2 hide-don't-stub) — this matrix is where those gaps stay "
        "visible. Spec sections read each name's latest sections sidecar; "
        "verdict/decision/macro/target/concentration/lease columns are live "
        "row counts.</p></section>"
    )
    return "".join(parts)
