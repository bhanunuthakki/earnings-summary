"""Approve Windows-local Operations identities for later pinned Mac review."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from fetch_windows_review_bundle import (
    exact_https_origin,
    identity_sha256,
    seal_windows_review_pins,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from operations.review_bundle import OperationsReviewBundle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-local-bundle", type=Path, required=True)
    parser.add_argument(
        "--serving-origin",
        required=True,
        help="Exact HTTPS origin independently observed from live tailscale serve status",
    )
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise RuntimeError("review pins must be enrolled on the Windows authority host")
    bundle = OperationsReviewBundle.model_validate_json(
        args.windows_local_bundle.read_text(encoding="utf-8")
    )
    serving_origin = exact_https_origin(args.serving_origin)
    if bundle.identity.serving_origin_sha256 != identity_sha256(serving_origin):
        raise ValueError("Windows-local bundle does not match the independently observed origin")
    pins = seal_windows_review_pins(
        bundle=bundle,
        approved_by=args.approved_by,
        approved_at=datetime.now(UTC),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("refusing to overwrite existing trusted review pins")
    args.output.write_text(pins.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(pins.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
