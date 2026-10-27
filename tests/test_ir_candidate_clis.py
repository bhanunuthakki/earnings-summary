"""Thin CLI boundaries for candidate materialization and exact capture."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from execution import capture_approved_ir_document as capture_cli
from execution import collect_approved_ir_observations as collect_cli
from execution import import_visible_rubrik_observations as import_rubrik_cli
from execution import import_visible_wix_observations as import_wix_cli
from execution import materialize_ir_approval_candidates as materialize_cli
from ir_pipeline.approved_ir_observation_capture import (
    ReviewedIrPeriod,
    ReviewedIrPeriodSelection,
)
from pipeline.ir_approval_capture import ExactIrCaptureReceipt


@contextmanager
def _job_lock(*_args: object, **_kwargs: object) -> Generator[None]:
    yield


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class _Plan:
    candidate_count = 20

    def model_dump_json(self, **_kwargs: object) -> str:
        return json.dumps({"candidate_count": self.candidate_count})


def _load_bundle(_path: Path) -> bytes:
    return b"sealed"


def _plan_candidates(_bundle: bytes, _request: materialize_cli.IrCandidateCallerRequest) -> _Plan:
    return _Plan()


def _unexpected_apply(*_args: object) -> object:
    raise AssertionError("dry run wrote candidates")


def test_materialize_cli_is_dry_run_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes(b"sealed")
    monkeypatch.setattr(materialize_cli, "load_ir_observation_artifact", _load_bundle)
    monkeypatch.setattr(materialize_cli, "plan_ir_candidates", _plan_candidates)
    monkeypatch.setattr(
        materialize_cli,
        "apply_ir_candidate_plan",
        _unexpected_apply,
    )

    result = materialize_cli.main(
        [
            "--db",
            str(tmp_path / "missing.db"),
            "--bundle",
            str(bundle),
            "--issuer",
            "WIX",
            "--recorded-by",
            "test",
            "--reason",
            "approved scope",
            "--blob-root",
            str(tmp_path / "blobs"),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"candidate_count": 20}
    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "blobs").exists()


def test_collect_cli_refuses_wix_server_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _unexpected_collect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Wix server collector must not use browser session capabilities")

    monkeypatch.setattr(collect_cli, "collect_approved_ir_observations", _unexpected_collect)

    result = collect_cli.main(
        [
            "--issuer",
            "WIX",
            "--output",
            str(tmp_path / ".tmp" / "wix-bundle.json"),
            "--user-agent",
            "earnings-summary-test/1.0",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    event = json.loads(captured.err)
    assert event["event"] == "approved_ir_observation_capture_failed"
    assert event["error_type"] == "ValueError"
    assert "sealed visible-browser observation bundle" in event["error"]


def test_collect_cli_verifies_reviewed_periods_before_browser_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection_path = collect_cli.PROJECT_ROOT / ".tmp" / "test-rbrk-period-selection.json"
    output = collect_cli.PROJECT_ROOT / ".tmp" / "test-rbrk-period-bundle.json"
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_bytes(b"invalid selection")

    def _unexpected_browser() -> object:
        raise AssertionError("invalid selection started publisher browser")

    monkeypatch.setattr(collect_cli, "PlaywrightApprovedIrBrowser", _unexpected_browser)
    try:
        result = collect_cli.main(
            [
                "--issuer",
                "RBRK",
                "--output",
                str(output),
                "--user-agent",
                "earnings-summary-test/1.0",
                "--reviewed-period-selection",
                str(selection_path),
                "--db",
                str(collect_cli.PROJECT_ROOT / ".tmp" / "missing.db"),
            ]
        )
        assert result == 1
        assert not output.exists()
        assert "approved_ir_observation_capture_failed" in capsys.readouterr().err
    finally:
        selection_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def test_collect_cli_passes_verified_periods_to_existing_collector(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection_path = collect_cli.PROJECT_ROOT / ".tmp" / "test-rbrk-selected-periods.json"
    output = collect_cli.PROJECT_ROOT / ".tmp" / "test-rbrk-selected-bundle.json"
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    periods = (
        date(2026, 7, 31),
        date(2026, 4, 30),
        date(2026, 1, 31),
        date(2025, 10, 31),
        date(2025, 7, 31),
    )
    selection = ReviewedIrPeriodSelection(
        issuer_identifier="sec-cik-0001943896",
        cutoff_at=datetime(2026, 10, 24, tzinfo=UTC),
        periods=tuple(
            ReviewedIrPeriod(
                document_version_id=f"document-{index}",
                source_sha256=f"{index + 1:064x}",
                source_url=f"https://s203.q4cdn.com/667520861/files/{index}.pdf",
                period_end=period,
            )
            for index, period in enumerate(periods)
        ),
    )
    selection_path.write_text(selection.model_dump_json())
    seen: list[tuple[date, ...]] = []
    verified_capture: list[datetime] = []

    class _Bundle:
        bundle_sha256 = "a" * 64
        artifacts = (object(),)

        @staticmethod
        def to_bytes() -> bytes:
            return b"sealed bundle"

    def _connection(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    def _collect(
        request: collect_cli.ApprovedIrObservationCaptureRequest, **_kwargs: object
    ) -> _Bundle:
        seen.append(request.requested_quarter_ends)
        assert verified_capture == [request.captured_at]
        return _Bundle()

    def _verified(*_args: object, **kwargs: object) -> tuple[date, ...]:
        captured_at = kwargs["capture_at"]
        assert isinstance(captured_at, datetime)
        assert selection.cutoff_at <= captured_at.replace(tzinfo=UTC) <= datetime.now(UTC)
        verified_capture.append(captured_at)
        return periods

    monkeypatch.setattr(collect_cli, "connect_sqlite", _connection)
    monkeypatch.setattr(collect_cli, "verify_reviewed_ir_period_selection", _verified)
    monkeypatch.setattr(collect_cli, "PlaywrightApprovedIrBrowser", object)
    monkeypatch.setattr(collect_cli, "collect_approved_ir_observations", _collect)
    try:
        assert (
            collect_cli.main(
                [
                    "--issuer",
                    "RBRK",
                    "--output",
                    str(output),
                    "--user-agent",
                    "earnings-summary-test/1.0",
                    "--reviewed-period-selection",
                    str(selection_path),
                    "--db",
                    str(selection_path),
                ]
            )
            == 0
        )
        assert seen == [periods]
        assert output.read_bytes() == b"sealed bundle"
        assert json.loads(capsys.readouterr().out)["reviewed_period_selection_sha256"]
    finally:
        selection_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


@pytest.mark.parametrize("issuer", ["WIX", "RBRK"])
def test_visible_import_cli_passes_reviewed_selection_to_source_verifier(
    issuer: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = import_wix_cli if issuer == "WIX" else import_rubrik_cli
    source = cli.PROJECT_ROOT / ".tmp" / f"test-{issuer}-reviewed-export.json"
    selection_path = cli.PROJECT_ROOT / ".tmp" / f"test-{issuer}-reviewed-selection.json"
    output = cli.PROJECT_ROOT / ".tmp" / f"test-{issuer}-reviewed-bundle.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"visible export bytes")
    periods = (
        (
            date(2026, 6, 30),
            date(2026, 3, 31),
            date(2025, 12, 31),
            date(2025, 9, 30),
            date(2025, 6, 30),
        )
        if issuer == "WIX"
        else (
            date(2026, 7, 31),
            date(2026, 4, 30),
            date(2026, 1, 31),
            date(2025, 10, 31),
            date(2025, 7, 31),
        )
    )
    selection = ReviewedIrPeriodSelection(
        issuer_identifier="sec-cik-0001576789" if issuer == "WIX" else "sec-cik-0001943896",
        cutoff_at=datetime(2026, 10, 24, tzinfo=UTC),
        periods=tuple(
            ReviewedIrPeriod(
                document_version_id=f"document-{index}",
                source_sha256=f"{index + 1:064x}",
                source_url=(
                    "https://investors.wix.com/static-files/"
                    if issuer == "WIX"
                    else "https://s203.q4cdn.com/667520861/files/"
                )
                + f"{index}.pdf",
                period_end=periods[index],
            )
            for index in range(5)
        ),
    )
    selection_path.write_text(selection.model_dump_json())

    class _Bundle:
        bundle_sha256 = "a" * 64
        artifacts = (object(),)

        @staticmethod
        def to_bytes() -> bytes:
            return b"sealed bundle"

    seen: list[tuple[bytes, ReviewedIrPeriodSelection]] = []

    def _import(
        value: bytes,
        *,
        reviewed_selection: ReviewedIrPeriodSelection,
        source_connection: sqlite3.Connection,
    ) -> _Bundle:
        seen.append((value, reviewed_selection))
        assert isinstance(source_connection, sqlite3.Connection)
        return _Bundle()

    def _connection(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(cli, "connect_sqlite", _connection)
    monkeypatch.setattr(
        cli,
        "import_wix_visible_browser_export"
        if issuer == "WIX"
        else "import_rubrik_visible_browser_export",
        _import,
    )
    try:
        assert (
            cli.main(
                [
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--reviewed-period-selection",
                    str(selection_path),
                    "--db",
                    str(source),
                ]
            )
            == 0
        )
        assert seen == [(b"visible export bytes", selection)]
        assert output.read_bytes() == b"sealed bundle"
        assert json.loads(capsys.readouterr().out)["bundle_sha256"] == "a" * 64
    finally:
        source.unlink(missing_ok=True)
        selection_path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def test_visible_wix_import_cli_delegates_exact_bytes_and_writes_bundle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = import_wix_cli.PROJECT_ROOT / ".tmp" / "test-visible-export.json"
    output = import_wix_cli.PROJECT_ROOT / ".tmp" / "test-sealed-bundle.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"visible export bytes")

    class _Bundle:
        bundle_sha256 = "a" * 64
        artifacts = (object(),)

        @staticmethod
        def to_bytes() -> bytes:
            return b"sealed bundle"

    seen: list[bytes] = []

    def _import(value: bytes) -> _Bundle:
        seen.append(value)
        return _Bundle()

    monkeypatch.setattr(import_wix_cli, "import_wix_visible_browser_export", _import)
    try:
        result = import_wix_cli.main(["--input", str(source), "--output", str(output)])
        receipt = json.loads(capsys.readouterr().out)
        assert result == 0
        assert seen == [b"visible export bytes"]
        assert output.read_bytes() == b"sealed bundle"
        assert receipt["bundle_sha256"] == "a" * 64
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def test_visible_rubrik_import_cli_delegates_exact_bytes_and_writes_bundle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = import_rubrik_cli.PROJECT_ROOT / ".tmp" / "test-rubrik-export.json"
    output = import_rubrik_cli.PROJECT_ROOT / ".tmp" / "test-rubrik-bundle.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"visible Rubrik export bytes")

    class _Bundle:
        bundle_sha256 = "b" * 64
        artifacts = (object(),)

        @staticmethod
        def to_bytes() -> bytes:
            return b"sealed Rubrik bundle"

    seen: list[bytes] = []

    def _import(value: bytes) -> _Bundle:
        seen.append(value)
        return _Bundle()

    monkeypatch.setattr(import_rubrik_cli, "import_rubrik_visible_browser_export", _import)
    try:
        result = import_rubrik_cli.main(["--input", str(source), "--output", str(output)])
        receipt = json.loads(capsys.readouterr().out)
        assert result == 0
        assert seen == [b"visible Rubrik export bytes"]
        assert output.read_bytes() == b"sealed Rubrik bundle"
        assert receipt["bundle_sha256"] == "b" * 64
    finally:
        source.unlink(missing_ok=True)
        output.unlink(missing_ok=True)


def test_capture_cli_delegates_only_candidate_identity_to_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate_id = "a" * 64
    calls: list[tuple[Path, str, str]] = []

    def _capture(
        db_path: Path,
        action_input: object,
        **kwargs: object,
    ) -> ExactIrCaptureReceipt:
        calls.append(
            (db_path, str(getattr(action_input, "candidate_id")), str(kwargs["owner_actor"]))
        )
        return ExactIrCaptureReceipt(
            outcome="admitted",
            candidate_id=candidate_id,
            selection_decision_id="b" * 64,
            document_version_id="document-version",
            final_url="https://ir.rubrik.com/static-files/report.pdf",
            content_sha256="c" * 64,
            byte_size=10,
            media_type="application/pdf",
            network_fetched=True,
        )

    monkeypatch.setattr(capture_cli, "JobLock", _job_lock)
    monkeypatch.setattr(capture_cli.requests, "Session", _Session)
    monkeypatch.setattr(capture_cli, "capture_and_admit_exact_ir_document", _capture)
    db_path = tmp_path / "candidate.db"
    result = capture_cli.main(
        [
            "--db",
            str(db_path),
            "--candidate-id",
            candidate_id,
            "--owner-actor",
            "owner@example.test",
            "--reason",
            "capture approved exact bytes",
            "--checkpoint-root",
            str(tmp_path / "checkpoints"),
            "--blob-root",
            str(tmp_path / "blobs"),
            "--task-id",
            "approved-rbrk",
            "--user-agent",
            "earnings-summary-test/1.0",
        ]
    )

    assert result == 0
    assert calls == [(db_path, candidate_id, "owner@example.test")]
    assert json.loads(capsys.readouterr().out)["candidate_id"] == candidate_id
