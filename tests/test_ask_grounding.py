"""Grounded evidence retrieval (src/ask/grounding.py, Ask v3): the three
channels (fact series / filing sections / transcript file lines), citation
numbering, the prompt block contract, and marker pruning. Pure
SQL + file reads — nothing here calls an LLM."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, cast

import pytest

import ask.grounding as grounding_module
from ask.grounding import (
    GroundingRetrievalError,
    StrictEvidenceConnection,
    build_evidence_block,
    gather_evidence,
    question_terms,
    used_citation_items,
)
from pipeline.confidence import IssuesByTicker


class _ScoredFilingSections(Protocol):
    def __call__(
        self,
        conn: sqlite3.Connection | None,
        repo_root: Path,
        terms: list[str],
        ticker: str,
        paths: list[tuple[str, int, Path]],
        *,
        strict: bool = False,
    ) -> list[tuple[int, dict[str, object]]]: ...


class _ScoredTranscriptLines(Protocol):
    def __call__(
        self,
        repo_root: Path,
        terms: list[str],
        ticker: str,
        docs: list[tuple[int, str, str, str]],
        *,
        strict: bool = False,
    ) -> list[tuple[int, dict[str, object]]]: ...


_score_filing_sections = cast(
    "_ScoredFilingSections",
    getattr(grounding_module, "_scored_filing_sections"),
)
_score_transcript_lines = cast(
    "_ScoredTranscriptLines",
    getattr(grounding_module, "_scored_transcript_lines"),
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
CREATE TABLE kpi_fact_semantic_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kpi_fact_id INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    supersedes_context_id INTEGER,
    status TEXT NOT NULL,
    publication_lane TEXT NOT NULL,
    metric_name_as_reported TEXT NOT NULL,
    accounting_basis TEXT NOT NULL,
    consolidation_scope TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    unit_scale TEXT NOT NULL
);
CREATE TABLE transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL, ticker TEXT NOT NULL,
    call_date TIMESTAMP, fiscal_period_type TEXT, period_end TIMESTAMP
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL, archived_at TEXT
);
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, valuation_date TEXT NOT NULL,
    npv_per_share NUMERIC, live_price NUMERIC,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        cursor = conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type,"
            " kpi_definition_id, value, unit, source_doc_id) VALUES ('TST', ?, ?, 7, ?,"
            " 'millions', 1)",
            (pe, fpt, v),
        )
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts (kpi_fact_id,status,publication_lane,"
            "metric_name_as_reported,accounting_basis,consolidation_scope,dimensions_json,"
            "unit_scale) VALUES (?, 'admitted', 'current_actual', ?, 'management',"
            "'consolidated', '{}', 'millions')",
            (cursor.lastrowid, "Total Customers (millions)"),
        )
    conn.execute(
        "INSERT INTO transcripts (id, document_id, ticker, fiscal_period_type, period_end)"
        " VALUES (1, 3, 'TST', 'Q1', '2026-03-31 00:00:00')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES ('TST', 'Test Co',"
        " 'portfolio')"
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price)"
        " VALUES ('TST', '2026-06-01', 50.0, 40.0)"
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


def test_strict_read_connection_cannot_hide_channel_sql_errors() -> None:
    raw = sqlite3.connect(":memory:")
    strict = StrictEvidenceConnection(raw)
    try:
        with pytest.raises(GroundingRetrievalError, match="query failed"):
            strict.execute("SELECT * FROM missing_grounding_table")
    finally:
        strict.close()


def test_strict_filing_retrieval_exposes_a_corrupt_source(tmp_path: Path) -> None:
    filing = tmp_path / "TST_form_10k_2025.json"
    filing.write_text("{not-json", encoding="utf-8")

    with pytest.raises(GroundingRetrievalError, match="filing evidence could not be read"):
        _score_filing_sections(
            None,
            tmp_path,
            ["revenue"],
            "TST",
            [("10k", 2025, filing)],
            strict=True,
        )


def test_strict_transcript_retrieval_exposes_an_unreadable_source(tmp_path: Path) -> None:
    with pytest.raises(
        GroundingRetrievalError,
        match="transcript evidence could not be read",
    ):
        _score_transcript_lines(
            tmp_path,
            ["revenue"],
            "TST",
            [(1, "missing-transcript.txt", "Q1", "2026-03-31")],
            strict=True,
        )


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


def _insert_kpi(repo: Path, def_id: int, name: str, value: float, unit: str = "percent") -> None:
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (?, 'TST', ?, ?)",
            (def_id, name, unit),
        )
        cursor = conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type,"
            " kpi_definition_id, value, unit, source_doc_id)"
            " VALUES ('TST', '2025-09-30 00:00:00', 'Q3', ?, ?, ?, 1)",
            (def_id, value, unit),
        )
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts (kpi_fact_id,status,publication_lane,"
            "metric_name_as_reported,accounting_basis,consolidation_scope,dimensions_json,"
            "unit_scale) VALUES (?, 'admitted', 'current_actual', ?, 'management',"
            "'consolidated', '{}', ?)",
            (
                cursor.lastrowid,
                name,
                unit if unit in {"thousands", "millions", "billions"} else "none",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_fact_channel_resolves_section_qualified_capture_name(repo: Path) -> None:
    """Capture-all stores section-qualified KPI names (``section — … — leaf``).
    A typed metric name must resolve via the leaf even when (a) the full qualified
    core is not a substring of the question and (b) a parenthetical (annual /
    annualized) qualifier sits on the leaf — the exact case the owner flagged."""
    _insert_kpi(
        repo,
        20,
        "Net Interest Income (Details) — Net interest margin (annualized)",
        18.5,
    )
    facts = [i for i in _gather(repo, "what is TST net interest margin?") if i.kind == "fact"]
    assert any("Net interest margin" in i.label for i in facts), [i.label for i in facts]


def test_fact_channel_single_word_leaf_does_not_flood(repo: Path) -> None:
    """A generic single-word leaf ("Total") must NOT match on the leaf alone —
    only the full qualified core can, so a bare "total" query the full core does
    not contain finds nothing. Guards the leaf-match broadening against noise."""
    _insert_kpi(repo, 21, "Schedule of Maturities (Details) — 2027 — Total", 999.0, "actual")
    facts = [i for i in _gather(repo, "what is the total?") if i.kind == "fact"]
    assert not any("Maturities" in i.label for i in facts), [i.label for i in facts]


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
    assert "Cite PER SENTENCE" in block  # the S8 per-claim contract
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


# ----------------------------------------------------------------------------
# Scored confidence on fact evidence (S2 → S8 citation popover)


def test_fact_items_carry_scored_confidence_when_the_column_exists(repo: Path) -> None:
    """With the S2 confidence column present, the newest fact row's score
    rides into the EvidenceItem and its chip payload (the citation popover
    renders it as a %)."""
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE kpi_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
    conn.execute("ALTER TABLE financial_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
    conn.execute("UPDATE kpi_facts SET confidence = 0.79 WHERE period_end LIKE '2025-09-30%'")
    conn.commit()
    conn.close()
    items = _gather(repo, "TST total customers and revenue trend")
    kpi = next(i for i in items if "Customers" in i.label)
    assert kpi.confidence == 0.79  # the newest row's score, not the default
    assert kpi.chip_payload()["confidence"] == 0.79
    fin = next(i for i in items if i.label == "TST · Revenue")
    assert fin.confidence == 1.0


def test_fact_items_carry_structured_popover_fields(repo: Path) -> None:
    """The newest fact row's issuer / backing-doc form / fiscal period / value
    ride into the chip payload so the citation popover can render the auditable
    header (ticker · doc-type pill · period) and the 'latest reported' value."""
    items = _gather(repo, "TST total customers and revenue trend")
    kpi = next(i for i in items if "Customers" in i.label)
    assert kpi.ticker == "TST"
    assert kpi.period == "Q3'25"  # newest row (2025-09-30, Q3)
    assert kpi.value == "110 millions"  # scaled value + unit affix
    assert kpi.doc_type == "fmp_income_statement"  # the backing document's form
    pay = kpi.chip_payload()
    assert pay["ticker"] == "TST"
    assert pay["period"] == "Q3'25"
    assert pay["value"] == "110 millions"
    assert pay["doc_type"] == "fmp_income_statement"
    # Currency line items get the $ affix.
    fin = next(i for i in items if i.label == "TST · Revenue")
    assert fin.value == "$150.0M"
    # A non-fact channel (transcript) carries no value (it's a passage, not a
    # single number) and no structured ticker header.
    tr_items = _gather(repo, "what did management say about credit quality and NPL formation?")
    tr = next(i for i in tr_items if i.kind == "transcript")
    assert tr.value is None
    assert tr.ticker is None
    assert tr.chip_payload()["value"] is None


def test_legacy_db_without_confidence_column_still_serves_facts(repo: Path) -> None:
    """The fixture DDL predates the confidence column — the fact channel
    must fall back (None confidence) instead of losing the series."""
    items = _gather(repo, "TST total customers and revenue trend")
    facts = [i for i in items if i.kind == "fact"]
    assert facts, items
    assert all(i.confidence is None for i in facts)
    assert all(i.chip_payload()["confidence"] is None for i in facts)


# ----------------------------------------------------------------------------
# Portfolio packs in the numbering (S4)


def test_selected_packs_lead_the_numbering(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ask.router import PackRoute

    def _dcf_route(_q: str, *, db_path: Path) -> PackRoute:
        return PackRoute(("dcf",), "ok")

    monkeypatch.setattr("ask.grounding.route_packs", _dcf_route)
    items = _gather(repo, "TST revenue trend — is it cheap vs my model?")
    assert items, "expected pack + fact evidence"
    assert items[0].kind == "dcf"
    assert items[0].href == "/#decisions_record"
    assert "fair $50.00 vs live $40.00" in items[0].text
    assert "+25% upside" in items[0].text
    # Document evidence still follows, numbering stays sequential.
    assert any(i.kind == "fact" for i in items[1:])
    assert [i.n for i in items] == list(range(1, len(items) + 1))


def test_pack_channel_failure_never_costs_document_evidence(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_q: str, *, db_path: Path) -> object:
        raise RuntimeError("router bug")

    monkeypatch.setattr("ask.grounding.route_packs", _boom)
    items = _gather(repo, "TST revenue trend")
    assert any(i.kind == "fact" for i in items)
    assert all(i.kind in ("fact", "filing", "transcript") for i in items)


def test_unpatched_router_is_blocked_by_conftest_and_degrades(repo: Path) -> None:
    """The autouse conftest blocker raises at the router's transport seam;
    route_packs swallows it (fail-closed) — document evidence is unaffected
    and no subprocess can launch from the suite."""
    items = _gather(repo, "TST revenue trend")
    assert any(i.kind == "fact" for i in items)
    assert all(i.kind in ("fact", "filing", "transcript") for i in items)


# ----------------------------------------------------------------------------
# fact_ref PK fast-path (S12, Instrument Paradigm Law 2)


def test_fact_ref_kpi_resolves_exact_series_by_pk(repo: Path) -> None:
    """A ``kpi:{ticker}:{def_id}`` token resolves the exact series by PK, sets
    the item's ``fact_ref``, and dedupes its NL twin away (one Customers item)."""
    items = _gather(repo, "Total Customers (millions) — kpi:TST:7")
    facts = [i for i in items if i.kind == "fact"]
    pinned = [i for i in facts if i.fact_ref == "kpi:TST:7"]
    assert len(pinned) == 1, facts
    it = pinned[0]
    assert it.label == "TST · Total Customers (millions)"
    assert "Q3'25 110" in it.text
    assert it.chip_payload()["fact_ref"] == "kpi:TST:7"
    # The fast-path leads the fact channel, and the NL match for the same
    # metric is deduped against it — exactly one Customers item.
    assert sum(1 for i in facts if "Customers" in i.label) == 1


