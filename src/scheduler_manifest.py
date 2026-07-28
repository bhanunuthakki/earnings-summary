"""Canonical Windows Task Scheduler inventory and deterministic artifacts."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import cast

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
NS = f"{{{TASK_NS}}}"
MANIFEST_VERSION = 1
SERVICE_OWNED_TASKS = frozenset({r"\earnings-summary\capture_poller".casefold()})
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    trigger: str
    start_boundary: str | None
    repetition_interval: str | None
    days_interval: int | None
    weeks_interval: int | None
    days_of_week: tuple[str, ...]
    days_of_month: tuple[int, ...]
    months: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "start_boundary": self.start_boundary,
            "repetition_interval": self.repetition_interval,
            "days_interval": self.days_interval,
            "weeks_interval": self.weeks_interval,
            "days_of_week": list(self.days_of_week),
            "days_of_month": list(self.days_of_month),
            "months": list(self.months),
        }


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_name: str
    xml: str
    wrapper: str
    schedule: ScheduleSpec

    def as_json(self) -> dict[str, object]:
        return {
            "task_name": self.task_name,
            "xml": self.xml,
            "wrapper": self.wrapper,
            "schedule": self.schedule.as_json(),
        }


@dataclass(frozen=True, slots=True)
class TaskManifest:
    version: int
    namespace: str
    tasks: tuple[TaskSpec, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "namespace": self.namespace,
            "tasks": [task.as_json() for task in self.tasks],
        }


@dataclass(frozen=True, slots=True)
class XmlTaskMetadata:
    task_name: str
    command: str
    enabled: bool
    schedule: ScheduleSpec


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _optional_int(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _string_tuple(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of strings")
    output: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise ValueError(f"{key} must be a list of strings")
        output.append(item)
    return tuple(output)


def _int_tuple(data: Mapping[str, object], key: str) -> tuple[int, ...]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of integers")
    output: list[int] = []
    for item in cast(list[object], value):
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError(f"{key} must be a list of integers")
        output.append(item)
    return tuple(output)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object with string keys")
    output: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise ValueError(f"{label} must be an object with string keys")
        output[key] = item
    return output


def load_manifest(path: Path) -> TaskManifest:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    root = _mapping(raw, "manifest")
    version = root.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != MANIFEST_VERSION:
        raise ValueError(f"unsupported task manifest version {version!r}")
    namespace = _required_str(root, "namespace")
    raw_tasks = root.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("tasks must be a list")
    task_values = cast(list[object], raw_tasks)
    tasks: list[TaskSpec] = []
    for index, raw_task in enumerate(task_values):
        task = _mapping(raw_task, f"tasks[{index}]")
        raw_schedule = _mapping(task.get("schedule"), f"tasks[{index}].schedule")
        tasks.append(
            TaskSpec(
                task_name=_required_str(task, "task_name"),
                xml=_required_str(task, "xml"),
                wrapper=_required_str(task, "wrapper"),
                schedule=ScheduleSpec(
                    trigger=_required_str(raw_schedule, "trigger"),
                    start_boundary=_optional_str(raw_schedule, "start_boundary"),
                    repetition_interval=_optional_str(raw_schedule, "repetition_interval"),
                    days_interval=_optional_int(raw_schedule, "days_interval"),
                    weeks_interval=_optional_int(raw_schedule, "weeks_interval"),
                    days_of_week=_string_tuple(raw_schedule, "days_of_week"),
                    days_of_month=_int_tuple(raw_schedule, "days_of_month"),
                    months=_string_tuple(raw_schedule, "months"),
                ),
            )
        )
    return TaskManifest(version=version, namespace=namespace, tasks=tuple(tasks))


def dump_manifest(manifest: TaskManifest) -> str:
    return json.dumps(manifest.as_json(), indent=2, ensure_ascii=False) + "\n"


def _text(root: ET.Element, path: str) -> str | None:
    element = root.find(path)
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _child_names(element: ET.Element | None) -> tuple[str, ...]:
    if element is None:
        return ()
    return tuple(child.tag.rsplit("}", 1)[-1] for child in element)


def extract_xml_metadata(path: Path) -> XmlTaskMetadata:
    root = ET.parse(path).getroot()
    uri = _text(root, f".//{NS}URI")
    if uri is None:
        raise ValueError("missing RegistrationInfo/URI")
    command = _text(root, f".//{NS}Actions/{NS}Exec/{NS}Command")
    if command is None:
        raise ValueError("missing Actions/Exec/Command")
    trigger_parent = root.find(f".//{NS}Triggers")
    if trigger_parent is None or len(trigger_parent) != 1:
        raise ValueError("exactly one trigger is required")
    trigger = trigger_parent[0]
    trigger_name = trigger.tag.rsplit("}", 1)[-1]
    start_boundary = _text(trigger, f"{NS}StartBoundary")
    repetition_interval = _text(trigger, f"{NS}Repetition/{NS}Interval")
    daily = trigger.find(f"{NS}ScheduleByDay")
    weekly = trigger.find(f"{NS}ScheduleByWeek")
    monthly = trigger.find(f"{NS}ScheduleByMonth")
    days_interval_text = _text(daily, f"{NS}DaysInterval") if daily is not None else None
    weeks_interval_text = _text(weekly, f"{NS}WeeksInterval") if weekly is not None else None
    days_of_week = _child_names(weekly.find(f"{NS}DaysOfWeek") if weekly is not None else None)
    days_of_month = tuple(
        int(day.text)
        for day in (monthly.findall(f"{NS}DaysOfMonth/{NS}Day") if monthly is not None else [])
        if day.text is not None
    )
    months = _child_names(monthly.find(f"{NS}Months") if monthly is not None else None)
    enabled_text = _text(root, f".//{NS}Settings/{NS}Enabled")
    return XmlTaskMetadata(
        task_name=uri,
        command=command,
        enabled=enabled_text is None or enabled_text.casefold() != "false",
        schedule=ScheduleSpec(
            trigger=trigger_name,
            start_boundary=start_boundary,
            repetition_interval=repetition_interval,
            days_interval=int(days_interval_text) if days_interval_text is not None else None,
            weeks_interval=int(weeks_interval_text) if weeks_interval_text is not None else None,
            days_of_week=days_of_week,
            days_of_month=days_of_month,
            months=months,
        ),
    )


def bootstrap_manifest(cron_dir: Path, *, namespace: str = r"\earnings-summary") -> TaskManifest:
    """Build the initial manifest from the current XML fleet.

    This is intentionally a bootstrap-only operation. Normal validation reads
    the checked-in manifest first and treats it as authoritative.
    """
    tasks: list[TaskSpec] = []
    for xml_path in sorted(cron_dir.glob("*.task.xml")):
        metadata = extract_xml_metadata(xml_path)
        wrapper = PureWindowsPath(metadata.command).name
        tasks.append(
            TaskSpec(
                task_name=metadata.task_name,
                xml=xml_path.name,
                wrapper=wrapper,
                schedule=metadata.schedule,
            )
        )
    return TaskManifest(version=MANIFEST_VERSION, namespace=namespace, tasks=tuple(tasks))


def validate_source_tree(
    manifest: TaskManifest,
    *,
    cron_dir: Path,
) -> list[str]:
    errors: list[str] = []
    task_names = [task.task_name.casefold() for task in manifest.tasks]
    xml_names = [task.xml.casefold() for task in manifest.tasks]
    wrapper_names = [task.wrapper.casefold() for task in manifest.tasks]
    for label, values in (
        ("task_name", task_names),
        ("xml", xml_names),
        ("wrapper", wrapper_names),
    ):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        errors.extend(f"duplicate {label}: {value}" for value in duplicates)

    disk_xml = {path.name.casefold() for path in cron_dir.glob("*.task.xml")}
    disk_wrappers = {
        path.name.casefold()
        for path in cron_dir.glob("run_*.bat")
        if path.name.casefold() != "run_python.bat"
    }
    for orphan in sorted(disk_xml - set(xml_names)):
        errors.append(f"orphan XML not in manifest: {orphan}")
    for missing in sorted(set(xml_names) - disk_xml):
        errors.append(f"manifest XML missing on disk: {missing}")
    for orphan in sorted(disk_wrappers - set(wrapper_names)):
        errors.append(f"orphan wrapper not in manifest: {orphan}")
    for missing in sorted(set(wrapper_names) - disk_wrappers):
        errors.append(f"manifest wrapper missing on disk: {missing}")

    expected_prefix = manifest.namespace.rstrip("\\").casefold() + "\\"
    for task in manifest.tasks:
        if not task.task_name.casefold().startswith(expected_prefix):
            errors.append(f"{task.xml}: task name outside {manifest.namespace}: {task.task_name}")
            continue
        xml_path = cron_dir / task.xml
        if not xml_path.is_file():
            continue
        try:
            metadata = extract_xml_metadata(xml_path)
        except (ET.ParseError, OSError, ValueError) as exc:
            errors.append(f"{task.xml}: {exc}")
            continue
        if metadata.task_name.casefold() != task.task_name.casefold():
            errors.append(f"{task.xml}: URI {metadata.task_name!r} != manifest {task.task_name!r}")
        if PureWindowsPath(metadata.command).name.casefold() != task.wrapper.casefold():
            errors.append(
                f"{task.xml}: action {metadata.command!r} does not invoke {task.wrapper!r}"
            )
        if metadata.schedule != task.schedule:
            errors.append(f"{task.xml}: schedule differs from manifest")
    return errors


def rendered_xml_bytes(task: TaskSpec, *, cron_dir: Path, project_root: Path) -> bytes:
    """Render XML whose action is rooted in the checkout invoking generation."""
    tree = ET.parse(cron_dir / task.xml)
    root = tree.getroot()
    command = root.find(f".//{NS}Actions/{NS}Exec/{NS}Command")
    if command is None:
        raise ValueError(f"{task.xml}: missing Actions/Exec/Command")
    command.text = str(project_root.resolve() / "cron" / task.wrapper)
    ET.register_namespace("", TASK_NS)
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def schedule_label(schedule: ScheduleSpec) -> str:
    if schedule.trigger != "CalendarTrigger":
        return schedule.trigger
    time = (
        schedule.start_boundary.split("T", 1)[1]
        if schedule.start_boundary and "T" in schedule.start_boundary
        else "unspecified"
    )
    if schedule.repetition_interval:
        return f"daily from {time}, repeats {schedule.repetition_interval}"
    if schedule.days_interval is not None:
        return f"daily at {time}"
    if schedule.weeks_interval is not None:
        return f"weekly {','.join(schedule.days_of_week)} at {time}"
    if schedule.days_of_month:
        return f"monthly day {','.join(str(day) for day in schedule.days_of_month)} at {time}"
    return f"calendar at {time}"


def generated_registration_script(manifest: TaskManifest) -> str:
    lines = [
        "# Generated from cron/task_manifest.json. Do not edit by hand.",
        "param(",
        "    [Parameter(Mandatory=$true)][string]$Python,",
        "    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path",
        ")",
        "$ErrorActionPreference = 'Stop'",
        "$renderDir = Join-Path $RepoRoot '.tmp\\scheduler_tasks'",
        "& $Python (Join-Path $RepoRoot 'execution\\generate_cron_artifacts.py') "
        "--project-root $RepoRoot --render-dir $renderDir --check",
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
    ]
    for task in manifest.tasks:
        if task.task_name.casefold() in SERVICE_OWNED_TASKS:
            continue
        lines.append(
            f"& schtasks.exe /Create /TN '{task.task_name}' "
            f"/XML (Join-Path $renderDir '{task.xml}') /F"
        )
    return "\n".join(lines) + "\n"


def generated_inventory_markdown(manifest: TaskManifest) -> str:
    lines = [
        "# Scheduled task inventory",
        "",
        "Generated from `cron/task_manifest.json`; do not edit by hand.",
        "",
        "| Task | Schedule | XML | Wrapper | Owner |",
        "|---|---|---|---|---|",
    ]
    for task in manifest.tasks:
        ownership = (
            "Windows service"
            if task.task_name.casefold() in SERVICE_OWNED_TASKS
            else "Task Scheduler"
        )
        lines.append(
            f"| `{task.task_name}` | {schedule_label(task.schedule)} | "
            f"`{task.xml}` | `{task.wrapper}` | {ownership} |"
        )
    lines.extend(
        [
            "",
            "Registration renders each XML action against the checkout invoking the command:",
            "",
            "```powershell",
            "powershell -File cron/register_tasks.generated.ps1 "
            "-Python <path-to-python.exe> -RepoRoot (Resolve-Path .)",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
