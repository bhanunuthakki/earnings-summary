"""PDF worker isolation, timeout cleanup, and atomic output contracts."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from pipeline import pdf_render


@pytest.mark.parametrize("operation", ["count", "dimensions", "texts", "bbox", "render"])
def test_hung_pdf_worker_is_killed_reaped_and_leaves_source_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    source = tmp_path / "input.pdf"
    original = b"synthetic PDF bytes; worker must never modify input"
    source.write_bytes(original)
    worker = tmp_path / "hung_worker.py"
    worker.write_text(
        "import json, pathlib, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "if request['operation'] == 'render':\n"
        "    pathlib.Path(request['output_path']).write_bytes(b'partial PNG')\n"
        "while True: time.sleep(1)\n"
    )
    monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-child-must-not-inherit")
    children: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def observe_child(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        env: dict[str, str],
    ) -> subprocess.Popen[str]:
        assert text
        assert "SYNTHETIC_API_KEY" not in env
        child = real_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, text=True, env=env)
        children.append(child)
        return child

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(pdf_render, "_WORKER_SLOTS", slots)
    monkeypatch.setattr(pdf_render, "PDF_WORKER_PATH", worker)
    monkeypatch.setattr(pdf_render, "PDF_OPERATION_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(subprocess, "Popen", observe_child)
    if operation == "count":
        result = pdf_render.page_count(source)
    elif operation == "dimensions":
        result = pdf_render.page_dimensions(source, 1)
    elif operation == "texts":
        result = pdf_render.extract_page_texts(source)
    elif operation == "bbox":
        result = pdf_render.find_quote_bbox(source, 1, "quote")
    else:
        result = pdf_render.render_page_image(tmp_path, pdf_path=source, sha256="a" * 64, page=1)
    assert result is None
    assert len(children) == 1
    assert children[0].poll() is not None
    assert slots.acquire(blocking=False), "timed-out worker leaked its admission slot"
    slots.release()
    assert source.read_bytes() == original
    assert not list(tmp_path.rglob("*.png"))
    assert not list(tmp_path.rglob(".pdf-worker-*"))


@pytest.mark.parametrize("failure", ["crash", "invalid_json", "oversized_result"])
def test_failed_worker_never_publishes_partial_preview_or_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], failure: str
) -> None:
    source = tmp_path / "input.pdf"
    source.write_bytes(b"synthetic")
    worker = tmp_path / "failed_worker.py"
    actions = {
        "crash": "print('synthetic-secret-do-not-log', file=sys.stderr); sys.exit(1)",
        "invalid_json": "pathlib.Path(request['result_path']).write_text('invalid json')",
        "oversized_result": "pathlib.Path(request['result_path']).write_text(' ' * 5000)",
    }
    worker.write_text(
        "import json, pathlib, sys\n"
        "request=json.load(sys.stdin)\n"
        "pathlib.Path(request['output_path']).write_bytes(b'partial PNG')\n" + actions[failure]
    )
    monkeypatch.setattr(pdf_render, "PDF_WORKER_PATH", worker)
    monkeypatch.setattr(pdf_render, "MAX_PDF_RESULT_BYTES", 1000)
    assert pdf_render.render_page_image(tmp_path, pdf_path=source, sha256="a" * 64, page=1) is None
    assert not list(tmp_path.rglob("*.png"))
    captured = capfd.readouterr()
    assert "synthetic-secret-do-not-log" not in captured.out + captured.err


def test_busy_pdf_workers_degrade_without_launching_another_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "input.pdf"
    source.write_bytes(b"synthetic")
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(pdf_render, "_WORKER_SLOTS", slots)

    def unexpected_child(*args: object, **kwargs: object) -> None:
        pytest.fail("admission limit launched an extra PDF worker")

    monkeypatch.setattr(subprocess, "run", unexpected_child)
    assert pdf_render.page_count(source) is None
    cached = pdf_render.rendered_page_path(tmp_path, sha256="a" * 64, page=1)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached image")
    assert (
        pdf_render.render_page_image(tmp_path, pdf_path=source, sha256="a" * 64, page=1) == cached
    )
    slots.release()


@pytest.mark.parametrize("operation", ["count", "dimensions", "texts", "bbox"])
@pytest.mark.parametrize("failure_stage", ["setup", "cleanup"])
def test_temporary_directory_failure_degrades_without_exposing_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    failure_stage: str,
) -> None:
    source = tmp_path / "input.pdf"
    source.write_bytes(b"synthetic PDF")

    @contextmanager
    def failing_directory(*, prefix: str) -> Generator[str]:
        assert prefix == ".pdf-worker-"
        if failure_stage == "setup":
            raise OSError("synthetic-secret-do-not-log")
        yield str(tmp_path)
        raise OSError("synthetic-secret-do-not-log")

    def completed_worker(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        (tmp_path / "result.json").write_text("null", encoding="utf-8")
        return subprocess.CompletedProcess(["synthetic-worker"], 0)

    monkeypatch.setattr(pdf_render.tempfile, "TemporaryDirectory", failing_directory)
    monkeypatch.setattr(subprocess, "run", completed_worker)
    if operation == "count":
        result = pdf_render.page_count(source)
    elif operation == "dimensions":
        result = pdf_render.page_dimensions(source, 1)
    elif operation == "texts":
        result = pdf_render.extract_page_texts(source)
    else:
        result = pdf_render.find_quote_bbox(source, 1, "synthetic quote")
    assert result is None
    assert "pdf_operation_failed" in caplog.text
    assert "synthetic-secret-do-not-log" not in caplog.text
    assert source.read_bytes() == b"synthetic PDF"
