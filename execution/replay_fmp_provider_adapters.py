"""Replay supported immutable FMP cache files through provider-neutral adapters.

This is an offline, read-only rehearsal.  It writes a JSON report only; it
does not fetch, mutate a database, or claim that ``--observed-at`` is an
original FMP retrieval timestamp.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = PROJECT_ROOT / ".tmp" / "fmp_provider_adapter_replay"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sources.fmp_replay import replay_fmp_adapter_corpus, validate_report_output_path  # noqa: E402


def _observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--observed-at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--observed-at must include a timezone")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--observed-at", type=_observed_at, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="Relative report path under .tmp"
    )
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        if args.output.is_absolute():
            raise ValueError("--output must be relative to the replay report root")
        output_path = validate_report_output_path(
            args.corpus_root,
            REPORT_ROOT,
            REPORT_ROOT / args.output,
            overwrite=args.overwrite,
        )
        report = replay_fmp_adapter_corpus(
            args.corpus_root,
            observed_at=args.observed_at,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
    except ValueError as exc:
        sys.stderr.write(json.dumps({"status": "error", "message": str(exc)}) + "\n")
        return 2

    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok" if report.failed_files == 0 else "failed",
                "report": str(output_path),
                "report_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "manifest_sha256": report.corpus_manifest_sha256,
                "selected_files": report.selected_files,
                "failed_files": report.failed_files,
            },
            sort_keys=True,
        )
    )
    return 0 if report.failed_files == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
