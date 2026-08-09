"""Shared pytest fixtures for the earnings-summary test suite."""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
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

# Test modules may resolve the default database during collection, before any
# fixture can redirect it. Point every pytest process at its own disposable DB
# so collection and tests cannot touch the checkout's data/portfolio.db or
# contend with another xdist worker. The PID is unique even across simultaneous
# local sessions; the worker id keeps paths useful when diagnosing a retained
# crash directory.
_worker_id = os.environ.get("PYTEST_XDIST_WORKER", "controller")
_test_db_dir = Path(tempfile.mkdtemp(prefix=f"earnings-summary-pytest-{_worker_id}-"))
_test_db_path = _test_db_dir / f"portfolio-{os.getpid()}.db"
_db_original_marker = "_EARNINGS_SUMMARY_PYTEST_ORIGINAL_DB_PATH"
_db_path_absent = "__pytest_db_path_was_absent__"
if _db_original_marker not in os.environ:
    os.environ[_db_original_marker] = os.environ.get(
        "EARNINGS_SUMMARY_DB_PATH",
        _db_path_absent,
    )
_encoded_original_db_path = os.environ[_db_original_marker]
_db_path_was_set = _encoded_original_db_path != _db_path_absent
_original_db_path = _encoded_original_db_path if _db_path_was_set else None
os.environ["EARNINGS_SUMMARY_DB_PATH"] = os.fspath(_test_db_path)


