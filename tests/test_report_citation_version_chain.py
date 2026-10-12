"""VAL-CROSS-002(3): report citations resolve the full provenance chain.

A citation click in a generated report must resolve with 200 to the full
provenance chain — issuer, fiscal period, source document, source-bytes
sha256 + locator, Observation Version — wherever the cited document has an
``evidence_document_versions`` row, and every citation whose document lacks
one must carry the explicit "not recorded" degraded state instead of a
silently partial chain.

Covered surfaces (branch decision — route anchors to /source/version/<id>):
- the batched legacy-id → document-version resolver (report sections);
- the Sources tab citation anchors and the visible per-row state column;
- the synthesis lens cite-mark payloads and their popover degraded state
  (Python mirror + the JS mirror staying aligned);
- the fact-cell source chip popover (chain link or degraded state);
- the financials section chip stamping pass;
- the legacy ``/source/<doc_id>`` fallback view embedding the same chain
  renderer for versioned documents and stating the absence explicitly
  otherwise (covers already-built reports whose hrefs cannot be rewritten).

Everything here is hermetic: synthetic SQLite fixtures and migrated_db
schemas only. No bench/portfolio database access and no network.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import report.renderers.workspace_sections.sources as sources_renderer
import report.sections.financials as fin_section
import report.sections.synthesis as synth_section
from pipeline.source_viewers import (
    load_document_version_by_legacy_id,
    render_fallback_page,
)
from report.models import (
    AnnualKpiSeries,
    AnnualLineItem,
    AppendixSection,
    CellSource,
    KpiSeries,
    ProvenanceSection,
    QuarterlyLineItem,
    SectionStatus,
    SourceDocRow,
)
from report.sections._common import document_version_ids
from report.sections.provenance import build as build_provenance
from ui.cite_marks import CITE_MARKS_JS, linkify
from ui.source_chip import source_chip_html

# Private-section seams, reached via getattr per the repo's new-test convention
# (tests/test_financials_calendar_quarter.py): pyright-strict clean with no
# suppression directives, and the seam is pinned as an explicit typed callable.
_citations_for_source_docs: Callable[..., list[dict[str, Any]]] = getattr(
    synth_section, "_citations_for_source_docs"
)
_stamp_observation_versions: Callable[..., None] = getattr(
    fin_section, "_stamp_observation_versions"
)

# Minimal evidence-plane table: the resolver reads only these columns.
_VERSIONS_DDL = """
CREATE TABLE evidence_document_versions (
    document_version_id TEXT PRIMARY KEY,
    legacy_document_id INTEGER,
    version_sequence INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
"""

_DOCS_DDL = """
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
    accession_number TEXT,
    filing_date TEXT,
    period_end TEXT
);
"""


# ---------------------------------------------------------------------------
# 1. The batched resolver.
# ---------------------------------------------------------------------------


def _versions_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(_VERSIONS_DDL)
    conn.executemany(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?)",
        [
            ("legacy-doc-42", 42, 1, "2026-05-01 10:00:00"),
            ("legacy-doc-43", 43, 1, "2026-05-02 10:00:00"),
            # Two versions bound to doc 44 — the superseding v2 answers.
            ("legacy-doc-44-v1", 44, 1, "2026-05-03 10:00:00"),
            ("legacy-doc-44-v2", 44, 2, "2026-06-03 10:00:00"),
        ],
    )
    return conn


def test_document_version_ids_resolves_and_picks_latest() -> None:
    resolved = document_version_ids(_versions_conn(), [42, 43, 44, 99])
    assert resolved == {42: "legacy-doc-42", 43: "legacy-doc-43", 44: "legacy-doc-44-v2"}


def test_document_version_ids_fail_closed_without_evidence_plane() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    assert document_version_ids(conn, [42]) == {}
    assert document_version_ids(_versions_conn(), []) == {}


def test_document_version_ids_tolerates_query_error() -> None:
    conn = sqlite3.connect(":memory:")
    # Table exists with the wrong shape → sqlite3.Error → {} (fail closed).
    conn.execute("CREATE TABLE evidence_document_versions (other TEXT)")
    assert document_version_ids(conn, [42]) == {}


# ---------------------------------------------------------------------------
# 2. Provenance section: SourceDocRow carries the Observation Version.
# ---------------------------------------------------------------------------


def _seed_provenance_repo(repo: Path) -> None:
    db = repo / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_DOCS_DDL)
    conn.execute(_VERSIONS_DDL)
    conn.executemany(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, period_end)"
        " VALUES ('NU', 'fmp', ?, ?, ?, '2026-05-19 10:00:00', 'ok', NULL, ?)",
        [
            (
                "fmp_analyst_estimates",
                "data/historical/fmp/NU_analyst_estimates.json",
                "a",
                "2029-12-31",
            ),
            (
                "fmp_income_statement",
                "data/historical/fmp/NU_income_statement.json",
                "b",
                "2026-03-31",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES ('legacy-doc-1', 1, 1, '2026-05-19 10:00:00')"
    )
    conn.commit()
    conn.close()


def test_provenance_source_docs_carry_observation_versions(tmp_path: Path) -> None:
    _seed_provenance_repo(tmp_path)
    section = build_provenance("NU", tmp_path)
    assert section.status == SectionStatus.OK
    by_doc_id = {row.doc_id: row for row in section.source_docs}
    assert by_doc_id[1].document_version_id == "legacy-doc-1"
    # Doc 2 has no Observation Version → explicit None, never a silent default.
    assert by_doc_id[2].document_version_id is None


# ---------------------------------------------------------------------------
# 3. Sources tab renderer: routed anchors + the visible state column.
# ---------------------------------------------------------------------------


def _render_sources_tab(rows: list[SourceDocRow]) -> str:
    prov = ProvenanceSection(
        status=SectionStatus.OK,
        source_docs=rows,
    )
    app = AppendixSection(status=SectionStatus.OK)
    body = StringIO()
    sources_renderer._sources_tab(body, prov, app, "/nonexistent-repo-root")
    return body.getvalue()


def test_sources_tab_routes_versioned_citation_to_the_chain() -> None:
    html = _render_sources_tab(
        [
            SourceDocRow(
                doc_type="fmp_analyst_estimates",
                period_end="2029-12-31",
                file_path="data/historical/fmp/NU_analyst_estimates.json",
                fetched_at="2026-05-19T10:00:00",
                doc_id=3350,
                document_version_id="legacy-doc-3350",
            )
        ]
    )
    assert 'href="/source/version/legacy-doc-3350"' in html
    assert 'data-server-path="/source/version/legacy-doc-3350"' in html
    assert 'href="/source/3350"' not in html
    assert "sortBy('obsver','text')" in html  # the visible state column is sortable
    assert '<span class="mono">legacy-doc-3350</span>' in html
    assert "not recorded" not in html


def test_sources_tab_keeps_legacy_link_with_explicit_degraded_state() -> None:
    html = _render_sources_tab(
        [
            SourceDocRow(
                doc_type="llm_summary",
                period_end="2026-03-31",
                file_path="output/llm/NU_summary.json",
                fetched_at="2026-04-01T10:00:00",
                doc_id=6827,
            )
        ]
    )
    assert 'href="/source/6827"' in html
    assert 'data-server-path="/source/6827"' in html
    assert "/source/version/" not in html
    # The degraded state is visible in the row AND on the anchor.
    assert ">not recorded</td>" in html
    assert 'title="No observation version recorded for this document' in html


def test_sources_tab_legacy_row_without_doc_id_stays_plain() -> None:
    html = _render_sources_tab(
        [SourceDocRow(doc_type="transcript", file_path="transcripts/NU_Q1.txt")]
    )
    assert ">transcripts/NU_Q1.txt</td>" in html
    assert "<a " not in html.split("<tbody>")[1].split("</tbody>")[0]
    assert ">—</td>" in html  # the Obs. version cell for a legacy row


# ---------------------------------------------------------------------------
# 4. Synthesis lens cite-marks: routed href + degraded payload flag.
# ---------------------------------------------------------------------------


def _synth_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_type TEXT, "
        "period_end TEXT, source_url TEXT)"
    )
    conn.execute("INSERT INTO documents VALUES (42, '10-K', '2024-12-31', 'https://sec.gov/x')")
    conn.execute("INSERT INTO documents VALUES (43, '10-Q', '2025-09-30', NULL)")
    conn.execute(_VERSIONS_DDL)
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES ('legacy-doc-42', 42, 1, '2026-05-01')"
    )
    return conn


def test_citation_resolver_routes_versioned_docs_and_flags_the_rest() -> None:
    items = _citations_for_source_docs(_synth_conn(), [42, 43, 99], ticker="NU")
    assert items[0]["href"] == "/source/version/legacy-doc-42"
    assert items[0]["document_version_id"] == "legacy-doc-42"
    assert "no_observation_version" not in items[0]
    # No Observation Version → legacy href + explicit degraded flag.
    assert items[1]["href"] == "/source/43"
    assert items[1]["document_version_id"] is None
    assert items[1]["no_observation_version"] is True
    # A vanished document row is degraded the same honest way.
    assert items[2]["href"] == "/source/99"
    assert items[2]["no_observation_version"] is True


def test_cite_mark_popover_renders_the_degraded_state_in_both_mirrors() -> None:
    payload = [
        {
            "n": 1,
            "kind": "10-Q",
            "label": "10-Q · 2025-09-30",
            "href": "/source/43",
            "no_observation_version": True,
        }
    ]
    html = linkify("Management guided up [1].", payload)
    assert 'href="/source/43"' in html
    assert "no observation version recorded" in html
    # The JS mirror must render the same phrase (byte-aligned semantics).
    assert "no observation version recorded" in CITE_MARKS_JS
    assert "no_observation_version" in CITE_MARKS_JS


def test_cite_mark_linkify_keeps_version_href() -> None:
    payload = [{"n": 1, "kind": "10-K", "label": "10-K", "href": "/source/version/legacy-doc-42"}]
    html = linkify("Revenue grew [1].", payload)
    assert 'href="/source/version/legacy-doc-42"' in html


# ---------------------------------------------------------------------------
# 5. Fact-cell source chip popover: chain link or degraded state.
# ---------------------------------------------------------------------------


def test_chip_popover_links_the_chain_when_a_version_exists() -> None:
    html = source_chip_html(
        CellSource(
            source="fmp_normalized",
            doc_type="fmp_analyst_estimates",
            doc_id=3350,
            document_version_id="legacy-doc-3350",
        )
    )
    assert "observation version legacy-doc-3350" in html
    assert 'href="/source/version/legacy-doc-3350"' in html
    assert "provenance chain ↗" in html
    assert "not recorded" not in html


def test_chip_popover_states_the_degraded_version_explicitly() -> None:
    html = source_chip_html(CellSource(source="llm_extracted", doc_type="llm_summary", doc_id=6827))
    assert "observation version not recorded" in html
    assert "/source/version/" not in html
    assert "provenance chain" not in html


def test_chip_popover_without_a_document_id_has_no_version_row() -> None:
    html = source_chip_html(CellSource(source="fmp_normalized"))
    assert "observation version" not in html


# ---------------------------------------------------------------------------
# 6. Financials section: the stamping pass over every chip-bearing field.
# ---------------------------------------------------------------------------


def _chip(doc_id: int | None) -> CellSource:
    return CellSource(source="fmp_normalized", doc_type="fmp_income_statement", doc_id=doc_id)


def test_financials_stamp_covers_all_chip_fields() -> None:
    conn = _versions_conn()
    quarterly = [
        QuarterlyLineItem(
            line_item="revenue", unit="USD millions", sources_full=[_chip(42), _chip(99)]
        )
    ]
    annual = [AnnualLineItem(line_item="revenue", unit="USD millions", sources_full=[_chip(43)])]
    kpis = [KpiSeries(name="GMV", unit="USD bn", sources_full=[_chip(None), _chip(42)])]
    annual_kpis = [AnnualKpiSeries(name="GMV", unit="USD bn", sources_full=[_chip(44)])]
    fin_stamp = _stamp_observation_versions
    fin_stamp(
        conn,
        line_items=quarterly,
        annual_items=annual,
        kpi_series=kpis,
        annual_kpi_series=annual_kpis,
    )
    q0 = quarterly[0].sources_full[0]
    q1 = quarterly[0].sources_full[1]
    a0 = annual[0].sources_full[0]
    k0 = kpis[0].sources_full[0]
    k1 = kpis[0].sources_full[1]
    ak0 = annual_kpis[0].sources_full[0]
    assert q0 is not None and q0.document_version_id == "legacy-doc-42"
    assert q1 is not None and q1.document_version_id is None
    assert a0 is not None and a0.document_version_id == "legacy-doc-43"
    assert k0 is not None and k0.document_version_id is None
    assert k1 is not None and k1.document_version_id == "legacy-doc-42"
    # Superseding version answers for the document.
    assert ak0 is not None and ak0.document_version_id == "legacy-doc-44-v2"


def test_financials_stamp_is_a_noop_without_chips() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    _stamp_observation_versions(
        conn, line_items=[], annual_items=[], kpi_series=[], annual_kpi_series=[]
    )


# ---------------------------------------------------------------------------
# 7. Legacy /source/<doc_id> fallback view: same chain renderer, or the
#    explicit degraded state (covers already-built reports).
# ---------------------------------------------------------------------------


# The full column set the by-legacy-id chain loader projects, plus the
# joined evidence tables it reads (seeded empty where unused).
_FALLBACK_EVIDENCE_DDL = """
CREATE TABLE evidence_source_observations (
    observation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    source_published_at TEXT,
    filing_at TEXT,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    retrieval_config_sha256 TEXT NOT NULL,
    collector_code_version TEXT NOT NULL
);
CREATE TABLE evidence_document_versions (
    document_version_id TEXT PRIMARY KEY,
    version_sequence INTEGER NOT NULL,
    issuer_id TEXT NOT NULL,
    ticker TEXT,
    document_type TEXT NOT NULL,
    form_type TEXT NOT NULL,
    accession_number TEXT,
    exhibit_id TEXT,
    period_start TEXT,
    period_end TEXT,
    as_of_at TEXT,
    language TEXT NOT NULL,
    replaces_document_version_id TEXT,
    legacy_document_id INTEGER,
    recorded_at TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    observation_id TEXT NOT NULL
);
CREATE TABLE evidence_extraction_runs (
    extraction_run_id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL
);
CREATE TABLE evidence_nodes (
    node_id TEXT PRIMARY KEY,
    extraction_run_id TEXT NOT NULL,
    node_kind TEXT NOT NULL,
    text TEXT NOT NULL,
    locator_json TEXT
);
CREATE TABLE search_chunks (
    chunk_id TEXT PRIMARY KEY,
    evidence_node_id TEXT NOT NULL,
    text TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL
);
CREATE TABLE canonical_fact_projection_entries (
    canonical_metric_cell_id TEXT PRIMARY KEY,
    canonical_metric_name TEXT NOT NULL,
    canonical_value TEXT,
    unit_key TEXT,
    currency TEXT,
    period_kind TEXT,
    period_start TEXT,
    period_end TEXT,
    evidence_node_id TEXT NOT NULL,
    evidence_locator_json TEXT,
    evidence_document_version_id TEXT NOT NULL,
    change_kind TEXT NOT NULL
);
"""

_LEGACY_DOC_ID = 3350
_LEGACY_VERSION_ID = "legacy-doc-3350"
_FALLBACK_OBSERVATION_ID = "obs-fallback-3350"
_FALLBACK_TICKER = "NU"
_FALLBACK_DOC_TYPE = "fmp_analyst_estimates"
_FALLBACK_BLOB = "a" * 64


def _seed_fallback_db(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_DOCS_DDL)
    conn.executescript(_FALLBACK_EVIDENCE_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, accession_number, filing_date, period_end)"
        " VALUES (?, ?, 'fmp', ?, ?, ?, '2026-05-19 10:00:00', 'ok', NULL, NULL, NULL, ?)",
        (
            _LEGACY_DOC_ID,
            _FALLBACK_TICKER,
            _FALLBACK_DOC_TYPE,
            "data/historical/fmp/NU_analyst_estimates_annual.json",
            _FALLBACK_BLOB,
            "2029-12-31 00:00:00",
        ),
    )
    # A second document with no Observation Version (degraded-state case).
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status, source_url, accession_number, filing_date, period_end)"
        " VALUES (2, ?, 'llm', 'llm_summary', 'output/llm/NU.json', ?,"
        " '2026-04-01 10:00:00', 'ok', NULL, NULL, NULL, '2026-03-31')",
        (_FALLBACK_TICKER, "b" * 64),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            _FALLBACK_OBSERVATION_ID,
            "idem-obs-fallback",
            "fmp",
            "file:///data/historical/fmp/NU_analyst_estimates_annual.json",
            _FALLBACK_BLOB,
            None,
            None,
            "2026-05-19 10:00:00",
            "2026-05-19 10:00:00",
            "c" * 64,
            "fixture-collector.v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _LEGACY_VERSION_ID,
            1,
            "legacy-ticker:NU",
            _FALLBACK_TICKER,
            _FALLBACK_DOC_TYPE,
            _FALLBACK_DOC_TYPE,
            None,
            None,
            None,
            "2029-12-31 00:00:00",
            None,
            "und",
            None,
            _LEGACY_DOC_ID,
            "2026-05-19 10:00:00",
            _FALLBACK_BLOB,
            _FALLBACK_OBSERVATION_ID,
        ),
    )
    conn.commit()
    conn.close()


def test_fallback_view_embeds_the_full_chain_for_a_versioned_document(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _seed_fallback_db(db)
    html = render_fallback_page(db, _LEGACY_DOC_ID)
    assert html is not None
    # The same chain rows the /source/version/<id> route serves.
    assert f"version {_LEGACY_VERSION_ID}" in html
    assert "fiscal period" in html
    assert "source bytes" in html
    assert "observation version" in html
    assert "sha256 " + "a" * 64 in html
    assert _FALLBACK_DOC_TYPE in html
    # ...and the legacy metadata is still there.
    assert "No in-app viewer for this document type." in html


def test_fallback_view_states_the_degraded_chain_for_versionless_documents(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _seed_fallback_db(db)
    html = render_fallback_page(db, 2)
    assert html is not None
    assert "not recorded for this document" in html
    assert "full provenance chain unavailable" in html
    assert "source bytes" not in html


def test_load_document_version_by_legacy_id_resolves_the_chain(tmp_path: Path) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _seed_fallback_db(db)
    view = load_document_version_by_legacy_id(db, _LEGACY_DOC_ID)
    assert view is not None
    assert view.document_version_id == _LEGACY_VERSION_ID
    assert view.legacy_document_id == _LEGACY_DOC_ID
    assert view.ticker == _FALLBACK_TICKER
    # Unknown legacy id → None (honest 404 upstream, never a fabricated chain).
    assert load_document_version_by_legacy_id(db, 999) is None


def test_load_document_version_by_legacy_id_fail_closed_without_evidence_plane(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_DOCS_DDL)
    conn.execute(
        "INSERT INTO documents (ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES ('NU', 'fmp', 'fmp_other', 'x.json', 'a',"
        " '2026-05-19 10:00:00', 'ok')"
    )
    conn.commit()
    conn.close()
    assert load_document_version_by_legacy_id(db, 1) is None


@pytest.mark.parametrize("fragment", [False, True])
def test_fallback_view_fragment_mode_keeps_the_chain_state(tmp_path: Path, fragment: bool) -> None:
    db = tmp_path / "data" / "portfolio.db"
    _seed_fallback_db(db)
    html = render_fallback_page(db, _LEGACY_DOC_ID, fragment=fragment)
    assert html is not None
    assert f"version {_LEGACY_VERSION_ID}" in html
    degraded = render_fallback_page(db, 2, fragment=fragment)
    assert degraded is not None
    assert "not recorded for this document" in degraded
