"""GET /mobile/inbox — the compact private mobile review surface (PRD §9.2,
§11.6). NOT a second application: it composes the same reads/action-cores the
desktop Ledger and Telegram already use, laid out for a phone screen behind
the existing Tailscale/private-host posture (comments_server's global CORS +
security-header + CSRF-origin hooks apply to this route unchanged — no new
auth surface).

Sections, each with an explicit loading/empty/stale/failed/last-valid state
(docs/design/personal_investment_partner_prd.md §12.2 — "not generated," "failed," and
"nothing pending"
must never collapse into the same blank card):

  1. current Incremental Dollar Recommendation — the same artifact id and
     preferred plan rendered by Portfolio → Allocation and loaded into Ask;
  2. pending Decision Drafts — Confirm / Correct / Dismiss / Defer, POSTing
     the SAME action core as Telegram (``capture.decision_draft_actions``);
  3. unresolved Investment Decision Card dispositions — evaluation-list names
     with a CURRENT card and no pass/watch/promote decision on record yet;
  4. the latest Senior Partner Brief (P2.2, PRD §9.1) — the five ordered
     sections, read from the SAME ``llm_artifacts`` row (scope='portfolio',
     purpose='senior_partner_brief') the Today doorway
     (``pipeline.senior_partner_brief_panel``) and the Telegram builder
     (``advisor.senior_partner_brief.build_telegram_text``) read — one
     artifact, three surfaces, no drift;
  5. a compact ``dashboard.inbox`` stream (the same read the Home rail uses,
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

from capture.decision_draft import ACTION_VOCAB as _RECEIPT_ACTION_VOCAB
from pipeline.calibration_receipt import render_calibration_receipt_for
from pipeline.cc_action import CC_ACTION_CSS, CC_ACTION_JS
from pipeline.operations_styles import MOBILE_INBOX_STYLE as _STYLE
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from ui.controls import controls_css, controls_js, ticker_label
from ui.tokens import palette_css

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
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _draft_card(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    ticker = str(row["ticker"] or "")
    action = str(row["action"] or "")
    ticker_html = ticker_label(ticker) if ticker else '<span class="k-chip">PORTFOLIO</span>'
    action_chip = f'<span class="k-chip k-chip-mono">{escape(action)}</span>' if action else ""
    # Calibration receipt (owner-ratified design review, 2026-08-02): "when
    # you've been here before" on the SAME verb the draft carries. Only a
    # closed decision-verb (never a bare 'musing'/'correction' intent, which
    # `action` falls back to when the parser found no proposed_action)
    # produces a meaningful cohort read. Best-effort — never raises.
    receipt_html = ""
    if action.lower() in _RECEIPT_ACTION_VOCAB:
        try:
            receipt_html = render_calibration_receipt_for(
                conn, action=action, ticker=ticker or None
            )
        except sqlite3.Error:
            receipt_html = ""
    fill_count = int(row["fill_count"])
    amount_raw = row["amount_usd"]
    amount_note = f" · ${float(amount_raw):,.0f} total" if amount_raw is not None else ""
    group_note = (
        f'<span class="k-chip k-chip-mono">{fill_count} split fills</span>'
        if fill_count > 1
        else ""
    )
    is_tracker_group = str(row["source_channel"]) == "tracker" and row["source_external_id"]
    ticker_value = escape(ticker, quote=True)
    action_value = action.lower()
    action_choices = (
        ("buy", "sell")
        if is_tracker_group
        else (
            "buy",
            "sell",
            "add",
            "trim",
            "hold",
            "pass",
            "watch",
            "promote",
        )
    )
    action_options = "".join(
        f'<option value="{choice}"{" selected" if choice == action_value else ""}>'
        f"{choice.title()}</option>"
        for choice in action_choices
    )
    amount_value = "" if amount_raw is None else escape(f"{float(amount_raw):g}", quote=True)
    rationale_value = escape(str(row["rationale"] or ""))
    data_attr = (
        f'data-draft-group-id="{int(row["id"])}"'
        if is_tracker_group
        else f'data-draft-id="{int(row["id"])}"'
    )
    confirm_label = "Confirm trade" if is_tracker_group else "Confirm"
    dismiss_label = "Dismiss trade" if is_tracker_group else "Dismiss"
    return (
        f'<div class="mi-card" {data_attr}>'
        f'<div class="mi-card-head">{ticker_html}{action_chip}'
        f'<span class="k-chip">{escape(str(row["source_channel"]))}</span>{group_note}</div>'
        f'<div class="mi-body">{escape(str(row["original_text"])[:280])}'
        f"{escape(amount_note)}</div>"
        f"{receipt_html}"
        '<div class="mi-actions">'
        f'<button type="button" class="k-btn k-btn-primary k-btn-sm" '
        f'data-mi-act="confirm">{confirm_label}</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-mi-act="correct">Correct</button>'
        f'<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        f'data-mi-act="dismiss">{dismiss_label}</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" data-mi-act="defer">Defer</button>'
        "</div>"
        '<form class="mi-correct-form" data-mi-correct-form hidden>'
        f'<label>Ticker<input type="text" name="proposed_ticker" value="{ticker_value}" '
        'autocomplete="off" required></label>'
        f'<label>Action<select name="proposed_action">{action_options}</select></label>'
        f'<label>Total USD<input type="number" name="proposed_amount_usd" value="{amount_value}" '
        'step="0.01" inputmode="decimal"></label>'
        f'<label class="mi-wide">Rationale<textarea name="proposed_rationale" rows="3">'
        f"{rationale_value}</textarea></label>"
        '<div class="mi-actions mi-wide">'
        '<button type="submit" class="k-btn k-btn-primary k-btn-sm">Save correction</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        "data-mi-cancel-correction>Cancel</button>"
        "</div>"
        '<div class="mi-failed mi-wide" data-mi-correct-error hidden></div>'
        "</form>"
        "</div>"
    )


def _allocation_recommendation_section(db_path: Path) -> str:
    """Render the current allocation artifact without re-deriving its plan."""
    try:
        import llm_artifact_store

        artifact = llm_artifact_store.read_current(
            ticker=None,
            purpose="incremental_dollar_recommendation",
            scope="portfolio",
            db_path=db_path,
        )
    except Exception:
        return '<div class="mi-failed">Allocation recommendation unavailable.</div>'
    if artifact is None:
        return '<div class="mi-not-generated">Allocation recommendation not generated yet.</div>'
    if not isinstance(artifact.content_json, dict):
        return '<div class="mi-failed">Allocation recommendation failed to read back.</div>'
    content = cast("dict[str, object]", artifact.content_json)
    plan_raw = content.get("preferred_plan")
    if not isinstance(plan_raw, dict):
        return '<div class="mi-failed">Allocation recommendation has no preferred plan.</div>'
    plan = cast("dict[str, object]", plan_raw)
    name = str(plan.get("name") or "Preferred plan")
    allocations_raw = plan.get("allocations")
    allocations = cast("list[object]", allocations_raw) if isinstance(allocations_raw, list) else []
    legs: list[str] = []
    for raw in allocations:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        ticker = str(item.get("ticker") or "").upper()
        pct = item.get("pct_of_cash")
        if ticker and isinstance(pct, (int, float)):
            legs.append(f"{ticker} {float(pct):.0f}%")
    plan_line = " · ".join(legs) if legs else "Retain new cash."
    return (
        f'<div class="mi-card" data-artifact-id="{artifact.id}">'
        '<div class="mi-card-head"><span class="k-chip">current allocation</span>'
        f'<span class="k-chip k-chip-mono">artifact #{artifact.id}</span></div>'
        f'<div class="mi-body"><strong>{escape(name)}</strong><br>{escape(plan_line)}</div>'
        '<div class="mi-actions"><a class="k-btn k-btn-quiet k-btn-sm" '
        'href="/#portfolio_allocation">Open Allocation</a></div></div>'
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
        counts = conn.execute(
            """
            SELECT COUNT(*) AS fill_count,
                   COUNT(DISTINCT CASE
                       WHEN source_channel = 'tracker' AND source_external_id IS NOT NULL
                       THEN 'tracker:' || source_external_id
                       ELSE 'draft:' || CAST(id AS TEXT)
                   END) AS group_count
            FROM decision_drafts
            WHERE status = 'awaiting_confirmation'
            """
        ).fetchone()
        if _table_exists(conn, "decisions"):
            rows = conn.execute(
                """
                WITH pending AS (
                    SELECT *,
                           CASE
                               WHEN source_channel = 'tracker'
                                    AND source_external_id IS NOT NULL
                               THEN 'tracker:' || source_external_id
                               ELSE 'draft:' || CAST(id AS TEXT)
                           END AS group_key
                    FROM decision_drafts
                    WHERE status = 'awaiting_confirmation'
                ),
                confirmed_totals AS (
                    SELECT dd.source_external_id,
                           MAX(d.size_usd) AS current_size_usd
                    FROM decision_drafts dd
                    JOIN decisions d ON d.id = dd.decision_id
                    WHERE dd.source_channel = 'tracker'
                      AND dd.source_external_id IS NOT NULL
                      AND dd.status IN ('confirmed', 'corrected')
                    GROUP BY dd.source_external_id
                )
                SELECT MIN(p.id) AS id,
                       MAX(p.id) AS newest_id,
                       MAX(p.source_channel) AS source_channel,
                       MAX(p.source_external_id) AS source_external_id,
                       MAX(p.original_text) AS original_text,
                       MAX(json_extract(p.draft_json, '$.proposed_ticker')) AS ticker,
                       MAX(COALESCE(
                           json_extract(p.draft_json, '$.proposed_action'),
                           json_extract(p.draft_json, '$.intent')
                       )) AS action,
                       MAX(json_extract(
                           p.draft_json, '$.proposed_rationale'
                       )) AS rationale,
                       CASE
                           WHEN MAX(ct.current_size_usd) IS NOT NULL
                                AND SUM(CAST(json_extract(
                                    p.draft_json, '$.proposed_amount_usd'
                                ) AS REAL)) IS NOT NULL
                           THEN MAX(ct.current_size_usd) + SUM(CAST(json_extract(
                               p.draft_json, '$.proposed_amount_usd'
                           ) AS REAL))
                           WHEN MAX(ct.current_size_usd) IS NOT NULL
                           THEN MAX(ct.current_size_usd)
                           ELSE SUM(CAST(json_extract(
                               p.draft_json, '$.proposed_amount_usd'
                           ) AS REAL))
                       END AS amount_usd,
                       COUNT(*) AS fill_count
                FROM pending p
                LEFT JOIN confirmed_totals ct
                  ON ct.source_external_id = p.source_external_id
                 AND p.source_channel = 'tracker'
                GROUP BY p.group_key
                ORDER BY newest_id DESC
                LIMIT 60
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                WITH pending AS (
                    SELECT *,
                           CASE
                               WHEN source_channel = 'tracker'
                                    AND source_external_id IS NOT NULL
                               THEN 'tracker:' || source_external_id
                               ELSE 'draft:' || CAST(id AS TEXT)
                           END AS group_key
                    FROM decision_drafts
                    WHERE status = 'awaiting_confirmation'
                )
                SELECT MIN(id) AS id,
                       MAX(id) AS newest_id,
                       MAX(source_channel) AS source_channel,
                       MAX(source_external_id) AS source_external_id,
                       MAX(original_text) AS original_text,
                       MAX(json_extract(draft_json, '$.proposed_ticker')) AS ticker,
                       MAX(COALESCE(
                           json_extract(draft_json, '$.proposed_action'),
                           json_extract(draft_json, '$.intent')
                       )) AS action,
                       MAX(json_extract(
                           draft_json, '$.proposed_rationale'
                       )) AS rationale,
                       SUM(CAST(json_extract(
                           draft_json, '$.proposed_amount_usd'
                       ) AS REAL)) AS amount_usd,
                       COUNT(*) AS fill_count
                FROM pending
                GROUP BY group_key
                ORDER BY newest_id DESC
                LIMIT 60
                """
            ).fetchall()
        if not rows:
            return (
                '<div class="mi-empty">Nothing pending — captures land here for confirmation.</div>'
            )
        summary = ""
        if counts is not None and int(counts["fill_count"]) != int(counts["group_count"]):
            summary = (
                '<div class="k-well mi-body">'
                f"Review {int(counts['group_count'])} trade decisions from "
                f"{int(counts['fill_count'])} underlying fills. Split fills stay linked "
                "as audit evidence.</div>"
            )
        # Cards render (incl. each row's calibration receipt) while the READ
        # connection is still open — it closes in the finally below, once.
        return summary + "".join(_draft_card(r, conn) for r in rows)
    except sqlite3.Error:
        return '<div class="mi-failed">Decision drafts unavailable.</div>'
    finally:
        conn.close()


def _card_disposition_card(row: sqlite3.Row) -> str:
    ticker_html = ticker_label(str(row["ticker"]))
    return (
        f'<div class="mi-card" data-card-artifact-id="{int(row["artifact_id"])}">'
        f'<div class="mi-card-head">{ticker_html}'
        '<span class="k-chip">investment decision card</span></div>'
        '<div class="mi-body">Current card has no Pass/Watch/Promote disposition yet.</div>'
        '<div class="mi-actions">'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        'data-card-disposition="pass">Pass</button>'
        '<button type="button" class="k-btn k-btn-quiet k-btn-sm" '
        'data-card-disposition="watch">Watch</button>'
        '<button type="button" class="k-btn k-btn-primary k-btn-sm" '
        'data-card-disposition="promote">Promote</button>'
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
            ORDER BY la.generated_at DESC
            """
        ).fetchall()
    except sqlite3.Error:
        return '<div class="mi-failed">Card dispositions unavailable.</div>'
    finally:
        conn.close()
    if not rows:
        return '<div class="mi-empty">No evaluation names awaiting a disposition.</div>'
    summary = (
        '<div class="k-well mi-body">'
        f"{len(rows)} evaluation names await Pass, Watch, or Promote. "
        "Every unresolved current card is shown below.</div>"
    )
    return summary + "".join(_card_disposition_card(r) for r in rows)


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


