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

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pipeline.ask_dock import _DOCK_JS, render_ask_dock  # noqa: E402
from pipeline.cc_overlay import CC_OVERLAY_CSS, CC_OVERLAY_JS  # noqa: E402
from pipeline.command_center_shell import SHELL_JS, render_shell  # noqa: E402

SHELL_HTML = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))
DOCK_HTML = render_ask_dock()


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
# The shell: Escape delegated to CCOverlay; no per-surface scrims/listeners
# ---------------------------------------------------------------------------


def test_shell_delegates_escape_to_the_primitive() -> None:
    # The shell's own script carries no Escape handler — CCOverlay owns it.
    assert "ev.key === 'Escape'" not in SHELL_JS
    # CCOverlay is inlined before the shell + dock scripts.
    assert CC_OVERLAY_JS in SHELL_HTML


def test_shell_has_no_per_surface_scrim_elements_or_listeners() -> None:
    for stale in ("cc-drawer-scrim", "cc-palette-scrim", "cc-peek-scrim", "cc-notes-scrim"):
        assert stale not in SHELL_HTML, f"stale per-surface scrim: {stale}"
    assert "scrim.addEventListener('click'" not in SHELL_JS  # only the primitive's


# Every modal surface CCOverlay registers in the shell + dock, with the id of
# its close control (the triad's x).
_MODAL_SURFACES = {
    "cc-drawer-close": SHELL_HTML,
    "cc-notes-close": SHELL_HTML,
    "cc-palette-close": SHELL_HTML,
    "cc-peek-close": SHELL_HTML,
    "ask-dock-close": DOCK_HTML,
    "ask-dock-threads-close": DOCK_HTML,
}


def test_every_registered_modal_surface_has_a_close_control_id() -> None:
    js = SHELL_JS + _DOCK_JS
    for close_id, html in _MODAL_SURFACES.items():
        # Registered with this closeId …
        assert f"closeId: '{close_id}'" in js, f"no register() declares closeId {close_id}"
        # … and the element actually exists in the markup.
        assert f'id="{close_id}"' in html, f"close-control element {close_id} missing from markup"


def test_shell_surfaces_register_with_the_priority_ladder() -> None:
    assert "window.CCOverlay.PRIORITY.PALETTE" in SHELL_JS
    assert "window.CCOverlay.PRIORITY.PEEK" in SHELL_JS
    assert "window.CCOverlay.PRIORITY.DRAWER" in SHELL_JS


# ---------------------------------------------------------------------------
# The Ask dock: no second keydown, no hardcoded id registry
# ---------------------------------------------------------------------------


def test_dock_has_no_hardcoded_overlay_id_registry() -> None:
    assert "overlays = [" not in DOCK_HTML
    assert "'cc-palette', 'cc-peek'" not in DOCK_HTML  # the exact stale list is gone


def test_dock_has_no_second_keydown_listener() -> None:
    # The split-exit keydown is gone; the only remaining keydown in the dock is
    # the inline-rename input's (a scoped control, not document-level).
    assert _DOCK_JS.count("document.addEventListener('keydown'") == 0


def test_dock_registers_as_a_scrimless_gesture_surface() -> None:
    assert "window.CCOverlay.PRIORITY.DOCK" in _DOCK_JS
    # Declared scrim:false (a page-darkening scrim would defeat a side-by-side
    # copilot — the same carve-out as the report comments sidebar).
    assert "scrim: false" in _DOCK_JS
    # Persistent surface: never toggled [hidden] by the primitive.
    assert "toggleHidden: false" in _DOCK_JS
