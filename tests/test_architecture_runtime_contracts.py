from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from materialized_cache import (
    MATERIALIZED_CACHE_VERSION,
    cache_metadata,
    read_fresh_payload,
    write_payload_atomically,
)
from runtime.python_process import ensure_managed_python_argv, managed_python_argv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_managed_python_argv_routes_repository_target_through_bootstrap() -> None:
    target = PROJECT_ROOT / "execution" / "fetch_fmp_news.py"
    argv = managed_python_argv(PROJECT_ROOT, target, "--days", "2", unbuffered=True)
    assert argv == [
        sys.executable,
        "-u",
        os.fspath(PROJECT_ROOT / "execution" / "sqlite_bootstrap.py"),
        os.fspath(target),
        "--days",
        "2",
    ]
    assert ensure_managed_python_argv(PROJECT_ROOT, argv) == argv


def test_raw_repository_python_child_launches_are_prohibited() -> None:
    offenders: list[str] = []
    for base in (PROJECT_ROOT / "execution", PROJECT_ROOT / "src"):
        for path in base.rglob("*.py"):
            if path.name in {"benchmark_sqlite_runtime.py", "python_process.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"sys\.executable", source):
                nearby = source[match.start() : match.start() + 240]
                if "sqlite_bootstrap" in nearby:
                    continue
                if re.search(r"(?:execution[/\\]|PROJECT_ROOT|repo_root).*?\.py", nearby, re.S):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
                    break
    assert offenders == [], f"raw repository Python child launch bypasses: {offenders}"


def test_no_connection_downgrades_canonical_busy_timeout() -> None:
    offenders: list[tuple[str, int]] = []
    pattern = re.compile(r"PRAGMA\s+busy_timeout\s*=\s*(\d+)", re.I)
    for base in (PROJECT_ROOT / "execution", PROJECT_ROOT / "src"):
        for path in base.rglob("*.py"):
            for raw in pattern.findall(path.read_text(encoding="utf-8")):
                timeout_ms = int(raw)
                if timeout_ms < 30_000:
                    offenders.append((str(path.relative_to(PROJECT_ROOT)), timeout_ms))
    assert offenders == []


def test_materialized_cache_rejects_stale_and_incompatible_payloads(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    stale = datetime.now(UTC) - timedelta(hours=49)
    payload = {**cache_metadata("example", now=stale), "rows": [1]}
    write_payload_atomically(path, payload, prefix="example.")
    assert read_fresh_payload(path, schema="example") == {}

    payload = {
        **cache_metadata("example"),
        "cache_version": MATERIALIZED_CACHE_VERSION + 1,
        "rows": [1],
    }
    write_payload_atomically(path, payload, prefix="example.")
    assert read_fresh_payload(path, schema="example") == {}


def test_materialized_cache_publish_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    payload = {**cache_metadata("example"), "rows": [1]}
    write_payload_atomically(path, payload, prefix="example.")
    assert read_fresh_payload(path, schema="example")["rows"] == [1]
    assert list(tmp_path.glob("example.*.tmp")) == []
