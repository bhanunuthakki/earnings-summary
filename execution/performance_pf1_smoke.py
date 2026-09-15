"""Fixed PF1 runner-fidelity smoke adapter; never performance admission evidence."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import TextIOBase
from pathlib import Path

import pytest
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from quality.performance_experiment import MAX_COMPANION_OUTPUT_BYTES  # noqa: E402

WORKLOAD_ID = "capture-poller-runner-fidelity-smoke/v1"
FIXTURE_PATH = Path("tests/fixtures/performance/pf1-smoke.txt")
SELECTED_NODES = (
    "tests/test_capture_poller.py::test_runtime_configuration_binds_implicit_consumers_to_canonical_db",
    "tests/test_capture_poller.py::test_load_save_offset_roundtrip",
)


class _OutputLimitError(Exception):
    pass


class _BoundedTextFile(TextIOBase):
    def __init__(self, path: Path, budget: list[int]) -> None:
        self._handle = path.open("w", encoding="utf-8", newline="\n")
        self._budget = budget

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        encoded_size = len(value.encode("utf-8"))
        if self._budget[0] + encoded_size > MAX_COMPANION_OUTPUT_BYTES:
            raise _OutputLimitError
        self._budget[0] += encoded_size
        return self._handle.write(value)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        if self.closed:
            return
        try:
            super().close()
        finally:
            self._handle.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _executed_nodes(junit_path: Path) -> tuple[str, ...]:
    root = ElementTree.fromstring(junit_path.read_bytes())
    nodes: list[str] = []
    for case in root.iter("testcase"):
        if any(case.find(state) is not None for state in ("failure", "error", "skipped")):
            raise ValueError("smoke test did not pass")
        class_name = case.attrib.get("classname")
        test_name = case.attrib.get("name")
        if not class_name or not test_name:
            raise ValueError("pytest outcome identity is incomplete")
        nodes.append(f"{class_name.replace('.', '/')}.py::{test_name}")
    return tuple(nodes)


def main() -> int:
    try:
        output_dir = Path(os.environ["PERFORMANCE_EXPERIMENT_OUTPUT_DIR"]).resolve(strict=True)
        revision = os.environ["PERFORMANCE_EXPERIMENT_REVISION"]
        fixture_sha256 = os.environ["PERFORMANCE_EXPERIMENT_FIXTURE_SHA256"]
        if os.environ["PERFORMANCE_EXPERIMENT_WORKLOAD_ID"] != WORKLOAD_ID:
            return 1
        fixture = FIXTURE_PATH.resolve(strict=True)
        if _sha256(fixture.read_bytes()) != fixture_sha256:
            return 1
        junit_path = output_dir / "pytest.xml"
        pytest_args = [
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit_path}",
            *SELECTED_NODES,
        ]
        budget = [0]
        stdout = _BoundedTextFile(output_dir / "pytest.stdout", budget)
        stderr = _BoundedTextFile(output_dir / "pytest.stderr", budget)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                returncode = pytest.main(pytest_args)
        finally:
            stdout.close()
            stderr.close()
        if returncode != pytest.ExitCode.OK:
            return 1
        executed = _executed_nodes(junit_path)
        if executed != SELECTED_NODES:
            return 1
        coverage_sha256 = _sha256("\n".join(SELECTED_NODES).encode())
        result_sha256 = _sha256("\n".join(f"{node}\tpassed" for node in executed).encode())
        peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform != "darwin":
            peak_rss *= 1024
        companion = {
            "schema_version": "performance-experiment-companion/v1",
            "workload_id": WORKLOAD_ID,
            "revision": revision,
            "fixture_sha256": fixture_sha256,
            "coverage_sha256": coverage_sha256,
            "result_sha256": result_sha256,
            "sql_statements": 0,
            "rows": len(executed),
            "peak_rss_bytes": max(0, peak_rss),
        }
        print(json.dumps(companion, sort_keys=True, separators=(",", ":")))
    except (
        KeyError,
        OSError,
        ElementTree.ParseError,
        DefusedXmlException,
        ValueError,
        _OutputLimitError,
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
