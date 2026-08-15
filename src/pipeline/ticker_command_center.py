"""Per-ticker command-center drill-down: assemble + render.

One page that answers "what's the full state of this name right now?" —
identity/freshness, the live position (from the companion portfolio-tracker),
the analyses that ran, the artifacts on disk, recent decisions, and the
read-only thesis. Pure reads; degrades panel-by-panel when a source is absent.

Single consumer (the live command-center server), so the builder and the
renderer live together here — unlike the analytical dashboard, whose render
layer is shared with a static exporter and therefore split out.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html import escape
from io import StringIO
from pathlib import Path
from typing import cast

from alerts import AlertRow, QueuedActionRow, list_alerts, list_queued_actions_for_alert
from compute.thesis_evaluation_episodes import episode_history_source
from dashboard import render_alert_card
from dashboard.evidence_drawer import load_brief_provenance
from pipeline.analysis_log import AnalysisLog, build_analysis_log
from pipeline.artifact_inventory import Artifact, build_artifact_inventory
from pipeline.freshness import freshness_verdict
from pipeline.you_said import render_you_said_strip_for_path
from provenance.selection import selected_transcripts_relation
from report.renderers.numfmt import fmt_date, fmt_reltime
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import controls_css, pill_tone_class, thesis_status_tone, ticker_label
from ui.prose import render_prose
from ui.time import stamp_html
from ui.tokens import FAVICON_LINK, palette_css
from user_state.notes import NOTE_KINDS, AnalystNoteRow, list_notes

_DEFAULT_TRACKER_URL = "http://localhost:5173"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TickerIdentity:
    ticker: str
    name: str | None = None
    list_type: str | None = None
    last_build_at: str | None = None
    last_fmp_at: str | None = None
    last_transcript_period: str | None = None
    breach_status: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ThesisKpi:
    name: str
    current: str | None
    status: str | None
    break_condition: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ThesisBreakRule:
    kpi_name: str
    comparator: str | None
    threshold: str | None
    unit: str | None
    narrative: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ThesisView:
    present: bool = False
    thesis: str | None = None
    verdict: str | None = None
    last_updated: str | None = None
    tier1: list[ThesisKpi] = field(default_factory=lambda: list[ThesisKpi]())
    break_rules: list[ThesisBreakRule] = field(default_factory=lambda: list[ThesisBreakRule]())
    qualitative_breakers: list[str] = field(default_factory=lambda: list[str]())

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "thesis": self.thesis,
            "verdict": self.verdict,
            "last_updated": self.last_updated,
            "tier1": [k.to_dict() for k in self.tier1],
            "break_rules": [r.to_dict() for r in self.break_rules],
            "qualitative_breakers": self.qualitative_breakers,
        }


@dataclass(slots=True)
class PositionAccount:
    account_name: str
    quantity: float
    market_value: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class PositionStrip:
    available: bool = False  # is the portfolio-tracker DB reachable at all?
    held: bool = False
    total_quantity: float | None = None
    total_cost_basis: float | None = None
    total_market_value: float | None = None
    total_unrealized_pnl: float | None = None
    total_unrealized_pct: float | None = None
    accounts: list[PositionAccount] = field(default_factory=lambda: list[PositionAccount]())
    last_decision: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "held": self.held,
            "total_quantity": self.total_quantity,
            "total_cost_basis": self.total_cost_basis,
            "total_market_value": self.total_market_value,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "total_unrealized_pct": self.total_unrealized_pct,
            "accounts": [a.to_dict() for a in self.accounts],
            "last_decision": self.last_decision,
        }


@dataclass(slots=True)
class DecisionLite:
    recommendation_kind: str
    recommendation_value: float | None
    conviction: str | None
    made_at: str
    outcome_label: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TickerCommandCenter:
    identity: TickerIdentity
    artifacts: list[Artifact] = field(default_factory=lambda: list[Artifact]())
    analysis: AnalysisLog = field(default_factory=AnalysisLog)
    recent_decisions: list[DecisionLite] = field(default_factory=lambda: list[DecisionLite]())
    thesis: ThesisView = field(default_factory=ThesisView)
    position: PositionStrip = field(default_factory=PositionStrip)
    tracker_url: str | None = None
    # YYYY-MM-DD of the latest workspace brief — the (ticker, report_date) key the
    # comment store + chat thread use, so the Holding tab's embedded report lines
    # up with the right day. None when no brief has been built.
    report_date: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "analysis": self.analysis.to_dict(),
            "recent_decisions": [d.to_dict() for d in self.recent_decisions],
            "thesis": self.thesis.to_dict(),
            "position": self.position.to_dict(),
            "tracker_url": self.tracker_url,
            "report_date": self.report_date,
        }


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_ticker_command_center(repo_root: Path, ticker: str) -> TickerCommandCenter:
    t = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
    try:
        identity = _identity(conn, repo_root, t)
        analysis = build_analysis_log(conn, t) if conn is not None else AnalysisLog()
        decisions = _recent_decisions(conn, t) if conn is not None else []
    finally:
        if conn is not None:
            conn.close()
    artifacts = build_artifact_inventory(repo_root, t)
    return TickerCommandCenter(
        identity=identity,
        artifacts=artifacts,
        analysis=analysis,
        recent_decisions=decisions,
        thesis=_thesis_view(repo_root, t),
        position=_position_strip(repo_root, t),
        tracker_url=_tracker_url(t),
        report_date=_report_date_from_artifacts(artifacts),
    )


def _report_date_from_artifacts(artifacts: list[Artifact]) -> str | None:
    """Parse the latest workspace brief's report date (YYYY-MM-DD) from its
    ``<DATE>_workspace.html`` filename. This is the (ticker, report_date) key the
    comment store + chat thread use, so the Holding tab's embedded report and any
    inline pipeline resolve to the right day."""
    for a in artifacts:
        if a.label == "Workspace report (HTML)" and a.exists and a.path:
            stem = a.path.rsplit("/", 1)[-1]  # <DATE>_workspace.html
            datepart = stem.split("_", 1)[0]
            try:
                date.fromisoformat(datepart)
            except ValueError:
                return None
            return datepart
    return None


def _identity(conn: sqlite3.Connection | None, repo_root: Path, t: str) -> TickerIdentity:
    ident = TickerIdentity(ticker=t, last_build_at=_last_build_at(repo_root, t))
    if conn is None:
        return ident
    if _has(conn, "tracked_companies"):
        row = conn.execute(
            "SELECT name, list_type FROM tracked_companies "
            "WHERE UPPER(ticker)=? AND archived_at IS NULL LIMIT 1",
            (t,),
        ).fetchone()
        if row is not None:
            ident.name = row["name"]
            ident.list_type = row["list_type"]
    if _has(conn, "fmp_endpoint_status"):
        row = conn.execute(
            "SELECT MAX(last_pulled) AS lp FROM fmp_endpoint_status WHERE UPPER(ticker)=?",
            (t,),
        ).fetchone()
        ident.last_fmp_at = str(row["lp"]) if row and row["lp"] else None
    if _has(conn, "transcripts"):
        transcripts = selected_transcripts_relation(conn).sql
        row = conn.execute(
            f"SELECT period_end FROM {transcripts} "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "WHERE UPPER(ticker)=? AND period_end IS NOT NULL "
            "ORDER BY period_end DESC LIMIT 1",
            (t,),
        ).fetchone()
        ident.last_transcript_period = (
            str(row["period_end"])[:10] if row and row["period_end"] else None
        )
    if _has(conn, "thesis_evaluations"):
        source = episode_history_source(conn)
        row = conn.execute(
            f"SELECT overall_status FROM {source.relation} WHERE UPPER(ticker)=? "
            f"ORDER BY {source.latest_checked_column} DESC LIMIT 1",  # nosec B608 -- trusted closed relation
            (t,),
        ).fetchone()
        ident.breach_status = row["overall_status"] if row else None
    return ident


def _recent_decisions(conn: sqlite3.Connection, t: str) -> list[DecisionLite]:
    if not _has(conn, "decisions"):
        return []
    rows = conn.execute(
        "SELECT recommendation_kind, recommendation_value, conviction, made_at, outcome_label "
        "FROM decisions WHERE UPPER(ticker)=? ORDER BY made_at DESC LIMIT 8",
        (t,),
    ).fetchall()
    out: list[DecisionLite] = []
    for r in rows:
        val = r["recommendation_value"]
        out.append(
            DecisionLite(
                recommendation_kind=str(r["recommendation_kind"]),
                recommendation_value=float(val) if val is not None else None,
                conviction=r["conviction"],
                made_at=str(r["made_at"])[:19],
                outcome_label=r["outcome_label"],
            )
        )
    return out


def _thesis_view(repo_root: Path, t: str) -> ThesisView:
    p = repo_root / "micro_thesis" / "holdings" / f"{t}.json"
    if not p.is_file():
        return ThesisView(present=False)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ThesisView(present=False)
    if not isinstance(data, dict):
        return ThesisView(present=False)
    obj = cast("dict[str, object]", data)  # JSON boundary: cast, don't suppress
    tier1 = [
        ThesisKpi(
            name=str(k.get("name", "")),
            current=_s(k.get("current")),
            status=_s(k.get("status")),
            break_condition=_s(k.get("break_condition")),
        )
        for k in _dict_list(obj.get("tier_1_kpis"))
    ]
    rules = [
        ThesisBreakRule(
            kpi_name=str(r.get("kpi_name", "")),
            comparator=_s(r.get("comparator")),
            threshold=_s(r.get("threshold")),
            unit=_s(r.get("unit")),
            narrative=_s(r.get("narrative")),
        )
        for r in _dict_list(obj.get("break_rules"))
    ]
    return ThesisView(
        present=True,
        thesis=_s(obj.get("thesis")),
        verdict=_s(obj.get("verdict")),
        last_updated=_s(obj.get("last_updated")),
        tier1=tier1,
        break_rules=rules,
        qualitative_breakers=_str_list(obj.get("thesis_breakers_qualitative")),
    )


def _position_strip(repo_root: Path, t: str) -> PositionStrip:
    # Lazy import: keep the heavy report.* import chain out of server boot.
    from report.models import SectionStatus
    from report.sections import portfolio_position
    from report.sections._common import open_portfolio_tracker_db

    probe = open_portfolio_tracker_db(repo_root)
    if probe is None:
        return PositionStrip(available=False)
    probe.close()

    section = portfolio_position.build(t, repo_root)
    if section.status != SectionStatus.OK:
        return PositionStrip(available=True, held=False)
    last_decision: str | None = None
    if section.open_decisions:
        d = section.open_decisions[0]
        last_decision = f"{d.action} ({fmt_date(d.decision_date.isoformat())})"
    elif section.closed_decisions:
        d = section.closed_decisions[0]
        last_decision = f"{d.action} → {d.outcome_status} ({fmt_date(d.decision_date.isoformat())})"
    return PositionStrip(
        available=True,
        held=bool(section.held),
        total_quantity=section.total_quantity,
        total_cost_basis=section.total_cost_basis,
        total_market_value=section.total_market_value,
        total_unrealized_pnl=section.total_unrealized_pnl,
        total_unrealized_pct=section.total_unrealized_pct,
        accounts=[
            PositionAccount(
                account_name=a.account_name, quantity=a.quantity, market_value=a.market_value
            )
            for a in section.accounts
        ],
        last_decision=last_decision,
    )


def _tracker_url(t: str) -> str:
    base = os.environ.get("PORTFOLIO_TRACKER_URL", _DEFAULT_TRACKER_URL).rstrip("/")
    return f"{base}/holdings?ticker={t}"


# --------------------------------------------------------------------------- #
# Holding-rail data (master build P1.3): open notes + recent alerts beside
# the embedded report.
# --------------------------------------------------------------------------- #
_RAIL_NOTES_LIMIT = 20
_RAIL_ALERTS_LIMIT = 5


@dataclass(slots=True)
class HoldingRail:
    """Side-rail substrate for the Holding tab: the analyst's open notes
    (analyst_notes) and the newest fired alerts for one name, surfaced beside
    the embedded report. ``notes`` / ``alerts`` are None when that substrate
    is unavailable (DB or table missing) — an unavailable-state distinct from
    "none yet"."""

    notes: list[AnalystNoteRow] | None = None
    alerts: list[tuple[AlertRow, list[QueuedActionRow]]] | None = None
    brief_provenance: Mapping[str, object] | None = None


def build_holding_rail(repo_root: Path, ticker: str) -> HoldingRail:
    """Pure reads for the Holding tab's side rail. Each source degrades to
    None independently when its table (or the whole DB) is missing, matching
    the drill-down's panel-by-panel degradation contract. The brief-provenance
    payload rides along so fact_id citations in the alert cards resolve to
    (source, fetched_at) exactly as they do on the digest/feed."""
    t = ticker.upper()
    db = repo_root / "data" / "portfolio.db"
    rail = HoldingRail()
    if not db.exists():
        return rail
    try:
        rail.notes = list_notes(ticker=t, status="open", limit=_RAIL_NOTES_LIMIT, db_path=db)
    except sqlite3.Error:
        rail.notes = None
    try:
        fired = list_alerts(ticker=t, limit=_RAIL_ALERTS_LIMIT, db_path=db)
        rail.alerts = [(a, list_queued_actions_for_alert(a.id, db_path=db)) for a in fired]
    except sqlite3.Error:
        rail.alerts = None
    if rail.alerts:
        rail.brief_provenance = load_brief_provenance(t, db_path=db)
    return rail


# --------------------------------------------------------------------------- #
# Shared ✎ Notes drawer fragment (UX9b): the shell's topbar drawer body.
# Lives here (not in the shell module) because it reuses the holding rail's
# substrate + renderers — open notes and, when ticker-scoped, recent alerts.
# --------------------------------------------------------------------------- #
_QUICK_NOTE_STYLE = """<style>
.cc-quicknote .qn-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
/* Inputs/selects/textarea: skinned by the shared control kit (ui/controls.py). */
.cc-quicknote textarea { width: 100%; box-sizing: border-box; resize: vertical;
  margin-bottom: 8px; }
