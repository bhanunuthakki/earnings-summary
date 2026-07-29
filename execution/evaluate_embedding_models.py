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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from search.embedding_eval import (  # noqa: E402
    DEFAULT_CANDIDATES,
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
from search.local_vector import (  # noqa: E402
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LanceVectorBackend,
    LanceVectorIndex,
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
    if missing:
        raise ValueError(f"required candidates missing: {', '.join(missing)}")
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
                )
            )
        artifact = evaluate_embedding_candidates(
            cases,
            retrievers,
            k=args.k,
            thresholds=EvalThresholds(minimum_cases=args.minimum_cases),
            golden_digest=golden_sha256(args.golden),
            runtime_artifact_sha256={
                model: artifact.sha256() for model, artifact in runtime_artifacts.items()
            },
        )
        _log("embedding_evaluation_finished", recommended_model=artifact.recommended_model)
        payload = artifact.model_dump_json()
        _write_atomic(args.output, payload)
        sys.stdout.write(
            json.dumps(
                {
                    "outcome": "applied",
                    "output": str(args.output.resolve()),
                    "artifact_sha256": hashlib.sha256(f"{payload}\n".encode()).hexdigest(),
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
