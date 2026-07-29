from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config

import provenance.source_fact_repository as repository_module
from alembic import command
from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
)
from provenance.source_fact_publication import (
    PUBLICATION_PAYLOAD_VERSION,
    canonical_json,
    digest_text,
    publication_payload,
    publication_seal_id,
    publication_seal_idempotency_key,
)
from provenance.source_fact_repository import (
    SourceFactPublication,
    SourceFactRepository,
)
from provenance.source_fact_stream import (
    INITIAL_EVENT_SHA256,
    PublicationCursor,
    PublicationEvent,
    PublicationStreamUnavailableError,
    PublicationStreamVerificationError,
    SequenceBasis,
    append_verified_publication_event,
    backfill_legacy_publication_stream,
    bind_resolution_snapshot_watermark,
    publication_cursor_through,
    read_publication_page,
    register_source_fact_stream_functions,
    verify_publication_event,
    verify_resolution_snapshot_watermark,
)

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "0213_decision_draft_provider_id"
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture(scope="module")
def migrated_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    path = tmp_path_factory.mktemp("source-fact-stream") / "template.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")
    return path


@pytest.fixture
def conn(
    tmp_path: Path,
    migrated_template: Path,
) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "stream.db"
    shutil.copy2(migrated_template, path)
    database = sqlite3.connect(path, timeout=30)
    database.execute("PRAGMA foreign_keys=ON")
    SourceFactRepository(database)
    try:
        yield database
    finally:
        database.close()


def _publication(
    publication_id: str,
    *,
    recorded_at: datetime = STAMP,
) -> SourceFactPublication:
    return SourceFactPublication(
        publication_id=publication_id,
        idempotency_key=f"publication:{publication_id}",
        created_at=recorded_at,
        recorded_at=recorded_at,
    )


def _insert_empty_0241_publication(
    conn: sqlite3.Connection,
    publication_id: str,
    *,
    recorded_at: datetime,
) -> None:
    SourceFactRepository(conn)
    idempotency_key = f"publication:{publication_id}"
    member_set_json = canonical_json([])
    member_set_sha256 = digest_text(member_set_json)
    payload_json = publication_payload(
        publication_id=publication_id,
        idempotency_key=idempotency_key,
        member_set_sha256=member_set_sha256,
        cell_count=0,
        observation_count=0,
        relation_count=0,
        derivation_seal_count=0,
        extraction_seal_count=0,
        resolution_revision_count=0,
        member_count=0,
        created_at=recorded_at,
        recorded_at=recorded_at,
    )
    payload_sha256 = digest_text(payload_json)
    conn.execute(
        "INSERT INTO source_fact_publications "
        "(publication_id,idempotency_key,payload_version,"
        "canonical_publication_payload_json,publication_payload_sha256,"
        "member_set_sha256,cell_count,observation_count,relation_count,"
        "derivation_seal_count,extraction_seal_count,"
        "resolution_revision_count,member_count,created_at,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            publication_id,
            idempotency_key,
            PUBLICATION_PAYLOAD_VERSION,
            payload_json,
            payload_sha256,
            member_set_sha256,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            recorded_at,
            recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO source_fact_publication_seals "
        "(publication_seal_id,idempotency_key,publication_id,"
        "member_count,canonical_member_set_json,member_set_sha256,"
        "publication_payload_sha256,sealed_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            publication_seal_id(publication_id),
            publication_seal_idempotency_key(idempotency_key),
            publication_id,
            0,
            member_set_json,
            member_set_sha256,
            payload_sha256,
            recorded_at,
        ),
    )


