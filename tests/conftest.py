"""Shared pytest fixtures for the earnings-summary test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# --- Deterministic FMP tier baseline (runs at conftest IMPORT, before pytest
# collects any test module) ---------------------------------------------------
#
# Several production modules call load_dotenv() at import time — llm_client.py
# uses a *bare* load_dotenv() that walks UP from cwd, and execution/save_fmp_data
# loads the repo .env explicitly. From any checkout nested under the main repo
# (every .claude/worktrees/<name> session, or the main checkout itself) that
# resolves the developer's real .env and injects its values into os.environ the
# first time a test file's top-level `import llm_client` runs during COLLECTION.
# The dev .env carries FMP_TIER=free (the 2026-06 free-tier cutover), which flips
# save_fmp_data's module-load gate `_stable_only` and silently drops the v3/v4
# fallback ladder. That made the save_fmp_data empty-classification suite fail
# whenever the budget-integration test file was collected alongside it — a
# selection-dependent flake (fixed surgically at the point of use in #413).
#
# Pin a deterministic, non-free tier HERE, before collection. load_dotenv never
# overrides an already-set var, so this value survives every later production
# load_dotenv() for the whole session and the suite stops depending on the
# machine's .env. Tests that need a specific tier set it themselves via
# monkeypatch (see test_fmp_tier_ladder) and are unaffected; setdefault (not a
# hard write) means an explicitly-exported FMP_TIER still wins.
os.environ.setdefault("FMP_TIER", "basic")


@pytest.fixture(autouse=True)
def _restore_os_environ() -> Iterator[None]:
    """Restore os.environ after every test so a test that mutates process env
    *directly* (not through monkeypatch) can't leak it to later tests.

    This is the runtime-mutation backstop that complements the import-time tier
    pin above. monkeypatch already auto-undoes setenv/delenv, but a bare
    ``os.environ[...] = ...`` write — or a mid-test module import that triggers
    its own ``load_dotenv()`` — would otherwise persist for the rest of the
    session and make a later test's result depend on what ran before it.
    """
    saved = dict(os.environ)
    yield
    # Drop keys the test added, then restore keys it changed or removed.
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    for key, value in saved.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _no_real_chat_llm_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite never spends: any test that reaches the claude-CLI chat
    transport unpatched fails loudly instead of launching a real subprocess.
    (The ask engine's narrative route makes this reachable from plain
    endpoint tests — e.g. an unrecognized query falls through to narrative.)
    Tests that exercise these paths monkeypatch the seams themselves."""
    import chat_session

    def _blocked(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "real chat-LLM transport invoked in a test — monkeypatch "
            "chat_session.stream_llm_text or "
            "chat_session.build_chat_response.stream_response"
        )

    monkeypatch.setattr(chat_session, "stream_llm_text", _blocked)
    monkeypatch.setattr(chat_session.build_chat_response, "stream_response", _blocked)


@pytest.fixture(autouse=True)
def _no_real_pack_router_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same never-spend rule for the ask pack router (S4): ``ask.grounding``
    consults it on every narrative turn, so any test with tracked companies
    in its fixture DB would otherwise launch a real Haiku subprocess. The
    block raises at the router's transport seam; ``route_packs`` catches it
    (its documented fail-closed contract) and the turn degrades to
    document-only evidence — no spend, prod-faithful behavior. Tests that
    exercise routing/packs monkeypatch ``ask.router.call_llm_structured``
    or ``ask.grounding.route_packs`` themselves."""
    import ask.router as ask_router

    def _blocked(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "real pack-router LLM invoked in a test — monkeypatch "
            "ask.router.call_llm_structured or ask.grounding.route_packs"
        )

    monkeypatch.setattr(ask_router, "call_llm_structured", _blocked)
