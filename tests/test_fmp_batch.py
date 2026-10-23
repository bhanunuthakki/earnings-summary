"""The CLI and quarterly refresh share ordered FMP extraction and recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from execution import extract_facts as cli
from pipeline import fmp_batch, quarterly_refresh
from pipeline.quarterly_refresh import StageResult, StageStatus
from provenance.evidence_backfill import ensure_legacy_document_evidence

_stage_extract_fmp_facts = cast(
    Callable[..., StageResult], getattr(quarterly_refresh, "_stage_extract_fmp_facts")
)


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    database = migrated_db(tmp_path / "fmp.sqlite")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _document(
    conn: sqlite3.Connection, root: Path, doc_type: str, payload: object, *, ticker: str = "TEST"
) -> int:
    ordinal = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    source = root / f"{ticker}-{doc_type}-{ordinal}.json"
    source.write_text(json.dumps(payload))
    body = source.read_bytes()
    cursor = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES (?, 'fmp', ?, ?, ?, ?, 'ok', ?, '2024-12-31')",
        (
            ticker,
            doc_type,
            str(source),
            hashlib.sha256(body).hexdigest(),
            datetime.now(),
            len(body),
        ),
    )
    assert cursor.lastrowid is not None
    ensure_legacy_document_evidence(conn, repo_root=root, document_id=cursor.lastrowid)
    conn.commit()
    return cursor.lastrowid


def _record(**fields: object) -> dict[str, object]:
    return {
        "symbol": "TEST",
        "date": "2024-12-31",
        "period": "Q4",
        "reportedCurrency": "USD",
        **fields,
    }


@pytest.mark.parametrize("route", ["cli", "quarterly"])
def test_routes_preserve_order_lineage_restatement_and_replay(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
) -> None:
    # Register segments first. Selection must still run income first.
    segment = _document(conn, tmp_path, "fmp_segment_product", [_record(data={"Cloud": 600})])
    over_cap = _document(conn, tmp_path, "fmp_segment_geographic", [_record(data={"US": 1500})])
    income = _document(conn, tmp_path, "fmp_income_statement", [_record(revenue=1000)])
    correction = _document(conn, tmp_path, "fmp_income_statement", [_record(revenue=1100)])
    malformed = _document(
        conn,
        tmp_path,
        "fmp_income_statement",
        [
            _record(date="2024-09-30", period="Q3", revenue=900),
            _record(date="invalid", revenue=1),
        ],
    )
    empty = _document(conn, tmp_path, "fmp_cashflow", [])
    assert [doc[0] for doc in fmp_batch.select_documents(conn, ["test"])] == [
        income,
        correction,
        malformed,
        segment,
        over_cap,
        empty,
    ]

    def run() -> tuple[int, int]:
        if route == "quarterly":
            result = _stage_extract_fmp_facts(conn, ticker="TEST", project_root=tmp_path)
            assert result.status == StageStatus.FAILED
            assert "1 failures" in result.notes
            return result.rows_processed, 1
        monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
        database = str(conn.execute("PRAGMA database_list").fetchone()[2])
        monkeypatch.setattr(
            sys, "argv", ["extract-facts", "--ticker", "TEST", "--db", database, "--force"]
        )
        assert cli.main() == 1
        result = json.loads(capsys.readouterr().out)
        assert result["documents"] == 6
        assert result["documents_ok"] == 5
        return result["facts_inserted"], result["documents_failed"]

    assert run() == (3, 1)
    facts = [
        tuple(row)
        for row in conn.execute(
            "SELECT id,ticker,period_end,fiscal_period_type,line_item,value,currency,unit,"
            "source_doc_id,supersedes_id,locator FROM financial_facts ORDER BY id"
        )
    ]
    assert len(facts) == 2
    assert [(row[1], row[3:9]) for row in facts] == [
        ("TEST", ("Q4", "revenue", 1000, "USD", "actual", income)),
        ("TEST", ("Q4", "revenue", 1100, "USD", "actual", correction)),
    ]
    assert facts[1][9] == facts[0][0]
    assert all(json.loads(row[10])["json_path"] == "[0].revenue" for row in facts)
    dimensions = conn.execute(
        "SELECT p.source_doc_id,d.dim_name,d.value,d.locator "
        "FROM segment_dimensions d JOIN segment_periods p ON p.id=d.period_id"
    ).fetchall()
    assert [(row[0], row[1], row[2]) for row in dimensions] == [(segment, "Cloud", 600)]
    assert json.loads(dimensions[0][3])["json_path"] == "[0].data.Cloud"
    assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM confidence_observations").fetchone()[0] == 1
    assert run() == (0, 1)
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT id,ticker,period_end,fiscal_period_type,line_item,value,currency,unit,"
            "source_doc_id,supersedes_id,locator FROM financial_facts ORDER BY id"
        )
    ] == facts
    assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 2
    assert not conn.in_transaction


@pytest.mark.parametrize("doc_type", fmp_batch.SUPPORTED_DOC_TYPES)
def test_every_dispatch_accepts_valid_zero_documents(
    conn: sqlite3.Connection,
    tmp_path: Path,
    doc_type: str,
) -> None:
    payload: object = {} if doc_type in {"fmp_10k_json", "fmp_10q_json"} else []
    _document(conn, tmp_path, doc_type, payload)
    result = fmp_batch.extract_documents(conn, fmp_batch.select_documents(conn, ["TEST"]), tmp_path)
    assert (result.documents, result.succeeded, result.rows_inserted, result.failed) == (1, 1, 0, 0)
    assert not conn.in_transaction


@pytest.mark.parametrize("doc_type", ["fmp_10k_json", "fmp_10q_json"])
def test_nu_ifrs_override_retains_geography_output(
    conn: sqlite3.Connection,
    tmp_path: Path,
    doc_type: str,
) -> None:
    title = "Segment information (Details) - $ in Millions"
    document = _document(
        conn,
        tmp_path,
        doc_type,
        {
            title: [
                {title: ["12 Months Ended"]},
                {"items": ["Dec. 31, 2024"]},
                {"Brazil [Member] | Reportable segments [member]": ["\u00a0"]},
                {"Revenue": [25]},
            ]
        },
        ticker="NU",
    )
    result = fmp_batch.extract_documents(conn, fmp_batch.select_documents(conn, ["NU"]), tmp_path)
    assert result.rows_inserted == 1
    assert tuple(
        conn.execute(
            "SELECT p.ticker,p.source_doc_id,d.dim_type,d.dim_name,d.value "
            "FROM segment_dimensions d JOIN segment_periods p ON p.id=d.period_id"
        ).fetchone()
    ) == ("NU", document, "geography", "Brazil", 25_000_000)


@pytest.mark.parametrize(
    "doc_type",
    [
        doc_type
        for doc_type in fmp_batch.SUPPORTED_DOC_TYPES
        if doc_type not in {"fmp_10k_json", "fmp_10q_json"}
    ],
)
def test_each_record_extractor_rolls_back_before_failure_accounting(
    conn: sqlite3.Connection,
    tmp_path: Path,
    doc_type: str,
) -> None:
    if doc_type == "fmp_income_statement":
        record = _record(revenue=100)
    elif doc_type == "fmp_balance_sheet":
        record = _record(totalAssets=100)
    elif doc_type == "fmp_cashflow":
        record = _record(netCashProvidedByOperatingActivities=100)
    else:
        record = _record(data={"revenueremainingperformanceobligation": 100})
    _document(conn, tmp_path, doc_type, [record, {**record, "date": "invalid"}])
    outcomes: list[fmp_batch.DocumentOutcome] = []
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    def report(outcome: fmp_batch.DocumentOutcome) -> None:
        target = "segment_dimensions" if "segment" in doc_type else "financial_facts"
        assert any(f"INTO {target} " in statement for statement in statements)
        assert "COMMIT" not in statements
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 0
        # A receipt writer can now commit without retaining the first record.
        conn.commit()
        outcomes.append(outcome)

    result = fmp_batch.extract_documents(
        conn,
        fmp_batch.select_documents(conn, ["TEST"]),
        tmp_path,
        report=report,
    )
    conn.set_trace_callback(None)
    assert (result.rows_inserted, result.failed) == (0, 1)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0].error, ValueError)


@pytest.mark.parametrize("ticker", ["TEST", "NU"])
@pytest.mark.parametrize("doc_type", ["fmp_10k_json", "fmp_10q_json"])
def test_each_filing_extractor_halts_and_rolls_back_on_database_failure(
    conn: sqlite3.Connection,
    tmp_path: Path,
    ticker: str,
    doc_type: str,
) -> None:
    title = "Segment information (Details) - $ in Millions"
    if ticker == "NU":
        rows: list[dict[str, list[object]]] = [
            {"Brazil [Member] | Reportable segments [member]": ["\u00a0"]},
            {"Revenue": [25]},
            {"Mexico [Member] | Reportable segments [member]": ["\u00a0"]},
            {"Revenue": [5]},
        ]
        rejected = "Mexico"
    else:
        rows = [
            {"Google Services": ["\u00a0"]},
            {"Total income from operations": [25]},
            {"Other Bets": ["\u00a0"]},
            {"Total income from operations": [5]},
        ]
        rejected = "Other Bets"
    _document(
        conn,
        tmp_path,
        doc_type,
        {
            title: [
                {title: ["12 Months Ended"]},
                {"items": ["Dec. 31, 2024"]},
                *rows,
            ]
        },
        ticker=ticker,
    )
    _document(conn, tmp_path, doc_type, {"empty": ticker}, ticker=ticker)
    conn.execute(
        "CREATE TRIGGER reject_dimension BEFORE INSERT ON segment_dimensions "
        f"WHEN NEW.dim_name = '{rejected}' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic dimension failure'); END"
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    outcomes: list[fmp_batch.DocumentOutcome] = []
    with pytest.raises(sqlite3.IntegrityError, match="synthetic dimension failure"):
        fmp_batch.extract_documents(
            conn,
            fmp_batch.select_documents(conn, [ticker]),
            tmp_path,
            report=outcomes.append,
        )
    conn.set_trace_callback(None)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0].error, sqlite3.IntegrityError)
    assert conn.execute("SELECT COUNT(*) FROM segment_periods").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM segment_dimensions").fetchone()[0] == 0
    assert not conn.in_transaction
    assert "COMMIT" not in statements


def test_fatal_database_error_stops_dependent_quarterly_stages(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import quarterly_refresh

    _document(conn, tmp_path, "fmp_income_statement", [_record(revenue=100)])
    conn.execute(
        "CREATE TRIGGER reject_fact BEFORE INSERT ON financial_facts "
        "BEGIN SELECT RAISE(ABORT, 'synthetic fact failure'); END"
    )

    def dependent_stage(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dependent stage ran after a fatal database failure")

    monkeypatch.setattr(quarterly_refresh, "_stage_ingest_ir_transcripts", dependent_stage)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic fact failure"):
        quarterly_refresh.refresh_ticker(
            conn,
            ticker="TEST",
            project_root=tmp_path,
            holdings_dir=tmp_path,
            run_id="synthetic",
        )
    assert not conn.in_transaction
