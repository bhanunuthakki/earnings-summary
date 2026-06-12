"""Tests for execution/verify_cron_registration.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from execution.verify_cron_registration import (
    _extract_next_run_time,  # pyright: ignore[reportPrivateUsage]
    _parse_xml,  # pyright: ignore[reportPrivateUsage]
    compare,
)

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _write_task_xml(
    path: Path, uri: str, start_time: str = "03:00:00", enabled: bool = True
) -> None:
    content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_NS}">
  <RegistrationInfo>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-06-12T{start_time}</StartBoundary>
      <Enabled>{"true" if enabled else "false"}</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings><Enabled>true</Enabled></Settings>
  <Actions Context="Author">
    <Exec><Command>C:\\foo.bat</Command></Exec>
  </Actions>
</Task>"""
    path.write_text(content, encoding="utf-16")


# ---------------------------------------------------------------------------
# _parse_xml
# ---------------------------------------------------------------------------


def test_parse_xml_extracts_uri_and_time(tmp_path: Path) -> None:
    p = tmp_path / "test.task.xml"
    _write_task_xml(p, r"\earnings-summary\refresh_cache", "03:00:00")
    task = _parse_xml(p)
    assert task is not None
    assert task.task_name == r"\earnings-summary\refresh_cache"
    assert task.start_time == "03:00:00"
    assert task.enabled is True


def test_parse_xml_disabled_flag(tmp_path: Path) -> None:
    p = tmp_path / "disabled.task.xml"
    _write_task_xml(p, r"\earnings-summary\disabled_task", enabled=False)
    task = _parse_xml(p)
    assert task is not None
    assert task.enabled is False


def test_parse_xml_missing_uri_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "nouri.task.xml"
    content = f'<?xml version="1.0"?><Task xmlns="{_NS}"><Settings/></Task>'
    p.write_text(content, encoding="utf-16")
    assert _parse_xml(p) is None


def test_parse_xml_bad_encoding_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "bad.task.xml"
    p.write_bytes(b"this is not xml at all <<<")
    assert _parse_xml(p) is None


# ---------------------------------------------------------------------------
# _extract_next_run_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("6/12/2026 3:00:00 AM", "03:00:00"),
        ("6/12/2026 5:35:00 AM", "05:35:00"),
        ("6/12/2026 11:30:00 PM", "23:30:00"),
        ("6/12/2026 12:00:00 PM", "12:00:00"),
        ("6/12/2026 12:00:00 AM", "00:00:00"),
        ("N/A", None),
    ],
)
def test_extract_next_run_time(raw: str, expected: str | None) -> None:
    assert _extract_next_run_time(raw) == expected


# ---------------------------------------------------------------------------
# compare: mock schtasks
# ---------------------------------------------------------------------------


def test_compare_all_ok(tmp_path: Path) -> None:
    _write_task_xml(
        tmp_path / "refresh_cache.task.xml", r"\earnings-summary\refresh_cache", "03:00:00"
    )
    _write_task_xml(tmp_path / "backup_db.task.xml", r"\earnings-summary\backup_db", "02:45:00")

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\refresh_cache".lower(): {
                "name": r"\earnings-summary\refresh_cache",
                "next_run": "6/12/2026 3:00:00 AM",
                "status": "Ready",
            },
            r"\earnings-summary\backup_db".lower(): {
                "name": r"\earnings-summary\backup_db",
                "next_run": "6/12/2026 2:45:00 AM",
                "status": "Ready",
            },
        }
        report, _xml_tasks = compare(tmp_path)

    assert not report.has_problems
    assert len(report.ok) == 2
    assert not report.missing
    assert not report.disabled
    assert not report.mismatch


def test_compare_missing_task(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "missing.task.xml", r"\earnings-summary\not_registered", "04:00:00")

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert r"\earnings-summary\not_registered" in report.missing


def test_compare_disabled_task(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "disabled.task.xml", r"\earnings-summary\disabled", "04:00:00")

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\disabled".lower(): {
                "name": r"\earnings-summary\disabled",
                "next_run": "N/A",
                "status": "Disabled",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert any("disabled" in d.lower() for d in report.disabled)


def test_compare_schedule_mismatch(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "wrong_time.task.xml", r"\earnings-summary\wrong", "03:00:00")

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\wrong".lower(): {
                "name": r"\earnings-summary\wrong",
                "next_run": "6/12/2026 5:00:00 AM",  # different time
                "status": "Ready",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert len(report.mismatch) == 1
    assert report.mismatch[0][0] == r"\earnings-summary\wrong"


def test_compare_no_xmls_returns_empty(tmp_path: Path) -> None:
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        report, xml_tasks = compare(tmp_path)
    assert xml_tasks == []
    assert not report.has_problems


def test_main_exit_code_zero_all_ok(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "t.task.xml", r"\earnings-summary\t", "03:00:00")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\t".lower(): {
                "name": r"\earnings-summary\t",
                "next_run": "6/12/2026 3:00:00 AM",
                "status": "Ready",
            }
        }
        from execution.verify_cron_registration import main

        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 0


def test_main_exit_code_one_on_missing(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "t.task.xml", r"\earnings-summary\t", "03:00:00")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        from execution.verify_cron_registration import main

        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 1
