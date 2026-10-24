from __future__ import annotations

import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from execution import filing_xbrl_appcontainer_launcher as launcher

_invocation = cast(
    Callable[[Sequence[str]], tuple[Path, Path, tuple[str, ...], int, int, int, str, str]],
    getattr(launcher, "_invocation"),
)
_load_support = cast(Callable[[Path], ModuleType], getattr(launcher, "_load_support"))


def _arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "earnings_summary_xbrl_launcher.py"))
    return [
        "--contract",
        "earnings-xbrl-os-sandbox.v1",
        "--deny-network",
        "--read-tree",
        str(tmp_path),
        "--read-input-tree",
        str(tmp_path.parent / "input"),
        "--timeout-seconds",
        "600",
        "--cancel-handle",
        "100",
        "--cleanup-handle",
        "101",
        "--invocation-id",
        "a" * 32,
        "--installation-id",
        "b" * 64,
        "--",
        str(tmp_path / "python.exe"),
    ]


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "--other"),
        (1, "v2"),
        (2, "--allow-network"),
        (3, "--write-tree"),
        (5, "--input"),
        (7, "--extra"),
    ],
)
def test_closed_cli_rejects_changed_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, value: str
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    arguments[index] = value
    with pytest.raises(ValueError, match="contract"):
        _invocation(arguments)


def test_cli_rejects_external_launcher_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    arguments[4] = str(tmp_path.parent)
    with pytest.raises(ValueError, match="identity"):
        _invocation(arguments)


def test_oversized_payload_refuses_before_support_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_MAXIMUM_STDIN_BYTES", 8)
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"private-value")))

    def unexpected_support(_root: Path) -> object:
        pytest.fail("support loaded")

    monkeypatch.setattr(launcher, "_load_support", unexpected_support)
    assert launcher.main(arguments) == 125
    result = json.loads(capsys.readouterr().err)
    assert result == {
        "event": "filing_xbrl_sandbox_refused",
        "error_type": "SandboxRefused",
        "native_error_code": 0,
        "stage": "source_launcher",
    }


def test_support_loads_only_exact_adjacent_source(tmp_path: Path) -> None:
    (tmp_path / "earnings_summary_windows_appcontainer.py").write_text("identity = 42\n")
    assert _load_support(tmp_path).identity == 42


def test_source_launcher_delegates_bounded_payload_without_project_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"request")))
    observed: list[object] = []

    def run(command: object, **kwargs: object) -> int:
        observed.extend((command, kwargs))
        return 17

    def support(_root: Path) -> object:
        return SimpleNamespace(run_filing_appcontainer=run)

    monkeypatch.setattr(launcher, "_load_support", support)
    acknowledgements: list[tuple[object, ...]] = []

    def acknowledge(*values: object) -> None:
        acknowledgements.append(values)

    monkeypatch.setattr(launcher, "_acknowledge", acknowledge)
    assert launcher.main(arguments) == 17
    assert acknowledgements == [(101, "a" * 32, "b" * 64, True)]
    assert observed == [
        (str(tmp_path / "python.exe"),),
        {
            "runtime_root": tmp_path,
            "input_root": tmp_path.parent / "input",
            "payload": b"request",
            "timeout_seconds": 600,
            "cancel_handle": 100,
        },
    ]


def test_source_launcher_never_emits_private_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"request")))

    def fail(_root: Path) -> object:
        raise ValueError("private/credential/path")

    monkeypatch.setattr(launcher, "_load_support", fail)
    assert launcher.main(arguments) == 125
    output = capsys.readouterr().err
    assert "private" not in output
    assert json.loads(output)["stage"] == "source_launcher"


@pytest.mark.parametrize("proved", [True, False])
def test_control_ack_requires_trusted_cleanup_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    proved: bool,
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"request")))

    class FailureError(RuntimeError):
        cleanup_proved = proved

    def run(*_args: object, **_kwargs: object) -> int:
        raise FailureError("private runtime path")

    def support(_root: Path) -> object:
        return SimpleNamespace(run_filing_appcontainer=run)

    observed: list[tuple[object, ...]] = []

    def acknowledge(*values: object) -> None:
        observed.append(values)

    monkeypatch.setattr(launcher, "_load_support", support)
    monkeypatch.setattr(launcher, "_acknowledge", acknowledge)
    assert launcher.main(arguments) == 125
    assert len(observed) == int(proved)
    assert "private" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "index,value",
    [(8, "3601"), (8, "+600"), (10, "0"), (12, "100"), (14, "foreign"), (16, "b" * 63)],
)
def test_control_contract_rejects_ambiguous_or_foreign_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, value: str
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    arguments[index] = value
    with pytest.raises(ValueError):
        _invocation(arguments)


@pytest.mark.parametrize("timeout", [901, 3600])
def test_control_contract_accepts_manifest_timeout_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: int
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    arguments[8] = str(timeout)
    assert _invocation(arguments)[3] == timeout


@pytest.mark.parametrize(
    "native_stage,expected",
    [
        ("private_desktop_restore", "private_desktop_restore"),
        ("C:\\private\\credential", "source_launcher"),
        ("foreign\nvalue", "source_launcher"),
    ],
)
def test_native_refusal_exposes_only_bounded_stage_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    native_stage: str,
    expected: str,
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"request")))

    class NativeFailureError(RuntimeError):
        stage = native_stage
        winerror = 5
        cleanup_proved = False

    def run(*_args: object, **_kwargs: object) -> int:
        raise NativeFailureError("private full exception text")

    def support(_root: Path) -> object:
        return SimpleNamespace(run_filing_appcontainer=run)

    monkeypatch.setattr(launcher, "_load_support", support)
    assert launcher.main(arguments) == 125
    output = json.loads(capsys.readouterr().err)
    assert output["stage"] == expected and output["native_error_code"] == 5
