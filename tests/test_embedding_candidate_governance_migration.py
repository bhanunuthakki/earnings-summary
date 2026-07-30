from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from execution.plan_embedding_cutover import main as plan_main
from search.embedding_eval import EmbeddingRecommendationArtifact
from search.embedding_evaluation_receipt import (
    persist_evaluation_receipt,
    receipt_from_evaluation,
)
from search.embedding_promotion import current_promotion
from search.embedding_runtime_artifact import (
    EmbeddingRuntimeArtifact,
    RuntimeArtifactFile,
    RuntimeComponentVersion,
)
from search.embedding_runtime_registration import (
    persist_runtime_registration,
    register_embedding_governance_functions,
    registration_from_artifact,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEAD = "0257_embedding_candidate_governance"


def _upgrade(path: Path) -> None:
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
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
        legacy.commit()
    finally:
        legacy.close()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)


def _connect_at_0257(path: Path) -> sqlite3.Connection:
    """Open the intentionally intermediate migration state under writer policy."""

    return connect_sqlite(
        path,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=False,
    )


def _artifact() -> EmbeddingRuntimeArtifact:
    return EmbeddingRuntimeArtifact(
        provider="fastembed",
        model="BAAI/bge-small-en-v1.5",
        dimensions=384,
        execution_provider="CPUExecutionProvider",
        execution_settings=(),
        component_versions=(RuntimeComponentVersion(component="fastembed", version="1.2.3"),),
        files=(
            RuntimeArtifactFile(
                logical_name="model/model.onnx",
                role="model",
                size_bytes=3,
                sha256="a" * 64,
            ),
        ),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _seed_evaluation_support(conn: sqlite3.Connection) -> list[dict[str, object]]:
    conn.execute("PRAGMA foreign_keys=OFF")
    for trigger in (
        "trg_embedding_runtime_registration_exact",
        "trg_search_projection_seals_run_contract",
        "trg_search_projection_seals_chunk_count",
        "trg_search_projection_seals_vector_coverage",
        "trg_search_projection_seals_lexical_coverage",
        "trg_vector_seals_runtime_binding",
    ):
        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
    models = ("BAAI/bge-base-en-v1.5", "BAAI/bge-small-en-v1.5")
    coordinates: list[dict[str, object]] = []
    for ordinal, model in enumerate(models, start=1):
        runtime_sha = f"{ordinal:x}" * 64
        runtime_id = f"runtime-{ordinal}"
        seal_id = f"seal-{ordinal}"
        conn.execute(
            "INSERT INTO search_embedding_runtime_registrations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                runtime_id,
                runtime_id,
                "evidence_vector_retrieval",
                "fastembed",
                model,
                384 if "small" in model else 768,
                "{}",
                runtime_sha,
                "2026-07-29T10:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO search_projection_seals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seal_id,
                seal_id,
                f"run-{ordinal}",
                "manifest-1",
                "vector",
                30,
                "a" * 64,
                f"{ordinal + 2:x}" * 64,
                f"{ordinal + 4:x}" * 64,
                "fastembed",
                model,
                384 if "small" in model else 768,
                f"{ordinal + 6:x}" * 64,
                f"lance://sealed/run-{ordinal}#evidence_chunks",
                "2026-07-29T10:00:00Z",
                runtime_sha,
            ),
        )
        coordinates.append(
            {
                "artifact_set_sha256": f"{ordinal + 4:x}" * 64,
                "chunk_count": 30,
                "chunk_set_sha256": "a" * 64,
                "config_sha256": f"{ordinal + 6:x}" * 64,
                "index_run_id": f"run-{ordinal}",
                "manifest_id": "manifest-1",
                "model": model,
                "projection_records_sha256": f"{ordinal + 2:x}" * 64,
                "projection_seal_id": seal_id,
                "runtime_artifact_sha256": runtime_sha,
                "runtime_registration_id": runtime_id,
                "sealed_at": "2026-07-29T10:00:00Z",
            }
        )
    return coordinates


