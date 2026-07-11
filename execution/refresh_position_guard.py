"""execution/refresh_position_guard.py — materialize the naked-position gate.

Computes ``position_guard.build_position_guard`` (three read-only checks per
held name: downside trigger encoded, realistic bear persisted, thesis fresh
— ``directives/monthly_red_team.md`` Phase 1 guard 7) and writes the result
to ``data/dashboard/position_guard.json`` so the Risk panel's render path
never recomputes it (``src/position_guard_cache.py`` owns the atomic write).
Wired as morning-pipeline stage 0h, also runnable by hand.

No network, no LLM — pure read-only SQLite + local JSON file reads, so a
DB-connect failure is the only realistic failure mode. On any exception the
last-good cache file is left untouched (mirrors ``fetch_factor_proxies.py``'s
"a failed fetch keeps the last-good file" contract) rather than clobbering a
working cache with a half-computed one.

CLI:
    python execution/refresh_position_guard.py
    python execution/refresh_position_guard.py --db-path /tmp/x.db

Exit code 0 on a successful compute + write (even with zero weighted
holdings — that's a legitimate "book is empty" cache state); 1 on any
exception building or writing the report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from db_paths import resolve_db_path  # noqa: E402
from position_guard import build_position_guard  # noqa: E402
from position_guard_cache import write_position_guard_cache  # noqa: E402


def _log(event: str, **kwargs: object) -> None:
    print(json.dumps({"event": event, **kwargs}), file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=None, help="portfolio.db override")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repo root whose data/dashboard/ receives the cache "
        "(default: the DB path's grandparent)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = resolve_db_path(args.db_path)
    if db_path is None:
        _log("halt", reason="DB path not configured")
        return 1
    repo_root = args.repo_root or db_path.parent.parent
    try:
        report = build_position_guard(db_path, repo_root)
        path = write_position_guard_cache(repo_root, report)
    except Exception as exc:  # last-good file stays untouched on any failure
        _log("failed", reason=str(exc))
        return 1
    n_violations = sum(1 for r in report.rows if not r.passed)
    print(
        json.dumps(
            {
                "path": str(path),
                "names_checked": len(report.rows),
                "violations": n_violations,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
