"""Hermetic smoke test for the named-flow benchmark harness.

Pins the plumbing, not performance: the harness must run every named flow
against a head-migrated database (including the subprocess-isolated cockpit
paint cycle) and emit a machine-readable artifact carrying medians,
dispersion, statement counts, and EXPLAIN QUERY PLAN output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / ".tmp" / "bench" / "smoke-bench-flows.json"

pytestmark = pytest.mark.slow

EXPECTED_FLOWS = (
    "kpi_roster_resolution",
    "cockpit_overview_build",
    "dashboard_rows_build",
    "report_financials_section",
    "cockpit_paint_cycle",
)


def test_harness_writes_complete_artifact(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    database = migrated_db(tmp_path / "bench-flows-smoke.db", target="head")
    completed = subprocess.run(
        [
            sys.executable,
            "execution/bench_flows.py",
            "--db",
            str(database),
            "--label",
            "smoke-bench-flows",
            "--runs",
            "1",
            "--warmup",
            "0",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["label"] == "smoke-bench-flows"
    assert artifact["database"]["schema_at_checkout_head"] is True
    snapshot_block = artifact["original_snapshot_integrity"]
    # When the mission's snapshot record exists, the snapshot must be unchanged.
    if snapshot_block["recorded_sha256"] is not None:
        assert snapshot_block["matches_record"] is True
    assert set(EXPECTED_FLOWS) <= set(artifact["flows"])
    for flow in EXPECTED_FLOWS:
        block = artifact["flows"][flow]
        assert len(block["samples_seconds"]) == 1
        assert block["median_seconds"] == pytest.approx(block["samples_seconds"][0])
        assert block["cov"] == 0.0
        statements = block["statements"]
        assert statements["total_statements"] >= 0
        assert isinstance(statements["top_shapes_by_time"], list)
        assert isinstance(block["eqp"], list)
    paint = artifact["flows"]["cockpit_paint_cycle"]
    assert paint["units"]["status_codes"] == {
        "overview": 200,
        "portfolio": 200,
        "evaluation": 200,
    }
    cache_states = paint["units"]["panel_cache_states"]
    assert set(cache_states) == {"overview", "portfolio", "evaluation"}
    assert all(isinstance(state, str) and state for state in cache_states.values())
