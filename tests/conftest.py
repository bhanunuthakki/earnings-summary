"""Shared pytest fixtures for the earnings-summary test suite."""

from __future__ import annotations

import atexit
import importlib
import os
import shutil
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from execution.sqlite_bootstrap import preload_sqlite

# Every pytest controller and xdist worker is its own Python process. Load the
# verified runtime before importing sqlite3 so tests exercise the same writer
# safety contract as scheduled and interactive production launchers.
if TYPE_CHECKING:
    import sqlite3
else:
    preload_sqlite()
    sqlite3 = importlib.import_module("sqlite3")

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

    Production keeps one active graph beginning at its consolidated baseline. Historical migration
    unit tests still exercise their exact old revisions. A relative target or
    ``head`` follows the graph already stamped in that SQLite database; a fresh
    database defaults to the active graph. Explicit ``version_locations`` is
    always authoritative, including the production upgrade bridge. Historical fixtures keep their real revision; they never claim the active schema.
    """
    from alembic.config import Config

    from alembic import command

    archive = Path(__file__).resolve().parents[1] / "alembic" / "versions_archived"
    active = archive.parent / "versions"
    original_stamp = command.stamp
    original_upgrade = command.upgrade
    original_downgrade = command.downgrade

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
        with operation_graph(config, graph):
            original_upgrade(config, revision, sql=sql, tag=tag)
        record_graph(config, graph)

    def downgrade(
        config: Config,
        revision: str,
        sql: bool = False,
        tag: str | None = None,
    ) -> None:
        graph = graph_for_operation(config, revision)
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
    from ask import narrative_transport

    def _blocked(*_a: object, **_k: object) -> object:
        raise AssertionError(
            "real Ask narrative transport invoked in a test — monkeypatch "
            "ask.narrative_transport.stream_llm_text"
        )

    monkeypatch.setattr(narrative_transport, "stream_llm_text", _blocked)


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
# Build each graph/target once per worker and copy the private template per test.
# Application tests use the complete active graph. Historical migration tests
# request an explicit archived graph and keep the revision they actually ran.

_DB_TEMPLATES: dict[tuple[str, str, str], Path] = {}


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
    downgrade from an active schema.
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
        upgrade_from: str | None = None,
        before_upgrade: Callable[[Path], None] | None = None,
        upgrade_existing: bool = False,
    ) -> Path:
        if (upgrade_from is None) != (before_upgrade is None):
            raise ValueError("upgrade_from and before_upgrade must be provided together")
        if upgrade_existing and (upgrade_from is not None or archived):
            raise ValueError("upgrade_existing cannot be combined with migration build options")
        if upgrade_existing:
            command.upgrade(_config(dest, archived=False), target)
            return dest
        if upgrade_from is not None and archived:
            raise ValueError("seeded upgrades are supported only on the active graph")
        if upgrade_from is not None:
            build(dest, target=upgrade_from)
            assert before_upgrade is not None
            before_upgrade(dest)
            command.upgrade(_config(dest, archived=False), target)
            return dest
        graph = "archived" if archived else "active"
        effective_stamp = stamp if archived else "squashed"
        key = (graph, effective_stamp, target)
        template = _DB_TEMPLATES.get(key)
        if template is None or not template.exists():
            safe = target.replace("/", "_").replace("\\", "_")
            stamp_safe = effective_stamp.replace("/", "_").replace("\\", "_")
            template = cache_dir / f"{graph}_{stamp_safe}_{safe}.db"
            config = _config(template, archived=archived)
            if archived and stamp not in {"base", "head", "heads"}:
                command.stamp(config, stamp)
            command.upgrade(config, target)
            _DB_TEMPLATES[key] = template
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, dest)
        return dest

    return build


# ----------------------------------------------------------------------------
# Un-amortised chain build — the control that keeps ``migrated_db`` honest
# ----------------------------------------------------------------------------
#
# ``migrated_db`` is only a safe substitute for a hand-rolled fixture while the
# template it copies is indistinguishable from a database built by replaying the
# chain. Nothing checked that, yet converting the remaining direct builders
# rests on it entirely, so this fixture pays the full un-amortised price once
# per session as the comparison control for
# ``tests/test_migrated_db_parity.py``.
#
# Reserved for that control. Every other test must use ``migrated_db``, which
# reaches the same schema without paying for another chain replay.

_DIRECT_CHAIN_DBS: dict[str, Path] = {}


@pytest.fixture(scope="session")
def direct_chain_db(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., Path]:
    """Return a builder that replays the migration chain, bypassing the cache."""
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cache_dir = tmp_path_factory.mktemp("direct_chain_dbs")

    def build(dest: Path, *, target: str = "head") -> Path:
        source = _DIRECT_CHAIN_DBS.get(target)
        if source is None or not source.exists():
            safe = target.replace("/", "_").replace("\\", "_")
            source = cache_dir / f"direct_{safe}.db"
            cfg = Config(str(project_root / "alembic.ini"))
            cfg.set_main_option("script_location", str(project_root / "alembic"))
            cfg.set_main_option("sqlalchemy.url", f"sqlite:///{source.as_posix()}")
            command.upgrade(cfg, target)
            _DIRECT_CHAIN_DBS[target] = source
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return dest

    return build


_ARCHIVED_CHAIN_DBS: dict[tuple[str, str], Path] = {}


@pytest.fixture(scope="session")
def archived_chain_db(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[..., Path]:
    """Return a builder that replays an archived stamp-to-head chain.

    This is the parity control for ``migrated_db(..., archived=True)``.
    The explicit historical stamp and subsequent upgrade use the archived
    graph, preserving its historical revision.
    """
    from alembic.config import Config

    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cache_dir = tmp_path_factory.mktemp("archived_chain_dbs")

    def build(
        dest: Path,
        *,
        stamp: str = "0059_kpi_facts_restatement",
        target: str = "head",
    ) -> Path:
        key = (stamp, target)
        source = _ARCHIVED_CHAIN_DBS.get(key)
        if source is None or not source.exists():
            safe_stamp = stamp.replace("/", "_").replace("\\", "_")
            safe_target = target.replace("/", "_").replace("\\", "_")
            source = cache_dir / f"archived_{safe_stamp}_{safe_target}.db"
            cfg = Config(str(project_root / "alembic.ini"))
            cfg.set_main_option("script_location", str(project_root / "alembic"))
            cfg.set_main_option("sqlalchemy.url", f"sqlite:///{source.as_posix()}")
            command.stamp(cfg, stamp)
            command.upgrade(cfg, target)
            _ARCHIVED_CHAIN_DBS[key] = source
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return dest

    return build


@pytest.fixture
def fact_source_document(tmp_path: Path) -> Callable[[sqlite3.Connection, str], int]:
    """Create preserved synthetic source bytes and their real evidence lineage."""
    import hashlib
    from datetime import datetime

    from provenance.evidence_ledger import (
        ContentBlob,
        DocumentVersion,
        EvidenceLedger,
        EvidenceNode,
        ExtractionRun,
        SourceObservation,
    )

    def create(conn: sqlite3.Connection, ticker: str) -> int:
        body = f"Synthetic reported figures for {ticker}."
        path = tmp_path / f"{ticker}-source.txt"
        path.write_text(body)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stamp = datetime(2026, 1, 5, 10)
        url = f"https://example.test/{ticker}/source"
        cursor = conn.execute(
            "INSERT INTO documents(ticker,source_type,doc_type,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size,source_url) "
            "VALUES (?,'test','fixture',?,?,'2026-01-05 10:00:00','ok',?,?)",
            (ticker, str(path), digest, path.stat().st_size, url),
        )
        assert cursor.lastrowid is not None
        document_id = cursor.lastrowid
        identity = f"fixture-{document_id}"
        ledger = EvidenceLedger(conn)
        ledger.persist(
            ContentBlob(
                sha256=digest,
                byte_size=path.stat().st_size,
                media_type="text/plain",
                storage_uri=path.as_uri(),
                recorded_at=stamp,
            )
        )
        ledger.persist(
            SourceObservation(
                observation_id=identity,
                idempotency_key=identity,
                source_kind="test_fixture",
                source_url=url,
                blob_sha256=digest,
                source_published_at=stamp,
                filing_at=None,
                accepted_at=None,
                observed_at=stamp,
                retrieved_at=stamp,
                retrieval_config_sha256=hashlib.sha256(b"fixture-v1").hexdigest(),
                collector_code_version="fixture-v1",
            )
        )
        ledger.persist(
            DocumentVersion(
                document_version_id=identity,
                document_key=identity,
                version_sequence=1,
                observation_id=identity,
                blob_sha256=digest,
                issuer_id=f"fixture:{ticker}",
                ticker=ticker,
                document_type="fixture",
                form_type="fixture",
                language="en",
                legacy_document_id=document_id,
                recorded_at=stamp,
            )
        )
        ledger.persist(
            ExtractionRun(
                extraction_run_id=identity,
                idempotency_key=identity,
                document_version_id=identity,
                input_sha256=digest,
                extractor_name="fixture",
                extractor_config_sha256=digest,
                extractor_code_version="fixture-v1",
                output_sha256=digest,
                started_at=stamp,
                completed_at=stamp,
                outcome="succeeded",
            )
        )
        ledger.persist(
            EvidenceNode(
                node_id=identity,
                evidence_key=identity,
                revision=1,
                extraction_run_id=identity,
                node_kind="document",
                text=body,
                recorded_at=stamp,
            )
        )
        return document_id

    return create
