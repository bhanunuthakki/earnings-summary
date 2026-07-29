# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import extract_competitive_mentions  # noqa: E402
import ingest_competitive_category_share  # noqa: E402
import refresh_ir_kpis  # noqa: E402

from models.runs import StageStatus  # noqa: E402
from pipeline.run_accounting import PipelineRunSuppressedError  # noqa: E402


class _Connection:
    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _suppressed() -> PipelineRunSuppressedError:
    return PipelineRunSuppressedError(
        pipeline_key="pipeline_same",
        attempt_id="attempt_live",
        status=StageStatus.IN_PROGRESS,
    )


@pytest.mark.parametrize(
    ("module", "argv", "call_name"),
    [
        (
            ingest_competitive_category_share,
            ["ingest_competitive_category_share.py", "--ticker", "RBRK"],
            "ingest_category_share",
        ),
        (
            extract_competitive_mentions,
            ["extract_competitive_mentions.py", "--ticker", "RBRK"],
            "extract_for_ticker",
        ),
    ],
)
def test_competitive_cli_boundaries_render_suppression_json(
    module: ModuleType,
    argv: list[str],
    call_name: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "open_db", lambda _: _Connection())

    def suppress(*_: object, **__: object) -> None:
        raise _suppressed()

    monkeypatch.setattr(module, call_name, suppress)
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "pipeline_same",
        "attempt_id": "attempt_live",
    }


def test_refresh_ir_kpis_boundary_renders_suppression_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(
        ticker="NU",
        quarters=8,
        file=tmp_path / "nu.xlsx",
        url=None,
        discover=False,
        platform=None,
        results_center_url=None,
        repo_root=tmp_path,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)
    monkeypatch.setattr(
        refresh_ir_kpis,
        "get_config",
        lambda *_: SimpleNamespace(spreadsheet_kpis=()),
    )
    monkeypatch.setattr(
        "ir_pipeline.config_builder.widen_config",
        lambda cfg, _path: cfg,
    )
    monkeypatch.setattr(refresh_ir_kpis, "save_config", lambda *_: None)
    monkeypatch.setattr(refresh_ir_kpis, "parse_spreadsheet", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(refresh_ir_kpis, "connect_sqlite", lambda *_args, **_kwargs: _Connection())

    def suppress(*_: object, **__: object) -> None:
        raise _suppressed()

    monkeypatch.setattr(refresh_ir_kpis, "ingest_spreadsheet_kpis", suppress)
    assert refresh_ir_kpis.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "pipeline_same",
        "attempt_id": "attempt_live",
    }
