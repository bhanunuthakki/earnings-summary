"""Regression checks for human-readable schedule metadata in task XML."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.mark.parametrize(
    ("filename", "weekday", "time"),
    [
        ("model_eval_sweep.task.xml", "Saturday", "20:00"),
        ("grade_calibration.task.xml", "Sunday", "10:30"),
    ],
)
def test_task_description_matches_weekly_trigger(
    filename: str,
    weekday: str,
    time: str,
) -> None:
    root = ET.parse(ROOT / "cron" / filename).getroot()
    description = root.findtext("t:RegistrationInfo/t:Description", namespaces=NS)
    start_boundary = root.findtext(".//t:StartBoundary", namespaces=NS)

    assert description is not None
    assert f"{weekday} {time}" in description
    assert start_boundary is not None
    assert start_boundary.endswith(f"T{time}:00")
    assert root.find(f".//t:DaysOfWeek/t:{weekday}", NS) is not None
