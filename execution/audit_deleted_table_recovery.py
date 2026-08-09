"""Prove that cataloged deleted-table rows remain readable in a verified backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

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
    catalog: object = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("candidates"), list):
        raise ValueError("deletion catalog must contain a candidates list")

    requested: list[tuple[str, str, str | None]] = []
    for raw_candidate in catalog["candidates"]:
        if not isinstance(raw_candidate, dict):
            raise ValueError("deletion catalog candidate must be an object")
        candidate_id = raw_candidate.get("id")
        schema_targets = raw_candidate.get("schema_targets")
        exemptions = raw_candidate.get("data_restore_exemptions", {})
        if not isinstance(candidate_id, str) or not isinstance(schema_targets, list):
            raise ValueError("candidate id/schema_targets contract is invalid")
        if not isinstance(exemptions, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in exemptions.items()
        ):
            raise ValueError("data_restore_exemptions must map target names to reasons")
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
        targets = [
            RecoveryTarget(
                candidate_id=candidate_id,
                name=name,
                object_type=objects.get(name, "absent"),
                present=name in objects,
                row_count=int(
                    conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(name)}").fetchone()[0]
                )
                if name in objects
                else 0,
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
