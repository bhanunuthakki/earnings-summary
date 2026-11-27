"""Patch collaborators of the cockpit server across every module ``create_app`` wires.

``create_app`` used to resolve every collaborator from one ``comments_server``
namespace. Its routes now live in ``comments_server_*`` modules that each bind
their own imports, so a test seam must replace the name in every one of them to
keep the single-namespace semantics the tests were written against.
"""

from __future__ import annotations

from types import ModuleType

import comments_server
import pytest

from cockpit import (
    actions_routes,
    advisor_routes,
    chat_routes,
    comments_routes,
    context,
    core_routes,
    middleware,
    ops_routes,
    panel_builders,
    panel_metrics_routes,
    panel_registry,
    panel_routes,
    shared,
    sibling_wiring,
    views_discovery_routes,
    viewspec_ask_routes,
    work_os_routes,
)

SERVER_MODULES: tuple[ModuleType, ...] = (
    comments_server,
    shared,
    context,
    middleware,
    panel_registry,
    panel_builders,
    panel_routes,
    core_routes,
    work_os_routes,
    ops_routes,
    panel_metrics_routes,
    viewspec_ask_routes,
    views_discovery_routes,
    actions_routes,
    advisor_routes,
    comments_routes,
    chat_routes,
    sibling_wiring,
)


def patch_server_seam(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: object,
    *,
    raising: bool = True,
) -> None:
    """Replace ``name`` in every server module that binds it."""
    targets = [module for module in SERVER_MODULES if hasattr(module, name)]
    if not targets:
        if raising:
            raise AttributeError(f"no comments_server module binds {name!r}")
        targets = [comments_server]
    for module in targets:
        monkeypatch.setattr(module, name, value, raising=False)


__all__ = ["SERVER_MODULES", "patch_server_seam"]
