"""Tests for P3.3 source chips + evidence-drawer dead-citation fixes:
the per-cell provenance loader, the section/model plumbing, the chip
renderers, and the drawer's news_url links + brief-provenance loading.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import cast

from alerts import AlertRow
from dashboard.evidence_drawer import load_brief_provenance, render_evidence_drawer
from report.models import CellSource, QuarterlyEarningsCard, QuarterlyLineItem
from report.renderers.charts_v2 import MatrixRow, yoy_heatmap_table
from report.renderers.workspace_html import (
    _financial_highlights_panel,  # pyright: ignore[reportPrivateUsage]
    _source_chip_html,  # pyright: ignore[reportPrivateUsage]
    _source_for_display_index,  # pyright: ignore[reportPrivateUsage]
    _source_hover_title,  # pyright: ignore[reportPrivateUsage]
)
from report.sections.financials import (
    _to_cell_source,  # pyright: ignore[reportPrivateUsage]
)
from timeseries.loaders import load_financial_cell_provenance

# ----------------------------------------------------------------------------
# loader
# ----------------------------------------------------------------------------

_LOADER_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source_type TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL,
    raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized',
    accession_number TEXT,
    filing_date TEXT
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT,
    unit TEXT NOT NULL DEFAULT 'actual',
    source_doc_id INTEGER NOT NULL,
    locator TEXT
);
"""


def _seed_loader_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_LOADER_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_url, source_quality_tier, accession_number, "
        " filing_date) "
        "VALUES (1, 'TST', 'fmp', 'fmp_income_statement', 'f.json', 'a', "
        " '2026-01-05 10:00:00', 'ok', 'https://fmp.example/f.json', 'fmp_normalized', "
        " NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_url, source_quality_tier, accession_number, "
        " filing_date) "
        "VALUES (2, 'TST', 'sec_xbrl', 'sec_10q', 's.json#accn=0001-01-000001', 'b', "
        " '2026-02-01 10:00:00', 'ok', 'https://sec.example/s', 'sec_official', "
        " '0001-01-000001', '2026-01-30')"
    )
    rows = [
        # Same logical key from both documents — doc 2 (higher id) must win,
        # matching the metrics view's source_doc_id DESC pick.
        ("TST", "2025-12-31 00:00:00", "Q4", "revenue", "100", 1, '{"json_path":"[0].revenue"}'),
        (
            "TST",
            "2025-12-31 00:00:00",
            "Q4",
            "revenue",
            "101",
            2,
            '{"json_path":"facts.us-gaap.Revenues.units.USD[3]"}',
        ),
        ("TST", "2025-09-30 00:00:00", "Q3", "revenue", "90", 1, None),
        ("TST", "2025-12-31 00:00:00", "Q4", "net_income", "20", 1, '{"json_path":"[0].ni"}'),
    ]
    conn.executemany(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        " value, source_doc_id, locator) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(t, pe, fpt, li, v, d, loc) for t, pe, fpt, li, v, d, loc in rows],
    )
    conn.commit()
    conn.close()


def test_cell_provenance_loader_matches_metrics_view_pick(tmp_path: Path) -> None:
    db = tmp_path / "prov.db"
    _seed_loader_db(db)

    out = load_financial_cell_provenance("TST", ["revenue", "net_income"], db_path=db)
    q4 = out["revenue"]["2025-12-31"]
    assert q4["source"] == "sec_official"  # doc 2 wins (higher source_doc_id)
    assert q4["accession_number"] == "0001-01-000001"
    assert q4["filing_date"] == "2026-01-30"
    assert q4["source_url"] == "https://sec.example/s"
    assert q4["locator"] == '{"json_path":"facts.us-gaap.Revenues.units.USD[3]"}'

    q3 = out["revenue"]["2025-09-30"]
    assert q3["source"] == "fmp_normalized"
    assert q3["locator"] is None
    assert out["net_income"]["2025-12-31"]["doc_type"] == "fmp_income_statement"


