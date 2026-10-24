"""Report synchronization ownership and publication regressions."""

from __future__ import annotations

import os
import subprocess
import sys
from contextvars import Context
from pathlib import Path

import pytest

from report.artifacts import (
    hold_report_index,
    load_report_artifact_index,
    reconcile_legacy_workspace_reports,
)
from runtime.job_runtime import JobAlreadyRunningError


def test_index_lock_is_cross_process_and_follows_resolved_output(tmp_path: Path) -> None:
    script = """
import sys
from pathlib import Path
from report.artifacts import hold_report_index
from runtime.job_runtime import JobAlreadyRunningError
try:
    with hold_report_index(Path(sys.argv[1]), wait_s=0):
        pass
except JobAlreadyRunningError:
    sys.exit(75)
"""
    with hold_report_index(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 75, result.stderr
    with hold_report_index(tmp_path, wait_s=0):
        pass


def test_reconcile_cannot_read_modify_write_outside_index_owner(tmp_path: Path) -> None:
    # A different context in the same process must also contend. The public
    # mutation borrows its exact synchronous owner only when explicitly nested.
    with hold_report_index(tmp_path), pytest.raises(JobAlreadyRunningError):
        Context().run(lambda: reconcile_legacy_workspace_reports(tmp_path))


def test_concurrent_ticker_writers_preserve_each_others_index_entries(tmp_path: Path) -> None:
    script = """
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from report.artifacts import (
    RenderedReportBody, ReportInteractionManifest, persist_report_artifact,
)
root, prefix = Path(sys.argv[1]), sys.argv[2]
for number in range(3):
    ticker = prefix + str(number)
    standalone = root / (ticker + '.html')
    standalone.write_text('<html>synthetic ' + ticker + '</html>')
    body = RenderedReportBody.from_html(
        ticker=ticker, report_date=date(2026, 10, 24),
        body_html='<main data-report-body="v1">synthetic ' + ticker + '</main>',
        sections=(), interaction_manifest=ReportInteractionManifest(),
    )
    persist_report_artifact(
        repo_root=root, body=body, standalone_path=standalone,
        generated_at=datetime(2026, 10, 24, tzinfo=UTC),
        coverage_role='unknown', title='Synthetic concurrency fixture',
    )
"""
    processes: list[subprocess.Popen[str]] = []
    try:
        for prefix in ("SYNTHA", "SYNTHB"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(tmp_path), prefix],
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                    },
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            _stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    assert {item.ticker for item in load_report_artifact_index(tmp_path).items} == {
        prefix + str(number) for prefix in ("SYNTHA", "SYNTHB") for number in range(3)
    }
