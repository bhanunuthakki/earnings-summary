"""Wiring tests for Wave 3 HTMX (self-updating cockpit tiles).

The live ``GET /api/cockpit`` route is exercised against real data by the running
server; these pin the pure-function wiring around it so it can't silently
regress: the Overview wraps the cockpit in an HTMX poller, the shell head loads
HTMX (and Alpine), the offline report stays HTMX-free, and the shell's
fragment-injector processes HTMX on injected content.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.command_center_shell import render_overview_panel, render_shell  # noqa: E402
from ui.htmx_runtime import htmx_head  # noqa: E402
from ui.living_grid import head_assets  # noqa: E402


def test_overview_wraps_cockpit_in_htmx_poller() -> None:
    ov = render_overview_panel({}, {})
    assert 'id="cc-cockpit-live"' in ov
    assert 'hx-get="/api/cockpit"' in ov
    assert 'hx-trigger="every 90s"' in ov


def test_shell_head_loads_htmx_and_alpine() -> None:
    html = render_shell(overview_html="<div>ov</div>")
    # HTMX is inlined in the head, before the body.
    assert "var htmx=function" in html
    assert html.index("var htmx=function") < html.index("</head>")
    # Alpine (the living-grid runtime) still loads too — they coexist.
    assert "window.livingGrid" in html
    # The fragment-injector processes HTMX on shell-injected content.
    assert "window.htmx.process" in html


def test_htmx_is_shell_only_not_in_offline_report_assets() -> None:
    # The offline file:// report uses living_grid.head_assets() (Alpine), which
    # must NOT carry HTMX — HTMX needs a server and is dead weight offline.
    assert "var htmx=function" not in head_assets()
    # ...and the shell-only loader is where HTMX actually lives.
    assert "var htmx=function" in htmx_head()
