"""One-shot migration: canonicalize bare transcript filenames in the index.

Before this fix, `register_transcript` stored `output_path.name` (e.g.
`AMZN_Q1_2026.txt`) into both `.tmp/transcript_index.json[KEY].filepath` and
`.tmp/document_index.json[KEY].local_path`. `process_ir_documents.py` later did
`Path(local_path).exists()` from project root and silently skipped every
transcript whose `local_path` was a bare basename.

This script walks both index files and rewrites any bare-filename entries to
their project-root-relative location (`transcripts/processed/<name>` or
`transcripts/raw/<name>`, resolved against what's actually on disk).

Idempotent: re-running after migration is a no-op.

Usage:
    python execution/migrate_transcript_local_paths.py            # apply
    python execution/migrate_transcript_local_paths.py --dry-run  # report only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import index_manager  # noqa: E402


def _migrate_dict(data: dict, key: str, label: str, dry_run: bool) -> tuple[int, int, int]:
    """Apply canonicalization to `data[KEY][key]` for each entry, where applicable.

    Returns (updated, already_canonical, skipped_non_transcript).
    """
    updated = 0
    already_canonical = 0
    skipped = 0
    for entry_key, entry in data.items():
        if label == "document_index" and entry.get("doc_type") != "transcript":
            skipped += 1
            continue
        current = entry.get(key)
        if current is None:
            skipped += 1
            continue
        new_value = index_manager._canonicalize_transcript_filepath(current)
        if new_value == current:
            already_canonical += 1
            continue
        if dry_run:
            print(f"  [{label}] {entry_key}: {current!r} -> {new_value!r}")
        else:
            entry[key] = new_value
        updated += 1
    return updated, already_canonical, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = parser.parse_args()

    transcript_index_path = Path(index_manager.TRANSCRIPT_INDEX_PATH)
    document_index_path = Path(index_manager.DOCUMENT_INDEX_PATH)

    print(f"transcript_index: {transcript_index_path}")
    print(f"document_index:   {document_index_path}")
    print()

    transcript_index = (
        json.loads(transcript_index_path.read_text(encoding="utf-8"))
        if transcript_index_path.exists()
        else {}
    )
    document_index = (
        json.loads(document_index_path.read_text(encoding="utf-8"))
        if document_index_path.exists()
        else {}
    )

    t_updated, t_same, t_skipped = _migrate_dict(
        transcript_index, "filepath", "transcript_index", args.dry_run
    )
    d_updated, d_same, d_skipped = _migrate_dict(
        document_index, "local_path", "document_index", args.dry_run
    )

    print()
    print(f"transcript_index: {t_updated} updated, {t_same} already canonical, {t_skipped} skipped")
    print(
        f"document_index:   {d_updated} updated, {d_same} already canonical, {d_skipped} skipped (non-transcript or null)"
    )

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return 0

    if t_updated:
        transcript_index_path.write_text(json.dumps(transcript_index, indent=4), encoding="utf-8")
        print(f"\nWrote {transcript_index_path}")
    if d_updated:
        document_index_path.write_text(json.dumps(document_index, indent=4), encoding="utf-8")
        print(f"Wrote {document_index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
