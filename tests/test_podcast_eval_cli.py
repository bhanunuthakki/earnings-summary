"""Provider-free reachability contract for the podcast takeaway golden eval."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PURPOSE = "podcast_takeaway_summary"


def _load_runner() -> ModuleType:
    source = PROJECT_ROOT / "execution" / "run_llm_evals.py"
    spec = importlib.util.spec_from_file_location("podcast_eval_cli_runner", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_routes_podcast_golden_without_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "portfolio.db").touch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_llm_evals.py",
            "--purpose",
            PURPOSE,
            "--repo-root",
            str(tmp_path),
            "--no-persist",
            "--limit",
            "0",
        ],
    )

    golden_purposes = cast(tuple[str, ...], getattr(runner, "GOLDEN_PURPOSES"))
    main = cast(Callable[[], int], getattr(runner, "main"))
    assert PURPOSE in golden_purposes
    assert main() == 0
