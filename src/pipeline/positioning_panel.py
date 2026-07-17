"""Portfolio → Positioning — the owner's target book, its history, the coach.

Three wells (command-center panel fragment, kit components only):

  1. **Active target** — the authoritative positioning vs the book's current
     readings, with gap chips. Reads the MATERIALIZED fit meta (Stage 0f's
     ``candidate_fit.json`` book/target blocks) so the GET path never touches
     the tracker; a stale/absent cache degrades to the stored intent alone.
  2. **Version history** — every saved intent, newest first (append-and-
     supersede: nothing is ever overwritten).
  3. **Coach** — the conversational surface (``positioning/coach_pack.py``
     via the unified ask engine, sessions scoped ``positioning``), with the
     propose→approve flow: "Propose targets" encodes the conversation into an
     editable form; only the owner's explicit approval persists, and the
     owner's edits — not the LLM's numbers — are what's validated and saved.

The manual path is the same form seeded from the active profile (no LLM).
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path
from typing import cast

from candidate_fit_cache import read_materialized_fit_meta
from positioning.encode import ProposedProfile
from positioning.profile import SLEEVE_KEYS, PositioningProfile, SectorTarget
from positioning.store import PositioningIntentRow, latest_intent, list_intents

_PANEL_STYLE = """<style>
.pos-grid { display:grid; grid-template-columns: 1fr 1fr; gap:16px; align-items:start; }
.pos-grid .pos-span { grid-column: 1 / -1; }
@media (max-width: 1100px) { .pos-grid { grid-template-columns: 1fr; } }
.pos-dim { display:flex; gap:10px; align-items:baseline; padding:4px 0;
  border-bottom:1px dashed var(--border); font-size:var(--fs-body); }
.pos-dim:last-child { border-bottom:none; }
.pos-dim .lbl { min-width:170px; color:var(--muted); }
.pos-dim .val { font-family:var(--mono); }
.pos-hist { font-size:var(--fs-body); }
.pos-hist td { padding:5px 10px 5px 0; vertical-align:top; }
.pos-chat-log { max-height:420px; overflow-y:auto; display:flex; flex-direction:column;
  gap:8px; padding:4px 0; }
.pos-msg { padding:8px 11px; border-radius:var(--radius); font-size:var(--fs-body);
  line-height:1.5; white-space:pre-wrap; }
.pos-msg.user { background:var(--surface); border:1px solid var(--border); align-self:flex-end;
  max-width:85%; }
.pos-msg.coach { background:var(--paper); border:1px solid var(--border); max-width:95%; }
.pos-chat-form { display:flex; gap:8px; margin-top:10px; }
.pos-chat-form textarea { flex:1; min-height:56px; resize:vertical; background:var(--paper);
  color:var(--fg); border:1px solid var(--border); border-radius:var(--radius);
  padding:8px 10px; font-size:var(--fs-body); font-family:inherit; }
.pos-form-grid { display:grid; grid-template-columns: 190px 1fr 1fr; gap:6px 10px;
  align-items:center; font-size:var(--fs-body); }
.pos-form-grid .hdr { color:var(--muted); font-size:var(--fs-caption); }
.pos-form-grid input, .pos-form-grid select, .pos-narrative {
  background:var(--paper); color:var(--fg); border:1px solid var(--border);
  border-radius:var(--radius); padding:5px 8px; font-size:var(--fs-body);
  font-family:var(--mono); width:100%; box-sizing:border-box; }
.pos-narrative { font-family:inherit; min-height:64px; width:100%; margin-top:6px; }
.pos-diff { font-size:var(--fs-caption); color:var(--muted); font-family:var(--mono); }
.pos-error { color:var(--bad); font-size:var(--fs-body); margin-top:8px; white-space:pre-wrap; }
.pos-actions { display:flex; gap:8px; margin-top:12px; align-items:center; }
</style>"""


# --------------------------------------------------------------------------- #
# Active-target card + history (pure reads)
# --------------------------------------------------------------------------- #


def _fmt_opt(v: object, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.2f}{suffix}" if suffix != "%" else f"{v * 100.0:.0f}%"
    return escape(str(v))


def _dim(label: str, value: str, chip: str = "") -> str:
    return (
        f'<div class="pos-dim"><span class="lbl">{escape(label)}</span>'
        f'<span class="val">{value}</span>{chip}</div>'
    )


def _gap_chip(gap_pp: float | None, band_pp: float) -> str:
    if gap_pp is None:
        return ""
    if abs(gap_pp) <= band_pp:
        return '<span class="k-chip k-chip-ok">on target</span>'
    arrow = "under" if gap_pp > 0 else "over"
    return f'<span class="k-chip k-chip-warn">{arrow} by {abs(gap_pp):.0f}pp</span>'


def _vol_target_chip(book_vol: float | None, target_vol_ann: float) -> str:
    """Book vol vs the active intent's ``target_vol_ann`` — absent when the
    book-vol figure isn't available (no new plumbing, no guess)."""
    if book_vol is None:
        return ""
    over = book_vol > target_vol_ann
    tone = "k-chip-warn" if over else "k-chip-ok"
    word = "over" if over else "under"
    return (
        f'<span class="k-chip {tone}">book vol {book_vol * 100.0:.0f}% vs target '
        f"{target_vol_ann * 100.0:.0f}% ({word})</span>"
    )


