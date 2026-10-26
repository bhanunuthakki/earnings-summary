"""Unit tests for require_product_db_path's product-vs-checkout selection.

The selection must never silently fall back to the checkout default, and it is
compared before resolution so a deployed checkout whose ``data/`` is a junction
or symlink to product state cannot disguise a checkout-default choice.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from db_paths import require_product_db_path


def _state_db(root: Path) -> Path:
    """A ``<root>/data/portfolio.db`` file, the product-state spelling."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / "portfolio.db"
    db_path.write_bytes(b"")
    return db_path


def test_missing_selection_is_rejected(tmp_path: Path) -> None:
    """No explicit path and no configured path is an error, never a fallback."""
    with pytest.raises(ValueError, match="EARNINGS_SUMMARY_DB_PATH is required"):
        require_product_db_path(tmp_path, environ={})


def test_configured_checkout_default_spelling_is_rejected(tmp_path: Path) -> None:
    """The configured environment path is refused when it names the checkout
    default, even though the file may resolve elsewhere."""
    checkout_default = tmp_path / "data" / "portfolio.db"

    with pytest.raises(ValueError, match="refusing the checkout-default database"):
        require_product_db_path(
            tmp_path, environ={"EARNINGS_SUMMARY_DB_PATH": str(checkout_default)}
        )


def test_explicit_checkout_default_is_rejected(tmp_path: Path) -> None:
    checkout_default = tmp_path / "data" / "portfolio.db"

    with pytest.raises(ValueError, match="refusing the checkout-default database"):
        require_product_db_path(tmp_path, explicit=checkout_default, environ={})


def test_redirected_checkout_data_still_rejects_the_default_spelling(tmp_path: Path) -> None:
    """When the checkout's ``data/`` is a symlink to state, the checkout-default
    spelling is still refused while the real target spelling is accepted."""
    state_data = tmp_path / "state" / "data"
    state_data.mkdir(parents=True)
    (state_data / "portfolio.db").write_bytes(b"")
    try:
        os.symlink(state_data, tmp_path / "data")
    except OSError as exc:
        pytest.skip(f"this platform cannot create symlinks: {exc}")

    with pytest.raises(ValueError, match="refusing the checkout-default database"):
        require_product_db_path(tmp_path, explicit=tmp_path / "data" / "portfolio.db", environ={})

    real = require_product_db_path(tmp_path, explicit=state_data / "portfolio.db", environ={})
    assert real == (state_data / "portfolio.db").resolve()


def test_explicit_path_is_returned_resolved(tmp_path: Path) -> None:
    """A normal explicit selection resolves through symlinked directories."""
    real_root = tmp_path / "real-root"
    _state_db(real_root)
    link = tmp_path / "linked-root"
    try:
        os.symlink(real_root, link)
    except OSError as exc:
        pytest.skip(f"this platform cannot create symlinks: {exc}")

    selected = require_product_db_path(
        tmp_path, explicit=link / "data" / "portfolio.db", environ={}
    )

    assert selected == real_root / "data" / "portfolio.db"
    assert selected.is_absolute()


def test_configured_environment_path_used_when_explicit_is_absent(tmp_path: Path) -> None:
    configured = _state_db(tmp_path / "managed")

    selected = require_product_db_path(
        tmp_path, environ={"EARNINGS_SUMMARY_DB_PATH": str(configured)}
    )

    assert selected == configured.resolve()
