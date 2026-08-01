"""Validate and append one production Ask retrieval-scope promotion."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ask.sealed_retrieval import (  # noqa: E402
    PromotionVerificationError,
    RetrievalPromotion,
    current_verifier_identity,
    persist_retrieval_promotion_with_outcome,
)
from search.embedding_promotion import LocalVectorRuntimeConfig  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote one exact sealed Research Snapshot for production Ask."
    )
    parser.add_argument("--db", type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--print-verifier-identity",
        action="store_true",
        help="Print the current policy/verifier coordinates and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_verifier_identity:
        policy, name, version, code_sha, config_sha = current_verifier_identity()
        print(
            json.dumps(
                {
                    "policy_version": policy,
                    "verifier_name": name,
                    "verifier_version": version,
                    "verifier_code_sha256": code_sha,
                    "verifier_config_sha256": config_sha,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.db is None or args.spec is None:
        raise SystemExit("--db and --spec are required unless --print-verifier-identity is used")
    if (args.index_root is None) != (args.runtime_root is None):
        raise SystemExit("--index-root and --runtime-root must be supplied together")
    runtime = (
        LocalVectorRuntimeConfig(
            index_root=args.index_root,
            runtime_root=args.runtime_root,
        )
        if args.index_root is not None
        else None
    )
    promotion = RetrievalPromotion.model_validate_json(args.spec.read_text(encoding="utf-8"))
    conn = connect_sqlite(
        args.db,
        role=SQLiteConnectionRole.WRITER,
        schema_preflight=True,
    )
    try:
        if args.dry_run:
            from ask.sealed_retrieval import verify_retrieval_promotion

            verify_retrieval_promotion(conn, promotion, runtime=runtime)
            conn.rollback()
            outcome = "verified"
        else:
            result = persist_retrieval_promotion_with_outcome(conn, promotion, runtime=runtime)
            promotion = result.promotion
            outcome = result.outcome
            conn.commit()
    except PromotionVerificationError as exc:
        conn.rollback()
        print(
            json.dumps(
                {
                    "outcome": "blocked",
                    "reason_code": exc.reason_code,
                    "details": exc.details,
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "outcome": outcome,
                "promotion_id": promotion.promotion_id,
                "scope_id": promotion.scope_id,
                "source_scope_key": promotion.source_scope_key,
                "source_scope_revision_id": promotion.source_scope_revision_id,
                "revision": promotion.revision,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
