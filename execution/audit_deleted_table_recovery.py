"""Prove that cataloged deleted-table rows remain readable in a verified backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field


class RecoveryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    name: str
    object_type: str
    present: bool
    row_count: int
    exemption_reason: str | None = None


class RecoveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_db: str
    source_sha256: str
    source_revision: str
    integrity_check: str
    foreign_key_violations: int
    targets: list[RecoveryTarget] = Field(default_factory=list[RecoveryTarget])

    @property
    def verified(self) -> bool:
        return (
            self.integrity_check.lower() == "ok"
            and self.foreign_key_violations == 0
            and bool(self.targets)
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def audit(source_db: Path, catalog_path: Path) -> RecoveryReceipt:
    """Validate one backup and count every cataloged schema target."""
    catalog_raw = cast(object, json.loads(catalog_path.read_text(encoding="utf-8")))
    if not isinstance(catalog_raw, dict):
        raise ValueError("deletion catalog must contain a candidates list")
    catalog = cast("dict[str, object]", catalog_raw)
    candidates_raw = catalog.get("candidates")
    if not isinstance(candidates_raw, list):
        raise ValueError("deletion catalog must contain a candidates list")
    candidates = cast("list[object]", candidates_raw)

    requested: list[tuple[str, str, str | None]] = []
    for candidate_raw in candidates:
        if not isinstance(candidate_raw, dict):
            raise ValueError("deletion catalog candidate must be an object")
        raw_candidate = cast("dict[str, object]", candidate_raw)
        candidate_id = raw_candidate.get("id")
        schema_targets_raw = raw_candidate.get("schema_targets")
        exemptions_raw = raw_candidate.get("data_restore_exemptions", {})
        if not isinstance(candidate_id, str) or not isinstance(schema_targets_raw, list):
            raise ValueError("candidate id/schema_targets contract is invalid")
        if not isinstance(exemptions_raw, dict):
            raise ValueError("data_restore_exemptions must map target names to reasons")
        exemptions_untyped = cast("dict[object, object]", exemptions_raw)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in exemptions_untyped.items()
        ):
            raise ValueError("data_restore_exemptions must map target names to reasons")
        exemptions = {cast(str, key): cast(str, value) for key, value in exemptions_untyped.items()}
        schema_targets = cast("list[object]", schema_targets_raw)
        for raw_name in schema_targets:
            if not isinstance(raw_name, str) or not raw_name:
                raise ValueError("schema target names must be non-empty strings")
            requested.append((candidate_id, raw_name, exemptions.get(raw_name)))

    uri = source_db.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "missing"
        foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        revision_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision_row is None:
            raise ValueError("source backup has no Alembic revision")
        objects = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing = sorted(
            name for _, name, exemption in requested if name not in objects and exemption is None
        )
        if missing:
            raise ValueError("backup is missing cataloged schema targets: " + ",".join(missing))

        def count_rows(name: str) -> int:
            query = f"SELECT COUNT(*) FROM {_quote_identifier(name)}"  # nosec B608 -- closed catalog identifier is escaped and quoted
            return int(conn.execute(query).fetchone()[0])

        targets = [
            RecoveryTarget(
                candidate_id=candidate_id,
                name=name,
                object_type=objects.get(name, "absent"),
                present=name in objects,
                row_count=count_rows(name) if name in objects else 0,
                exemption_reason=exemption if name not in objects else None,
            )
            for candidate_id, name, exemption in requested
        ]
    finally:
        conn.close()
    receipt = RecoveryReceipt(
        source_db=str(source_db.resolve()),
        source_sha256=_sha256(source_db),
        source_revision=str(revision_row[0]),
        integrity_check=integrity,
        foreign_key_violations=foreign_key_violations,
        targets=targets,
    )
    if not receipt.verified:
        raise ValueError("backup failed deletion-recovery verification")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "docs"
        / "design"
        / "deletion_catalog_2026_08.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = audit(args.source_db, args.catalog)
    payload = receipt.model_dump_json(indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
