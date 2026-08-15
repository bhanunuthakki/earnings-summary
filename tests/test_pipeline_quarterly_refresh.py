"""Tests for src/pipeline/quarterly_refresh.py — DAG orchestration + per-stage idempotency."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from models.kpis import BreachStatus
from pipeline.quarterly_refresh import (
    RefreshReport,
    StageName,
    StageResult,
    StageStatus,
    TickerExecutionReceipt,
    TickerExecutionStatus,
    TickerRefreshReport,
    refresh_portfolio,
    refresh_ticker,
)


def _create_schema(conn: sqlite3.Connection) -> None:
    """Mirror the production schema for the tables refresh_ticker touches."""
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start TIMESTAMP,
            period_end TIMESTAMP,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at TIMESTAMP NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0
        );
        CREATE TABLE segment_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            segment_name TEXT NOT NULL,
            metric TEXT NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low REAL,
            threshold_high REAL,
            notes TEXT,
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
        ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        CREATE TABLE transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            call_date TIMESTAMP,
            fiscal_period_type TEXT,
            period_end TIMESTAMP,
            source_url TEXT
        );
        CREATE TABLE transcript_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            speaker TEXT,
            speaker_role TEXT,
            time_code_start TEXT,
            time_code_end TEXT,
            text TEXT NOT NULL
        );
        CREATE TABLE management_commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_made TIMESTAMP NOT NULL,
            transcript_segment_id INTEGER NOT NULL,
            period_target TIMESTAMP NOT NULL,
            kpi_name TEXT NOT NULL,
            comparator TEXT NOT NULL,
            target_value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            narrative TEXT NOT NULL,
            realized_value NUMERIC(24, 6),
            realized_doc_id INTEGER,
            outcome TEXT,
            evaluated_at TIMESTAMP
        );
        CREATE TABLE thesis_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL UNIQUE,
            thesis TEXT,
            last_updated TIMESTAMP,
            breach_status TEXT,
            raw_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        );
        CREATE TABLE thesis_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            evaluated_at TIMESTAMP NOT NULL,
            overall_status TEXT NOT NULL,
            rule_evaluations_json TEXT NOT NULL,
            run_id TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


def _seed_thesis_state(
    conn: sqlite3.Connection, ticker: str, status: BreachStatus | None = None
) -> None:
    conn.execute(
        "INSERT INTO thesis_state (ticker, raw_json, breach_status, ingested_at) "
        "VALUES (?, '{}', ?, ?)",
        (ticker, status.value if status else None, datetime.now()),
    )
    conn.commit()


def _seed_quarterly_income(conn: sqlite3.Connection, ticker: str) -> None:
    """Insert one quarterly income statement document + its 4 line items."""
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES (?, 'fmp', 'fmp_income_statement', ?, ?, ?, 'ok', 1, ?)",
        (
            ticker,
            f"data/historical/fmp/{ticker}_income_statement_quarterly.json",
            "a" * 64,
            datetime.now(),
            datetime(2024, 12, 31),
        ),
    )
    doc_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    pe = datetime(2024, 12, 31)
    for line, val in [
        ("revenue", 1000),
        ("operating_income", 200),
        ("net_income", 150),
        ("gross_profit", 500),
    ]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, "
            "line_item, value, currency, unit, source_doc_id) "
            "VALUES (?, ?, 'Q4', ?, ?, 'USD', 'actual', ?)",
            (ticker, pe, line, str(val), doc_id),
        )
    conn.commit()


def _seed_ir_pdf(conn: sqlite3.Connection, ticker: str, doc_type: str = "ir_presentation") -> int:
    """Insert one IR doc with no kpi_facts yet (becomes pending LLM work)."""
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, "
        "sha256, fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES (?, 'ir_doc', ?, ?, ?, ?, 'ok', 1, ?)",
        (
            ticker,
            doc_type,
            f"ir_documents/{ticker}/2024-12-31/x.pdf",
            "b" * 64,
            datetime.now(),
            datetime(2024, 12, 31),
        ),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def _write_holdings(tmp_path: Path, ticker: str, threshold: float = 0) -> None:
    payload = {
        "ticker": ticker,
        "thesis": "test",
        "break_rules": [
            {
                "rule_id": "op_margin_below",
                "kpi_name": "Operating Margin (GAAP)",
                "comparator": "lt",
                "threshold": threshold,
                "unit": "percent",
                "consecutive_periods": 1,
                "narrative": f"OpMargin < {threshold}",
            },
        ],
    }
    (tmp_path / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_refresh_ticker_runs_eight_stages_by_default(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Without --fetch-sec, the 8 network-free stages execute, in order.

    validate_segment_cache leads (a post-fetch gate over the raw cache before the
    extractors run); persist_timeseries_signals sits between evaluate_thesis
    (which needs facts settled) and surface_pending_llm (which surfaces follow-ups
    that may now include signal-driven items)."""
    _seed_thesis_state(conn, "X")
    _seed_quarterly_income(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)

    report = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    stage_names = [s.name for s in report.stages]
    assert stage_names == [
        StageName.VALIDATE_SEGMENT_CACHE,
        StageName.EXTRACT_FMP_FACTS,
        StageName.INGEST_IR_TRANSCRIPTS,
        StageName.DERIVE_FMP_KPIS,
        StageName.MATCH_COMMITMENTS,
        StageName.EVALUATE_THESIS,
        StageName.PERSIST_TIMESERIES_SIGNALS,
        StageName.SURFACE_PENDING_LLM,
    ]


