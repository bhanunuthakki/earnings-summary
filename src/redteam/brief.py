"""Render the monthly Red Team Brief — one dense page of items, kit-composed
(monthly_red_team.md Phase 2: "one dense Red Team Brief (ui-kit composed):
each item = falsifiable attack + one question + a proposed rule/scenario
change").

Pure render function: no DB access here (``pipeline.red_team_panel`` owns the
DB read + composes this). Items render READ-ONLY in PR5 — status chip only,
no response actions (PR6's scope, per the task spec).
"""

from __future__ import annotations

from html import escape

from redteam.models import RedTeamItemRow
from ui.controls import panel_toolbar, ticker_label
from ui.prose import render_prose

# ---------------------------------------------------------------------------
# Self-registered style module (design_language / tests/test_ui_controls.py):
# layout + severity/status tone only — every color/size/radius rides a token,
# never a raw hex or off-scale value. Registered as "redteam/brief.py" in
# tests/test_ui_controls.py's REGISTERED set.
# ---------------------------------------------------------------------------
_BRIEF_CSS = """<style>
.rt-brief { display: flex; flex-direction: column; gap: var(--sp-3); }
.rt-empty { color: var(--muted); font-size: var(--fs-body); padding: var(--sp-4) 0; }
.rt-group-title { font-size: var(--fs-caption); font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.06em; margin: var(--sp-2) 0 0; }
.rt-item { display: flex; flex-direction: column; gap: var(--sp-2); }
.rt-item-head { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.rt-item-cross { color: var(--fg-soft); font-size: var(--fs-body); font-weight: 600; }
.rt-item .prose { margin-top: var(--sp-1); }
.rt-item .prose p { margin: 0 0 var(--sp-2); }
</style>"""

_SEVERITY_TONE: dict[str, str] = {"high": "bad", "med": "warn", "low": "ok"}
_STATUS_TONE: dict[str, str] = {
    "open": "accent",
    "refuted": "ok",
    "accepted": "ok",
    "deferred": "warn",
    "closed": "",
}
_LENS_LABEL: dict[str, str] = {
    "shared_factor": "Shared Factor",
    "fx_translation": "FX Translation",
    "competitive_encroachment": "Competitive Encroachment",
    "model_vs_market": "Model vs Market",
    "behavioral_consistency": "Behavioral Consistency",
    "factor_block": "Factor Block",
    "style_drift": "Style Drift",
    "human_capital": "Human Capital",
}


def _lens_chip(lens: str) -> str:
    label = escape(_LENS_LABEL.get(lens, lens))
    return f'<span class="k-chip k-chip-mono">{label}</span>'


def _severity_pill(severity: str) -> str:
    tone = _SEVERITY_TONE.get(severity, "")
    cls = f"k-pill k-pill-{tone}".strip() if tone else "k-pill"
    return f'<span class="{cls}">{escape(severity.upper())}</span>'


def _status_chip(status: str) -> str:
    tone = _STATUS_TONE.get(status, "")
    cls = f"k-chip k-chip-{tone}".strip() if tone else "k-chip"
    return f'<span class="{cls}">{escape(status.upper())}</span>'


def _render_item(item: RedTeamItemRow) -> str:
    tone = _SEVERITY_TONE.get(item.severity, "")
    well_cls = f"k-well k-well-{tone} rt-item".strip() if tone else "k-well rt-item"
    subject = (
        ticker_label(item.ticker)
        if item.ticker
        else '<span class="rt-item-cross">Cross-book</span>'
    )
    head = (
        f'<div class="rt-item-head">{_lens_chip(item.lens)}{_severity_pill(item.severity)}'
        f"{subject}{_status_chip(item.status)}</div>"
    )
    body_md = (
        f"**Attack:** {item.attack_md}\n\n"
        f"**Question:** {item.question_md}\n\n"
        f"**Proposed change:** {item.proposed_change_md}"
    )
    return f'<div class="{well_cls}">{head}<div class="prose">{render_prose(body_md)}</div></div>'


def render_red_team_brief(items: list[RedTeamItemRow], *, run_key: str | None = None) -> str:
    """One dense Red Team Brief fragment. Empty state when ``items`` is
    empty (no run yet, or the latest run produced nothing)."""
    title = f"Red Team — {run_key}" if run_key else "Red Team"
    toolbar = panel_toolbar(title)
    if not items:
        empty = (
            '<p class="rt-empty">No red-team items yet. The First-Saturday run '
            "generates one adversarial attack per held name plus the three "
            "cross-book passes — see <code>execution/run_red_team.py</code>.</p>"
        )
        return f"{_BRIEF_CSS}{toolbar}{empty}"

    per_name = [i for i in items if i.kind == "per_name"]
    cross = [i for i in items if i.kind == "cross_book"]

    parts: list[str] = [_BRIEF_CSS, toolbar, '<div class="rt-brief">']
    if per_name:
        parts.append('<h3 class="rt-group-title">Per-name</h3>')
        parts.extend(_render_item(i) for i in per_name)
    if cross:
        parts.append('<h3 class="rt-group-title">Cross-book</h3>')
        parts.extend(_render_item(i) for i in cross)
    parts.append("</div>")
    return "".join(parts)
