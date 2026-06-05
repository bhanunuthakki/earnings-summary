"""Tests for src/identity.py — the single source of truth for the user id."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import identity  # noqa: E402


def test_default_user_id_falls_back_to_bhanu(monkeypatch):
    """With no env override, the default preserves the historical "bhanu" id so
    rows already keyed under it keep resolving."""
    monkeypatch.delenv("CIO_USER_ID", raising=False)
    importlib.reload(identity)
    assert identity.DEFAULT_USER_ID == "bhanu"


def test_default_user_id_honors_cio_user_id_env(monkeypatch):
    """CIO_USER_ID overrides the default without any schema change."""
    monkeypatch.setenv("CIO_USER_ID", "analyst-2")
    importlib.reload(identity)
    try:
        assert identity.DEFAULT_USER_ID == "analyst-2"
    finally:
        # Restore the module global so later tests see the default.
        monkeypatch.delenv("CIO_USER_ID", raising=False)
        importlib.reload(identity)
