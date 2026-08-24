"""Standalone, evidence-first sizing-intent review page.

The page is intentionally a thin local UI over :mod:`advisor.sizing_intent_review`
and the governed checkpoint API.  It renders only persisted facts and lets the
owner paste the already-reviewed checkpoint payload; it never manufactures a
target, a threshold, or a thesis break condition.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from advisor.sizing_intent_review import (
    SizingIntentReview,
    SizingIntentReviewEntry,
    load_sizing_intent_review,
)
from pipeline.portfolio_styles import sizing_intent_review_css
from research.owner_decision_checkpoint import TargetBand
from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css

__all__ = ["render_sizing_intent_review_page"]


def render_sizing_intent_review_page(db_path: Path, ticker: str) -> str:
    """Render one ticker's dense, full-page owner review surface."""

    clean_ticker = ticker.strip().upper()
    review = load_sizing_intent_review(db_path)
    entry = next((item for item in review.entries if item.intent.ticker.upper() == clean_ticker), None)
    return "".join(
        (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"{FAVICON_LINK}<title>{escape(clean_ticker)} sizing-intent review</title><style>",
            palette_css("dark"),
            controls_css("dark"),
            sizing_intent_review_css(),
            "</style></head><body class=\"sir-body\"><main class=\"sir-main\">",
            f"<a class=\"sir-back\" href=\"/\">← Workspace</a><header class=\"sir-head\"><p class=\"k-label\">Owner decision record</p><h1>{escape(clean_ticker)} sizing-intent review</h1>",
            "<p class=\"sir-sub\">Review persisted owner evidence only. This page does not recommend a trade and cannot place one.</p></header>",
            _status_block(review, entry),
            _evidence_block(entry),
            _action_form(clean_ticker, entry),
            "</main><script>",
            _PAGE_JS,
            "</script></body></html>",
        )
    )


def _status_block(review: SizingIntentReview, entry: SizingIntentReviewEntry | None) -> str:
    status, tone, detail = _review_status(review, entry)
    return (
        '<section class="k-card k-card-dense sir-status" aria-labelledby="sir-status-title">'
        '<div><p class="k-label" id="sir-status-title">Review state</p>'
        f'<p class="sir-status-line"><span class="k-pill k-pill-{tone}">{escape(status)}</span> '
        f"{escape(detail)}</p>"
        '<p class="sir-state-key">States: ratified; draft / review-needed; partial; stale '
        '(only when a canonical freshness state is recorded); unencoded; unavailable.</p>'
        "</div></section>"
    )


def _review_status(
    review: SizingIntentReview, entry: SizingIntentReviewEntry | None
) -> tuple[str, str, str]:
    if not review.sizing_intent_source_available:
        return "unavailable", "bad", "Sizing-intent storage is unavailable; no history is assumed."
    if entry is None:
        return "unencoded", "muted", "No current sizing intent is encoded for this ticker."
    if not review.checkpoint_link_source_available:
        return "partial", "warn", "Intent history is present, but checkpoint linkage is unavailable."
    if entry.checkpoint_evidence_available:
        return "ratified", "ok", "Checkpoint evidence verifies this current revision."
    if entry.checkpoint_linked:
        return "review-needed", "warn", "A checkpoint link exists, but its evidence cannot be verified."
    # There is deliberately no local freshness policy for sizing evidence.  Do
    # not silently call an old as-of value "stale" without an owner-approved
    # threshold; render it below and state that staleness is unencoded.
    return "draft", "warn", "Current intent is not checkpoint-ratified; staleness is unencoded."


def _evidence_block(entry: SizingIntentReviewEntry | None) -> str:
    if entry is None:
        return (
            '<section class="k-card sir-section"><h2 class="k-card-title">Persisted evidence</h2>'
            '<p class="k-empty">No revision, target band, holdings basis, or provenance is encoded for this ticker.</p>'
            '<p class="sir-boundary">No broker connection or execution control is available on this page.</p></section>'
        )
    intent = entry.intent
    facts = (
        ("Revision", str(intent.id)),
        ("Intent kind", intent.intent_kind),
        ("Intent value", _number(intent.intent_value, "%")),
        ("Recorded", intent.created_at.isoformat()),
        ("Updated", intent.updated_at.isoformat()),
        ("Checkpoint", _number(entry.checkpoint_id)),
        ("Checkpoint confirmed", entry.checkpoint_confirmed_at or "unencoded"),
        ("Holdings source", entry.holdings_source or "unencoded"),
        ("Holdings as-of", entry.holdings_as_of or "unencoded"),
        ("Observed weight", _number(entry.observed_weight_pct, "%")),
        ("Holding availability", _enum_value(entry.holding_availability)),
        ("Target verification", _enum_value(entry.target_verification)),
        ("Target band", _target_band(entry)),
        ("Price level", _number(entry.price_level)),
        ("Provenance digest", entry.checkpoint_payload_sha256 or "unencoded"),
    )
    rows = "".join(
        f'<div class="sir-fact"><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>'
        for label, value in facts
    )
    narrative = escape(intent.narrative or "unencoded")
    return (
        '<section class="k-card sir-section" aria-labelledby="sir-evidence-title">'
        '<h2 class="k-card-title" id="sir-evidence-title">Persisted evidence</h2>'
        f'<dl class="sir-facts">{rows}</dl><div class="k-well sir-narrative"><p class="k-label">Owner narrative</p><p>{narrative}</p></div>'
        '<p class="sir-boundary">No broker connection or execution control is available on this page.</p></section>'
    )


