from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from pipeline.work_os_shell import render_work_os_shell
from ui.cite_marks import CITE_MARKS_SNIPPET


def test_retired_owner_capacity_importer_is_absent() -> None:
    importer = Path(__file__).resolve().parents[1] / "execution" / "import_owner_capacity.py"
    assert not importer.exists()


def test_windows_sqlite_import_never_resolves_mac_default_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.resolve

    def checked_resolve(path: Path, strict: bool = False) -> Path:
        if path.name == "portfolio.db" and path.parent.name == "data":
            raise OSError("synthetic inaccessible Windows data junction")
        return original(path, strict=strict)

    source = Path(__file__).resolve().parents[1] / "src" / "sqlite_runtime.py"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "resolve", checked_resolve)
    namespace = runpy.run_path(str(source))
    assert namespace["_FORBIDDEN_MAC_CHECKOUT_DB"] is None


def test_current_shell_retains_shared_citation_runtime() -> None:
    html = render_work_os_shell()
    assert html.count(CITE_MARKS_SNIPPET) == 1
