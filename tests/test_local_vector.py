from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from provenance.search_index_lineage import load_projection_seal
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.grounded import SearchFilter
from search.local_vector import (
    EmbeddingModelSpec,
    LanceVectorBackend,
    LanceVectorIndex,
    LocalVectorCapabilityError,
    ResumableVectorIndexBuilder,
    VectorBatchReceipt,
    VectorBuildCheckpointStore,
    VectorBuildRequest,
    VectorDocument,
    canonical_float32_vector,
    vector_records_digest,
    vector_sha256,
)

STAMP = datetime(2026, 7, 26, 20, 0, 0)
HASH = "a" * 64


def _runtime_artifact() -> EmbeddingRuntimeArtifact:
    return EmbeddingRuntimeArtifact(
        provider="fastembed",
        model="BAAI/bge-small-en-v1.5",
        dimensions=2,
        execution_provider="CPUExecutionProvider",
        execution_settings=(),
        component_versions=(RuntimeComponentVersion(component="fastembed", version="1.2.3"),),
        files=(
            RuntimeArtifactFile(
                logical_name="model.onnx",
                role="model",
                size_bytes=1,
                sha256="b" * 64,
            ),
        ),
    )


class FakeEncoder:
    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return [[2.0, float(len(text))] for text in texts]


class StagedFakeIndex:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.published = False

    def stage_batch(
        self,
        index_run_id: str,
        records: list[dict[str, object]],
    ) -> None:
        assert not self.published
        for record in records:
            chunk_id = str(record["chunk_id"])
            existing = self.records.get(chunk_id)
            if existing is not None and existing != record:
                raise ValueError("conflict")
            self.records[chunk_id] = record

    def verify_batch(self, index_run_id: str, receipt: VectorBatchReceipt) -> bool:
        rows = self.read_batch(index_run_id, receipt)
        return (
            len(rows) == receipt.chunk_count
            and vector_records_digest(rows) == receipt.records_sha256
        )

    def read_batch(
        self,
        index_run_id: str,
        receipt: VectorBatchReceipt,
    ) -> list[dict[str, object]]:
        return [
            self.records[chunk_id]
            for chunk_id in sorted(self.records)
            if receipt.first_chunk_id <= chunk_id <= receipt.last_chunk_id
        ]

    def count_records(self, index_run_id: str) -> int:
        return len(self.records)

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        rows = [self.records[chunk_id] for chunk_id in sorted(self.records)]
        if len(rows) != expected_count:
            raise LocalVectorCapabilityError("count mismatch")
        return rows

    def publish(self, index_run_id: str) -> str:
        self.published = True
        return f"fake://published/{index_run_id}"

    def published_storage_uri(self, index_run_id: str) -> str:
        if not self.published:
            raise LocalVectorCapabilityError("absent")
        return f"fake://published/{index_run_id}"


class CountingEncoder(FakeEncoder):
    def __init__(self) -> None:
        self.passage_calls: list[list[str]] = []

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        self.passage_calls.append(texts)
        return super().encode_passages(texts)


class QueryOnlyIndex:
    def __init__(self) -> None:
        self.predicate = ""

    def open_table(self, index_run_id: str) -> QueryTable:
        return QueryTable(self)


class QueryTable:
    def __init__(self, index: QueryOnlyIndex) -> None:
        self._index = index

    def search(self, vector: list[float]) -> QuerySearch:
        assert vector == [2.0, 13.0]
        return QuerySearch(self._index)


class QuerySearch:
    def __init__(self, index: QueryOnlyIndex) -> None:
        self._index = index

    def where(self, predicate: str, *, prefilter: bool) -> QuerySearch:
        assert prefilter is True
        self._index.predicate = predicate
        return self

    def limit(self, limit: int) -> QuerySearch:
        assert limit == 3
        return self

    def to_list(self) -> list[dict[str, object]]:
        return [{"chunk_id": "acme", "_distance": 0.25}]


class RuntimeHashIndex:
    def __init__(
        self, row: dict[str, object], *, storage_uri: str = "fake://published/run-1"
    ) -> None:
        self.row = row
        self.storage_uri = storage_uri
        self.deleted = False

    def open_table(self, index_run_id: str) -> RuntimeHashTable:
        return RuntimeHashTable(self.row)

    def read_projection(self, index_run_id: str, *, expected_count: int) -> list[dict[str, object]]:
        if self.deleted or expected_count != 1:
            raise LocalVectorCapabilityError("projection absent")
        return [self.row]

    def published_storage_uri(self, index_run_id: str) -> str:
        if self.deleted:
            raise LocalVectorCapabilityError("projection absent")
        return self.storage_uri


