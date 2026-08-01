from __future__ import annotations

from pathlib import Path

import pytest

from execution.populate_latest_governed_state import build_parser, safe_receipt_path


def test_cli_requires_typed_operational_inputs() -> None:
    args = build_parser().parse_args(
        [
            "--database",
            "candidate.db",
            "--eligibility",
            "eligibility.json",
            "--scope-registry",
            "registry.json",
            "--expected-revision",
            "0267_source_definition_taxonomy_identity",
            "--operation-recorded-at",
            "2026-07-31T23:00:00Z",
            "--receipt",
            "receipt.json",
        ]
    )
    assert args.max_scopes == 1
    assert not args.apply


@pytest.mark.parametrize("suffix", ("", "-wal", "-shm", "-journal"))
def test_cli_receipt_cannot_alias_database_or_sidecars(tmp_path: Path, suffix: str) -> None:
    database = tmp_path / "candidate.db"
    database.write_bytes(b"db")
    with pytest.raises(ValueError, match="protected artifact"):
        safe_receipt_path(
            Path(f"{database}{suffix}"),
            database=database,
            inputs=(tmp_path / "eligibility.json", tmp_path / "registry.json"),
        )


def test_cli_receipt_cannot_alias_input_artifact(tmp_path: Path) -> None:
    database = tmp_path / "candidate.db"
    eligibility = tmp_path / "eligibility.json"
    database.write_bytes(b"db")
    eligibility.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="protected artifact"):
        safe_receipt_path(
            eligibility,
            database=database,
            inputs=(eligibility, tmp_path / "registry.json"),
        )
