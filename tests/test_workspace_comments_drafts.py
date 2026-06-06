"""Inline-comment drafts are autosaved to localStorage so an unposted
comment isn't lost on tab close, page refresh, or a server-down outage.

Pure render-helper test — same pattern as test_workspace_chat_sidebar.py:
assert key string fragments are present in the inlined JS bundle so we
catch accidental removal of the autosave wiring during refactors.

Three lifecycle hooks must exist:
  1. Restore   — opening a sidebar rehydrates the textarea from localStorage.
  2. Save      — every keystroke writes the current text under the anchor key.
  3. Clear     — a successful POST removes the draft for the *submit-time*
                 anchor (not whatever anchor is open when the response
                 arrives — the user may have navigated away).

The "Server unreachable" fallback must also keep the draft on disk (not
clear it), so the user can recover after restarting the Flask server.
"""

from __future__ import annotations

from report.renderers.workspace_comments import JS as COMMENTS_JS

# ---------------------------------------------------------------------------
# Draft key shape — namespaced by (ticker, report_date, anchor.type, anchor.key)
# so two reports / two anchors don't collide.
# ---------------------------------------------------------------------------


def test_draft_key_is_namespaced_by_ticker_date_and_anchor() -> None:
    # The helper uses the boot-loaded TICKER + REPORT_DATE plus the anchor
    # type/key. We can't run the JS in pytest, but we can assert the literal
    # key shape is built — this is what guarantees no cross-report leakage.
    assert "'cmt-draft:' + TICKER + ':' + REPORT_DATE" in COMMENTS_JS
    assert "anchor.type" in COMMENTS_JS and "anchor.key" in COMMENTS_JS


# ---------------------------------------------------------------------------
# Three lifecycle hooks must all be present.
# ---------------------------------------------------------------------------


def test_draft_save_runs_on_textarea_input() -> None:
    # Autosave fires per-keystroke against the form's textarea.
    assert "addEventListener('input', function()" in COMMENTS_JS
    assert "saveDraft(currentAnchor, draftArea.value)" in COMMENTS_JS


def test_draft_restore_runs_when_sidebar_opens() -> None:
    # openWithAnchor rehydrates the textarea from localStorage.
    assert "loadDraft(anchor)" in COMMENTS_JS
    # And user-visible signal: the hint says "Draft restored." when it actually
    # found one. (Empty draft → no hint, so we don't false-alarm.)
    assert "'Draft restored.'" in COMMENTS_JS


def test_draft_cleared_on_successful_post() -> None:
    # Clear must reference the submit-time anchor, not currentAnchor —
    # otherwise a slow response after the user navigates away clears the
    # wrong draft.
    assert "var anchorAtSubmit = currentAnchor" in COMMENTS_JS
    assert "clearDraft(anchorAtSubmit)" in COMMENTS_JS


# ---------------------------------------------------------------------------
# Server-down failure path doesn't silently lose the user's text. Originally
# (Fix 1 alone) this was guaranteed by keeping the draft in localStorage.
# With Fix 2 layered on, the outbox takes over: the catch branch enqueues
# the payload and clears the draft (the outbox is now the source of truth).
# The invariant tested here — "the user's text survives a server outage" —
# is unchanged; only the mechanism moves up a layer.
# ---------------------------------------------------------------------------


def test_server_unreachable_preserves_payload_via_outbox() -> None:
    catch_block = COMMENTS_JS.split(".catch(function(err)")[1].split("});")[0]
    # The text the user submitted is now durable via the outbox enqueue.
    assert "enqueueOutbox(payload)" in catch_block, (
        "server-down branch must enqueue the post into the outbox"
    )
    # And the user sees a "Queued" hint instead of "lost".
    assert "Queued" in catch_block


# ---------------------------------------------------------------------------
# Storage failures (quota exceeded, private-mode disabled, etc.) must not
# break the form. Each accessor wraps its localStorage call in try/catch.
# ---------------------------------------------------------------------------


def test_storage_errors_are_swallowed() -> None:
    # All three helpers (save/load/clear) must guard against the storage
    # API throwing — Safari private mode and quota-exceeded both throw
    # synchronously from setItem/getItem.
    for fn in ("function saveDraft", "function loadDraft", "function clearDraft"):
        block = COMMENTS_JS.split(fn)[1].split("\n  }")[0]
        assert "try {" in block, f"{fn} missing try/catch around localStorage"
        assert "catch (e)" in block, f"{fn} missing catch for storage errors"