def _brief_empty_card(label: str, message: str) -> str:
    return (
        '<div class="mi-card">'
        f'<div class="mi-card-head"><span class="k-chip k-chip-mono">{escape(label)}</span>'
        '<span class="k-chip">no material item</span></div>'
        f'<div class="mi-body">{escape(message)}</div></div>'
    )


def _senior_partner_brief_section(db_path: Path) -> str:
    """P2.2 (src/advisor/senior_partner_brief.py, PRD §9.1): the five ordered
    sections from the latest ``senior_partner_brief`` artifact. The
    ``docs/design/personal_investment_partner_prd.md`` §12.2 states stay
    distinct: 'not generated yet' (no artifact at all) vs. 'failed to
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
    cards.append(
        _brief_item_card("Highest priority", content.get("highest_priority_decision"))
        or _brief_empty_card(
            "Highest priority",
            "No portfolio decision met the brief's action threshold this week.",
        )
    )
    cards.append(
        _brief_item_card("Capital use", content.get("capital_use"))
        or _brief_empty_card("Capital use", "No material capital-use decision this week.")
    )
    cards.append(
        _brief_item_card("Worth challenging", content.get("assumption_challenge"))
        or _brief_empty_card(
            "Worth challenging",
            "No assumption challenge was sufficiently grounded.",
        )
    )
    cards.append(
        _brief_item_card("Worth revisiting", content.get("decision_revisit"))
        or _brief_empty_card(
            "Worth revisiting",
            "No prior Owner Decision is ready to revisit.",
        )
    )
    body = "".join(c for c in cards if c)
    if not body:
        return '<div class="mi-empty">This week\'s brief has nothing to surface.</div>'
    return body


def _inbox_stream_section(db_path: Path) -> str:
    # Imported OUTSIDE the try: inside it, an import failure would leave
    # schema_drift_notice unbound and the handler would raise NameError while
    # trying to report the original error.
    from dashboard.inbox import collect_inbox, render_inbox_stream, schema_drift_notice
    from schema_compat import SchemaRevisionMismatch

    try:
        items = collect_inbox(db_path, limit=12)
        return render_inbox_stream(items, db_path=db_path, compact=True, surface="mobile")
    except SchemaRevisionMismatch as exc:
        # Named, not lumped into the generic failure line: schema drift has a
        # specific cause and a specific fix, and the owner should not have to
        # guess which of many things "unavailable" meant.
        return schema_drift_notice(exc)
    except Exception:
        return '<div class="mi-failed">Inbox stream unavailable.</div>'


_JS = """
<script>
(function () {
  document.body.addEventListener('submit', function (ev) {
    var form = ev.target.closest('[data-mi-correct-form]');
    if (!form) return;
    ev.preventDefault();
    var card = form.closest('[data-draft-id], [data-draft-group-id]');
    if (!card) return;
    var id = card.getAttribute('data-draft-id');
    var groupId = card.getAttribute('data-draft-group-id');
    var submit = form.querySelector('button[type="submit"]');
    var error = form.querySelector('[data-mi-correct-error]');
    var fields = new FormData(form);
    var amount = String(fields.get('proposed_amount_usd') || '').trim();
    var payload = {
      proposed_ticker: String(fields.get('proposed_ticker') || '').trim(),
      proposed_action: String(fields.get('proposed_action') || '').trim(),
      proposed_rationale: String(fields.get('proposed_rationale') || '').trim() || null
    };
    if (amount) payload.proposed_amount_usd = Number(amount);
    CCAction.busy(submit, 'Saving...');
    error.hidden = true;
    var endpoint = groupId
      ? ('/api/decision-draft-groups/' + groupId + '/correct')
      : ('/api/decision-drafts/' + id + '/correct');
    fetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (body) { return { r: r, body: body }; });
    }).then(function (res) {
      if (res.r.ok) { CCAction.leave(card); return; }
      CCAction.release(submit);
      error.textContent = res.body.error || 'Correction failed.';
      error.hidden = false;
    }).catch(function () {
      CCAction.release(submit);
      error.textContent = 'Correction failed.';
      error.hidden = false;
    });
  });
  document.body.addEventListener('click', function (ev) {
    var cancelCorrection = ev.target.closest('[data-mi-cancel-correction]');
    if (cancelCorrection) {
      var cancelForm = cancelCorrection.closest('[data-mi-correct-form]');
      if (cancelForm) cancelForm.hidden = true;
      return;
    }
    var dispositionBtn = ev.target.closest('[data-card-disposition]');
    if (dispositionBtn) {
      var dispositionCard = dispositionBtn.closest('[data-card-artifact-id]');
      if (!dispositionCard) return;
      var artifactId = dispositionCard.getAttribute('data-card-artifact-id');
      var disposition = dispositionBtn.getAttribute('data-card-disposition');
      var buttons = dispositionCard.querySelectorAll('[data-card-disposition]');
      buttons.forEach(function (button) { CCAction.busy(button); });
      CCAction.busy(dispositionBtn, '...');
      function releaseDisposition() {
        buttons.forEach(function (button) { CCAction.release(button); });
      }
      fetch('/api/research/card/' + artifactId + '/' + disposition, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      }).then(function (r) {
        if (r.ok) { CCAction.leave(dispositionCard); }
        else { releaseDisposition(); }
      }).catch(releaseDisposition);
      return;
    }
    var dismissBtn = ev.target.closest('[data-mi-dismiss-ping]');
    if (dismissBtn) {
      var pingId = dismissBtn.getAttribute('data-mi-dismiss-ping');
      CCAction.busy(dismissBtn, '...');
      fetch('/api/senior-partner-brief/dismiss-item/' + pingId, { method: 'POST' })
        .then(function (r) { return r.json().then(function (body) { return { r: r, body: body }; }); })
        .then(function (res) {
          var card = dismissBtn.closest('.mi-card');
          if (res.r.ok) {
            // Receipt beat (what happened, visibly) before the card leaves —
            // the ledger packet-walk pacing.
            CCAction.receipt(dismissBtn, res.body.muted_class
              ? ('Muted ' + res.body.muted_class)
              : 'Dismissed');
            if (card) { setTimeout(function () { CCAction.leave(card); }, 1100); }
          } else {
            CCAction.release(dismissBtn);
          }
        })
        .catch(function () { CCAction.release(dismissBtn); });
      return;
    }
    var btn = ev.target.closest('[data-mi-act]');
    if (!btn) return;
    var act = btn.getAttribute('data-mi-act');
    var card = btn.closest('[data-draft-id], [data-draft-group-id]');
    if (!card) return;
    var id = card.getAttribute('data-draft-id');
    var groupId = card.getAttribute('data-draft-group-id');
    if (act === 'defer') {
      // Deliberately client-only: defer writes nothing server-side, so the
      // receipt must say exactly that rather than imply a persisted state.
      CCAction.receipt(btn, 'Deferred (this session only)');
      card.classList.add('mi-deferred');
      return;
    }
    if (act === 'correct') {
      var correctionForm = card.querySelector('[data-mi-correct-form]');
      if (correctionForm) {
        correctionForm.hidden = false;
        var tickerInput = correctionForm.querySelector('[name="proposed_ticker"]');
        if (tickerInput) tickerInput.focus();
      }
      return;
    }
    CCAction.busy(btn, '...');
    var endpoint = groupId
      ? ('/api/decision-draft-groups/' + groupId + '/' + act)
      : ('/api/decision-drafts/' + id + '/' + act);
    fetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function (r) {
      if (r.ok) { CCAction.leave(card); } else { CCAction.release(btn); }
    }).catch(function () { CCAction.release(btn); });
  });
})();
</script>
"""


def render_mobile_inbox(db_path: Path) -> str:
    """The full standalone mobile page. Composes the five sections above; a
    single section's read failure never blanks the rest (each is isolated)."""
    # Standalone document (not the shell): the CCAction primitive rides along
    # explicitly, same as palette + controls.
    style = (
        f"<style>{palette_css('paper')}</style><style>{controls_css('paper')}</style>"
        f"<style>{CC_ACTION_CSS}</style>{_STYLE}"
    )
    body = (
        '<h1 class="mi-h1">Inbox</h1>'
        '<section class="mi-sec"><h2 class="mi-sec-h">Allocation decision</h2>'
        f"{_allocation_recommendation_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Decision drafts</h2>'
        f"{_drafts_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Card dispositions</h2>'
        f"{_card_dispositions_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Senior Partner Brief</h2>'
        f"{_senior_partner_brief_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Recent activity</h2>'
        f"{_inbox_stream_section(db_path)}</section>"
    )
    scripts = (
        f"<script>{CC_ACTION_JS}</script>{_JS}"
        f"<script data-k-select-runtime>{controls_js()}</script>"
    )
    return f"<!doctype html><html><head>{_HEAD}{style}</head><body>{body}{scripts}</body></html>"


__all__ = ["render_mobile_inbox"]
