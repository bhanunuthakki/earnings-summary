"""When the comments server is unreachable, the inline-comment POST
isn't lost: the payload is queued in localStorage and retried on a timer
/ focus / online event until it lands. Drafts (Fix 1) cover the *still-
composing* case; the outbox covers the *already-submitted* case.

Pure render-helper test — same string-assertion pattern as
test_workspace_comments_drafts.py and test_workspace_chat_sidebar.py.

The outbox contract this PR commits to:

  • One localStorage key (`cmt-outbox`) holding a JSON array of
    {id, payload, ts} entries. Each entry is a full POST body.
  • Failure path of onSubmit() must enqueue + clear textarea +
    clear draft (because the outbox is now the source of truth).
  • flushOutbox() POSTs entries sequentially, stops on the first
    failure, and clears each entry's draft on success.
  • Three wake-up triggers: setInterval + 'online' + 'focus'.
  • Window-level hook `__flushOutbox` so the health-pill module
    (Fix 3) can kick a flush on server-recovery without coupling.
  • A visible badge (`#cmt-outbox-badge`) is injected into the
    sidebar header and counts pending entries.
  • 7-day TTL drop so a long-dead queue doesn't grow forever.
  • Re-entrancy guard so the timer + focus event don't double-flush.
"""

from __future__ import annotations

from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS

# ---------------------------------------------------------------------------
# Storage shape — one key, JSON array, namespaced helpers.
# ---------------------------------------------------------------------------


def test_outbox_uses_single_localStorage_key() -> None:
    assert "'cmt-outbox'" in COMMENTS_JS
    # All three accessors funnel through it.
    assert "function loadOutbox" in COMMENTS_JS
    assert "function saveOutbox" in COMMENTS_JS
    assert "function enqueueOutbox" in COMMENTS_JS


def test_outbox_helpers_swallow_storage_errors() -> None:
    # Same robustness contract as the draft helpers — Safari private
    # mode and quota-exceeded throw synchronously from localStorage.
    for fn in ("function loadOutbox", "function saveOutbox"):
        block = COMMENTS_JS.split(fn)[1].split("\n  }")[0]
        assert "try {" in block, f"{fn} missing try/catch around localStorage"
        assert "catch (e)" in block, f"{fn} missing catch for storage errors"


# ---------------------------------------------------------------------------
# Submit failure path — the actual contract change vs Fix 1.
# ---------------------------------------------------------------------------


def test_submit_failure_enqueues_and_clears_form() -> None:
    catch_block = COMMENTS_JS.split(".catch(function(err)")[1].split("});")[0]
    # The failure branch must enqueue the payload it tried to POST.
    assert "enqueueOutbox(payload)" in catch_block
    # And clear textarea + draft so the form returns to a "ready" state.
    # The outbox is now the source of truth for that submission.
    assert "form.reset()" in catch_block
    assert "clearDraft(anchorAtSubmit)" in catch_block
    # User-visible signal — "Queued" hint, not "Server unreachable".
    assert "Queued" in catch_block


# ---------------------------------------------------------------------------
# Flush behavior — sequential, halts on first failure, clears drafts.
# ---------------------------------------------------------------------------


def test_flush_is_sequential_and_halts_on_failure() -> None:
    flush_block = COMMENTS_JS.split("function flushOutbox")[1].split("\n  }")[0]
    # for-loop over entries — sequential, not Promise.all.
    assert "for (var i = 0" in flush_block
    # Network error (catch) breaks the loop, leaving the rest queued.
    assert "break;" in flush_block
    # 4xx/5xx response also halts — don't drop entries on transient
    # server-side errors.
    assert "if (!r.ok) break" in flush_block


def test_flush_clears_each_entrys_draft_on_success() -> None:
    assert "clearDraft(it.payload.anchor)" in COMMENTS_JS


def test_flush_is_reentrancy_guarded() -> None:
    # Timer + focus events overlap — without a guard, two concurrent
    # flushes would race on the same queue.
    assert "outboxFlushing" in COMMENTS_JS
    assert "if (outboxFlushing) return" in COMMENTS_JS


def test_flush_drops_entries_older_than_a_week() -> None:
    assert "OUTBOX_MAX_AGE_MS" in COMMENTS_JS
    # 7 days literal so a future tweak doesn't accidentally shrink it.
    assert "7 * 24 * 60 * 60 * 1000" in COMMENTS_JS


# ---------------------------------------------------------------------------
# Wake-up triggers + external hook.
# ---------------------------------------------------------------------------


def test_flush_triggered_on_timer_online_and_focus() -> None:
    # All three triggers must be wired — relying on just the timer
    # makes the recovery feel laggy on focus return.
    assert "setInterval(flushOutbox" in COMMENTS_JS
    assert "addEventListener('online', flushOutbox)" in COMMENTS_JS
    assert "addEventListener('focus', flushOutbox)" in COMMENTS_JS


def test_flush_exposed_to_health_pill_module() -> None:
    # Fix 3 (health pill) needs to be able to kick a flush on server-
    # recovery without knowing our internals.
    assert "window.__flushOutbox = flushOutbox" in COMMENTS_JS


# ---------------------------------------------------------------------------
# Badge UI — visible counter in the sidebar header.
# ---------------------------------------------------------------------------


def test_badge_is_injected_and_updated() -> None:
    # Element id + class are referenced by the badge updater.
    assert "id = 'cmt-outbox-badge'" in COMMENTS_JS
    assert "function updateOutboxBadge" in COMMENTS_JS
    # Badge text is "Queued: N" when n > 0, hidden when empty.
    badge_block = COMMENTS_JS.split("function updateOutboxBadge")[1].split("\n  }")[0]
    assert "'Queued: '" in badge_block
    assert "display = n ? 'inline-block' : 'none'" in badge_block


def test_badge_css_exists() -> None:
    # Amber pill, not error red — pending recovery, not failure. The warn tone
    # rides the --warn token (color-mix), not a hardcoded amber rgba.
    badge_rule = COMMENTS_CSS.split(".cmt-outbox-badge {", 1)
    assert len(badge_rule) == 2
    rule = badge_rule[1].split("}", 1)[0]
    assert "color: var(--warn)" in rule
    assert "color-mix(in srgb, var(--warn)" in rule
