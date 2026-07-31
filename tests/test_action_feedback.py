"""The universal action-feedback contract (``window.CCAction``).

The 2026-07-30 responsiveness sweep found five divergent click-feedback
patterns across the POST-action surfaces: HTMX buttons with no busy state,
disable-only handlers, instant ``.remove()`` pop-outs, dim-in-place, and
no-feedback double-submit buttons. ``pipeline/cc_action.py`` is the ONE
primitive (busy / release / receipt / animated leave); these assertions pin
the structural contract so per-surface drift can't come back:

* the primitive is inlined into BOTH documents (live shell + offline report),
  before the panel/section scripts that call it;
* the kit styles the pressed state declaratively — ``[aria-busy="true"]`` and
  HTMX's in-flight ``.htmx-request`` on ``.k-btn`` — so declarative buttons
  need no JS for busy feedback;
* the leave motion is tokenized (``var(--transition)``), two-beat
  (fade+settle → height collapse), driven by transitionend with a timeout
  fallback, and respects ``prefers-reduced-motion``.

Runtime behavior (the actual animation) is verified out-of-band against the
real page (preview-UI memo); these source-level assertions are the CI guard.
"""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pipeline.cc_action import CC_ACTION_CSS, CC_ACTION_JS  # noqa: E402
from pipeline.command_center_shell import render_shell  # noqa: E402
from report.renderers import workspace_html  # noqa: E402
from ui.controls import controls_css  # noqa: E402

SHELL_HTML = render_shell(overview_html="x", generated_at=datetime(2026, 6, 1, tzinfo=UTC))


def test_primitive_is_inlined_in_both_documents() -> None:
    # Shell: JS before the panel fragments, CSS in the page style.
    assert CC_ACTION_JS in SHELL_HTML
    assert CC_ACTION_CSS.strip() in SHELL_HTML
    # Report: a separate document — the template must inline both explicitly.
    src = inspect.getsource(workspace_html)
    assert "{CC_ACTION_JS}" in src and "{CC_ACTION_CSS}" in src


def test_primitive_defines_the_four_verbs_once() -> None:
    assert CC_ACTION_JS.count("window.CCAction =") == 1
    for verb in ("busy:", "release:", "receipt:", "leave:"):
        assert verb in CC_ACTION_JS, f"missing verb: {verb}"
    # Idempotent under double-inlining (shell + a fragment both load it).
    assert "if (window.CCAction) return;" in CC_ACTION_JS


def test_busy_state_is_aria_and_label_roundtrip() -> None:
    # busy() marks in-flight state accessibly and stashes the label once, so
    # busy → release restores the exact original text.
    assert "setAttribute('aria-busy', 'true')" in CC_ACTION_JS
    assert "data-cc-label" in CC_ACTION_JS


def test_kit_styles_pressed_state_for_ccaction_and_htmx() -> None:
    css = controls_css("paper")
    assert '.k-btn[aria-busy="true"]' in css
    assert ".k-btn.htmx-request" in css


def test_leave_motion_is_tokenized_two_beat_and_reduced_motion_safe() -> None:
    assert "var(--transition)" in CC_ACTION_CSS
    assert "prefers-reduced-motion: reduce" in CC_ACTION_CSS
    # Beat 1 is transform+opacity; beat 2 deliberately collapses the box.
    assert "opacity" in CC_ACTION_CSS and "transform" in CC_ACTION_CSS
    assert "height: 0 !important" in CC_ACTION_CSS
    # transitionend-driven with a timeout fallback (mirrors CCOverlay), and
    # reduced-motion falls straight through to removal.
    assert "transitionend" in CC_ACTION_JS
    assert "setTimeout(next, 240)" in CC_ACTION_JS
    assert "reduceMotion()" in CC_ACTION_JS
