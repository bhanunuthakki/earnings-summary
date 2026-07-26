"""Tests for execution/verify_cron_registration.py."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from execution.verify_cron_registration import (
    TaskReport,
    _extract_next_run_time,  # pyright: ignore[reportPrivateUsage]
    _parse_xml,  # pyright: ignore[reportPrivateUsage]
    _print_report,  # pyright: ignore[reportPrivateUsage]
    compare,
    main,
    report_payload,
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


def _write_hourly_task_xml(path: Path, uri: str, start_time: str = "00:17:00") -> None:
    """A daily trigger carrying an hourly Repetition (fires at :MM each hour)."""
    content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_NS}">
  <RegistrationInfo><URI>{uri}</URI></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-06-12T{start_time}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition><Interval>PT1H</Interval><Duration>P1D</Duration></Repetition>
    </CalendarTrigger>
  </Triggers>
  <Settings><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>C:\\foo.bat</Command></Exec></Actions>
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


def test_parse_xml_bad_content_raises(tmp_path: Path) -> None:
    # An unparseable task XML is a HARD failure — _parse_xml must raise, not
    # swallow it and return None (which silently dropped the file from the audit).
    p = tmp_path / "bad.task.xml"
    p.write_bytes(b"this is not xml at all <<<")
    with pytest.raises(ET.ParseError):
        _parse_xml(p)


def test_parse_xml_utf8_bytes_declaring_utf16_raises(tmp_path: Path) -> None:
    # Regression for the exact root-cause bug: UTF-8 bytes whose XML
    # declaration claims encoding="UTF-16". ET honors the declaration and
    # raises; this must propagate as a hard failure rather than be skipped.
    p = tmp_path / "mislabeled.task.xml"
    p.write_bytes(
        b'<?xml version="1.0" encoding="UTF-16"?>\r\n'
        b'<Task version="1.4" xmlns="' + _NS.encode() + b'">'
        b"<RegistrationInfo><URI>\\earnings-summary\\x</URI></RegistrationInfo></Task>"
    )
    with pytest.raises(ET.ParseError):
        _parse_xml(p)


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


def test_parse_xml_detects_repetition(tmp_path: Path) -> None:
    daily = tmp_path / "daily.task.xml"
    _write_task_xml(daily, r"\earnings-summary\daily")
    hourly = tmp_path / "hourly.task.xml"
    _write_hourly_task_xml(hourly, r"\earnings-summary\hourly")

    daily_task = _parse_xml(daily)
    hourly_task = _parse_xml(hourly)
    assert daily_task is not None and daily_task.has_repetition is False
    assert hourly_task is not None and hourly_task.has_repetition is True


def test_hourly_repetition_not_flagged_as_mismatch(tmp_path: Path) -> None:
    """An hourly task's next-run time is rarely its StartBoundary time, so the
    schedule-mismatch check must skip repeating triggers (regression: this used
    to flag onboard_pending forever)."""
    _write_hourly_task_xml(
        tmp_path / "onboard.task.xml", r"\earnings-summary\onboard_pending", "00:17:00"
    )
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\onboard_pending".lower(): {
                "name": r"\earnings-summary\onboard_pending",
                "next_run": "6/12/2026 7:17:00 PM",  # 19:17 — a later hourly occurrence
                "status": "Ready",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert not report.mismatch
    assert not report.has_problems


def test_compare_no_xmls_returns_empty(tmp_path: Path) -> None:
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        report, xml_tasks = compare(tmp_path)
    assert xml_tasks == []
    assert not report.has_problems


def test_report_payload_is_machine_readable() -> None:
    report = TaskReport(missing=[r"\earnings-summary\daily"])
    payload = report_payload(report, [])
    assert payload["status"] == "failed"
    assert payload["missing"] == [r"\earnings-summary\daily"]


def test_alert_hook_receives_only_failed_health(monkeypatch: pytest.MonkeyPatch) -> None:
    import execution.verify_cron_registration as verifier

    calls: list[list[str]] = []
    monkeypatch.setenv("ES_CRON_ALERT_HOOK", "C:/tools/alert.exe")

    def fake_run(command: list[str], **_kwargs: object) -> None:
        calls.append(command)

    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        fake_run,
    )
    verifier._notify_alert_hook(  # pyright: ignore[reportPrivateUsage]
        {"status": "failed"}
    )
    assert calls == [["C:/tools/alert.exe"]]


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
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 0


def test_main_exit_code_one_on_missing(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "t.task.xml", r"\earnings-summary\t", "03:00:00")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 1


# ---------------------------------------------------------------------------
# unparseable / no_uri: a malformed task XML must never be silently skipped
# ---------------------------------------------------------------------------


def _write_mislabeled_utf16(path: Path, uri: str = r"\earnings-summary\x") -> None:
    """UTF-8 bytes whose declaration claims encoding=UTF-16 (the prod root cause)."""
    path.write_bytes(
        b'<?xml version="1.0" encoding="UTF-16"?>\r\n'
        b'<Task version="1.4" xmlns="' + _NS.encode() + b'">'
        b"<RegistrationInfo><URI>" + uri.encode() + b"</URI></RegistrationInfo></Task>"
    )


def test_compare_unparseable_is_problem(tmp_path: Path) -> None:
    _write_mislabeled_utf16(tmp_path / "broken.task.xml")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        report, xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert len(report.unparseable) == 1
    assert "broken.task.xml" in report.unparseable[0]
    assert xml_tasks == []  # the malformed file is NOT counted as a checked task


def test_compare_no_uri_is_problem(tmp_path: Path) -> None:
    p = tmp_path / "nouri.task.xml"
    content = f'<?xml version="1.0"?><Task xmlns="{_NS}"><Settings/></Task>'
    p.write_text(content, encoding="utf-16")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert report.no_uri == ["nouri.task.xml"]


def test_main_exit_nonzero_on_unparseable(tmp_path: Path) -> None:
    _write_mislabeled_utf16(tmp_path / "broken.task.xml")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {}
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 1  # malformed file → problem, never a false exit-0


def test_print_report_uses_ascii_only_markers(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression: report markers must stay ASCII.

    The earlier ✓/✗ glyphs are not encodable in cp1252 (the default Windows
    console / redirected-log encoding), so the first problem line raised
    UnicodeEncodeError and crashed the weekly audit mid-report — exactly when it
    had something to say. The markers must be ASCII so the report always prints.
    """
    report = TaskReport()
    report.missing.append(r"\earnings-summary\not_registered")
    report.mismatch.append((r"\earnings-summary\w", "03:00:00", "05:00:00"))

    _print_report(report, [])
    out = capsys.readouterr().out

    assert "✓" not in out
    assert "✗" not in out
    # Got past the former crash point: the problem sections actually rendered.
    assert "MISSING" in out
    assert "SCHEDULE MISMATCH" in out
    # And the output is encodable in cp1252 (the prod console/log codec).
    out.encode("cp1252")


def test_main_no_false_allclear_when_unparseable_mixed_with_ok(tmp_path: Path) -> None:
    # One perfectly-registered task plus one malformed file. The audit must NOT
    # report "all clear" while ignoring the file it failed to parse.
    _write_task_xml(tmp_path / "good.task.xml", r"\earnings-summary\good", "03:00:00")
    _write_mislabeled_utf16(tmp_path / "broken.task.xml")
    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            r"\earnings-summary\good".lower(): {
                "name": r"\earnings-summary\good",
                "next_run": "6/12/2026 3:00:00 AM",
                "status": "Ready",
            }
        }
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 1
