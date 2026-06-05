"""One-off: promote `transcripts/raw/<T>_Q<n>_<Y>.{pdf,txt}` files left over
after the 2026-05-21 manual move, by re-using the same `_promote_raw_to_processed`
logic that just landed in `execution/ingest_transcripts.py`.

Run `--dry-run` first; spot-check the proposed moves; then re-run without the
flag. Delete this script after the migration completes — it has no recurring
purpose (the orchestrator now handles promotion automatically).

What this covers:
    - Files in raw/ whose sha256 already has a documents row pointing at
      `transcripts/raw/<name>`. Those are the leftover-from-manual-fetch
      ingests that the new auto-promoter would have moved.

What this deliberately doesn't cover:
    - Files in raw/ that have no corresponding documents row (= never
      ingested). The next regular `ingest_transcripts.py` run will ingest
      AND promote those in one pass.
    - Files already in processed/. Untouched.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.transcript_ingest import (  # noqa: E402
    IngestResult,
    QASectionStatus,
    parse_transcript_filename,
    sha256_of,
)
from pipeline.queries import open_db  # noqa: E402


def _load_ingest_module():
    spec = importlib.util.spec_from_file_location(
        "ingest_transcripts_runtime",
        PROJECT_ROOT / "execution" / "ingest_transcripts.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the move plan without touching the filesystem or DB.",
    )
    parser.add_argument(
        "--db", default=str(PROJECT_ROOT / "data" / "portfolio.db"),
    )
    args = parser.parse_args()

    raw_dir = PROJECT_ROOT / "transcripts" / "raw"
    if not raw_dir.exists():
        print(f"[skip] {raw_dir} does not exist")
        return 0

    mod = _load_ingest_module()
    conn = open_db(args.db)
    try:
        candidates: list[tuple[Path, int, str]] = []
        skipped_no_match = 0
        skipped_db_already_processed = 0

        for path in sorted(raw_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".pdf", ".txt"}:
                continue
            parsed = parse_transcript_filename(path)
            if parsed is None:
                continue

            sha = sha256_of(path)
            row = conn.execute(
                "SELECT id, file_path FROM documents "
                "WHERE sha256 = ? AND doc_type = 'earnings_call_transcript'",
                (sha,),
            ).fetchone()
            if row is None:
                skipped_no_match += 1
                continue
            doc_id = int(row["id"])
            db_path = row["file_path"]
            if not db_path.startswith("transcripts/raw/"):
                # DB already points elsewhere (processed/, or something exotic).
                # Promoter would no-op on this anyway; skip the read.
                skipped_db_already_processed += 1
                continue
            candidates.append((path, doc_id, sha))

        print(
            f"raw_files_in_dir={sum(1 for _ in raw_dir.iterdir())}  "
            f"promotable={len(candidates)}  "
            f"no_db_match={skipped_no_match}  "
            f"db_already_processed={skipped_db_already_processed}"
        )

        if args.dry_run:
            for path, doc_id, _ in candidates:
                print(f"[dry] would promote doc#{doc_id} {path.name}")
            return 0

        moved = 0
        for path, doc_id, _ in candidates:
            parsed = parse_transcript_filename(path)
            assert parsed is not None  # guarded above
            # The promoter expects an IngestResult; synthesize one matching what
            # ingest_one would have returned at fresh-ingest time.
            stub = IngestResult(
                file_path=path,
                ticker=parsed.ticker,
                period_end=datetime.min,  # unused by the promoter
                document_id=doc_id,
                transcript_id=None,
                segment_count=0,
                skipped_existing=False,
                qa_status=QASectionStatus.UNKNOWN,
                qa_signals=(),
            )
            new_path = mod._promote_raw_to_processed(stub, parsed, conn, PROJECT_ROOT)
            if new_path != path:
                moved += 1
                print(f"[done] doc#{doc_id} {path.name} -> {new_path.relative_to(PROJECT_ROOT)}")
            else:
                print(f"[skip] doc#{doc_id} {path.name} (conflict; see stderr)")
        print(f"moved={moved}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
