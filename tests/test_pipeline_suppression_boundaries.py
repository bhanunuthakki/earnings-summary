from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from execution import (
    extract_competitive_mentions,
    ingest_competitive_category_share,
    refresh_ir_kpis,
)
from ir_pipeline.config import IrConfig
from ir_pipeline.spreadsheet import SpreadsheetParseResult
from models.runs import StageStatus
from pipeline.run_accounting import PipelineRunSuppressedError


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

    def open_db(_path: object) -> _Connection:
        return _Connection()

    monkeypatch.setattr(module, "open_db", open_db)

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
        quarters=5,
        file=tmp_path / "nu.xlsx",
        url=None,
        discover=False,
        platform=None,
        results_center_url=None,
        repo_root=tmp_path,
        db=tmp_path / "portfolio.db",
        owner_requested=True,
    )
    monkeypatch.setattr(refresh_ir_kpis, "_parse_args", lambda: args)
    config = IrConfig(ticker="NU", platform="mz", results_center_url="")

    def authorize(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True)

    def get_config(*_args: object) -> IrConfig:
        return config

    def widen_config(current: IrConfig, _path: Path) -> IrConfig:
        return current

    def save_config(*_args: object) -> None:
        pass

    def parse(*_args: object, **_kwargs: object) -> SpreadsheetParseResult:
        return SpreadsheetParseResult(
            workbook_sha256="0" * 64,
            workbook_bytes_size=0,
            config_sha256="0" * 64,
            max_quarters=5,
            cells=(),
            configured_name_order=(),
        )

    def connect(*_args: object, **_kwargs: object) -> _Connection:
        return _Connection()

    monkeypatch.setattr(refresh_ir_kpis, "authorize_stored_collection_target", authorize)
    monkeypatch.setattr(refresh_ir_kpis, "get_config", get_config)
    monkeypatch.setattr("ir_pipeline.config_builder.widen_config", widen_config)
    monkeypatch.setattr(refresh_ir_kpis, "save_config", save_config)
    monkeypatch.setattr(refresh_ir_kpis, "parse_spreadsheet_source", parse)
    monkeypatch.setattr(refresh_ir_kpis, "connect_sqlite", connect)

    def suppress(*_: object, **__: object) -> None:
        raise _suppressed()

    monkeypatch.setattr(refresh_ir_kpis, "ingest_spreadsheet_kpis", suppress)
    assert refresh_ir_kpis.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_running",
        "pipeline_key": "pipeline_same",
        "attempt_id": "attempt_live",
    }
