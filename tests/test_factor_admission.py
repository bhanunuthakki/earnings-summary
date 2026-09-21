"""Holdings-native factor freshness must reach real owner-facing consumers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from risk_factors import (
    TAXONOMY,
    FactorLoading,
    book_factor_vector,
    compute_input_sha,
    persist_exposures,
)


def _seed(root: Path, db: Path, *, stale_weights: bool = False) -> Path:
    holdings = root / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    path = holdings / "NU.json"
    path.write_text(
        json.dumps({"ticker": "NU", "thesis": "Consumer lending", "key_driver": "credit"})
    )
    inputs = compute_input_sha(
        "NU",
        geo_mix=None,
        product_mix=None,
        thesis_sha=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    persist_exposures(
        "NU",
        [FactorLoading(TAXONOMY[0], 0.0, "Measured zero")],
        provenance="thesis_derived",
        input_sha=inputs,
        db_path=db,
    )
    (root / "data").mkdir()
    (root / "data" / "portfolio_weights.json").write_text(
        json.dumps(
            {
                "weights": {"NU": 1.0},
                "computed_at": (
                    datetime.now(UTC) - timedelta(days=3 if stale_weights else 0)
                ).isoformat(),
            }
        )
    )
    return path


def test_changed_inputs_are_stale_not_neutral(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "factor.db")
    source = _seed(tmp_path, db)
    assert book_factor_vector(db, tmp_path).vector == {TAXONOMY[0]: 0.0}
    source.write_text(
        json.dumps({"ticker": "NU", "thesis": "Changed business", "key_driver": "credit"})
    )
    result = book_factor_vector(db, tmp_path)
    assert result.availability == "stale"
    assert result.vector == {}
    assert result.excluded_tickers == ("NU",)


def test_stale_materialized_holdings_degrade(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "factor.db")
    _seed(tmp_path, db, stale_weights=True)
    result = book_factor_vector(db, tmp_path)
    assert result.availability == "stale"
    assert result.vector == {}


def test_unheld_rows_do_not_change_lineage_and_position_review_uses_admission(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import sqlite3

    from advisor.position_review import build_risk_context, render_risk_lines
    from pipeline.portfolio_panel import business_factor_section

    db = migrated_db(tmp_path / "factor.db")
    path = _seed(tmp_path, db)
    before = book_factor_vector(db, tmp_path)
    persist_exposures(
        "OTHER",
        [FactorLoading(TAXONOMY[0], 1, "Unheld")],
        provenance="thesis_derived",
        input_sha="unheld",
        db_path=db,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE business_factor_exposures SET created_at='2099-01-01' WHERE ticker='OTHER'"
        )
    after = book_factor_vector(db, tmp_path)
    assert after.source_as_of == before.source_as_of
    assert after.input_sha == before.input_sha
    assert after.input_sha and after.input_sha in business_factor_section(after).replace(
        "<wbr>", ""
    )
    path.write_text(json.dumps({"thesis": "changed"}))
    context = build_risk_context("NU", db, tmp_path)
    assert context is not None and context.top_factors == ()
    assert context.factor_provenance and "stale" in context.factor_provenance
    assert "stale" in "\n".join(render_risk_lines(context))


def test_factor_cli_dry_run_uses_materialized_holdings_without_writes(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import subprocess
    import sys

    db = migrated_db(tmp_path / "factor.db")
    _seed(tmp_path, db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "execution" / "refresh_business_factors.py"),
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(db),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "candidate: NU" in result.stderr
    assert "0 LLM calls" in result.stderr
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not (tmp_path / "data" / "portfolio.db").exists()


def test_factor_population_entrypoint_holds_shared_database_lock(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:

    import risk_factors
    from execution import refresh_business_factors

    db = migrated_db(tmp_path / "factor.db")
    _seed(tmp_path, db)
    observed: list[bool] = []

    def refresh(db_path: Path, repo_root: Path) -> dict[str, int]:
        assert db_path == db and repo_root == tmp_path
        observed.append(Path(str(db) + ".write.lock").is_file())
        return {"tickers": 1, "cache_hit": 1}

    monkeypatch.setattr(risk_factors, "refresh_all", refresh)
    assert refresh_business_factors.main(["--repo-root", str(tmp_path), "--db-path", str(db)]) == 0
    assert observed == [True]
    assert not Path(str(db) + ".write.lock").exists()
