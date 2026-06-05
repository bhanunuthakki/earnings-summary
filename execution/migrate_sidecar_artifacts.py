"""One-shot backfill: migrate JSON sidecars under `data/{purpose}/<T>.json`
into the unified `llm_artifacts` DB table.

The bear_case and other section caches predate the llm_artifacts table.
Phase 1 introduced the unified store but didn't backfill — so existing
sidecars are invisible to the lens runner (which only reads
llm_artifacts).

This script reads disk sidecars and writes corresponding rows. Idempotent:
running twice produces zero new rows (the artifact_store's natural-key
check short-circuits).

Sidecar directories migrated:
  data/bear_case/<T>.json         → purpose='bear_case'
  data/company_description/<T>.json → purpose='company_description'
  data/valuation_basis/<T>.json   → purpose='valuation_basis'
  data/filing_intelligence/<T>.json → purpose='filing_intelligence'
  data/qa_topics/<T>.json         → purpose='qa_topics'
  data/saydo_filter/<T>.json      → purpose='saydo_filter'
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm_artifact_store import UpsertRequest, upsert  # noqa: E402

log = logging.getLogger("migrate_sidecar")

SIDECAR_DIRS = {
    "bear_case": "data/bear_case",
    "company_description": "data/company_description",
    "valuation_basis": "data/valuation_basis",
    "filing_intelligence": "data/filing_intelligence",
    "qa_topics": "data/qa_topics",
    "saydo_filter": "data/saydo_filter",
    "platform_diagram": "data/platform_diagram",
    "segment_definitions": "data/segment_definitions",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=PROJECT_ROOT, help="Repo root containing data/."
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = args.repo_root / "data" / "portfolio.db"
    n_total = 0
    n_inserted = 0
    n_skipped = 0
    for purpose, rel_dir in SIDECAR_DIRS.items():
        dir_path = args.repo_root / rel_dir
        if not dir_path.exists():
            continue
        for json_file in dir_path.glob("*.json"):
            ticker = json_file.stem.upper()
            n_total += 1
            try:
                raw = json_file.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning({"event": "read_failed", "path": str(json_file), "error": str(exc)})
                n_skipped += 1
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None

            # Compose cache_inputs from sha of raw text (one-shot import key)
            artifact_id, was_hit = upsert(
                UpsertRequest(
                    ticker=ticker,
                    purpose=purpose,
                    content_md=raw if not isinstance(payload, (dict, list)) else None,
                    content_json=payload,
                    cache_inputs=[
                        ticker,
                        purpose,
                        f"sidecar_migration_v1:{json_file.stat().st_size}",
                        raw[:5000],  # first 5KB as part of cache key
                    ],
                    model="(legacy_sidecar)",
                ),
                db_path=db_path,
            )
            if artifact_id is None:
                n_skipped += 1
                continue
            if was_hit:
                # Already in table — no work
                continue
            n_inserted += 1
            log.debug(
                {
                    "event": "sidecar_migrated",
                    "purpose": purpose,
                    "ticker": ticker,
                    "artifact_id": artifact_id,
                }
            )

    print(
        f"Sidecar migration complete · scanned {n_total} files · "
        f"inserted {n_inserted} new artifacts · {n_skipped} skipped/error"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