def _action_form(ticker: str, entry: SizingIntentReviewEntry | None) -> str:
    revision = "" if entry is None else str(entry.intent.id)
    return (
        '<section class="k-card sir-section" aria-labelledby="sir-action-title">'
        '<h2 class="k-card-title" id="sir-action-title">Owner checkpoint</h2>'
        '<p class="sir-sub">Add, revise, or ratify an already-reviewed payload. Required fields remain explicit so this UI does not invent decision context.</p>'
        '<form id="sir-form" novalidate>'
        '<div class="sir-form-grid">'
        '<label>Action<select id="sir-action" name="action"><option value="add">Add</option><option value="revise">Revise</option><option value="ratify">Ratify</option></select></label>'
        f'<label>Expected current revision<input id="sir-prior" name="expected_prior_sizing_intent_id" type="number" min="1" value="{escape(revision, quote=True)}" inputmode="numeric"></label>'
        '<label>Source event ID<input id="sir-event" name="source_event_id" type="text" required autocomplete="off"></label>'
        '</div><label class="sir-payload-label" for="sir-payload">Reviewed checkpoint payload (JSON)</label>'
        '<textarea id="sir-payload" required spellcheck="false" aria-describedby="sir-payload-help" placeholder="Paste the reviewed holdings_basis, leg, sizing_intent, and any ledger_entries."></textarea>'
        '<p id="sir-payload-help" class="sir-help">The page sets action, source event ID, and expected revision. Payload ticker values must match the page ticker.</p>'
        '<p id="sir-error" class="sir-error" role="alert" aria-live="polite"></p>'
        '<div class="sir-actions"><button class="k-btn k-btn-primary" type="submit">Save owner checkpoint</button><span id="sir-result" aria-live="polite"></span></div>'
        '</form></section>'
        f'<script type="application/json" id="sir-config">{{"ticker":"{escape(ticker)}","currentRevision":{revision or "null"}}}</script>'
    )


def _number(value: object, suffix: str = "") -> str:
    return "unencoded" if value is None else f"{value}{suffix}"


def _enum_value(value: object | None) -> str:
    return "unencoded" if value is None else str(getattr(value, "value", value))


def _target_band(entry: SizingIntentReviewEntry) -> str:
    band: TargetBand | None = entry.target_band
    if band is None:
        return "unencoded"
    return f"{band.minimum_pct}% to {band.maximum_pct}%"


_PAGE_JS = r"""
(() => {
  const config = JSON.parse(document.getElementById('sir-config').textContent);
  const form = document.getElementById('sir-form'); const action = document.getElementById('sir-action');
  const prior = document.getElementById('sir-prior'); const eventId = document.getElementById('sir-event');
  const payload = document.getElementById('sir-payload'); const error = document.getElementById('sir-error');
  const result = document.getElementById('sir-result');
  eventId.value = `sizing-review:${config.ticker}:${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`;
  function errorFor(message, focus) { error.textContent = message; if (focus) focus.focus(); }
  action.addEventListener('change', () => { prior.disabled = action.value === 'add'; if (action.value === 'add') prior.value = ''; else if (!prior.value && config.currentRevision) prior.value = config.currentRevision; });
  action.dispatchEvent(new Event('change'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); error.textContent = ''; result.textContent = '';
    let body; try { body = JSON.parse(payload.value); } catch (_) { errorFor('Paste a valid reviewed JSON payload.', payload); return; }
    if (!body || Array.isArray(body)) { errorFor('Reviewed payload must be a JSON object.', payload); return; }
    body.action = action.value; body.source_event_id = eventId.value.trim();
    if (!body.source_event_id) { errorFor('Source event ID is required.', eventId); return; }
    const expected = prior.value.trim(); body.expected_prior_sizing_intent_id = expected ? Number(expected) : null;
    if ((body.action === 'revise' || body.action === 'ratify') && !Number.isInteger(body.expected_prior_sizing_intent_id)) { errorFor('Revise and ratify require the current revision.', prior); return; }
    if (body.action === 'ratify') { body.sizing_intent = {...body.sizing_intent, existing_sizing_intent_id: body.expected_prior_sizing_intent_id}; }
    const submit = form.querySelector('button[type="submit"]'); submit.disabled = true; submit.textContent = 'Saving…';
    try { const response = await fetch(`/api/sizing-intents/${encodeURIComponent(config.ticker)}/checkpoint`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Checkpoint could not be saved.'); result.textContent = `Saved revision ${data.projection.sizing_intent_id}.`; window.setTimeout(() => window.location.reload(), 500); }
    catch (cause) { errorFor(cause instanceof Error ? cause.message : 'Checkpoint could not be saved.', payload); }
    finally { submit.disabled = false; submit.textContent = 'Save owner checkpoint'; }
  });
})();
"""
