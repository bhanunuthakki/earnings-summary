"""Credibility panel — is the stored confidence number actually calibrated? (L10).

The read side of the self-calibrating credibility engine. Reads the
``confidence_observations`` ledger (graded restatements + resolved
disagreements) via ``credibility.calibration`` and renders the reliability
table + Brier score on the Provenance console, right after Restatements (which
are the ground truth this panel scores against).

Each reliability row shows the *predicted* confidence band against the
*observed* hold-rate; a gap is miscalibration. Sparse cells (below the shared
minimum-n floor) are shown but tagged low-confidence — labelling thin data is
more honest than hiding it. The panel degrades to a quiet stub when the ledger
is absent (pre-0106 DB) or empty (no restatements/disagreements observed yet).
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

from calibration_guard import confidence_note
from credibility.calibration import ReliabilityTable, build_reliability_table

_PANEL_STYLE = """<style>
.cred-note { margin-top:14px; padding:10px 13px; background:var(--paper);
  border:1px solid var(--border); border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.55; }
.cred-over { color:var(--bad); }
.cred-under { color:var(--ok); }
.cred-spark { font-weight:600; }
.cred-thin { color:var(--muted); font-size:var(--fs-caption); }
</style>"""


def render_credibility_panel(db_path: Path, *, user_id: str = "bhanu") -> str:
    """The Credibility tab fragment: reliability table + Brier over the ledger."""
    table = _load(db_path, user_id=user_id)
    if table is None:
        return (
            '<section class="panel"><h2>Credibility</h2>'
            '<p class="muted">No confidence-observation ledger in this DB — run '
            "<code>alembic upgrade head</code> (0106) then "
            "<code>python execution/build_confidence_observations.py --apply</code>.</p></section>"
        )
    if table.total_n == 0:
        return (
            _PANEL_STYLE + '<section class="panel"><h2>Credibility</h2>'
            '<p class="sub">Is the stored confidence number calibrated? This scores it '
            "against what later happened to each fact.</p>"
            '<div class="cred-note">No graded observations yet. The ledger fills as later '
            "filings restate already-reported numbers and cross-source disagreements get "
            "resolved.</div></section>"
        )
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Credibility</h2>',
            '<p class="sub">Stored confidence (the <em>predicted</em> prior) scored against '
            "the <em>observed</em> hold-rate of each fact a later filing revisited or a "
            "cross-source disagreement contested. A gap is miscalibration.</p>",
            _kpi_strip(table),
            _reliability_table(table),
            _tier_table(table),
            _footnote(table),
            "</section>",
        ]
    )


def _load(db_path: Path, *, user_id: str) -> ReliabilityTable | None:
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        return build_reliability_table(conn, user_id=user_id)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _kpi_strip(t: ReliabilityTable) -> str:
    brier = "—" if t.brier is None else f"{t.brier:.3f}"
    cards = [
        '<div class="kpi-card"><div class="kpi-label">Brier score</div>'
        f'<div class="kpi-value">{brier}</div>'
        '<div class="kpi-sub">lower = better (0.25 = no skill)</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Observed hold-rate</div>'
        f'<div class="kpi-value">{_pct(t.observed_rate)}</div>'
        f'<div class="kpi-sub">predicted {_pct(t.mean_predicted)}</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Observations</div>'
        f'<div class="kpi-value">{t.total_n:,}</div>'
        f'<div class="kpi-sub">{confidence_note(t.total_n, min_n=t.min_n)}</div></div>',
        '<div class="kpi-card"><div class="kpi-label">Sources</div>'
        f'<div class="kpi-value">{t.restatement_n:,} · {t.disagreement_n:,}</div>'
        '<div class="kpi-sub">restatements · disagreements</div></div>',
    ]
    return '<div class="kpi-strip">' + "".join(cards) + "</div>"


def _gap_cell(predicted: float, observed: float, n: int, *, min_n: int) -> str:
    gap = predicted - observed
    if not (n >= min_n):
        return f'<td class="num cred-thin">{gap * 100:+.0f} pp</td>'
    cls = "cred-over" if gap > 0 else "cred-under"
    return f'<td class="num {cls}">{gap * 100:+.0f} pp</td>'


def _reliability_table(t: ReliabilityTable) -> str:
    body: list[str] = []
    for b in t.buckets:
        tag = "" if b.confident else ' <span class="cred-thin">(low n)</span>'
        body.append(
            "<tr>"
            f"<td>{escape(b.label)}{tag}</td>"
            f'<td class="num">{b.n:,}</td>'
            f'<td class="num">{_pct(b.mean_predicted)}</td>'
            f'<td class="num cred-spark">{_pct(b.observed_rate)}</td>'
            f"{_gap_cell(b.mean_predicted, b.observed_rate, b.n, min_n=t.min_n)}"
            "</tr>"
        )
    return (
        "<h3>Reliability by confidence band</h3>"
        '<table class="p-table"><thead><tr>'
        "<th>Predicted band</th>"
        '<th class="num">n</th><th class="num">Predicted</th>'
        '<th class="num">Observed held</th><th class="num">Gap</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _tier_table(t: ReliabilityTable) -> str:
    if not t.tiers:
        return ""
    body: list[str] = []
    for tr in t.tiers:
        tag = "" if tr.confident else ' <span class="cred-thin">(low n)</span>'
        body.append(
            "<tr>"
            f"<td>{escape(tr.tier)}{tag}</td>"
            f'<td class="num">{tr.n:,}</td>'
            f'<td class="num">{_pct(tr.mean_predicted)}</td>'
            f'<td class="num cred-spark">{_pct(tr.observed_rate)}</td>'
            f'<td class="num">{tr.brier:.3f}</td>'
            f"{_gap_cell(tr.mean_predicted, tr.observed_rate, tr.n, min_n=t.min_n)}"
            "</tr>"
        )
    return (
        "<h3>Reliability by source tier</h3>"
        '<table class="p-table"><thead><tr>'
        "<th>Tier</th>"
        '<th class="num">n</th><th class="num">Predicted</th>'
        '<th class="num">Observed held</th><th class="num">Brier</th><th class="num">Gap</th>'
        f"</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _footnote(t: ReliabilityTable) -> str:
    gate = (
        "At least one cell has crossed the minimum-n floor — a measured prior may "
        "replace the hand-set constant for it."
        if t.has_confident_cell
        else "No cell has reached the minimum-n floor yet — every confidence number is "
        "still the hand-set constant, by design."
    )
    return (
        '<div class="cred-note">Conditional denominator: an outcome is only observable for a '
        "fact a later filing revisited (a restatement chain) or that a logged cross-source "
        "disagreement contested — so <em>observed held</em> is the hold-rate among checked "
        f"facts, not unconditional accuracy. {escape(gate)}</div>"
    )