def _sharpe_floor_chip(book_sharpe: float | None, sharpe_floor: float) -> str:
    """Book Sharpe vs the active intent's ``sharpe_floor`` — absent when the
    book Sharpe figure isn't available."""
    if book_sharpe is None:
        return ""
    below = book_sharpe < sharpe_floor
    tone = "k-chip-warn" if below else "k-chip-ok"
    word = "below" if below else "above"
    return (
        f'<span class="k-chip {tone}">book Sharpe {book_sharpe:+.2f} vs floor '
        f"{sharpe_floor:+.2f} ({word})</span>"
    )


def render_active_target_card(db_path: Path, repo_root: Path) -> str:
    """The authoritative target vs current book readings. Materialized-cache
    reads only — safe on the GET path."""
    meta = read_materialized_fit_meta(repo_root)
    book = cast("dict[str, object]", meta.get("book") or {})

    intent: PositioningIntentRow | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            intent = latest_intent(conn)
        finally:
            conn.close()

    rows: list[str] = []
    if intent is None:
        badge = '<span class="k-chip">defaulting to current book</span>'
        rows.append(
            '<p class="muted">No positioning saved yet — the current book is the target '
            "(every gap reads zero). Talk to the coach below, or set targets manually.</p>"
        )
    else:
        badge = (
            f'<span class="k-pill k-pill-ok">intent v{intent.id}</span> '
            f'<span class="k-chip k-chip-mono">{escape(intent.created_at[:10])}</span> '
            f'<span class="k-chip">{escape(intent.source)}</span>'
        )
        p = intent.profile
        book_tilt = (
            book.get("growth_tilt") if isinstance(book.get("growth_tilt"), (int, float)) else None
        )
        if p.target_growth_tilt is not None:
            gap = (
                (p.target_growth_tilt - float(cast("float", book_tilt))) * 100.0
                if book_tilt is not None
                else None
            )
            rows.append(
                _dim(
                    "Growth tilt (QQQ-SPY β)",
                    f"target {p.target_growth_tilt:+.2f} · book {_fmt_opt(book_tilt)}",
                    _gap_chip(gap, p.growth_tilt_band * 100.0),
                )
            )
        for st in p.sector_targets:
            rows.append(
                _dim(
                    f"Sector · {st.sector}",
                    f"target {st.target_weight * 100.0:.0f}% (±{st.band * 100.0:.0f}pp)",
                )
            )
        for sleeve, frac in sorted(p.sleeves.items()):
            rows.append(_dim(f"Sleeve · {sleeve}", f"target {frac * 100.0:.0f}%"))
        if p.vol_posture:
            rows.append(_dim("Vol posture", escape(p.vol_posture)))
        book_vol = book.get("vol_ann") if isinstance(book.get("vol_ann"), (int, float)) else None
        if p.target_vol_ann is not None:
            rows.append(
                _dim(
                    "Target vol (ann.)",
                    f"{p.target_vol_ann * 100.0:.0f}%",
                    _vol_target_chip(cast("float | None", book_vol), p.target_vol_ann),
                )
            )
        book_sharpe = book.get("sharpe") if isinstance(book.get("sharpe"), (int, float)) else None
        if p.sharpe_floor is not None:
            rows.append(
                _dim(
                    "Sharpe floor",
                    f"{p.sharpe_floor:+.2f}",
                    _sharpe_floor_chip(cast("float | None", book_sharpe), p.sharpe_floor),
                )
            )
        if p.max_position_weight is not None:
            rows.append(_dim("Max position", f"{p.max_position_weight * 100.0:.0f}%"))
        if p.horizon_years is not None:
            rows.append(_dim("Horizon", f"{p.horizon_years:g}y"))
        if p.life_circumstances:
            rows.append(_dim("Life circumstances", escape("; ".join(p.life_circumstances))))
        rows.append(f'<p class="muted" style="margin-top:8px">“{escape(intent.narrative)}”</p>')
    degraded = cast("list[object]", book.get("degraded") or [])
    degraded_html = (
        '<p class="pos-diff">book context degraded: '
        + escape(" · ".join(str(d) for d in degraded))
        + "</p>"
        if degraded
        else ""
    )
    return (
        f'<div class="k-well" id="pos-active-card"><h3>Active target {badge}</h3>'
        + "".join(rows)
        + degraded_html
        + "</div>"
    )