.cc-quicknote .qn-ticker { width: 120px; text-transform: uppercase; }
.cc-quicknote .qn-msg { font-size: var(--fs-caption); }
.cc-quicknote .qn-musing-label { display: flex; align-items: center; gap: 4px;
  color: var(--muted); font-size: var(--fs-caption); cursor: pointer; }
.cc-notes-foot { color: var(--muted); font-size: var(--fs-caption); }
</style>"""

_QUICK_NOTE_SCRIPT = """<script>
(function () {
  var root = document.querySelector('.cc-quicknote');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  var msg = root.querySelector('.qn-msg');
  var saveBtn = root.querySelector('.qn-save');
  function save() {
    if (saveBtn.disabled) return;  // Enter + click can't double-submit
    var body = root.querySelector('.qn-body').value.trim();
    if (!body) { msg.textContent = 'write the note first'; return; }
    // Default (unchecked): journal note, unchanged behavior. Checked: the
    // SAME text routes to the Ledger capture spine instead (wondering/pledge
    // taps run) — the endpoint the markup carries via data-musing-endpoint.
    var musing = root.querySelector('.qn-musing');
    var toCaptureSpine = !!(musing && musing.checked);
    var url = toCaptureSpine ? root.getAttribute('data-musing-endpoint') : '/api/notes';
    var t = root.querySelector('.qn-ticker').value.trim().toUpperCase();
    // The capture spine takes bare text; a typed ticker rides along as a
    // $-mention so the roster matcher links it (the tray's prefill idiom) —
    // silently dropping it would land a needs_ticker musing.
    var payload = toCaptureSpine
      ? { text: (t ? '$' + t + ' ' : '') + body }
      : { kind: root.querySelector('.qn-kind').value, body: body };
    if (!toCaptureSpine && t) payload.ticker = t;
    msg.textContent = 'saving\\u2026';
    CCAction.busy(saveBtn);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      CCAction.release(saveBtn);
      if (!res.ok) { msg.textContent = 'error: ' + (res.j.error || 'failed'); return; }
      msg.textContent = 'saved \\u2713';
      // The shell re-fetches this whole fragment, so the new note appears in
      // the list below (and the form resets).
      if (window.ccReloadNotesDrawer) window.ccReloadNotesDrawer();
    }).catch(function () { CCAction.release(saveBtn); msg.textContent = 'network error'; });
  }
  saveBtn.addEventListener('click', save);
  root.querySelector('.qn-body').addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); save(); }
  });
})();
</script>"""


def _quick_add_form(ticker: str | None) -> str:
    """Kind · ticker · body → POST /api/notes (source="manual"), OR — when the
    "musing" toggle is checked — POST /api/capture/text so the same write
    feeds the Ledger capture spine (wondering/pledge taps run) instead of
    landing as a plain journal note. The ticker input pre-fills with the
    Holding tab's selection when the drawer opens ticker-scoped, and stays
    editable — a quick note about another name shouldn't need a tab switch.
    ``data-musing-endpoint`` is the alternate-endpoint contract the client JS
    reads; routing itself is client-side (no server change needed)."""
    kind_opts = "".join(
        f'<option value="{escape(k)}"{" selected" if k == "observation" else ""}>{escape(k)}</option>'
        for k in NOTE_KINDS
    )
    tval = f' value="{escape(ticker, quote=True)}"' if ticker else ""
    return (
        '<section class="panel cc-rail-panel cc-quicknote" data-musing-endpoint="/api/capture/text">'
        "<h2>Quick note</h2>"
        '<div class="qn-row">'
        f'<select class="qn-kind" aria-label="Note kind">{kind_opts}</select>'
        f'<input class="qn-ticker" type="text" placeholder="ticker (optional)" maxlength="8"'
        f'{tval} aria-label="Ticker" autocapitalize="characters" spellcheck="false">'
        "</div>"
        '<textarea class="qn-body" rows="3" '
        'placeholder="What did you notice? Enter saves · Shift+Enter for a newline."></textarea>'
        '<div class="qn-row"><label class="qn-musing-label" title="Route to the Ledger capture '
        'spine — wondering/pledge taps run.">'
        '<input type="checkbox" class="qn-musing">musing</label>'
        '<button type="button" class="qn-save k-btn k-btn-primary">Add note</button>'
        '<span class="qn-msg muted"></span></div>'
        "</section>" + _QUICK_NOTE_STYLE + _QUICK_NOTE_SCRIPT
    )


def render_notes_drawer_fragment(repo_root: Path, ticker: str | None = None) -> str:
    """The shared ✎ Notes drawer body (UX9b): quick-add above the open-notes
    list. Ticker-scoped (Holding tab open) it also carries that name's recent
    alerts — the content the holding page's PR4 Notes drawer held — so nothing
    the analyst used to see there is lost. The full lifecycle UI (resolve ·
    reclassify · supersede) stays on Review → Journal; this drawer is
    capture + recall without leaving the current screen."""
    t = ticker.strip().upper() if ticker and ticker.strip() else None
    parts = [_quick_add_form(t)]
    if t:
        rail = build_holding_rail(repo_root, t)
        parts.append(_notes_rail_section(rail.notes))
        parts.append(_alerts_rail_section(t, rail.alerts, rail.brief_provenance))
    else:
        db = repo_root / "data" / "portfolio.db"
        notes: list[AnalystNoteRow] | None = None
        if db.exists():
            try:
                notes = list_notes(status="open", limit=_RAIL_NOTES_LIMIT, db_path=db)
            except sqlite3.Error:
                notes = None
        parts.append(_notes_rail_section(notes))
    parts.append(
        '<p class="cc-notes-foot">Resolve · reclassify · supersede live in '
        '<a href="/#journal">Review → Journal</a>.</p>'
    )
    return "".join(parts)


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _last_build_at(repo_root: Path, t: str) -> str | None:
    research_dir = repo_root / "output" / "research" / t
    if not research_dir.is_dir():
        return None
    matches = sorted(research_dir.glob("*_workspace.html"))
    if not matches:
        return None
    from datetime import UTC

    return datetime.fromtimestamp(matches[-1].stat().st_mtime, tz=UTC).isoformat()


def _s(v: object) -> str | None:
    return str(v) if v is not None else None


def _dict_list(v: object) -> list[dict[str, object]]:
    """Coerce an arbitrary JSON value to a list of dicts (drops non-dict items)."""
    if not isinstance(v, list):
        return []
    items = cast("list[object]", v)
    return [cast("dict[str, object]", x) for x in items if isinstance(x, dict)]


def _str_list(v: object) -> list[str]:
    """Coerce an arbitrary JSON value to a list of strings (drops non-strings)."""
    if not isinstance(v, list):
        return []
    items = cast("list[object]", v)
    return [x for x in items if isinstance(x, str)]


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
_REFRESH_SCRIPT = """<script>
document.querySelectorAll('.tcc-refresh').forEach(function (b) {
  b.addEventListener('click', function () {
    var msg = document.querySelector('.tcc-refresh-msg');
    msg.textContent = 'starting\\u2026';
    CCAction.busy(b);
    fetch('/actions/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ticker: b.getAttribute('data-ticker'),
        mode: 'stale',
        force_budget_bypass: b.getAttribute('data-bypass') === '1'
      })
    }).then(function (r) { return r.json(); }).then(function (j) {
      CCAction.release(b);
      msg.innerHTML = j.job_id
        ? 'started \\u2014 <a href="' + j.stream_url + '">view log</a>'
        : ('error: ' + (j.error || 'failed'));
    }).catch(function () { CCAction.release(b); msg.textContent = 'network error'; });
  });
});
var _t = document.querySelector('.tcc-bypass-toggle');
if (_t) {
  var _tk = _t.getAttribute('data-ticker');
  var _tm = document.querySelector('.tcc-bypass-msg');
  fetch('/api/ticker-settings/' + encodeURIComponent(_tk))
    .then(function (r) { return r.json(); })
    .then(function (s) { _t.checked = !!s.bypass_budget; })
    .catch(function () {});
  _t.addEventListener('change', function () {
    _tm.textContent = 'saving\\u2026';
    fetch('/api/ticker-settings/' + encodeURIComponent(_tk), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bypass_budget: _t.checked })
    }).then(function (r) { return r.json(); }).then(function (s) {
      _tm.textContent = ('bypass_budget' in s) ? 'saved \\u2713' : ('error: ' + (s.error || 'failed'));
    }).catch(function () { _tm.textContent = 'network error'; });
  });
}
</script>"""


def _refresh_section(ticker: str) -> str:
    """Rebuild-this-ticker action panel. "Run anyway" passes force_budget_bypass
    so analyses a skip-mode cap would forgo are run regardless."""
    t = escape(ticker)
    return (
        '<section class="panel"><h2>Refresh</h2>'
        '<p class="sub">Rebuild this brief. "Run anyway" ignores per-purpose LLM budget '
        "caps for this run, so analyses forgone under a skip-mode cap are included.</p>"
        f'<button type="button" class="tcc-refresh k-btn k-btn-primary" data-ticker="{t}" data-bypass="0">'
        "Refresh</button> "
        f'<button type="button" class="tcc-refresh k-btn k-btn-quiet" data-ticker="{t}" data-bypass="1">'
        "Run anyway (ignore caps)</button> "
        '<span class="tcc-refresh-msg muted"></span>'
        '<p style="margin-top:8px">'
        f'<label><input type="checkbox" class="tcc-bypass-toggle" data-ticker="{t}"> '
        "Always ignore budget caps for this ticker (persistent)</label> "
        '<span class="tcc-bypass-msg muted"></span></p>' + _REFRESH_SCRIPT + "</section>"
    )


_DCF_SHEETS_SCRIPT = """<script>
(function () {
  var root = document.querySelector('.tcc-dcfsheets');
  if (!root) return;
  var tk = root.getAttribute('data-ticker');
  var msg = root.querySelector('.tcc-dcfsheets-msg');
  var openLink = root.querySelector('.tcc-dcfsheets-open');
  function refreshLink() {
    fetch('/api/dcf-sheet/' + encodeURIComponent(tk))
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s && s.url) { openLink.href = s.url; openLink.style.display = ''; }
        else { openLink.style.display = 'none'; }
      }).catch(function () {});
  }
  function post(url, label, btn) {
    msg.textContent = label + '\\u2026';
    CCAction.busy(btn);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: tk })
    }).then(function (r) { return r.json(); }).then(function (j) {
      CCAction.release(btn);
      msg.innerHTML = j.job_id
        ? 'started \\u2014 <a href="' + j.stream_url + '">view log</a>'
        : ('error: ' + (j.error || 'failed'));
      if (j.job_id) { setTimeout(refreshLink, 2000); }
    }).catch(function () { CCAction.release(btn); msg.textContent = 'network error'; });
  }
  root.querySelector('.tcc-dcf-export').addEventListener('click', function (ev) {
    post('/actions/dcf-export', 'pushing to Sheets', ev.currentTarget);
  });
  root.querySelector('.tcc-dcf-import').addEventListener('click', function (ev) {
    post('/actions/dcf-import', 'pulling + recomputing', ev.currentTarget);
  });
  refreshLink();
})();
</script>"""


def _dcf_sheets_section(ticker: str) -> str:
    """DCF ⇄ Google Sheets round-trip panel: push the workbook to a Sheet,
    edit the Forecast INPUTS in the browser, then re-ingest to recompute. Needs
    Google credentials configured server-side (directives/dcf_gsheets_setup.md) — the
    buttons surface the job's error if they aren't. The "Open" link appears once
    a Sheet has been linked (via GET /api/dcf-sheet/<ticker>)."""
    t = escape(ticker)
    return (
        f'<section class="panel tcc-dcfsheets" data-ticker="{t}">'
        "<h2>DCF ⇄ Google Sheets</h2>"
        '<p class="sub">Push this ticker\'s DCF workbook to a Google Sheet, edit the Forecast '
        "INPUTS in the browser, then re-ingest to recompute the fair value into "
        "<code>dcf_runs</code>.</p>"
        '<button type="button" class="tcc-dcf-export k-btn k-btn-primary">Push to Sheets</button> '
        '<button type="button" class="tcc-dcf-import k-btn k-btn-quiet">Re-ingest from Sheets</button> '
        '<a class="tcc-dcfsheets-open" href="#" target="_blank" rel="noopener" '
        'style="display:none">Open in Google Sheets ↗</a> '
        '<span class="tcc-dcfsheets-msg muted"></span>' + _DCF_SHEETS_SCRIPT + "</section>"
    )


def render_ticker_html(tcc: TickerCommandCenter, *, generated_at: datetime) -> str:
    ident = tcc.identity
    # The canonical two-part ticker label (mono symbol + muted name), display
    # size — never the "T · Name" single string.
    heading = ticker_label(ident.ticker, ident.name, name_max="36ch")
    parts: list[str] = [
        _PAGE_HEAD.format(
            ticker=escape(ident.ticker),
            generated_at=stamp_html(generated_at, prefix="updated "),
        ),
        f"<header><div><h1>{heading}</h1>",
        '<nav class="top-nav">',
        '<a href="/">&larr; Dashboard</a> · ',
        '<a href="/analytical">Analytical overview</a> · ',
        f'<a href="/reports/{escape(ident.ticker)}">Latest brief &nearr;</a>',
        (
            f' · <a href="{escape(tcc.tracker_url)}" target="_blank" rel="noopener">Open in Portfolio Tracker &nearr;</a>'
            if tcc.tracker_url
            else ""
        ),
        "</nav></div>",
        _identity_badges(ident),
        "</header>",
        _freshness_strip(ident),
        _refresh_section(ident.ticker),
        _dcf_sheets_section(ident.ticker),
        _position_section(tcc.position),
        _analyses_section(tcc.analysis),
        _decisions_section(tcc.recent_decisions),
        _artifacts_section(tcc.artifacts),
        _thesis_section(tcc.thesis),
        _PAGE_FOOT,
    ]
    return "".join(parts)


def render_ticker_fragment(tcc: TickerCommandCenter) -> str:
    """Head/foot-less command-center fragment for the unified shell's Holding tab
    (UX redesign PR4 → squeezed to one band in UX9c): a single ~40px utility band
    — the search combobox inline left, verdict · freshness dot · report/DCF/tracker
    links · Ops/Notes icons right — and NOTHING else inline. The config/meta
    sections (refresh, DCF⇄Sheets, analyses log, artifacts, 5-min reread) live in
    the Ops drawer; notes + alerts ride in the shared ✎ Notes drawer; the analysis
    sections the old layout stacked here (position / decisions / thesis) are
    already in the embedded report's own tabs."""
    return _holding_band(tcc)


