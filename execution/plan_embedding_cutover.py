"""Plan the next deterministic embedding cutover step without mutation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from search.embedding_eval import (  # noqa: E402
    DEFAULT_CANDIDATES,
    EmbeddingRecommendationArtifact,
)
from search.embedding_promotion import current_promotion  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    manifest_id: str,
    models: tuple[str, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for model in models:
        registration_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM search_embedding_runtime_registrations "
                "WHERE purpose='evidence_vector_retrieval' AND model=?",
                (model,),
            ).fetchone()[0]
        )
        sealed = conn.execute(
            "SELECT runtime.runtime_registration_id,seal.projection_seal_id,"
            "seal.index_run_id,seal.chunk_count,seal.chunk_set_sha256 "
            "FROM search_embedding_runtime_registrations runtime "
            "JOIN search_projection_seals seal ON seal.provider=runtime.provider "
            "AND seal.model=runtime.model AND seal.dimensions=runtime.dimensions "
            "AND seal.runtime_artifact_sha256=runtime.runtime_artifact_sha256 "
            "WHERE runtime.purpose='evidence_vector_retrieval' AND runtime.model=? "
            "AND seal.manifest_id=? AND seal.index_kind='vector' "
            "ORDER BY seal.sealed_at DESC,seal.projection_seal_id DESC LIMIT 1",
            (model, manifest_id),
        ).fetchone()
        latest = conn.execute(
            "SELECT runtime_registration_id,runtime_artifact_sha256 "
            "FROM search_embedding_runtime_registrations "
            "WHERE purpose='evidence_vector_retrieval' AND model=? "
            "ORDER BY registered_at DESC,runtime_registration_id DESC LIMIT 1",
            (model,),
        ).fetchone()
        rows.append(
            {
                "model": model,
                "runtime_registration_id": (
                    None
                    if registration_count == 0
                    else str(sealed[0] if sealed is not None else latest[0])
                ),
                "projection_seal_id": None if sealed is None else str(sealed[1]),
                "index_run_id": None if sealed is None else str(sealed[2]),
                "chunk_count": None if sealed is None else int(sealed[3]),
                "chunk_set_sha256": None if sealed is None else str(sealed[4]),
            }
        )
    return rows


def _matching_receipt_ids(
    conn: sqlite3.Connection,
    candidates: list[dict[str, object]],
) -> set[str]:
    expected = {(str(item["model"]), str(item["projection_seal_id"])) for item in candidates}
    matches: set[str] = set()
    for row in conn.execute(
        "SELECT evaluation_receipt_id,evaluation_artifact_json "
        "FROM search_embedding_evaluation_receipts"
    ):
        artifact = EmbeddingRecommendationArtifact.model_validate_json(
            str(row["evaluation_artifact_json"])
        )
        coordinates = {
            (item.model, item.projection_seal_id) for item in artifact.candidate_coordinates
        }
        if coordinates == expected:
            matches.add(str(row["evaluation_receipt_id"]))
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest-id", required=True)
    parser.add_argument("--model", action="append")
    args = parser.parse_args(argv)
    models = tuple(sorted(args.model or DEFAULT_CANDIDATES))
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        candidates = _candidate_rows(conn, manifest_id=args.manifest_id, models=models)
        if any(item["runtime_registration_id"] is None for item in candidates):
            state = "runtime_registration_required"
        elif any(item["projection_seal_id"] is None for item in candidates):
            state = "candidate_vector_required"
        else:
            receipt_ids = _matching_receipt_ids(conn, candidates)
            promotion = current_promotion(conn)
            if not receipt_ids:
                state = "evaluation_required"
            elif promotion is None or promotion.evaluation_receipt_id not in receipt_ids:
                state = "approval_required"
            else:
                state = "ready"
        sys.stdout.write(
            json.dumps(
                {
                    "mode": "dry_run",
                    "manifest_id": args.manifest_id,
                    "state": state,
                    "candidates": candidates,
                    "routing_changed": False,
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