def _history_well(db_path: Path) -> str:
    rows: list[PositioningIntentRow] = []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            rows = list_intents(conn, limit=12)
        finally:
            conn.close()
    if not rows:
        return '<div class="k-well"><h3>History</h3><p class="muted">No versions yet.</p></div>'
    body = "".join(
        "<tr>"
        f'<td><span class="k-chip k-chip-mono">v{r.id}</span></td>'
        f"<td>{escape(r.created_at[:16].replace('T', ' '))}</td>"
        f'<td><span class="k-chip">{escape(r.source)}</span></td>'
        f"<td>{escape(r.narrative.splitlines()[0][:120]) if r.narrative else ''}"
        + (
            f'<div class="pos-diff">superseded {escape(str(r.superseded_at)[:10])}</div>'
            if not r.is_latest and r.superseded_at
            else ""
        )
        + "</td></tr>"
        for r in rows
    )
    return (
        '<div class="k-well"><h3>History</h3>'
        f'<table class="pos-hist"><tbody>{body}</tbody></table></div>'
    )


# --------------------------------------------------------------------------- #
# Approval form (proposal → editable inputs; owner-wins)
# --------------------------------------------------------------------------- #

_EXTRA_SECTOR_ROWS = 2


def render_approval_form(proposal: ProposedProfile) -> str:
    """The encode proposal as an editable form. Every value is an input the
    owner can change before approving — what persists is re-validated from
    the FORM, never from the raw proposal."""
    p = proposal.profile
    diffs = (
        '<div class="pos-diff">changes vs active: '
        + escape("; ".join(f"{d.fieldname}: {d.active} → {d.proposed}" for d in proposal.diffs))
        + "</div>"
        if proposal.diffs
        else ""
    )
    sector_rows: list[str] = []
    targets: list[SectorTarget] = list(p.sector_targets)
    for i in range(len(targets) + _EXTRA_SECTOR_ROWS):
        st = targets[i] if i < len(targets) else None
        label_v = escape(st.sector) if st else ""
        weight_v = f"{st.target_weight * 100.0:g}" if st else ""
        band_v = f"{st.band * 100.0:g}" if st else "5"
        sector_rows.append(
            f'<input name="sector_{i}" placeholder="sector label" value="{label_v}">'
            f'<input name="sector_target_{i}" placeholder="weight %" value="{weight_v}">'
            f'<input name="sector_band_{i}" placeholder="band pp" value="{band_v}">'
        )
    sleeves_inputs = "".join(
        f'<label class="hdr">{key}</label>'
        f'<input name="sleeve_{key}" placeholder="%" '
        + (f'value="{p.sleeves[key] * 100.0:g}"' if key in p.sleeves else 'value=""')
        + '><span class="hdr">% of book</span>'
        for key in sorted(SLEEVE_KEYS)
    )

    def _num(name: str, value: float | None, placeholder: str) -> str:
        v = f'value="{value:g}"' if value is not None else 'value=""'
        return f'<input name="{name}" placeholder="{placeholder}" {v}>'

    posture_opts = "".join(
        f'<option value="{o}"{" selected" if p.vol_posture == o else ""}>{o or "(unset)"}</option>'
        for o in ("", "reduce", "hold", "increase")
    )
    life = escape("\n".join(p.life_circumstances))
    return f"""<div class="k-well" id="pos-approval-form">
<h3>Proposed targets — review, edit, approve</h3>
<p class="muted">{escape(proposal.rationale or "Encoded from the coach conversation.")}</p>
{diffs}
<form id="pos-approve-form">
<input type="hidden" name="session_id" value="{escape(proposal.session_id)}">
<div class="pos-form-grid">
  <span class="hdr">dimension</span><span class="hdr">value</span><span class="hdr"></span>
  <label>Growth tilt target</label>{_num("target_growth_tilt", p.target_growth_tilt, "e.g. -0.10")}
  <span class="hdr">QQQ-SPY β, -2..2 (blank = unexpressed)</span>
  <label>Tilt band</label>{_num("growth_tilt_band", p.growth_tilt_band, "0.15")}
  <span class="hdr">± tolerance</span>
  <label>Vol posture</label><select name="vol_posture">{posture_opts}</select><span class="hdr"></span>
  <label>Target vol (ann.) %</label>{_num("target_vol_ann", p.target_vol_ann * 100.0 if p.target_vol_ann is not None else None, "e.g. 14")}
  <span class="hdr"></span>
  <label>Sharpe floor</label>{_num("sharpe_floor", p.sharpe_floor, "")}<span class="hdr"></span>
  <label>Max position %</label>{_num("max_position_weight", p.max_position_weight * 100.0 if p.max_position_weight is not None else None, "e.g. 10")}
  <span class="hdr"></span>
  <label>Horizon (years)</label>{_num("horizon_years", p.horizon_years, "")}<span class="hdr"></span>
</div>
<h4>Sector targets <span class="pos-diff">(weight/band in %, tracker labels verbatim; blank rows ignored)</span></h4>
<div class="pos-form-grid">{"".join(sector_rows)}</div>
<h4>Sleeves</h4>
<div class="pos-form-grid">{sleeves_inputs}</div>
<h4>Life circumstances <span class="pos-diff">(one per line, your words)</span></h4>
<textarea class="pos-narrative" name="life_circumstances">{life}</textarea>
<h4>Narrative <span class="pos-diff">(persisted with the profile)</span></h4>
<textarea class="pos-narrative" name="narrative">{escape(proposal.summary)}</textarea>
<div class="pos-actions">
  <button type="submit" class="k-btn k-btn-primary">Approve &amp; save</button>
  <button type="button" class="k-btn k-btn-quiet" id="pos-approve-cancel">Discard</button>
</div>
<div class="pos-error" id="pos-approve-error" hidden></div>
</form></div>"""


