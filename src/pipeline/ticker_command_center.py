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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import cast

from pipeline.analysis_log import AnalysisLog, build_analysis_log
from pipeline.artifact_inventory import Artifact, build_artifact_inventory

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
    tier1: list[ThesisKpi] = field(default_factory=list)
    break_rules: list[ThesisBreakRule] = field(default_factory=list)
    qualitative_breakers: list[str] = field(default_factory=list)

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
    accounts: list[PositionAccount] = field(default_factory=list)
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
    artifacts: list[Artifact] = field(default_factory=list)
    analysis: AnalysisLog = field(default_factory=AnalysisLog)
    recent_decisions: list[DecisionLite] = field(default_factory=list)
    thesis: ThesisView = field(default_factory=ThesisView)
    position: PositionStrip = field(default_factory=PositionStrip)
    tracker_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "analysis": self.analysis.to_dict(),
            "recent_decisions": [d.to_dict() for d in self.recent_decisions],
            "thesis": self.thesis.to_dict(),
            "position": self.position.to_dict(),
            "tracker_url": self.tracker_url,
        }


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
def build_ticker_command_center(repo_root: Path, ticker: str) -> TickerCommandCenter:
    t = ticker.upper()
    db_path = repo_root / "data" / "portfolio.db"
    conn: sqlite3.Connection | None = None
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    try:
        identity = _identity(conn, repo_root, t)
        analysis = build_analysis_log(conn, t) if conn is not None else AnalysisLog()
        decisions = _recent_decisions(conn, t) if conn is not None else []
    finally:
        if conn is not None:
            conn.close()
    return TickerCommandCenter(
        identity=identity,
        artifacts=build_artifact_inventory(repo_root, t),
        analysis=analysis,
        recent_decisions=decisions,
        thesis=_thesis_view(repo_root, t),
        position=_position_strip(repo_root, t),
        tracker_url=_tracker_url(t),
    )


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
        row = conn.execute(
            "SELECT period_end FROM transcripts WHERE UPPER(ticker)=? AND period_end IS NOT NULL "
            "ORDER BY period_end DESC LIMIT 1",
            (t,),
        ).fetchone()
        ident.last_transcript_period = (
            str(row["period_end"])[:10] if row and row["period_end"] else None
        )
    if _has(conn, "thesis_evaluations"):
        row = conn.execute(
            "SELECT overall_status FROM thesis_evaluations WHERE UPPER(ticker)=? "
            "ORDER BY evaluated_at DESC LIMIT 1",
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
        last_decision = f"{d.action} ({d.decision_date.isoformat()})"
    elif section.closed_decisions:
        d = section.closed_decisions[0]
        last_decision = f"{d.action} → {d.outcome_status} ({d.decision_date.isoformat()})"
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
    return f"{base}/trade-analysis?ticker={t}"


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
    fetch('/actions/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ticker: b.getAttribute('data-ticker'),
        mode: 'stale',
        force_budget_bypass: b.getAttribute('data-bypass') === '1'
      })
    }).then(function (r) { return r.json(); }).then(function (j) {
      msg.innerHTML = j.job_id
        ? 'started \\u2014 <a href="' + j.stream_url + '">view log</a>'
        : ('error: ' + (j.error || 'failed'));
    }).catch(function () { msg.textContent = 'network error'; });
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
        f'<button type="button" class="tcc-refresh" data-ticker="{t}" data-bypass="0">'
        "Refresh</button> "
        f'<button type="button" class="tcc-refresh" data-ticker="{t}" data-bypass="1">'
        "Run anyway (ignore caps)</button> "
        '<span class="tcc-refresh-msg muted"></span>'
        '<p style="margin-top:8px">'
        f'<label><input type="checkbox" class="tcc-bypass-toggle" data-ticker="{t}"> '
        "Always ignore budget caps for this ticker (persistent)</label> "
        '<span class="tcc-bypass-msg muted"></span></p>'
        + _REFRESH_SCRIPT
        + "</section>"
    )


def render_ticker_html(tcc: TickerCommandCenter, *, generated_at: datetime) -> str:
    ident = tcc.identity
    title = f"{ident.ticker}" + (f" · {ident.name}" if ident.name else "")
    parts: list[str] = [
        _PAGE_HEAD.format(
            ticker=escape(ident.ticker),
            generated_at=escape(generated_at.isoformat(timespec="seconds")),
        ),
        f"<header><div><h1>{escape(title)}</h1>",
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
        _position_section(tcc.position),
        _analyses_section(tcc.analysis),
        _decisions_section(tcc.recent_decisions),
        _artifacts_section(tcc.artifacts),
        _thesis_section(tcc.thesis),
        _PAGE_FOOT,
    ]
    return "".join(parts)