class RuntimeHashTable:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def search(self, vector: list[float]) -> RuntimeHashSearch:
        return RuntimeHashSearch(self.row)


class RuntimeHashSearch:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row

    def where(self, predicate: str, *, prefilter: bool) -> RuntimeHashSearch:
        return self

    def limit(self, limit: int) -> RuntimeHashSearch:
        return self

    def to_list(self) -> list[dict[str, object]]:
        return [self.row]


class FakeLanceQuery:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.maximum = len(rows)

    def where(self, predicate: str) -> FakeLanceQuery:
        quoted = predicate.split("'")
        first = quoted[1].replace("''", "'")
        last = quoted[3].replace("''", "'")
        self.rows = [row for row in self.rows if first <= str(row["chunk_id"]) <= last]
        return self

    def limit(self, limit: int) -> FakeLanceQuery:
        self.maximum = limit
        return self

    def to_list(self) -> list[dict[str, object]]:
        return self.rows[: self.maximum]


class FakeLanceTable:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def add(self, data: list[dict[str, object]]) -> None:
        self.rows.extend(data)

    def query(self) -> FakeLanceQuery:
        return FakeLanceQuery(list(self.rows))

    def count_rows(self, where_filter: str | None = None) -> int:
        assert where_filter is None
        return len(self.rows)


class FakeLanceDatabase:
    def __init__(self, module: FakeLanceModule, key: str) -> None:
        self.module = module
        self.key = key

    def table_names(self) -> list[str]:
        return ["evidence_chunks"] if self.key in self.module.tables else []

    def create_table(
        self,
        name: str,
        *,
        data: list[dict[str, object]],
    ) -> FakeLanceTable:
        assert name == "evidence_chunks"
        table = FakeLanceTable(list(data))
        self.module.tables[self.key] = table
        return table

    def open_table(self, name: str) -> FakeLanceTable:
        assert name == "evidence_chunks"
        return self.module.tables[self.key]


