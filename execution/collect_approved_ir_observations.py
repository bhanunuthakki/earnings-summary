"""Capture and seal public Wix/Rubrik reporting-page observations."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.approved_ir_observation_capture import (  # noqa: E402
    ApprovedIrObservationCaptureRequest,
    PlaywrightApprovedIrBrowser,
    collect_approved_ir_observations,
    public_robots_allows,
)
from log_redact import redact  # noqa: E402

_PERIODS = {
    "WIX": (
        date(2026, 6, 30),
        date(2026, 3, 31),
        date(2025, 12, 31),
        date(2025, 9, 30),
        date(2025, 6, 30),
    ),
    "RBRK": (
        date(2026, 4, 30),
        date(2026, 1, 31),
        date(2025, 10, 31),
        date(2025, 7, 31),
        date(2025, 4, 30),
    ),
}


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", required=True, choices=tuple(_PERIODS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-agent", required=True)
    parser.add_argument("--timeout-ms", type=int, default=30_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    issuer = str(args.issuer)
    try:
        if issuer == "WIX":
            raise ValueError(
                "WIX server-side collection is unavailable by design; import a sealed "
                "visible-browser observation bundle instead"
            )
        _event("approved_ir_observation_capture_started", issuer=issuer)
        bundle = collect_approved_ir_observations(
            ApprovedIrObservationCaptureRequest(
                issuer_identifier=issuer,
                requested_quarter_ends=_PERIODS[issuer],
                captured_at=datetime.now(UTC).replace(tzinfo=None),
                user_agent=str(args.user_agent),
                timeout_ms=int(args.timeout_ms),
            ),
            browser=PlaywrightApprovedIrBrowser(),
            robots_allows=public_robots_allows,
        )
        output = args.output.resolve()
        if PROJECT_ROOT.resolve() not in output.parents or ".tmp" not in output.parts:
            raise ValueError(
                "sealed observation output must be under this repository's .tmp directory"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(output, bundle.to_bytes())
    except Exception as exc:
        _event(
            "approved_ir_observation_capture_failed",
            issuer=issuer,
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(
        json.dumps(
            {
                "issuer": issuer,
                "output": str(output),
                "bundle_sha256": bundle.bundle_sha256,
                "artifact_count": len(bundle.artifacts),
            },
            sort_keys=True,
        )
        + "\n"
    )
    _event(
        "approved_ir_observation_capture_completed",
        issuer=issuer,
        artifact_count=len(bundle.artifacts),
    )
    return 0


def _write_atomically(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ir-observation-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