def test_0257_registration_is_inert_idempotent_and_sql_hardened(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "candidate-governance.db"
    _upgrade(database)
    conn = _connect_at_0257(database)
    try:
        registration = registration_from_artifact(
            _artifact(),
            registered_at=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
        )
        assert persist_runtime_registration(conn, registration)
        assert not persist_runtime_registration(conn, registration)
        assert current_promotion(conn) is None
        malformed = registration.model_copy(update={"model": "wrong"})
        with pytest.raises(sqlite3.IntegrityError, match="digest mismatch"):
            conn.execute(
                "INSERT INTO search_embedding_runtime_registrations VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(getattr(malformed, field) for field in malformed.__class__.model_fields),
            )
        forged = "{}"
        forged_sha = hashlib.sha256(forged.encode()).hexdigest()
        with pytest.raises(sqlite3.IntegrityError, match="receipt mismatch"):
            conn.execute(
                "INSERT INTO search_embedding_evaluation_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"embedding-evaluation:{forged_sha}",
                    f"embedding-evaluation:{forged_sha}",
                    "evidence_vector_retrieval",
                    "b" * 64,
                    forged,
                    forged_sha,
                    forged,
                    forged_sha,
                    "2026-07-29T10:00:00Z",
                ),
            )
    finally:
        conn.close()

    assert plan_main(["--db", str(database), "--manifest-id", "manifest-1"]) == 0
    assert '"state": "runtime_registration_required"' in capsys.readouterr().out


def test_0257_sql_rejects_duplicate_result_coordinate_raw_insert(
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate-duplicate-result.db"
    _upgrade(database)
    conn = _connect_at_0257(database)
    try:
        register_embedding_governance_functions(conn)
        coordinates = _seed_evaluation_support(conn)
        valid_results = [
            {
                "case_count": 30,
                "coverage": 1.0,
                "mean_latency_ms": 25.0,
                "model": coordinate["model"],
                "mrr": 0.8,
                "ndcg": 0.8,
                "recall_at_k": 0.8,
                "runtime_artifact_sha256": coordinate["runtime_artifact_sha256"],
            }
            for coordinate in coordinates
        ]
        valid_artifact = EmbeddingRecommendationArtifact.model_validate(
            {
                "candidate_coordinates": coordinates,
                "evaluated_at": "2026-07-29T10:00:00Z",
                "golden_sha256": "b" * 64,
                "k": 10,
                "purpose": "evidence_vector_retrieval",
                "reason": "valid exact result-to-candidate bijection",
                "recommended_model": None,
                "results": valid_results,
                "thresholds": {
                    "max_mean_latency_ms": 1500.0,
                    "min_mrr": 0.65,
                    "min_ndcg": 0.7,
                    "min_recall_at_k": 0.75,
                    "minimum_cases": 30,
                    "parity_tolerance": 0.02,
                },
            }
        )
        assert persist_evaluation_receipt(
            conn,
            receipt_from_evaluation(
                valid_artifact,
                evaluated_at=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
            ),
        )
        duplicated_result = {
            "case_count": 30,
            "coverage": 1.0,
            "mean_latency_ms": 25.0,
            "model": coordinates[0]["model"],
            "mrr": 0.8,
            "ndcg": 0.8,
            "recall_at_k": 0.8,
            "runtime_artifact_sha256": coordinates[0]["runtime_artifact_sha256"],
        }
        artifact = {
            "candidate_coordinates": coordinates,
            "evaluated_at": "2026-07-29T10:00:00Z",
            "golden_sha256": "b" * 64,
            "k": 10,
            "purpose": "evidence_vector_retrieval",
            "reason": "near-valid raw receipt with a duplicated metric coordinate",
            "recommended_model": None,
            "results": [duplicated_result, duplicated_result],
            "thresholds": {
                "max_mean_latency_ms": 1500.0,
                "min_mrr": 0.65,
                "min_ndcg": 0.7,
                "min_recall_at_k": 0.75,
                "minimum_cases": 30,
                "parity_tolerance": 0.02,
            },
        }
        artifact_json = _canonical_json(artifact)
        artifact_sha = hashlib.sha256(artifact_json.encode()).hexdigest()
        candidate_json = _canonical_json(coordinates)
        candidate_sha = hashlib.sha256(candidate_json.encode()).hexdigest()
        receipt_id = f"embedding-evaluation:{artifact_sha}"
        with pytest.raises(sqlite3.IntegrityError, match="receipt mismatch"):
            conn.execute(
                "INSERT INTO search_embedding_evaluation_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    receipt_id,
                    "evidence_vector_retrieval",
                    "b" * 64,
                    artifact_json,
                    artifact_sha,
                    candidate_json,
                    candidate_sha,
                    "2026-07-29T10:00:00Z",
                ),
            )
    finally:
        conn.close()


def test_0257_downgrade_fails_closed_with_candidate_history(tmp_path: Path) -> None:
    database = tmp_path / "candidate-downgrade.db"
    _upgrade(database)
    conn = _connect_at_0257(database)
    try:
        registration = registration_from_artifact(
            _artifact(),
            registered_at=datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
        )
        persist_runtime_registration(conn, registration)
        conn.commit()
    finally:
        conn.close()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    with pytest.raises(RuntimeError, match="would orphan"):
        command.downgrade(config, "0256_population_cutover_receipts")
