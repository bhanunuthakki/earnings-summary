"""Server-health pill: polls /healthz on a timer + on focus and exposes
the state via a green/red pill in the sidebar header plus an offline
banner above the textarea. Replaces the silent "find out at submit
time" failure mode with eyes-open status before the user clicks Post.

Wires into Fix 2 (#308): on the offline → online edge, kicks
`window.__flushOutbox` immediately so the queue drains without waiting
for its own 15s tick.

Pure render-helper test — string-assertion pattern, same as siblings
(test_workspace_comments_drafts.py, test_workspace_comments_outbox.py,
test_workspace_chat_sidebar.py).
"""

from __future__ import annotations

from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS

# ---------------------------------------------------------------------------
# State machine — three states, transitions trigger UI updates.
# ---------------------------------------------------------------------------


def test_three_health_states() -> None:
    # 'unknown' is the initial state (before the first poll resolves);
    # explicit so a slow first GET doesn't show false-green/red.
    assert "'unknown'" in COMMENTS_JS
    assert "'online'" in COMMENTS_JS
    assert "'offline'" in COMMENTS_JS


def test_setHealthState_short_circuits_on_no_change() -> None:
    # Without this guard, every 10s poll re-renders the DOM even when
    # nothing changed — wasteful and triggers an unnecessary edge check.
    block = COMMENTS_JS.split("function setHealthState")[1].split("\n  }")[0]
    assert "if (next === prev) return" in block


def test_offline_to_online_edge_kicks_flush() -> None:
    # The whole point of the pill being in the same module as the
    # outbox: recovery → drain, not wait-for-tick.
    block = COMMENTS_JS.split("function setHealthState")[1].split("\n  }")[0]
    assert "prev === 'offline'" in block
    assert "next === 'online'" in block
    assert "window.__flushOutbox" in block


# ---------------------------------------------------------------------------
# Polling — interval + focus + initial poll, cache-busted, timeout-bounded.
# ---------------------------------------------------------------------------


def test_health_polled_on_interval_and_focus() -> None:
    assert "setInterval(pollHealth" in COMMENTS_JS
    assert "addEventListener('focus', pollHealth)" in COMMENTS_JS


def test_first_poll_fires_on_init() -> None:
    # Without an init-time poll the user sees "○ …" for 10s when they
    # open the sidebar. Explicit immediate poll in the inject block.
    init_block = COMMENTS_JS.split("if (typeof pollHealth === 'function')")[1].split("\n")[0]
    assert "pollHealth()" in init_block


def test_healthz_fetch_is_cache_busted_and_bounded() -> None:
    # cache:no-store so an HTTP intermediary doesn't mask a dead server
    # with a stale 200. AbortController prevents a hung socket from
    # blocking the next tick forever.
    fn = COMMENTS_JS.split("function pollHealth")[1].split("\n  }")[0]
    assert "cache: 'no-store'" in fn
    assert "AbortController" in fn
    assert "ctrl.abort()" in fn


def test_non_ok_response_is_treated_as_offline() -> None:
    # A 200 means the route hit; anything else (5xx, 404, network err)
    # is "not reliably up".
    fn = COMMENTS_JS.split("function pollHealth")[1].split("\n  }")[0]
    assert "r.ok ? 'online' : 'offline'" in fn


# ---------------------------------------------------------------------------
# DOM — pill in the header, banner above the textarea.
# ---------------------------------------------------------------------------


def test_pill_and_banner_are_injected_on_sidebar_init() -> None:
    assert "id = 'cmt-health-pill'" in COMMENTS_JS
    assert "id = 'cmt-offline-banner'" in COMMENTS_JS
    # Pill goes before the close button so it sits at the right edge of
    # the header content, not after the close glyph.
    assert "head.insertBefore(pill, closeBtn)" in COMMENTS_JS
    # Banner goes at the top of the form so it sits above the textarea,
    # not at the bottom.
    assert "formEl.insertBefore(banner, formEl.firstChild)" in COMMENTS_JS


def test_render_functions_drive_dom_from_state() -> None:
    pill_fn = COMMENTS_JS.split("function renderHealthPill")[1].split("\n  }")[0]
    # The class encodes state so CSS can color the pill without JS poking
    # individual style props. The exact assignment is
    # "cmt-health-pill cmt-health-<state>" — assert the state-suffix
    # piece so this doesn't trip on the base-class prefix changing.
    assert "cmt-health-' + healthState" in pill_fn
    # Three textContent variants for the three states.
    assert "'● Online'" in pill_fn
    assert "'● Offline'" in pill_fn
    assert "'○ …'" in pill_fn

    banner_fn = COMMENTS_JS.split("function renderOfflineBanner")[1].split("\n  }")[0]
    assert "healthState === 'offline' ? 'block' : 'none'" in banner_fn


# ---------------------------------------------------------------------------
# CSS — three state colors plus the offline banner.
# ---------------------------------------------------------------------------


def test_pill_css_has_distinct_state_colors() -> None:
    assert ".cmt-health-pill {" in COMMENTS_CSS
    assert ".cmt-health-pill.cmt-health-online" in COMMENTS_CSS
    assert ".cmt-health-pill.cmt-health-offline" in COMMENTS_CSS
    # Green for online, red for offline — semantic, not arbitrary.
    online = COMMENTS_CSS.split(".cmt-health-pill.cmt-health-online")[1].split("}")[0]
    offline = COMMENTS_CSS.split(".cmt-health-pill.cmt-health-offline")[1].split("}")[0]
    assert "#3cc878" in online or "rgba(60, 200, 120" in online
    assert "#ff7070" in offline or "rgba(255, 80, 80" in offline


def test_offline_banner_css_exists() -> None:
    assert ".cmt-offline-banner {" in COMMENTS_CSS


# ---------------------------------------------------------------------------
# Hook for tests / manual debug.
# ---------------------------------------------------------------------------


def test_pollHealth_exposed_for_debug() -> None:
    assert "window.__pollCommentHealth = pollHealth" in COMMENTS_JS


def test_pollHealth_returns_its_promise() -> None:
    # Without `return fetch(...)`, awaiting __pollCommentHealth is a no-op
    # — caller's `await` resolves before the state actually settles. Found
    # the hard way during Fix 3 live verification.
    fn = COMMENTS_JS.split("function pollHealth")[1].split("\n  }")[0]
    assert "return fetch(" in fn