def _holding_band(tcc: TickerCommandCenter) -> str:
    """The one-line holding utility band (UX9c). Left: the type-ahead combobox
    (replacing the PR4 ticker/name heading + the shell's old cc-picker dropdown).
    Right: verdict + freshness dot + report/DCF/tracker/review/Ledger links +
    Ops/Notes icons. The ✎ Notes button opens the shell's SHARED notes drawer
    (data-cc-notes-open), which scopes to this ticker — so the holding's own
    PR4 notes drawer retires. The Ledger link (PR9) is the one-click doorway
    back to the Ledger tab while its sub-tab row is suppressed (see below).

    The Review link (PR5 — the behavioral guard's only point-of-action doorway
    before this) peeks the instant pre-analysis + live graded-sells base rate
    in place, with an escalation button to the full LLM-calibrated review;
    ``/ticker/<T>`` stays its real href for middle-click / new tab (the
    evaluation-report route also carries the position tab)."""
    ident = tcc.identity
    t = escape(ident.ticker)
    links = [
        f'<a href="/reports/{t}" target="_blank" rel="noopener">Report ↗</a>',
        f'<a href="/dcf/{t}">DCF ↓</a>',
        f'<a href="/ticker/{t}" data-peek-url="/api/peek/review/{t}" '
        f'data-peek-title="Position review · {t}">Review</a>',
    ]
    if tcc.tracker_url:
        links.append(
            f'<a href="{escape(tcc.tracker_url)}" target="_blank" rel="noopener">Tracker ↗</a>'
        )
    # PR9 — the Ledger doorway: while a holding is open the Companies sub-row
    # (which carries the Ledger sub-tab) is suppressed for reading cleanliness
    # (UX9c), so this is the one-click way back to the Ledger without first
    # clearing the holding. A plain panel hash (no ticker) — the shell's
    # hashchange router lands on the Ledger tab exactly like clicking its
    # sub-tab would.
    links.append('<a href="#musings" title="Open the Ledger">Ledger</a>')
    return (
        '<div class="cc-holding-head">'
        f"{_combobox(ident.ticker, ident.name)}"
        '<div class="cc-holding-right">'
        f"{_identity_badges(ident)}{_freshness_dot(ident)}"
        f'<span class="cc-holding-links">{" · ".join(links)}</span>'
        ' <button type="button" class="tcc-drawer-btn k-btn k-btn-quiet k-btn-sm" data-tcc-drawer="ops" '
        'title="Refresh · budgets · DCF⇄Sheets · analyses log · artifacts · 5-min reread">'
        "⚙ Ops</button>"
        ' <button type="button" class="tcc-drawer-btn k-btn k-btn-quiet k-btn-sm" data-cc-notes-open '
        'title="Quick note + open notes + recent alerts for this name">✎ Notes</button>'
        "</div></div>"
    )


