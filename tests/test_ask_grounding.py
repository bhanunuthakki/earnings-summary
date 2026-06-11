"""Grounded evidence retrieval (src/ask/grounding.py, Ask v3): the three
channels (fact series / filing sections / transcript file lines), citation
numbering, the prompt block contract, and marker pruning. Pure
SQL + file reads — nothing here calls an LLM."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ask.grounding import (
    build_evidence_block,
    gather_evidence,
    question_terms,
    used_citation_items,
)

_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, source_url TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual'
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
    value TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    call_date TIMESTAMP, fiscal_period_type TEXT, period_end TIMESTAMP
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL
);
"""

_TRANSCRIPT_LINES = [
    "TST Q1 2026 Earnings Call",  # short header — never cited
    "CFO: Credit quality improved this quarter, with NPL formation slowing"
    " across every cohort and coverage holding stable.",
    "CEO: We continue to invest in the platform and expect operating leverage"
    " to build through the year as volumes scale.",
]

_FORM_10K = {
    "symbol": "TST",
    "period": "FY",
    "year": 2025,
    "Risk Factors": [
        {
            "Competition": [
                "We face intense competition in payments from large incumbents"
                " and well-funded entrants, which may pressure take rates."
            ]
        }
    ],
    "Income Taxes": [{"Effective rate": ["22% effective rate for fiscal 2025"]}],
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(_DDL)
    docs = [
        (
            1,
            "TST",
            "fmp",
            "fmp_income_statement",
            "data/historical/fmp/TST_income.json",
            "a",
            "2026-01-05 10:00:00",
            "ok",
            "https://fmp.example/f.json",
        ),
        (
            2,
            "TST",
            "fmp",
            "fmp_10k_json",
            "data/historical/fmp/TST_form_10k_2025.json",
            "b",
            "2026-01-05 10:00:00",
            "ok",
            None,
        ),
        (
            3,
            "TST",
            "transcript_audio",
            "earnings_call_transcript",
            "transcripts/processed/TST_Q1_2026.txt",
            "c",
            "2026-04-05 10:00:00",
            "ok",
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url) VALUES (?,?,?,?,?,?,?,?,?)",
        docs,
    )
    for pe, fpt, v in [
        ("2025-09-30 00:00:00", "Q3", 150_000_000.0),
        ("2025-06-30 00:00:00", "Q2", 132_000_000.0),
        ("2025-03-31 00:00:00", "Q1", 120_000_000.0),
    ]:
        conn.execute(
            "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type,"
            " line_item, value, unit, source_doc_id) VALUES ('TST', ?, ?, 'revenue', ?,"
            " 'USD', 1)",
            (pe, fpt, v),
        )
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit)"
        " VALUES (7, 'TST', 'Total Customers (millions)', 'millions')"
    )
    for pe, fpt, v in [("2025-09-30 00:00:00", "Q3", 110.0), ("2025-06-30 00:00:00", "Q2", 104.0)]:
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type,"
            " kpi_definition_id, value, unit, source_doc_id) VALUES ('TST', ?, ?, 7, ?,"
            " 'millions', 1)",
            (pe, fpt, v),
        )
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end)"
        " VALUES (1, 3, 'TST', 'Q1', '2026-03-31 00:00:00')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('TST', 'Test Co',"
        " 'portfolio')"
    )
    conn.commit()
    conn.close()

    filing = tmp_path / "data" / "historical" / "fmp" / "TST_form_10k_2025.json"
    filing.parent.mkdir(parents=True, exist_ok=True)
    filing.write_text(json.dumps(_FORM_10K), encoding="utf-8")
    transcript = tmp_path / "transcripts" / "processed" / "TST_Q1_2026.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(_TRANSCRIPT_LINES), encoding="utf-8")
    return tmp_path


def _gather(repo: Path, question: str, tickers: list[str] | None = None):
    return gather_evidence(
        question,
        repo_root=repo,
        db_path=repo / "data" / "portfolio.db",
        scope_tickers=tickers if tickers is not None else ["TST"],
    )


# ----------------------------------------------------------------------------


def test_fact_channel_matches_kpis_and_line_items(repo: Path) -> None:
    items = _gather(repo, "TST total customers and revenue trend")
    facts = [i for i in items if i.kind == "fact"]
    labels = [i.label for i in facts]
    assert "TST · Total Customers (millions)" in labels
    assert "TST · Revenue" in labels
    kpi = next(i for i in facts if "Customers" in i.label)
    # Newest-first series with period labels + formatted values.
    assert "Q3'25 110" in kpi.text
    assert kpi.text.index("Q3'25") < kpi.text.index("Q2'25")
    assert kpi.href == "/source/1"
    assert kpi.source_url == "https://fmp.example/f.json"
    fin = next(i for i in facts if i.label == "TST · Revenue")
    assert "150.0M" in fin.text  # humanized values
    assert ", USD" in fin.text


def test_filing_channel_scores_sections_and_deep_links(repo: Path) -> None:
    items = _gather(repo, "how bad is competition risk in payments?")
    filings = [i for i in items if i.kind == "filing"]
    assert filings, items
    top = filings[0]
    assert top.label == "TST 10-K FY2025 · Risk Factors"
    assert "intense competition" in top.text
    assert top.href == "/source/2?section=Risk%20Factors"


def test_transcript_channel_cites_file_lines(repo: Path) -> None:
    items = _gather(repo, "what did management say about credit quality and NPL formation?")
    transcripts = [i for i in items if i.kind == "transcript"]
    assert transcripts, items
    top = transcripts[0]
    assert top.href == "/source/3#L2"  # the CFO credit-quality line
    assert top.label == "TST Q1'26 call · L2"
    assert "NPL formation slowing" in top.text


def test_named_ticker_beats_scope_default(repo: Path) -> None:
    items = _gather(repo, "TST revenue trend", tickers=["OTHER"])
    assert any(i.label == "TST · Revenue" for i in items)


def test_numbering_is_sequential_and_block_carries_contract(repo: Path) -> None:
    items = _gather(repo, "total customers, competition risk, and credit quality")
    assert [i.n for i in items] == list(range(1, len(items) + 1))
    assert len(items) >= 3  # all three channels contributed
    block = build_evidence_block(items)
    assert "[1]" in block
    assert "CITE-OR-SAY-UNSURE" in block
    assert "Never invent numbers" in block
    assert build_evidence_block([]) == ""


def test_used_citation_items_prunes_unused_markers(repo: Path) -> None:
    items = _gather(repo, "total customers, competition risk, and credit quality")
    assert len(items) >= 3
    final = f"Customers grew nicely [{items[0].n}], and the filing flags it [{items[2].n}]."
    used = used_citation_items(final, items)
    assert [i.n for i in used] == [items[0].n, items[2].n]
    assert used_citation_items("no markers here", items) == []


def test_degrades_to_empty_without_db_or_files(tmp_path: Path) -> None:
    assert (
        gather_evidence(
            "revenue trend",
            repo_root=tmp_path,
            db_path=tmp_path / "data" / "portfolio.db",
            scope_tickers=["TST"],
        )
        == []
    )
    assert (
        gather_evidence("", repo_root=tmp_path, db_path=tmp_path / "x.db", scope_tickers=[]) == []
    )


def test_question_terms_drop_stopwords_and_fold_plurals() -> None:
    terms = question_terms("Why did the operating margins compress last quarter?")
    assert "margin" in terms  # plural folded
    assert "operating" in terms
    assert "why" not in terms
    assert "the" not in terms