def test_fact_ref_fin_resolves_financial_line_item(repo: Path) -> None:
    items = _gather(repo, "fin:TST:revenue:Q")
    pinned = [i for i in items if i.fact_ref == "fin:TST:revenue:Q"]
    assert len(pinned) == 1, items
    assert pinned[0].label == "TST · Revenue"
    assert "150.0M" in pinned[0].text


def test_fact_ref_unknown_or_mismatched_resolves_nothing(repo: Path) -> None:
    """The handle is PK + ticker guarded: an unknown def_id, and a real def_id
    under the wrong ticker, both resolve nothing — and never raise."""
    assert not [i for i in _gather(repo, "kpi:TST:999") if i.fact_ref]
    assert not [i for i in _gather(repo, "kpi:OTHER:7", tickers=["OTHER"]) if i.fact_ref]


def test_nl_matched_facts_carry_no_fact_ref(repo: Path) -> None:
    """The string-match path is the FALLBACK: its items resolve no handle."""
    items = _gather(repo, "TST total customers and revenue trend")
    facts = [i for i in items if i.kind == "fact"]
    assert facts
    assert all(i.fact_ref is None for i in facts)
    assert all(i.chip_payload()["fact_ref"] is None for i in facts)


# ----------------------------------------------------------------------------
# Provenance into the prompt (L2, Close-the-Loops): the scored confidence and
# any cross-source ⚠ disagreement reach the MODEL-read text, not only the chip.


