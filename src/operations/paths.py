"""Canonical read-only paths for externally produced Operations receipts."""

from __future__ import annotations

from pathlib import Path


def operations_runtime_directory(repo_root: Path) -> Path:
    return repo_root / ".tmp" / "operations" / "runtime"


def scheduler_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "scheduler.latest.json"


def service_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "services.latest.json"


__all__ = ["operations_runtime_directory", "scheduler_receipt_path", "service_receipt_path"]
