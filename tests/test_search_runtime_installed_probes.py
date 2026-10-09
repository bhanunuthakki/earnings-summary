"""Real installed-package probes for the owner-approved search runtime.

These probes run only when the separately locked search dependencies
(``requirements-search.lock``) are installed in the active environment. They
exercise exactly the compatibility surface ``src/search/local_vector.py``
uses: fastembed 0.9.0's kwargs-based ``local_files_only`` contract and
lancedb 0.40.0's synchronous ``search(None)`` scan API. No model is downloaded
here (the encoder probe pins offline fail-closed behavior on an empty cache;
the one-time approved model downloads belong to the separately scoped runtime
comparison feature), no database is opened, no corpus is built, and nothing is
evaluated. Scratch directories are disposable and live under the mission
``.tmp/search-validation/`` root.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import json
import platform
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from search.local_vector import vector_sha256

fastembed_available = importlib.util.find_spec("fastembed") is not None
lancedb_available = importlib.util.find_spec("lancedb") is not None

pytestmark = pytest.mark.skipif(
    not (fastembed_available and lancedb_available),
    reason="search extra (requirements-search.lock) is not installed in this environment",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_ROOT = REPO_ROOT / ".tmp" / "search-validation" / "runtime-probes"
ARTIFACT_PATH = REPO_ROOT / ".tmp" / "search-validation" / "runtime-api-probe.json"

SEARCH_EXTRA_PACKAGES = (
    "fastembed",
    "lancedb",
    "pyarrow",
    "loguru",
    "mmh3",
    "py-rust-stemmers",
    "deprecation",
    "lance-namespace",
    "lance-namespace-urllib3-client",
)

STAMP = datetime(2026, 7, 26, 20, 0, 0)


@pytest.fixture()
def probe_directory() -> Iterator[Path]:
    directory = PROBE_ROOT / f"probe-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _search_lock_pin(package: str) -> str:
    for line in (REPO_ROOT / "requirements-search.lock").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{package}=="):
            return line.split("==", 1)[1].split()[0]
    raise AssertionError(f"{package} is not pinned in requirements-search.lock")


def _probe_records() -> list[dict[str, object]]:
    return [
        {
            "chunk_id": f"chunk-{number}",
            "vector": [float(number), 1.0],
            "vector_sha256": vector_sha256([float(number), 1.0], dimensions=2),
            "dimensions": 2,
            "input_sha256": f"{number}" * 64,
            "manifest_id": "manifest-probe",
            "issuer_id": "issuer-probe",
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


class _FixedEncoder:
    """Deterministic 2-dimensional passage/query encoder for API probes."""

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(text))] for text in texts]


def _recursive_listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_installed_search_runtime_matches_the_hash_locked_pins() -> None:
    """The active environment serves exactly the lock's pinned versions."""
    for package in SEARCH_EXTRA_PACKAGES:
        installed = importlib.metadata.version(package)
        pinned = _search_lock_pin(package)
        assert installed == pinned, f"{package}: installed {installed} != lock pin {pinned}"
    # The compatible shared pin is preserved by the search lock, not replaced.
    assert importlib.metadata.version("onnxruntime") == _search_lock_pin("onnxruntime")
    assert importlib.metadata.version("onnxruntime") == "1.28.0"


