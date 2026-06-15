"""Tests for the Stage-A capture-all XBRL walker + the persist_manifest capture seam.

Covers (1) the pure parse/classify/name/period helpers in
``table_extractors.generic_xbrl_capture`` against synthetic FMP-shaped sections,
(2) the ``persist_manifest(origin=CAPTURE)`` seam (canonicalize + stamp origin)
in ``pipeline.kpi_persistence``, and (3) a self-contained end-to-end ``extract``
against a hand-rolled file DB. The real migrated-schema end-to-end check is the
pilot run (see the program directive).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import SourceType  # noqa: E402
from models.facts import FiscalPeriodType, Unit  # noqa: E402
from models.kpis import DefinitionOrigin  # noqa: E402
from pipeline.kpi_persistence import (  # noqa: E402
    KpiExtractionManifest,
    KpiValue,
    persist_manifest,
)
from table_extractors import generic_xbrl_capture as g  # noqa: E402

# ---------------------------------------------------------------------------
# Synthetic FMP form_10k payload — mirrors the real section/period shapes.
# ---------------------------------------------------------------------------


def _payload() -> dict[str, object]:
    return {
        "symbol": "X",
        "period": "FY",
        "year": "2024",
        # Clean monetary detail table: capture the $ rows, defer the stray rate
        # + count rows that share the section.
        "Debt - Long-Term": [
            {
                "Debt - Long-Term Debt (Details) - USD ($) $ in Millions": [
                    "Dec. 31, 2024",
                    "Dec. 31, 2023",
                ]
            },
            {"Total long-term debt": [10883, 11870]},
            {"Coupon Rate": [0.0338, 0.04]},  # rate token -> defer
            {"Number of notes outstanding": [12, 13]},  # count token -> defer
        ],
        # Primary statement (no '(Details)') -> already in financial_facts -> skip.
        "CONSOLIDATED BALANCE SHEETS": [
            {
                "CONSOLIDATED BALANCE SHEETS - USD ($) $ in Millions": [
                    "Dec. 31, 2024",
                    "Dec. 31, 2023",
                ]
            },
            {"Cash and cash equivalents": [23466, 24048]},
        ],
        # Per-share-mixed section ('$ / shares') -> deferred wholesale to Stage B.
        "EPS - Sched": [
            {
                "Net Income Per Share - Schedule (Details) - USD ($) $ / shares in Units, $ in Millions": [
                    "Dec. 31, 2024"
                ]
            },
            {"Net income": [100118]},
            {"Basic net income per share (in dollars per share)": [8.13]},
        ],
        # FYE column + an off-cycle (Sep-30) comparative column -> only FYE kept.
        "Goodwill - Roll": [
            {
                "Goodwill - Changes (Details) - USD ($) $ in Millions": [
                    "Dec. 31, 2024",
                    "Sep. 30, 2024",
                ]
            },
            {"Google Cloud": ["\xa0", "\xa0"]},  # axis marker
            {"Goodwill, End of Period": [31885, 30000]},
        ],
        # A stray sub-unit ratio that slipped into a $-scaled section -> defer.
        "Cov - Detail": [
            {"Coverage (Details) - USD ($) $ in Millions": ["Dec. 31, 2024"]},
            {"Some fractional coverage": [0.5]},
        ],
        # A clean section carrying a per-share dollar row (NOT '/ shares' titled):
        # the per-share row is captured WITHOUT the $-scale; the aggregate is scaled.
        "Div - Detail": [
            {"Dividends (Details) - USD ($) $ in Millions": ["Dec. 31, 2024"]},
            {"Dividends declared per share": [0.80]},
            {"Total dividends": [7363]},
        ],
    }


def _walk_all(payload: dict[str, object]) -> tuple[dict[datetime, list[KpiValue]], g.CaptureAudit]:
    fye = g._detect_fye_monthday(payload)  # pyright: ignore[reportPrivateUsage]
    per: dict[datetime, list[KpiValue]] = defaultdict(list)
    audit = g.CaptureAudit()
    for key, section in g._iter_sections(payload):  # pyright: ignore[reportPrivateUsage]
        audit.sections_seen += 1
        g._walk_section(key, section, per, audit, fye)  # pyright: ignore[reportPrivateUsage]
    return per, audit


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_parse_period_formats() -> None:
    assert g._parse_period("Dec. 31, 2024") == datetime(2024, 12, 31)  # pyright: ignore[reportPrivateUsage]
    assert g._parse_period("Jan. 31, 2025") == datetime(2025, 1, 31)  # pyright: ignore[reportPrivateUsage]
    assert g._parse_period("12 Months Ended Dec. 31, 2024") == datetime(2024, 12, 31)  # pyright: ignore[reportPrivateUsage]
    # Non-date column headers (fair-value levels, members) are not periods.
    assert g._parse_period("Level 1") is None  # pyright: ignore[reportPrivateUsage]
    assert g._parse_period("") is None  # pyright: ignore[reportPrivateUsage]


def test_detect_fye_monthday_is_modal() -> None:
    # Dec-31 dominates (Debt x2, Goodwill, Cov, Div); Sep-30 appears once.
    assert g._detect_fye_monthday(_payload()) == (12, 31)  # pyright: ignore[reportPrivateUsage]


def test_accept_period_drops_off_cycle() -> None:
    sep = datetime(2024, 9, 30)
    dec = datetime(2024, 12, 31)
    assert g._accept_period(sep, (12, 31)) is None  # pyright: ignore[reportPrivateUsage]
    assert g._accept_period(dec, (12, 31)) == dec  # pyright: ignore[reportPrivateUsage]
    # No FYE detected → accept any parsed date (don't drop everything).
    assert g._accept_period(sep, None) == sep  # pyright: ignore[reportPrivateUsage]


def test_is_detail_section() -> None:
    assert g._is_detail_section("Debt - Long-Term Debt (Details) - USD ($) $ in Millions")  # pyright: ignore[reportPrivateUsage]
    assert not g._is_detail_section("CONSOLIDATED BALANCE SHEETS - USD ($) $ in Millions")  # pyright: ignore[reportPrivateUsage]
    # Non-financial denylist, even with '(Details)'.
    assert not g._is_detail_section("Insider Trading Arrangements (Details)")  # pyright: ignore[reportPrivateUsage]


def test_is_unit_ambiguous_section() -> None:
    amb = g._is_unit_ambiguous_section  # pyright: ignore[reportPrivateUsage]
    # Equity / share-rollforward family — FMP mislabels share counts as "$ in
    # Thousands" (NU's "Equity (Details)" lists billions of *shares*), so defer.
    assert amb("Equity (Details) - USD ($) $ in Thousands")
    assert amb("Consolidated Statements of Changes in Equity - USD ($) $ in Thousands")
    assert amb("Stockholders' Equity - Narrative (Details) - USD ($) $ in Millions")
    # A declared share-count / per-share scale is unit-mixed too.
    assert amb("Statement (Details) - USD ($) shares in Millions, $ in Millions")
    assert amb("Net Income Per Share (Details) - USD ($) $ / shares in Units, $ in Millions")
    # NOT ambiguous: ordinary $ tables — including "equity method/securities"
    # (which merely START with 'equity' but are plain monetary tables).
    assert not amb("Equity Method Investments (Details) - USD ($) $ in Millions")
    assert not amb("Equity Securities (Details) - USD ($) $ in Millions")
    assert not amb("Debt - Long-Term Debt (Details) - USD ($) $ in Millions")


def test_semantic_section_title_strips_unit_and_details() -> None:
    title = "Goodwill - Changes in Carrying Amount of Goodwill (Details) - USD ($) $ in Millions"
    assert (
        g._semantic_section_title(title)  # pyright: ignore[reportPrivateUsage]
        == "Goodwill - Changes in Carrying Amount of Goodwill"
    )


def test_build_name_qualifies_and_folds() -> None:
    name = g._build_name("Goodwill - Changes", ["Google Cloud"], "Additions")  # pyright: ignore[reportPrivateUsage]
    assert name == "Goodwill - Changes — Google Cloud — Additions"
    # XBRL taxonomy boilerplate rows are dropped from the name.
    assert (
        g._build_name("Goodwill", ["Goodwill [Roll Forward]"], "Additions")  # pyright: ignore[reportPrivateUsage]
        == "Goodwill — Additions"
    )
    # Consecutive duplicate parts fold (a label echoing its axis).
    assert g._build_name("S", ["Total"], "total") == "S — Total"  # pyright: ignore[reportPrivateUsage]
    # Clipped to the column width.
    assert len(g._build_name("x" * 300, [], "y")) <= 200  # pyright: ignore[reportPrivateUsage]


def test_classify_value_monetary_is_scaled() -> None:
    val, reason = g._classify_value("Total long-term debt", 10883, 1_000_000)  # pyright: ignore[reportPrivateUsage]
    assert reason == "" and val == Decimal("10883000000")


def test_classify_value_rate_deferred() -> None:
    val, reason = g._classify_value("Coupon Rate", 0.0338, 1_000_000)  # pyright: ignore[reportPrivateUsage]
    assert val is None and reason == "rate_or_percent"


def test_classify_value_count_deferred() -> None:
    val, reason = g._classify_value("Number of notes outstanding", 12, 1_000_000)  # pyright: ignore[reportPrivateUsage]
    assert val is None and reason == "share_or_count"


def test_classify_value_per_share_not_scaled() -> None:
    val, reason = g._classify_value("Dividends declared per share", 0.80, 1_000_000)  # pyright: ignore[reportPrivateUsage]
    assert reason == "" and val == Decimal("0.80")  # NOT 800,000


def test_classify_value_subunit_deferred() -> None:
    # A <1 magnitude in a millions section is a rate that slipped the label net.
    val, reason = g._classify_value("Some fractional coverage", 0.5, 1_000_000)  # pyright: ignore[reportPrivateUsage]
    assert val is None and reason == "subunit_magnitude"


# ---------------------------------------------------------------------------
# Whole-section walk
# ---------------------------------------------------------------------------


def test_walk_captures_expected_cells() -> None:
    per, _audit = _walk_all(_payload())
    by_name_2024 = {kv.name: kv.value for kv in per[datetime(2024, 12, 31)]}
    assert by_name_2024 == {
        "Debt - Long-Term Debt — Total long-term debt": Decimal("10883000000"),
        "Goodwill - Changes — Google Cloud — Goodwill, End of Period": Decimal("31885000000"),
        "Dividends — Dividends declared per share": Decimal("0.80"),
        "Dividends — Total dividends": Decimal("7363000000"),
    }
    # The prior-year FYE column is captured too (multi-year history in one pass).
    by_name_2023 = {kv.name: kv.value for kv in per[datetime(2023, 12, 31)]}
    assert by_name_2023 == {
        "Debt - Long-Term Debt — Total long-term debt": Decimal("11870000000"),
    }
    # No off-cycle (Sep-30) period was created.
    assert datetime(2024, 9, 30) not in per


def test_walk_audit_accounts_for_every_skip() -> None:
    per, audit = _walk_all(_payload())
    # 4 detail sections walked (Debt, Goodwill, Cov, Div); EPS per-share-mixed and
    # the balance sheet (no '(Details)') are not counted as detail.
    assert audit.sections_detail == 4
    assert audit.skipped.get("section_unit_ambiguous") == 1
    # The Debt section has two period columns, so its rate + count rows skip 2 cells each.
    assert audit.skipped.get("rate_or_percent") == 2
    assert audit.skipped.get("share_or_count") == 2
    assert audit.skipped.get("subunit_magnitude") == 1
    assert audit.skipped.get("off_cycle_or_unparseable_period") == 1
    # Every cell SEEN is either captured or skipped for a cell-level reason —
    # nothing silently dropped. ('section_'-prefixed reasons defer whole sections
    # before their cells are counted, so they're excluded from the cell tally.)
    captured = sum(len(v) for v in per.values())
    cell_skips = sum(v for k, v in audit.skipped.items() if not k.startswith("section_"))
    assert captured + cell_skips == audit.cells_seen


# ---------------------------------------------------------------------------
# persist_manifest capture seam
# ---------------------------------------------------------------------------

_PROVENANCE_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_provenance "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)"
)


def _capture_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
            threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
            definition_origin TEXT NOT NULL DEFAULT 'analyst',
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0, extracted_by TEXT,
            locator TEXT, source_excerpt TEXT, supersedes_id INTEGER
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        """
    )
    c.execute(_PROVENANCE_UNIQUE)
    c.commit()
    return c


