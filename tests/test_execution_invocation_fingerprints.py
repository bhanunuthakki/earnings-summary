# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
"""Focused coverage for execution-layer material fingerprints and suppression.

The CLI modules are loaded dynamically, so monkeypatch callbacks cannot inherit
call signatures even though their observable inputs and outputs are asserted.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

from models.runs import StageStatus
from pipeline.run_accounting import PipelineRunSuppressedError

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_execution_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"{name}_invocation_test",
        _REPO_ROOT / "execution" / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_facts_fingerprint_tracks_registered_source_bytes(tmp_path: Path) -> None:
    module = _load_execution_module("extract_facts")
    source = tmp_path / "data" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"value": 1}', encoding="utf-8")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, sha256 TEXT)"
    )
    conn.execute(
        "INSERT INTO documents (id, file_path, sha256) VALUES (1, ?, 'registered')",
        ("data/source.json",),
    )
    docs = [(1, "NU", "fmp_income_statement")]

    first = module._invocation_inputs(
        conn,
        docs,
        ["fmp_income_statement"],
        tmp_path,
    )
    source.write_text('{"value": 2}', encoding="utf-8")
    second = module._invocation_inputs(
        conn,
        docs,
        ["fmp_income_statement"],
        tmp_path,
    )

    first_file = first["documents"][0]["source"]
    second_file = second["documents"][0]["source"]
    assert first_file["sha256"] != second_file["sha256"]
    assert first_file["path"] == "data/source.json"


def test_crosstab_fingerprint_selects_requested_immutable_filing(tmp_path: Path) -> None:
    module = _load_execution_module("extract_segment_crosstabs")
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True)
    older = fmp_dir / "NU_form_10k_2024.json"
    newer = fmp_dir / "NU_form_10k_2025.json"
    older.write_text('{"year": 2024}', encoding="utf-8")
    newer.write_text('{"year": 2025}', encoding="utf-8")

    latest = module._invocation_inputs(tmp_path, ["NU"], None)
    requested = module._invocation_inputs(tmp_path, ["NU"], 2024)

    assert latest["sources"][0]["file"]["path"].endswith("NU_form_10k_2025.json")
    assert requested["sources"][0]["file"]["path"].endswith("NU_form_10k_2024.json")
    assert latest["sources"][0]["file"]["sha256"] != requested["sources"][0]["file"]["sha256"]


class _ClosableConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("run_status", "payload_status"),
    [
        (StageStatus.IN_PROGRESS, "already_running"),
        (StageStatus.OK, "already_done"),
    ],
)
def test_extract_facts_cli_emits_suppression_as_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_status: StageStatus,
    payload_status: str,
) -> None:
    module = _load_execution_module("extract_facts")
    conn = _ClosableConnection()
    monkeypatch.setattr(module, "open_db", lambda _path: conn)
    monkeypatch.setattr(module, "_resolve_tickers", lambda _conn, _args: ["NU"])
    monkeypatch.setattr(module, "_resolve_doc_types", lambda _args: ["fmp_income_statement"])
    monkeypatch.setattr(module, "_documents_for_extraction", lambda *_args: [])
    monkeypatch.setattr(module, "_invocation_inputs", lambda *_args: {})
    suppressed = PipelineRunSuppressedError(
        "pipeline_extract",
        "attempt_extract",
        run_status,
    )
    start_kwargs: dict[str, object] = {}

    def suppress_start(*_args: object, **kwargs: object) -> str:
        start_kwargs.update(kwargs)
        raise suppressed

    monkeypatch.setattr(module, "start_run", suppress_start)
    monkeypatch.setattr(sys, "argv", ["extract_facts.py", "--ticker", "NU"])

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": payload_status,
        "pipeline_key": "pipeline_extract",
        "attempt_id": "attempt_extract",
    }
    assert start_kwargs["deduplicate_completed"] is True
    assert conn.closed


def test_kpi_capture_cli_does_not_turn_suppression_into_capture_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_execution_module("extract_kpis_from_ir")
    conn = _ClosableConnection()
    monkeypatch.setattr(module, "open_db", lambda _path: conn)
    suppressed = PipelineRunSuppressedError(
        "pipeline_done",
        "attempt_done",
        StageStatus.OK,
    )
    monkeypatch.setattr(
        module,
        "_run_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(suppressed),
    )
    monkeypatch.setattr(sys, "argv", ["extract_kpis_from_ir.py", "--capture"])

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "already_done",
        "pipeline_key": "pipeline_done",
        "attempt_id": "attempt_done",
    }
    assert conn.closed


def test_comparable_set_completed_deduplication_is_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_execution_module("build_comparable_sets")
    conn = _ClosableConnection()
    monkeypatch.setattr(module, "open_db", lambda _path: conn)
    monkeypatch.setattr(module, "_resolve_tickers", lambda _conn, _args: ["NU"])
    monkeypatch.setattr(module, "_invocation_inputs", lambda *_args: {"complete": True})
    suppressed = PipelineRunSuppressedError(
        "pipeline_comps",
        "attempt_comps",
        StageStatus.OK,
    )
    start_kwargs: dict[str, object] = {}

    def suppress_start(*_args: object, **kwargs: object) -> str:
        start_kwargs.update(kwargs)
        raise suppressed

    monkeypatch.setattr(module, "start_run", suppress_start)
    monkeypatch.setattr(sys, "argv", ["build_comparable_sets.py", "--ticker", "NU"])

    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_done"
    assert start_kwargs["deduplicate_completed"] is True
    assert start_kwargs["force"] is False
    assert conn.closed
