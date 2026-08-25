"""Produce the bounded Portfolio Tracker daily-refresh receipt.

This command is read-only: it probes the configured v1 API and records typed
evidence. It never starts a listener, changes Scheduler state, or mutates the
tracker database.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SCHEDULED_TASK_NAME = r"\earnings-summary\refresh_portfolio_tracker"
SCHEDULER_PROOF_TIMEOUT_SECONDS = 5.0

from operations.paths import portfolio_tracker_receipt_path  # noqa: E402
from runtime.portfolio_tracker import produce_daily_refresh_receipt  # noqa: E402


def canonical_scheduler_task_is_running(
    repo_root: Path,
    *,
    windows: bool | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Prove this process is executing the canonical Windows task, not a flag.

    The public ``--scheduled-task`` switch merely requests this bounded local
    COM check. A direct invocation cannot mint refresh attribution unless the
    exact registered task is running with this checkout's wrapper and its task
    engine is an ancestor of this Python process.
    """

    if not (os.name == "nt" if windows is None else windows):
        return False
    wrapper = (repo_root / "cron" / "run_refresh_portfolio_tracker.bat").resolve()
    expected_wrapper = json.dumps(str(wrapper))
    current_pid = os.getpid()
    script = (
        "$ErrorActionPreference='Stop';"
        "$service=New-Object -ComObject 'Schedule.Service';$service.Connect();"
        "$task=$service.GetFolder('\\earnings-summary').GetTask('refresh_portfolio_tracker');"
        "$action=$task.Definition.Actions.Item(1);"
        f"$expected={expected_wrapper};"
        f"$currentPid={current_pid};"
        "$maxProcessRows=4096;$maxAncestryHops=32;"
        "$processRows=@(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | "
        "Where-Object { [int]$_.ProcessId -gt 0 } | ForEach-Object { "
        "try { $processId=[int]$_.ProcessId;$parent=[int]$_.ParentProcessId } catch { exit 1 };"
        "'{0}|{1}' -f $processId,$parent"
        "});"
        "$parents=@{};"
        "if ($processRows.Count -eq 0 -or $processRows.Count -gt $maxProcessRows) { exit 1 };"
        "foreach ($row in $processRows) {"
        "$fields=$row -split '\\|';"
        "if ($fields.Count -ne 2) { exit 1 };"
        "try { $processId=[int]$fields[0];$parent=[int]$fields[1] } catch { exit 1 };"
        "if ($processId -le 0 -or $parent -lt 0 -or $parents.ContainsKey($processId)) { exit 1 };"
        "$parents.Add($processId,$parent)"
        "};"
        "$ancestry=New-Object 'System.Collections.Generic.HashSet[int]';"
        "$next=$currentPid;$hops=0;"
        "while ($next -gt 0 -and $hops -lt $maxAncestryHops -and $ancestry.Add([int]$next)) {"
        "if (-not $parents.ContainsKey([int]$next)) { break };"
        "$next=[int]$parents[[int]$next];$hops+=1"
        "};"
        "$instances=$task.GetInstances(0);$engineIsAncestor=$false;"
        "for ($index=1; $index -le $instances.Count; $index++) {"
        "if ($ancestry.Contains([int]$instances.Item($index).EnginePID)) {"
        "$engineIsAncestor=$true;break"
        "}"
        "};"
        "if ($task.Path -ne '\\earnings-summary\\refresh_portfolio_tracker' "
        "-or $task.State -ne 4 -or $task.Definition.Actions.Count -ne 1 -or $action.Type -ne 0 "
        "-or [String]::Compare([IO.Path]::GetFullPath($action.Path), "
        "[IO.Path]::GetFullPath($expected), $true) -ne 0 -or -not $engineIsAncestor) { exit 1 }"
    )
    try:
        result = run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=SCHEDULER_PROOF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--api-url", default=os.environ.get("PORTFOLIO_TRACKER_API_URL"))
    parser.add_argument(
        "--scheduled-task",
        action="store_true",
        help="record a successful receipt only for the canonical Scheduler wrapper",
    )
    args = parser.parse_args()
    if not args.api_url:
        parser.error("PORTFOLIO_TRACKER_API_URL or --api-url is required")
    if args.scheduled_task and not canonical_scheduler_task_is_running(args.repo_root):
        parser.error("canonical refresh_portfolio_tracker Scheduler context is not running")
    now = datetime.now(UTC)
    receipt = produce_daily_refresh_receipt(
        api_url=args.api_url,
        receipt_path=portfolio_tracker_receipt_path(args.repo_root),
        now=now,
        daily_refresh_owner="portfolio-tracker-refresh" if args.scheduled_task else None,
        scheduler_task_name=(SCHEDULED_TASK_NAME if args.scheduled_task else None),
    )
    print(receipt.model_dump_json())
    return 0 if receipt.lifecycle_state == "already_running" else 1


if __name__ == "__main__":
    raise SystemExit(main())