class FormError(ValueError):
    """A form value failed parsing/validation — message is owner-facing."""


def _form_float(form: dict[str, str], name: str, *, pct: bool = False) -> float | None:
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError as exc:
        raise FormError(f"{name}: {raw!r} is not a number") from exc
    return v / 100.0 if pct else v


def profile_from_form(form: dict[str, str]) -> tuple[PositioningProfile, str]:
    """Re-validate a PositioningProfile from the SUBMITTED form values (the
    owner-wins seam: what persists is what the owner sees in the inputs).
    Raises :class:`FormError` with an owner-facing message on bad values."""
    sector_targets: list[dict[str, object]] = []
    i = 0
    while f"sector_{i}" in form:
        label = (form.get(f"sector_{i}") or "").strip()
        weight = _form_float(form, f"sector_target_{i}", pct=True)
        band = _form_float(form, f"sector_band_{i}", pct=True)
        if label and weight is not None:
            sector_targets.append(
                {
                    "sector": label,
                    "target_weight": weight,
                    "band": band if band is not None else 0.05,
                }
            )
        i += 1
    sleeves = {
        key: v
        for key in SLEEVE_KEYS
        if (v := _form_float(form, f"sleeve_{key}", pct=True)) is not None
    }
    life = [ln.strip() for ln in (form.get("life_circumstances") or "").splitlines() if ln.strip()]
    payload: dict[str, object] = {
        "target_growth_tilt": _form_float(form, "target_growth_tilt"),
        "growth_tilt_band": _form_float(form, "growth_tilt_band") or 0.15,
        "vol_posture": (form.get("vol_posture") or "").strip() or None,
        "target_vol_ann": _form_float(form, "target_vol_ann", pct=True),
        "sharpe_floor": _form_float(form, "sharpe_floor"),
        "sector_targets": sector_targets,
        "sleeves": sleeves,
        "max_position_weight": _form_float(form, "max_position_weight", pct=True),
        "horizon_years": _form_float(form, "horizon_years"),
        "life_circumstances": life,
    }
    try:
        profile = PositioningProfile.model_validate(payload)
    except ValueError as exc:
        raise FormError(str(exc)) from exc
    narrative = (form.get("narrative") or "").strip()
    if not narrative:
        raise FormError(
            "narrative is required — say what this positioning is, in a sentence or two"
        )
    if profile.is_empty():
        raise FormError("no quantitative target set — fill in at least one dimension")
    return profile, narrative


