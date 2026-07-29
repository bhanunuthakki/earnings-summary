"""Generate the reviewable, committed production Ask scope registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ask.audit_store import canonical_json  # noqa: E402
from ask.sealed_retrieval import derive_production_scope_registry  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

DEFAULT_DRY_RUN_OUTPUT = REPO_ROOT / ".tmp" / "ask_retrieval_production_scopes.json"
PRODUCTION_OUTPUT = REPO_ROOT / "config" / "ask_retrieval_production_scopes.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Derive the frozen core operating-company Ask cohort from canonical "
            "issuer, reporting-entity, and listing registries."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_DRY_RUN_OUTPUT)
    parser.add_argument("--apply", action="store_true")
    return parser


_derive_registry = derive_production_scope_registry


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.resolve()
    if args.apply:
        if output != PRODUCTION_OUTPUT.resolve():
            raise SystemExit(
                "--apply may write only config/ask_retrieval_production_scopes.json"
            )
    elif output != DEFAULT_DRY_RUN_OUTPUT.resolve():
        raise SystemExit("dry-run output is fixed under .tmp for review")
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        registry = _derive_registry(conn)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"production scope derivation failed: {exc}") from exc
    finally:
        conn.close()
    scopes = registry["scopes"]
    if not isinstance(scopes, list):
        raise RuntimeError("generated registry scopes are not a list")
    scope_payloads = cast(list[object], scopes)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(f".{output.name}.tmp")
    try:
        staged.write_text(canonical_json(registry) + "\n", encoding="utf-8")
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "applied": bool(args.apply),
                "output": str(output),
                "registry_sha256": registry["registry_sha256"],
                "scope_count": len(scope_payloads),
                "scope_set_sha256": registry["scope_set_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
