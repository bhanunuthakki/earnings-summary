"""Verify that every cron/*.task.xml is registered and enabled in Windows Task Scheduler.

Reads all *.task.xml files under the cron/ directory, extracts the registered
task name (from the <URI> element), then queries ``schtasks /query /fo csv`` for
the live state of each.  Reports three problem categories:

  missing   — XML exists but no matching task is registered
  disabled  — registered but the task's Status is not Ready/Running
  mismatch  — registered but the scheduled trigger time differs from the XML

Human-readable table is printed to stdout.  Exit code:

  0  all tasks registered, enabled, and schedule matches
  1  one or more tasks have a problem (missing / disabled / mismatch)
  2  could not query the scheduler (non-Windows or permission error)

Intended to run weekly via Task Scheduler (see cron/verify_cron.task.xml) and
from the morning pipeline preflight so drift surfaces immediately.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = PROJECT_ROOT / "cron"

# Windows Task Scheduler XML namespace
_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# Task statuses considered "healthy" — both mean the task is scheduled and
# ready to fire; "Running" appears when the task is currently executing.
_HEALTHY_STATUSES = {"Ready", "Running"}


class _XmlTask(NamedTuple):
    """Parsed facts extracted from one .task.xml file."""

    xml_path: Path
    task_name: str  # e.g. r"\earnings-summary\refresh_cache"
    start_time: str | None  # ISO local time, e.g. "03:00:00"
    enabled: bool


@dataclass(slots=True)
class TaskReport:
    """Aggregated comparison result."""

    ok: list[str] = field(default_factory=list[str])
    missing: list[str] = field(default_factory=list[str])
    disabled: list[str] = field(default_factory=list[str])
    mismatch: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]]
    )  # (name, xml_time, sched_time)

    @property
    def has_problems(self) -> bool:
        return bool(self.missing or self.disabled or self.mismatch)


def _parse_xml(path: Path) -> _XmlTask | None:
    """Extract task name and first trigger start time from a .task.xml file.

    Returns None if the file cannot be parsed or lacks a <URI>.
    """
    try:
        tree = ET.parse(str(path))
    except ET.ParseError as exc:
        sys.stderr.write(f"WARNING: cannot parse {path.name}: {exc}\n")
        return None

    root = tree.getroot()
    ns = f"{{{_NS}}}"

    uri_el = root.find(f".//{ns}URI")
    uri_text = uri_el.text if uri_el is not None else None
    if not (uri_text or "").strip():
        return None
    task_name = (uri_text or "").strip()

    # Pull the first CalendarTrigger start time (HH:MM:SS portion).
    start_time: str | None = None
    sb_el = root.find(f".//{ns}StartBoundary")
    if sb_el is not None and sb_el.text:
        # Format: "2026-06-12T05:35:00" — take the time portion.
        parts = sb_el.text.strip().split("T", 1)
        if len(parts) == 2:
            start_time = parts[1]

    enabled_el = root.find(f".//{ns}Enabled")
    enabled = (enabled_el is None) or (enabled_el.text or "true").lower() != "false"

    return _XmlTask(
        xml_path=path,
        task_name=task_name,
        start_time=start_time,
        enabled=enabled,
    )


def _query_schtasks() -> dict[str, dict[str, str]] | None:
    """Run ``schtasks /query /fo csv`` and return a dict keyed by task name.

    Keys are normalised to lower-case for case-insensitive comparison.
    Returns None if the command fails (non-Windows, permission error, etc.).
    """
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"ERROR: cannot run schtasks: {exc}\n")
        return None

    if result.returncode != 0:
        sys.stderr.write(f"ERROR: schtasks exited {result.returncode}: {result.stderr.strip()}\n")
        return None

    tasks: dict[str, dict[str, str]] = {}
    reader = csv.reader(io.StringIO(result.stdout))
    for row in reader:
        # CSV columns: "TaskName","Next Run Time","Status"
        if len(row) < 3:
            continue
        name = row[0].strip().strip('"')
        if not name:
            continue
        tasks[name.lower()] = {
            "name": name,
            "next_run": row[1].strip().strip('"'),
            "status": row[2].strip().strip('"'),
        }
    return tasks


def _extract_next_run_time(next_run: str) -> str | None:
    """Extract HH:MM:SS from a schtasks 'Next Run Time' like '6/12/2026 5:35:00 AM'."""
    # Format varies by locale; we look for the time part after the date.
    parts = next_run.strip().split()
    for part in parts:
        if ":" in part:
            # Normalize to zero-padded HH:MM:SS.
            hms = part.split(":")
            if len(hms) >= 2:
                hour = int(hms[0])
                minute = int(hms[1])
                second = int(hms[2]) if len(hms) > 2 else 0
                # AM/PM correction.
                ampm = parts[-1].upper() if parts[-1].upper() in ("AM", "PM") else ""
                if ampm == "PM" and hour != 12:
                    hour += 12
                elif ampm == "AM" and hour == 12:
                    hour = 0
                return f"{hour:02d}:{minute:02d}:{second:02d}"
    return None


def compare(cron_dir: Path = CRON_DIR) -> tuple[TaskReport, list[_XmlTask]]:
    """Parse all XMLs in cron_dir and compare against the live scheduler.

    Returns (TaskReport, xml_tasks_list). If schtasks cannot be queried,
    returns a TaskReport where every XML is listed as "missing" so the exit
    code is still non-zero.
    """
    xml_tasks = [t for p in sorted(cron_dir.glob("*.task.xml")) if (t := _parse_xml(p)) is not None]

    live = _query_schtasks()
    report = TaskReport()

    for xt in xml_tasks:
        key = xt.task_name.lower()
        if live is None or key not in live:
            report.missing.append(xt.task_name)
            continue

        task_info = live[key]
        status = task_info["status"]

        if not xt.enabled:
            # XML says disabled — if it's also not registered that's fine;
            # if it IS registered, flag as a "disabled in XML" note.
            report.ok.append(f"{xt.task_name} (xml disabled, registered)")
            continue

        if status not in _HEALTHY_STATUSES:
            report.disabled.append(f"{xt.task_name} (Status={status!r})")
            continue

        # Schedule-mismatch check: compare trigger start time from XML vs
        # schtasks' "Next Run Time" column (only the time portion matters
        # since the date advances each run).
        if xt.start_time is not None:
            next_run_time = _extract_next_run_time(task_info["next_run"])
            if next_run_time is not None and next_run_time != xt.start_time:
                report.mismatch.append((xt.task_name, xt.start_time, next_run_time))
                continue

        report.ok.append(xt.task_name)

    return report, xml_tasks


def _print_report(report: TaskReport, xml_tasks: list[_XmlTask]) -> None:
    total = len(xml_tasks)
    ok = len(report.ok)
    print(f"\nCron registration check — {total} task XML(s) found\n")

    if not report.has_problems:
        print(f"  ✓  All {ok} tasks registered and enabled\n")
        return

    if report.ok:
        print(f"  OK ({len(report.ok)}): {', '.join(report.ok)}\n")

    if report.missing:
        print(f"  MISSING ({len(report.missing)}) — not registered in Windows Task Scheduler:")
        for name in report.missing:
            print(f"    ✗  {name}")
        print()

    if report.disabled:
        print(f"  DISABLED ({len(report.disabled)}) — registered but not Ready:")
        for desc in report.disabled:
            print(f"    ✗  {desc}")
        print()

    if report.mismatch:
        print(f"  SCHEDULE MISMATCH ({len(report.mismatch)}) — registered time differs from XML:")
        for name, xml_time, sched_time in report.mismatch:
            print(f"    ✗  {name}: XML={xml_time} vs scheduler={sched_time}")
        print()

    problems = len(report.missing) + len(report.disabled) + len(report.mismatch)
    print(f"  {problems} problem(s) found. Fix: re-run schtasks /create or check the XML.\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cron-dir",
        type=Path,
        default=CRON_DIR,
        help="Directory containing *.task.xml files (default: cron/).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable table; only the exit code matters.",
    )
    args = parser.parse_args(argv)

    report, xml_tasks = compare(args.cron_dir)

    if not args.quiet:
        _print_report(report, xml_tasks)

    if not xml_tasks:
        sys.stderr.write("WARNING: no *.task.xml files found — nothing to check.\n")
        return 0

    # If schtasks was unreachable, every task ends up in "missing" — return 2
    # (scheduler query failure) rather than 1 (task mismatch).
    live = _query_schtasks()
    if live is None:
        return 2

    return 1 if report.has_problems else 0


if __name__ == "__main__":
    sys.exit(main())
