"""Production-derived SQLite Overview benchmark contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sqlite_overview_benchmark import benchmark_production_overview

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_production_overview_benchmark_is_source_read_only_and_counts_connections(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    source = migrated_db(tmp_path / "repo" / "data" / "portfolio.db")
    weights = source.parent / "portfolio_weights.json"
    weights.write_text('{"schema_version": 1, "weights": {}}', encoding="utf-8")
    before = _digest(source)

    payload = benchmark_production_overview(source, iterations=3)

    assert _digest(source) == before
    assert not Path(f"{source}-wal").exists()
    assert payload["mode"] == "production_overview_read"
    assert payload["wal_read"] == {
        "journal_mode": "wal",
        "committed_marker_visible": True,
        "wal_bytes": payload["wal_read"]["wal_bytes"],
    }
    assert payload["wal_read"]["wal_bytes"] > 0
    legacy = payload["legacy_component_owned"]
    scoped = payload["request_scoped_per_request"]
    assert legacy["connection_opens"] > scoped["connection_opens"]
    assert scoped["connection_opens"] == 3
    assert payload["connection_open_reduction"] == (
        legacy["connection_opens"] - scoped["connection_opens"]
    )
    assert len(legacy["request_ms"]) == len(scoped["request_ms"]) == 3
    assert legacy["warm_median_ms"] >= 0
    assert scoped["warm_median_ms"] >= 0
    assert payload["receipts_equal"] is True
    assert payload["workload_receipt"]["inbox_items"] >= 0
    assert payload["staged_cache_files"] == ["data/portfolio_weights.json"]


def test_cli_emits_structured_production_overview_json(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    source = migrated_db(tmp_path / "cli-derived.db")
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution" / "benchmark_sqlite_runtime.py"),
            "--production-overview-db",
            str(source),
            "--iterations",
            "2",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(completed.stdout)
    assert payload["mode"] == "production_overview_read"
    assert payload["request_scoped_per_request"]["connection_opens"] == 2
    assert payload["legacy_component_owned"]["connection_opens"] > 2
    assert payload["receipts_equal"] is True
    assert completed.stderr == ""
