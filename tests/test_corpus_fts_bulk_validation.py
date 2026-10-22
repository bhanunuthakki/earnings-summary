"""Native corpus completeness must not rescan unindexed FTS IDs per chunk."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine

from alembic import command
from provenance.search_index_lineage import (
    SearchProjectionSeal,
    lexical_projection_commitment,
    manifest_chunk_commitment,
    persist_projection_seal,
)
from search.corpus_builder import (
    ChunkerConfig,
    CorpusBuildRequest,
    CorpusBuildResult,
    build_grounded_search_corpus,
    lexical_index_config_sha256,
)
from search.grounded import (
    CorpusDocumentMembership,
    CorpusManifestSeal,
    GroundedSearchStore,
    IndexRun,
    membership_digest,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_search_corpus_builder as foundation

PRIOR = "0060_owner_governed_proposals"
CURRENT = "0061_bulk_lexical_projection_coverage"
TRIGGER = "trg_search_projection_seals_lexical_coverage"
seed: Callable[..., None] = getattr(foundation, "_seed")
request_for: Callable[..., CorpusBuildRequest] = getattr(foundation, "_request")


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    path = migrated_db(tmp_path / "bulk-fts.db")
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(database)
    yield database
    database.close()


def config(path: Path) -> Config:
    result = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    result.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    return result


def request(*, key: str = "target", chunks: int = 8) -> CorpusBuildRequest:
    return request_for(apply=True).model_copy(
        update={
            "corpus_key": key,
            "chunker": ChunkerConfig(max_characters=2, max_tokens=1),
            "persist_batch_size": chunks,
        }
    )


def stage(conn: sqlite3.Connection, candidate: CorpusBuildRequest) -> CorpusBuildResult:
    plan = build_grounded_search_corpus(conn, candidate.model_copy(update={"apply": False}))

    def stop(completed: int) -> None:
        if completed == plan.chunks_planned:
            raise RuntimeError("test stops after committed chunk staging")

    with pytest.raises(RuntimeError, match="test stops"):
        build_grounded_search_corpus(conn, candidate, on_chunk_batch_complete=stop)
    return plan


def prepare_seal(conn: sqlite3.Connection, plan: CorpusBuildResult) -> SearchProjectionSeal:
    """Use public native owners; isolate SQL guard from Python pre-validation."""
    memberships = tuple(
        CorpusDocumentMembership.model_validate(dict(row))
        for row in conn.execute(
            "SELECT * FROM search_corpus_document_memberships WHERE manifest_id=?",
            (plan.manifest_id,),
        )
    )
    store = GroundedSearchStore(conn)
    store.persist(
        CorpusManifestSeal(
            manifest_id=plan.manifest_id,
            expected_document_count=len(memberships),
            membership_digest_sha256=membership_digest(memberships),
            completion_status="complete",
            sealed_at=foundation.STAMP,
        )
    )
    digest = lexical_index_config_sha256(conn, manifest_id=plan.manifest_id)
    store.persist(
        IndexRun(
            index_run_id=plan.lexical_index_run_id,
            idempotency_key=plan.lexical_index_run_id,
            index_key=plan.lexical_index_run_id,
            revision=1,
            manifest_id=plan.manifest_id,
            index_kind="lexical",
            config_sha256=digest,
            code_version="test-native-seal@1",
            outcome="succeeded",
            started_at=foundation.STAMP,
            completed_at=foundation.STAMP,
        )
    )
    count, chunk_digest = manifest_chunk_commitment(conn, manifest_id=plan.manifest_id)
    _, projection_digest = lexical_projection_commitment(conn, manifest_id=plan.manifest_id)
    conn.commit()
    return SearchProjectionSeal(
        projection_seal_id="seal:" + plan.manifest_id.removeprefix("corpus-manifest:"),
        idempotency_key="seal:" + plan.manifest_id.removeprefix("corpus-manifest:"),
        index_run_id=plan.lexical_index_run_id,
        manifest_id=plan.manifest_id,
        index_kind="lexical",
        chunk_count=count,
        chunk_set_sha256=chunk_digest,
        projection_records_sha256=projection_digest,
        config_sha256=digest,
        storage_uri="sqlite-fts5://search_lexical_chunks",
        sealed_at=foundation.STAMP,
    )


def tamper(conn: sqlite3.Connection, manifest_id: str, mode: str) -> None:
    row = conn.execute(
        "SELECT lexical.rowid,lexical.chunk_id,lexical.text "
        "FROM search_lexical_chunks lexical JOIN search_chunks chunk "
        "ON chunk.chunk_id=lexical.chunk_id WHERE chunk.manifest_id=? LIMIT 1",
        (manifest_id,),
    ).fetchone()
    assert row is not None
    if mode == "missing":
        conn.execute("DELETE FROM search_lexical_chunks WHERE rowid=?", (row[0],))
    elif mode in {"duplicate", "conflicting_duplicate", "balanced_duplicate"}:
        if mode == "balanced_duplicate":
            conn.execute(
                "DELETE FROM search_lexical_chunks WHERE rowid=("
                "SELECT lexical.rowid FROM search_lexical_chunks lexical "
                "JOIN search_chunks chunk ON chunk.chunk_id=lexical.chunk_id "
                "WHERE chunk.manifest_id=? AND lexical.rowid<>? LIMIT 1)",
                (manifest_id, row[0]),
            )
        conn.execute(
            "INSERT INTO search_lexical_chunks(chunk_id,text) VALUES (?,?)",
            (row[1], "altered" if mode == "conflicting_duplicate" else row[2]),
        )
    else:
        conn.execute(
            "UPDATE search_lexical_chunks SET text=? WHERE rowid=?",
            (None if mode == "null" else "altered", row[0]),
        )
    conn.commit()


def vm_steps(conn: sqlite3.Connection, operation: Callable[[], object]) -> int:
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        operation()
    finally:
        conn.set_progress_handler(None, 0)
    return steps


def test_apply_uses_one_bulk_fts_check_and_preserves_plan_replay(
    conn: sqlite3.Connection,
) -> None:
    seed(conn, text="x " * 16)
    unrelated = build_grounded_search_corpus(conn, request(key="unrelated", chunks=16))
    candidate = request(chunks=16)
    plan = build_grounded_search_corpus(conn, candidate.model_copy(update={"apply": False}))
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    result = build_grounded_search_corpus(conn, candidate)
    conn.set_trace_callback(None)
    assert result.manifest_id == plan.manifest_id != unrelated.manifest_id
    assert result.chunks_planned == plan.chunks_planned == 16
    assert result.lexical_index_run_id == plan.lexical_index_run_id
    assert not any(
        "MIN(text), MAX(text) FROM search_lexical_chunks WHERE chunk_id =" in sql
        for sql in statements
    )
    before = lexical_index_config_sha256(conn, manifest_id=result.manifest_id)
    replay = build_grounded_search_corpus(conn, candidate)
    assert replay.records_created == 0
    assert lexical_index_config_sha256(conn, manifest_id=result.manifest_id) == before
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE search_chunks SET text='changed' WHERE manifest_id=?", (result.manifest_id,)
        )


@pytest.mark.parametrize(
    "mode",
    ["missing", "duplicate", "balanced_duplicate", "conflicting_duplicate", "altered", "null"],
)
def test_python_bulk_guard_rejects_tampered_staged_rows(
    conn: sqlite3.Connection, mode: str
) -> None:
    seed(conn, text="x " * 8)
    candidate = request()
    plan = stage(conn, candidate)
    tamper(conn, plan.manifest_id, mode)
    with pytest.raises(RuntimeError, match="FTS5 row missing or conflicting"):
        build_grounded_search_corpus(conn, candidate)
    assert conn.execute("SELECT COUNT(*) FROM search_projection_seals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mode",
    ["missing", "duplicate", "balanced_duplicate", "conflicting_duplicate", "altered", "null"],
)
def test_native_sql_guard_independently_rejects_tampered_rows(
    conn: sqlite3.Connection, mode: str
) -> None:
    seed(conn, text="x " * 8)
    plan = stage(conn, request())
    seal = prepare_seal(conn, plan)
    tamper(conn, plan.manifest_id, mode)
    with pytest.raises(
        sqlite3.IntegrityError, match="lexical projection seal requires exact FTS rows"
    ):
        persist_projection_seal(conn, seal)


def test_native_sql_guard_has_one_materialized_fts_scan_and_linear_vm_growth(
    conn: sqlite3.Connection, record_property: Callable[[str, object], None]
) -> None:
    seed(conn, text="x " * 256)
    plan = stage(conn, request(chunks=256))
    seal = prepare_seal(conn, plan)
    # The seal's exact native predicate is compiled against supplied NEW values.
    sql = str(conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()[0])
    predicate = sql.split(" WHEN ", 1)[1].split(" BEGIN ", 1)[0]
    predicate = predicate.replace("NEW.index_kind", "'lexical'").replace(
        "NEW.manifest_id", "'" + plan.manifest_id + "'"
    )
    query = "SELECT " + predicate
    details = [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert not any("CORRELATED" in item for item in details)
    assert sum("VIRTUAL TABLE" in item for item in details) == 1
    record_property("native_coverage_query_plan", str(details))
    base = vm_steps(conn, lambda: conn.execute(query).fetchall())
    # Add unrelated rows directly to the FTS representation. They must not
    # affect target coverage, and only add one scan's work.
    conn.executemany(
        "INSERT INTO search_lexical_chunks(chunk_id,text) VALUES (?,?)",
        ((f"unrelated-{i}", "other") for i in range(256)),
    )
    grown = vm_steps(conn, lambda: conn.execute(query).fetchall())
    assert grown < base * 3
    record_property("native_coverage_vm_base", base)
    record_property("native_coverage_vm_unrelated", grown)
    assert persist_projection_seal(conn, seal)


def test_target_checks_ignore_unrelated_corruption_but_reject_extra_target_chunk(
    conn: sqlite3.Connection,
) -> None:
    seed(conn, text="x " * 8)
    unrelated = build_grounded_search_corpus(conn, request(key="unrelated"))
    tamper(conn, unrelated.manifest_id, "balanced_duplicate")
    candidate = request()
    plan = stage(conn, candidate)
    extra = plan.planned_chunks[0].model_copy(
        update={"chunk_id": "extra-chunk", "idempotency_key": "extra-chunk", "chunk_key": "extra"}
    )
    GroundedSearchStore(conn).persist(extra)
    conn.commit()
    with pytest.raises(RuntimeError, match="missing or unexpected chunks"):
        build_grounded_search_corpus(conn, candidate)
    # A distinct exact target succeeds despite unrelated FTS corruption.
    assert (
        build_grounded_search_corpus(conn, request(key="valid-target")).completion_status
        == "complete"
    )


def coverage_query(conn: sqlite3.Connection, manifest_id: str) -> str:
    sql = str(conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()[0])
    predicate = sql.split(" WHEN ", 1)[1].split(" BEGIN ", 1)[0]
    return "SELECT " + predicate.replace("NEW.index_kind", "'lexical'").replace(
        "NEW.manifest_id", "'" + manifest_id + "'"
    )


def test_vm_scaling_compares_actual_legacy_and_bulk_guards(
    tmp_path: Path, migrated_db: Callable[..., Path], record_property: Callable[[str, object], None]
) -> None:
    legacy_counts: list[int] = []
    bulk_counts: list[int] = []
    python_legacy_counts: list[int] = []
    python_bulk_counts: list[int] = []
    for count in (64, 256):
        path = migrated_db(tmp_path / f"scaling-{count}.db")
        database = sqlite3.connect(path)
        register_sqlite_integrity_functions(database)
        try:
            seed(database, text="x " * count)
            build_grounded_search_corpus(database, request(key="unrelated", chunks=count))
            candidate = request(chunks=count)
            plan = stage(database, candidate)
            # Capture the exact bulk query executed by the public builder.
            statements: list[str] = []
            database.set_trace_callback(statements.append)
            build_grounded_search_corpus(database, candidate)
            database.set_trace_callback(None)
            bulk_query = next(
                sql for sql in statements if sql.startswith("SELECT lexical.chunk_id, COUNT(*)")
            )
            details = [str(row[3]) for row in database.execute("EXPLAIN QUERY PLAN " + bulk_query)]
            assert "VIRTUAL TABLE" in details[0]
            assert sum("VIRTUAL TABLE" in item for item in details) == 1
            record_property(f"python_bulk_query_plan_{count}", str(details))
            python_bulk_counts.append(
                vm_steps(database, lambda: database.execute(bulk_query).fetchall())
            )

            def old_python_check() -> None:
                for chunk in plan.planned_chunks:
                    database.execute(
                        "SELECT COUNT(*),MIN(text),MAX(text) FROM search_lexical_chunks WHERE chunk_id=?",
                        (chunk.chunk_id,),
                    ).fetchone()

            python_legacy_counts.append(vm_steps(database, old_python_check))
            bulk_query = coverage_query(database, plan.manifest_id)
            bulk_counts.append(vm_steps(database, lambda: database.execute(bulk_query).fetchall()))
            commitment = lexical_index_config_sha256(database, manifest_id=plan.manifest_id)
            retained = tuple(
                database.execute("SELECT * FROM search_projection_seals ORDER BY index_run_id")
            )
            command.downgrade(config(path), PRIOR)
            legacy_query = coverage_query(database, plan.manifest_id)
            legacy_counts.append(
                vm_steps(database, lambda: database.execute(legacy_query).fetchall())
            )
            command.upgrade(config(path), CURRENT)
            assert lexical_index_config_sha256(database, manifest_id=plan.manifest_id) == commitment
            assert (
                tuple(
                    database.execute("SELECT * FROM search_projection_seals ORDER BY index_run_id")
                )
                == retained
            )
            assert coverage_query(database, plan.manifest_id) == bulk_query
            assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            database.close()
    # Four times as many target and unrelated rows: legacy scans grow near
    # quadratically; the grouped scans must remain well below that growth.
    assert legacy_counts[1] > legacy_counts[0] * 12
    assert python_legacy_counts[1] > python_legacy_counts[0] * 12
    assert bulk_counts[1] < bulk_counts[0] * 6
    assert python_bulk_counts[1] < python_bulk_counts[0] * 6
    assert legacy_counts[1] > bulk_counts[1] * 8
    assert python_legacy_counts[1] > python_bulk_counts[1] * 8
    for name, values in (
        ("native_legacy_vm", legacy_counts),
        ("native_bulk_vm", bulk_counts),
        ("python_legacy_vm", python_legacy_counts),
        ("python_bulk_vm", python_bulk_counts),
    ):
        record_property(name, str(values))


@pytest.mark.parametrize(
    "mode",
    ["missing", "duplicate", "balanced_duplicate", "conflicting_duplicate", "altered", "null"],
)
def test_0061_native_guard_matches_0060_for_adversarial_rows(
    conn: sqlite3.Connection, mode: str
) -> None:
    seed(conn, text="x " * 8)
    plan = stage(conn, request())
    seal = prepare_seal(conn, plan)
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    tamper(conn, plan.manifest_id, mode)
    for revision in (PRIOR, CURRENT):
        if revision == PRIOR:
            command.downgrade(config(path), PRIOR)
        else:
            command.upgrade(config(path), CURRENT)
        with pytest.raises(
            sqlite3.IntegrityError, match="lexical projection seal requires exact FTS rows"
        ):
            persist_projection_seal(conn, seal)
        conn.rollback()


@pytest.mark.parametrize("mode", ["missing", "unowned", "ddl_failure"])
def test_migration_refuses_unknown_guard_and_rolls_back_replacement(
    conn: sqlite3.Connection, mode: str
) -> None:
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
    command.downgrade(config(path), PRIOR)
    before = str(
        conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()[0]
    )
    if mode in {"missing", "unowned"}:
        conn.execute(f"DROP TRIGGER {TRIGGER}")
        if mode == "unowned":
            conn.execute(before.replace("<> 1", "<> 2", 1))
        conn.commit()

    def fail_create(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if mode == "ddl_failure" and statement.startswith(f"CREATE TRIGGER {TRIGGER}"):
            raise RuntimeError("test interrupted replacement")

    event.listen(Engine, "before_cursor_execute", fail_create)
    try:
        with pytest.raises(RuntimeError, match=r"missing|migration owner|interrupted replacement"):
            command.upgrade(config(path), CURRENT)
    finally:
        event.remove(Engine, "before_cursor_execute", fail_create)
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()
    assert (None if row is None else str(row[0])) == (
        None
        if mode == "missing"
        else before.replace("<> 1", "<> 2", 1)
        if mode == "unowned"
        else before
    )
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PRIOR