def test_installed_fastembed_consumes_local_files_only_through_kwargs(
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed fastembed must accept local_files_only via **kwargs and fail
    closed on an empty cache instead of downloading."""
    import fastembed

    parameters = inspect.signature(fastembed.TextEmbedding).parameters
    assert {"model_name", "cache_dir", "providers"}.issubset(parameters)
    assert "local_files_only" not in parameters, (
        "fastembed 0.9.0 contract: local_files_only arrives through **kwargs"
    )
    var_keyword = [
        parameter.name
        for parameter in parameters.values()
        if parameter.kind is inspect.Parameter.VAR_KEYWORD
    ]
    assert var_keyword == ["kwargs"]

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cache = probe_directory / "cache"
    cache.mkdir()
    before = _recursive_listing(cache)
    with pytest.raises(ValueError, match=r"Could not load model .* from any source"):
        fastembed.TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            cache_dir=str(cache),
            local_files_only=True,
            providers=["CPUExecutionProvider"],
        )
    assert _recursive_listing(cache) == before, (
        "offline construction must not download or stage any model file"
    )


def test_production_encoder_guard_accepts_the_installed_fastembed_signature(
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastEmbedEncoder.from_spec passes the real 0.9.0 signature guard and fails
    closed only at the offline model load, never at the API check."""
    from search.embedding_runtime_artifact import RuntimeArtifactSource, build_runtime_artifact
    from search.local_vector import EmbeddingModelSpec, FastEmbedEncoder, LocalVectorCapabilityError

    runtime_root = probe_directory / "runtime"
    runtime_root.mkdir()
    (runtime_root / "model.onnx").write_bytes(b"probe-model")
    (runtime_root / "tokenizer.json").write_bytes(b"probe-tokenizer")
    sources = [
        RuntimeArtifactSource(
            logical_name="model.onnx",
            role="model",
            relative_path=Path("model.onnx"),
        ),
        RuntimeArtifactSource(
            logical_name="tokenizer.json",
            role="tokenizer",
            relative_path=Path("tokenizer.json"),
        ),
    ]
    artifact = build_runtime_artifact(
        runtime_root,
        sources,
        provider="fastembed",
        model="BAAI/bge-small-en-v1.5",
        dimensions=2,
        execution_provider="CPUExecutionProvider",
        execution_settings={"threads": 1},
        component_versions={
            "fastembed": importlib.metadata.version("fastembed"),
            "onnxruntime": importlib.metadata.version("onnxruntime"),
        },
    )
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    before = _recursive_listing(runtime_root)
    with pytest.raises(LocalVectorCapabilityError, match="initialization failed closed") as excinfo:
        FastEmbedEncoder.from_spec(
            EmbeddingModelSpec(
                provider="fastembed",
                model="BAAI/bge-small-en-v1.5",
                dimensions=2,
            ),
            runtime_artifact=artifact,
            runtime_root=runtime_root,
        )
    assert "cannot prove offline" not in str(excinfo.value), (
        "the guard must accept the installed 0.9.0 kwargs signature"
    )
    assert _recursive_listing(runtime_root) == before


def test_installed_lancedb_round_trips_the_production_index(
    probe_directory: Path,
) -> None:
    """The production LanceVectorIndex works end to end against installed
    lancedb 0.40.0: stage, bounded read, plain scan, count, publish."""
    from search.local_vector import LanceVectorIndex, VectorBatchReceipt, vector_records_digest

    records = _probe_records()
    index = LanceVectorIndex(probe_directory / "vectors")
    index.stage_batch("probe-run", records[:1])
    index.stage_batch("probe-run", records[1:])
    receipt = VectorBatchReceipt(
        batch_number=1,
        first_chunk_id="chunk-1",
        last_chunk_id="chunk-2",
        chunk_count=2,
        records_sha256=vector_records_digest(records),
    )
    assert index.verify_batch("probe-run", receipt) is True
    assert index.count_records("probe-run") == 2
    bounded = index.read_batch("probe-run", receipt)
    assert [str(row["chunk_id"]) for row in bounded] == ["chunk-1", "chunk-2"]
    projection = index.read_projection("probe-run", expected_count=2)
    assert [str(row["chunk_id"]) for row in projection] == ["chunk-1", "chunk-2"]
    uri = index.publish("probe-run")
    assert index.published_storage_uri("probe-run") == uri
    assert uri.startswith("lance://")
    assert uri.endswith("#evidence_chunks")
    table = index.open_table("probe-run")
    assert not hasattr(table, "query"), "lancedb 0.40.0 sync tables must not expose query()"


def test_installed_lancedb_serves_the_backend_vector_search_shape(
    probe_directory: Path,
) -> None:
    """LanceVectorBackend's vector-search chain (search(vector) with prefilter)
    behaves against the installed lancedb 0.40.0."""
    from search.grounded import SearchFilter
    from search.local_vector import (
        LanceVectorBackend,
        LanceVectorIndex,
        VectorBatchReceipt,
        vector_records_digest,
    )

    records = _probe_records()
    index = LanceVectorIndex(probe_directory / "vectors")
    index.stage_batch("probe-run", records[:1])
    index.stage_batch("probe-run", records[1:])
    receipt = VectorBatchReceipt(
        batch_number=1,
        first_chunk_id="chunk-1",
        last_chunk_id="chunk-2",
        chunk_count=2,
        records_sha256=vector_records_digest(records),
    )
    assert index.verify_batch("probe-run", receipt) is True
    index.publish("probe-run")
    backend = LanceVectorBackend(
        index,
        index_run_id="probe-run",
        manifest_id="manifest-probe",
        encoder=_FixedEncoder(),
        dimensions=2,
    )
    candidates = backend.search("revenue", SearchFilter(), 2)
    assert [candidate.chunk_id for candidate in candidates] == ["chunk-1", "chunk-2"]
    assert all(candidate.index_run_id == "probe-run" for candidate in candidates)


def test_probe_records_machine_readable_artifact(
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave the runtime-probe receipt under .tmp/search-validation/ for the
    mission's evidence trail."""
    import fastembed
    import lancedb

    parameters = inspect.signature(fastembed.TextEmbedding).parameters
    database = lancedb.connect(str(probe_directory / "scan"))
    database.create_table("evidence_chunks", data=_probe_records())
    builder = database.open_table("evidence_chunks").search(None)
    scan_rows = cast("list[dict[str, object]]", builder.limit(10).to_list())
    rows_from_search_none = [str(row["chunk_id"]) for row in scan_rows]
    local_files_only_named = "local_files_only" in parameters
    var_keyword = "kwargs" in parameters
    sync_table_has_query = hasattr(database, "query")
    plain_scan_builder = type(builder).__name__
    where_signature = str(inspect.signature(type(builder).where))

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    cache = probe_directory / "cache"
    cache.mkdir()
    offline_outcome = "unexpected-success"
    try:
        fastembed.TextEmbedding(
            model_name="BAAI/bge-small-en-v1.5",
            cache_dir=str(cache),
            local_files_only=True,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # record the exact fail-closed error for the receipt
        offline_outcome = f"{type(exc).__name__}: {exc}"

    fastembed_signature: dict[str, object] = {
        "named_parameters": list(parameters),
        "local_files_only_named": local_files_only_named,
        "var_keyword": var_keyword,
    }
    fastembed_offline_probe: dict[str, object] = {
        "model": "BAAI/bge-small-en-v1.5",
        "hf_hub_offline": "1",
        "local_files_only": True,
        "outcome": offline_outcome,
        "cache_files_after": _recursive_listing(cache),
    }
    lancedb_scan_probe: dict[str, object] = {
        "sync_table_has_query": sync_table_has_query,
        "plain_scan_builder": plain_scan_builder,
        "where_signature": where_signature,
        "rows_from_search_none": rows_from_search_none,
    }
    payload: dict[str, object] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "purpose": (
            "Runtime API compatibility probe for M4 (VAL-SEARCH-012). Compatibility "
            "evidence only: no model download, no corpus, no database, no "
            "retrieval-quality claim, no promotion."
        ),
        "installed_versions": {
            package: importlib.metadata.version(package) for package in SEARCH_EXTRA_PACKAGES
        },
        "onnxruntime": importlib.metadata.version("onnxruntime"),
        "fastembed_signature": fastembed_signature,
        "fastembed_offline_probe": fastembed_offline_probe,
        "lancedb_scan_probe": lancedb_scan_probe,
    }
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert local_files_only_named is False
    assert var_keyword is True
    assert sync_table_has_query is False
    assert plain_scan_builder == "LanceEmptyQueryBuilder"
    assert offline_outcome != "unexpected-success"
    assert rows_from_search_none == ["chunk-1", "chunk-2"]
