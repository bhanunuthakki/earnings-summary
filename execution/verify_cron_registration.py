"""Verify that every cron/*.task.xml is registered and enabled in Windows Task Scheduler.

Reads ``cron/task_manifest.json``, validates its exact XML/wrapper coverage and
metadata, then queries ``schtasks /query /fo csv`` for each declared task.

  manifest  â€” coverage, registration identity, action, or schedule drift
  unparseable — the .task.xml could not be read/parsed (e.g. the bytes are
                UTF-8 but the XML declaration claims encoding="UTF-16"); a HARD
                failure — never silently skipped, or the audit would give a
                false all-clear over files it never actually checked
  no_uri    — file parsed but contains no <URI> (structurally incomplete task)
  missing   — XML exists but no matching task is registered
  disabled  — registered but the task's Status is not Ready/Running
  mismatch  — registered but the scheduled trigger time differs from the XML
  wrong_root— registered, enabled and on-schedule, but the action executes a
              wrapper in a DIFFERENT checkout of this repo. The wrappers derive
              PROJECT_ROOT from their own location (%~dp0..), so the image path
              alone decides which repo — and which data/portfolio.db — the run
              uses. The manifest pins the wrapper *filename*, which such a task
              still matches exactly, so nothing else here can see it

Human-readable table is printed to stdout.  Exit code:

  0  all tasks parsed, registered, enabled, and schedule matches
  1  one or more tasks have a problem (manifest / unparseable / no_uri /
     missing / disabled / mismatch)
  2  could not query the scheduler (non-Windows or permission error)

Intended to run weekly via Task Scheduler (see cron/verify_cron.task.xml) and
from the morning pipeline preflight so drift surfaces immediately.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = PROJECT_ROOT / "cron"
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scheduler_manifest import (  # noqa: E402
    TaskManifest,
    load_manifest,
    validate_source_tree,
)

MANIFEST_PATH = CRON_DIR / "task_manifest.json"

# Windows Task Scheduler XML namespace
_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# Task statuses considered "healthy" — both mean the task is scheduled and
# ready to fire; "Running" appears when the task is currently executing.
_HEALTHY_STATUSES = {"Ready", "Running"}
_CAPTURE_POLLER_TASK = r"\earnings-summary\capture_poller".lower()
_CAPTURE_POLLER_SERVICE = "es-poller"


class _XmlTask(NamedTuple):
    """Parsed facts extracted from one .task.xml file."""

    xml_path: Path
    task_name: str  # e.g. r"\earnings-summary\refresh_cache"
    start_time: str | None  # ISO local time, e.g. "03:00:00"
    enabled: bool
    has_repetition: bool  # True for hourly/repeating triggers (skip time-match)


@dataclass(slots=True)
class TaskReport:
    """Aggregated comparison result."""

    ok: list[str] = field(default_factory=list[str])
    manifest_errors: list[str] = field(default_factory=list[str])
    unparseable: list[str] = field(default_factory=list[str])  # "name: error"
    no_uri: list[str] = field(default_factory=list[str])  # filenames
    missing: list[str] = field(default_factory=list[str])
    disabled: list[str] = field(default_factory=list[str])
    mismatch: list[tuple[str, str, str]] = field(
        default_factory=list[tuple[str, str, str]]
    )  # (name, xml_time, sched_time)
    wrong_root: list[tuple[str, str]] = field(
        default_factory=list[tuple[str, str]]
    )  # (name, registered_command)
    scheduler_unavailable: bool = False

    @property
    def has_problems(self) -> bool:
        return bool(
            self.manifest_errors
            or self.unparseable
            or self.no_uri
            or self.missing
            or self.disabled
            or self.mismatch
            or self.wrong_root
        )


def _parse_xml(path: Path) -> _XmlTask | None:
    """Extract task name and first trigger start time from a .task.xml file.

    Raises ``ParseError`` (or ``OSError``) when the file cannot be read or
    parsed — an unparseable task XML is a HARD failure that ``compare`` records
    as a problem, never a swallowed warning. Returns None only when the file
    parses cleanly but lacks a <URI> (a structurally incomplete task).
    """
    tree = ElementTree.parse(str(path))
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

    # A repeating trigger (e.g. hourly: Repetition Interval=PT1H) fires at
    # StartBoundary + n*interval, so schtasks' "Next Run Time" is almost never
    # the StartBoundary time — comparing the two would be a guaranteed false
    # "schedule mismatch". Flag it so the mismatch check skips these.
    has_repetition = root.find(f".//{ns}Repetition/{ns}Interval") is not None

    return _XmlTask(
        xml_path=path,
        task_name=task_name,
        start_time=start_time,
        enabled=enabled,
        has_repetition=has_repetition,
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


@lru_cache(maxsize=1)
def _query_task_commands() -> dict[str, str] | None:
    """Map lower-cased task name -> its registered "Task To Run" command.

    Cached: ``/v`` is markedly slower than the plain query and the answer
    cannot change within one audit run.

    Needs the verbose form: the plain ``/fo csv /nh`` query used by
    ``_query_schtasks`` returns only TaskName/Next Run Time/Status, so the
    action a task actually executes is invisible to it. Headers are kept (no
    ``/nh``) so the column is found by name rather than a fixed index — the
    verbose layout is long and locale-dependent.

    Returns None when the command is unavailable, so a scheduler we cannot
    interrogate degrades to "check skipped" rather than a false accusation
    that every task runs from the wrong checkout.
    """
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/v"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    commands: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(result.stdout))
    for row in reader:
        name = (row.get("TaskName") or "").strip()
        command = (row.get("Task To Run") or "").strip()
        # /v repeats the header row between folders; skip those echoes.
        if not name or name == "TaskName" or not command:
            continue
        commands[name.lower()] = command
    return commands or None


def _command_executable(command: str) -> str | None:
    """Extract the executable path from a "Task To Run" string.

    Handles both ``"C:\\dir with spaces\\run.bat" --flag`` and the bare
    ``C:\\dir\\run.bat`` form. Arguments are discarded — only the image path
    decides which checkout a task runs from.
    """
    text = command.strip()
    if not text:
        return None
    if text.startswith('"'):
        closing = text.find('"', 1)
        if closing == -1:
            return None
        return text[1:closing].strip() or None
    # Unquoted: such a path cannot contain spaces, so the first token is it.
    return text.split(" ", 1)[0].strip() or None


def _is_under(path_text: str, root: Path) -> bool:
    """Whether ``path_text`` sits inside ``root`` (case-insensitive on Windows)."""
    try:
        candidate = Path(path_text)
        root_text = os.path.normcase(str(root.resolve()))
        cand_text = os.path.normcase(str(candidate.resolve()))
    except (OSError, ValueError):
        return False
    return cand_text == root_text or cand_text.startswith(root_text + os.sep)


def _windows_service_is_running(service_name: str) -> bool:
    """Return whether a named Windows service positively reports RUNNING."""
    try:
        result = subprocess.run(
            ["sc.exe", "query", service_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return any(
        line.strip().startswith("STATE") and line.rstrip().endswith("RUNNING")
        for line in result.stdout.splitlines()
    )


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


def compare(
    cron_dir: Path = CRON_DIR,
    *,
    manifest: TaskManifest | None = None,
    project_root: Path | None = None,
) -> tuple[TaskReport, list[_XmlTask]]:
    """Parse manifest-declared XMLs and compare against the live scheduler.

    Returns (TaskReport, xml_tasks_list). Files that cannot be parsed are
    recorded under report.unparseable (a hard problem) rather than being
    skipped; files that parse but lack a <URI> go to report.no_uri. If schtasks
    cannot be queried, every parsed XML is listed as "missing" so the exit code
    is still non-zero.
    """
    # Resolved at call time, not bound as a default, so the module global stays
    # patchable (and a caller can audit a checkout other than this one).
    root = PROJECT_ROOT if project_root is None else project_root
    report = TaskReport()
    xml_tasks: list[_XmlTask] = []
    if manifest is None and cron_dir.resolve() == CRON_DIR.resolve():
        try:
            manifest = load_manifest(MANIFEST_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report.manifest_errors.append(f"{MANIFEST_PATH.name}: {exc}")

    if manifest is not None:
        report.manifest_errors.extend(validate_source_tree(manifest, cron_dir=cron_dir))
        paths = [cron_dir / task.xml for task in manifest.tasks]
    else:
        # Compatibility seam for isolated unit fixtures. The production CLI
        # always passes the canonical manifest and never discovers ownership
        # from directory contents.
        paths = sorted(cron_dir.glob("*.task.xml"))

    for p in paths:
        try:
            xt = _parse_xml(p)
        except (ParseError, OSError) as exc:
            report.unparseable.append(f"{p.name}: {exc}")
            continue
        if xt is None:
            report.no_uri.append(p.name)
            continue
        xml_tasks.append(xt)

    live = _query_schtasks()
    report.scheduler_unavailable = live is None
    # Which checkout each task actually executes from. A task can be
    # registered, enabled and on-schedule while running a *different* clone of
    # this repo — same wrapper filename, different root, its own data/
    # directory. That drift is invisible to every other check here.
    commands = _query_task_commands() if live is not None else None

    for xt in xml_tasks:
        key = xt.task_name.lower()
        if live is None or key not in live:
            if key == _CAPTURE_POLLER_TASK and _windows_service_is_running(_CAPTURE_POLLER_SERVICE):
                report.ok.append(
                    f"{xt.task_name} (scheduler absent; {_CAPTURE_POLLER_SERVICE} service running)"
                )
                continue
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
            if (
                key == _CAPTURE_POLLER_TASK
                and status.casefold() == "disabled"
                and _windows_service_is_running(_CAPTURE_POLLER_SERVICE)
            ):
                report.ok.append(
                    f"{xt.task_name} (scheduler disabled; "
                    f"{_CAPTURE_POLLER_SERVICE} service running)"
                )
                continue
            report.disabled.append(f"{xt.task_name} (Status={status!r})")
            continue

        # Schedule-mismatch check: compare trigger start time from XML vs
        # schtasks' "Next Run Time" column (only the time portion matters
        # since the date advances each run). Skipped for repeating triggers
        # (hourly etc.) — their next run is StartBoundary + n*interval, so the
        # time almost never equals StartBoundary and the check would always
        # false-positive.
        if xt.start_time is not None and not xt.has_repetition:
            next_run_time = _extract_next_run_time(task_info["next_run"])
            if next_run_time is not None and next_run_time != xt.start_time:
                report.mismatch.append((xt.task_name, xt.start_time, next_run_time))
                continue

        # Wrong-checkout check. The cron wrappers derive PROJECT_ROOT from
        # their own location (%~dp0..), so the registered image path alone
        # decides which repo — and therefore which data/portfolio.db — the run
        # uses. A task pointed at a second clone matches the manifest wrapper
        # name exactly and passes every check above, then runs against that
        # clone's database. On 2026-07-30 the whole fleet was re-pointed this
        # way and spent days "succeeding" against a 32KB empty stub.
        if commands is not None:
            registered = commands.get(key)
            exe = _command_executable(registered) if registered else None
            if exe is not None and not _is_under(exe, root):
                report.wrong_root.append((xt.task_name, registered or ""))
                continue

        report.ok.append(xt.task_name)

    return report, xml_tasks


def _print_report(report: TaskReport, xml_tasks: list[_XmlTask]) -> None:
    # Count every .task.xml discovered, including those we failed to parse —
    # otherwise the header would silently undercount the unparseable files.
    total = len(xml_tasks) + len(report.unparseable) + len(report.no_uri)
    ok = len(report.ok)
    print(f"\nCron registration check — {total} task XML(s) found\n")

    if not report.has_problems:
        print(f"  OK  All {ok} tasks parsed, registered and enabled\n")
        return

    if report.ok:
        print(f"  OK ({len(report.ok)}): {', '.join(report.ok)}\n")

    if report.manifest_errors:
        print(
            f"  MANIFEST ({len(report.manifest_errors)}) "
            "â€” coverage, identity, action, or schedule drift:"
        )
        for desc in report.manifest_errors:
            print(f"    x  {desc}")
        print()

    if report.unparseable:
        print(f"  UNPARSEABLE ({len(report.unparseable)}) — could not read/parse the XML:")
        for desc in report.unparseable:
            print(f"    x  {desc}")
        print()

    if report.no_uri:
        print(f"  NO URI ({len(report.no_uri)}) — parsed but missing <URI>:")
        for name in report.no_uri:
            print(f"    x  {name}")
        print()

    if report.missing:
        print(f"  MISSING ({len(report.missing)}) — not registered in Windows Task Scheduler:")
        for name in report.missing:
            print(f"    x  {name}")
        print()

    if report.disabled:
        print(f"  DISABLED ({len(report.disabled)}) — registered but not Ready:")
        for desc in report.disabled:
            print(f"    x  {desc}")
        print()

    if report.mismatch:
        print(f"  SCHEDULE MISMATCH ({len(report.mismatch)}) — registered time differs from XML:")
        for name, xml_time, sched_time in report.mismatch:
            print(f"    x  {name}: XML={xml_time} vs scheduler={sched_time}")
        print()

    if report.wrong_root:
        print(
            f"  WRONG CHECKOUT ({len(report.wrong_root)}) — registered outside "
            f"{PROJECT_ROOT} (runs against that clone's data/portfolio.db):"
        )
        for name, command in report.wrong_root:
            print(f"    x  {name}: {command}")
        print()

    problems = (
        len(report.manifest_errors)
        + len(report.unparseable)
        + len(report.no_uri)
        + len(report.missing)
        + len(report.disabled)
        + len(report.mismatch)
        + len(report.wrong_root)
    )
    print(f"  {problems} problem(s) found. Fix: re-run schtasks /create or check the XML.\n")


def report_payload(report: TaskReport, xml_tasks: list[_XmlTask]) -> dict[str, object]:
    """Stable machine-readable cron health contract for alerting consumers."""
    return {
        "status": "ok" if not report.has_problems else "failed",
        "task_count": len(xml_tasks) + len(report.unparseable) + len(report.no_uri),
        "ok": report.ok,
        "manifest_errors": report.manifest_errors,
        "unparseable": report.unparseable,
        "no_uri": report.no_uri,
        "missing": report.missing,
        "disabled": report.disabled,
        "mismatch": [
            {"task": name, "xml_time": xml_time, "scheduler_time": scheduler_time}
            for name, xml_time, scheduler_time in report.mismatch
        ],
        "wrong_root": [
            {"task": name, "command": command, "expected_root": str(PROJECT_ROOT)}
            for name, command in report.wrong_root
        ],
        "scheduler_unavailable": report.scheduler_unavailable,
    }


def _notify_alert_hook(payload: dict[str, object]) -> None:
    """Send failed cron health to an opt-in local alert hook.

    The executable path is operator-configured (``ES_CRON_ALERT_HOOK``) and is
    invoked without a shell; its stdin is the stable JSON health record.  A
    notification failure never hides the underlying scheduler failure.
    """
    hook = os.environ.get("ES_CRON_ALERT_HOOK")
    if not hook:
        return
    try:
        subprocess.run([hook], input=json.dumps(payload), text=True, check=False, timeout=30)
    except OSError as exc:
        sys.stderr.write(f"WARNING: cron alert hook failed: {exc}\n")


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
        "--manifest",
        type=Path,
        default=None,
        help="Canonical task manifest (default: cron/task_manifest.json).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable health record for an alert hook or dashboard.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable table; only the exit code matters.",
    )
    args = parser.parse_args(argv)

    manifest: TaskManifest | None = None
    manifest_path = args.manifest
    if manifest_path is not None or args.cron_dir.resolve() == CRON_DIR.resolve():
        try:
            manifest = load_manifest(manifest_path or MANIFEST_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"ERROR: cannot load task manifest: {exc}\n")
            return 1
    report, xml_tasks = compare(args.cron_dir, manifest=manifest)

    payload = report_payload(report, xml_tasks)
    if report.has_problems:
        _notify_alert_hook(payload)

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    elif not args.quiet:
        _print_report(report, xml_tasks)

    # "Nothing to check" means zero *.task.xml files on disk — NOT zero that
    # happened to parse. If every file failed to parse, xml_tasks is empty but
    # report.unparseable is not, and that must surface as a problem (exit 1),
    # never a false all-clear.
    total_found = len(xml_tasks) + len(report.unparseable) + len(report.no_uri)
    if total_found == 0:
        sys.stderr.write("WARNING: no *.task.xml files found — nothing to check.\n")
        return 0

    # If schtasks was unreachable, every task ends up in "missing" — return 2
    # (scheduler query failure) rather than 1 (task mismatch).
    if report.scheduler_unavailable:
        return 2

    return 1 if report.has_problems else 0


if __name__ == "__main__":
    sys.exit(main())
