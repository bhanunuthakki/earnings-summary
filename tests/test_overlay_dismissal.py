"""The universal surface-dismissal contract (directive Law 3, design_language §3).

S4 replaced five hand-rolled ``close*`` functions, an enumerated Escape
``switch``, a Tab trap, a second dock ``keydown`` + hardcoded id registry, and a
cross-document ``window.__close*`` handshake with ONE primitive — ``CCOverlay``
(``pipeline/cc_overlay.py``). These tests pin the structural contract so the
class of regression (a surface re-deriving its own dismissal, a stale id list, a
recency-ordered Escape) can't come back:

* every registered MODAL surface declares a close-control id that exists in the
  markup (the triad's close (x) is guaranteed, not per-surface);
* each document emits exactly ONE Escape handler and ONE scrim-click listener —
  the shell and the dock here; the report iframe is added by S4 PR3;
* the Ask dock carries no hardcoded overlay-id registry;
* Escape resolves by stored PRIORITY (palette > peek > drawer > dock), with
  recency only a same-priority tie-break — NOT "top of the recency stack".

The runtime behavior (open/close, scrim z-index, group exclusion, focus
trap/restore, priority-not-recency, popover-dismisser ordering) is verified
out-of-band via preview_eval against the real ``CC_OVERLAY_JS`` (the preview-UI
memo); these source-level assertions are the CI guard.
"""

from __future__ import annotations

import inspect
import re
import sys
from io import StringIO
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pipeline.cc_overlay import CC_OVERLAY_CSS, CC_OVERLAY_JS  # noqa: E402
from report.renderers import workspace_html  # noqa: E402
from report.renderers.workspace_chat import JS as CHAT_JS  # noqa: E402
from report.renderers.workspace_comments import JS as COMMENTS_JS  # noqa: E402
from report.renderers.workspace_script import JS as REPORT_JS  # noqa: E402
from report.renderers.workspace_sections.boot import (  # noqa: E402
    _chat_drawer_shell,
    _comment_sidebar_shell,
)
from ui.cite_marks import CITE_MARKS_CSS, CITE_MARKS_JS  # noqa: E402
from ui.source_chip import SOURCE_CHIP_JS  # noqa: E402

# ---------------------------------------------------------------------------
# The primitive: one keydown, one scrim listener, priority resolution
# ---------------------------------------------------------------------------


def test_primitive_has_exactly_one_keydown_and_one_scrim_listener() -> None:
    assert CC_OVERLAY_JS.count("document.addEventListener('keydown'") == 1
    assert CC_OVERLAY_JS.count("scrim.addEventListener('click'") == 1


def test_escape_resolves_by_priority_not_recency() -> None:
    # The resolver picks the higher PRIORITY; recency (seq) only breaks a tie
    # between equal priorities — never the primary key.
    assert "s.opts.priority > best.opts.priority" in CC_OVERLAY_JS
    assert "s.opts.priority === best.opts.priority && s.seq > best.seq" in CC_OVERLAY_JS
    # And it does NOT just grab the most-recently-opened surface.
    assert "surfaces[surfaces.length - 1]" not in CC_OVERLAY_JS


def test_priority_ladder_is_strictly_descending() -> None:
    m = re.search(
        r"PRIORITY:\s*\{\s*DOCK:\s*(\d+),\s*DRAWER:\s*(\d+),\s*PEEK:\s*(\d+),\s*PALETTE:\s*(\d+)",
        CC_OVERLAY_JS,
    )
    assert m, "PRIORITY ladder not found in CC_OVERLAY_JS"
    dock, drawer, peek, palette = (int(g) for g in m.groups())
    assert dock < drawer < peek < palette, (dock, drawer, peek, palette)


def test_popover_dismissers_run_before_modal_surfaces() -> None:
    # In the keydown handler the dismisser loop must precede the topModalSurface
    # resolution, so non-modal popovers (cite-marks/source-chip/hover) claim
    # Escape first.
    body = CC_OVERLAY_JS.split("if (ev.key === 'Escape')", 1)[1]
    dismiss_at = body.find("dismissers[i]()")
    modal_at = body.find("topModalSurface()")
    assert 0 <= dismiss_at < modal_at
    assert "addPopoverDismisser" in CC_OVERLAY_JS


def test_primitive_provides_focus_trap_and_restore() -> None:
    assert "ev.key === 'Tab'" in CC_OVERLAY_JS
    assert "focusableIn" in CC_OVERLAY_JS
    assert "s.opener" in CC_OVERLAY_JS  # restoreFocus path


# ---------------------------------------------------------------------------
# Close motion (the "delightful" deliverable): tokenized, transform/opacity only
# ---------------------------------------------------------------------------