def test_repository_replay_returns_stable_sequence(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    publication = _publication("publication-replay")

    first = repository.publish(publication)
    replay = repository.publish(publication)

    assert first.publication_sequence == replay.publication_sequence == 1
    assert first.publication_event_sha256 == replay.publication_event_sha256
    assert first.exact_replay is False
    assert replay.exact_replay is True
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publication_stream").fetchone() == (1,)


def test_timestamp_collision_keeps_strict_database_order(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)

    second_id_first = repository.publish(_publication("publication-z"))
    alphabetic_first_second = repository.publish(_publication("publication-a"))

    assert second_id_first.publication_sequence == 1
    assert alphabetic_first_second.publication_sequence == 2
    assert conn.execute(
        "SELECT publication_id FROM source_fact_publication_stream ORDER BY publication_sequence"
    ).fetchall() == [("publication-z",), ("publication-a",)]


def test_concurrent_writers_allocate_unique_monotonic_sequences(
    conn: sqlite3.Connection,
) -> None:
    _insert_empty_0241_publication(
        conn,
        "publication-concurrent-a",
        recorded_at=STAMP,
    )
    _insert_empty_0241_publication(
        conn,
        "publication-concurrent-b",
        recorded_at=STAMP,
    )
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))

    def append(publication_id: str) -> int:
        worker = sqlite3.connect(path, timeout=30)
        worker.execute("PRAGMA foreign_keys=ON")
        register_source_fact_stream_functions(worker)
        try:
            event = append_verified_publication_event(
                worker,
                publication_id=publication_id,
                sequence_basis="transactional_publish",
                assigned_at=STAMP,
            )
            return event.publication_sequence
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        sequences = tuple(
            executor.map(
                append,
                (
                    "publication-concurrent-a",
                    "publication-concurrent-b",
                ),
            )
        )

    assert sorted(sequences) == [1, 2]
    assert conn.execute(
        "SELECT COUNT(DISTINCT publication_sequence) FROM source_fact_publication_stream"
    ).fetchone() == (2,)


def test_concurrent_idempotent_append_returns_one_event(
    conn: sqlite3.Connection,
) -> None:
    publication_id = "publication-concurrent-replay"
    _insert_empty_0241_publication(
        conn,
        publication_id,
        recorded_at=STAMP,
    )
    conn.commit()
    path = Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))

    def append() -> tuple[int, str]:
        worker = sqlite3.connect(path, timeout=30)
        worker.execute("PRAGMA foreign_keys=ON")
        try:
            event = append_verified_publication_event(
                worker,
                publication_id=publication_id,
                sequence_basis="transactional_publish",
                assigned_at=STAMP,
            )
            assert worker.in_transaction is False
            return event.publication_sequence, event.event_sha256
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(append) for _ in range(2))
        results = tuple(future.result() for future in futures)

    assert len(set(results)) == 1
    assert conn.execute("SELECT COUNT(*) FROM source_fact_publication_stream").fetchone() == (1,)


def test_stream_sequence_allows_clock_gaps(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "UPDATE source_fact_publication_stream_clock SET next_sequence=2 WHERE singleton_key=1"
    )
    receipt = SourceFactRepository(conn).publish(_publication("publication-after-gap"))

    assert receipt.publication_sequence == 2
    page = read_publication_page(
        conn,
        after=PublicationCursor.initial(),
        through_sequence=2,
        limit=10,
    )
    assert tuple(event.publication_sequence for event in page.events) == (2,)


