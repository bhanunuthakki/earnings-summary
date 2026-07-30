"""Evaluate explicitly supplied local-vector index runs against a closed golden.

Default mode only validates and reports the planned comparison.  ``--apply``
opens the supplied immutable LanceDB runs and emits a recommendation artifact;
it never edits model routing or downloads/install dependencies unless the user
explicitly invokes that apply mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from provenance.search_index_lineage import (  # noqa: E402
    SearchProjectionSeal,
    load_projection_seal,
    verify_ledger_projection_seal,
)
from search.embedding_eval import (  # noqa: E402
    DEFAULT_CANDIDATES,
    CandidateEvaluationCoordinate,
    EvalThresholds,
    VectorEvalCase,
    VectorRetriever,
    evaluate_embedding_candidates,
    golden_sha256,
    load_embedding_golden,
)
from search.embedding_runtime_artifact import (  # noqa: E402
    EmbeddingRuntimeArtifact,
    load_runtime_artifact,
)
from search.embedding_runtime_registration import (  # noqa: E402
    load_runtime_registration,
)
from search.local_vector import (  # noqa: E402
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LanceVectorBackend,
    LanceVectorIndex,
    vector_records_digest,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _log(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _write_atomic(path: Path, payload: str) -> None:
    """Durably replace one explicitly requested evaluation artifact."""
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(resolved)
    finally:
        temporary.unlink(missing_ok=True)


def _candidate_map(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        model, separator, run_id = value.partition("=")
        if not separator or not model or not run_id:
            raise ValueError("--candidate must use MODEL=INDEX_RUN_ID")
        if model in parsed:
            raise ValueError(f"duplicate candidate model {model!r}")
        parsed[model] = run_id
    missing = [model for model in DEFAULT_CANDIDATES if model not in parsed]
    unexpected = [model for model in parsed if model not in DEFAULT_CANDIDATES]
    if missing or unexpected:
        raise ValueError(
            "candidates differ from governed policy"
            f"; missing={','.join(missing) or 'none'}"
            f"; unexpected={','.join(sorted(unexpected)) or 'none'}"
        )
    return parsed


def _path_map(values: list[str], *, option: str) -> dict[str, Path]:
    parsed: dict[str, Path] = {}
    for value in values:
        model, separator, raw_path = value.partition("=")
        if not separator or not model or not raw_path:
            raise ValueError(f"{option} must use MODEL=PATH")
        if model in parsed:
            raise ValueError(f"duplicate model for {option}")
        parsed[model] = Path(raw_path)
    return parsed


def _model_dimensions(conn: sqlite3.Connection, model: str, index_run_id: str) -> int:
    rows = conn.execute(
        "SELECT DISTINCT artifact.dimensions FROM search_embedding_artifacts AS artifact "
        "JOIN search_index_memberships AS membership ON membership.chunk_id = artifact.chunk_id "
        "WHERE membership.index_run_id = ? AND artifact.index_run_id = membership.index_run_id "
        "AND artifact.model = ? AND artifact.provider = 'fastembed' "
        "AND artifact.outcome = 'succeeded'",
        (index_run_id, model),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"index run {index_run_id!r} lacks one auditable FastEmbed dimension for {model}"
        )
    return int(rows[0][0])


def _model_runtime_sha(conn: sqlite3.Connection, model: str, index_run_id: str) -> str:
    rows = conn.execute(
        "SELECT DISTINCT runtime_artifact_sha256 FROM search_embedding_artifacts "
        "WHERE index_run_id = ? AND model = ? AND outcome = 'succeeded'",
        (index_run_id, model),
    ).fetchall()
    if len(rows) != 1 or rows[0][0] is None or len(str(rows[0][0])) != 64:
        raise ValueError("candidate run lacks one runtime artifact binding")
    return str(rows[0][0])


def _retriever(backend: LanceVectorBackend) -> VectorRetriever:
    def retrieve(case: VectorEvalCase, limit: int):
        return backend.search(case.query, case.filters, limit)

    return retrieve


def _candidate_coordinate(
    conn: sqlite3.Connection,
    *,
    seal: SearchProjectionSeal,
    model: str,
    runtime_artifact_sha256: str,
) -> CandidateEvaluationCoordinate:
    rows = conn.execute(
        "SELECT runtime_registration_id FROM search_embedding_runtime_registrations "
        "WHERE purpose='evidence_vector_retrieval' AND provider='fastembed' "
        "AND model=? AND dimensions=? AND runtime_artifact_sha256=?",
        (model, seal.dimensions, runtime_artifact_sha256),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("candidate runtime is not uniquely registered")
    registration_id = str(rows[0][0])
    registration = load_runtime_registration(conn, registration_id)
    if registration is None:
        raise ValueError("candidate runtime registration is absent")
    return CandidateEvaluationCoordinate(
        model=model,
        index_run_id=seal.index_run_id,
        manifest_id=seal.manifest_id,
        projection_seal_id=seal.projection_seal_id,
        projection_records_sha256=seal.projection_records_sha256,
        artifact_set_sha256=seal.artifact_set_sha256 or "",
        config_sha256=seal.config_sha256,
        chunk_count=seal.chunk_count,
        chunk_set_sha256=seal.chunk_set_sha256,
        runtime_registration_id=registration.runtime_registration_id,
        runtime_artifact_sha256=registration.runtime_artifact_sha256,
        sealed_at=seal.sealed_at,
    )


def _verified_projection_seal(
    conn: sqlite3.Connection,
    index: LanceVectorIndex,
    *,
    model: str,
    index_run_id: str,
    manifest_id: str,
    dimensions: int,
    runtime_artifact_sha256: str,
) -> SearchProjectionSeal:
    """Bind evaluation to the exact SQL ledger and published vector bytes."""

    seal = load_projection_seal(conn, index_run_id=index_run_id)
    if seal is None or seal.index_kind != "vector":
        raise ValueError("candidate index run lacks an immutable vector projection seal")
    if (
        seal.index_run_id,
        seal.manifest_id,
        seal.provider,
        seal.model,
        seal.dimensions,
        seal.runtime_artifact_sha256,
    ) != (
        index_run_id,
        manifest_id,
        "fastembed",
        model,
        dimensions,
        runtime_artifact_sha256,
    ):
        raise ValueError("candidate vector identity differs from its projection seal")
    verify_ledger_projection_seal(conn, seal)
    if index.published_storage_uri(index_run_id) != seal.storage_uri:
        raise ValueError("candidate index root does not contain the sealed vector projection")
    records = index.read_projection(index_run_id, expected_count=seal.chunk_count)
    if vector_records_digest(records) != seal.projection_records_sha256:
        raise ValueError("external vector projection bytes differ from their immutable seal")
    return seal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--index-root", type=Path, required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--runtime-artifact", action="append", required=True)
    parser.add_argument("--runtime-root", action="append", required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--minimum-cases", type=int, default=30)
    parser.add_argument(
        "--output",
        type=Path,
        help="Required with --apply; atomic destination for the recommendation artifact.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    candidates = _candidate_map(args.candidate)
    artifact_paths = _path_map(args.runtime_artifact, option="--runtime-artifact")
    runtime_roots = _path_map(args.runtime_root, option="--runtime-root")
    if set(candidates) != set(artifact_paths) or set(candidates) != set(runtime_roots):
        raise ValueError("every candidate requires one runtime artifact and runtime root")
    runtime_artifacts: dict[str, EmbeddingRuntimeArtifact] = {
        model: load_runtime_artifact(path) for model, path in artifact_paths.items()
    }
    cases = load_embedding_golden(args.golden)
    if not args.apply:
        sys.stdout.write(
            json.dumps(
                {
                    "outcome": "dry_run",
                    "case_count": len(cases),
                    "candidates": sorted(candidates),
                    "golden_sha256": golden_sha256(args.golden),
                    "runtime_artifact_sha256": {
                        model: runtime_artifacts[model].sha256()
                        for model in sorted(runtime_artifacts)
                    },
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    if args.output is None:
        parser.error("--output is required with --apply")
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        index = LanceVectorIndex(args.index_root)
        retrievers: dict[str, VectorRetriever] = {}
        coordinates: dict[str, CandidateEvaluationCoordinate] = {}
        for model, run_id in candidates.items():
            row = conn.execute(
                "SELECT manifest_id, outcome FROM search_index_runs WHERE index_run_id = ? "
                "AND index_kind = 'vector'",
                (run_id,),
            ).fetchone()
            if row is None or str(row[1]) != "succeeded":
                raise ValueError(f"candidate index run {run_id!r} is not a successful vector run")
            spec = EmbeddingModelSpec(
                provider="fastembed", model=model, dimensions=_model_dimensions(conn, model, run_id)
            )
            runtime_artifact = runtime_artifacts[model]
            if (
                runtime_artifact.provider,
                runtime_artifact.model,
                runtime_artifact.dimensions,
                runtime_artifact.sha256(),
            ) != (
                spec.provider,
                spec.model,
                spec.dimensions,
                _model_runtime_sha(conn, model, run_id),
            ):
                raise ValueError("candidate runtime descriptor differs from vector ledger")
            runtime_sha256 = runtime_artifact.sha256()
            seal = _verified_projection_seal(
                conn,
                index,
                model=model,
                index_run_id=run_id,
                manifest_id=str(row[0]),
                dimensions=spec.dimensions,
                runtime_artifact_sha256=runtime_sha256,
            )
            coordinates[model] = _candidate_coordinate(
                conn,
                seal=seal,
                model=model,
                runtime_artifact_sha256=runtime_sha256,
            )
            retrievers[model] = _retriever(
                LanceVectorBackend(
                    index,
                    index_run_id=run_id,
                    manifest_id=str(row[0]),
                    encoder=FastEmbedEncoder.from_spec(
                        spec,
                        runtime_artifact=runtime_artifact,
                        runtime_root=runtime_roots[model],
                    ),
                    dimensions=spec.dimensions,
                    ledger_conn=conn,
                    projection_seal=seal,
                )
            )
        manifest_ids = {item.manifest_id for item in coordinates.values()}
        if len(manifest_ids) != 1:
            raise ValueError("all embedding candidates must use one sealed corpus manifest")
        manifest_id = next(iter(manifest_ids))
        relevant_chunk_ids = {chunk_id for case in cases for chunk_id in case.relevant_chunk_ids}
        if relevant_chunk_ids:
            placeholders = ",".join("?" for _ in relevant_chunk_ids)
            covered = {
                str(row[0])
                for row in conn.execute(
                    "SELECT chunk_id FROM search_chunks "
                    f"WHERE manifest_id=? AND chunk_id IN ({placeholders})",  # nosec B608 -- placeholder count only
                    (manifest_id, *sorted(relevant_chunk_ids)),
                )
            }
            if covered != relevant_chunk_ids:
                raise ValueError("golden references chunks outside the candidate corpus")
        artifact = evaluate_embedding_candidates(
            cases,
            retrievers,
            k=args.k,
            thresholds=EvalThresholds(minimum_cases=args.minimum_cases),
            golden_digest=golden_sha256(args.golden),
            runtime_artifact_sha256={
                model: artifact.sha256() for model, artifact in runtime_artifacts.items()
            },
            candidate_coordinates=coordinates,
            evaluated_at=datetime.now(UTC),
        )
        _log("embedding_evaluation_finished", recommended_model=artifact.recommended_model)
        payload = artifact.canonical_json()
        _write_atomic(args.output, payload)
        sys.stdout.write(
            json.dumps(
                {
                    "outcome": "applied",
                    "output": str(args.output.resolve()),
                    "artifact_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    "recommended_model": artifact.recommended_model,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