def test_close_motion_is_tokenized_transform_opacity_only() -> None:
    assert "var(--transition)" in CC_OVERLAY_CSS
    assert "prefers-reduced-motion: reduce" in CC_OVERLAY_CSS
    # Animate ONLY transform + opacity — never layout properties.
    for banned in ("width:", "height:", "left:", "top:", "margin", "padding"):
        assert banned not in CC_OVERLAY_CSS, f"close motion touches layout prop: {banned}"
    assert "transform" in CC_OVERLAY_CSS and "opacity" in CC_OVERLAY_CSS


# ---------------------------------------------------------------------------
# Non-modal phrasing popovers: Escape-only, NOT the full modal triad
# ---------------------------------------------------------------------------


def test_cite_mark_popover_is_escape_only_dismisser_not_modal() -> None:
    # Registers a dismisser (Escape-only) — never register()s as a modal, never
    # gains a scrim or focus trap (its :focus-within popover is phrasing content
    # that would split the paragraph as a <details>).
    assert "addPopoverDismisser" in CITE_MARKS_JS
    assert ".cite-wrap" in CITE_MARKS_JS
    assert "CCOverlay.register" not in CITE_MARKS_JS
    assert "k-scrim" not in CITE_MARKS_JS and "k-scrim" not in CITE_MARKS_CSS


def test_source_chip_popover_is_escape_only_dismisser_not_modal() -> None:
    assert "addPopoverDismisser" in SOURCE_CHIP_JS
    assert "details.src-pop[open]" in SOURCE_CHIP_JS
    assert "CCOverlay.register" not in SOURCE_CHIP_JS
    # Self-guarded so the many surfaces embedding the chip register it once.
    assert "__ccSrcChipEsc" in SOURCE_CHIP_JS


# ---------------------------------------------------------------------------
# The report iframe (a SEPARATE document): same ONE-Escape / ONE-scrim contract
# ---------------------------------------------------------------------------

# Everything the report document inlines, in load order. The iframe re-inlines
# the primitive (it can't import controls_css across the document boundary).
_IFRAME_SCRIPTS = (CC_OVERLAY_JS, SOURCE_CHIP_JS, COMMENTS_JS, CHAT_JS, REPORT_JS)


def test_iframe_emits_exactly_one_escape_and_one_scrim_listener() -> None:
    escape = sum(js.count("ev.key === 'Escape'") for js in _IFRAME_SCRIPTS)
    scrim = sum(js.count("scrim.addEventListener('click'") for js in _IFRAME_SCRIPTS)
    assert escape == 1, f"iframe has {escape} Escape handlers (want 1: CCOverlay)"
    assert scrim == 1, f"iframe has {scrim} scrim listeners (want 1: CCOverlay)"
    # CCOverlay + the source-chip dismisser are inlined into the report document.
    src = inspect.getsource(workspace_html)
    assert "{CC_OVERLAY_JS}" in src and "{SOURCE_CHIP_JS}" in src


def test_iframe_sidebars_drop_the_cross_document_globals() -> None:
    # The window.__close* handshake is gone — exclusivity is the stack's job.
    assert "window.__closeChatSidebar =" not in CHAT_JS
    assert "window.__closeCommentSidebar =" not in COMMENTS_JS
    assert "window.__closeCommentSidebar()" not in CHAT_JS
    assert "window.__closeChatSidebar()" not in COMMENTS_JS


def test_iframe_sidebars_register_grouped_and_scrimless() -> None:
    for js in (CHAT_JS, COMMENTS_JS):
        assert "CCOverlay" in js and "register" in js
        assert "group: 'report-sidebar'" in js  # one-open-at-a-time via the stack
        assert "scrim: false" in js  # push-sidebar gesture surface, declared
        assert "toggleHidden: false" in js  # visibility is its own .open class


def test_comments_sidebar_keeps_its_deliberate_no_click_out() -> None:
    # PRESERVED: the comments sidebar's no-outside-click is load-bearing (an
    # outside-click raced the floater's mousedown-open) — scrim:false is a
    # DECLARED option, and its floater is Escape-only via a dismisser, not a
    # second keydown.
    assert "scrim: false" in COMMENTS_JS
    assert "addPopoverDismisser" in COMMENTS_JS
    assert COMMENTS_JS.count("document.addEventListener('keydown'") == 0


def test_iframe_close_controls_exist_in_the_shells() -> None:
    chat = StringIO()
    _chat_drawer_shell(chat, "NU", "2026-06-01")
    cmt = StringIO()
    _comment_sidebar_shell(cmt)
    assert 'id="chat-close"' in chat.getvalue()
    assert "closeId: 'chat-close'" in CHAT_JS
    assert 'id="cmt-close"' in cmt.getvalue()
    assert "closeId: 'cmt-close'" in COMMENTS_JS
