"""DCF coverage panel — which of the ~90 workbooks are live, stale, skipped,
or orphaned (S11 PR2).

One row per ticker in the union of: the actively-maintained names (the
``ACTIVE_LIST_TYPES`` — portfolio / watchlist / evaluation — whose daily
pipeline runs ``refresh_dcf`` per name, plus legacy wacc-seeded holdings),
`dcf/*.xlsx` workbooks, `dcf_runs` rows, and `data/dcf_assumptions/*.json`
files — so every artifact is accounted for, including orphans (a workbook
whose name no refresh maintains) and runs whose workbook has vanished. Per
name: valuation model + how it resolved, workbook existence/mtime, and the two
DCF freshness legs toned independently — the fair-value leg (`valuation_date`,
moves only on a fundamentals refresh) and the price leg (`live_price_at`,
re-priced daily by `reprice_dcf`); a fresh fair value next to a stale price is
the honest tell that `over_under_pct` (and the trim/sell ladder + next-dollar
"ret" factor it feeds) is running on an old price. Plus the assumptions-JSON
state (Opus pass vs sync-created vs missing) and the workbook→JSON sync outcome
persisted by migration 0091.

Render-path discipline: file *stats* and small-JSON reads only — never opens
an .xlsx (an openpyxl load per workbook would put ~90 × ~100ms on a panel
fetch). Format comes from the persisted `dcf_runs` snapshot/notes instead.

The legacy `dcf/redesign/` output directory (the pre-S11 build_all default)
is surfaced as a stale-copies callout: the canonical location is
`dcf/<T>.xlsx` — the refresher, the Sheets round-trip, the served `/dcf/<T>`
route and `dcf_runs.notes` all point there.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import escape
from pathlib import Path
from typing import cast

from ui import living_grid as lg

FRESH_DAYS = 7  # refreshed within a week = fresh
STALE_DAYS = 30  # older than a month = loudly stale

# Mirror of db.ACTIVE_LIST_TYPES / BRIEFED_LIST_TYPES, duplicated like
# dcf.universe does on purpose: importing ``db`` runs init_db() at import
# time, a side effect a read-only panel must not have.
_ACTIVE_LIST_TYPES: tuple[str, ...] = ("portfolio", "watchlist", "evaluation")
_BRIEFED_LIST_TYPES: tuple[str, ...] = ("portfolio", "evaluation")

_PANEL_STYLE = """<style>
.dcv-tick { font-weight:600; white-space:nowrap; }
.dcv-ok   { color:var(--ok); font-weight:600; }
.dcv-warn { color:var(--warn); font-weight:600; }
.dcv-bad  { color:var(--bad); font-weight:600; }
.dcv-muted { color:var(--muted); }
.dcv-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius);
  font-size:var(--fs-body); line-height:1.55; }
