"""Tests for the 8-quarter portfolio coverage matrix module, CLI, and API route."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import comments_server
import pytest

from compute.portfolio_coverage_matrix import (
    build_portfolio_coverage_matrix,
    format_matrix_markdown,
    format_matrix_table,
)
from execution import portfolio_coverage_matrix as cli
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def test_build_matrix_generates_consecutive_quarters_and_detects_gaps(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    with connect_sqlite(db_path, role=SQLiteConnectionRole.WRITER, schema_preflight=False) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end,instrument_type) "
            "VALUES ('ACME','Acme Corp','portfolio','12-31','equity'),"
            "('RBRK','Rubrik Inc','portfolio','01-31','equity')"
        )
        conn.execute(
            "INSERT INTO documents (id,ticker,source_type,doc_type,period_end,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size) VALUES "
            "(1,'ACME','ir_doc','sec_10q','2026-09-30','filings/acme_q3.htm','1111','2026-11-01','ok',100),"
            "(2,'ACME','ir_doc','ir_press_release','2026-09-30','ir_documents/acme_pr.pdf','2222','2026-11-01','ok',100),"
            "(3,'ACME','ir_doc','ir_presentation','2026-09-30','ir_documents/acme_dk.pdf','3333','2026-11-01','ok',100),"
            "(4,'ACME','ir_doc','ir_transcript','2026-09-30','transcripts/processed/ACME_Q3_2026.txt','4444','2026-11-01','ok',100),"
            "(5,'ACME','ir_doc','ir_transcript','2026-03-31','transcripts/processed/ACME_Q1_2026.txt','5555','2026-05-01','ok',100),"
            "(6,'RBRK','ir_doc','sec_10q','2026-07-31','filings/rbrk_q2.htm','6666','2026-09-01','ok',100),"
            "(7,'ACME','fmp','fmp_analyst_estimates','2030-12-31','ir_documents/acme_est.json','7777','2026-11-01','ok',100),"
            "(8,'ACME','llm','llm_summary','2027-01-31','ir_documents/acme_sum.md','8888','2026-11-01','ok',100),"
            "(9,'ACME','ir_doc','ir_transcript','2027-03-31','transcripts/processed/ACME_FUTURE.txt','9999','2026-11-02','ok',100)"
        )
        conn.execute(
            "INSERT INTO transcripts (id,document_id,ticker,call_date,fiscal_period_type,period_end,"
            "source,has_qa_section,is_active,is_current,recorded_at) VALUES "
            "(10,4,'ACME','2026-11-01','Q3','2026-09-30','ir_transcript_doc',1,1,1,'2026-11-01'),"
            "(11,5,'ACME','2026-05-01','Q1','2026-03-31','aggregator_roic',0,1,1,'2026-05-01'),"
            "(12,9,'ACME','2026-11-02','Q1','2027-03-31','aggregator_roic',0,1,1,'2026-11-02')"
        )
        conn.execute(
            "INSERT INTO transcript_segments (id,transcript_id,seq,speaker,text) "
            "VALUES (100,10,0,'CEO','We will reach $1B ARR.')"
        )
        conn.execute(
            "INSERT INTO management_commitments "
            "(ticker,period_made,transcript_segment_id,period_target,kpi_name,comparator,"
            "target_value,unit,narrative,created_at) "
            "VALUES ('ACME','2026-09-30',100,'2027-12-31','ARR','>=',1000000000,'USD','Reach $1B ARR','2026-11-02')"
        )
        conn.execute(
            "INSERT INTO llm_artifacts (ticker,scope,purpose,fiscal_period,content_md,"
            "input_sha256,output_sha256,model,prompt_version,generated_at,dirty) VALUES "
            "('ACME','ticker','post_earnings_readout','2026-09-30','# Readout','a','b','m','v1','2026-11-25T10:00:00Z',0),"
            "('ACME','ticker','post_earnings_readout','2026-03-31','# Old','c','d','m','v1','2026-06-01T10:00:00Z',1)"
        )
        conn.execute(
            "INSERT INTO brief_provenance_log (ticker,generation_date,generated_at,sources_used,"
            "sections_status,trigger,artifact_path) VALUES "
            "('ACME','2026-11-25','2026-11-25T11:00:00Z','[]','{}','manual','output/research/ACME/2026-11-25_workspace.html')"
        )
        conn.commit()

    with connect_sqlite(
        db_path, role=SQLiteConnectionRole.READ_ONLY, schema_preflight=False
    ) as conn:
        matrix = build_portfolio_coverage_matrix(conn, quarters=4, list_type="portfolio")

    assert matrix.summary.companies == 2
    assert matrix.summary.total_cells == 8

    acme, rbrk = matrix.rows
    assert acme.ticker == "ACME"
    assert acme.latest_brief_at == "2026-11-25T11:00:00Z"
    assert [c.period_end for c in acme.cells] == [
        "2026-09-30",
        "2026-06-30",
        "2026-03-31",
        "2025-12-31",
    ]
    assert [c.fiscal_quarter for c in acme.cells] == [
        "Q3 FY26",
        "Q2 FY26",
        "Q1 FY26",
        "Q4 FY25",
    ]
    q3, q2_gap, q1, _ = acme.cells
    assert (q3.filing, q3.press_release, q3.deck) == (True, True, True)
    assert q3.transcript == "qa"
    assert q3.commitments == "extracted"
    assert q3.commitments_count == 1
    assert q3.readout == "ok"

    assert (q2_gap.filing, q2_gap.press_release, q2_gap.deck) == (False, False, False)
    assert q2_gap.transcript == "missing"
    assert q2_gap.commitments == "missing"
    assert q2_gap.readout == "missing"

    assert q1.transcript == "present"
    assert q1.commitments == "pending"
    assert q1.readout == "dirty"

    assert rbrk.ticker == "RBRK"
    assert [c.period_end for c in rbrk.cells] == [
        "2026-07-31",
        "2026-04-30",
        "2026-01-31",
        "2025-10-31",
    ]
    assert [c.fiscal_quarter for c in rbrk.cells] == [
        "Q2 FY27",
        "Q1 FY27",
        "Q4 FY26",
        "Q3 FY26",
    ]

    table_out = format_matrix_table(matrix)
    md_out = format_matrix_markdown(matrix)
    assert "ACME" in table_out and "Q2 FY26" in table_out
    assert "| ACME | 12-31 |" in md_out and "| RBRK | 01-31 |" in md_out


def test_cli_and_flask_route_serve_coverage_matrix(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = migrated_db(tmp_path / "portfolio.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO tracked_companies (ticker,name,list_type,fiscal_year_end,instrument_type) "
            "VALUES ('TEST','Test Co','portfolio','12-31','equity')"
        )
        conn.commit()

    rc = cli.main(["--db", str(db_path), "--quarters", "4", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarters"] == 4
    assert payload["summary"]["companies"] == 1
    assert payload["rows"][0]["ticker"] == "TEST"

    app = comments_server.create_app(repo_root=tmp_path, db_path=db_path)
    client = app.test_client()
    resp = client.get("/api/portfolio/coverage-matrix?quarters=4&list=portfolio")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body is not None
    assert body["summary"]["companies"] == 1
    assert body["rows"][0]["ticker"] == "TEST"

    bad = client.get("/api/portfolio/coverage-matrix?quarters=99")
    assert bad.status_code == 400