# --------------------------------------------------------------------------- #
# Panel assembly
# --------------------------------------------------------------------------- #

_COACH_JS = """<script>
(function () {
  var log = document.getElementById('pos-chat-log');
  var form = document.getElementById('pos-chat-form');
  var input = document.getElementById('pos-chat-input');
  var propose = document.getElementById('pos-propose');
  var approvalHost = document.getElementById('pos-approval');
  var sessionId = '';
  function msg(role, text) {
    var el = document.createElement('div');
    el.className = 'pos-msg ' + role;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var q = input.value.trim();
    if (!q) return;
    input.value = '';
    msg('user', q);
    var pending = msg('coach', 'thinking…');
    fetch('/api/positioning/coach', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, session_id: sessionId })
    }).then(function (r) { return r.json(); }).then(function (data) {
      sessionId = data.session_id || sessionId;
      pending.textContent = data.text || data.message || '(no answer)';
    }).catch(function (err) { pending.textContent = 'error: ' + err; });
  });
  propose.addEventListener('click', function () {
    if (!sessionId) { msg('coach', 'Talk to the coach first — there is no conversation to encode yet.'); return; }
    propose.disabled = true; propose.textContent = 'Encoding…';
    fetch('/api/positioning/propose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    }).then(function (r) {
      return r.text().then(function (t) { return { ok: r.ok, text: t }; });
    }).then(function (res) {
      propose.disabled = false; propose.textContent = 'Propose targets from this conversation';
      if (!res.ok) { msg('coach', 'encode failed: ' + res.text); return; }
      approvalHost.innerHTML = res.text;
      wireApproval();
    });
  });
  function wireApproval() {
    var f = document.getElementById('pos-approve-form');
    if (!f) return;
    var errEl = document.getElementById('pos-approve-error');
    var cancel = document.getElementById('pos-approve-cancel');
    if (cancel) cancel.addEventListener('click', function () { approvalHost.innerHTML = ''; });
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      fetch('/api/positioning/approve', { method: 'POST', body: new FormData(f) })
        .then(function (r) { return r.text().then(function (t) { return { ok: r.ok, text: t }; }); })
        .then(function (res) {
          if (!res.ok) { errEl.hidden = false; errEl.textContent = res.text; return; }
          document.getElementById('pos-active-card').outerHTML = res.text;
          approvalHost.innerHTML = '';
          msg('coach', 'Saved. The new positioning is now the active target — fit chips re-score against it on the next morning run (or refresh_candidate_fit).');
        });
    });
  }
})();
</script>"""


def render_positioning_panel(db_path: Path, repo_root: Path) -> str:
    """The Positioning tab fragment."""
    return "".join(
        [
            _PANEL_STYLE,
            '<section class="panel"><h2>Positioning</h2>',
            '<p class="sub">Your durable portfolio positioning — the target book the '
            "evaluation-list fit math scores against. The coach pushes back and grounds the "
            "conversation in the live book; nothing persists until you approve the encoded "
            "targets (your edits win). The prior saved state stays authoritative until you "
            "express new views.</p>",
            '<div class="pos-grid">',
            render_active_target_card(db_path, repo_root),
            _history_well(db_path),
            # "Positioning coach" (wave B B6) — disambiguates from Record's
            # Calibration coach; a bare "Coach" read as the same product twice.
            '<div class="k-well pos-span"><h3>Positioning coach</h3>',
            '<div class="pos-chat-log" id="pos-chat-log"></div>',
            '<form class="pos-chat-form" id="pos-chat-form">',
            '<textarea id="pos-chat-input" placeholder="e.g. I want more international ',
            "small-cap value exposure and less mega-cap growth — kid on the way, thinking "
            'about lowering volatility…"></textarea>',
            '<button type="submit" class="k-btn k-btn-primary">Send</button></form>',
            '<div class="pos-actions">',
            '<button type="button" class="k-btn k-btn-quiet" id="pos-propose">',
            "Propose targets from this conversation</button>",
            "</div>",
            '<div id="pos-approval"></div>',
            "</div>",
            "</div></section>",
            _COACH_JS,
        ]
    )