def test_bounded_page_uses_verified_cursor(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    for suffix in ("1", "2", "3"):
        repository.publish(_publication(f"publication-page-{suffix}"))

    first = read_publication_page(
        conn,
        after=PublicationCursor.initial(),
        through_sequence=3,
        limit=2,
    )
    second = read_publication_page(
        conn,
        after=first.next_cursor,
        through_sequence=3,
        limit=2,
    )

    assert [event.publication_sequence for event in first.events] == [1, 2]
    assert first.has_more is True
    assert [event.publication_sequence for event in second.events] == [3]
    assert second.has_more is False
    with pytest.raises(
        PublicationStreamVerificationError,
        match="publication_cursor_hash_mismatch",
    ):
        read_publication_page(
            conn,
            after=PublicationCursor(
                publication_sequence=2,
                event_sha256=INITIAL_EVENT_SHA256.replace("0", "1"),
            ),
            through_sequence=3,
            limit=1,
        )


def test_verifier_rejects_tampered_event(
    conn: sqlite3.Connection,
) -> None:
    receipt = SourceFactRepository(conn).publish(_publication("publication-tamper"))
    conn.execute("DROP TRIGGER trg_source_fact_publication_stream_append_only")
    conn.execute(
        "UPDATE source_fact_publication_stream SET event_sha256=? WHERE publication_sequence=?",
        ("f" * 64, receipt.publication_sequence),
    )

    with pytest.raises(
        PublicationStreamVerificationError,
        match="publication_event_tampered",
    ):
        verify_publication_event(
            conn,
            publication_sequence=receipt.publication_sequence,
        )


def test_verifier_rejects_tampered_stream_clock(
    conn: sqlite3.Connection,
) -> None:
    receipt = SourceFactRepository(conn).publish(_publication("publication-clock-tamper"))
    conn.execute("DROP TRIGGER trg_source_fact_publication_stream_clock_monotonic")
    conn.execute(
        "UPDATE source_fact_publication_stream_clock SET next_sequence=? WHERE singleton_key=1",
        (receipt.publication_sequence,),
    )

    with pytest.raises(
        PublicationStreamVerificationError,
        match="publication_stream_clock_tampered",
    ):
        verify_publication_event(
            conn,
            publication_sequence=receipt.publication_sequence,
        )


def test_standalone_append_commits_and_failure_rolls_back_clock(
    conn: sqlite3.Connection,
) -> None:
    publication_id = "publication-standalone"
    _insert_empty_0241_publication(
        conn,
        publication_id,
        recorded_at=STAMP,
    )
    conn.commit()

    event = append_verified_publication_event(
        conn,
        publication_id=publication_id,
        sequence_basis="transactional_publish",
        assigned_at=STAMP,
    )
    assert event.publication_sequence == 1
    assert conn.in_transaction is False

    failed_id = "publication-standalone-failure"
    _insert_empty_0241_publication(
        conn,
        failed_id,
        recorded_at=STAMP,
    )
    conn.commit()
    conn.execute(
        "CREATE TRIGGER fail_stream_append "
        "BEFORE INSERT ON source_fact_publication_stream "
        "BEGIN SELECT RAISE(ABORT,'forced failure'); END"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        append_verified_publication_event(
            conn,
            publication_id=failed_id,
            sequence_basis="transactional_publish",
            assigned_at=STAMP,
        )

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT next_sequence FROM source_fact_publication_stream_clock"
    ).fetchone() == (2,)
    assert conn.execute(
        "SELECT COUNT(*) FROM source_fact_publication_stream WHERE publication_id=?",
        (failed_id,),
    ).fetchone() == (0,)


def test_repository_rolls_back_ledger_and_stream_together(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = repository_module.append_verified_publication_event

    def append_then_fail(
        database: sqlite3.Connection,
        *,
        publication_id: str,
        sequence_basis: SequenceBasis,
        assigned_at: datetime,
    ) -> PublicationEvent:
        event = original(
            database,
            publication_id=publication_id,
            sequence_basis=sequence_basis,
            assigned_at=assigned_at,
        )
        raise RuntimeError(f"fail after event {event.publication_sequence}")

    monkeypatch.setattr(
        repository_module,
        "append_verified_publication_event",
        append_then_fail,
    )
    publication_id = "publication-atomic-rollback"
    with pytest.raises(RuntimeError, match="fail after event"):
        SourceFactRepository(conn).publish(_publication(publication_id))

    assert conn.execute(
        "SELECT COUNT(*) FROM source_fact_publications WHERE publication_id=?",
        (publication_id,),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT COUNT(*) FROM source_fact_publication_stream WHERE publication_id=?",
        (publication_id,),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT next_sequence FROM source_fact_publication_stream_clock"
    ).fetchone() == (1,)


def test_repository_fails_closed_without_0246_schema() -> None:
    database = sqlite3.connect(":memory:")
    try:
        repository = SourceFactRepository(database)
        with pytest.raises(
            PublicationStreamUnavailableError,
            match="requires migration 0246",
        ):
            repository.publish(_publication("publication-before-0246"))
    finally:
        database.close()


def test_legacy_backfill_is_deterministic_and_replayable(
    conn: sqlite3.Connection,
) -> None:
    _insert_empty_0241_publication(
        conn,
        "publication-b",
        recorded_at=STAMP,
    )
    _insert_empty_0241_publication(
        conn,
        "publication-a",
        recorded_at=STAMP,
    )

    first = backfill_legacy_publication_stream(conn)
    replay = backfill_legacy_publication_stream(conn)

    assert first.publication_count == 2
    assert first.created is True
    assert replay.created is False
    assert replay.ordered_event_set_sha256 == first.ordered_event_set_sha256
    assert conn.execute(
        "SELECT publication_id,sequence_basis "
        "FROM source_fact_publication_stream ORDER BY publication_sequence"
    ).fetchall() == [
        ("publication-a", "legacy_backfill"),
        ("publication-b", "legacy_backfill"),
    ]


def test_empty_legacy_backfill_is_an_idempotent_noop(
    conn: sqlite3.Connection,
) -> None:
    first = backfill_legacy_publication_stream(conn)
    replay = backfill_legacy_publication_stream(conn)

    assert first == replay
    assert first.publication_count == 0
    assert first.created is False
    assert first.first_cursor == PublicationCursor.initial()
    assert first.last_cursor == PublicationCursor.initial()


def test_legacy_backfill_rejects_transactional_stream(
    conn: sqlite3.Connection,
) -> None:
    SourceFactRepository(conn).publish(_publication("publication-transactional"))

    with pytest.raises(
        PublicationStreamVerificationError,
        match="legacy_backfill_conflicts_with_existing_stream",
    ):
        backfill_legacy_publication_stream(conn)


def test_resolution_snapshot_watermark_is_exact_and_time_complete(
    conn: sqlite3.Connection,
) -> None:
    repository = SourceFactRepository(conn)
    repository.publish(_publication("publication-watermark-1"))
    second = repository.publish(_publication("publication-watermark-2"))
    cutoff = STAMP + timedelta(minutes=1)
    engine = CanonicalFactResolutionEngine(conn)
    engine.seal_snapshot("resolution-snapshot-1", cutoff, cutoff)

    bound = bind_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-snapshot-1",
        cutoff_at=cutoff,
        recorded_at=cutoff,
    )
    replay = verify_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-snapshot-1",
        cutoff_at=cutoff,
    )
    assert replay == bound
    assert bound.publication_high_watermark == second.publication_sequence
    assert bound.high_watermark_event_sha256 == second.publication_event_sha256


