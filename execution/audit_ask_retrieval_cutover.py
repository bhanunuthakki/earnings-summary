"""Audit all current Ask retrieval promotions without mutating the database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ask.audit_store import (  # noqa: E402
    audit_answer_audit_integrity,
    canonical_json,
    digest_text,
)
from ask.sealed_retrieval import (  # noqa: E402
    PRODUCTION_SCOPE_SCHEMA_VERSION,
    RetrievalScope,
    assess_retrieval_readiness,
    derive_production_scope_registry,
)
from search.embedding_promotion import LocalVectorRuntimeConfig  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

PRODUCTION_SCOPE_REGISTRY = REPO_ROOT / "config" / "ask_retrieval_production_scopes.json"

RegistryArtifactSnapshot = tuple[int, int, int, int, int, str]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail closed unless every requested Ask scope is production-ready."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--scope-set-sha256", required=True)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    return parser


def _load_authoritative_scopes(
    *,
    registry_path: Path,
    expected_sha256: str,
    registry_bytes: bytes | None = None,
) -> tuple[RetrievalScope, ...]:
    if registry_path.resolve() != PRODUCTION_SCOPE_REGISTRY.resolve():
        raise SystemExit("cutover must use the committed production scope registry")
    try:
        payload_bytes = (
            _read_registry_artifact(registry_path)[1] if registry_bytes is None else registry_bytes
        )
        decoded = json.loads(payload_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"production scope registry is unavailable: {exc}") from exc
    if not isinstance(decoded, dict):
        raise SystemExit("production scope registry must be a JSON object")
    payload = cast(dict[str, object], decoded)
    if payload.get("registry_id") != "ask-retrieval-production-scopes":
        raise SystemExit("production scope registry identity is invalid")
    if payload.get("schema_version") != PRODUCTION_SCOPE_SCHEMA_VERSION or payload.get(
        "supported_cohort"
    ) != ["operating_company:legal_registrant"]:
        raise SystemExit("production scope registry cohort contract is invalid")
    stored_registry_sha256 = payload.get("registry_sha256")
    registry_core = {key: value for key, value in payload.items() if key != "registry_sha256"}
    if (
        not isinstance(stored_registry_sha256, str)
        or digest_text(canonical_json(registry_core)) != stored_registry_sha256
    ):
        raise SystemExit("production scope registry commitment mismatch")
    raw_scopes = payload.get("scopes")
    committed_sha256 = payload.get("scope_set_sha256")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise SystemExit("production scope registry must contain at least one scope")
    scope_payloads = cast(list[object], raw_scopes)
    try:
        scopes = tuple(RetrievalScope.model_validate(item) for item in scope_payloads)
    except ValueError as exc:
        raise SystemExit(f"production scope registry is invalid: {exc}") from exc
    if tuple(sorted(scopes, key=lambda item: item.scope_id)) != scopes:
        raise SystemExit("production scopes must be sorted by scope_id")
    canonical = canonical_json([item.model_dump(mode="json") for item in scopes])
    computed_sha256 = digest_text(canonical)
    if (
        not isinstance(committed_sha256, str)
        or committed_sha256 != computed_sha256
        or expected_sha256 != computed_sha256
    ):
        raise SystemExit("production scope-set commitment mismatch")
    return scopes


def _read_registry_artifact(path: Path) -> tuple[RegistryArtifactSnapshot, bytes]:
    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise SystemExit(f"production scope registry is unavailable: {exc}") from exc
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        raise SystemExit("production scope registry changed while being read")
    return (*after_identity, hashlib.sha256(payload).hexdigest()), payload


def _verify_registry_against_live(
    conn: sqlite3.Connection,
    *,
    registry_payload: object,
) -> None:
    try:
        derived = derive_production_scope_registry(conn)
    except ValueError as exc:
        raise SystemExit(f"production scope registry re-derivation failed: {exc}") from exc
    if canonical_json(registry_payload) != canonical_json(derived):
        raise SystemExit(
            "production scope registry differs from the live frozen cohort/source revisions"
        )


def _verify_claim_audit_budget(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute(
            "SELECT hard_block,on_exceed FROM llm_budgets WHERE purpose='ask_claim_audit'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise SystemExit(f"ask_claim_audit budget governance is unavailable: {exc}") from exc
    if row is None or int(row[0]) != 1 or str(row[1]) != "block":
        raise SystemExit("ask_claim_audit budget must be configured hard_block/on_exceed=block")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        registry_before, registry_bytes = _read_registry_artifact(PRODUCTION_SCOPE_REGISTRY)
        scopes = _load_authoritative_scopes(
            registry_path=PRODUCTION_SCOPE_REGISTRY,
            expected_sha256=args.scope_set_sha256,
            registry_bytes=registry_bytes,
        )
        registry_payload: object = json.loads(registry_bytes)
        conn.execute("BEGIN")
        _verify_registry_against_live(
            conn,
            registry_payload=registry_payload,
        )
        _verify_claim_audit_budget(conn)
        integrity = audit_answer_audit_integrity(conn)
        if not integrity.ready:
            print(
                json.dumps(
                    {
                        "outcome": "unavailable",
                        "reason_code": "answer_audit_integrity_failed",
                        "details": integrity.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            return 2
        readiness = assess_retrieval_readiness(conn, scopes, runtime=runtime)
        registry_after, _ = _read_registry_artifact(PRODUCTION_SCOPE_REGISTRY)
        if registry_after != registry_before:
            raise SystemExit("production scope registry changed during cutover audit")
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    print(readiness.model_dump_json())
    return 0 if readiness.outcome == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
