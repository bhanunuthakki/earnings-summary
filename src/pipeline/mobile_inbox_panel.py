"""GET /mobile/inbox — the compact private mobile review surface (PRD §9.2,
§11.6). NOT a second application: it composes the same reads/action-cores the
desktop Ledger and Telegram already use, laid out for a phone screen behind
the existing Tailscale/private-host posture (comments_server's global CORS +
security-header + CSRF-origin hooks apply to this route unchanged — no new
auth surface).

Sections, each with an explicit loading/empty/stale/failed/last-valid state
(design_language §12.2 — "not generated," "failed," and "nothing pending"
must never collapse into the same blank card):

  1. pending Decision Drafts — Confirm / Correct / Dismiss / Defer, POSTing
     the SAME action core as Telegram (``capture.decision_draft_actions``);
  2. unresolved Investment Decision Card dispositions — evaluation-list names
     with a CURRENT card and no pass/watch/promote decision on record yet;
  3. the latest Senior Partner Brief (P2.2, PRD §9.1) — the five ordered
     sections, read from the SAME ``llm_artifacts`` row (scope='portfolio',
     purpose='senior_partner_brief') the Today doorway
     (``pipeline.senior_partner_brief_panel``) and the Telegram builder
     (``advisor.senior_partner_brief.build_telegram_text``) read — one
     artifact, three surfaces, no drift;
  4. a compact ``dashboard.inbox`` stream (the same read the Home rail uses,
     ``compact=True``).

Confirm/Correct/Dismiss POST to ``/api/decision-drafts/<id>/confirm|correct|
dismiss`` (comments_server, thin handlers over ``decision_draft_actions``).
Defer is a client-side-only "not now" (the draft stays pending; nothing is
written — mirrors the weekly packet's own Defer semantics) so a slow network
never strands the owner on a card they only meant to skip past this session.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path
from typing import cast

from ui.controls import controls_css, ticker_label
from ui.tokens import palette_css

_STYLE = """<style>
:root { color-scheme: light; }
body { margin:0; padding:var(--sp-3); background:var(--paper); font-family:var(--sans);
  max-width:560px; margin-left:auto; margin-right:auto; }
.mi-h1 { font-size:var(--fs-title); font-weight:600; color:var(--fg); margin:0 0 var(--sp-3); }
.mi-sec { margin-bottom:var(--sp-5); }
.mi-sec-h { font-size:var(--fs-caption); font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:0.06em; margin:0 0 var(--sp-2); }
.mi-card { border:1px solid var(--border); border-radius:var(--radius); background:var(--surface);
  padding:var(--sp-3); margin-bottom:var(--sp-2); }
.mi-card-head { display:flex; align-items:baseline; gap:var(--sp-2); flex-wrap:wrap;
  margin-bottom:var(--sp-1); }
.mi-body { color:var(--fg-soft); font-size:var(--fs-body); line-height:1.5; margin:var(--sp-1) 0; }
.mi-actions { display:flex; gap:var(--sp-2); flex-wrap:wrap; margin-top:var(--sp-2); }
.mi-empty, .mi-failed, .mi-not-generated { color:var(--muted); font-size:var(--fs-body);
  padding:var(--sp-3) 0; }