def _capture_manifest(values: list[KpiValue], origin: DefinitionOrigin) -> KpiExtractionManifest:
    return KpiExtractionManifest(
        ticker="X",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.FY,
        source_doc_id=1,
        primary_source=SourceType.SEC_XBRL,
        extracted_by="capture_xbrl_v1",
        origin=origin,
        values=values,
    )


def _origins(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(r["name"]): str(r["definition_origin"])
        for r in conn.execute("SELECT name, definition_origin FROM kpi_definitions").fetchall()
    }


def test_capture_mode_canonicalizes_onto_existing_analyst_def() -> None:
    """A CAPTURE label that's a unit/casing variant of an existing analyst metric
    collapses onto it — no near-duplicate minted, and the analyst origin stands."""
    conn = _capture_db()
    conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, definition_origin) "
        "VALUES ('X', 'GMV', 'millions', 'sec_xbrl', 'analyst')"
    )
    conn.commit()
    res = persist_manifest(
        conn,
        run_id="r1",
        manifest=_capture_manifest(
            [KpiValue(name="GMV (USD)", value=Decimal("5"), unit=Unit.MILLIONS)],
            DefinitionOrigin.CAPTURE,
        ),
    )
    assert res.inserted == 1
    assert _origins(conn) == {"GMV": "analyst"}  # collapsed onto the analyst row


