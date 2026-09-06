"""Pytest plugin emitting one raw performance-evidence fragment per worker."""

from __future__ import annotations

import json
import os
import platform
import resource
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest

from .test_ci_performance import (
    CacheState,
    PhaseTimings,
    TestCounts,
    WorkerEvidence,
    node_identity,
)

_START = time.perf_counter()
_COLLECTION_SECONDS = 0.0
_REPORTS: dict[str, dict[str, Any]] = {}


def pytest_collection_finish(session: Any) -> None:
    del session
    global _COLLECTION_SECONDS
    _COLLECTION_SECONDS = max(0.0, time.perf_counter() - _START)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Generator[None, Any, None]:
    outcome: Any = yield
    report: Any = outcome.get_result()
    _REPORTS.setdefault(item.nodeid, {})[call.when] = report


def _rss() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value * 1024 if platform.system() != "Darwin" else value


def _fixture_timings(directory: Path, worker: str) -> tuple[float, float, int, int]:
    path = directory / f"fixture-{worker}.jsonl"
    build_seconds = 0.0
    copy_seconds = 0.0
    builds = 0
    copies = 0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return build_seconds, copy_seconds, builds, copies
    for line in lines:
        try:
            event = json.loads(line)
            kind = event["kind"]
            seconds = max(0.0, float(event["seconds"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if kind == "migrated-db-template-build":
            build_seconds += seconds
            builds += 1
        elif kind == "migrated-db-template-copy":
            copy_seconds += seconds
            copies += 1
    return build_seconds, copy_seconds, builds, copies


def _counts() -> TestCounts:
    values = {name: 0 for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")}
    for reports in _REPORTS.values():
        setup = reports.get("setup")
        call = reports.get("call")
        teardown = reports.get("teardown")
        if setup is not None and setup.skipped:
            values["skipped"] += 1
        elif (
            (setup is not None and setup.failed)
            or (teardown is not None and teardown.failed)
            or call is None
        ):
            values["errors"] += 1
        elif getattr(call, "wasxfail", None) is not None:
            values["xfailed" if call.skipped else "xpassed"] += 1
        elif call.failed:
            values["failed"] += 1
        elif call.skipped:
            values["skipped"] += 1
        else:
            values["passed"] += 1
    return TestCounts(**values)


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    del session, exitstatus
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    destination_raw = os.environ.get("TEST_CI_PERFORMANCE_FRAGMENT_DIR")
    if not destination_raw or not _REPORTS:
        return
    destination = Path(destination_raw)
    cache_state_raw = os.environ.get("TEST_CI_PERFORMANCE_CACHE_STATE", "unknown")
    if cache_state_raw not in {"cold", "warm", "unknown"}:
        cache_state_raw = "unknown"
    cache_state = cast(CacheState, cache_state_raw)
    phase_seconds = {name: 0.0 for name in ("setup", "call", "teardown")}
    for reports in _REPORTS.values():
        for name in phase_seconds:
            report = reports.get(name)
            if report is not None:
                phase_seconds[name] += max(0.0, float(report.duration))
    fixture = _fixture_timings(destination, worker)
    node_ids = tuple(sorted(_REPORTS))
    payload = WorkerEvidence(
        worker_id=worker,
        node_ids=node_ids,
        node_id_sha256=node_identity(node_ids),
        counts=_counts(),
        timings=PhaseTimings(
            collection_seconds=_COLLECTION_SECONDS,
            setup_seconds=phase_seconds["setup"],
            call_seconds=phase_seconds["call"],
            teardown_seconds=phase_seconds["teardown"],
            migrated_db_template_build_seconds=fixture[0],
            migrated_db_template_copy_seconds=fixture[1],
            migrated_db_template_builds=fixture[2],
            migrated_db_template_copies=fixture[3],
        ),
        elapsed_seconds=max(0.0, time.perf_counter() - _START),
        peak_rss_bytes=_rss(),
        cache_state=cache_state,
    )
    path = destination / f"worker-{worker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload.model_dump(mode="json"), sort_keys=True) + "\n")
