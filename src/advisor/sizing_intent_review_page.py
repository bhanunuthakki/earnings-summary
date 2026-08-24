"""Standalone, evidence-first sizing-intent review page.

The page is intentionally a thin local UI over :mod:`advisor.sizing_intent_review`
and the governed checkpoint API.  It renders only persisted facts and lets the
owner paste the already-reviewed checkpoint payload; it never manufactures a
target, a threshold, or a thesis break condition.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from advisor.price_action_bands import PriceActionBandProjection
from advisor.sizing_intent_review import (
    SizingIntentReview,
    SizingIntentReviewEntry,
    load_sizing_intent_review,
)
from pipeline.portfolio_styles import sizing_intent_review_css
from pipeline.work_os_route_contract import DESTINATION_SURFACE_IDS
from ui.controls import controls_css
from ui.tokens import FAVICON_LINK, palette_css

__all__ = ["render_sizing_intent_review_page"]


_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.=-]{0,14}\Z")
_SECTION_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")


def _work_os_return_url(origin: str | None) -> str:
    """Decode only the compact Work OS origin contract; never reflect a URL."""

    fields = origin.split("|") if isinstance(origin, str) else []
    if len(fields) != 3 or fields[0] not in DESTINATION_SURFACE_IDS:
        return "/"
    surface, ticker, section = fields
    if ticker and _TICKER_RE.fullmatch(ticker) is None:
        return "/"
    if section and _SECTION_RE.fullmatch(section) is None:
        return "/"
    query: list[str] = []
    if ticker:
        query.append(f"ticker={ticker}")
    if section in {"company-desk", "analytics-playground"}:
        query.append(f"screen={section}")
    return "/" + ("?" + "&".join(query) if query else "") + "#" + surface


def render_sizing_intent_review_page(
    db_path: Path, ticker: str, *, work_os_origin: str | None = None
) -> str:
    """Render one ticker's dense, full-page owner review surface."""

    clean_ticker = ticker.strip().upper()
    review = load_sizing_intent_review(db_path)
    entries = tuple(item for item in review.entries if item.intent.ticker.upper() == clean_ticker)
    return "".join(
        (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"{FAVICON_LINK}<title>{escape(clean_ticker)} sizing-intent review</title><style>",
            palette_css("dark"),
            controls_css("dark"),
            sizing_intent_review_css(),
            '</style></head><body class="sir-body"><main class="sir-main">',
            f'<a class="sir-back" href="{escape(_work_os_return_url(work_os_origin), quote=True)}">← Workspace</a><header class="sir-head"><p class="k-label">Owner decision record</p><h1>{escape(clean_ticker)} sizing-intent review</h1>',
            '<p class="sir-sub">Review persisted owner evidence only. This page does not recommend a trade and cannot place one.</p></header>',
            _status_block(review, entries),
            _evidence_block(entries),
            _action_form(clean_ticker, entries),
            "</main><script>",
            _PAGE_JS,
            "</script></body></html>",
        )
    )


def _status_block(review: SizingIntentReview, entries: tuple[SizingIntentReviewEntry, ...]) -> str:
    if not review.sizing_intent_source_available or not entries:
        status_rows = _status_row(review, None)
    else:
        status_rows = "".join(_status_row(review, entry) for entry in entries)
    return (
        '<section class="k-card k-card-dense sir-status" aria-labelledby="sir-status-title">'
        '<div><p class="k-label" id="sir-status-title">Review state</p>'
        f"{status_rows}"
        '<p class="sir-state-key">States: ratified; draft / review-needed; partial; stale '
        "(only when a canonical freshness state is recorded); unencoded; unavailable.</p>"
        "</div></section>"
    )


def _status_row(review: SizingIntentReview, entry: SizingIntentReviewEntry | None) -> str:
    status, tone, detail = _review_status(review, entry)
    kind = "" if entry is None else f" · {entry.intent.intent_kind}"
    return (
        f'<p class="sir-status-line"><span class="k-pill k-pill-{tone}">{escape(status)}</span> '
        f"{escape(detail)}{escape(kind)}</p>"
    )


def _review_status(
    review: SizingIntentReview, entry: SizingIntentReviewEntry | None
) -> tuple[str, str, str]:
    if not review.sizing_intent_source_available:
        return "unavailable", "bad", "Sizing-intent storage is unavailable; no history is assumed."
    if entry is None:
        return "unencoded", "muted", "No current sizing intent is encoded for this ticker."
    if not review.checkpoint_link_source_available:
        return (
            "partial",
            "warn",
            "Intent history is present, but checkpoint linkage is unavailable.",
        )
    if entry.checkpoint_evidence_available:
        return "ratified", "ok", "Checkpoint evidence verifies this current revision."
    if entry.checkpoint_linked:
        return (
            "review-needed",
            "warn",
            "A checkpoint link exists, but its evidence cannot be verified.",
        )
    # There is deliberately no local freshness policy for sizing evidence.  Do
    # not silently call an old as-of value "stale" without an owner-approved
    # threshold; render it below and state that staleness is unencoded.
    return "draft", "warn", "Current intent is not checkpoint-ratified; staleness is unencoded."


