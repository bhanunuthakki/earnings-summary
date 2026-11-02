"""Manual portfolio targets and deterministic active-target readings.

The owner edits the current profile and explicitly saves through
``/api/positioning/approve``. No conversation or model proposal is required.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path
from typing import cast

from candidate_fit_cache import read_materialized_fit_meta
from pipeline.portfolio_styles import positioning_css
from positioning.profile import SLEEVE_KEYS, PositioningProfile, SectorTarget
from positioning.store import PositioningIntentRow, latest_intent
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.panel import panel_section

_PANEL_STYLE = positioning_css()


# --------------------------------------------------------------------------- #
# Active-target card (pure reads)
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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
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
            "(every gap reads zero). Set targets with the form below.</p>"
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
        rows.append(f'<p class="muted pos-intent-quote">“{escape(intent.narrative)}”</p>')
    # D4 (surface_density_jit_redesign.md): a degraded book context announces
    # itself in the owner's vocabulary — a warn pill + what it means for THIS
    # card — with the raw engineering reasons one click away in a details
    # peek. The old rendering dumped the diagnostic strings verbatim
    # ("tracker offline and no risk snapshot — book Sharpe unknown · …") into
    # the card body in monospace. Still visibly distinct from the healthy
    # state (silent-degradation rule) — just no longer written for a debugger.
    degraded = cast("list[object]", book.get("degraded") or [])
    degraded_html = (
        (
            '<div class="pos-degraded">'
            '<span class="k-pill k-pill-warn">context degraded</span> '
            f"<span>{len(degraded)} book-context leg(s) unscored — targets still "
            "render; the affected gap chips are missing rather than guessed.</span>"
            "<details><summary>details</summary><ul>"
            + "".join(f"<li>{escape(str(d))}</li>" for d in degraded)
            + "</ul></details></div>"
        )
        if degraded
        else ""
    )
    return (
        f'<div class="k-well" id="pos-active-card"><h3 class="k-well-title">Active target {badge}</h3>'
        + "".join(rows)
        + degraded_html
        + "</div>"
    )


# --------------------------------------------------------------------------- #
# Manual approval form (owner-entered inputs)
# --------------------------------------------------------------------------- #

_EXTRA_SECTOR_ROWS = 2


def render_approval_form(profile: PositioningProfile, narrative: str = "") -> str:
    """Render manual inputs. Submitted values are revalidated before saving."""
    p = profile
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
<h3 class="k-well-title">Set targets</h3>
<form id="pos-approve-form">
<input type="hidden" name="session_id" value="">
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
<h4 class="k-well-title">Sector targets <span class="pos-diff">(weight/band in %, tracker labels verbatim; blank rows ignored)</span></h4>
<div class="pos-form-grid">{"".join(sector_rows)}</div>
<h4 class="k-well-title">Sleeves</h4>
<div class="pos-form-grid">{sleeves_inputs}</div>
<h4 class="k-well-title">Life circumstances <span class="pos-diff">(one per line, your words)</span></h4>
<textarea class="pos-narrative" name="life_circumstances">{life}</textarea>
<h4 class="k-well-title">Narrative <span class="pos-diff">(persisted with the profile)</span></h4>
<textarea class="pos-narrative" name="narrative">{escape(narrative)}</textarea>
<div class="pos-actions">
  <button type="submit" class="k-btn k-btn-primary">Save targets</button>
  <button type="reset" class="k-btn k-btn-quiet">Reset</button>
</div>
<div class="pos-error" id="pos-approve-error" role="alert" hidden></div>
<p class="muted" id="pos-save-status" role="status" hidden></p>
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

_APPROVAL_JS = """<script>
(function () {
  var form = document.getElementById('pos-approve-form');
  if (!form) return;
  var error = document.getElementById('pos-approve-error');
  var status = document.getElementById('pos-save-status');
  var submit = form.querySelector('button[type="submit"]');
  form.addEventListener('reset', function () {
    error.hidden = true;
    status.hidden = true;
  });
  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    if (submit.disabled) return;
    error.hidden = true;
    status.hidden = false;
    status.textContent = 'Saving targets…';
    CCAction.busy(submit);
    fetch('/api/positioning/approve', { method: 'POST', body: new FormData(form) })
      .then(function (r) { return r.text().then(function (t) { return { ok: r.ok, text: t }; }); })
      .then(function (res) {
        if (!res.ok) {
          status.hidden = true;
          error.hidden = false;
          error.textContent = res.text;
          return;
        }
        document.getElementById('pos-active-card').outerHTML = res.text;
        status.textContent = 'Targets saved. Portfolio comparisons update on the next refresh.';
      })
      .catch(function () {
        status.hidden = true;
        error.hidden = false;
        error.textContent = 'Could not save targets. Your entries remain in the form; try again.';
      })
      .finally(function () { CCAction.release(submit); });
  });
})();
</script>"""


def render_positioning_panel(db_path: Path, repo_root: Path) -> str:
    """Show current targets and an explicit manual editor without model calls."""
    intent: PositioningIntentRow | None = None
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            intent = latest_intent(conn)
        finally:
            conn.close()
    profile = intent.profile if intent is not None else PositioningProfile()
    narrative = intent.narrative if intent is not None else ""
    return panel_section(
        '<p class="sub">Record portfolio targets. Changes take effect when you save.</p>',
        '<div class="pos-grid">',
        '<details class="pos-span"><summary>Active target detail</summary>',
        render_active_target_card(db_path, repo_root),
        "</details>",
        '<div class="pos-span">',
        render_approval_form(profile, narrative),
        "</div>",
        "</div>",
        title="Positioning",
        style=_PANEL_STYLE,
        tail=_APPROVAL_JS,
    )
