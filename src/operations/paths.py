"""Canonical read-only paths for externally produced Operations receipts."""

from __future__ import annotations

from pathlib import Path


def configured_product_state_root(code_root: Path) -> Path:
    """Resolve product state from its canonical DB declaration, never code fallback."""

    import os

    from db_paths import configured_db_path
    from runtime.secrets import load_project_env

    load_project_env(code_root)
    if not os.environ.get("EARNINGS_SUMMARY_DB_PATH", "").strip():
        raise ValueError("EARNINGS_SUMMARY_DB_PATH is required when --repo-root is not provided")
    database = configured_db_path(code_root)
    if database.name.casefold() != "portfolio.db" or database.parent.name.casefold() != "data":
        raise ValueError("configured database must be <product-state-root>/data/portfolio.db")
    return database.parent.parent.resolve()


def operations_runtime_directory(repo_root: Path) -> Path:
    return repo_root / ".tmp" / "operations" / "runtime"


def scheduler_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "scheduler.latest.json"


def service_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "services.latest.json"


def portfolio_tracker_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "portfolio-tracker.latest.json"


def portfolio_tracker_activation_receipt_path(repo_root: Path) -> Path:
    return operations_runtime_directory(repo_root) / "portfolio-tracker.activation.latest.json"


__all__ = [
    "configured_product_state_root",
    "operations_runtime_directory",
    "portfolio_tracker_activation_receipt_path",
    "portfolio_tracker_receipt_path",
    "scheduler_receipt_path",
    "service_receipt_path",
]
