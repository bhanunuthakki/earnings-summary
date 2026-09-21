from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sources.canary_corpus import STATEMENT_TYPES, seal_statement_corpus


def seed_canary(conn: sqlite3.Connection, root: Path) -> tuple[int, ...]:
    identifiers: list[int] = []
    for ticker in ("WIX", "RBRK"):
        for kind in STATEMENT_TYPES:
            for cadence in ("FY", "Q1"):
                year = 2027 if ticker == "RBRK" and cadence == "Q1" else 2026
                period_end = (
                    "2026-04-30"
                    if ticker == "RBRK" and cadence == "Q1"
                    else "2026-03-31"
                    if cadence == "Q1"
                    else "2026-01-31"
                    if ticker == "RBRK"
                    else "2025-12-31"
                )
                payload = json.dumps(
                    [
                        {
                            "symbol": ticker,
                            "date": period_end,
                            "fiscalYear": str(year),
                            "period": cadence,
                            "reportedCurrency": "USD",
                            "syntheticValue": 100,
                            "statementType": kind,
                        }
                    ]
                ).encode()
                path = root / f"{ticker}-{kind}-{cadence}.json"
                path.write_bytes(payload)
                row = conn.execute(
                    "INSERT INTO documents (ticker,source_type,doc_type,file_path,sha256,fetched_at,fetch_status,raw_bytes_size) VALUES (?,'fmp',?,?,?,'2026-08-01T00:00:00Z','ok',?) RETURNING id",
                    (ticker, kind, str(path), hashlib.sha256(payload).hexdigest(), len(payload)),
                ).fetchone()
                assert row is not None
                identifiers.append(int(row[0]))
    conn.commit()
    return tuple(identifiers)


def test_seal_is_source_bound_fiscal_aware_and_read_only(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        ids = seed_canary(conn, tmp_path)
        before = conn.total_changes
        cutoff = datetime(2026, 9, 1, tzinfo=UTC)
        first = seal_statement_corpus(conn, document_ids=ids, repo_root=tmp_path, cutoff_at=cutoff)
        second = seal_statement_corpus(conn, document_ids=ids, repo_root=tmp_path, cutoff_at=cutoff)
        assert first == second
        assert conn.total_changes == before
        rbrk = next(
            item for item in first.files if item.ticker == "RBRK" and item.cadence == "quarterly"
        )
        assert rbrk.periods[0].period_end.isoformat() == "2026-04-30"
        assert rbrk.periods[0].vendor_fiscal_year == 2027
        assert first.current_entitlement == first.output_readiness == "unverified"
        assert not first.acquisition_provenance_complete
        assert all("syntheticValue" not in item.model_dump_json() for item in first.files)
        (tmp_path / "RBRK-fmp_income_statement-Q1.json").write_bytes(b"changed")
        with pytest.raises(ValueError, match="byte identity"):
            seal_statement_corpus(conn, document_ids=ids, repo_root=tmp_path, cutoff_at=cutoff)


def test_no_empty_or_partial_canary_seal(tmp_path: Path, migrated_db: Callable[..., Path]) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        ids = seed_canary(conn, tmp_path)
        with pytest.raises(ValueError, match="exactly12"):
            seal_statement_corpus(
                conn,
                document_ids=ids[:-1],
                repo_root=tmp_path,
                cutoff_at=datetime(2026, 9, 1, tzinfo=UTC),
            )


def test_cli_seals_selected_bytes_without_promoting_readiness(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution.attribute_source_cost import main

    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        ids = seed_canary(conn, tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    output = tmp_path / "seal-receipt.json"
    arguments = [
        "--db",
        str(database),
        "--repo-root",
        str(tmp_path),
        "--cutoff-at",
        "2026-09-01T00:00:00+00:00",
        "--json",
        "--output-receipt",
        str(output),
    ]
    for identifier in ids:
        arguments += ["--document-id", str(identifier)]
    assert main([*arguments, "--check-only"]) == 0
    assert not output.exists()
    assert main(arguments) == 1
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "PARTIAL"
    assert len(receipt["canary_seal"]["files"]) == 12
    assert receipt["measurements"]["measured_attempts"] == 0
    assert receipt["measurements"]["provider_cost_usd"] is None
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert "PARTIAL" in capsys.readouterr().out


def test_calendar_year_is_not_relabelled_fiscal_year(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        ids = seed_canary(conn, tmp_path)
        path = tmp_path / "RBRK-fmp_income_statement-Q1.json"
        records = json.loads(path.read_bytes())
        records[0].pop("fiscalYear")
        records[0]["calendarYear"] = "2026"
        payload = json.dumps(records).encode()
        path.write_bytes(payload)
        conn.execute(
            "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE file_path=?",
            (hashlib.sha256(payload).hexdigest(), len(payload), str(path)),
        )
        conn.commit()
        seal = seal_statement_corpus(
            conn, document_ids=ids, repo_root=tmp_path, cutoff_at=datetime(2026, 9, 1, tzinfo=UTC)
        )
        period = next(item.periods[0] for item in seal.files if item.filename == path.name)
        assert period.vendor_fiscal_year is None
        assert period.vendor_calendar_year == 2026


@pytest.mark.parametrize("changed", [{"symbol": "OTHER"}, {"period": "H1"}, {"date": "2027-12-31"}])
def test_seal_rejects_wrong_subject_unsupported_cadence_and_future_period(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    changed: dict[str, str],
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        ids = seed_canary(conn, tmp_path)
        path = tmp_path / "WIX-fmp_income_statement-Q1.json"
        records = json.loads(path.read_bytes())
        records[0].update(changed)
        payload = json.dumps(records).encode()
        path.write_bytes(payload)
        conn.execute(
            "UPDATE documents SET sha256=?,raw_bytes_size=? WHERE file_path=?",
            (hashlib.sha256(payload).hexdigest(), len(payload), str(path)),
        )
        conn.commit()
        with pytest.raises(ValueError):
            seal_statement_corpus(
                conn,
                document_ids=ids,
                repo_root=tmp_path,
                cutoff_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
