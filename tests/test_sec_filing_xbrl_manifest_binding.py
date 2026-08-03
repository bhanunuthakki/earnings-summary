from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import execution.ingest_sec_filing_xbrl as ingest_cli
from filings.inline_xbrl_processor import (
    ApprovedProcessorBundle,
    load_processor_bundle_manifest,
)
from provenance.sec_filing_xbrl_ingest import (
    FilingXbrlIngestRequest,
    ingest_sec_filing_xbrl,
)

ROOT = Path(__file__).resolve().parents[1]


def test_ingest_refuses_arbitrary_manifest_even_when_caller_claims_approval(
    tmp_path: Path,
) -> None:
    manifest = load_processor_bundle_manifest(ROOT / "config" / "filing_xbrl_processor_bundle.json")
    request = FilingXbrlIngestRequest(
        inventory_key="inventory-1",
        accession_number="0000000001-26-000001",
        expected_cik="0000000001",
        runtime_root=tmp_path / "runtime",
        bundle_python=tmp_path / "python.exe",
        sandbox_launcher=tmp_path / "launcher.exe",
        recorded_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    caller_claimed_bundle = cast(ApprovedProcessorBundle, manifest)
    with pytest.raises(ValueError, match="requires an approved processor bundle"):
        ingest_sec_filing_xbrl(
            sqlite3.connect(":memory:"),
            request,
            approved_bundle=caller_claimed_bundle,
        )


def test_apply_lock_refuses_hardlink_alias_to_portfolio_database(tmp_path: Path) -> None:
    portfolio = tmp_path / "portfolio.db"
    portfolio.write_bytes(b"sqlite")
    alias = tmp_path / "alias.db"
    try:
        os.link(portfolio, alias)
    except OSError:
        pytest.skip("hardlinks are unavailable")

    resources = ingest_cli.population_database_lock_resources(alias, portfolio)
    assert resources == (f"sqlite:{alias.resolve()}", "portfolio-db")
    with pytest.raises(ValueError, match="aliases the portfolio database"):
        ingest_cli.validate_population_database_target(alias, portfolio)