def test_cell_provenance_loader_tolerates_legacy_schema(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, ticker TEXT, source_type TEXT, doc_type TEXT,
            file_path TEXT, sha256 TEXT, fetched_at TIMESTAMP, fetch_status TEXT,
            raw_bytes_size INTEGER DEFAULT 0, source_url TEXT
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY, ticker TEXT, period_end TIMESTAMP,
            fiscal_period_type TEXT, line_item TEXT, value TEXT, source_doc_id INTEGER
        );
        INSERT INTO documents VALUES
          (1, 'TST', 'fmp', 'fmp_income_statement', 'f.json', 'a',
           '2026-01-05 10:00:00', 'ok', 0, NULL);
        INSERT INTO financial_facts VALUES
          (1, 'TST', '2025-12-31 00:00:00', 'Q4', 'revenue', '100', 1);
        """
    )
    conn.commit()
    conn.close()

    out = load_financial_cell_provenance("TST", ["revenue"], db_path=db)
    prov = out["revenue"]["2025-12-31"]
    assert prov["source"] == "fmp"  # source_type fallback (no tier column)
    assert prov["locator"] is None
    assert prov["accession_number"] is None


def test_cell_provenance_loader_missing_db_returns_empty(tmp_path: Path) -> None:
    assert load_financial_cell_provenance("TST", ["revenue"], db_path=tmp_path / "no.db") == {}
    db = tmp_path / "empty_items.db"
    _seed_loader_db(db)
    assert load_financial_cell_provenance("TST", [], db_path=db) == {}


# ----------------------------------------------------------------------------
# section/model mapping
# ----------------------------------------------------------------------------


def test_to_cell_source_maps_payload() -> None:
    src = _to_cell_source(
        {
            "source": "sec_official",
            "fetched_at": "2026-02-01 10:00:00",
            "source_url": "https://sec.example/s",
            "doc_type": "sec_10q",
            "accession_number": "0001-01-000001",
            "filing_date": "2026-01-30",
            "locator": '{"json_path":"x"}',
            "fact_id": 7,
            "source_doc_id": 2,
        }
    )
    assert src is not None
    assert src.source == "sec_official"
    assert src.accession_number == "0001-01-000001"
    assert src.locator == '{"json_path":"x"}'
    # source_doc_id rides through so chips can deep-link /source/<id> (P4.3).
    assert src.doc_id == 2
    assert _to_cell_source(None) is None


def _line_item_with_sources() -> QuarterlyLineItem:
    full = [None, 100.0, 110.0, 120.0]
    srcs: list[CellSource | None] = [
        None,
        CellSource(source="fmp_normalized", fetched_at="2026-01-05 10:00:00"),
        None,
        CellSource(
            source="sec_official",
            fetched_at="2026-02-01 10:00:00",
            source_url="https://sec.example/s",
            accession_number="0001-01-000001",
        ),
    ]
    return QuarterlyLineItem(
        line_item="Revenue",
        unit="USD millions",
        quarters=["2025 Q3", "2025 Q4"],
        values=full[-2:],
        levels_full=full,
        sources_full=srcs,
    )


def test_source_for_display_index_translates_window() -> None:
    li = _line_item_with_sources()
    # values[0] == levels_full[2] (None source); values[1] == levels_full[3].
    assert _source_for_display_index(li, 0) is None
    s = _source_for_display_index(li, 1)
    assert s is not None
    assert s.source == "sec_official"
    assert _source_for_display_index(li, -1) is None
    assert _source_for_display_index(QuarterlyLineItem(line_item="x", unit=""), 0) is None


# ----------------------------------------------------------------------------
# chip rendering
# ----------------------------------------------------------------------------


def test_source_chip_html_contents_and_escaping() -> None:
    src = CellSource(
        source="sec_official",
        fetched_at="2026-02-01 10:00:00",
        source_url='https://sec.example/s?a=1&b="x"',
        doc_type="sec_10q",
        accession_number="0001-01-000001",
        filing_date="2026-01-30",
        locator='{"json_path":"facts.us-gaap.Revenues.units.USD[3]"}',
    )
    html_out = _source_chip_html(src)
    assert 'class="src-chip src-sec-official"' in html_out
    assert 'title="sec_official · fetched 2026-02-01"' in html_out
    assert ">SEC</summary>" in html_out
    assert "0001-01-000001" in html_out
    assert "filed 2026-01-30" in html_out
    assert "open source ↗" in html_out
    assert "&quot;x&quot;" in html_out  # URL is attribute-escaped
    assert _source_hover_title(CellSource(source="fmp_normalized")) == "fmp_normalized"


def test_source_chip_links_viewer_with_line_anchor() -> None:
    """P4.3: a doc-id-bearing chip deep-links the in-app /source reader; a
    transcript_line locator becomes the #L<n> anchor and the raw source_url
    demotes to the 'original document' link."""
    src = CellSource(
        source="llm_extracted",
        doc_id=5186,
        locator='{"transcript_line":42}',
        source_url="https://example.com/orig",
    )
    html_out = _source_chip_html(src)
    assert 'href="/source/5186#L42"' in html_out
    assert "open in viewer ↗" in html_out
    assert "original document ↗" in html_out


def test_source_chip_links_viewer_with_section_query() -> None:
    src = CellSource(
        source="sec_official",
        doc_id=31,
        locator='{"section":"item7_management_discussion"}',
    )
    html_out = _source_chip_html(src)
    assert 'href="/source/31?section=item7_management_discussion"' in html_out


def test_source_chip_without_doc_id_keeps_source_url_only() -> None:
    src = CellSource(source="fmp_normalized", source_url="https://fmp.example/x")
    html_out = _source_chip_html(src)
    assert "/source/" not in html_out
    assert "open source ↗" in html_out


def test_yoy_matrix_emits_cell_titles_and_label_chip() -> None:
    row = MatrixRow(
        name="Revenue",
        levels=[100.0, 105.0, 110.0, 115.0, 121.0],
        unit="USD millions",
        cell_titles=[None, None, None, None, "sec_official · fetched 2026-02-01"],
        label_suffix_html='<span class="src-chip">SEC</span>',
    )
    out = yoy_heatmap_table([row], [f"Q{i}" for i in range(5)], title="", display_quarters=2)
    assert 'title="sec_official · fetched 2026-02-01"' in out
    assert '<span class="src-chip">SEC</span>' in out


def test_financial_highlights_panel_renders_chip() -> None:
    li = _line_item_with_sources()
    card = QuarterlyEarningsCard(quarter="Q4", year=2025)
    body = StringIO()
    _financial_highlights_panel(body, card, [(li, 110.0, None)])
    out = body.getvalue()
    assert 'class="src-chip src-sec-official"' in out
    assert "open source ↗" in out


# ----------------------------------------------------------------------------
# evidence drawer: news_url links + brief-provenance loading
# ----------------------------------------------------------------------------


def _make_alert(evidence: str) -> AlertRow:
    return AlertRow(
        id=1,
        user_id="bhanu",
        ticker="GOOG",
        trigger_kind="material_news",
        fired_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        status="pending",
        memo_artifact_id=None,
        evidence_json=evidence,
        signature_sha="sig-test",
        dismissed_at=None,
        approved_at=None,
    )


def test_drawer_links_url_locators() -> None:
    evidence = json.dumps(
        {
            "summary": "s",
            "citations": [
                {"kind": "news_url", "locator": "https://example.com/a?b=1", "excerpt": "e"},
                {"kind": "transcript_line", "locator": "Q1-2026 · line 4", "excerpt": "t"},
            ],
        }
    )
    out = render_evidence_drawer(_make_alert(evidence))
    assert '<a href="https://example.com/a?b=1" target="_blank" rel="noopener">' in out
    assert "Q1-2026 · line 4</td>" in out  # non-URL locators stay plain text


def test_load_brief_provenance_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "prov.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE brief_provenance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR, generation_date VARCHAR(10), generated_at DATETIME,
            sources_used TEXT, sections_status TEXT, trigger VARCHAR(32),
            artifact_path VARCHAR
        );
        """
    )
    payload = {"per_metric": {"revenue_q_latest": {"source": "sec_official"}}}
    conn.execute(
        "INSERT INTO brief_provenance_log (ticker, generation_date, generated_at, sources_used) "
        "VALUES ('GOOG', '2026-06-01', '2026-06-01 10:00:00', ?)",
        (json.dumps(payload),),
    )
    conn.execute(
        "INSERT INTO brief_provenance_log (ticker, generation_date, generated_at, sources_used) "
        "VALUES ('GOOG', '2026-06-09', '2026-06-09 10:00:00', ?)",
        (json.dumps({"per_metric": {"revenue_q_latest": {"source": "fmp_normalized"}}}),),
    )
    conn.commit()
    conn.close()

    prov = load_brief_provenance("goog", db_path=db)
    assert prov is not None
    sources_used = prov["sources_used"]
    assert isinstance(sources_used, dict)
    per_metric = cast("dict[str, object]", sources_used)["per_metric"]
    # Latest generated_at row wins.
    assert per_metric == {"revenue_q_latest": {"source": "fmp_normalized"}}

    assert load_brief_provenance("MELI", db_path=db) is None
    assert load_brief_provenance("GOOG", db_path=tmp_path / "missing.db") is None


