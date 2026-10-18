"""CLI failures retain request identity without inventing input coverage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from dcf import input_evidence
from dcf.input_evidence import InputEvidenceError
from execution import preview_meli_inputs as cli
from provenance.fact_read_model import FactAdmissionError, ObservationUnavailableError
from provenance.immutable_artifact import ImmutableArtifactConflictError
from provenance.source_fact_publication import PublicationVerificationError
from sqlite_runtime import register_sqlite_integrity_functions
from tests import test_meli_input_evidence as model_fixtures
from tests.test_meli_input_evidence import END, NOW

coverage_fixture: Callable[[pytest.MonkeyPatch], sqlite3.Connection] = getattr(
    model_fixtures, "_coverage_fixture"
)


def _arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    return [
        "--db",
        str(tmp_path / "inputs.db"),
        "--snapshot-id",
        "snapshot",
        "--period-end",
        END.isoformat(),
        "--as-of",
        NOW.isoformat(),
        "--output",
        str(tmp_path / "preview.json"),
        "--state-root",
        str(tmp_path / "source-state"),
    ]


def _assert_unavailable(
    capsys: pytest.CaptureFixture[str], *, reason: str, inspection: str = "not_performed"
) -> None:
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == {
        "state": "unavailable",
        "reason_code": reason,
        "model_ready": False,
        "research_snapshot_id": "snapshot",
        "financial_period_end": END.isoformat(),
        "artifact_financial_as_of": NOW.isoformat(),
        "candidate_inspection": inspection,
    }
    assert captured.err == ""


@pytest.mark.parametrize(
    "change,reason",
    [
        ("stale", "source_inventory_stale_or_future"),
        ("new-period", "financial_anchor_not_latest_published_period"),
    ],
)
def test_real_coverage_failure_is_unavailable_without_candidate_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: str,
    reason: str,
) -> None:
    source = coverage_fixture(monkeypatch)
    try:
        if change == "stale":
            source.execute("UPDATE source_inventory_snapshots SET completed_at='2020-01-01'")
        else:
            source.execute("UPDATE expected_documents SET period_end='2026-09-30'")
        source.commit()
        with sqlite3.connect(tmp_path / "inputs.db") as destination:
            source.backup(destination)
    finally:
        source.close()
    assert cli.main(_arguments(tmp_path, monkeypatch)) == 3
    _assert_unavailable(capsys, reason=reason)
    assert not (tmp_path / "preview.json").exists()


@pytest.mark.parametrize("snapshot_state", ["missing", "unsealed"])
def test_missing_snapshot_has_a_typed_unavailable_result(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    snapshot_state: str,
) -> None:
    database = migrated_db(tmp_path / "inputs.db")
    with sqlite3.connect(database) as conn:
        register_sqlite_integrity_functions(conn)
        if snapshot_state != "missing":
            conn.execute(
                "INSERT INTO research_snapshot_headers VALUES (?,?,?,?,?,?)",
                (
                    "snapshot",
                    "snapshot",
                    "{}",
                    hashlib.sha256(b"{}").hexdigest(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
    capsys.readouterr()  # Exclude fixture migration diagnostics from CLI output.
    assert cli.main(_arguments(tmp_path, monkeypatch)) == 3
    _assert_unavailable(capsys, reason=f"research_snapshot_{snapshot_state}")
    assert not (tmp_path / "preview.json").exists()


def test_present_invalid_snapshot_keeps_its_original_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = coverage_fixture(monkeypatch)
    try:
        # Reuse the coverage-only fixture. This test owns absence classification,
        # not the snapshot owner's full corruption/integrity oracle.
        source.execute("CREATE TABLE research_snapshot_seals (research_snapshot_id TEXT)")
        source.execute("INSERT INTO research_snapshot_seals VALUES ('snapshot')")
        source.commit()
        with sqlite3.connect(tmp_path / "inputs.db") as destination:
            source.backup(destination)
    finally:
        source.close()

    def invalid(_conn: sqlite3.Connection, _snapshot_id: str) -> None:
        raise ValueError("invalid sealed snapshot")

    monkeypatch.setattr(input_evidence, "verify_research_snapshot", invalid)
    with pytest.raises(ValueError, match="invalid sealed snapshot") as failure:
        cli.main(_arguments(tmp_path, monkeypatch))
    assert not isinstance(failure.value, InputEvidenceError)
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "preview.json").exists()


@pytest.mark.parametrize(
    "failure,reason",
    [
        (
            InputEvidenceError("model_input_source_digest_or_size_mismatch:/private/document.pdf"),
            "model_input_source_digest_or_size_mismatch",
        ),
        (
            InputEvidenceError("unrecognized_private_/private/document.pdf"),
            "input_verification_failed",
        ),
        (ObservationUnavailableError("/private/observation"), "observation_unavailable_at_cutoff"),
        (
            FactAdmissionError(
                "private_reason",
                record_kind="fact_observation",
                record_id="/private/observation",
                disposition="quarantined",
            ),
            "fact_admission_failed",
        ),
        (
            PublicationVerificationError(
                "private_reason", publication_id="/private/publication", disposition="quarantined"
            ),
            "source_publication_unverified",
        ),
    ],
)
def test_typed_failure_is_safe_and_does_not_claim_inspection_never_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    reason: str,
) -> None:
    with sqlite3.connect(tmp_path / "inputs.db"):
        pass

    def refuse(conn: sqlite3.Connection, **_kwargs: object) -> None:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE prohibited_write (value TEXT)")
        raise failure

    monkeypatch.setattr(cli, "preview_meli_inputs", refuse)
    assert cli.main(_arguments(tmp_path, monkeypatch)) == 3
    _assert_unavailable(capsys, reason=reason, inspection="not_completed")
    assert not (tmp_path / "preview.json").exists()


@pytest.mark.parametrize(
    "failure", [ValueError("programming fault"), RuntimeError("runtime fault")]
)
def test_unknown_failures_remain_command_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    with sqlite3.connect(tmp_path / "inputs.db"):
        pass

    def refuse(_conn: sqlite3.Connection, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(cli, "preview_meli_inputs", refuse)
    with pytest.raises(type(failure), match=str(failure)):
        cli.main(_arguments(tmp_path, monkeypatch))
    assert capsys.readouterr().out == ""


def test_publication_conflict_remains_a_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dcf.meli_input_preview import MeliInputPreview

    with sqlite3.connect(tmp_path / "inputs.db"):
        pass
    preview = MeliInputPreview(
        research_snapshot_id="snapshot",
        snapshot_member_sha256="a" * 64,
        financial_period_end=END,
        as_of=NOW,
        state="incomplete",
        slots=(),
        facts={},
    )

    def prepared(_conn: sqlite3.Connection, **_kwargs: object) -> MeliInputPreview:
        return preview

    monkeypatch.setattr(cli, "preview_meli_inputs", prepared)
    output = tmp_path / "preview.json"
    output.write_text("previous artifact", encoding="utf-8")
    with pytest.raises(ImmutableArtifactConflictError):
        cli.main(_arguments(tmp_path, monkeypatch))
    assert output.read_text(encoding="utf-8") == "previous artifact"
    assert capsys.readouterr().out == ""


def test_invalid_source_root_keeps_request_context_without_database_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = _arguments(tmp_path, monkeypatch)
    arguments[-1] = "relative-source-root"
    assert cli.main(arguments) == 3
    _assert_unavailable(capsys, reason="source_authority_unavailable")
    assert not (tmp_path / "inputs.db").exists()
    assert not (tmp_path / "preview.json").exists()