def test_resolution_watermark_excludes_events_after_cutoff(
    conn: sqlite3.Connection,
) -> None:
    after = STAMP + timedelta(hours=1)
    SourceFactRepository(conn).publish(
        _publication(
            "publication-after-cutoff",
            recorded_at=after,
        )
    )
    CanonicalFactResolutionEngine(conn).seal_snapshot(
        "resolution-snapshot-before",
        STAMP,
        after,
    )

    bound = bind_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-snapshot-before",
        cutoff_at=STAMP,
        recorded_at=after,
    )

    assert bound.cursor == PublicationCursor.initial()
    assert (
        publication_cursor_through(
            conn,
            cutoff_at=STAMP,
        )
        == PublicationCursor.initial()
    )


def test_empty_stream_resolution_watermark_uses_initial_cursor(
    conn: sqlite3.Connection,
) -> None:
    CanonicalFactResolutionEngine(conn).seal_snapshot(
        "resolution-snapshot-empty",
        STAMP,
        STAMP,
    )

    bound = bind_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-snapshot-empty",
        cutoff_at=STAMP,
        recorded_at=STAMP,
    )
    replay = verify_resolution_snapshot_watermark(
        conn,
        resolution_snapshot_id="resolution-snapshot-empty",
        cutoff_at=STAMP,
    )

    assert bound.cursor == PublicationCursor.initial()
    assert replay == bound
