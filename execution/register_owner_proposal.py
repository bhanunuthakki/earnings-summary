"""Validate an owner-reviewed diff; --apply registers it for a separate decision."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

try:
    from _lib import command_parser, log_event
except ImportError:
    from execution._lib import command_parser, log_event

from research.proposal_approval import (
    create_owner_proposal,
    get_ask_proposal_detail,
    preview_owner_proposal,
)


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--diff", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        raw_diff: object = json.loads(args.diff.read_text(encoding="utf-8"))
        preview = preview_owner_proposal(
            raw_diff,
            repo_root=args.repo_root,
            authorization=json.loads(args.authorization.read_text(encoding="utf-8")),
        )
        if args.apply:
            ref = create_owner_proposal(
                raw_diff,
                repo_root=args.repo_root,
                db_path=args.db,
                authorization=preview.authorization,
            )
            detail = get_ask_proposal_detail(ref.proposal_id, db_path=args.db)
            if detail is None:
                raise RuntimeError("registered owner proposal is unavailable")
            output = detail.model_dump(mode="json")
        else:
            output = preview.model_dump(mode="json")
            output["dry_run"] = True
        log_event(
            "owner_proposal_registered" if args.apply else "owner_proposal_validated",
            authorization_id=preview.authorization.authorization_id,
            canonical_content_sha256=preview.canonical_content_sha256,
        )
        print(json.dumps(output, sort_keys=True))
        return 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        log_event("owner_proposal_registration_failed", error_type=type(exc).__name__)
        print(json.dumps({"status": "invalid", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
