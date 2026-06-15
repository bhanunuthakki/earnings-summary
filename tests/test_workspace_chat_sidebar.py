"""The in-report chat panel is a push-sidebar (like the comments sidebar),
not a floating overlay, and the two are mutually exclusive (one open at a
time). Tests the render-helper markup + the inlined CSS/JS contract that
wires the push + the one-open-at-a-time handoff, matching the repo's
render-helper test convention (see test_renderer_forgone_budget.py).
"""

from __future__ import annotations

from io import StringIO

from report.renderers import workspace_html as ws_r
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import JS as COMMENTS_JS

# ---------------------------------------------------------------------------
# Markup — the launcher and the panel are split: a fixed .chat-drawer holding
# only the toggle, and a .chat-sidebar that is a flex sibling of .l1-root.
# ---------------------------------------------------------------------------


def test_chat_shell_emits_push_sidebar_plus_launcher() -> None:
    body = StringIO()
    ws_r._chat_drawer_shell(body, "NU", "2026-06-01")
    s = body.getvalue()
    # Push-sidebar panel (flex sibling of .l1-root, mirrors .cmt-sidebar).
    assert '<aside class="chat-sidebar" id="chat-sidebar"' in s
    # Fixed launcher pill, now toggle-only, with a stable id for the JS. The
    # button keeps its .chat-toggle JS hook and rides the kit primary alongside.
    assert '<aside class="chat-drawer" id="chat-drawer"' in s
    assert "chat-toggle" in s and 'id="chat-toggle"' in s
    assert "k-btn k-btn-primary" in s  # composes the control kit
    # Thread + form still live inside the (now push-sidebar) panel.
    assert 'id="chat-thread"' in s and 'id="chat-form"' in s
    # The old floating overlay panel is gone.
    assert "chat-panel" not in s


# ---------------------------------------------------------------------------
# CSS — .chat-sidebar is a zero-width flex sibling that expands on .open,
# the same push mechanism the comments sidebar uses. No floating overlay.
# ---------------------------------------------------------------------------


def test_chat_css_is_a_push_sidebar() -> None:
    assert ".chat-sidebar {" in CHAT_CSS
    assert ".chat-sidebar.open" in CHAT_CSS
    # Collapsed → expanded width is the push.
    assert "width: 0" in CHAT_CSS and "width: 460px" in CHAT_CSS
    assert "flex-shrink: 0" in CHAT_CSS
    # No leftover fixed-overlay panel rules.
    assert ".chat-panel" not in CHAT_CSS


# ---------------------------------------------------------------------------
# JS — one-open-at-a-time. Since S4 PR3 this is the CCOverlay open-surface
# stack: both sidebars register in the 'report-sidebar' group (opening one
# closes the other), replacing the cross-document window.__close* handshake.
# Both still drive the shared --sidebar-open-width that pushes the document.
# ---------------------------------------------------------------------------


def test_chat_js_enforces_mutual_exclusivity_and_push() -> None:
    # Mutual exclusivity is the shared group, not a cross-document global.
    assert "group: 'report-sidebar'" in CHAT_JS
    assert "window.__closeCommentSidebar" not in CHAT_JS
    assert "window.__closeChatSidebar =" not in CHAT_JS
    # Chat drives the same push variable the comments sidebar uses.
    assert "--sidebar-open-width" in CHAT_JS
    # No stale references to the removed floating panel element.
    assert "var panel" not in CHAT_JS


def test_comments_js_closes_chat_when_a_comment_opens() -> None:
    # Reverse direction: the same 'report-sidebar' group closes chat when a
    # comment opens — no window.__close* global on either side.
    assert "group: 'report-sidebar'" in COMMENTS_JS
    assert "window.__closeChatSidebar" not in COMMENTS_JS
    assert "window.__closeCommentSidebar = closeSidebar" not in COMMENTS_JS
    # Selecting text inside the chat sidebar must not pop the comment floater.
    assert "contains('chat-sidebar')" in COMMENTS_JS
