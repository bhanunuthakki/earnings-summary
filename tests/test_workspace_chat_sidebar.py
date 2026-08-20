"""The report exposes one compact handoff to the durable Work OS Copilot."""

from __future__ import annotations

from io import StringIO

from report.renderers import workspace_html as ws_r
from report.renderers.workspace_chat import CSS as CHAT_CSS
from report.renderers.workspace_chat import JS as CHAT_JS
from report.renderers.workspace_comments import CSS as COMMENTS_CSS
from report.renderers.workspace_comments import JS as COMMENTS_JS

# ---------------------------------------------------------------------------
# Markup — the launcher and the panel are split: a fixed .chat-drawer holding
# only the toggle, and a .chat-sidebar that is a flex sibling of .l1-root.
# ---------------------------------------------------------------------------


def test_chat_shell_emits_copilot_handoff_without_a_second_composer() -> None:
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
    assert 'id="chat-open-copilot"' in s
    assert "Open in Copilot" in s
    assert 'id="chat-thread"' not in s
    assert 'id="chat-form"' not in s
    assert "textarea" not in s
    assert 'type="submit"' not in s
    assert "chat-panel" not in s


# ---------------------------------------------------------------------------
# CSS — .chat-sidebar is a zero-width flex sibling that expands on .open,
# the same push mechanism the comments sidebar uses. No floating overlay.
# ---------------------------------------------------------------------------


def test_chat_css_is_a_push_sidebar() -> None:
    assert ".chat-sidebar {" in CHAT_CSS
    assert ".chat-sidebar.open" in CHAT_CSS
    # Collapsed → expanded width is the push.
    assert "width: 0" in CHAT_CSS and "width: var(--sidebar-width)" in CHAT_CSS
    assert "flex-shrink: 0" in CHAT_CSS
    # No leftover fixed-overlay panel rules.
    assert ".chat-panel" not in CHAT_CSS
    assert ".chat-form" not in CHAT_CSS
    assert ".chat-diff-actions" not in CHAT_CSS


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
    # JS owns state; the master stylesheet maps that state to the shared push width.
    assert "classList.toggle('chat-sidebar-open', open)" in CHAT_JS
    assert ":root.chat-sidebar-open { --sidebar-open-width:" in COMMENTS_CSS
    # No stale references to the removed floating panel element.
    assert "var panel" not in CHAT_JS


def test_report_chat_hands_off_context_without_legacy_chat_or_apply_calls() -> None:
    assert "function openDurableCopilot(context)" in CHAT_JS
    assert "window.parent.openWorkOsCopilot" in CHAT_JS
    assert "company_ticker: TICKER" in CHAT_JS
    assert "report_date: REPORT_DATE" in CHAT_JS
    assert "category: 'research'" in CHAT_JS
    assert "fact_ref: factRef" in CHAT_JS
    assert "Open in Copilot" in CHAT_JS
    assert "copilot: '1'" in CHAT_JS
    assert "fetch(SERVER_URL + '/chat/'" not in CHAT_JS
    assert "'/apply'" not in CHAT_JS
    assert "requestSubmit()" not in CHAT_JS
    assert "diff_proposal" not in CHAT_JS


def test_comments_js_closes_chat_when_a_comment_opens() -> None:
    # Reverse direction: the same 'report-sidebar' group closes chat when a
    # comment opens — no window.__close* global on either side.
    assert "group: 'report-sidebar'" in COMMENTS_JS
    assert "window.__closeChatSidebar" not in COMMENTS_JS
    assert "window.__closeCommentSidebar = closeSidebar" not in COMMENTS_JS
    # Selecting text inside the chat sidebar must not pop the comment floater.
    assert "contains('chat-sidebar')" in COMMENTS_JS