.dcv-note code { background:var(--surface); padding:1px 5px; border-radius:var(--radius); }
.dcv-mono { font-family:var(--mono); }
</style>"""


@dataclass(slots=True)
class CoverageRow:
    """Everything the table shows for one ticker."""

    ticker: str
    list_type: str | None  # active tracked list ("portfolio"/"watchlist"/"evaluation")
    legacy_maintained: bool  # holdings JSON carries a hand-seeded wacc
    model: str  # resolved valuation model ("fcff_dcf", "bank_excess_return", ...)
    model_source: str  # "holdings" | "opus" | "default"
    workbook_exists: bool
    workbook_mtime: date | None
    last_valued: date | None  # dcf_runs.valuation_date — the fair-value leg
    last_priced: date | None  # dcf_runs.live_price_at — the price leg (re-priced daily)
    run_format: str | None  # from dcf_runs notes/snapshot ("redesigned", "holdco_sotp", ...)
    sync_status: str | None
    synced_at: date | None
    assumptions_state: str  # "opus" | "opus (seeded)" | "sync-created" | "none"
    note: str  # skip reason / orphan / suggestion

    @property
    def in_universe(self) -> bool:
        """On a briefed list (portfolio/evaluation) — the dcf_universe set."""
        return self.list_type in _BRIEFED_LIST_TYPES

    @property
    def maintained(self) -> bool:
        """Some refresh keeps this name's DCF current: the daily pipeline runs
        ``refresh_dcf`` per ACTIVE-list ticker, and ``--all-named`` adds the
        legacy wacc-seeded holdings."""
        return self.list_type is not None or self.legacy_maintained


# --------------------------------------------------------------------------- #
# Data assembly (cheap reads only)
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict[str, object] | None:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, object]", raw) if isinstance(raw, dict) else None


def _parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _query_runs(db_path: Path) -> dict[str, dict[str, object]]:
    """dcf_runs keyed by ticker — the NEWEST run per ticker: valuation_date
    (the fair-value leg), live_price_at (the price leg — re-priced daily by
    reprice_dcf), notes, snapshot format, sync columns (when the DB has them —
    pre-0091 DBs simply lack the keys).

    ORDER BY matters: the versioned dcf_runs schema (migration 0137) keeps
    superseded history rows per ticker, so an unordered scan only picks the
    newest run by rowid accident — SQLite guarantees no scan order. Newest
    first + first-wins, the same current-run selection every other dcf_runs
    reader uses (report snapshot, research cockpit, next-dollar model)."""
    out: dict[str, dict[str, object]] = {}
    if not db_path.exists():
        return out
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return out
    conn.row_factory = sqlite3.Row
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dcf_runs)")}
        if not cols:
            return out
        sync_sel = (
            ", assumptions_sync_status, assumptions_synced_at"
            if "assumptions_sync_status" in cols and "assumptions_synced_at" in cols
            else ""
        )
        rows = conn.execute(
            "SELECT ticker, valuation_date, live_price_at, notes, assumption_snapshot_json"
            f"{sync_sel} FROM dcf_runs ORDER BY valuation_date DESC, id DESC"
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for r in rows:
        t = str(r["ticker"]).upper()
        if t in out:
            continue
        d: dict[str, object] = {
            "valuation_date": r["valuation_date"],
            "live_price_at": r["live_price_at"],
            "notes": r["notes"],
            "snapshot": r["assumption_snapshot_json"],
        }
        if sync_sel:
            d["sync_status"] = r["assumptions_sync_status"]
            d["synced_at"] = r["assumptions_synced_at"]
        out[t] = d
    return out


def _run_format(run: dict[str, object] | None) -> str | None:
    """The workbook format the last run recorded — snapshot JSON first
    ("format": "redesign"), else the notes suffix ("workbook=X.xlsx (...)")."""
    if run is None:
        return None
    snap = run.get("snapshot")
    if isinstance(snap, str) and snap:
        try:
            parsed: object = json.loads(snap)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            fmt = cast("dict[str, object]", parsed).get("format")
            if isinstance(fmt, str) and fmt:
                return fmt
    notes = run.get("notes")
    if isinstance(notes, str) and "(" in notes and notes.endswith(")"):
        return notes[notes.rfind("(") + 1 : -1]
    return None


def _resolve_model(
    holdings: dict[str, object] | None, block: dict[str, object] | None
) -> tuple[str, str, str]:
    """(model, source, note) mirroring refresh_dcf._valuation_model's order:
    holdings override > Opus block > dcf_applicable heuristic."""
    if holdings is not None:
        vm = holdings.get("valuation_model")
        if isinstance(vm, str) and vm:
            return vm, "holdings", ""
    if block is not None:
        vm = block.get("valuation_model")
        if isinstance(vm, str) and vm:
            note = ""
            if vm == "new":
                sugg = block.get("valuation_model_suggestion")
                if isinstance(sugg, str) and sugg:
                    note = f"Opus suggests: {sugg}"
            return vm, "opus", note
        if block.get("dcf_applicable") is False:
            bm = block.get("business_model")
            bm_s = bm if isinstance(bm, str) else "financial"
            if bm_s == "bank":
                return "bank_excess_return", "default", ""
            return "none", "default", f"FCFF not applicable ({bm_s})"
    return "fcff_dcf", "default", ""


def _legacy_wacc_names(repo_root: Path) -> set[str]:
    """Holdings JSONs carrying a hand-seeded numeric ``wacc`` — the legacy
    half of ``refresh_dcf.dcf_maintained_universe`` (mirrored here because
    pipeline modules don't import execution CLIs). These names are refreshed
    by ``--all-named`` even when not on a tracked list."""
    out: set[str] = set()
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    if not holdings_dir.exists():
        return out
    for p in holdings_dir.glob("*.json"):
        data = _load_json(p)
        if data is not None and isinstance(data.get("wacc"), (int, float)):
            out.add(p.stem.upper())
    return out


def _active_list_types(db_path: Path) -> dict[str, str]:
    """ticker -> list_type for the actively-maintained tracked lists."""
    out: dict[str, str] = {}
    if not db_path.exists():
        return out
    placeholders = ", ".join("?" for _ in _ACTIVE_LIST_TYPES)
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute(
            "SELECT UPPER(ticker), list_type FROM tracked_companies "
            f"WHERE list_type IN ({placeholders})",
            _ACTIVE_LIST_TYPES,
        ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    for ticker, list_type in rows:
        t = str(ticker)
        lt = str(list_type)
        # A name on several lists keeps its most-committed tier.
        order = {"portfolio": 0, "evaluation": 1, "watchlist": 2}
        if t not in out or order.get(lt, 9) < order.get(out[t], 9):
            out[t] = lt
    return out


def collect_rows(db_path: Path, repo_root: Path) -> tuple[list[CoverageRow], list[str]]:
    """All coverage rows (maintained first, then orphans, alpha within each) +
    the list of stale `dcf/redesign/` copies."""
    active = _active_list_types(db_path)
    legacy = _legacy_wacc_names(repo_root)
    dcf_dir = repo_root / "dcf"
    workbooks: dict[str, date | None] = {}
    for p in dcf_dir.glob("*.xlsx"):
        if p.stem.startswith("_") or p.stem.startswith("~"):
            continue
        try:
            workbooks[p.stem.upper()] = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).date()
        except OSError:
            workbooks[p.stem.upper()] = None
    runs = _query_runs(db_path)
    adir = repo_root / "data" / "dcf_assumptions"
    assumption_files = {p.stem.upper(): p for p in adir.glob("*.json")} if adir.exists() else {}

    redesign_dir = repo_root / "dcf" / "redesign"
    stale_copies = (
        sorted(p.stem.upper() for p in redesign_dir.glob("*.xlsx")) if redesign_dir.exists() else []
    )

    rows: list[CoverageRow] = []
    for t in sorted(set(active) | set(workbooks) | set(runs) | set(assumption_files)):
        run = runs.get(t)
        data = _load_json(assumption_files[t]) if t in assumption_files else None
        block_raw = data.get("redesign") if data is not None else None
        block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else None
        holdings = _load_json(repo_root / "micro_thesis" / "holdings" / f"{t}.json")
        model, model_source, note = _resolve_model(holdings, block)

        if data is None:
            assumptions_state = "none"
        else:
            baseline_raw = data.get("opus_baseline")
            baseline = (
                cast("dict[str, object]", baseline_raw) if isinstance(baseline_raw, dict) else None
            )
            if block is not None and block.get("set_by") == "sync":
                assumptions_state = "sync-created"
            elif baseline is not None and baseline.get("seeded"):
                assumptions_state = "opus (seeded)"
            else:
                assumptions_state = "opus"

        list_type = active.get(t)
        legacy_maintained = t in legacy
        if list_type is None and not legacy_maintained:
            orphan_of: list[str] = []
            if t in workbooks:
                orphan_of.append("workbook")
            if run is not None:
                orphan_of.append("dcf_runs row")
            if not orphan_of:
                orphan_of.append("assumptions JSON")
            note = (note + "; " if note else "") + (
                f"orphan ({' + '.join(orphan_of)}; not on an active list, no wacc seed)"
            )

        sync_status = run.get("sync_status") if run else None
        synced_at = _parse_date(run.get("synced_at")) if run else None
        rows.append(
            CoverageRow(
                ticker=t,
                list_type=list_type,
                legacy_maintained=legacy_maintained,
                model=model,
                model_source=model_source,
                workbook_exists=t in workbooks,
                workbook_mtime=workbooks.get(t),
                last_valued=_parse_date(run.get("valuation_date")) if run else None,
                last_priced=_parse_date(run.get("live_price_at")) if run else None,
                run_format=_run_format(run),
                sync_status=str(sync_status) if isinstance(sync_status, str) else None,
                synced_at=synced_at,
                assumptions_state=assumptions_state,
                note=note,
            )
        )
    rows.sort(key=lambda r: (not r.maintained, not r.in_universe, r.ticker))
    return rows, stale_copies


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _age_cell(d: date | None, today: date) -> str:
    if d is None:
        return '<span class="dcv-muted">never</span>'
    age = (today - d).days
    tone = "dcv-ok" if age <= FRESH_DAYS else ("dcv-warn" if age <= STALE_DAYS else "dcv-bad")
    return f'<span class="{tone}" title="{d.isoformat()}">{d.isoformat()} ({age}d)</span>'


def _priced_cell(d: date | None, today: date) -> str:
    """The price leg's own freshness, separate from the fair-value leg.

    over_under_pct = (live_price - fair) / fair: the fair value moves only on a
    fundamentals refresh, but the price leg should be current every day
    (reprice_dcf re-divides it daily). A fresh fair value (green "Last valued")
    next to a stale price reading here is the honest signal that the trim/sell
    ladder / next-dollar "ret" factor are running on an old price — what a
    single valuation-date tone used to hide. ``None`` = the run never carried a
    live price (over/under is NULL → the ladder reads 'unknown')."""
    if d is None:
        return '<span class="dcv-muted" title="no live price on the DCF run">never priced</span>'
    return _age_cell(d, today)


def _sync_cell(r: CoverageRow) -> str:
    if r.sync_status is None:
        if r.model == "fcff_dcf" and r.last_valued is not None:
            return '<span class="dcv-muted">pre-0091 run</span>'
        return '<span class="dcv-muted">—</span>'
    if r.sync_status.startswith("failed"):
        return f'<span class="dcv-bad" title="{escape(r.sync_status)}">FAILED</span>'
    when = (
        f' <span class="dcv-mono">{escape(r.synced_at.isoformat())}</span>' if r.synced_at else ""
    )
    return f'<span class="dcv-ok">{escape(r.sync_status)}</span>{when}'


def _workbook_cell(r: CoverageRow) -> str:
    if not r.workbook_exists:
        return '<span class="dcv-bad">missing</span>'
    fmt = (
        f' <span class="dcv-muted"> · </span>'
        f'<span class="dcv-muted dcv-mono">{escape(r.run_format)}</span>'
        if r.run_format
        else ""
    )
    return f'<span class="dcv-ok">yes</span>{fmt}'


def _kpi_strip(rows: list[CoverageRow], today: date) -> str:
    maintained = [r for r in rows if r.maintained]
    skipped = [r for r in maintained if r.model in ("none", "new")]
    expected = [r for r in maintained if r.model not in ("none", "new")]
    fresh_tickers = {
        r.ticker for r in expected if r.last_valued and (today - r.last_valued).days <= FRESH_DAYS
    }
    fresh = [r for r in expected if r.ticker in fresh_tickers]
    stale = [r for r in expected if r.ticker not in fresh_tickers]
    failed = [r for r in rows if r.sync_status and r.sync_status.startswith("failed")]
    no_json = [r for r in expected if r.assumptions_state == "none"]
    orphans = [r for r in rows if not r.maintained]

    def card(label: str, value: int, sub: str, tone: str = "") -> str:
        return (
            f'<div class="kpi-card {tone}"><div class="kpi-label">{escape(label)}</div>'
            f'<div class="kpi-value">{value}</div>'
            f'<div class="kpi-sub">{escape(sub)}</div></div>'
        )

    return (
        '<div class="kpi-strip">'
        + card("Maintained", len(maintained), "active lists + legacy wacc seeds")
        + card(
            "Fresh",
            len(fresh),
            f"fair value valued within {FRESH_DAYS}d",
            "tone-good" if len(fresh) >= len(expected) and expected else "",
        )
        + card(
            "Stale / never", len(stale), "DCF-expected, not refreshed", "tone-warn" if stale else ""
        )
        + card(
            "Sync failed",
            len(failed),
            "workbook→JSON mirror broken",
            "tone-bad" if failed else "tone-good",
        )
        + card(
            "No assumptions JSON",
            len(no_json),
            "next refresh creates one",
            "tone-warn" if no_json else "tone-good",
        )
        + card("Orphans", len(orphans), "artifacts no refresh maintains")
        + card("Skipped", len(skipped), "no fitting valuation template")
        + "</div>"
    )


def _date_num(d: date | None) -> float | None:
    return float(d.toordinal()) if d is not None else None


def _row_html(r: CoverageRow, today: date) -> str:
    model = escape(r.model)
    if r.model_source != "default":
        model += f' <span class="k-chip">{escape(r.model_source)}</span>'
    state_cls = {
        "none": "dcv-warn",
        "sync-created": "dcv-muted",
        "opus (seeded)": "dcv-muted",
        "opus": "dcv-ok",
    }.get(r.assumptions_state, "dcv-muted")
    list_key = r.list_type or ("wacc seed" if r.legacy_maintained else "orphan")
    if r.list_type is not None:
        list_cell = escape(r.list_type)
    elif r.legacy_maintained:
        list_cell = '<span class="dcv-muted">wacc seed</span>'
    else:
        list_cell = '<span class="dcv-warn">orphan</span>'
    data = (
        lg.data_text(f"{r.ticker} {list_key} {r.model} {r.assumptions_state} {r.note}")
        + lg.data_text_key("ticker", r.ticker)
        + lg.data_text_key("list", list_key)
        + lg.data_text_key("model", r.model)
        + lg.data_num("valued", _date_num(r.last_valued))
        + lg.data_num("priced", _date_num(r.last_priced))
        + lg.data_text_key("assumptions", r.assumptions_state)
    )
    return (
        f"<tr{data}>"
        f'<td class="dcv-tick">{escape(r.ticker)}</td>'
        f"<td>{list_cell}</td>"
        f"<td>{model}</td>"
        f"<td>{_workbook_cell(r)}</td>"
        f"<td>{_age_cell(r.last_valued, today)}</td>"
        f"<td>{_priced_cell(r.last_priced, today)}</td>"
        f'<td><span class="{state_cls}">{escape(r.assumptions_state)}</span></td>'
        f"<td>{_sync_cell(r)}</td>"
        f'<td class="dcv-muted">{escape(r.note)}</td>'
        "</tr>"
    )


def render_dcf_coverage_panel(db_path: Path, repo_root: Path) -> str:
    """The DCF Coverage tab fragment: KPI strip + per-name coverage table +
    the stale `dcf/redesign/` callout."""
    today = date.today()
    rows, stale_copies = collect_rows(db_path, repo_root)
    if not rows:
        return (
            '<section class="panel"><h2>DCF coverage</h2>'
            '<p class="muted">No DCF artifacts found — no briefed names, '
            "<code>dcf/*.xlsx</code> workbooks, <code>dcf_runs</code> rows or "
            "assumptions JSONs under this repo root.</p></section>"
        )

    table_rows = "".join(_row_html(r, today) for r in rows)
    stale_note = ""
    if stale_copies:
        stale_note = (
            '<div class="dcv-note"><strong>Stale copies in '
            f"<code>dcf/redesign/</code> ({len(stale_copies)})</strong> — the "
            "pre-S11 <code>build_all_redesigned_dcf</code> default output dir. "
            "The canonical workbook location is <code>dcf/&lt;T&gt;.xlsx</code> "
            "(what the refresher, Sheets round-trip and <code>/dcf/&lt;T&gt;</code> "
            "serve); these copies are superseded and safe to delete: "
            f"{escape(', '.join(stale_copies))}</div>"
        )

    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>DCF coverage</h2>',
            '<p class="sub">Every DCF artifact accounted for: the actively '
            "maintained names (portfolio · watchlist · evaluation, plus legacy "
            "wacc seeds) unioned with <code>dcf/*.xlsx</code>, "
            "<code>dcf_runs</code> and <code>data/dcf_assumptions/</code>. "
            "Refresh a name with "
            "<code>python execution/refresh_dcf.py --ticker T</code>.</p>",
            _kpi_strip(rows, today),
            lg.grid_open(),
            lg.filter_bar(len(rows), noun="names", placeholder="Filter by ticker / model / note…"),
            '<table class="p-table"><thead><tr>'
            + lg.th("Ticker", "ticker", "text", num=False)
            + lg.th("List", "list", "text", num=False)
            + lg.th("Model", "model", "text", num=False)
            + "<th>Workbook</th>"
            + lg.th("Last valued", "valued", "num", num=False)
            + lg.th("Priced", "priced", "num", num=False)
            + lg.th("Assumptions", "assumptions", "text", num=False)
            + "<th>JSON sync</th><th>Note</th>"
            + "</tr></thead><tbody>",
            table_rows,
            "</tbody></table>",
            lg.grid_close(),
            stale_note,
            "</section>",
        ]
    )
