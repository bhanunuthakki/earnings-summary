"""Thin CLI boundaries for candidate materialization and exact capture."""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

EXECUTION = Path(__file__).resolve().parents[1] / "execution"
sys.path.insert(0, str(EXECUTION))

import capture_approved_ir_document as capture_cli  # noqa: E402
import collect_approved_ir_observations as collect_cli  # noqa: E402
import import_visible_rubrik_observations as import_rubrik_cli  # noqa: E402
import import_visible_wix_observations as import_wix_cli  # noqa: E402
import materialize_ir_approval_candidates as materialize_cli  # noqa: E402
import pytest  # noqa: E402

from pipeline.ir_approval_capture import ExactIrCaptureReceipt  # noqa: E402


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