def _restore_collection_db_override() -> None:
    if os.environ.get("EARNINGS_SUMMARY_DB_PATH") != os.fspath(_test_db_path):
        return
    if _db_path_was_set and _original_db_path is not None:
        os.environ["EARNINGS_SUMMARY_DB_PATH"] = _original_db_path
    else:
        os.environ.pop("EARNINGS_SUMMARY_DB_PATH", None)
    os.environ.pop(_db_original_marker, None)


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Restore runtime env semantics after each process collects its tests."""
    del session, config, items
    _restore_collection_db_override()


atexit.register(shutil.rmtree, _test_db_dir, ignore_errors=True)
atexit.register(_restore_collection_db_override)


@pytest.fixture(scope="session", autouse=True)
def archived_migration_harness() -> Iterator[None]:
    """Route explicit historical revision tests to the archived Alembic graph.

    Production keeps one simple active graph (0001→0003). Historical migration
    unit tests still exercise their exact old revisions. A relative target or
    ``head`` follows the graph already stamped in that SQLite database; a fresh
    database defaults to the active graph. Explicit ``version_locations`` is
    always authoritative, including the production upgrade bridge. Completed
    legacy fixtures expose the schema-equivalent active head to runtime writer
    guards, while historical downgrade operations restore the archived head.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command

    archive = Path(__file__).resolve().parents[1] / "alembic" / "versions_archived"
    active = archive.parent / "versions"
    original_stamp = command.stamp
    original_upgrade = command.upgrade
    original_downgrade = command.downgrade
    reanchored_archive_databases: set[Path] = set()

    def graph_head(directory: Path) -> str:
        graph_config = Config()
        graph_config.set_main_option("script_location", str(directory.parent))
        graph_config.set_main_option("version_locations", str(directory))
        head = ScriptDirectory.from_config(graph_config).get_current_head()
        if head is None:
            raise RuntimeError(f"migration graph has no head: {directory}")
        return head

    archive_head = graph_head(archive)
    active_head = graph_head(active)

    def database_path(config: Config) -> Path | None:
        from sqlalchemy.engine import make_url

        raw_url = config.get_main_option("sqlalchemy.url", "").strip()
        if not raw_url:
            return None
        url = make_url(raw_url)
        if not url.drivername.startswith("sqlite") or url.database in {None, "", ":memory:"}:
            return None
        return Path(str(url.database)).resolve()

    def database_revision(database: Path) -> str | None:
        if not database.exists():
            return None
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else str(row[0])

    def replace_database_revision(database: Path, old: str, new: str) -> None:
        with sqlite3.connect(database) as connection:
            updated = connection.execute(
                "UPDATE alembic_version SET version_num=? WHERE version_num=?",
                (new, old),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"test migration graph re-anchor failed for {database}: expected revision {old}"
                )

    def configured_graph(config: Config) -> str | None:
        configured_locations = config.get_main_option("version_locations", "").strip()
        if configured_locations:
            location = Path(configured_locations).resolve()
            if location == archive.resolve():
                return "archive"
            if location == active.resolve():
                return "active"
            return "configured"
        return None

    def database_graph(config: Config) -> str | None:
        """Infer the graph for a new Config from its stamped SQLite revision."""
        database = database_path(config)
        if database is None:
            return None
        if database in reanchored_archive_databases:
            return "archive"
        current = database_revision(database)
        if current is None:
            return None
        if any(archive.glob(f"{current}*.py")):
            return "archive"
        if any(active.glob(f"{current}*.py")):
            return "active"
        return None

    def graph_for_operation(
        config: Config,
        revision: str | list[str] | tuple[str, ...],
    ) -> str:
        configured = configured_graph(config)
        if configured is not None:
            return configured
        revisions = (revision,) if isinstance(revision, str) else tuple(revision)
        for requested in revisions:
            for token in requested.split(":"):
                if token in {"base", "head", "heads"} or token.startswith(("+", "-")):
                    continue
                if any(archive.glob(f"{token}*.py")):
                    return "archive"
                if any(active.glob(f"{token}*.py")):
                    return "active"
        remembered = config.attributes.get("pytest_last_migration_graph")
        if remembered in {"archive", "active"}:
            return str(remembered)
        return database_graph(config) or "active"

    @contextmanager
    def selected_graph(config: Config, directory: Path) -> Generator[None, None, None]:
        section = config.config_ini_section
        had_locations = config.file_config.has_option(section, "version_locations")
        previous = config.get_main_option("version_locations")
        config.set_main_option("version_locations", str(directory))
        try:
            yield
        finally:
            if had_locations and previous is not None:
                config.set_main_option("version_locations", previous)
            else:
                config.file_config.remove_option(section, "version_locations")

    @contextmanager
    def operation_graph(config: Config, graph: str) -> Generator[None, None, None]:
        if graph == "configured":
            yield
            return
        with selected_graph(config, archive if graph == "archive" else active):
            yield

    def record_graph(config: Config, graph: str) -> None:
        if graph != "configured":
            config.attributes["pytest_last_migration_graph"] = graph

    def restore_archived_revision(config: Config, graph: str) -> None:
        """Undo a test-only active-head re-anchor before another archive op."""
        if graph != "archive" or configured_graph(config) is not None:
            return
        database = database_path(config)
        # The squashed active baseline is a schema-equivalent snapshot of the
        # archived head. Historical downgrade tests therefore re-anchor only
        # Alembic's metadata before walking the archived graph; they never
        # replay the active baseline over existing tables.
        if database is not None and database_revision(database) == active_head:
            replace_database_revision(database, active_head, archive_head)

    def expose_archive_schema_as_current(config: Config, graph: str) -> None:
        """Let production writer guards accept a fully upgraded legacy fixture."""
        if graph != "archive" or configured_graph(config) is not None:
            return
        database = database_path(config)
        if database is not None and database_revision(database) == archive_head:
            replace_database_revision(database, archive_head, active_head)
            reanchored_archive_databases.add(database)

    def stamp(
        config: Config,
        revision: str | list[str] | tuple[str, ...],
        sql: bool = False,
        tag: str | None = None,
        purge: bool = False,
    ) -> None:
        graph = graph_for_operation(config, revision)
        with operation_graph(config, graph):
            original_stamp(config, revision, sql=sql, tag=tag, purge=purge)
        record_graph(config, graph)

    def upgrade(
        config: Config,
        revision: str,
        sql: bool = False,
        tag: str | None = None,
    ) -> None:
        graph = graph_for_operation(config, revision)
        restore_archived_revision(config, graph)
        with operation_graph(config, graph):
            original_upgrade(config, revision, sql=sql, tag=tag)
        if not sql:
            expose_archive_schema_as_current(config, graph)
        record_graph(config, graph)

    def downgrade(
        config: Config,
        revision: str,
        sql: bool = False,
        tag: str | None = None,
    ) -> None:
        graph = graph_for_operation(config, revision)
        restore_archived_revision(config, graph)
        with operation_graph(config, graph):
            original_downgrade(config, revision, sql=sql, tag=tag)
        record_graph(config, graph)

    patcher = pytest.MonkeyPatch()
    patcher.setattr(command, "stamp", stamp)
    patcher.setattr(command, "upgrade", upgrade)
    patcher.setattr(command, "downgrade", downgrade)
    try:
        yield
    finally:
        patcher.undo()


