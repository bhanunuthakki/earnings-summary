"""Tests for execution/verify_cron_registration.py."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from execution.verify_cron_registration import (
    TaskReport,
    _command_executable,  # pyright: ignore[reportPrivateUsage]
    _extract_next_run_time,  # pyright: ignore[reportPrivateUsage]
    _parse_xml,  # pyright: ignore[reportPrivateUsage]
    _print_report,  # pyright: ignore[reportPrivateUsage]
    _windows_service_is_running,  # pyright: ignore[reportPrivateUsage]
    compare,
    main,
    report_payload,
)

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _write_task_xml(
    path: Path,
    uri: str,
    start_time: str = "03:00:00",
    *,
    trigger_enabled: bool = True,
    settings_enabled: bool = True,
) -> None:
    content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_NS}">
  <RegistrationInfo>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-06-12T{start_time}</StartBoundary>
      <Enabled>{"true" if trigger_enabled else "false"}</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings><Enabled>{"true" if settings_enabled else "false"}</Enabled></Settings>
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
    _write_task_xml(
        p,
        r"\earnings-summary\disabled_task",
        trigger_enabled=True,
        settings_enabled=False,
    )
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

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        # These fixtures reuse REAL task names, so leaving the action lookup
        # live would consult this machine's scheduler and judge it against a
        # tmp_path root. This test is about registration, not checkout roots.
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
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


def test_compare_flags_stale_required_writer_as_extra(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = r"\earnings-summary\daily"
    stale = r"\earnings-summary\stale_required_writer"
    _write_task_xml(tmp_path / "daily.task.xml", expected, "03:00:00")

    with (
        patch(
            "execution.verify_cron_registration._query_schtasks",
            return_value={
                expected.lower(): _ready(expected),
                stale.lower(): _ready(stale),
            },
        ),
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
        report, xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert report.extra == [stale]
    payload = report_payload(report, xml_tasks)
    assert payload["status"] == "failed"
    assert payload["extra"] == [stale]

    _print_report(report, xml_tasks)
    output = capsys.readouterr().out
    assert "EXTRA" in output
    assert stale in output


def test_compare_ignores_live_tasks_outside_product_namespace(tmp_path: Path) -> None:
    expected = r"\earnings-summary\daily"
    unrelated = r"\Microsoft\Windows\Maintenance\WinSAT"
    _write_task_xml(tmp_path / "daily.task.xml", expected, "03:00:00")

    with (
        patch(
            "execution.verify_cron_registration._query_schtasks",
            return_value={
                expected.lower(): _ready(expected),
                unrelated.lower(): _ready(unrelated),
            },
        ),
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
        report, _xml_tasks = compare(tmp_path)

    assert not report.has_problems
    assert report.extra == []


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


def test_compare_source_disabled_task_requires_live_disabled_state(tmp_path: Path) -> None:
    task_name = r"\earnings-summary\held"
    _write_task_xml(
        tmp_path / "held.task.xml",
        task_name,
        settings_enabled=False,
    )

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            task_name.lower(): {
                "name": task_name,
                "next_run": "N/A",
                "status": "Ready",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert report.ok == []
    assert report.disabled == [f"{task_name} (XML disabled, Status='Ready')"]


def test_compare_source_disabled_task_accepts_live_disabled_state(tmp_path: Path) -> None:
    task_name = r"\earnings-summary\held"
    _write_task_xml(
        tmp_path / "held.task.xml",
        task_name,
        settings_enabled=False,
    )

    with patch("execution.verify_cron_registration._query_schtasks") as mock_q:
        mock_q.return_value = {
            task_name.lower(): {
                "name": task_name,
                "next_run": "N/A",
                "status": "Disabled",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert not report.has_problems
    assert report.ok == [f"{task_name} (xml disabled, scheduler disabled)"]


def test_capture_poller_disabled_is_ok_when_service_is_running(tmp_path: Path) -> None:
    task_name = r"\earnings-summary\capture_poller"
    _write_task_xml(tmp_path / "capture_poller.task.xml", task_name, "04:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_tasks,
        patch(
            "execution.verify_cron_registration._windows_service_is_running",
            return_value=True,
        ) as mock_service,
    ):
        mock_tasks.return_value = {
            task_name.lower(): {
                "name": task_name,
                "next_run": "N/A",
                "status": "Disabled",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert not report.has_problems
    assert report.extra == []
    assert report.disabled == []
    assert report.ok == [rf"{task_name} (scheduler disabled; es-poller service running)"]
    mock_service.assert_called_once_with("es-poller")


def test_capture_poller_absent_is_ok_when_service_is_running(tmp_path: Path) -> None:
    task_name = r"\earnings-summary\capture_poller"
    _write_task_xml(tmp_path / "capture_poller.task.xml", task_name, "04:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks", return_value={}),
        patch(
            "execution.verify_cron_registration._windows_service_is_running",
            return_value=True,
        ) as mock_service,
    ):
        report, _xml_tasks = compare(tmp_path)

    assert not report.has_problems
    assert report.extra == []
    assert report.missing == []
    assert report.ok == [rf"{task_name} (scheduler absent; es-poller service running)"]
    mock_service.assert_called_once_with("es-poller")


def test_capture_poller_disabled_is_problem_when_service_is_stopped(
    tmp_path: Path,
) -> None:
    task_name = r"\earnings-summary\capture_poller"
    _write_task_xml(tmp_path / "capture_poller.task.xml", task_name, "04:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_tasks,
        patch(
            "execution.verify_cron_registration._windows_service_is_running",
            return_value=False,
        ),
    ):
        mock_tasks.return_value = {
            task_name.lower(): {
                "name": task_name,
                "next_run": "N/A",
                "status": "Disabled",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert report.ok == []
    assert report.disabled == [rf"{task_name} (Status='Disabled')"]


def test_other_disabled_task_is_not_waived_by_poller_service(tmp_path: Path) -> None:
    task_name = r"\earnings-summary\other"
    _write_task_xml(tmp_path / "other.task.xml", task_name, "04:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_tasks,
        patch("execution.verify_cron_registration._windows_service_is_running") as mock_service,
    ):
        mock_tasks.return_value = {
            task_name.lower(): {
                "name": task_name,
                "next_run": "N/A",
                "status": "Disabled",
            }
        }
        report, _xml_tasks = compare(tmp_path)

    assert report.has_problems
    assert report.disabled == [rf"{task_name} (Status='Disabled')"]
    mock_service.assert_not_called()


def test_windows_service_running_requires_positive_running_state() -> None:
    completed = __import__("subprocess").CompletedProcess(
        args=["sc.exe", "query", "es-poller"],
        returncode=0,
        stdout=(
            "SERVICE_NAME: es-poller\n"
            "        TYPE               : 10  WIN32_OWN_PROCESS\n"
            "        STATE              : 4  RUNNING\n"
        ),
        stderr="",
    )
    with patch(
        "execution.verify_cron_registration.subprocess.run",
        return_value=completed,
    ):
        assert _windows_service_is_running("es-poller") is True


def test_windows_service_query_failure_is_not_healthy() -> None:
    with patch(
        "execution.verify_cron_registration.subprocess.run",
        side_effect=OSError("sc.exe unavailable"),
    ):
        assert _windows_service_is_running("es-poller") is False


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
    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        # Real task name — keep the live action lookup out of this test.
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
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


def test_main_exit_code_two_only_when_scheduler_unavailable(tmp_path: Path) -> None:
    _write_task_xml(tmp_path / "t.task.xml", r"\earnings-summary\t", "03:00:00")
    with patch("execution.verify_cron_registration._query_schtasks", return_value=None):
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 2


def test_main_exit_code_one_on_extra_scheduler_task(tmp_path: Path) -> None:
    expected = r"\earnings-summary\daily"
    stale = r"\earnings-summary\stale_required_writer"
    _write_task_xml(tmp_path / "daily.task.xml", expected, "03:00:00")
    with (
        patch(
            "execution.verify_cron_registration._query_schtasks",
            return_value={
                expected.lower(): _ready(expected),
                stale.lower(): _ready(stale),
            },
        ),
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])
    assert rc == 1


def test_main_exit_code_one_on_manifest_error_with_empty_manifest(
    tmp_path: Path,
) -> None:
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "orphan.task.xml").write_text("<Task/>", encoding="utf-8")
    (cron_dir / "run_orphan.bat").write_text("@echo off\n", encoding="utf-8")
    manifest = tmp_path / "task_manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "namespace": r"\earnings-summary", "tasks": []}),
        encoding="utf-8",
    )

    with patch(
        "execution.verify_cron_registration._query_schtasks",
        return_value={},
    ):
        rc = main(
            [
                "--cron-dir",
                str(cron_dir),
                "--manifest",
                str(manifest),
                "--quiet",
            ]
        )

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


# ---------------------------------------------------------------------------
# wrong-checkout detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r'"C:\repo dir\cron\run_x.bat" --flag', r"C:\repo dir\cron\run_x.bat"),
        (r"C:\repo\cron\run_x.bat", r"C:\repo\cron\run_x.bat"),
        (r"C:\repo\cron\run_x.bat --flag", r"C:\repo\cron\run_x.bat"),
        ('"unterminated', None),
        ("", None),
        ("   ", None),
    ],
)
def test_command_executable_extracts_image_path(command: str, expected: str | None) -> None:
    assert _command_executable(command) == expected


def _ready(name: str, next_run: str = "6/12/2026 3:00:00 AM") -> dict[str, str]:
    return {"name": name, "next_run": next_run, "status": "Ready"}


def test_compare_flags_task_registered_in_another_checkout(tmp_path: Path) -> None:
    """The 2026-07-30 outage: right wrapper name, wrong repo, own empty DB."""
    root = tmp_path / "main-checkout"
    other = tmp_path / "runtime-checkout"
    (root / "cron").mkdir(parents=True)
    (other / "cron").mkdir(parents=True)
    name = r"\earnings-summary\refresh_cache"
    _write_task_xml(tmp_path / "refresh_cache.task.xml", name, "03:00:00")

    stray = str(other / "cron" / "run_refresh_cache.bat")
    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        patch("execution.verify_cron_registration._query_task_commands") as mock_c,
    ):
        mock_q.return_value = {name.lower(): _ready(name)}
        mock_c.return_value = {name.lower(): stray}
        report, _xml = compare(tmp_path, project_root=root)

    assert report.has_problems
    assert [n for n, _cmd in report.wrong_root] == [name]
    # It is NOT reported as healthy — the whole point is that every other
    # check passes for this task.
    assert name not in report.ok
    assert not report.missing and not report.disabled and not report.mismatch


def test_compare_accepts_task_registered_in_this_checkout(tmp_path: Path) -> None:
    root = tmp_path / "main-checkout"
    (root / "cron").mkdir(parents=True)
    name = r"\earnings-summary\refresh_cache"
    _write_task_xml(tmp_path / "refresh_cache.task.xml", name, "03:00:00")

    good = f'"{root / "cron" / "run_refresh_cache.bat"}"'
    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        patch("execution.verify_cron_registration._query_task_commands") as mock_c,
    ):
        mock_q.return_value = {name.lower(): _ready(name)}
        mock_c.return_value = {name.lower(): good}
        report, _xml = compare(tmp_path, project_root=root)

    assert not report.has_problems
    assert name in report.ok


def test_compare_skips_root_check_when_commands_unavailable(tmp_path: Path) -> None:
    """No action data (non-Windows, permission error) must not accuse every task."""
    root = tmp_path / "main-checkout"
    (root / "cron").mkdir(parents=True)
    name = r"\earnings-summary\refresh_cache"
    _write_task_xml(tmp_path / "refresh_cache.task.xml", name, "03:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        patch("execution.verify_cron_registration._query_task_commands", return_value=None),
    ):
        mock_q.return_value = {name.lower(): _ready(name)}
        report, _xml = compare(tmp_path, project_root=root)

    assert not report.has_problems
    assert not report.wrong_root
    assert name in report.ok


def test_wrong_root_surfaces_in_payload_and_output(tmp_path: Path) -> None:
    root = tmp_path / "main-checkout"
    other = tmp_path / "runtime-checkout"
    (root / "cron").mkdir(parents=True)
    (other / "cron").mkdir(parents=True)
    name = r"\earnings-summary\refresh_cache"
    _write_task_xml(tmp_path / "refresh_cache.task.xml", name, "03:00:00")

    stray = str(other / "cron" / "run_refresh_cache.bat")
    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        patch("execution.verify_cron_registration._query_task_commands") as mock_c,
    ):
        mock_q.return_value = {name.lower(): _ready(name)}
        mock_c.return_value = {name.lower(): stray}
        report, xml_tasks = compare(tmp_path, project_root=root)

    payload = report_payload(report, xml_tasks)
    assert payload["status"] == "failed"
    # report_payload's contract is dict[str, object]; narrow at the boundary.
    entries = cast(list[dict[str, str]], payload["wrong_root"])
    assert len(entries) == 1
    assert entries[0]["task"] == name
    assert entries[0]["command"] == stray

    _print_report(report, xml_tasks)


def test_main_exits_nonzero_on_wrong_checkout(tmp_path: Path) -> None:
    root = tmp_path / "main-checkout"
    other = tmp_path / "runtime-checkout"
    (root / "cron").mkdir(parents=True)
    (other / "cron").mkdir(parents=True)
    name = r"\earnings-summary\refresh_cache"
    _write_task_xml(tmp_path / "refresh_cache.task.xml", name, "03:00:00")

    with (
        patch("execution.verify_cron_registration._query_schtasks") as mock_q,
        patch("execution.verify_cron_registration._query_task_commands") as mock_c,
        patch("execution.verify_cron_registration.PROJECT_ROOT", root),
    ):
        mock_q.return_value = {name.lower(): _ready(name)}
        mock_c.return_value = {name.lower(): str(other / "cron" / "run_refresh_cache.bat")}
        rc = main(["--cron-dir", str(tmp_path), "--quiet"])

    assert rc == 1
