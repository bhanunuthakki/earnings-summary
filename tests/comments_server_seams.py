"""Patch collaborators of the cockpit server across every module ``create_app`` wires.

``create_app`` used to resolve every collaborator from one ``comments_server``
namespace. Its routes now live in ``comments_server_*`` modules that each bind
their own imports, so a test seam must replace the name in every one of them to
keep the single-namespace semantics the tests were written against.
"""

from __future__ import annotations

from types import ModuleType

import comments_server
import comments_server_actions_routes
import comments_server_advisor_routes
import comments_server_chat_routes
import comments_server_comments_routes
import comments_server_context
import comments_server_core_routes
import comments_server_middleware
import comments_server_ops_routes
import comments_server_panel_builders
import comments_server_panel_metrics_routes
import comments_server_panel_registry
import comments_server_panel_routes
import comments_server_shared
import comments_server_sibling_wiring
import comments_server_views_discovery_routes
import comments_server_viewspec_ask_routes
import comments_server_work_os_routes
import pytest

SERVER_MODULES: tuple[ModuleType, ...] = (
    comments_server,
    comments_server_shared,
    comments_server_context,
    comments_server_middleware,
    comments_server_panel_registry,
    comments_server_panel_builders,
    comments_server_panel_routes,
    comments_server_core_routes,
    comments_server_work_os_routes,
    comments_server_ops_routes,
    comments_server_panel_metrics_routes,
    comments_server_viewspec_ask_routes,
    comments_server_views_discovery_routes,
    comments_server_actions_routes,
    comments_server_advisor_routes,
    comments_server_comments_routes,
    comments_server_chat_routes,
    comments_server_sibling_wiring,
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
