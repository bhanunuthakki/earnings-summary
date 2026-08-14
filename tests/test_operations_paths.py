from __future__ import annotations

from pathlib import Path

from operations.paths import scheduler_receipt_path, service_receipt_path


def test_runtime_receipt_paths_are_canonical_and_read_only(tmp_path: Path) -> None:
    assert scheduler_receipt_path(tmp_path) == (
        tmp_path / ".tmp" / "operations" / "runtime" / "scheduler.latest.json"
    )
    assert service_receipt_path(tmp_path) == (
        tmp_path / ".tmp" / "operations" / "runtime" / "services.latest.json"
    )
    assert not tuple(tmp_path.iterdir())
