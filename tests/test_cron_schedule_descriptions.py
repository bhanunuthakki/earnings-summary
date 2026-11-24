"""Regression checks for human-readable schedule metadata in task XML."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_model_eval_description_matches_monthly_trigger() -> None:
    root = ET.parse(ROOT / "cron" / "model_eval_sweep.task.xml").getroot()
    description = root.findtext("t:RegistrationInfo/t:Description", namespaces=NS)
    start_boundary = root.findtext(".//t:StartBoundary", namespaces=NS)

    assert description is not None
    assert "Monthly, day 1 20:00" in description
    assert start_boundary is not None
    assert start_boundary.endswith("T20:00:00")
    assert root.findtext(".//t:ScheduleByMonth/t:DaysOfMonth/t:Day", namespaces=NS) == "1"
    assert root.find(".//t:DaysOfWeek", NS) is None
