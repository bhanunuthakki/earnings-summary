"""The CCState client store contract (S14 PR2).

``CC_STATE_JS`` is a plain JS module string; these tests lock the contract
its consumers (SHELL_JS, the ask dock, the explore panel) rely on — the
namespace, the key table with its legacy migrations, the API surface, and
the panel-cache compatibility with PR1's ``cc:v1:panel:*`` entries.
"""

from __future__ import annotations

from pipeline.cc_state import CC_STATE_JS


def test_store_namespace_and_api_surface() -> None:
    """One namespaced version prefix; explicit getters/setters + JSON wraps +
    the panel-cache sub-api, all exported on window.CCState."""
    assert "var NS = 'cc:v1:';" in CC_STATE_JS
    assert "window.CCState = {" in CC_STATE_JS
    for member in ("get: get", "set: set", "del: del", "getJSON: getJSON", "setJSON: setJSON"):
        assert member in CC_STATE_JS
    assert "panel: { key: panelKey, get: panelGet, set: panelSet }" in CC_STATE_JS


def test_store_contract_comment_block() -> None:
    """The store documents its own contract in the leading comment block:
    every key with backend + writer/reader, the migration policy, and what
    is deliberately NOT in the store."""
    head = CC_STATE_JS[: CC_STATE_JS.index("var NS")]
    for key in (
        "section",
        "tab",
        "ticker",
        "askQ",
        "askViewId",
        "askThread",
        "askSessionId",
        "askTail",
        "dockMode",
        "drawer:<ep>",
        "panel:<id>",
    ):
        assert key in head
    assert "Migration:" in head
    assert "Deliberately NOT in the store" in head
    assert "ix-last-seen" in head  # the inbox marks stay out (shared with /feed)


def test_every_legacy_key_migrates() -> None:
    """The scattered pre-S14 keys all appear as legacy entries — reads fall
    back to them, copy forward, and delete; writes clear them so a stale
    legacy copy can never shadow the namespaced value."""
    for legacy in (
        "cc-ask-q",
        "cc-view-id",
        "cc-ask-thread",
        "cc-ask-session-id",
        "cc-ask-dock-tail",
        "askDockMode",
        "askDockOpen",
        "cc-drawer-sec:",
    ):
        assert legacy in CC_STATE_JS
    # Migrate-forward mechanics: namespaced write + legacy delete on read.
    assert "set(key, mapped);  // migrate forward" in CC_STATE_JS
    assert "function clearLegacy" in CC_STATE_JS
    # The pre-dock boolean maps to a mode, not a raw copy.
    assert "return v === '1' ? 'float' : 'min';" in CC_STATE_JS


def test_backends_split_session_vs_local() -> None:
    """Working state (handoffs, thread ids, tab) is session-scoped; the dock
    mode and drawer sections are cross-session preferences in localStorage."""
    assert "dockMode:     { store: 'local'" in CC_STATE_JS
    assert "{ store: 'local', legacy: [{ k: 'cc-drawer-sec:' + key.slice(7) }] }" in CC_STATE_JS
    for session_key in ("askQ:", "askTail:", "section:", "tab:", "ticker:"):
        line = next(ln for ln in CC_STATE_JS.splitlines() if ln.strip().startswith(session_key))
        assert "'session'" in line


def test_panel_cache_stays_compatible_with_pr1_entries() -> None:
    """PR1's SWR cache wrote cc:v1:panel:<id>[:T] entries; the store's panel
    sub-api addresses the same keys (entries written before PR2 stay valid)
    and keeps the quota-eviction behavior."""
    assert "var PANEL_PREFIX = NS + 'panel:';" in CC_STATE_JS
    assert "PANEL_PREFIX + pid + (ticker ? ':' + ticker : '')" in CC_STATE_JS
    assert "typeof entry.html === 'string'" in CC_STATE_JS
    # Quota pressure: evict only panel entries, retry once, give up quietly.
    assert "k.indexOf(PANEL_PREFIX) === 0" in CC_STATE_JS
    assert "sessionStorage.removeItem(k)" in CC_STATE_JS
    assert "entry.html.length > 400000" in CC_STATE_JS
