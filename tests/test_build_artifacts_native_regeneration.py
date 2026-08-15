"""Purpose-targeted native regeneration must not build unrelated LLM sections."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from report.models import SectionStatus

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "build_artifacts.py"
    spec = importlib.util.spec_from_file_location("build_artifacts_native_test", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_artifacts_native_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def builder() -> Any:
    return _load_module()


@pytest.mark.parametrize("purpose", ["bear_case", "qa_topics", "valuation_basis"])
def test_targeted_regeneration_calls_only_the_requested_llm_section(
    builder: Any,
    purpose: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def _dependency(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(status=SectionStatus.OK)

    def _target(name: str):
        def _call(*args: object, **kwargs: object) -> object:
            del args
            calls.append((name, dict(kwargs)))
            return SimpleNamespace(status=SectionStatus.OK)

        return _call

    def _bomb(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("unrelated LLM/report path was invoked")

    def _no_bypass(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    monkeypatch.setattr(builder.ticker_settings, "get_bypass_budget", _no_bypass)
    monkeypatch.setattr(builder.thesis, "build", _dependency)
    monkeypatch.setattr(builder.financials_section_mod, "build", _dependency)
    monkeypatch.setattr(builder.segments, "build", _dependency)
    monkeypatch.setattr(builder.earnings, "build", _dependency)
    monkeypatch.setattr(builder.appendix, "build", _dependency)
    monkeypatch.setattr(builder, "build_report", _bomb)
    monkeypatch.setattr(builder, "_ensure_peer_selection", _bomb)
    monkeypatch.setattr(builder, "_ensure_key_metrics", _bomb)
    monkeypatch.setattr(
        builder.bear_case,
        "build",
        _target("bear_case") if purpose == "bear_case" else _bomb,
    )
    monkeypatch.setattr(
        builder.qa_roster,
        "build",
        _target("qa_topics") if purpose == "qa_topics" else _bomb,
    )
    monkeypatch.setattr(
        builder.valuation,
        "build",
        _target("valuation_basis") if purpose == "valuation_basis" else _bomb,
    )

    result = builder._regenerate_native_purpose(
        "NU",
        tmp_path,
        purpose=purpose,
        force_budget_bypass=False,
    )

    assert [name for name, _kwargs in calls] == [purpose]
    assert result == {"ticker": "NU", "purpose": purpose, "status": "ok"}
    kwargs = calls[0][1]
    assert kwargs["force_refresh"] is True


def test_saydo_filter_regeneration_forces_only_the_native_filter(
    builder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = SimpleNamespace(current_quarter="Q2", current_year=2026)
    row = SimpleNamespace(metric="Revenue", guide="$1", actual="$2", verdict="EXCEEDED")
    calls: list[dict[str, object]] = []

    def _no_bypass(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    def _saydo_section(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(status=SectionStatus.OK, cards=[card])

    def _filter(*args: object, **kwargs: object) -> list[object]:
        del args
        calls.append(dict(kwargs))
        return [row]

    def _parse(_card: object) -> list[object]:
        return [row]

    def _bomb(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("unrelated LLM/report path was invoked")

    monkeypatch.setattr(builder.ticker_settings, "get_bypass_budget", _no_bypass)
    monkeypatch.setattr(builder.saydo, "build", _saydo_section)
    monkeypatch.setattr(builder, "parse_print_vs_guide", _parse)
    monkeypatch.setattr(builder, "filter_important_print_vs_guide", _filter)
    monkeypatch.setattr(builder, "build_report", _bomb)

    result = builder._regenerate_native_purpose(
        "NU",
        tmp_path,
        purpose="saydo_filter",
        force_budget_bypass=False,
    )

    assert result == {"ticker": "NU", "purpose": "saydo_filter", "status": "ok"}
    assert calls == [
        {
            "ticker": "NU",
            "repo_root": tmp_path,
            "card": card,
            "rows": [row],
            "enable_llm": True,
            "force_refresh": True,
            "strict_refresh": True,
        }
    ]


def test_exec_comp_regeneration_calls_only_alignment_section(
    builder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _no_bypass(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return False

    def _target(*args: object, **kwargs: object) -> object:
        del args
        calls.append(dict(kwargs))
        return SimpleNamespace(status=SectionStatus.OK)

    def _bomb(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("unrelated LLM/report path was invoked")

    monkeypatch.setattr(builder.ticker_settings, "get_bypass_budget", _no_bypass)
    monkeypatch.setattr(builder.exec_compensation, "build", _target)
    monkeypatch.setattr(builder, "build_report", _bomb)

    result = builder._regenerate_native_purpose(
        "NU",
        tmp_path,
        purpose="exec_comp_alignment",
        force_budget_bypass=False,
    )

    assert result == {
        "ticker": "NU",
        "purpose": "exec_comp_alignment",
        "status": "ok",
    }
    assert calls == [{"enable_llm": True, "force_budget_bypass": False, "conn": None}]