def _combobox(ticker: str, name: str | None) -> str:
    """Search-first holding picker (UX9c): a type-ahead combobox over the tracked
    list. The input VALUE is the bare ticker (mono); the company name renders as
    a separate muted overlay inside the field (the canonical two-part label —
    never "T · Name" as one string), hidden while the field has focus so typing
    is unobstructed. On focus it select-alls so the first keystroke filters.
    Selection (click / arrow+Enter) drives the same ``#holding=<T>`` hash the
    old ``cc-picker`` dropdown did — so the shell's activation + deep-link
    contract is unchanged. Tickers come from the shared ``/api/tickers`` source,
    fetched lazily on first focus."""
    cur = escape(ticker, quote=True)
    placeholder = "Search holdings — ticker or name…"
    name_span = (
        f'<span class="cc-combo-name" title="{escape(name, quote=True)}">{escape(name)}</span>'
        if ticker and name
        else ""
    )
    return (
        f'<div class="cc-combo" data-current="{cur}">'
        '<input class="cc-combo-input" type="text" role="combobox" aria-expanded="false" '
        'aria-autocomplete="list" aria-controls="cc-combo-list" autocomplete="off" '
        f'spellcheck="false" value="{cur}" placeholder="{placeholder}" '
        'aria-label="Search holdings">'
        f"{name_span}"
        '<ul class="cc-combo-list" id="cc-combo-list" role="listbox" hidden></ul>'
        "</div>"
    )


def render_holding_picker_band(_repo_root: Path) -> str:
    """The Holding tab's no-ticker state (UX9c): the combobox band alone, with a
    hint — so the picker is always present, including before any holding is
    opened. ``_repo_root`` is accepted for a uniform route signature (the band is
    static; the combobox fetches /api/tickers client-side)."""
    return (
        '<div class="cc-holding-head cc-holding-empty">'
        f"{_combobox('', None)}"
        '<span class="cc-holding-hint">Search a ticker or name to open a holding.</span>'
        "</div>" + _COMBO_STYLE + _COMBO_SCRIPT
    )


def _freshness_dot(ident: TickerIdentity) -> str:
    """One dot instead of three giant cards: the per-source freshness verdict
    (``pipeline.freshness``, PR: canonical latest_dcf_run reader + per-source
    freshness rule) — the SAME rule ``research_cockpit`` uses, so the two
    surfaces can never disagree about a name's data freshness. Was
    ``max(build_age, fmp_age)`` against one shared 7/21d bar; now the FMP pull
    is judged against its own 3/14d bar and the build against its own
    10/30d bar, worst tone wins — a stale FMP pull can no longer hide behind
    a fresh build, or the reverse. Clicking peeks the per-source provenance
    card (UX9d) — ages + inline refresh — with /#system as the real href for
    middle-click."""
    from datetime import UTC as _UTC

    now = datetime.now(_UTC).replace(tzinfo=None)
    tone = freshness_verdict(fmp_at=ident.last_fmp_at, build_at=ident.last_build_at, now=now).tone
    bits = [
        f"Build {fmt_reltime(ident.last_build_at)}" if ident.last_build_at else "Never built",
        f"FMP pull {fmt_reltime(ident.last_fmp_at)}" if ident.last_fmp_at else "No FMP pull",
        f"Transcript {ident.last_transcript_period}" if ident.last_transcript_period else None,
    ]
    title = escape(" · ".join(b for b in bits if b), quote=True)
    t = escape(ident.ticker, quote=True)
    return (
        f'<a class="cc-fdot" href="/#system" '
        f'data-peek-url="/api/peek/provenance?ticker={t}" '
        f'data-peek-title="Data provenance · {t}" title="{title}">'
        f'<span class="k-dot k-dot-{tone}"></span></a>'
    )


