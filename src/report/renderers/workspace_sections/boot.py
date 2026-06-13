"""Boot payloads + shells: comment boot JSON, comment sidebar, chat drawer.

Split out of ``workspace_html.py`` (S13 renderer modularization). The
public entry point and output contract live in ``workspace_html``;
names here keep their original (underscore) spellings and are exported
via ``__all__`` for the package-internal imports and the back-compat
re-exports in ``workspace_html``."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from report.models import ReportSpec
from report.renderers.workspace_sections._shared import _esc

__all__ = [
    "_chat_drawer_shell",
    "_comment_boot_data",
    "_comment_sidebar_shell",
]


def _comment_boot_data(body: StringIO, spec: ReportSpec) -> None:
    """Embed `<script type="application/json">` blocks the JS modules pick up:
    - workspace-boot: ticker, report_date, server URL (default localhost:7421)
    - workspace-comments: existing comments for this (ticker, date) so pins
      render on first paint without a server fetch.

    No server connection required for read-only display (pins + side panel).
    POSTing new comments + chat needs the server (`python execution/comments_server.py`)."""
    import json as _json

    from comments import load_store, to_json_payload

    boot = {
        "ticker": spec.ticker,
        "report_date": spec.generation_date.isoformat(),
        "server_url": "http://localhost:7421",
    }
    body.write(f'<script id="workspace-boot" type="application/json">{_json.dumps(boot)}</script>')
    payload: dict[str, object]
    try:
        store = load_store(Path(spec.repo_root), spec.ticker, spec.generation_date)
        payload = to_json_payload(store)
    except Exception:
        payload = {
            "ticker": spec.ticker,
            "report_date": spec.generation_date.isoformat(),
            "comments": [],
        }
    body.write(
        f'<script id="workspace-comments" type="application/json">{_json.dumps(payload)}</script>'
    )


def _comment_sidebar_shell(body: StringIO) -> None:
    """Static sidebar shell — flex sibling of .l1-root.

    The push-sidebar layout requires this `<aside>` to be a direct child
    of `<body>` (which is `display: flex; flex-direction: row`). Emitting
    it at render time makes that relationship explicit in the markup;
    the JS only toggles `.open` and writes into `#cmt-list` /
    `#cmt-anchor-label`. No outside-click dismissal — close via the ×
    button or Escape.
    """
    body.write(
        '<aside class="cmt-sidebar" id="cmt-sidebar" aria-hidden="true">'
        '<header class="cmt-sidebar-head">'
        "<div>"
        '<div class="cmt-sidebar-title">Comments</div>'
        '<div class="cmt-sidebar-sub" id="cmt-anchor-label"></div>'
        "</div>"
        '<button class="cmt-close" id="cmt-close" type="button" aria-label="close">&times;</button>'
        "</header>"
        '<div class="cmt-list" id="cmt-list"></div>'
        '<form class="cmt-form" id="cmt-form">'
        '<textarea name="comment" rows="3" required '
        'placeholder="Write a comment&hellip; '
        "(tip: prefix with /kpi /thesis /q /ask /fix /update /rewrite /peers "
        'to skip auto-classify)"></textarea>'
        '<div class="cmt-form-row">'
        '<select name="intent" title="What should the processor do?">'
        '<option value="">Auto-classify</option>'
        '<option value="drop_kpi">Drop this KPI</option>'
        '<option value="edit_thesis">Edit thesis</option>'
        '<option value="curate_peers">Curate peers</option>'
        '<option value="ask_question">Ask question</option>'
        '<option value="fix_data">Flag data issue</option>'
        '<option value="rewrite_section">Rewrite this section</option>'
        "</select>"
        '<button type="submit">Post</button>'
        "</div>"
        # P4.5 "add note" capture: saves the text above straight into the
        # analyst journal (analyst_notes), anchored to this section — no
        # comment processor involved. Wired in workspace_comments.JS.
        '<div class="cmt-form-row cmt-note-row">'
        '<select name="note_kind" title="Journal note kind">'
        '<option value="watch">Watch item</option>'
        '<option value="question">Question</option>'
        '<option value="observation">Observation</option>'
        '<option value="assumption">Assumption</option>'
        '<option value="decision">Decision</option>'
        "</select>"
        '<button type="button" id="cmt-save-note" '
        'title="Save the text above as a durable journal note">Save to journal</button>'
        "</div>"
        '<div class="cmt-form-hint" id="cmt-form-hint"></div>'
        "</form>"
        "</aside>"
    )


def _chat_drawer_shell(body: StringIO, ticker: str, report_date: str) -> None:
    """Chat shell — a push-sidebar (`.chat-sidebar`) plus a fixed launcher
    (`.chat-drawer`).

    The panel is a flex sibling of `.l1-root`, mirroring
    `_comment_sidebar_shell`: opening it slides the document aside rather
    than floating an overlay. The chat JS toggles `.open`, sets
    `--sidebar-open-width`, and enforces one-open-at-a-time with the
    comments sidebar. The launcher pill stays `position: fixed` and rides
    the open sidebar's left edge.
    """
    body.write(
        '<aside class="chat-sidebar" id="chat-sidebar" aria-hidden="true">'
        '<header class="chat-head">'
        "<div>"
        f'<div class="chat-title">Ask Claude about {_esc(ticker)}</div>'
        f'<div class="chat-sub">{_esc(report_date)} '
        "&middot; streams from comments_server &middot; "
        # P2.4 entry point: stances live behind the Socratic flow, never in chat.
        f'<a href="http://localhost:7421/socratic/{_esc(ticker)}" target="_blank" '
        'rel="noopener">think it through &rarr;</a></div>'
        "</div>"
        '<button class="chat-close" id="chat-close" type="button" aria-label="close">&times;</button>'
        "</header>"
        '<div class="chat-thread" id="chat-thread"></div>'
        '<form class="chat-form" id="chat-form">'
        '<textarea name="message" rows="3" required '
        'placeholder="Ask about a KPI, propose an edit, '
        'look up a quote in the transcript&hellip;"></textarea>'
        '<div class="chat-form-row">'
        '<span class="chat-hint" id="chat-hint">Cmd+Enter to send</span>'
        '<button type="submit">Send</button>'
        "</div>"
        "</form>"
        "</aside>"
        '<aside class="chat-drawer" id="chat-drawer">'
        '<button class="chat-toggle" id="chat-toggle" type="button" aria-label="Open chat">'
        '<span class="chat-toggle-icon">&#8984;</span>'
        '<span class="chat-toggle-label">Chat</span>'
        "</button>"
        "</aside>"
    )
