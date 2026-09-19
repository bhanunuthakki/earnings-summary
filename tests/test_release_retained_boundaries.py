from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest
from import_owner_capacity import stage_wealthplan_facts

from pipeline.work_os_shell import render_work_os_shell
from ui.cite_marks import CITE_MARKS_SNIPPET


def test_requested_wealthplan_file_reaches_owner_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = tmp_path / "requested"
    plan = requested / "data" / "plan.local.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({"household": {"source": "requested"}, "baseline": {}}))
    received: list[object] = []

    class ValidationReachedError(Exception):
        pass

    class OwnerModel:
        @classmethod
        def model_validate(cls, value: object) -> object:
            received.append(value)
            raise ValidationReachedError

    models = ModuleType("wealthplan.models")
    for name in (
        "BabyEvent",
        "BuyHouseEvent",
        "ExitPayoutEvent",
        "Household",
        "MoveCityEvent",
        "ParentCareEvent",
        "Scenario",
        "StartupEvent",
        "WorkBreakEvent",
    ):
        setattr(models, name, OwnerModel)
    monkeypatch.setitem(sys.modules, "wealthplan.models", models)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    with pytest.raises(ValidationReachedError):
        stage_wealthplan_facts(requested)
    assert received == [{"source": "requested"}]


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
