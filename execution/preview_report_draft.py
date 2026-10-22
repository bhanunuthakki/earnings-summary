"""Bind and save an explicitly supplied PRE/POST draft; never call a model or collect data."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import TypeAdapter

from earnings_readout import ReportedQuarter
from research.report_contract import ReportDraft
from research.report_preview import save_report_preview
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--post-quarter", type=Path)
    args = parser.parse_args(argv)
    draft = ReportDraft.model_validate_json(args.draft.read_bytes())
    quarter = (
        TypeAdapter(ReportedQuarter).validate_json(args.post_quarter.read_bytes())
        if args.post_quarter is not None
        else None
    )
    conn = connect_sqlite(args.db, role=SQLiteConnectionRole.READ_ONLY)
    try:
        artifact = save_report_preview(conn, draft, repo_root=args.repo_root, quarter=quarter)
    finally:
        conn.close()
    print(artifact.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
