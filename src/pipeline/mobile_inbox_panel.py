"""Compact private mobile review surface.

This responsive view reuses the canonical Decision Draft, Investment Decision
Card, and activity-stream reads and action cores. Retired allocation-
recommendation and weekly-brief products are deliberately absent.
"""

from __future__ import annotations

import sqlite3
from html import escape
from pathlib import Path

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
    """The full standalone mobile page. Composes the three sections above; a
    single section's read failure never blanks the rest (each is isolated)."""
    # Standalone document (not the shell): the CCAction primitive rides along
    # explicitly, same as palette + controls.
    style = (
        f"<style>{palette_css('paper')}</style><style>{controls_css('paper')}</style>"
        f"<style>{CC_ACTION_CSS}</style>{_STYLE}"
    )
    body = (
        '<h1 class="mi-h1">Inbox</h1>'
        '<section class="mi-sec"><h2 class="mi-sec-h">Decision drafts</h2>'
        f"{_drafts_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Card dispositions</h2>'
        f"{_card_dispositions_section(db_path)}</section>"
        '<section class="mi-sec"><h2 class="mi-sec-h">Recent activity</h2>'
        f"{_inbox_stream_section(db_path)}</section>"
    )
    scripts = (
        f"<script>{CC_ACTION_JS}</script>{_JS}"
        f"<script data-k-select-runtime>{controls_js()}</script>"
    )
    return f"<!doctype html><html><head>{_HEAD}{style}</head><body>{body}{scripts}</body></html>"


__all__ = ["render_mobile_inbox"]