.mi-failed { color:var(--bad); }
.mi-recover { display:block; margin-top:var(--sp-1); font-size:var(--fs-caption); }
</style>"""

_HEAD = (
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">'
    "<title>Mobile Inbox</title>"
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _open(db_path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _draft_card(row: sqlite3.Row) -> str:
    ticker = ""
    action = ""
    if row["draft_json"]:
        import json

        try:
            content_raw: object = json.loads(row["draft_json"])
        except (ValueError, TypeError):
            content_raw = {}
        if isinstance(content_raw, dict):
            content = cast("dict[str, object]", content_raw)
            ticker = str(content.get("proposed_ticker") or "")
            action = str(content.get("proposed_action") or content.get("intent") or "")
    ticker_html = ticker_label(ticker) if ticker else '<span class="k-chip">PORTFOLIO</span>'
    action_chip = f'<span class="k-chip k-chip-mono">{escape(action)}</span>' if action else ""
    return (
        f'<div class="mi-card" data-draft-id="{int(row["id"])}">'
        f'<div class="mi-card-head">{ticker_html}{action_chip}'
        f'<span class="k-chip">{escape(str(row["source_channel"]))}</span></div>'
        f'<div class="mi-body">{escape(str(row["original_text"])[:280])}</div>'
        '<div class="mi-actions">'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" data-mi-act="confirm">Confirm</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-mi-act="correct">Correct</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-mi-act="dismiss">Dismiss</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-mi-act="defer">Defer</button>'
        "</div></div>"
    )


def _drafts_section(db_path: Path) -> str:
    conn = _open(db_path)
    if conn is None:
        return '<div class="mi-failed">Decision drafts unavailable — database unreachable.</div>'
    try:
        if not _table_exists(conn, "decision_drafts"):
            return (
                '<div class="mi-failed">Decision drafts unavailable.'
                '<span class="mi-recover">Run <code>alembic upgrade head</code> to migrate.</span>'
                "</div>"
            )
        rows = conn.execute(
            "SELECT * FROM decision_drafts WHERE status = 'awaiting_confirmation' "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    except sqlite3.Error:
        return '<div class="mi-failed">Decision drafts unavailable.</div>'
    finally:
        conn.close()
    if not rows:
        return '<div class="mi-empty">Nothing pending — captures land here for confirmation.</div>'
    return "".join(_draft_card(r) for r in rows)


def _card_disposition_card(row: sqlite3.Row) -> str:
    ticker_html = ticker_label(str(row["ticker"]))
    return (
        f'<div class="mi-card">'
        f'<div class="mi-card-head">{ticker_html}'
        '<span class="k-chip">investment decision card</span></div>'
        '<div class="mi-body">Current card has no Pass/Watch/Promote disposition yet.</div>'
        '<div class="mi-actions">'
        f'<a class="k-btn k-btn-quiet k-btn-sm" href="/ticker/{escape(str(row["ticker"]))}">'
        "Open full app</a>"
        "</div></div>"
    )


def _card_dispositions_section(db_path: Path) -> str:
    conn = _open(db_path)
    if conn is None:
        return '<div class="mi-failed">Card dispositions unavailable — database unreachable.</div>'
    try:
        if not (_table_exists(conn, "tracked_companies") and _table_exists(conn, "llm_artifacts")):
            return '<div class="mi-failed">Card dispositions unavailable.</div>'
        rows = conn.execute(
            """
            SELECT tc.ticker AS ticker, la.id AS artifact_id
            FROM tracked_companies tc
            JOIN llm_artifacts la
              ON la.purpose = 'investment_decision_card'
             AND UPPER(la.ticker) = UPPER(tc.ticker)
             AND la.superseded_by_id IS NULL
            WHERE tc.list_type = 'evaluation'
              AND NOT EXISTS (
                SELECT 1 FROM decisions d
                WHERE d.advice_artifact_id = la.id
                  AND d.recommendation_kind IN ('pass', 'watch', 'promote')
              )
            ORDER BY la.generated_at DESC LIMIT 10
            """
        ).fetchall()
    except sqlite3.Error:
        return '<div class="mi-failed">Card dispositions unavailable.</div>'
    finally:
        conn.close()
    if not rows:
        return '<div class="mi-empty">No evaluation names awaiting a disposition.</div>'
    return "".join(_card_disposition_card(r) for r in rows)


_COACH_PING_REF_RX = "coach_ping:"


def _dismiss_ping_ids(item: dict[str, object]) -> list[int]:
    """The governor-routed moment(s) backing this brief item, if any —
    parsed from ``source_refs`` entries shaped ``coach_ping:<id>`` (the ref
    format ``advisor.senior_partner_brief._gather_routed_moments`` cites).
    Powers the per-item Dismiss button (P2.2 mute-learning fix): dismissing
    HERE calls the same action core (``dismiss_routed_moment`` ->
    ``research.governor.record_dismissal``) the Telegram
    ``spb:dismiss_item:<id>`` callback calls, so 3 consecutive dismissals of
    a class mute it regardless of which surface the owner used."""
    refs = item.get("source_refs")
    if not isinstance(refs, list):
        return []
    ids: list[int] = []
    for ref in cast("list[object]", refs):
        text = str(ref)
        if text.startswith(_COACH_PING_REF_RX):
            suffix = text[len(_COACH_PING_REF_RX) :]
            if suffix.isdigit():
                ids.append(int(suffix))
    return ids


def _brief_item_card(label: str, item_raw: object) -> str:
    if not isinstance(item_raw, dict):
        return ""
    item = cast("dict[str, object]", item_raw)
    title = str(item.get("title") or "")
    if not title:
        return ""
    body = str(item.get("body") or "")
    disposition = str(item.get("disposition") or "")
    effort = item.get("effort_estimate")
    ticker = item.get("ticker")
    ticker_html = ticker_label(str(ticker)) if ticker else ""
    effort_chip = f'<span class="k-chip">{escape(str(effort))}</span>' if effort else ""
    dismiss_buttons = "".join(
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        f'data-mi-dismiss-ping="{pid}">Dismiss</button>'
        for pid in _dismiss_ping_ids(item)
    )
    actions = f'<div class="mi-actions">{dismiss_buttons}</div>' if dismiss_buttons else ""
    return (
        '<div class="mi-card">'
        f'<div class="mi-card-head"><span class="k-chip k-chip-mono">{escape(label)}</span>'
        f'{ticker_html}<span class="k-chip">{escape(disposition)}</span>{effort_chip}</div>'
        f'<div class="mi-body"><strong>{escape(title)}</strong><br>{escape(body[:400])}</div>'
        f"{actions}</div>"
    )


def _senior_partner_brief_section(db_path: Path) -> str:
    """P2.2 (src/advisor/senior_partner_brief.py, PRD §9.1): the five ordered
    sections from the latest ``senior_partner_brief`` artifact. §12.2 states
    kept distinct: 'not generated yet' (no artifact at all) vs. 'failed to
    read back' (artifact exists but content_json isn't the expected shape)
    vs. the populated card set — never collapsed into the same blank card."""
    try:
        import llm_artifact_store

        artifact = llm_artifact_store.read_current(
            ticker=None, purpose="senior_partner_brief", scope="portfolio", db_path=db_path
        )
    except Exception:
        return (
            '<div class="mi-failed">Senior Partner Brief unavailable — database unreachable.</div>'
        )
    if artifact is None:
        return '<div class="mi-not-generated">Senior Partner Brief not generated yet.</div>'
    if not isinstance(artifact.content_json, dict):
        return '<div class="mi-failed">Senior Partner Brief failed to read back.</div>'

    content = cast("dict[str, object]", artifact.content_json)
    mode = str(content.get("selection_mode") or "llm")
    mode_note = (
        '<div class="mi-body">Mechanical digest — no LLM synthesis applied this week.</div>'
        if mode == "deterministic_fallback"
        else ""
    )
    cards = [mode_note]
    what_changed = content.get("what_changed")
    if isinstance(what_changed, list):
        for raw in cast("list[object]", what_changed)[:5]:
            cards.append(_brief_item_card("What changed", raw))
    cards.append(_brief_item_card("Highest priority", content.get("highest_priority_decision")))
    cards.append(_brief_item_card("Capital use", content.get("capital_use")))
    cards.append(_brief_item_card("Worth challenging", content.get("assumption_challenge")))
    cards.append(_brief_item_card("Worth revisiting", content.get("decision_revisit")))
    body = "".join(c for c in cards if c)
    if not body:
        return '<div class="mi-empty">This week\'s brief has nothing to surface.</div>'
    return body


def _inbox_stream_section(db_path: Path) -> str:
    try:
        from dashboard.inbox import collect_inbox, render_inbox_stream

        items = collect_inbox(db_path, limit=12)
        return render_inbox_stream(items, db_path=db_path, compact=True, surface="mobile")
    except Exception:
        return '<div class="mi-failed">Inbox stream unavailable.</div>'


_JS = """
<script>
(function () {
  document.body.addEventListener('click', function (ev) {
    var dismissBtn = ev.target.closest('[data-mi-dismiss-ping]');
    if (dismissBtn) {
      var pingId = dismissBtn.getAttribute('data-mi-dismiss-ping');
      dismissBtn.disabled = true;
      dismissBtn.textContent = '...';
      fetch('/api/senior-partner-brief/dismiss-item/' + pingId, { method: 'POST' })
        .then(function (r) { return r.json().then(function (body) { return { r: r, body: body }; }); })
        .then(function (res) {
          var card = dismissBtn.closest('.mi-card');
          if (res.r.ok) {
            if (card) { card.style.opacity = '0.4'; }
            dismissBtn.textContent = res.body.muted_class
              ? ('Muted ' + res.body.muted_class)
              : 'Dismissed';
          } else {
            dismissBtn.disabled = false;
            dismissBtn.textContent = 'Dismiss';
          }
        })
        .catch(function () { dismissBtn.disabled = false; dismissBtn.textContent = 'Dismiss'; });
      return;
    }
    var btn = ev.target.closest('[data-mi-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-mi-act');
    var card = btn.closest('[data-draft-id]');
    if (!card) return;
    var id = card.getAttribute('data-draft-id');
    if (act === 'defer') { card.style.opacity = '0.4'; btn.disabled = true; return; }
    if (act === 'correct') {
      window.location.href = '/#ledger-console?draft=' + id;
      return;
    }
    btn.disabled = true;
    btn.textContent = '...';
    fetch('/api/decision-drafts/' + id + '/' + act, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function (r) {
      if (r.ok) { card.remove(); } else { btn.disabled = false; }
    }).catch(function () { btn.disabled = false; });
  });
})();
</script>
"""


def render_mobile_inbox(db_path: Path) -> str:
    """The full standalone mobile page. Composes the four sections above; a
    single section's read failure never blanks the rest (each is isolated)."""
    style = f"<style>{palette_css('paper')}</style><style>{controls_css('paper')}</style>{_STYLE}"
    body = (
        '<h1 class="mi-h1">Inbox</h1>'
        '<section class="mi-sec"><h2 class="mi-sec-h">Decision drafts</h2>'
        f"{_drafts_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Card dispositions</h2>'
        f"{_card_dispositions_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Senior Partner Brief</h2>'
        f"{_senior_partner_brief_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Recent activity</h2>'
        f"{_inbox_stream_section(db_path)}</section>"
    )
    return f"<!doctype html><html><head>{_HEAD}{style}</head><body>{body}{_JS}</body></html>"


__all__ = ["render_mobile_inbox"]
