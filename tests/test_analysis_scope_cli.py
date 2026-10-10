"""Evidence selection is immutable and failed publication leaves no partial state."""

from __future__ import annotations

import json
import sqlite3
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import plan_analysis_evidence_scope as cli
from provenance.analysis_scope import AnalysisScopeRequest, build_analysis_scope
from provenance.immutable_artifact import ImmutableArtifactConflictError
from tests.test_analysis_scope import scope_db


def test_selection_cli_is_read_only_and_replays_exact_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    before = conn.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0]
    conn.commit()
    conn.close()
    request = tmp_path / "request.json"
    output = tmp_path / "scope.json"
    request.write_text(scope.request.model_dump_json() + "\n", encoding="utf-8")
    args = [
        "--db",
        str(tmp_path / "analysis-scope.db"),
        "--request",
        str(request),
        "--scope-receipt",
        str(output),
    ]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "selection_only_not_model_ready"
    assert result["scope_id"] == scope.scope_id
    assert result["outside_scope_count"] == 1
    original = output.read_bytes()
    assert cli.main(args) == 0
    assert output.read_bytes() == original
    with sqlite3.connect(tmp_path / "analysis-scope.db") as check:
        assert check.execute("SELECT COUNT(*) FROM expected_documents").fetchone()[0] == before
        assert check.execute("SELECT COUNT(*) FROM research_snapshot_headers").fetchone()[0] == 0


def test_selection_cli_refuses_changed_request_before_publishing(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    conn.commit()
    conn.close()
    request = tmp_path / "request.json"
    output = tmp_path / "scope.json"
    request.write_text(scope.request.model_dump_json() + "\n", encoding="utf-8")

    def changed_request(db: sqlite3.Connection, selected: AnalysisScopeRequest):
        built = build_analysis_scope(db, selected)
        request.write_text(selected.model_dump_json() + " \n", encoding="utf-8")
        return built

    monkeypatch.setattr(cli, "build_analysis_scope", changed_request)
    with pytest.raises(ImmutableArtifactConflictError, match="changed after admission"):
        cli.main(
            [
                "--db",
                str(tmp_path / "analysis-scope.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(output),
            ]
        )
    assert not output.exists()


def test_selection_cli_cannot_replace_request(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text("preserved", encoding="utf-8")
    with pytest.raises(ValueError, match="must not replace"):
        cli.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(request),
            ]
        )
    assert request.read_text(encoding="utf-8") == "preserved"


@pytest.mark.parametrize("malformed_json", [False, True])
def test_selection_cli_does_not_echo_invalid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed_json: bool
) -> None:
    sentinel = "synthetic-private-request-sentinel"
    request = tmp_path / "request.json"
    payload = (
        '{"purpose": "' + sentinel if malformed_json else json.dumps({"issuer_id": [sentinel]})
    )
    request.write_text(payload, encoding="utf-8")
    output = tmp_path / "scope.json"

    def unexpected_database(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid request opened a database")

    monkeypatch.setattr(cli, "connect_sqlite", unexpected_database)
    with pytest.raises(ValueError) as caught:
        cli.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(output),
            ]
        )
    assert sentinel not in "".join(traceback.format_exception(caught.value))
    assert not output.exists()
    assert not (tmp_path / "absent.db").exists()
    assert request.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("sidecar", ["", "-wal", "-shm", "-journal"])
def test_selection_cli_cannot_replace_database_or_sidecar(tmp_path: Path, sidecar: str) -> None:
    database = tmp_path / "preserved.db"
    output = Path(str(database) + sidecar)
    output.write_bytes(b"preserved database bytes")
    with pytest.raises(ValueError, match="must not replace"):
        cli.main(
            [
                "--db",
                str(database),
                "--request",
                str(tmp_path / "absent.json"),
                "--scope-receipt",
                str(output),
            ]
        )
    assert output.read_bytes() == b"preserved database bytes"


def test_selection_cli_preserves_conflicting_receipt(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    conn, scope = scope_db(tmp_path, migrated_db)
    conn.commit()
    conn.close()
    request = tmp_path / "request.json"
    request.write_text(scope.request.model_dump_json() + "\n", encoding="utf-8")
    output = tmp_path / "scope.json"
    output.write_bytes(b"existing conflicting receipt")
    with pytest.raises(ImmutableArtifactConflictError):
        cli.main(
            [
                "--db",
                str(tmp_path / "analysis-scope.db"),
                "--request",
                str(request),
                "--scope-receipt",
                str(output),
            ]
        )
    assert output.read_bytes() == b"existing conflicting receipt"
