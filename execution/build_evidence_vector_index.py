"""Build one opt-in, immutable local vector projection for a sealed corpus.

The default is read-only planning.  ``--apply`` is required before FastEmbed or
LanceDB are loaded, before a model can be downloaded, or before an external
index directory / SQLite ledger row is created.  stdout is one JSON result;
stderr carries JSONL progress only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from search.embedding_runtime_artifact import load_runtime_artifact  # noqa: E402
from search.local_vector import (  # noqa: E402
    EmbeddingModelSpec,
    FastEmbedEncoder,
    LanceVectorIndex,
    LocalVectorCapabilityError,
    PassageQueryEncoder,
    ResumableVectorIndexBuilder,
    VectorBuildCheckpointStore,
    VectorBuildRequest,
    VectorBuildResult,
    count_documents_for_manifest,
    document_batches_for_manifest,
    request_config_sha256,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _log(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _checkpoint_path(index_run_id: str, checkpoint_root: Path | None = None) -> Path:
    digest = hashlib.sha256(index_run_id.encode()).hexdigest()
    root = (
        PROJECT_ROOT / ".tmp" / "evidence-vector-index"
        if checkpoint_root is None
        else checkpoint_root
    )
    return root / digest / "state.json"


class _UnavailableEncoder:
    """Routes an explicit capability failure through the immutable run ledger."""

    def __init__(self, error: LocalVectorCapabilityError) -> None:
        self._error = error

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        raise self._error

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        raise self._error


def _require_complete_manifest(conn: sqlite3.Connection, manifest_id: str) -> None:
    row = conn.execute(
        "SELECT completion_status FROM search_corpus_manifest_seals WHERE manifest_id = ?",
        (manifest_id,),
    ).fetchone()
    if row is None or str(row[0]) != "complete":
        raise ValueError("vector indexing requires a sealed complete corpus manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--index-run-id", required=True)
    parser.add_argument("--index-key", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimensions", type=int, required=True)
    parser.add_argument("--provider", default="fastembed")
    parser.add_argument("--runtime-artifact", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--index-root", type=Path, required=True)
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        help="Resume-state root (defaults to repo .tmp/evidence-vector-index)",
    )
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply:
        try:
            with JobLock(
                PROJECT_ROOT,
                "build-evidence-vector-index",
                [
                    "portfolio-db",
                    f"sqlite:{args.db.resolve()}",
                    f"vector-index:{args.index_root.resolve()}",
                    f"vector-checkpoint:{args.index_run_id}",
                ],
            ):
                return _run(args)
        except JobAlreadyRunningError as exc:
            _log("vector_index_locked", detail=str(exc))
            return 75
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    role = SQLiteConnectionRole.WRITER if args.apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(args.db, role=role, schema_preflight=args.apply)
    try:
        _require_complete_manifest(conn, args.manifest_id)
        runtime_artifact = load_runtime_artifact(args.runtime_artifact)
        if (
            runtime_artifact.provider,
            runtime_artifact.model,
            runtime_artifact.dimensions,
        ) != (args.provider, args.model, args.dimensions):
            raise ValueError("requested model differs from runtime artifact")
        total_documents = count_documents_for_manifest(conn, args.manifest_id)
        config = {
            "manifest_id": args.manifest_id,
            "index_key": args.index_key,
            "revision": args.revision,
            "model": args.model,
            "dimensions": args.dimensions,
            "provider": args.provider,
            "batch_size": args.batch_size,
            "runtime_artifact_sha256": runtime_artifact.sha256(),
        }
        if not args.apply:
            sys.stdout.write(
                json.dumps(
                    {
                        "outcome": "dry_run",
                        "chunk_count": total_documents,
                        "index_run_id": args.index_run_id,
                        "config_sha256": request_config_sha256(config),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 0
        request = VectorBuildRequest(
            index_run_id=args.index_run_id,
            index_key=args.index_key,
            revision=args.revision,
            manifest_id=args.manifest_id,
            code_version="build_evidence_vector_index@2",
            request_config_sha256=request_config_sha256(config),
            model=EmbeddingModelSpec(
                provider=args.provider, model=args.model, dimensions=args.dimensions
            ),
            runtime_artifact=runtime_artifact,
            batch_size=args.batch_size,
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        checkpoint = _checkpoint_path(args.index_run_id, args.checkpoint_root)

        def on_batch_complete(completed: int, total: int) -> None:
            _log("embedding_batch_completed", completed_chunks=completed, total_chunks=total)

        try:
            encoder: PassageQueryEncoder = FastEmbedEncoder.from_spec(
                request.model,
                runtime_artifact=request.runtime_artifact,
                runtime_root=args.runtime_root,
            )
        except LocalVectorCapabilityError as exc:
            encoder = _UnavailableEncoder(exc)
        builder = ResumableVectorIndexBuilder(
            conn,
            encoder,
            LanceVectorIndex(args.index_root),
            VectorBuildCheckpointStore(checkpoint),
        )
        try:
            result = builder.build(
                request,
                total_documents=total_documents,
                document_batches=lambda after: document_batches_for_manifest(
                    conn,
                    args.manifest_id,
                    batch_size=request.batch_size,
                    after_chunk_id=after,
                ),
                on_batch_complete=on_batch_complete,
            )
        except (LocalVectorCapabilityError, ValueError) as exc:
            result = VectorBuildResult(
                index_run_id=request.index_run_id,
                outcome="failed",
                created=False,
                chunk_count=total_documents,
                failure_reason=type(exc).__name__,
            )
        _log("vector_index_finished", index_run_id=request.index_run_id, outcome=result.outcome)
        sys.stdout.write(result.model_dump_json() + "\n")
        return 0 if result.outcome == "succeeded" else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
