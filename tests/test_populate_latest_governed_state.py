from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from execution.populate_latest_governed_state import (
    build_parser,
    population_database_lock_resources,
    safe_receipt_path,
    validate_existing_export_database_evidence,
    validate_population_resume_heads,
)
from provenance.immutable_artifact import ImmutableArtifactConflictError
from provenance.latest_governed_population import LatestGovernedPopulationReceipt


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
            "0269_latest_governed_population_receipt_v2",
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


def test_cli_database_lock_rejects_hardlink_alias_of_portfolio_database(
    tmp_path: Path,
) -> None:
    portfolio = tmp_path / "portfolio.db"
    alias = tmp_path / "candidate-alias.db"
    portfolio.write_bytes(b"sqlite")
    alias.hardlink_to(portfolio)

    with pytest.raises(ValueError, match="aliases the portfolio database"):
        population_database_lock_resources(alias, portfolio)

    assert population_database_lock_resources(portfolio, portfolio) == (
        f"sqlite:{portfolio.resolve()}",
        "portfolio-db",
    )


def test_cli_resume_recovers_committed_successor_before_missing_export() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE latest_governed_scope_heads ("
        "scope_key TEXT PRIMARY KEY,refresh_receipt_id TEXT,state_sha256 TEXT)"
    )
    conn.execute(
        "INSERT INTO latest_governed_scope_heads VALUES ('scope-1','receipt-b',?)",
        ("b" * 64,),
    )
    prior = {"scope-1": ("receipt-a", "a" * 64)}
    successor = {"scope-1": ("receipt-b", "b" * 64)}

    validate_population_resume_heads(
        conn,
        prior_heads=prior,
        stored_successor_heads=successor,
    )
    with pytest.raises(ValueError, match="prior checkpoint"):
        validate_population_resume_heads(
            conn,
            prior_heads=prior,
            stored_successor_heads=None,
        )


def test_cli_dry_run_defers_existing_export_check_until_exact_receipt_is_built() -> None:
    existing = LatestGovernedPopulationReceipt.model_construct()
    validate_existing_export_database_evidence(
        apply=False,
        existing=existing,
        stored=None,
    )
    with pytest.raises(ImmutableArtifactConflictError, match="database evidence"):
        validate_existing_export_database_evidence(
            apply=True,
            existing=existing,
            stored=None,
        )