def render_holding_fragment(repo_root: Path, ticker: str) -> str:
    """Assemble the Holding-tab panel for ``ticker`` (UX redesign PR4):
    the report IS the page. Slim utility header → collapsed 5-minute reread →
    the embedded ``/reports/<t>`` iframe at full width (it carries the inline
    comment / chat / apply pipeline). Everything operational moved into two
    on-demand drawers: **Ops** (refresh · budget bypass · DCF⇄Sheets ·
    analyses log · artifacts · freshness detail) and **Notes** (open analyst
    notes + recent alerts — the old right-hand rail)."""
    from identity import DEFAULT_USER_ID
    from pipeline.analytical_dashboard import build_analytical_dashboard
    from pipeline.analytical_dashboard_html import render_panel_fragment
    from pipeline.attribution_panel import render_attribution_section
    from pipeline.position_lifecycle_panel import render_position_lifecycle_section

    t = ticker.upper()
    tcc = build_ticker_command_center(repo_root, t)
    db_path = repo_root / "data" / "portfolio.db"
    dash = build_analytical_dashboard(db_path, sections={"rereads"}, ticker=t)
    reread_html = render_panel_fragment(dash, "prereads") or ""
    # The 5-min reread folds into the Ops drawer (UX9c) — one band above the
    # report, nothing else inline. The reread leads the drawer (it's the most
    # likely thing reached for) ahead of the config/meta sections.
    reread_section = (
        '<section class="panel"><h2>5-minute reread</h2>' + reread_html + "</section>"
        if reread_html
        else ""
    )
    ops_body = "".join(
        [
            reread_section,
            # The position-lifecycle timeline (S5 PR2): entry/exit snapshots +
            # post-exit grading. Live-rendered, between the reread and the
            # operational sections — analytical content leads the drawer.
            render_position_lifecycle_section(db_path, t, user_id=DEFAULT_USER_ID),
            # What drove this position's window alpha (S15 PR2): tracker
            # dollar alpha + entry posture + thesis/alert/decision events,
            # phrased deterministically. Degrades tracker-offline.
            render_attribution_section(db_path, t, user_id=DEFAULT_USER_ID),
            _freshness_strip(tcc.identity),
            _refresh_section(t),
            _dcf_sheets_section(t),
            _analyses_section(tcc.analysis),
            _artifacts_section(tcc.artifacts),
        ]
    )
    # Notes + alerts now ride in the shell's SHARED ✎ drawer (PR1), which the
    # band's Notes button opens ticker-scoped — so the holding's own PR4 notes
    # drawer is retired (one notes surface, not two).
    return "".join(
        [
            render_ticker_fragment(tcc),
            f'<div class="tcc-yousaid">{render_you_said_strip_for_path(db_path, t)}</div>',
            _disclosure_change_strip(db_path, t),
            f'<div class="tcc-report-main">{_report_embed_section(t, tcc.report_date)}</div>',
            _tcc_drawer("ops", "Ops · refresh & data", ops_body),
            _COMBO_STYLE,
            _COMBO_SCRIPT,
            _TCC_DRAWER_STYLE,
            _DISCLOSURE_STYLE,
            _YOUSAID_STYLE,
            _TCC_DRAWER_SCRIPT,
        ]
    )


def _disclosure_change_strip(db_path: Path, ticker: str) -> str:
    """Compact, drift-framed disclosure panel with verbatim source receipts.

    Elevation-gated (owner ruling 2026-08-02): only events an LLM judged
    ``thesis_materiality = 'restricts_measurement'`` — the change fundamentally
    restricts the ability to measure the thesis — render here. Unjudged rows
    (NULL) are NOT elevated; the strip reports the on-file / awaiting-judgment
    counts so an empty state is distinguishable from an unswept one. The
    stored ``materiality`` float is three incommensurable per-detector scales
    and must never order or gate this surface.
    """

    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT event_type, fiscal_year, fiscal_period, canonical_id,
                       subject, subject_label, source_doc_id, evidence_quote,
                       verdict, interpretation_md, thesis_materiality_rationale
                FROM disclosure_events
                WHERE ticker = ? AND status != 'dismissed'
                  AND thesis_materiality = 'restricts_measurement'
                ORDER BY created_at DESC, id DESC
                LIMIT 4
                """,
                (ticker.upper(),),
            ).fetchall()
            on_file, judged = conn.execute(
                """
                SELECT COUNT(*), COUNT(thesis_materiality)
                FROM disclosure_events
                WHERE ticker = ? AND status != 'dismissed'
                """,
                (ticker.upper(),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return (
            '<section class="disclosure-strip k-well">'
            "<h2>Disclosure drift</h2>"
            '<p class="muted">Disclosure-change history is unavailable in this database.</p>'
            "</section>"
        )
    counts_note = (
        f"{int(on_file)} drift events on file · {int(on_file) - int(judged)} awaiting "
        "the thesis-materiality judgment"
    )
    if not rows:
        if int(on_file) == 0:
            body = (
                "No stored change events for this name. That is a coverage state, "
                "not evidence that disclosures were unchanged."
            )
        else:
            body = (
                "Nothing judged to restrict measuring the thesis. "
                f"{counts_note}; unjudged events never elevate."
            )
        return (
            '<section class="disclosure-strip k-well">'
            '<div class="disclosure-head"><h2>Disclosure drift</h2>'
            '<span class="k-chip">thesis-materiality gated</span></div>'
            f'<p class="muted">{escape(body)}</p></section>'
        )

    out = [
        '<section class="disclosure-strip k-well">',
        '<div class="disclosure-head"><h2>Disclosure drift</h2>',
        '<span class="k-chip">thesis-materiality gated</span></div>',
        '<p class="sub">Only changes an LLM judged to restrict measuring the thesis '
        f"are shown ({escape(counts_note)}). Read the verdict with the verbatim "
        "receipt; the event label alone has no direction.</p>",
        '<div class="disclosure-rows">',
    ]
    for row in rows:
        verdict = str(row["verdict"] or "unclassified")
        tone = (
            "bad"
            if verdict == "concealment"
            else "warn"
            if verdict in {"substantive", "unclassified"}
            else "ok"
            if verdict == "maturity"
            else ""
        )
        period = " ".join(
            part
            for part in (str(row["fiscal_year"] or ""), str(row["fiscal_period"] or ""))
            if part
        )
        subject = str(row["subject_label"] or row["subject"] or "unnamed subject")
        event_label = str(row["event_type"] or "").replace("_", " ")
        concept = str(row["canonical_id"] or "").replace("_", " ")
        receipt = " ".join(str(row["evidence_quote"] or "").split())
        interpretation = str(row["interpretation_md"] or "").strip()
        out.append('<article class="disclosure-row">')
        out.append('<div class="disclosure-row-head">')
        out.append(
            f'<span class="k-pill{pill_tone_class(tone)}">{escape(verdict)}</span>'
            f'<span class="k-chip k-chip-mono">{escape(period or "period unknown")}</span>'
        )
        out.append(
            f"<strong>{escape(subject)}</strong>"
            f'<span class="muted">{escape(event_label)} · {escape(concept or "cross-document")}</span>'
        )
        if row["source_doc_id"] is not None:
            out.append(
                f'<a class="k-btn k-btn-quiet k-btn-sm" href="/source/{int(row["source_doc_id"])}">'
                "Source</a>"
            )
        out.append("</div>")
        if receipt:
            out.append(f'<p class="disclosure-receipt">“{escape(receipt)}”</p>')
        gate_rationale = " ".join(str(row["thesis_materiality_rationale"] or "").split())
        if gate_rationale:
            out.append(
                f'<p class="disclosure-gate muted">Why elevated: {escape(gate_rationale)}</p>'
            )
        if interpretation:
            out.append(
                f'<div class="disclosure-interpretation">{render_prose(interpretation)}</div>'
            )
        out.append("</article>")
    out.append("</div></section>")
    return "".join(out)


def _tcc_drawer(drawer_id: str, title: str, body: str) -> str:
    return (
        f'<div class="tcc-drawer-scrim" data-tcc-scrim="{escape(drawer_id)}" hidden></div>'
        f'<aside class="tcc-drawer" data-tcc-panel="{escape(drawer_id)}" hidden '
        f'aria-label="{escape(title, quote=True)}">'
        f'<div class="tcc-drawer-head"><span>{escape(title)}</span>'
        f'<button type="button" class="tcc-drawer-close" data-tcc-close="{escape(drawer_id)}" '
        'aria-label="Close">&times;</button></div>'
        f'<div class="tcc-drawer-body">{body}</div></aside>'
    )


_COMBO_STYLE = """<style>
.cc-combo { position: relative; flex: 1 1 280px; max-width: 420px; min-width: 200px; }
/* The input itself is skinned by the shared control kit (ui/controls.py);
   the results list is anchored furniture fused to the bar (see .cc-combo-list
   below), NOT a floating .k-menu popover (feedback #4 / Law 3).
   (.cc-combo prefix: outranks the kit's input[type] baseline so the mono
   ticker face survives the baseline's `font: inherit`.) */
.cc-combo .cc-combo-input { width: 100%; box-sizing: border-box; padding: 6px 11px;
  font-family: var(--mono); font-weight: 600; letter-spacing: 0.02em; }
.cc-combo .cc-combo-input::placeholder { font-family: var(--sans); font-weight: 400;
  letter-spacing: normal; }