def test_load_brief_provenance_tolerates_missing_table(tmp_path: Path) -> None:
    db = tmp_path / "no_table.db"
    sqlite3.connect(db).close()
    assert load_brief_provenance("GOOG", db_path=db) is None


def test_drawer_resolves_fact_id_with_loaded_provenance(tmp_path: Path) -> None:
    evidence = json.dumps(
        {
            "summary": "s",
            "citations": [{"kind": "fact_id", "locator": "revenue_q_latest", "excerpt": "e"}],
        }
    )
    db = tmp_path / "prov.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE brief_provenance_log (id INTEGER PRIMARY KEY, ticker VARCHAR, "
        "generated_at DATETIME, sources_used TEXT);"
    )
    conn.execute(
        "INSERT INTO brief_provenance_log (ticker, generated_at, sources_used) "
        "VALUES ('GOOG', '2026-06-09 10:00:00', ?)",
        (
            json.dumps(
                {
                    "per_metric": {
                        "revenue_q_latest": {
                            "source": "sec_official",
                            "fetched_at": "2026-06-08 09:00:00",
                        }
                    }
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    prov = load_brief_provenance("GOOG", db_path=db)
    out = render_evidence_drawer(_make_alert(evidence), prov)
    assert "sec_official" in out
    assert "no brief provenance" not in out


# ----------------------------------------------------------------------------
# P4.3 — transcript citations deep-link the /source reader
# ----------------------------------------------------------------------------


def _seed_transcript_docs(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, ticker TEXT, doc_type TEXT, "
        "file_path TEXT);"
    )
    conn.executemany(
        "INSERT INTO documents (id, ticker, doc_type, file_path) VALUES (?, ?, ?, ?)",
        [
            (360, "NU", "earnings_call_transcript", "transcripts/processed/NU_Q1_2026.txt"),
            (361, "NU", "earnings_call_transcript", "transcripts/processed/NU_Q4_2025.txt"),
            (362, "NU", "ir_transcript", "ir_documents/NU/2026-03-31/ir_transcript__x.pdf"),
            (363, "NU", "earnings_call_transcript", "transcripts/processed/oddly_named.txt"),
        ],
    )
    conn.commit()
    conn.close()


def test_load_brief_provenance_carries_transcript_doc_map(tmp_path: Path) -> None:
    """Even without a brief_provenance_log table, the payload carries the
    period → transcript-doc map so transcript_line citations can link."""
    db = tmp_path / "docs.db"
    _seed_transcript_docs(db)
    prov = load_brief_provenance("nu", db_path=db)
    assert prov is not None
    assert prov.get("transcript_docs") == {"Q1-2026": 360, "Q4-2025": 361}


def test_drawer_links_transcript_citations_to_reader(tmp_path: Path) -> None:
    db = tmp_path / "docs.db"
    _seed_transcript_docs(db)
    evidence = json.dumps(
        {
            "summary": "tone shift",
            "shifts": [
                {
                    "citations": [
                        {
                            "kind": "transcript_line",
                            "period": "Q1-2026",
                            "line_number": 165,
                            "excerpt": "risk-adjusted NIM came in at 9.5%",
                        },
                        {
                            "kind": "transcript_line",
                            "period": "Q3-2024",  # no transcript on file
                            "line_number": 9,
                            "excerpt": "older quote",
                        },
                    ]
                }
            ],
        }
    )
    prov = load_brief_provenance("NU", db_path=db)
    out = render_evidence_drawer(_make_alert(evidence), prov)
    assert '<a href="/source/360#L165"' in out
    assert "Q1-2026 · line 165" in out
    # Unresolvable period stays plain text — no dead link fabricated.
    assert "/source/" not in out.split("Q3-2024")[1][:80]
