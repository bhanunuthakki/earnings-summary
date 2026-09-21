"""A valid labelled cache is decoded once and returned without invoking the picker."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from compute.valuation_basis import extract_for_ticker
from report.render_clock import fixed_render_clock
from tests.test_valuation_basis_provenance import seed_captured_inputs


def test_cache_hit_decodes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    def fail_picker(*args: object, **kwargs: object) -> str:
        raise AssertionError("picker must not be called for owner override or valid cache")

    monkeypatch.setattr("compute.valuation_basis.generate_valuation_basis", fail_picker)
    with (
        sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn,
        fixed_render_clock(date(2026, 9, 18)),
    ):
        seed_captured_inputs(conn, tmp_path)
        expected = extract_for_ticker("WIX", tmp_path, conn)
        cache_path = tmp_path / "data/valuation_basis/WIX.json"
        original_read = Path.read_text
        calls: list[Path] = []

        def counting(
            path: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if path == cache_path:
                calls.append(path)
            return original_read(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", counting)
        observed = extract_for_ticker("WIX", tmp_path, conn)
    assert calls == [cache_path]
    assert observed == expected
    assert observed.current_value == 20 and observed.history
    assert (
        observed.requested_multiple == "P/E (NTM)"
        and observed.multiple_name == "P/E (FY1 estimate)"
    )