def _enable_confidence_and_issues(db: Path) -> None:
    """Add the S2 confidence columns + a validation_issues table to the
    fixture, then plant a sub-par revenue fact disagreeing with SEC."""
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE financial_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
    conn.execute("ALTER TABLE kpi_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
    # The displayed cell is FMP (doc 1) → the disagreement quotes the OTHER side.
    conn.execute("ALTER TABLE documents ADD COLUMN source_quality_tier TEXT")
    conn.execute("UPDATE documents SET source_quality_tier = 'fmp_normalized' WHERE id = 1")
    # One unresolved cross-source disagreement on the newest revenue row drops
    # its score below the 0.8 affordance threshold.
    conn.execute("UPDATE financial_facts SET confidence = 0.79 WHERE period_end LIKE '2025-09-30%'")
    conn.execute(
        "CREATE TABLE validation_issues ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ticker TEXT, severity TEXT,"
        " rule TEXT, raw_value TEXT, raised_at TIMESTAMP, resolved_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, raised_at)"
        " VALUES ('r', 'TST', 'warn', 'source_disagreement',"
        " 'revenue @ 2025-09-30: fmp=149000000 vs sec_xbrl=150000000 (0.67%)', '2026-06-01')"
    )
    conn.commit()
    conn.close()


def test_low_confidence_and_disagreement_reach_the_model_text(repo: Path) -> None:
    """The newest revenue row is contested (conf 0.79, SEC disagrees). Both the
    score and the disagreement now ride in the text the model reads — verbatim
    from display_issues_for_fact, read from the FMP cell's perspective."""
    _enable_confidence_and_issues(repo / "data" / "portfolio.db")
    items = _gather(repo, "TST revenue trend")
    fin = next(i for i in items if i.label == "TST · Revenue")
    assert "[conf 79%; ⚠ SEC says $150M, 0.67% delta]" in fin.text
    # The series values still lead the text — enrichment is appended, not a swap.
    assert "150.0M" in fin.text
    assert fin.text.index("150.0M") < fin.text.index("[conf 79%")
    # The chip's structured confidence is unchanged (the % popover still works).
    assert fin.confidence == 0.79


def test_clean_high_confidence_fact_carries_no_caution_tag(repo: Path) -> None:
    """A fact at/above the threshold with no unresolved issue stays
    un-annotated — no prompt noise on solid facts. The KPI here is conf 1.0."""
    _enable_confidence_and_issues(repo / "data" / "portfolio.db")
    items = _gather(repo, "TST total customers and revenue trend")
    kpi = next(i for i in items if "Customers" in i.label)
    assert kpi.confidence == 1.0
    assert "conf" not in kpi.text
    assert "⚠" not in kpi.text


def test_evidence_block_instructs_the_model_to_discount_contested_figures(repo: Path) -> None:
    """build_evidence_block carries the rule that turns the annotation into
    behavior: don't assert a tagged figure as settled fact."""
    items = _gather(repo, "TST revenue trend")
    block = build_evidence_block(items)
    assert "WEIGH THE PROVENANCE" in block
    assert "settled fact" in block


def test_disagreement_without_displayed_tier_names_both_sides(repo: Path) -> None:
    """A legacy DB lacking source_quality_tier still surfaces the conflict —
    _doc_meta falls back to the 2-column query (tier None), so the model reads
    both sides rather than losing the warning entirely."""
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE financial_facts ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
    conn.execute("UPDATE financial_facts SET confidence = 0.79 WHERE period_end LIKE '2025-09-30%'")
    conn.execute(
        "CREATE TABLE validation_issues ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ticker TEXT, severity TEXT,"
        " rule TEXT, raw_value TEXT, raised_at TIMESTAMP, resolved_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, raised_at)"
        " VALUES ('r', 'TST', 'warn', 'source_disagreement',"
        " 'revenue @ 2025-09-30: fmp=149000000 vs sec_xbrl=150000000 (0.67%)', '2026-06-01')"
    )
    conn.commit()
    conn.close()
    fin = next(i for i in _gather(repo, "TST revenue trend") if i.label == "TST · Revenue")
    assert "FMP $149M vs SEC $150M (0.67% delta)" in fin.text


def test_ask_scopes_unresolved_issues_to_fact_evidence_tickers(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ask passes its bounded fact-ticker set to the issue loader instead of
    requesting a full-ledger parse before it retrieves the fact series."""
    captured_scopes: list[tuple[str, ...]] = []

    def _capture_issue_scope(
        _conn: sqlite3.Connection,
        *,
        tickers: Iterable[str] | None = None,
    ) -> IssuesByTicker:
        captured_scopes.append(tuple(tickers or ()))
        return {}

    monkeypatch.setattr("ask.grounding.load_unresolved_issues", _capture_issue_scope)
    _gather(repo, "TST revenue trend")

    assert captured_scopes == [("TST",)]


def test_enrichment_is_inert_without_a_validation_issues_table(repo: Path) -> None:
    """The fixture DDL predates validation_issues — load_unresolved_issues
    returns {} and the fact text is exactly the un-enriched series (no tag)."""
    items = _gather(repo, "TST revenue trend")
    fin = next(i for i in items if i.label == "TST · Revenue")
    assert "[" not in fin.text  # no bracketed caution tag at all
    assert "⚠" not in fin.text
