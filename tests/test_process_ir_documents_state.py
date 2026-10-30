"""Registered transcript selection and explicit authority routing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

_CIK = "0001691493"
_NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _no_environment(_root: Path) -> None:
    return None