def _evidence_block(entries: tuple[SizingIntentReviewEntry, ...]) -> str:
    if not entries:
        return (
            '<section class="k-card sir-section"><h2 class="k-card-title">Persisted evidence</h2>'
            '<p class="k-empty">No revision, target band, holdings basis, or provenance is encoded for this ticker.</p>'
            '<p class="sir-boundary">No broker connection or execution control is available on this page.</p></section>'
        )
    return "".join(_entry_evidence_block(entry) for entry in entries)


def _entry_evidence_block(entry: SizingIntentReviewEntry) -> str:
    intent = entry.intent
    facts = (
        ("Revision", str(intent.id)),
        ("Intent kind", intent.intent_kind),
        (
            "Intent value",
            _number(intent.intent_value, "%" if intent.intent_kind.endswith("_pct") else ""),
        ),
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
    price_action_bands = _price_action_band_block(entry.price_action_bands, intent.id)
    return (
        '<section class="k-card sir-section" aria-labelledby="sir-evidence-title-'
        + str(intent.id)
        + '">'
        + '<h2 class="k-card-title" id="sir-evidence-title-'
        + str(intent.id)
        + '">Persisted evidence · '
        + escape(intent.intent_kind)
        + "</h2>"
        f'<dl class="sir-facts">{rows}</dl>{price_action_bands}<div class="k-well sir-narrative"><p class="k-label">Owner narrative</p><p>{narrative}</p></div>'
        '<p class="sir-boundary">No broker connection or execution control is available on this page.</p></section>'
    )


def _price_action_band_block(projection: PriceActionBandProjection, intent_id: int) -> str:
    """Render only the typed ladder projection, never an inferred condition."""

    state = projection.state.value
    actionability = (
        "Actionable for a future deterministic sensor only; no execution route exists."
        if projection.is_actionable
        else "Not actionable; no sensor may arm this ladder."
    )
    facts = (
        ("Ladder state", state),
        ("Actionability", actionability),
        ("Add / Buy below", _price_level(projection.add_below, projection.currency)),
        ("Hold low", _price_level(projection.hold_low, projection.currency)),
        ("Hold high", _price_level(projection.hold_high, projection.currency)),
        ("Trim above", _price_level(projection.trim_above, projection.currency)),
        ("Sell above", _price_level(projection.sell_above, projection.currency)),
        (
            "Approach add / buy below",
            _price_level(
                None
                if projection.approach_bands is None
                else projection.approach_bands.add_buy_below,
                projection.currency,
            ),
        ),
        (
            "Approach trim above",
            _price_level(
                None if projection.approach_bands is None else projection.approach_bands.trim_above,
                projection.currency,
            ),
        ),
        (
            "Approach sell above",
            _price_level(
                None if projection.approach_bands is None else projection.approach_bands.sell_above,
                projection.currency,
            ),
        ),
        ("Band owner", projection.owner or "unencoded"),
        ("Band revision", projection.revision or "unencoded"),
        ("Band as-of", _datetime_value(projection.as_of)),
        ("Checkpoint-bound source", projection.source_ref or "unencoded"),
        ("Checkpoint-bound digest", projection.source_content_sha256 or "unencoded"),
        ("Declared input source", projection.declared_source_ref or "unencoded"),
        (
            "Declared input digest",
            projection.declared_source_content_sha256 or "unencoded",
        ),
    )
    rows = "".join(
        f'<div class="sir-fact"><dt>{escape(label)}</dt><dd>{escape(value)}</dd></div>'
        for label, value in facts
    )
    return (
        f'<section class="k-well sir-price-action-bands" aria-labelledby="sir-bands-title-{intent_id}">'
        f'<p class="k-label" id="sir-bands-title-{intent_id}">Structured price-action bands</p>'
        '<p class="sir-sub">Only typed checkpoint fields appear here. Owner narrative and thesis conditions are not sizing thresholds.</p>'
        f'<dl class="sir-facts">{rows}</dl></section>'
    )


def _action_form(ticker: str, entries: tuple[SizingIntentReviewEntry, ...]) -> str:
    revisions = {entry.intent.intent_kind: entry.intent.id for entry in entries}
    config = json.dumps(
        {"ticker": ticker, "currentRevisions": revisions}, separators=(",", ":")
    ).replace("<", "\\u003c")
    return (
        '<section class="k-card sir-section" aria-labelledby="sir-action-title">'  # nosec B608 -- static HTML form; no SQL is constructed.
        '<h2 class="k-card-title" id="sir-action-title">Owner checkpoint</h2>'
        '<p class="sir-sub">Add, revise, or ratify an already-reviewed payload. Required fields remain explicit so this UI does not invent decision context.</p>'
        '<form id="sir-form" novalidate>'
        '<div class="sir-form-grid">'
        '<label>Action<select id="sir-action" name="action"><option value="add">Add</option><option value="revise">Revise</option><option value="ratify">Ratify</option></select></label>'
        '<label>Expected current revision<output id="sir-prior">Derived from the reviewed payload intent kind on save.</output></label>'
        '<label>Source event ID<input id="sir-event" name="source_event_id" type="text" required autocomplete="off"></label>'
        '</div><label class="sir-payload-label" for="sir-payload">Reviewed checkpoint payload (JSON)</label>'
        '<textarea id="sir-payload" required spellcheck="false" aria-describedby="sir-payload-help" placeholder="Paste the reviewed holdings_basis, leg, sizing_intent, and any ledger_entries."></textarea>'
        '<p id="sir-payload-help" class="sir-help">The page sets action, source event ID, and expected revision. Payload ticker values must match the page ticker.</p>'
        '<p id="sir-error" class="sir-error" role="alert" aria-live="polite"></p>'
        '<div class="sir-actions"><button class="k-btn k-btn-primary" type="submit">Save owner checkpoint</button><span id="sir-result" aria-live="polite"></span></div>'
        "</form></section>"
        f'<script type="application/json" id="sir-config">{config}</script>'
    )


def _number(value: object, suffix: str = "") -> str:
    return "unencoded" if value is None else f"{value}{suffix}"


def _enum_value(value: object | None) -> str:
    return "unencoded" if value is None else str(getattr(value, "value", value))


def _target_band(entry: SizingIntentReviewEntry) -> str:
    if entry.target_band is None:
        return "unencoded"
    return f"{entry.target_band.minimum_pct}% to {entry.target_band.maximum_pct}%"


def _price_level(value: float | None, currency: str | None) -> str:
    if value is None:
        return "unencoded"
    return f"{value} {currency or 'currency unencoded'}"


def _datetime_value(value: object | None) -> str:
    if value is None:
        return "unencoded"
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat() if callable(isoformat) else value)