_worker_suffix = f"-{_worker_id}" if _worker_id != "controller" else ""
os.environ.setdefault(
    "EARNINGS_SUMMARY_SECRETS_DIR",
    os.path.join(
        tempfile.gettempdir(),
        f"earnings-summary-pytest-secrets{_worker_suffix}",
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
def _isolate_default_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind ``db`` defaults per test without changing runtime environment."""
    import db

    database = tmp_path / "default-db" / "portfolio.db"
    monkeypatch.setattr(db, "DB_PATH", os.fspath(database))
    monkeypatch.setattr(db, "DATA_DIR", os.fspath(database.parent))
    monkeypatch.setattr(db, "FMP_DIR", os.fspath(database.parent / "historical" / "fmp"))


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
# ``migrated_db`` builds each distinct graph/target once per session
# and hands every test a fresh copy. Correctness is unchanged — each test still
# gets its own private, writable database file; only the construction is
# amortised.
#
# NOT every fixture can use this. Some do more than stamp+upgrade (the
# 0215_observation_resolution_ledger recipe, for one, needs predecessor tables
# created first and fails outright on a bare chain build). Those keep building
# their own. The helper is for the pure stamp→upgrade case, which is most of
# them.

_DB_TEMPLATES: dict[tuple[str, str, str, bool], Path] = {}


@pytest.fixture(scope="session")
def migrated_db(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., Path]:
    """Return a cached migration-template builder.

    Copies a session-cached migrated database instead of replaying migrations.
    ``stamp`` remains accepted as compatibility metadata for older tests, but
    the squashed graph normally builds directly to ``target``. Migration-only
    downgrade tests may request the archived graph explicitly; they share one
    archived-head template rather than attempting an unsafe cross-graph
    downgrade from active 0003.
    """
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cache_dir = tmp_path_factory.mktemp("migrated_db_templates")

    def _config(db: Path, *, archived: bool) -> Config:
        cfg = Config(str(project_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "alembic"))
        if archived:
            cfg.set_main_option(
                "version_locations",
                str(project_root / "alembic" / "versions_archived"),
            )
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db.as_posix()}")
        return cfg

    def build(
        dest: Path,
        *,
        stamp: str = "head",
        target: str = "head",
        archived: bool = False,
        reanchor_to_active_head: bool = False,
    ) -> Path:
        if reanchor_to_active_head and not archived:
            raise ValueError("only an archived migration graph can be re-anchored")
        graph = "archived" if archived else "active"
        effective_stamp = stamp if archived else "squashed"
        key = (graph, effective_stamp, target, reanchor_to_active_head)
        template = _DB_TEMPLATES.get(key)
        if template is None or not template.exists():
            safe = target.replace("/", "_").replace("\\", "_")
            stamp_safe = effective_stamp.replace("/", "_").replace("\\", "_")
            template = cache_dir / f"{graph}_{stamp_safe}_{safe}.db"
            config = _config(template, archived=archived)
            if archived and stamp not in {"base", "head", "heads"}:
                command.stamp(config, stamp)
            command.upgrade(config, target)
            if reanchor_to_active_head:
                from alembic.script import ScriptDirectory

                active_head = ScriptDirectory.from_config(
                    _config(template, archived=False)
                ).get_current_head()
                if active_head is None:
                    raise RuntimeError("active migration graph has no head")
                with sqlite3.connect(template) as connection:
                    connection.execute("UPDATE alembic_version SET version_num=?", (active_head,))
            _DB_TEMPLATES[key] = template
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, dest)
        return dest

    return build