/* The two-part label inside the field: muted company name, right-aligned,
   out of the way while typing. */
.cc-combo-name { position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
  color: var(--muted); font-size: var(--fs-caption); max-width: 58%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  pointer-events: none; }
.cc-combo:focus-within .cc-combo-name { display: none; }
/* The results are in-section furniture, not a floating overlay (Law 3 / §6.1):
   they sit flush under the search bar (no gap, no pop shadow) and share the
   focused input's accent border, so the input + results read as ONE seated
   control belonging to the Holding band — not a tooltip hovering over the
   report. Absolute positioning keeps the band height stable. */
.cc-combo-list { position: absolute; z-index: 25; top: 100%; left: 0; right: 0;
  margin: 0; padding: 4px 0; list-style: none; max-height: 320px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--accent); border-top: none;
  border-radius: 0 0 var(--radius) var(--radius); }
.cc-combo-list[hidden] { display: none; }
/* While the results are open the input squares its bottom edge so it meets the
   flush results panel cleanly at the corners (its own 1px bottom border is the
   divider between the search field and its results). */
.cc-combo .cc-combo-input[aria-expanded="true"] { border-bottom-left-radius: 0;
  border-bottom-right-radius: 0; }
.cc-combo-list li { display: flex; align-items: baseline; gap: 8px; padding: 6px 12px;
  cursor: pointer; font-size: var(--fs-body); }
.cc-combo-list li.sel, .cc-combo-list li:hover { background: var(--paper); }
.cc-combo-tk { font-family: var(--mono); font-weight: 600; color: var(--fg); }
.cc-combo-nm { color: var(--muted); font-size: var(--fs-caption);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cc-combo-none { color: var(--muted); cursor: default; }
.cc-holding-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cc-holding-hint { color: var(--muted); font-size: var(--fs-caption); }
</style>"""

_DISCLOSURE_STYLE = """<style>
.disclosure-strip { margin: 10px 0; }
.disclosure-head, .disclosure-row-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.disclosure-head { justify-content:space-between; }
.disclosure-head h2 { margin:0; }
.disclosure-rows { display:grid; gap:8px; margin-top:8px; }
.disclosure-row { display:grid; gap:5px; padding-top:8px; border-top:1px solid var(--border); }
.disclosure-row-head strong { margin-right:auto; }
.disclosure-receipt, .disclosure-gate, .disclosure-interpretation p { margin:0; }
.disclosure-receipt { color:var(--fg); }
</style>"""

# "You said" strip (pipeline/you_said.py) — near the Holding tab's utility
# band, above the disclosure strip: the owner's own last decision on this
# name, ambient rather than a click away.
_YOUSAID_STYLE = """<style>
.tcc-yousaid { margin: 4px 0 10px; font-size: var(--fs-body); }
.tcc-yousaid .ys-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; }
.tcc-yousaid .k-empty { padding: 0; }
</style>"""

# Self-contained combobox wiring, re-run on every fragment inject (the shell's
# injectHtml re-creates <script> tags). Guards on data-wired so a re-inject only
# wires the fresh widget. Navigation is hash-only — identical contract to the
# retired cc-picker <select>.
_COMBO_SCRIPT = """<script>
(function () {
  var combo = document.querySelector('.cc-combo');
  if (!combo || combo.dataset.wired) return;
  combo.dataset.wired = '1';
  var input = combo.querySelector('.cc-combo-input');
  var list = combo.querySelector('.cc-combo-list');
  var all = null, loading = null, matches = [], sel = -1;
  var current = combo.getAttribute('data-current') || '';
  var display = input.value;
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function fetchT() {
    if (all) return Promise.resolve(all);
    if (loading) return loading;
    loading = fetch('/api/tickers').then(function (r) { return r.json(); })
      .then(function (j) { all = (j && j.tickers) || []; return all; })
      .catch(function () { all = []; return all; });
    return loading;
  }
  function listPriority(t) {
    if (t.list_type === 'portfolio') return 0;
    if (t.list_type === 'evaluation') return 1;
    if (t.list_type === 'watchlist') return 2;
    return 9;
  }
  function matchScore(t, ql) {
    var ticker = (t.ticker || '').toLowerCase();
    var name = (t.name || '').toLowerCase();
    if (!ql) return 100;
    if (ticker === ql) return 0;
    if (ticker.indexOf(ql) === 0) return 10;
    if (name === ql) return 20;
    if (name.indexOf(ql) === 0) return 30;
    if (name.split(/\\s+/).some(function (word) { return word.indexOf(ql) === 0; })) return 35;
    if (ticker.indexOf(ql) !== -1) return 40;
    if (name.indexOf(ql) !== -1) return 50;
    return null;
  }
  function render(q, resetSelection) {
    var ql = (q || '').trim().toLowerCase();
    matches = (all || []).map(function (t) {
      return { ticker: t, score: matchScore(t, ql) };
    }).filter(function (row) {
      return row.score !== null;
    }).sort(function (a, b) {
      return a.score - b.score
        || listPriority(a.ticker) - listPriority(b.ticker)
        || a.ticker.ticker.localeCompare(b.ticker.ticker);
    }).slice(0, 12).map(function (row) { return row.ticker; });
    if (resetSelection) sel = matches.length ? 0 : -1;
    else if (sel >= matches.length) sel = matches.length - 1;
    var html = '';
    for (var i = 0; i < matches.length; i++) {
      var t = matches[i];
      html += '<li role="option" id="cc-combo-opt-' + i + '" aria-selected="'
        + (i === sel ? 'true' : 'false') + '" class="' + (i === sel ? 'sel' : '')
        + '" data-i="' + i
        + '" data-tk="' + esc(t.ticker) + '"><span class="cc-combo-tk">' + esc(t.ticker)
        + '</span>' + (t.name ? '<span class="cc-combo-nm">' + esc(t.name) + '</span>' : '')
        + '</li>';
    }
    list.innerHTML = html || '<li class="cc-combo-none">No match.</li>';
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (sel >= 0) input.setAttribute('aria-activedescendant', 'cc-combo-opt-' + sel);
    else input.removeAttribute('aria-activedescendant');
  }
  function open() { fetchT().then(function () { render(input.value, true); }); }
  function close() {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  function pick(tk) {
    if (tk && tk !== current) { location.hash = '#holding=' + encodeURIComponent(tk); }
    else { input.value = display; close(); }
  }
  input.addEventListener('focus', function () { sel = -1; input.select(); open(); });
  input.addEventListener('input', function () {
    fetchT().then(function () { render(input.value, true); });
  });
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'ArrowDown') { ev.preventDefault(); if (matches.length) { sel = Math.min(sel + 1, matches.length - 1); render(input.value, false); } }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); if (matches.length) { sel = Math.max(sel - 1, 0); render(input.value, false); } }
    else if (ev.key === 'Enter') { ev.preventDefault(); if (sel >= 0 && matches[sel]) pick(matches[sel].ticker); }
    else if (ev.key === 'Escape') { input.value = display; close(); input.blur(); }
  });
  list.addEventListener('mousedown', function (ev) {
    var li = ev.target.closest('li[data-tk]');
    if (!li) return;
    ev.preventDefault();  // keep focus off blur until we navigate
    pick(li.getAttribute('data-tk'));
  });
  input.addEventListener('blur', function () {
    // Delay so a list mousedown registers first; then restore the display label.
    setTimeout(function () { if (!list.hidden) { close(); input.value = display; } }, 150);
  });
})();
</script>"""


_TCC_DRAWER_STYLE = """<style>
/* The one-line holding utility band (UX9c): ~40px, combobox left, the
   verdict/freshness/links/icons cluster right. */
.cc-holding-head { display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 12px; min-height: 40px; margin-bottom: 14px; padding-bottom: 10px;
  border-bottom: 1px solid var(--border); }
.cc-fdot { cursor: help; margin-left: 6px; }
a.cc-fdot { text-decoration: none; cursor: pointer; }
.tcc-report-main .cc-report-frame { height: calc(100vh - 200px); height: calc(100dvh - 200px); }
.tcc-drawer-scrim { position: fixed; inset: 0; background: var(--scrim); z-index: 34;
  animation: cc-fade-in var(--transition); }
.tcc-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(680px, 94vw);
  background: var(--bg); border-left: 1px solid var(--border); z-index: 35;
  display: flex; flex-direction: column; box-shadow: var(--shadow-pop);
  animation: cc-slide-in-right var(--transition); }
.tcc-drawer[hidden], .tcc-drawer-scrim[hidden] { display: none; }
.tcc-drawer-head { display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 600; }
.tcc-drawer-close { background: transparent; border: none; color: var(--muted);
  font-size: var(--fs-display); cursor: pointer; line-height: 1; padding: 2px 6px;
  transition: color var(--transition); }
.tcc-drawer-close:hover { color: var(--fg); }
.tcc-drawer-body { overflow-y: auto; padding: 14px 18px 40px; }
/* Standardized overlay motion (mirrors the shell keyframes for the
   standalone /ticker page, which does not load SHELL_CSS). */
