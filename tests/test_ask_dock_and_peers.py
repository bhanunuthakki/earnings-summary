"""Ask dock surfaces (Ask v5): the persistent shell-chrome dock (three
states, split-view reflow, pop-out handoff contract) and the
/api/peers/<ticker> route (the PR #400 scored comparable set the
"+ Peers" actions inject)."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from pipeline.ask_dock import render_ask_dock
from pipeline.command_center_shell import SHELL_CSS, render_overview_panel, render_shell

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import comments_server  # noqa: E402

# ----------------------------------------------------------------------------
# The persistent Ask dock
# ----------------------------------------------------------------------------


def test_dock_fragment_is_self_contained() -> None:
    html = render_ask_dock()
    for dom_id in (
        "ask-dock",
        "ask-dock-toggle",
        "ask-dock-thread",
        "ask-dock-form",
        "ask-dock-q",
        "ask-dock-min",
        "ask-dock-split",
        "ask-dock-pop",
    ):
        assert f'id="{dom_id}"' in html
    # Same engine as the Ask tab, minimized (the slim pill) by default.
    assert "/api/ask/stream" in html
    assert 'data-mode="min"' in html
    # It renders the full event vocabulary: stages, prose, views, citations.
    for marker in ("'stage'", "'delta'", "'fragment'", "'final'", "'citations'", "'error'"):
        assert marker in html


def test_dock_renders_inline_cite_marks_with_claims() -> None:
    """Grounded answers get the shared inline cite chips (S8): the fragment
    embeds ui.cite_marks (self-guarded global + popover CSS), upgrades the
    finished prose through it, captures the event's claims, and surfaces the
    unverified-claims chip in the cite row."""
    html = render_ask_dock()
    # The shared helper rides in the fragment — popover anatomy included.
    assert "window.ccCiteMarks" in html
    assert ".cite-pop" in html
    assert "confidence " in html  # the popover's S2 confidence % line
    # The finish path linkifies prose and threads claims into the cite row.
    assert "linkifyProse(md(text), citations)" in html
    assert "claims = ev2.claims || []" in html
    assert "unverifiedChipHtml" in html


def test_dock_popout_hands_thread_to_the_ask_tab() -> None:
    """The ⇗ pop-out uses the palette's handoff contract via the shared
    store (S14 PR2): thread under askThread, pending input under askQ, jump
    to #explore + event (the EVENT name stays 'cc-ask-q') — and the dock
    minimizes so the thread doesn't render twice beside the Ask panel."""
    html = render_ask_dock()
    assert "CCState.setJSON('askThread', history)" in html
    assert "CCState.set('askQ', pending)" in html
    assert "#explore" in html
    assert "new Event('cc-ask-q')" in html
    assert "setMode('min', true)" in html
    # The dock holds NO raw storage calls — every key rides window.CCState.
    assert "sessionStorage" not in html
    assert "localStorage" not in html


def test_dock_three_states_and_persistence() -> None:
    """Ask v5: min / float / split with explicit header controls. The mode
    and the thread tail persist through the shared store (dockMode local,
    askTail/askSessionId session; legacy keys migrate inside cc_state)."""
    html = render_ask_dock()
    # Split view: the dock-side CSS hook + the body attribute that reflows
    # the shell panels beside the column over the standard transition.
    assert '.ask-dock[data-mode="split"]' in html
    assert 'body[data-ask-split="1"] .cc-panels' in html
    assert "transition: margin-right var(--transition)" in html
    assert "data-ask-split" in html
    # State + thread persistence: store keys, not raw storage names.
    assert "CCState.set('dockMode', mode)" in html
    assert "var TAIL_KEY = 'askTail'" in html
    assert "var SID_KEY = 'askSessionId'" in html
    # Esc exits split, deferring to the shell's overlays first.
    assert "'Escape'" in html
    for overlay in ("cc-palette", "cc-peek", "cc-notes-drawer", "cc-drawer"):
        assert overlay in html


def _block_z_index(css: str, selector: str) -> int:
    m = re.search(re.escape(selector) + r" \{[^}]*z-index:\s*(\d+)", css)
    assert m is not None, f"no z-index found for {selector}"
    return int(m.group(1))


def test_dock_stacks_under_shell_overlays() -> None:
    """The dock sits above the panels but BELOW every shell overlay — the
    drawers (Settings/Notes), peek, and palette all cover it, so split mode
    never fights them."""
    dock_z = _block_z_index(render_ask_dock(), ".ask-dock")
    drawer_scrim_z = _block_z_index(SHELL_CSS, ".cc-drawer-scrim")
    palette_z = _block_z_index(SHELL_CSS, ".cc-palette")
    assert dock_z < drawer_scrim_z < palette_z


def test_dock_is_shell_chrome_not_home_panel_content() -> None:
    """Ask v5: the dock renders ONCE in the shell body, outside the
    panel-swap container, so it exists on every tab — the Home panel
    fragment no longer carries it (it used to vanish with the panel)."""
    for overview in (
        render_overview_panel({}, None),
        render_overview_panel({}, None, inbox_html="<div>inbox</div>"),
    ):
        assert 'id="ask-dock"' not in overview
    # The rail layout itself is unchanged.
    assert "cc-home-grid" in render_overview_panel({}, None, inbox_html="<div>inbox</div>")
    shell = render_shell(overview_html="<p>ov</p>", generated_at=datetime(2026, 6, 11, tzinfo=UTC))
    assert shell.count('id="ask-dock"') == 1
    assert shell.index('id="ask-dock"') > shell.index("</main>")


# ----------------------------------------------------------------------------
# /api/peers/<ticker>
# ----------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    (tmp_path / "data").mkdir()
    return comments_server.create_app(tmp_path).test_client()


def test_peers_api_returns_scored_set(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from report.sections import p3_data

    def fake_load(ticker: str, *, repo_root: Path, max_peers: int = 6) -> list[object]:
        assert ticker == "NU"
        return [
            p3_data.PeerCompRow(
                peer_ticker="MELI",
                peer_name="MercadoLibre",
                market_cap_usd=None,
                revenue_ttm_usd=None,
                net_margin_ttm=None,
                roic_ttm=None,
                match_reasons=("named rival", "same industry"),
            )
        ]

    monkeypatch.setattr(p3_data, "load_peer_comp", fake_load)
    res = client.get("/api/peers/nu")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ticker"] == "NU"
    assert body["peers"] == [
        {"ticker": "MELI", "name": "MercadoLibre", "reasons": ["named rival", "same industry"]}
    ]


def test_peers_api_degrades_on_lookup_failure(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.sections import p3_data

    def boom(*_a: object, **_kw: object) -> list[object]:
        raise RuntimeError("no peer file")

    monkeypatch.setattr(p3_data, "load_peer_comp", boom)
    res = client.get("/api/peers/NU")
    assert res.status_code == 200  # best-effort surface, never a 500
    body = res.get_json()
    assert body["peers"] == []
    assert "peer lookup failed" in body["error"]
