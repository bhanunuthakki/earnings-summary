"""Append to / list the deferred-FMP-task backlog.

The deferred-FMP-task log records work blocked on FMP access being restored (an
authoritative splits feed, a clean consensus re-pull, a re-authed key). The
split-normalization guard auto-logs into it on quarantine; this CLI is the manual
entry point to add items and to review what is open.

Store: ``data/deferred_fmp/deferred_fmp.jsonl`` (git-tracked backlog). Idempotent:
appending an item with the same ``(area, task, ticker)`` refreshes the existing
row rather than duplicating it.

Usage:
    # append (or refresh) a blocked item
    python execution/deferred_fmp_log.py add \
        --area split_normalization --ticker BKNG \
        --task "Back-adjust per-share history with authoritative splits feed" \
        --blocked-on fmp_splits_feed \
        --context "ratio-heuristic placeholder; replace with real split factors"

    # list open items (default) or all
    python execution/deferred_fmp_log.py list
    python execution/deferred_fmp_log.py list --all

    # close an item
    python execution/deferred_fmp_log.py done \
        --area auth --task "Re-auth the FMP MCP key (Invalid API KEY)"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.deferred_fmp import (  # noqa: E402
    DeferredFmpTask,
    DeferredStatus,
    default_store_path,
    list_tasks,
    log_deferred,
    mark_done,
)


def _emit(event: dict[str, object]) -> None:
    """One JSON line per event to stderr (data stays on stdout)."""
    print(json.dumps(event), file=sys.stderr)


def _cmd_add(args: argparse.Namespace, store_path: Path) -> int:
    task = DeferredFmpTask(
        area=args.area,
        task=args.task,
        blocked_on=args.blocked_on,
        ticker=args.ticker,
        context=args.context or "",
    )
    stored, created = log_deferred(task, store_path)
    _emit(
        {
            "event": "deferred_add",
            "created": created,
            "area": stored.area,
            "task": stored.task,
            "ticker": stored.ticker,
        }
    )
    verb = "added" if created else "refreshed"
    print(
        f"{verb}: [{stored.area}] {stored.task}" + (f" ({stored.ticker})" if stored.ticker else "")
    )
    return 0


def _cmd_list(args: argparse.Namespace, store_path: Path) -> int:
    status = None if args.all else DeferredStatus.OPEN
    tasks = list_tasks(store_path, status=status)
    if not tasks:
        print("(no deferred FMP tasks)")
        return 0
    for t in tasks:
        tick = f" [{t.ticker}]" if t.ticker else ""
        print(f"- {t.status.value.upper():4} ({t.area}){tick} {t.task}")
        print(f"    blocked_on: {t.blocked_on}")
        if t.context:
            print(f"    context: {t.context}")
    _emit({"event": "deferred_list", "count": len(tasks), "all": args.all})
    return 0


def _cmd_done(args: argparse.Namespace, store_path: Path) -> int:
    ok = mark_done(args.area, args.task, args.ticker, store_path)
    _emit(
        {
            "event": "deferred_done",
            "matched": ok,
            "area": args.area,
            "task": args.task,
            "ticker": args.ticker,
        }
    )
    print(("closed" if ok else "no match") + f": [{args.area}] {args.task}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deferred-FMP-task backlog CLI.")
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Override the JSONL store path (default: data/deferred_fmp/deferred_fmp.jsonl).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Append or refresh a blocked item.")
    p_add.add_argument("--area", required=True, type=str)
    p_add.add_argument("--task", required=True, type=str)
    p_add.add_argument("--blocked-on", required=True, type=str, dest="blocked_on")
    p_add.add_argument("--ticker", type=str, default=None)
    p_add.add_argument("--context", type=str, default="")

    p_list = sub.add_parser("list", help="List open (default) or all items.")
    p_list.add_argument("--all", action="store_true", help="Include closed items.")

    p_done = sub.add_parser("done", help="Close a matching item.")
    p_done.add_argument("--area", required=True, type=str)
    p_done.add_argument("--task", required=True, type=str)
    p_done.add_argument("--ticker", type=str, default=None)

    args = parser.parse_args(argv)
    store_path = args.store or default_store_path(PROJECT_ROOT)

    if args.command == "add":
        return _cmd_add(args, store_path)
    if args.command == "list":
        return _cmd_list(args, store_path)
    if args.command == "done":
        return _cmd_done(args, store_path)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