@keyframes cc-slide-in-right { from { transform: translateX(18px); opacity: 0; }
  to { transform: none; opacity: 1; } }
@keyframes cc-fade-in { from { opacity: 0; } to { opacity: 1; } }
</style>"""

_TCC_DRAWER_SCRIPT = """<script>
(function () {
  function setOpen(id, open) {
    var panel = document.querySelector('.tcc-drawer[data-tcc-panel="' + id + '"]');
    var scrim = document.querySelector('.tcc-drawer-scrim[data-tcc-scrim="' + id + '"]');
    if (panel) panel.hidden = !open;
    if (scrim) scrim.hidden = !open;
  }
  document.querySelectorAll('.tcc-drawer-btn[data-tcc-drawer]').forEach(function (b) {
    b.addEventListener('click', function () { setOpen(b.getAttribute('data-tcc-drawer'), true); });
  });
  document.querySelectorAll('.tcc-drawer-close[data-tcc-close]').forEach(function (b) {
    b.addEventListener('click', function () { setOpen(b.getAttribute('data-tcc-close'), false); });
  });
  document.querySelectorAll('.tcc-drawer-scrim[data-tcc-scrim]').forEach(function (s) {
    s.addEventListener('click', function () { setOpen(s.getAttribute('data-tcc-scrim'), false); });
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    document.querySelectorAll('.tcc-drawer:not([hidden])').forEach(function (p) {
      setOpen(p.getAttribute('data-tcc-panel'), false);
    });
  });
})();
</script>"""


def _report_embed_section(ticker: str, report_date: str | None) -> str:
    """Embed the full workspace brief in an iframe. The report page boots the
    comment pins + free-text commenting + Ask-Claude chat + apply-diff pipeline
    against this same server, so the Holding tab carries all of it without
    duplicating any of that JS here. 404-safe: a "no brief yet" note when the
    ticker has never been built."""
    t = escape(ticker)
    if not report_date:
        return (
            '<section class="panel"><h2>Full report</h2>'
            '<p class="muted">No workspace brief built yet for this ticker — '
            "open ⚙ Ops above and hit Refresh to build one.</p></section>"
        )
    # Report-first (PR4): no panel heading/sub — the report carries its own
    # identity header; this wrapper exists only for the frame border.
    return (
        '<section class="panel cc-report-embed" style="padding:6px">'
        f'<iframe class="cc-report-frame" src="/reports/{t}" '
        f'title="{t} workspace report" loading="lazy"></iframe>'
        "</section>"
    )


def _notes_rail_section(notes: list[AnalystNoteRow] | None) -> str:
    """The rail's "Open notes" panel: live analyst memory for this name,
    newest first. Read-only here — the notes lifecycle UI (resolve /
    reclassify / supersede) is the P4.5 journal; this surfaces what's open
    while the analyst reads the report."""
    out = ['<section class="panel cc-rail-panel"><h2>Open notes</h2>']
    if notes is None:
        out.append(
            '<p class="muted">Notes substrate unavailable — the analyst_notes '
            "table is not in this DB.</p>"
        )
    elif not notes:
        out.append(
            '<p class="muted">No open notes on this name. Notes arrive from '
            "report comments, chat, and alert flows.</p>"
        )
    else:
        out.append(
            f'<p class="sub">Open questions, watch-items, and decisions — newest first, '
            f"up to {_RAIL_NOTES_LIMIT}.</p>"
        )
        for n in notes:
            meta_bits = [f"via {escape(n.source)}"]
            if n.anchor_key:
                meta_bits.append(f"<code>{escape(n.anchor_key)}</code>")
            out.append(
                f'<div class="rail-note nk-{escape(n.kind)}">'
                '<div class="rail-note-head">'
                f'<span class="rail-note-kind">{escape(n.kind)}</span>'
                f"{stamp_html(n.created_at, mode='date', css='rail-note-when')}"
                "</div>"
                f'<div class="rail-note-body">{render_prose(n.body)}</div>'
                f'<div class="rail-note-meta">{" · ".join(meta_bits)}</div>'
                "</div>"
            )
    out.append("</section>")
    return "".join(out)


def _alerts_rail_section(
    ticker: str,
    alerts: list[tuple[AlertRow, list[QueuedActionRow]]] | None,
    brief_provenance: Mapping[str, object] | None,
) -> str:
    """The rail's "Recent alerts" panel: the newest fired alerts for this
    name as the same cards the digest/feed render (status badge + memo +
    queued actions), with the evidence drawer collapsed for rail density."""
    out = ['<section class="panel cc-rail-panel"><h2>Recent alerts</h2>']
    if alerts is None:
        out.append(
            '<p class="muted">Alerts substrate unavailable — the alerts table '
            "is not in this DB.</p>"
        )
    elif not alerts:
        out.append('<p class="muted">No alerts fired on this name yet.</p>')
    else:
        out.append(
            f'<p class="sub">Newest {len(alerts)} fired — '
            f'<a href="/feed?ticker={escape(ticker)}">full feed ↗</a></p>'
        )
        buf = StringIO()
        for alert, actions in alerts:
            render_alert_card(
                buf,
                alert,
                actions=actions,
                show_status_badge=True,
                brief_provenance=brief_provenance,
                drawer_open=False,
            )
        out.append(buf.getvalue())
    out.append("</section>")
    return "".join(out)


def _identity_badges(ident: TickerIdentity) -> str:
    bits: list[str] = []
    if ident.list_type:
        # list_type is a category label → the quiet outline kit chip (§2).
        bits.append(f'<span class="k-chip">{escape(ident.list_type)}</span>')
    breach = ident.breach_status
    if breach:
        # breach status → the kit filled status pill + the SHARED tone
        # resolver (the local dict here missed `warn`, which rendered as a
        # neutral gray pill while every other surface showed amber).
        cls = f"k-pill{pill_tone_class(thesis_status_tone(breach))}"
        bits.append(f'<span class="{cls}">{escape(breach)}</span>')
    return f'<div class="badges">{"".join(bits)}</div>'


def _freshness_strip(ident: TickerIdentity) -> str:
    # Relative stamps ("26d ago") — staleness is the question this strip
    # answers; the exact instant rides in the tooltip.
    cells = [
        ("Last build", stamp_html(ident.last_build_at)),
        ("Last FMP pull", stamp_html(ident.last_fmp_at)),
        ("Last transcript", f"<span>{escape(ident.last_transcript_period or '—')}</span>"),
    ]
    inner = "".join(
        f'<div class="fresh-cell"><div class="fresh-label">{escape(lbl)}</div>'
        f'<div class="fresh-val">{val}</div></div>'
        for lbl, val in cells
    )
    return f'<div class="fresh-strip">{inner}</div>'


def _position_section(pos: PositionStrip) -> str:
    if not pos.available:
        return (
            '<section class="panel"><h2>Position</h2>'
            '<p class="muted">Portfolio-tracker not connected. Set PORTFOLIO_TRACKER_URL and run it '
            "alongside this app to see live shares / cost / P&amp;L here.</p></section>"
        )
    if not pos.held:
        return (
            '<section class="panel"><h2>Position</h2>'
            '<p class="muted">No current position in this name.</p></section>'
        )
    pnl = pos.total_unrealized_pnl
    pct = pos.total_unrealized_pct
    pnl_tone = "k-num-pos" if (pnl or 0) >= 0 else "k-num-neg"
    rows = "".join(
        f"<tr><td>{escape(a.account_name)}</td>"
        f'<td class="num">{a.quantity:,.2f}</td>'
        f'<td class="num">{_money(a.market_value)}</td></tr>'
        for a in pos.accounts
    )
    decision = (
        f'<p class="sub">Last decision: {escape(pos.last_decision)}</p>'
        if pos.last_decision
        else ""
    )
    return (
        '<section class="panel"><h2>Position</h2>'
        f'<div class="kpi-strip">'
        f'<div class="kpi-card"><div class="kpi-label">Shares</div><div class="kpi-value">{(pos.total_quantity or 0):,.2f}</div></div>'
        f'<div class="kpi-card"><div class="kpi-label">Cost basis</div><div class="kpi-value">{_money(pos.total_cost_basis)}</div></div>'
        f'<div class="kpi-card"><div class="kpi-label">Market value</div><div class="kpi-value">{_money(pos.total_market_value)}</div></div>'
        f'<div class="kpi-card"><div class="kpi-label">Unrealized P&amp;L</div>'
        f'<div class="kpi-value {pnl_tone}">{_money(pnl)}{f" ({pct * 100:+.1f}%)" if pct is not None else ""}</div></div>'
        "</div>"
        f"{decision}"
        f'<table><thead><tr><th>Account</th><th class="num">Shares</th><th class="num">Value</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
        "</section>"
    )


def _analyses_section(log: AnalysisLog) -> str:
    if not log.rows:
        return (
            '<section class="panel"><h2>Analyses run</h2>'
            '<p class="muted">No analysis tables present yet.</p></section>'
        )
    body = "".join(
        f"<tr><td>{escape(r.analysis)}</td><td>{_date(r.last_run)}</td>"
        f"<td>{escape(r.summary)}</td></tr>"
        for r in log.rows
    )
    out = [
        '<section class="panel"><h2>Analyses run</h2>',
        "<table><thead><tr><th>Analysis</th><th>Last run</th><th>Summary</th></tr></thead>",
        f"<tbody>{body}</tbody></table>",
    ]
    if log.recent_alerts:
        alerts = "".join(
            f"<tr><td>{escape(a.trigger_kind)}</td><td>{_date(a.fired_at)}</td>"
            f"<td>{escape(a.status)}</td></tr>"
            for a in log.recent_alerts
        )
        out.append(
            '<h3 class="panel-h3">Recent alerts</h3>'
            "<table><thead><tr><th>Trigger</th><th>Fired</th><th>Status</th></tr></thead>"
            f"<tbody>{alerts}</tbody></table>"
        )
    if log.recent_llm_calls:
        calls = "".join(
            f"<tr><td>{escape(c.purpose or '—')}</td><td>{escape(c.model or '—')}</td>"
            f'<td class="num">{_money(c.cost_usd)}</td><td>{_date(c.called_at)}</td></tr>'
            for c in log.recent_llm_calls
        )
        out.append(
            f'<h3 class="panel-h3">Recent LLM calls · ${log.llm_cost_30d_usd:,.2f} (30d)</h3>'
            '<table><thead><tr><th>Purpose</th><th>Model</th><th class="num">Cost</th><th>When</th></tr></thead>'
            f"<tbody>{calls}</tbody></table>"
        )
    out.append("</section>")
    return "".join(out)


def _decisions_section(decisions: list[DecisionLite]) -> str:
    if not decisions:
        return ""
    body = "".join(
        f"<tr><td>{_date(d.made_at)}</td>"
        f"<td>{escape(d.recommendation_kind.upper())}"
        f"{f' {d.recommendation_value:g}%' if d.recommendation_value is not None else ''}</td>"
        f"<td>{escape(d.conviction or '—')}</td>"
        f"<td>{escape(d.outcome_label or 'pending')}</td></tr>"
        for d in decisions
    )
    return (
        '<section class="panel"><h2>Recent decisions</h2>'
        "<table><thead><tr><th>When</th><th>Recommendation</th><th>Conviction</th><th>Outcome</th></tr></thead>"
        f"<tbody>{body}</tbody></table></section>"
    )


def _artifacts_section(artifacts: list[Artifact]) -> str:
    if not artifacts:
        return ""
    out = ['<section class="panel"><h2>Artifacts</h2>']
    seen: set[str] = set()
    categories = [a.category for a in artifacts if not (a.category in seen or seen.add(a.category))]
    for cat in categories:
        rows = "".join(_artifact_row(a) for a in artifacts if a.category == cat)
        out.append(
            f'<h3 class="panel-h3">{escape(cat)}</h3>'
            '<table class="artifact-table"><thead><tr><th>Artifact</th><th>Path</th>'
            "<th>Status</th><th>Modified</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    out.append("</section>")
    return "".join(out)


def _artifact_row(a: Artifact) -> str:
    if a.exists:
        status = f"{a.count} file(s)" if a.count is not None else _size(a.size_bytes)
        status_html = f'<span class="k-dot k-dot-ok"></span> {escape(status)}'
    else:
        status_html = '<span class="muted">absent</span>'
    return (
        f"<tr><td>{escape(a.label)}</td>"
        f"<td><code>{escape(a.path)}</code></td>"
        f"<td>{status_html}</td>"
        f"<td>{_date(a.modified_at)}</td></tr>"
    )


def _thesis_section(th: ThesisView) -> str:
    if not th.present:
        return (
            '<section class="panel"><h2>Thesis</h2>'
            '<p class="muted">No holdings JSON for this ticker.</p></section>'
        )
    out = ['<section class="panel"><h2>Thesis</h2>']
    meta: list[str] = []
    if th.verdict:
        meta.append(f"verdict: <strong>{escape(th.verdict)}</strong>")
    if th.last_updated:
        meta.append(stamp_html(th.last_updated, mode="date", prefix="updated "))
    if meta:
        out.append(f'<p class="sub">{" · ".join(meta)}</p>')
    if th.thesis:
        out.append(f"<p>{escape(th.thesis)}</p>")
    if th.tier1:
        rows = "".join(
            f"<tr><td>{escape(k.name)}</td><td>{escape(k.current or '—')}</td>"
            f"<td>{escape(k.status or '—')}</td><td>{escape(k.break_condition or '—')}</td></tr>"
            for k in th.tier1
        )
        out.append(
            '<h3 class="panel-h3">Tier-1 KPIs</h3>'
            "<table><thead><tr><th>KPI</th><th>Current</th><th>Status</th><th>Break condition</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    if th.break_rules:
        rows = "".join(
            f"<tr><td>{escape(r.kpi_name)}</td>"
            f"<td>{escape((r.comparator or '') + ' ' + (r.threshold or '') + ' ' + (r.unit or ''))}</td>"
            f"<td>{escape(r.narrative or '')}</td></tr>"
            for r in th.break_rules
        )
        out.append(
            '<h3 class="panel-h3">Break rules</h3>'
            "<table><thead><tr><th>KPI</th><th>Trigger</th><th>Narrative</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    if th.qualitative_breakers:
        items = "".join(f"<li>{escape(b)}</li>" for b in th.qualitative_breakers)
        out.append(f'<h3 class="panel-h3">Qualitative breakers</h3><ul>{items}</ul>')
    out.append("</section>")
    return "".join(out)


def _date(iso: str | None) -> str:
    """Calendar-date stamp with the exact instant in the tooltip (PR1 human-time
    convention). Returns HTML — callers embed raw, never re-escape."""
    if not iso:
        return "—"
    return stamp_html(str(iso), mode="date")


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}"


def _size(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} KB"
    return f"{n} B"


# The whole head goes through str.format(ticker=, generated_at=), so the
# palette block's literal CSS braces are doubled before splicing.
_PAGE_HEAD = (
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ticker} · command center</title>
"""
    + FAVICON_LINK
    + "\n<style>\n"
    + (palette_css("dark") + controls_css("dark")).replace("{", "{{").replace("}", "}}")
    + """
  body {{ margin: 0; padding: 24px; font-family: var(--sans); background: var(--bg); color: var(--fg); line-height: 1.5; font-size: var(--fs-body); }}
  header {{ margin-bottom: var(--sp-3); border-bottom: 1px solid var(--border); padding-bottom: 10px; display: flex; justify-content: space-between; align-items: flex-start; }}
  h1 {{ font-size: var(--fs-display); margin: 0 0 4px; font-weight: 600; }}
  h1 .k-tick-name {{ font-size: var(--fs-body); }}
  h2 {{ font-size: var(--fs-title); margin: 0 0 8px; font-weight: 600; }}
  a {{ transition: color var(--transition); }}
  .top-nav {{ font-size: var(--fs-caption); }}
  .top-nav a {{ color: var(--accent); text-decoration: none; }}
  .top-nav a:hover {{ text-decoration: underline; }}
  .badges {{ text-align: right; }}
  /* identity badges → the kit (.k-chip list_type + .k-pill breach status). */
  .panel {{ margin-bottom: var(--sp-4); background: var(--surface); border-radius: var(--radius); padding: 14px 16px; }}
  .panel .sub {{ color: var(--muted); font-size: var(--fs-caption); margin: 0 0 12px; }}
  .panel-h3 {{ font-size: var(--fs-title); margin: 16px 0 6px; color: var(--fg); font-weight: 600; }}
  .muted {{ color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: var(--fs-body); font-variant-numeric: tabular-nums; }}
  th {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid var(--hairline); vertical-align: top; }}
  tbody tr:hover td {{ background: var(--paper); }}
  td.num {{ text-align: right; }}
  code {{ font-family: var(--mono); font-size: 0.93em; color: var(--fg-soft); }}
  .fresh-strip {{ display: flex; gap: 1px; margin-bottom: var(--sp-4); background: var(--border); border-radius: var(--radius); overflow: hidden; }}
  .fresh-cell {{ background: var(--surface); padding: 8px 14px; flex: 1; }}
  .fresh-label {{ font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }}
  .fresh-val {{ font-size: var(--fs-body); font-variant-numeric: tabular-nums; }}
  .kpi-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 1px; margin-bottom: 12px; background: var(--border); border-radius: var(--radius); overflow: hidden; }}
  .kpi-card {{ background: var(--surface); padding: 10px 12px; }}
  .kpi-label {{ font-size: var(--fs-caption); text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }}
  .kpi-value {{ font-size: var(--fs-title); font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
  li {{ margin-bottom: 3px; }}
</style>
</head>
<body>
<div class="stamp" style="display:none">{generated_at}</div>
"""
)

_PAGE_FOOT = "</body></html>"
