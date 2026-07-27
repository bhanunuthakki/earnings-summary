"""Encrypt and verify legacy plaintext `.gz` snapshots before removing them.

Dry-run is the default. With ``--apply``, each plaintext snapshot is encrypted
to ``.gz.enc``, restored into a throwaway SQLite file, integrity-checked, and
only then removed. A failed item stops the migration and preserves its source.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "cron"))

import restore_db  # noqa: E402

from runtime.backup_crypto import encrypt_file, load_or_create_key  # noqa: E402
from runtime.secrets import load_project_env  # noqa: E402


def migrate(backup_dir: Path, *, apply: bool) -> dict[str, object]:
    plaintext = sorted(backup_dir.glob("portfolio.db.*.gz"))
    report: dict[str, object] = {
        "apply": apply,
        "backup_dir": str(backup_dir),
        "candidates": len(plaintext),
        "migrated": 0,
    }
    if not apply:
        return report

    key = load_or_create_key()
    migrated = 0
    for source in plaintext:
        encrypted = source.with_name(source.name + ".enc")
        if encrypted.exists():
            raise FileExistsError(
                f"refusing ambiguous migration; encrypted destination already exists: {encrypted}"
            )
        encrypt_file(source, encrypted, key=key)
        with tempfile.TemporaryDirectory(prefix="backup_migration.") as tmp:
            target = Path(tmp) / "verified.db"
            restore_db.restore_snapshot(encrypted, target, force=False)
        source.unlink()
        migrated += 1
    report["migrated"] = migrated
    return report


def main(argv: list[str] | None = None) -> int:
    load_project_env(PROJECT_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=restore_db.configured_backup_dir(),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = migrate(args.backup_dir, apply=args.apply)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