def _write_segment_cache(
    project_root: Path, ticker: str, *, q4_cloud: int, q4_revenue: int
) -> None:
    """Write a minimal product-segment + income-statement cache for one Q4 quarter.

    q4_cloud drives the segment sum; q4_revenue is the income-statement total the
    audit reconciles against. A contaminated record sets q4_cloud high enough that
    the segment sum exceeds q4_revenue * (1 + tolerance).
    """
    fmp = project_root / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / f"{ticker}_product_segments_quarterly.json").write_text(
        json.dumps(
            [
                {
                    "symbol": ticker,
                    "fiscalYear": 2025,
                    "period": "Q4",
                    "reportedCurrency": "USD",
                    "date": "2025-12-31",
                    "data": {"Search": 60_000_000_000, "Cloud": q4_cloud},
                }
            ]
        ),
        encoding="utf-8",
    )
    (fmp / f"{ticker}_income_statement_quarterly.json").write_text(
        json.dumps(
            [{"symbol": ticker, "period": "Q4", "date": "2025-12-31", "revenue": q4_revenue}]
        ),
        encoding="utf-8",
    )


def test_validate_segment_cache_skipped_when_no_cache(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """No cache files under project_root -> SKIPPED (the common test/bootstrap case)."""
    _seed_thesis_state(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)
    report = refresh_ticker(
        conn, ticker="X", project_root=tmp_path, holdings_dir=tmp_path, run_id="r1"
    )
    stage = next(s for s in report.stages if s.name == StageName.VALIDATE_SEGMENT_CACHE)
    assert stage.status == StageStatus.SKIPPED
    assert "no segment cache" in stage.notes


def test_validate_segment_cache_passes_clean_cache(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A cache whose segment sum reconciles with revenue -> OK."""
    _seed_thesis_state(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)
    # 60B + 17B = 77B vs 77B revenue -> within tolerance.
    _write_segment_cache(tmp_path, "X", q4_cloud=17_000_000_000, q4_revenue=77_000_000_000)
    report = refresh_ticker(
        conn, ticker="X", project_root=tmp_path, holdings_dir=tmp_path, run_id="r1"
    )
    stage = next(s for s in report.stages if s.name == StageName.VALIDATE_SEGMENT_CACHE)
    assert stage.status == StageStatus.OK


def test_validate_segment_cache_flags_contamination(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A contaminated record (segment sum >> revenue) -> FAILED with the quarter in notes."""
    _seed_thesis_state(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)
    # 60B + 60B = 120B vs 77B revenue -> 1.56x, over the 1.10 cap.
    _write_segment_cache(tmp_path, "X", q4_cloud=60_000_000_000, q4_revenue=77_000_000_000)
    report = refresh_ticker(
        conn, ticker="X", project_root=tmp_path, holdings_dir=tmp_path, run_id="r1"
    )
    stage = next(s for s in report.stages if s.name == StageName.VALIDATE_SEGMENT_CACHE)
    assert stage.status == StageStatus.FAILED
    assert stage.rows_processed == 1
    assert "2025-12-31" in stage.notes


def test_refresh_ticker_includes_sec_stage_when_opt_in(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch
) -> None:
    """fetch_sec=True prepends the SEC fetch stage. Stub the network call."""
    _seed_thesis_state(conn, "MELI")
    _seed_quarterly_income(conn, "MELI")
    _write_holdings(tmp_path, "MELI", threshold=0)

    from pipeline import quarterly_refresh as qr_mod
    from pipeline.sec_xbrl import IngestStats

    def fake_ingest(conn, *, ticker, project_root):
        return IngestStats(accessions_inserted=2, facts_inserted=10)

    monkeypatch.setattr(qr_mod, "ingest_sec_for_ticker", fake_ingest)
    report = refresh_ticker(
        conn,
        ticker="MELI",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
        fetch_sec=True,
    )
    stage_names = [s.name for s in report.stages]
    assert stage_names[0] == StageName.FETCH_SEC_XBRL


def test_refresh_ticker_skips_sec_stage_for_unmapped_ticker(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A ticker absent from CIK_MAP gets SKIPPED on the SEC stage (no error)."""
    _seed_thesis_state(conn, "FLKR")
    _write_holdings(tmp_path, "FLKR", threshold=0)
    report = refresh_ticker(
        conn,
        ticker="FLKR",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
        fetch_sec=True,
    )
    sec_stage = next(s for s in report.stages if s.name == StageName.FETCH_SEC_XBRL)
    assert sec_stage.status == StageStatus.SKIPPED
    assert "no CIK" in sec_stage.notes


def test_quarterly_sec_stage_denies_watchlist_before_network(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import quarterly_refresh as module

    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, "
        "fiscal_year_end TEXT, archived_at TEXT)"
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('MELI', 'watchlist', '12-31', NULL)")

    def _unexpected_ingest(*_args: object, **_kwargs: object) -> object:
        pytest.fail("network boundary was crossed")

    monkeypatch.setattr(module, "ingest_sec_for_ticker", _unexpected_ingest)

    stage = module._stage_fetch_sec_xbrl(
        conn,
        ticker="MELI",
        project_root=tmp_path,
        owner_requested=True,
    )

    assert stage.status is StageStatus.SKIPPED
    assert "coverage_depth_denied" in stage.notes


def test_quarterly_sec_stage_allows_explicit_evaluation(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import quarterly_refresh as module
    from pipeline.sec_xbrl import IngestStats

    conn.execute(
        "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, "
        "fiscal_year_end TEXT, archived_at TEXT)"
    )
    conn.execute("INSERT INTO tracked_companies VALUES ('MELI', 'evaluation', '12-31', NULL)")
    calls: list[str] = []

    def _ingest(*_args: object, ticker: str, **_kwargs: object) -> IngestStats:
        calls.append(ticker)
        return IngestStats(accessions_inserted=1, facts_inserted=2)

    monkeypatch.setattr(module, "ingest_sec_for_ticker", _ingest)
    stage = module._stage_fetch_sec_xbrl(
        conn,
        ticker="MELI",
        project_root=tmp_path,
        owner_requested=True,
    )

    assert stage.status is StageStatus.OK
    assert calls == ["MELI"]


def test_refresh_ticker_derives_kpis_when_facts_present(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _seed_thesis_state(conn, "X")
    _seed_quarterly_income(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)

    report = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    derive_stage = next(s for s in report.stages if s.name == StageName.DERIVE_FMP_KPIS)
    assert derive_stage.status == StageStatus.OK
    assert derive_stage.rows_processed >= 3  # OpMargin, NetMargin, GrossMargin


def test_refresh_ticker_skips_derive_when_no_facts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Ticker with no FMP income statement -> DERIVE_FMP_KPIS skipped, not failed."""
    _seed_thesis_state(conn, "Y")
    _write_holdings(tmp_path, "Y", threshold=0)

    report = refresh_ticker(
        conn,
        ticker="Y",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    derive_stage = next(s for s in report.stages if s.name == StageName.DERIVE_FMP_KPIS)
    assert derive_stage.status == StageStatus.SKIPPED


def test_refresh_ticker_detects_status_change(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Prior status OK + new BREACH eval -> breach_status_changed = True."""
    _seed_thesis_state(conn, "X", status=BreachStatus.OK)
    _seed_quarterly_income(conn, "X")
    _write_holdings(tmp_path, "X", threshold=50)  # 200/1000=20%, < 50% -> BREACH

    report = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    assert report.breach_status == BreachStatus.BREACH
    assert report.breach_status_changed is True


def test_refresh_ticker_surfaces_pending_ir_pdfs(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """IR PDFs without kpi_facts show up as pending LLM work."""
    _seed_thesis_state(conn, "X")
    _seed_ir_pdf(conn, "X", "ir_press_release")
    _seed_ir_pdf(conn, "X", "ir_presentation")
    _write_holdings(tmp_path, "X", threshold=0)

    report = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    pdf_items = [p for p in report.pending_work if p.kind == "ir_pdf_kpi_extraction"]
    assert len(pdf_items) == 2

    pending_stage = next(s for s in report.stages if s.name == StageName.SURFACE_PENDING_LLM)
    assert pending_stage.status == StageStatus.NEEDS_LLM
    assert pending_stage.rows_processed == 2


def test_refresh_ticker_surfaces_pending_transcripts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Transcripts without commitments are surfaced for follow-up."""
    _seed_thesis_state(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)
    cur = conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256, "
        "fetched_at, fetch_status, raw_bytes_size, period_end) "
        "VALUES ('X', 'transcript_audio', 'earnings_call_transcript', 'a.pdf', ?, ?, 'ok', 1, ?)",
        ("c" * 64, datetime.now(), datetime(2024, 12, 31)),
    )
    doc_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    cur = conn.execute(
        "INSERT INTO transcripts (document_id, ticker, period_end) VALUES (?, 'X', ?)",
        (doc_id, datetime(2024, 12, 31)),
    )
    transcript_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "INSERT INTO transcript_segments (transcript_id, seq, text) VALUES (?, 0, 'hello')",
        (transcript_id,),
    )
    conn.commit()

    report = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    transcript_items = [
        p for p in report.pending_work if p.kind == "transcript_commitment_extraction"
    ]
    assert len(transcript_items) == 1


def test_refresh_ticker_idempotent_on_rerun(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Running the DAG twice in a row produces zero new rows on the second run."""
    _seed_thesis_state(conn, "X")
    _seed_quarterly_income(conn, "X")
    _write_holdings(tmp_path, "X", threshold=0)

    refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    second = refresh_ticker(
        conn,
        ticker="X",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r2",
    )
    derive_stage = next(s for s in second.stages if s.name == StageName.DERIVE_FMP_KPIS)
    # Re-derivation produces the same kpi_facts; UNIQUE index dedupes
    assert derive_stage.rows_processed == 0
    assert derive_stage.status == StageStatus.OK


def test_refresh_ticker_handles_missing_holdings_gracefully(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Watchlist ticker (no holdings JSON) -> evaluate stage SKIPPED, doesn't raise."""
    _seed_thesis_state(conn, "Z")
    # No holdings JSON written
    report = refresh_ticker(
        conn,
        ticker="Z",
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="r1",
    )
    eval_stage = next(s for s in report.stages if s.name == StageName.EVALUATE_THESIS)
    assert eval_stage.status == StageStatus.SKIPPED
    assert report.breach_status is None


def test_refresh_portfolio_marks_failed_and_unattempted_after_exception(
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import quarterly_refresh as module

    attempted: list[str] = []

    def _fake_refresh(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: Path,
        holdings_dir: Path,
        run_id: str,
        fetch_sec: bool,
        transcript_artifacts: object | None = None,
    ) -> TickerRefreshReport:
        del project_root, holdings_dir, run_id, fetch_sec, transcript_artifacts
        attempted.append(ticker)
        if ticker == "MELI":
            raise RuntimeError("provider exploded")
        return TickerRefreshReport(
            ticker=ticker,
            stages=(),
            breach_status=None,
            breach_status_changed=False,
            pending_work=(),
        )

    monkeypatch.setattr(module, "refresh_ticker", _fake_refresh)
    report = refresh_portfolio(
        conn,
        tickers=["NU", "MELI", "ORCL"],
        project_root=tmp_path,
        holdings_dir=tmp_path,
        run_id="run-1",
    )

    assert attempted == ["NU", "MELI"]
    assert [(item.ticker, item.status) for item in report.execution] == [
        ("NU", TickerExecutionStatus.COMPLETED),
        ("MELI", TickerExecutionStatus.FAILED),
        ("ORCL", TickerExecutionStatus.UNATTEMPTED),
    ]
    assert report.execution[1].error == "RuntimeError: provider exploded"


@pytest.mark.parametrize("failure_kind", ["stage", "exception"])
def test_cli_emits_one_redacted_terminal_receipt_and_ends_run_once(
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import quarterly_refresh as cli

    leak_sentinel = "supersecretvalue"
    ticker_report = TickerRefreshReport(
        ticker="NU",
        stages=()
        if failure_kind == "exception"
        else (
            StageResult(
                name=StageName.DERIVE_FMP_KPIS,
                status=StageStatus.FAILED,
                rows_processed=0,
                notes=f"provider failed api_key={leak_sentinel}",
            ),
        ),
        breach_status=None,
        breach_status_changed=False,
        pending_work=(),
    )
    now = datetime.now()
    report = RefreshReport(
        run_id="run-1",
        started_at=now,
        ended_at=now,
        tickers=() if failure_kind == "exception" else (ticker_report,),
        execution=(
            TickerExecutionReceipt(
                ticker="NU",
                status=(
                    TickerExecutionStatus.FAILED
                    if failure_kind == "exception"
                    else TickerExecutionStatus.COMPLETED
                ),
                error=(
                    f"RuntimeError: api_key={leak_sentinel}"
                    if failure_kind == "exception"
                    else None
                ),
            ),
        ),
    )

    conn = sqlite3.connect(":memory:")
    ended: list[tuple[object, ...]] = []

    def fake_open_db(_path: str | Path) -> sqlite3.Connection:
        return conn

    def fake_start_run(*_args: object, **_kwargs: object) -> str:
        return "run-1"

    def fake_refresh_portfolio(*_args: object, **_kwargs: object) -> RefreshReport:
        return report

    monkeypatch.setattr(cli, "open_db", fake_open_db)
    monkeypatch.setattr(cli, "stage_pending_issuer_transcripts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "start_run", fake_start_run)
    monkeypatch.setattr(cli, "refresh_portfolio", fake_refresh_portfolio)

    def record_end(*args: object, **kwargs: object) -> None:
        ended.append((*args, kwargs))

    monkeypatch.setattr(cli, "end_run", record_end)

    assert cli.main(["--ticker", "NU", "--json", "--db", "unused.db"]) == 1
    output = capsys.readouterr().out
    assert output.count('"receipt"') == 1
    assert leak_sentinel not in output
    parsed = json.loads(output)
    assert parsed["receipt"]["status"] == "failed"
    assert len(parsed["receipt"]["failed"]) == 1
    assert len(ended) == 1


def test_cli_human_output_redacts_all_stage_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from execution import quarterly_refresh as cli

    leak_sentinel = "supersecretvalue"
    stage_results = tuple(
        StageResult(
            name=name,
            status=(
                StageStatus.FAILED if name is StageName.VALIDATE_SEGMENT_CACHE else StageStatus.OK
            ),
            rows_processed=0,
            notes=(
                f"provider failed api_key={leak_sentinel}"
                if name in {StageName.VALIDATE_SEGMENT_CACHE, StageName.EVALUATE_THESIS}
                else "ok"
            ),
        )
        for name in (
            StageName.VALIDATE_SEGMENT_CACHE,
            StageName.EXTRACT_FMP_FACTS,
            StageName.INGEST_IR_TRANSCRIPTS,
            StageName.DERIVE_FMP_KPIS,
            StageName.MATCH_COMMITMENTS,
            StageName.EVALUATE_THESIS,
            StageName.PERSIST_TIMESERIES_SIGNALS,
            StageName.SURFACE_PENDING_LLM,
        )
    )
    now = datetime.now()
    report = RefreshReport(
        run_id="run-1",
        started_at=now,
        ended_at=now,
        tickers=(
            TickerRefreshReport(
                ticker="NU",
                stages=stage_results,
                breach_status=BreachStatus.BREACH,
                breach_status_changed=True,
                pending_work=(),
            ),
        ),
        execution=(
            TickerExecutionReceipt(
                ticker="NU",
                status=TickerExecutionStatus.COMPLETED,
            ),
        ),
    )
    conn = sqlite3.connect(":memory:")

    def fake_open_db(_path: str | Path) -> sqlite3.Connection:
        return conn

    def fake_start_run(*_args: object, **_kwargs: object) -> str:
        return "run-1"

    def fake_refresh_portfolio(*_args: object, **_kwargs: object) -> RefreshReport:
        return report

    def fake_end_run(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cli, "open_db", fake_open_db)
    monkeypatch.setattr(cli, "stage_pending_issuer_transcripts", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "start_run", fake_start_run)
    monkeypatch.setattr(cli, "refresh_portfolio", fake_refresh_portfolio)
    monkeypatch.setattr(cli, "end_run", fake_end_run)

    assert cli.main(["--ticker", "NU", "--db", "unused.db"]) == 1
    output = capsys.readouterr().out
    assert output.count('"receipt"') == 1
    assert leak_sentinel not in output
    assert output.count("api_key=***") >= 3
