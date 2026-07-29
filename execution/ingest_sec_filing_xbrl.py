"""Validate and publish one completely captured SEC Inline-XBRL accession.

Dry-run is the default.  SEC inventory and byte capture are explicit
prerequisites; this command admits only a sealed current inventory and an
offline, qualified processor bundle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from filings.inline_xbrl_processor import (  # noqa: E402
    ProcessorPackageMember,
    load_processor_bundle_manifest,
)
from provenance.sec_filing_xbrl_ingest import (  # noqa: E402
    FilingXbrlIngestRequest,
    ingest_sec_filing_xbrl,
)
from runtime.job_runtime import JobAlreadyRunningError, JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

_MEMBERS = TypeAdapter(tuple[ProcessorPackageMember, ...])


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inventory-key", required=True)
    parser.add_argument("--accession", required=True)
    parser.add_argument("--cik", required=True)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        required=True,
        help="Root whose complete file inventory is pinned by the processor runtime lock",
    )
    parser.add_argument("--bundle-python", type=Path, required=True)
    parser.add_argument(
        "--sandbox-launcher",
        type=Path,
        required=True,
        help="Hash-pinned OS sandbox launcher that enforces network denial",
    )
    parser.add_argument(
        "--bundle-manifest",
        type=Path,
        default=PROJECT_ROOT / "config" / "filing_xbrl_processor_bundle.json",
    )
    parser.add_argument(
        "--offline-artifact-manifest",
        type=Path,
        help="Optional JSON array of pinned standard-taxonomy/network package members",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _offline_artifacts(path: Path | None) -> tuple[ProcessorPackageMember, ...]:
    if path is None:
        return ()
    return _MEMBERS.validate_json(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = load_processor_bundle_manifest(args.bundle_manifest)
    request = FilingXbrlIngestRequest(
        inventory_key=str(args.inventory_key),
        accession_number=str(args.accession),
        expected_cik=str(args.cik).strip().zfill(10),
        runtime_root=args.runtime_root,
        bundle_python=args.bundle_python,
        sandbox_launcher=args.sandbox_launcher,
        recorded_at=datetime.now(UTC),
        offline_artifacts=_offline_artifacts(args.offline_artifact_manifest),
        apply=bool(args.apply),
    )
    write_sets = [
        f"filing-xbrl-accession:{request.accession_number}",
        f"filing-xbrl-bundle:{manifest.manifest_sha256}",
    ]
    if request.apply:
        write_sets.append(f"sqlite:{args.db.resolve()}")
    try:
        with JobLock(PROJECT_ROOT, "ingest-sec-filing-xbrl", write_sets):
            role = SQLiteConnectionRole.WRITER if request.apply else SQLiteConnectionRole.READ_ONLY
            conn = connect_sqlite(
                args.db,
                role=role,
                schema_preflight=request.apply,
            )
            try:
                _event(
                    "filing_xbrl_ingest_started",
                    accession=request.accession_number,
                    mode="apply" if request.apply else "dry_run",
                )
                result = ingest_sec_filing_xbrl(conn, request, manifest=manifest)
            finally:
                conn.close()
    except JobAlreadyRunningError:
        _event("filing_xbrl_ingest_locked", accession=request.accession_number)
        return 75
    except Exception as exc:
        _event(
            "filing_xbrl_ingest_failed",
            accession=request.accession_number,
            error_type=type(exc).__name__,
        )
        return 2
    sys.stdout.write(result.model_dump_json() + "\n")
    _event(
        "filing_xbrl_ingest_completed",
        accession=request.accession_number,
        mode=result.mode,
        published=result.published_count,
        quarantined=result.quarantined_count,
        exact_replay=result.exact_replay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
