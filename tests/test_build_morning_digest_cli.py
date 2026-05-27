"""Tests for execution/build_morning_digest.py — the digest-generation CLI."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "build_digest.db"
    cfg = _build_config(db)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


def _load_cli() -> Any:
    src = PROJECT_ROOT / "execution" / "build_morning_digest.py"
    spec = importlib.util.spec_from_file_location("build_morning_digest", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_morning_digest"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cli() -> Any:
    return _load_cli()


# ----------------------------------------------------------------------------
# --date + --out
# ----------------------------------------------------------------------------


def test_explicit_date_and_out_writes_to_path(
    cli: Any,
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "x.html"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_morning_digest",
            "--date",
            "2026-05-01",
            "--out",
            str(out),
            "--db-path",
            str(db_path),
            "--no-index",
        ],
    )
    rc = cli.main()
    assert rc == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "Morning digest · 2026-05-01" in html


# ----------------------------------------------------------------------------
# Default --date → today; default --out → data/dashboard/morning/<today>.html
# ----------------------------------------------------------------------------


def test_default_invocation_writes_to_canonical_path(
    cli: Any,
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --date, no --out → the CLI computes today's date and the
    canonical path under data/dashboard/morning/. The fake repo-root
    is a tmp_path so we don't pollute the real data/ tree."""
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    today = datetime.now(UTC).date()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_morning_digest",
            "--repo-root",
            str(fake_repo),
            "--db-path",
            str(db_path),
        ],
    )
    rc = cli.main()
    assert rc == 0

    expected = fake_repo / "data" / "dashboard" / "morning" / f"{today.isoformat()}.html"
    assert expected.exists()
    # The index.html copy is created by default
    index = fake_repo / "data" / "dashboard" / "morning" / "index.html"
    assert index.exists()
    # The two files should agree byte-for-byte (it's a copy)
    assert index.read_bytes() == expected.read_bytes()


def test_no_index_flag_skips_index_copy(
    cli: Any,
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "single.html"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_morning_digest",
            "--out",
            str(out),
            "--db-path",
            str(db_path),
            "--no-index",
        ],
    )
    rc = cli.main()
    assert rc == 0
    assert out.exists()
    # index.html should NOT exist in the same dir
    assert not (out.parent / "index.html").exists()


# ----------------------------------------------------------------------------
# Empty-state still writes a valid file
# ----------------------------------------------------------------------------


def test_empty_db_still_writes_valid_html_file(
    cli: Any,
    tmp_path: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "empty.html"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_morning_digest",
            "--out",
            str(out),
            "--db-path",
            str(db_path),
            "--no-index",
        ],
    )
    rc = cli.main()
    assert rc == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "Nothing fired" in html