_PAGE_JS = r"""
(() => {
  const config = JSON.parse(document.getElementById('sir-config').textContent);
  const form = document.getElementById('sir-form'); const action = document.getElementById('sir-action');
  const prior = document.getElementById('sir-prior'); const eventId = document.getElementById('sir-event');
  const payload = document.getElementById('sir-payload'); const error = document.getElementById('sir-error');
  const result = document.getElementById('sir-result');
  eventId.value = `sizing-review:${config.ticker}:${crypto.randomUUID ? crypto.randomUUID() : Date.now()}`;
  function errorFor(message, focus) { error.textContent = message; if (focus) focus.focus(); }
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); error.textContent = ''; result.textContent = '';
    let body; try { body = JSON.parse(payload.value); } catch (_) { errorFor('Paste a valid reviewed JSON payload.', payload); return; }
    if (!body || Array.isArray(body)) { errorFor('Reviewed payload must be a JSON object.', payload); return; }
    if (!body.sizing_intent || typeof body.sizing_intent !== 'object' || Array.isArray(body.sizing_intent)) { errorFor('Reviewed payload must include one sizing_intent object.', payload); return; }
    const intentKind = String(body.sizing_intent.intent_kind || '').trim();
    if (!intentKind) { errorFor('Reviewed sizing intent must include intent_kind.', payload); return; }
    const currentRevision = Object.prototype.hasOwnProperty.call(config.currentRevisions, intentKind) ? config.currentRevisions[intentKind] : null;
    body.action = action.value; body.source_event_id = eventId.value.trim();
    if (!body.source_event_id) { errorFor('Source event ID is required.', eventId); return; }
    body.expected_prior_sizing_intent_id = currentRevision;
    if (body.action === 'add' && currentRevision !== null) { errorFor('This intent kind already has revision ' + currentRevision + '; choose Revise or Ratify.', action); return; }
    if ((body.action === 'revise' || body.action === 'ratify') && !Number.isInteger(currentRevision)) { errorFor('Revise and ratify require an encoded current revision for intent kind ' + intentKind + '.', payload); return; }
    if (body.action === 'ratify') { body.sizing_intent = {...body.sizing_intent, existing_sizing_intent_id: body.expected_prior_sizing_intent_id}; }
    prior.textContent = currentRevision === null ? 'No current revision is encoded for ' + intentKind + '.' : 'Revision ' + currentRevision + ' derived for ' + intentKind + '.';
    const submit = form.querySelector('button[type="submit"]'); submit.disabled = true; submit.textContent = 'Saving…';
    try { const response = await fetch(`/api/sizing-intents/${encodeURIComponent(config.ticker)}/checkpoint`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Checkpoint could not be saved.'); result.textContent = `Saved revision ${data.projection.sizing_intent_id}.`; window.setTimeout(() => window.location.reload(), 500); }
    catch (cause) { errorFor(cause instanceof Error ? cause.message : 'Checkpoint could not be saved.', payload); }
    finally { submit.disabled = false; submit.textContent = 'Save owner checkpoint'; }
  });
})();
"""