def _identity_badges(ident: TickerIdentity) -> str:
    bits: list[str] = []
    if ident.list_type:
        bits.append(f'<span class="badge">{escape(ident.list_type)}</span>')
    breach = ident.breach_status
    if breach:
        tone = {
            "intact": "b-ok",
            "ok": "b-ok",
            "watch": "b-warn",
            "broken": "b-bad",
            "breach": "b-bad",
        }.get(breach, "b-muted")
        bits.append(f'<span class="badge {tone}">{escape(breach)}</span>')
    return f'<div class="badges">{"".join(bits)}</div>'


def _freshness_strip(ident: TickerIdentity) -> str:
    cells = [
        ("Last build", _date(ident.last_build_at)),
        ("Last FMP pull", _date(ident.last_fmp_at)),
        ("Last transcript", ident.last_transcript_period or "—"),
    ]
    inner = "".join(
        f'<div class="fresh-cell"><div class="fresh-label">{escape(lbl)}</div>'
        f'<div class="fresh-val">{escape(val)}</div></div>'
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
    pnl_tone = "pos" if (pnl or 0) >= 0 else "neg"
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
        f'<div class="kpi-card {pnl_tone}"><div class="kpi-label">Unrealized P&amp;L</div>'
        f'<div class="kpi-value">{_money(pnl)}{f" ({pct * 100:+.1f}%)" if pct is not None else ""}</div></div>'
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
        f"<tr><td>{escape(d.made_at[:10])}</td>"
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
        status_html = f'<span class="ok-dot">●</span> {escape(status)}'
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
        meta.append(f"updated {escape(th.last_updated)}")
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
    if not iso:
        return "—"
    return escape(str(iso)[:10])


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


_PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{ticker} · command center</title>
<style>
  body {{ margin: 0; padding: 24px; font-family: 'Inter', -apple-system, sans-serif; background: #0c0d10; color: #e5e5e2; line-height: 1.5; font-size: 14px; }}
  header {{ margin-bottom: 20px; border-bottom: 1px solid #2a2c30; padding-bottom: 12px; display: flex; justify-content: space-between; align-items: flex-start; }}
  h1 {{ font-size: 24px; margin: 0 0 4px; font-weight: 600; }}
  h2 {{ font-size: 17px; margin: 0 0 8px; font-weight: 600; }}
  .top-nav {{ font-size: 12px; }}
  .top-nav a {{ color: #6ea8fe; text-decoration: none; }}
  .top-nav a:hover {{ text-decoration: underline; }}
  .badges {{ text-align: right; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; background: #2a2c30; margin-left: 4px; }}
  .badge.b-ok {{ background: #14532d; color: #4ade80; }}
  .badge.b-warn {{ background: #422006; color: #fbbf24; }}
  .badge.b-bad {{ background: #450a0a; color: #f87171; }}
  .panel {{ margin-bottom: 24px; background: #16171a; border: 1px solid #2a2c30; border-radius: 8px; padding: 16px 18px; }}
  .panel .sub {{ color: #999; font-size: 12px; margin: 0 0 12px; }}
  .panel-h3 {{ font-size: 13px; margin: 16px 0 6px; color: #f5f5f0; font-family: 'JetBrains Mono', monospace; text-transform: uppercase; letter-spacing: 0.4px; }}
  .muted {{ color: #888; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 6px 10px; border-bottom: 2px solid #2a2c30; font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; color: #888; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #1f2125; vertical-align: top; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: #b8b8b0; }}
  .pos .kpi-value, td.pos {{ color: #4ade80; }}
  .neg .kpi-value, td.neg {{ color: #f87171; }}
  .ok-dot {{ color: #4ade80; }}
  .fresh-strip {{ display: flex; gap: 10px; margin-bottom: 22px; }}
  .fresh-cell {{ background: #16171a; border: 1px solid #2a2c30; border-radius: 6px; padding: 8px 14px; flex: 1; }}
  .fresh-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; }}
  .fresh-val {{ font-size: 15px; font-variant-numeric: tabular-nums; }}
  .kpi-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 12px; }}
  .kpi-card {{ background: #1f2125; border: 1px solid #2a2c30; border-radius: 6px; padding: 10px 12px; }}
  .kpi-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; }}
  .kpi-value {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
  li {{ margin-bottom: 3px; }}
</style>
</head>
<body>
<div class="stamp" style="display:none">{generated_at}</div>
"""

_PAGE_FOOT = "</body></html>"