class FakeLanceModule:
    def __init__(self) -> None:
        self.tables: dict[str, FakeLanceTable] = {}

    def connect(self, uri: str) -> FakeLanceDatabase:
        path = Path(uri)
        path.mkdir(parents=True, exist_ok=True)
        key = path.name.removesuffix(".staging")
        return FakeLanceDatabase(self, key)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE search_embedding_artifacts (
            embedding_artifact_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, purpose TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL, dimensions INTEGER NOT NULL, vector_sha256 TEXT,
            storage_uri TEXT, input_sha256 TEXT NOT NULL, request_config_sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL, failure_reason TEXT, cost_usd REAL, latency_ms INTEGER,
            started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
            runtime_artifact_sha256 TEXT
        );
        CREATE TABLE search_index_runs (
            index_run_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            index_key TEXT NOT NULL, revision INTEGER NOT NULL, manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL, config_sha256 TEXT NOT NULL, code_version TEXT NOT NULL,
            outcome TEXT NOT NULL, failure_reason TEXT, started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        CREATE TABLE search_index_memberships (
            index_run_id TEXT NOT NULL, chunk_id TEXT NOT NULL, membership_status TEXT NOT NULL,
            failure_reason TEXT, recorded_at TEXT NOT NULL, PRIMARY KEY(index_run_id, chunk_id)
        );
        CREATE TABLE search_chunks (
            chunk_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL
        );
        CREATE TABLE search_projection_seals (
            projection_seal_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
            index_run_id TEXT UNIQUE NOT NULL, manifest_id TEXT NOT NULL,
            index_kind TEXT NOT NULL, chunk_count INTEGER NOT NULL,
            chunk_set_sha256 TEXT NOT NULL, projection_records_sha256 TEXT NOT NULL,
            artifact_set_sha256 TEXT, provider TEXT, model TEXT, dimensions INTEGER,
            config_sha256 TEXT NOT NULL, storage_uri TEXT NOT NULL,
            sealed_at TEXT NOT NULL, runtime_artifact_sha256 TEXT
        );
        """
    )
    return conn


def _request() -> VectorBuildRequest:
    return VectorBuildRequest(
        index_run_id="run-1",
        index_key="evidence-vector",
        revision=1,
        manifest_id="manifest-1",
        code_version="test@1",
        request_config_sha256=HASH,
        model=EmbeddingModelSpec(
            provider="fastembed", model="BAAI/bge-small-en-v1.5", dimensions=2
        ),
        runtime_artifact=_runtime_artifact(),
        started_at=STAMP,
    )


def _resumable_documents() -> list[VectorDocument]:
    texts = ("alpha", "bravo", "charlie")
    return [
        VectorDocument(
            chunk_id=f"chunk-{ordinal}",
            text=text,
            input_sha256=hashlib.sha256(text.encode()).hexdigest(),
            manifest_id="manifest-1",
            issuer_id="issuer-1",
            ticker="ACME",
            form_type="10-Q",
            period_start="2026-01-01",
            period_end="2026-03-31",
            node_kind="passage",
            available_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
        )
        for ordinal, text in enumerate(texts, start=1)
    ]


def _document_batches(
    documents: Sequence[VectorDocument],
    after: str | None,
) -> list[list[VectorDocument]]:
    remaining = [document for document in documents if after is None or document.chunk_id > after]
    return [[document] for document in remaining]


def test_canonical_float32_hash_rejects_nonfinite_and_dimension_mismatch() -> None:
    payload = canonical_float32_vector([1, 2.5], dimensions=2)
    assert len(payload) == 8
    assert vector_sha256([1.0, 2.5], dimensions=2) == vector_sha256([1, 2.5], dimensions=2)
    with pytest.raises(ValueError, match="finite"):
        canonical_float32_vector([float("nan")], dimensions=1)
    with pytest.raises(ValueError, match="dimensions"):
        canonical_float32_vector([1.0], dimensions=2)


def test_resumable_builder_does_not_reembed_checkpointed_batches(
    tmp_path: Path,
) -> None:
    conn = _conn()
    index = StagedFakeIndex()
    checkpoint = VectorBuildCheckpointStore(tmp_path / "state.json")
    request = _request().model_copy(update={"batch_size": 1})
    documents = _resumable_documents()
    conn.executemany(
        "INSERT INTO search_chunks VALUES (?, 'manifest-1', ?)",
        [(document.chunk_id, document.input_sha256) for document in documents],
    )
    conn.commit()
    first_encoder = CountingEncoder()
    first_builder = ResumableVectorIndexBuilder(
        conn,
        first_encoder,
        index,
        checkpoint,
    )

    def interrupt(completed: int, total: int) -> None:
        assert (completed, total) == (1, 3)
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        first_builder.build(
            request,
            total_documents=3,
            document_batches=lambda after: _document_batches(documents, after),
            on_batch_complete=interrupt,
        )
    assert first_encoder.passage_calls == [["alpha"]]
    assert index.published is False
    assert conn.execute("SELECT COUNT(*) FROM search_index_runs").fetchone() == (0,)

    resumed_encoder = CountingEncoder()
    result = ResumableVectorIndexBuilder(
        conn,
        resumed_encoder,
        index,
        checkpoint,
    ).build(
        request,
        total_documents=3,
        document_batches=lambda after: _document_batches(documents, after),
    )
    assert result.outcome == "succeeded"
    assert resumed_encoder.passage_calls == [["bravo"], ["charlie"]]
    assert index.published is True
    assert conn.execute("SELECT COUNT(*) FROM search_embedding_artifacts").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM search_index_memberships").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM search_projection_seals").fetchone() == (1,)
    replay_encoder = CountingEncoder()
    replay = ResumableVectorIndexBuilder(
        conn,
        replay_encoder,
        index,
        checkpoint,
    ).build(
        request.model_copy(update={"started_at": datetime(2026, 7, 27)}),
        total_documents=3,
        document_batches=lambda _after: (_ for _ in ()).throw(
            AssertionError("published replay must not reopen the embedding source")
        ),
    )
    assert replay.created is False
    assert replay_encoder.passage_calls == []
    conn.close()


def test_vector_publication_rolls_back_run_artifacts_memberships_and_seal_together(
    tmp_path: Path,
) -> None:
    conn = _conn()
    documents = _resumable_documents()
    conn.executemany(
        "INSERT INTO search_chunks VALUES (?, 'manifest-1', ?)",
        [(document.chunk_id, document.input_sha256) for document in documents],
    )
    conn.commit()
    index = StagedFakeIndex()
    checkpoint = VectorBuildCheckpointStore(tmp_path / "atomic-state.json")
    builder = ResumableVectorIndexBuilder(conn, CountingEncoder(), index, checkpoint)
    conn.execute(
        "CREATE TRIGGER simulate_publication_crash "
        "BEFORE INSERT ON search_index_memberships "
        "WHEN (SELECT COUNT(*) FROM search_index_memberships) >= 1 "
        "BEGIN SELECT RAISE(ABORT, 'simulated SQL publication crash'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="publication crash"):
        builder.build(
            _request(),
            total_documents=3,
            document_batches=lambda after: _document_batches(documents, after),
        )
    assert index.published is True
    for table in (
        "search_index_runs",
        "search_embedding_artifacts",
        "search_index_memberships",
        "search_projection_seals",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

    conn.execute("DROP TRIGGER simulate_publication_crash")
    replay = builder.build(
        _request(),
        total_documents=3,
        document_batches=lambda _after: (_ for _ in ()).throw(
            AssertionError("published replay must not re-embed")
        ),
    )
    assert replay.outcome == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM search_projection_seals").fetchone() == (1,)
    conn.close()


def test_resumable_builder_halts_on_staged_hash_drift_without_reembedding(
    tmp_path: Path,
) -> None:
    conn = _conn()
    index = StagedFakeIndex()
    checkpoint = VectorBuildCheckpointStore(tmp_path / "state.json")
    request = _request().model_copy(update={"batch_size": 1})
    documents = _resumable_documents()
    builder = ResumableVectorIndexBuilder(conn, CountingEncoder(), index, checkpoint)
    with pytest.raises(RuntimeError):
        builder.build(
            request,
            total_documents=3,
            document_batches=lambda after: _document_batches(documents, after),
            on_batch_complete=lambda _completed, _total: (_ for _ in ()).throw(
                RuntimeError("stop")
            ),
        )
    index.records["chunk-1"]["vector"] = [99.0, 1.0]
    resumed_encoder = CountingEncoder()
    with pytest.raises(LocalVectorCapabilityError, match="checkpoint batch"):
        ResumableVectorIndexBuilder(
            conn,
            resumed_encoder,
            index,
            checkpoint,
        ).build(
            request,
            total_documents=3,
            document_batches=lambda after: _document_batches(documents, after),
        )
    assert resumed_encoder.passage_calls == []
    assert index.published is False
    assert conn.execute("SELECT COUNT(*) FROM search_index_runs").fetchone() == (0,)
    conn.close()


def test_lance_staging_path_is_published_only_after_bounded_receipt_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import search.local_vector as local_vector

    module = FakeLanceModule()
    monkeypatch.setattr(local_vector, "require_lancedb", lambda: module)
    index = LanceVectorIndex(tmp_path / "vectors")
    records: list[dict[str, object]] = [
        {
            "chunk_id": f"chunk-{number}",
            "vector": [float(number), 1.0],
            "vector_sha256": vector_sha256([float(number), 1.0], dimensions=2),
            "dimensions": 2,
            "input_sha256": f"{number}" * 64,
            "manifest_id": "manifest-1",
            "issuer_id": "issuer-1",
            "ticker": "ACME",
            "form_type": "10-Q",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "node_kind": "passage",
            "available_at": STAMP.isoformat(),
            "observed_at": STAMP.isoformat(),
            "retrieved_at": STAMP.isoformat(),
        }
        for number in (1, 2)
    ]
    index.stage_batch("run-atomic", records[:1])
    index.stage_batch("run-atomic", records[1:])
    staging = next((tmp_path / "vectors").glob("*.staging"))
    assert staging.is_dir()
    assert all(path.name.endswith(".staging") for path in (tmp_path / "vectors").iterdir())
    receipt = VectorBatchReceipt(
        batch_number=1,
        first_chunk_id="chunk-1",
        last_chunk_id="chunk-2",
        chunk_count=2,
        records_sha256=vector_records_digest(records),
    )
    assert index.verify_batch("run-atomic", receipt) is True
    uri = index.publish("run-atomic")
    assert ".staging" not in uri
    assert not staging.exists()
    assert index.verify_batch("run-atomic", receipt) is True
    with pytest.raises(ValueError, match="published"):
        index.stage_batch("run-atomic", records[:1])


def test_optional_dependency_error_is_loud() -> None:
    with pytest.raises(LocalVectorCapabilityError, match="fastembed"):
        from search.local_vector import require_fastembed

        require_fastembed(importer=lambda _name: (_ for _ in ()).throw(ImportError("missing")))


def test_query_backend_uses_query_encoding_and_sends_manifest_metadata_prefilters() -> None:
    from search.grounded import SearchFilter

    index = QueryOnlyIndex()
    backend = LanceVectorBackend(
        cast(LanceVectorIndex, index),
        index_run_id="run-1",
        manifest_id="manifest-1",
        encoder=FakeEncoder(),
        dimensions=2,
    )
    candidates = backend.search(
        "revenue query",
        SearchFilter(
            ticker="ACME",
            form_types=("10-Q",),
            node_kinds=("passage",),
            knowledge_cutoff=datetime(2026, 4, 1),
        ),
        3,
    )
    assert candidates[0].index_run_id == "run-1"
    assert "manifest_id = 'manifest-1'" in index.predicate
    assert "ticker = 'ACME'" in index.predicate
    assert "form_type = '10-Q'" in index.predicate
    assert "node_kind = 'passage'" in index.predicate
    assert "available_at <= '2026-04-01T00:00:00'" in index.predicate
    assert "observed_at <= '2026-04-01T00:00:00'" in index.predicate
    assert "retrieved_at <= '2026-04-01T00:00:00'" in index.predicate


def test_runtime_vector_result_recomputes_stored_hash_before_trusting_chunk() -> None:
    from search.grounded import SearchFilter

    conn = _conn()
    conn.execute(
        "INSERT INTO search_chunks VALUES ('chunk-1', 'manifest-1', ?)",
        ("b" * 64,),
    )
    conn.execute(
        "INSERT INTO search_index_runs VALUES "
        "('run-1','run-1','key',1,'manifest-1','vector',?,'test@1','succeeded',NULL,?,?)",
        (HASH, STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO search_embedding_artifacts VALUES "
        "('artifact','artifact','run-1','chunk-1','passage','fastembed','model',2,?,"
        "'fake://row',?,?,'succeeded',NULL,NULL,0,?,?,NULL)",
        (vector_sha256([1.0, 2.0], dimensions=2), "b" * 64, HASH, STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO search_index_memberships VALUES ('run-1','chunk-1','included',NULL,?)",
        (STAMP,),
    )
    tampered: dict[str, object] = {
        "chunk_id": "chunk-1",
        "vector": [9.0, 9.0],
        "vector_sha256": vector_sha256([1.0, 2.0], dimensions=2),
        "dimensions": 2,
        "input_sha256": "b" * 64,
        "_distance": 0.1,
    }
    backend = LanceVectorBackend(
        cast(LanceVectorIndex, RuntimeHashIndex(tampered)),
        index_run_id="run-1",
        manifest_id="manifest-1",
        encoder=FakeEncoder(),
        dimensions=2,
        ledger_conn=conn,
    )
    with pytest.raises(LocalVectorCapabilityError, match="hash"):
        backend.search("query", SearchFilter(), 1)
    conn.close()


def test_sealed_runtime_fails_closed_after_external_projection_deletion(
    tmp_path: Path,
) -> None:
    conn = _conn()
    document = _resumable_documents()[0]
    conn.execute(
        "INSERT INTO search_chunks VALUES (?, 'manifest-1', ?)",
        (document.chunk_id, document.input_sha256),
    )
    conn.commit()
    staged = StagedFakeIndex()
    ResumableVectorIndexBuilder(
        conn,
        CountingEncoder(),
        staged,
        VectorBuildCheckpointStore(tmp_path / "runtime-state.json"),
    ).build(
        _request(),
        total_documents=1,
        document_batches=lambda after: _document_batches([document], after),
    )
    seal = load_projection_seal(conn, index_run_id="run-1")
    assert seal is not None
    runtime = RuntimeHashIndex(staged.records[document.chunk_id], storage_uri=seal.storage_uri)
    backend = LanceVectorBackend(
        cast(LanceVectorIndex, runtime),
        index_run_id="run-1",
        manifest_id="manifest-1",
        encoder=FakeEncoder(),
        dimensions=2,
        ledger_conn=conn,
        projection_seal=seal,
    )
    assert backend.search("query", SearchFilter(), 1)
    original_vector = runtime.row["vector"]
    runtime.row["vector"] = [99.0, 99.0]
    with pytest.raises(LocalVectorCapabilityError, match="projection"):
        backend.search("query", SearchFilter(), 1)
    runtime.row["vector"] = original_vector
    runtime.deleted = True
    with pytest.raises(LocalVectorCapabilityError, match="projection"):
        backend.search("query", SearchFilter(), 1)
    conn.close()
