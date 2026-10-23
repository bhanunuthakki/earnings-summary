"""Data registry for the cockpit ``/api/panel/<name>`` fragment dispatcher.

Panel names not registered here fall through to the analytical-dashboard
section renderer, and anything else is a 404.
"""

from __future__ import annotations

from collections.abc import Callable

from comments_server_context import ServerContext
from flask import Response

# A panel builder returns the rendered fragment (or its documented error tuple).
PanelBuilder = Callable[[ServerContext], "Response | tuple[str, int]"]

PANEL_BUILDERS: dict[str, PanelBuilder] = {}


def register_panel(name: str, builder: PanelBuilder) -> None:
    """Register one panel name -> builder. A duplicate name is a wiring error."""
    if name in PANEL_BUILDERS:
        raise ValueError(f"duplicate panel registration: {name}")
    PANEL_BUILDERS[name] = builder