def test_capture_mode_mints_new_capture_origin() -> None:
    conn = _capture_db()
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_capture_manifest(
            [KpiValue(name="Brand New Long-Tail Metric", value=Decimal("9"), unit=Unit.ACTUAL)],
            DefinitionOrigin.CAPTURE,
        ),
    )
    assert _origins(conn) == {"Brand New Long-Tail Metric": "capture"}


def test_analyst_mode_stores_name_verbatim() -> None:
    """Default (ANALYST) origin must NOT canonicalize — the curated watchlist name
    is stored exactly as given, so the existing pipeline is untouched."""
    conn = _capture_db()
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_capture_manifest(
            [KpiValue(name="Monthly ARPAC (USD)", value=Decimal("11"), unit=Unit.ACTUAL)],
            DefinitionOrigin.ANALYST,
        ),
    )
    assert _origins(conn) == {"Monthly ARPAC (USD)": "analyst"}


def test_capture_within_batch_dedup() -> None:
    """Two unit/casing variants in ONE capture manifest collapse to a single
    definition (the resolve sees the row minted earlier in the same batch)."""
    conn = _capture_db()
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_capture_manifest(
            [
                KpiValue(name="Total bookings", value=Decimal("5"), unit=Unit.MILLIONS),
                KpiValue(name="total   bookings (USD)", value=Decimal("5"), unit=Unit.MILLIONS),
            ],
            DefinitionOrigin.CAPTURE,
        ),
    )
    rows = conn.execute("SELECT COUNT(*) FROM kpi_definitions").fetchone()
    assert rows[0] == 1


