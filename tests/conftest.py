"""Shared pytest fixtures for the earnings-summary test suite."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

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
os.environ.setdefault(
    "EARNINGS_SUMMARY_ENV_FILE",
    os.path.join(os.path.dirname(__file__), ".pytest-no-external-env"),
)
os.environ.setdefault(
    "EARNINGS_SUMMARY_SECRETS_DIR",
    os.path.join(
        os.environ.get("TEMP", os.path.dirname(__file__)), "earnings-summary-pytest-secrets"
    ),
)
# Production is Codex-first. Unit tests pin the reversible Claude mode so
# legacy tests never launch a real membership subprocess; dedicated routing
# tests opt back into Codex and patch the transport seam.
os.environ.setdefault("LLM_PRIMARY_SUBSCRIPTION_BACKEND", "claude")
os.environ.setdefault("COMMENTS_SERVER_REPORT_CAPABILITY", "test-report-capability")


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
def _clear_ask_turn_caches() -> Iterator[None]:
    """Reset the L14 ask turn caches (corpus / route / gather) before AND after
    every test. Process-local module state would otherwise leak between tests —
    most importantly the route cache, which is keyed on the normalized question
    and could hand one test another test's monkeypatched router decision (several
    router tests reuse the same question string). Clearing makes the caches
    invisible to every test that doesn't explicitly exercise them."""
    from ask import turn_cache

    turn_cache.clear_all()
    yield
    turn_cache.clear_all()


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


@pytest.fixture(autouse=True)
def _no_real_claim_grounding_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same never-spend rule for the claim-grounding audit (S8): the ask
    engine runs it after every grounded narrative answer, so any test that
    stubs evidence + transport would otherwise launch a real Haiku
    subprocess. The block raises at the transport seam;
    ``extract_claim_map`` catches it (its documented fail-closed contract)
    and the citations event degrades to the answer-level shape — no spend,
    prod-faithful behavior. Tests that exercise the map monkeypatch
    ``ask.claims.call_llm_structured`` themselves."""
    import ask.claims as ask_claims

    def _blocked(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "real claim-grounding LLM invoked in a test — monkeypatch "
            "ask.claims.call_llm_structured or ask.claims.extract_claim_map"
        )

    monkeypatch.setattr(ask_claims, "call_llm_structured", _blocked)


# ----------------------------------------------------------------------------
# Migrated-database templates — build the chain ONCE, copy it per test
# ----------------------------------------------------------------------------
#
# The suite's dominant cost is not the number of tests: it is that ~228 test
# files build a schema with an UNSCOPED fixture shaped like
#
#     @pytest.fixture
#     def db_path(tmp_path):
#         command.stamp(cfg, PRIOR_HEAD)
#         command.upgrade(cfg, "head")
#
# Function scope is pytest's default, so a 262-migration chain replays for
# EVERY test. Measured on this repo: 18-56s per test depending on the stamp
# point, against 13.5ms to copy the resulting file. That is the difference
# between a 24-minute CI run behind 8-way sharding and a few minutes.
#
# ``migrated_db`` builds each distinct (stamp, target) recipe once per session
# and hands every test a fresh copy. Correctness is unchanged — each test still
# gets its own private, writable database file; only the construction is
# amortised.
#
# NOT every fixture can use this. Some do more than stamp+upgrade (the
# 0215_observation_resolution_ledger recipe, for one, needs predecessor tables
# created first and fails outright on a bare chain build). Those keep building
# their own. The helper is for the pure stamp→upgrade case, which is most of
# them.

_DB_TEMPLATES: dict[tuple[str, str], Path] = {}


@pytest.fixture(scope="session")
def migrated_db(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., Path]:
    """Return ``build(dest, stamp=..., target="head") -> dest``.

    Copies a session-cached migrated database instead of replaying migrations.
    The cache key is the exact ``(stamp, target)`` recipe, so files that build
    different schemas do not collide and each distinct recipe is paid for once.
    """
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cache_dir = tmp_path_factory.mktemp("migrated_db_templates")

    def _config(db: Path) -> Config:
        cfg = Config(str(project_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "alembic"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
        return cfg

    def build(dest: Path, *, stamp: str, target: str = "head") -> Path:
        key = (stamp, target)
        template = _DB_TEMPLATES.get(key)
        if template is None or not template.exists():
            safe = f"{stamp}__{target}".replace("/", "_").replace("\\", "_")
            template = cache_dir / f"{safe}.db"
            command.stamp(_config(template), stamp)
            command.upgrade(_config(template), target)
            _DB_TEMPLATES[key] = template
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, dest)
        return dest

    return build
