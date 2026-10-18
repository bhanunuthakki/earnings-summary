from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution import run_weekly_cleanup as cleanup
from src.operations.temp_coverage import inventory_temp_coverage


def test_system_test_pools_are_visible_without_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cleanup.tempfile, "gettempdir", lambda: str(tmp_path))
    pool = tmp_path / "pytest-of-owner"
    pool.mkdir()
    payload = pool / "unique.bin"
    payload.write_bytes(b"unique")
    controller = tmp_path / "earnings-summary-pytest-controller-123"
    controller.mkdir()
    (controller / "fixture").write_bytes(b"fixture")
    unrelated = tmp_path / "other-app"
    unrelated.mkdir()
    roots = cleanup.system_temp_coverage_roots()
    assert roots == [(controller, "unclassified"), (pool, "unclassified")]
    result = inventory_temp_coverage(roots, now=datetime.now(UTC), decisions=[], run_statuses={})
    assert result.bytes == 13
    assert result.files == 2
    assert payload.read_bytes() == b"unique"
    assert unrelated.exists()


def test_linked_pool_is_visible_as_unsafe_without_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "unique").write_bytes(b"preserve")
    alias = base / "pytest-of-linked"
    alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(cleanup.tempfile, "gettempdir", lambda: str(base))
    roots = cleanup.system_temp_coverage_roots()
    assert roots == [(alias, "unclassified")]
    result = inventory_temp_coverage(roots, now=datetime.now(UTC), decisions=[], run_statuses={})
    assert result.unsafe_paths == 1
    assert result.bytes == 0
    assert (outside / "unique").read_bytes() == b"preserve"


def test_trusted_system_base_alias_normalizes_only_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "pytest-of-owner").mkdir()
    alias = tmp_path / "os-base"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(cleanup.tempfile, "gettempdir", lambda: str(alias))
    assert cleanup.system_temp_coverage_roots() == [(actual / "pytest-of-owner", "unclassified")]