# ---------------------------------------------------------------------------
# End-to-end extract() against a hand-rolled file DB
# ---------------------------------------------------------------------------


def test_extract_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
            primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT,
            threshold_tier TEXT, threshold_low FLOAT, threshold_high FLOAT, notes TEXT,
            definition_origin TEXT NOT NULL DEFAULT 'analyst',
            UNIQUE(ticker, name)
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0, extracted_by TEXT,
            locator TEXT, source_excerpt TEXT, supersedes_id INTEGER
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            source_doc_id INTEGER, ticker TEXT, severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT, raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        CREATE TABLE ingestion_runs (
            run_id TEXT PRIMARY KEY, started_at TIMESTAMP, ended_at TIMESTAMP,
            directive TEXT, ticker_scope TEXT, status TEXT, error_summary TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, doc_type TEXT, file_path TEXT
        );
        CREATE UNIQUE INDEX uq_kpi_facts_provenance
            ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);
        """
    )
    conn.execute(
        "INSERT INTO documents (id, ticker, doc_type, file_path) "
        "VALUES (77, 'X', 'fmp_10k_json', 'data/historical/fmp/X_form_10k_2024.json')"
    )
    conn.commit()
    conn.close()

    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True)
    (fmp_dir / "X_form_10k_2024.json").write_text(json.dumps(_payload()), encoding="utf-8")

    outcome = g.extract(
        ticker="X",
        fiscal_year=2024,
        fmp_payload=_payload(),
        sec_text=None,
        db_path=db,
        repo_root=tmp_path,
        filing_doc_id=None,  # forces the file_path-first re-resolve
    )
    assert outcome.status == "ok"
    assert outcome.n_rows_inserted > 0

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = {
            str(r["name"]): (
                Decimal(str(r["value"])),
                str(r["definition_origin"]),
                str(r["source_doc_id"]),
            )
            for r in conn.execute(
                "SELECT d.name, f.value, d.definition_origin, f.source_doc_id "
                "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
                "WHERE f.period_end = '2024-12-31 00:00:00'"
            ).fetchall()
        }
    finally:
        conn.close()
    debt = rows["Debt - Long-Term Debt — Total long-term debt"]
    assert debt[0] == Decimal("10883000000")
    assert debt[1] == "capture"  # origin stamped
    assert debt[2] == "77"  # provenance back to the filing doc

    # PR3: a coverage record was written for this Stage-A run.
    from pipeline.capture_coverage import load_coverage

    cov = load_coverage(tmp_path)
    assert len(cov) == 1
    assert cov[0].stage == "xbrl_capture"
    assert cov[0].ticker == "X"
    assert cov[0].captured > 0
    # Nothing silently dropped: captured + cell-level skips == seen.
    cell_skips = sum(n for k, n in cov[0].skipped.items() if not k.startswith("section_"))
    assert cov[0].captured + cell_skips == cov[0].seen
