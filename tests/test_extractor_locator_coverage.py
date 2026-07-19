"""CI guard for the provenance click-through program (Phase A §4.2; extended
with Phase C's §3.3 anchor-quote-verification fixtures for provenance.edgar_8k)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from models.facts import FactLocator, LegacyEscapeHatch, LocatorKind  # noqa: E402

REGISTERED_EXTRACTOR_LOCATOR_VERSIONS: dict[str, int] = {
    "table_extractors.generic_xbrl_capture": 2,
    "compute._common": 2,
    "compute.as_reported": 2,
    "compute.s1_financials": 1,
    "ir_pipeline.ingest": 1,
    "compute.kpi_extract_summaries": 1,
    "competitive.transcript_mentions": 1,
    "competitive.category_share": 1,
}

# Writers that persist a kpi_facts/financial_facts row WITHOUT going through
# a KpiValue/FinancialFact model instance (so they sit outside
# _discover_writer_modules's regex scan above) but that this hardening pass
# specifically requires to emit a renderable locator anyway. Currently just
# the metrics engine's own insert_kpi_with_restatement_detection call
# (compute.metrics_engine.io._persist_attempt writes a `derived`-kind
# FactLocator alongside computed_from -- see
# tests/test_metrics_engine_io.py::test_compute_for_ticker_writes_derived_locator
# for the full functional test; the fixture test below is the CI-guard-file
# registration Scope item 3a asks for, kept lightweight and self-contained).
REGISTERED_DERIVED_LOCATOR_EMITTERS: frozenset[str] = frozenset({"compute.metrics_engine.io"})

# Phase C (docs/design/provenance_clickthrough.md §3.3): the 8-K segment
# override extractor builds a FactLocator directly (html_span, after
# pipeline.locators.verify_quote_in_source passes) rather than through a
# KpiValue/FinancialFact instance -- it writes fact_overrides, not
# financial_facts/kpi_facts/segment_dimensions, so it sits outside BOTH the
# discovery scan above AND that scan's declared table scope. Registered here,
# same pattern as REGISTERED_DERIVED_LOCATOR_EMITTERS, so this hardening pass
# still proves (fixture-tested below) that it emits a real v2 locator AND
# that a fabricated anchor_quote is rejected, never silently trusted.
REGISTERED_HTML_SPAN_LOCATOR_EMITTERS: frozenset[str] = frozenset({"provenance.edgar_8k"})

# compute.segments writes segment_dimensions rows via SegmentDimension(...)
# instances (through pipeline.segment_junction_writer), not a
# FinancialFact/KpiValue instance -- it sits outside BOTH the discovery scan
# above AND that scan's declared table scope, same as the two frozensets
# above. compute.segments.extract_segment_facts is the writer used for legacy
# per-ticker quarterly FMP segment JSON ingest (e.g. data/historical/fmp/
# MELI_product_segments_quarterly.json / MELI_geo_segments_quarterly.json);
# it predates the locator contract, so every row it wrote before this
# registration landed with segment_dimensions.locator NULL. Registered here so
# this pass proves (fixture-tested below) it now emits a real v2
# `fmp_json_table` locator per dimension row, AND that re-ingesting over an
# already-landed locator-NULL row (the pre-fix shape) heals it in place
# rather than duplicating the row -- see tests/test_compute_segments.py for
# the full functional coverage of both cases.
REGISTERED_FMP_JSON_TABLE_LOCATOR_EMITTERS: frozenset[str] = frozenset({"compute.segments"})

_WRITER_CALL_RX = re.compile(r"(?<!class )\b(KpiValue|FinancialFact)\(")
_STRING_LITERAL_RX = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')


def _iter_escape_hatch_reasons(text: str) -> list[str]:
    """Extract every REAL ``LegacyEscapeHatch(reason="...")`` reason string
    from ``text``, tolerating Python's implicit adjacent-string-literal
    concatenation across multiple lines (this repo's own style for a long
    reason, e.g. ``reason=("a " "b " "c")``) -- a single-string regex would
    miss every multi-line reason actually used in this codebase.

    Skips ``class LegacyEscapeHatch(BaseModel):`` (the class definition
    itself) and any comment/docstring mention that merely NAMES the pattern
    without a quoted string following (e.g. "...an explicit
    ``LegacyEscapeHatch(reason=...)``" in prose) -- both produce zero string
    literals inside the balanced parens, which is the actual signal used to
    tell "a real call" from "text about the call" apart, rather than trying
    to separately detect comments/docstrings.
    """
    reasons: list[str] = []
    for m in re.finditer(r"LegacyEscapeHatch\(", text):
        depth = 1
        i = m.end()
        while depth > 0 and i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        call_body = text[m.end() : i - 1]
        parts = [g1 or g2 for g1, g2 in _STRING_LITERAL_RX.findall(call_body)]
        if parts:
            reasons.append("".join(parts))
    return reasons


_BOILERPLATE_ESCAPE_HATCH_REASONS: frozenset[str] = frozenset(
    {
        "todo",
        "fixme",
        "n/a",
        "na",
        "tbd",
        "reason",
        "no reason",
        "no reason given",
        "...",
        "unknown",
        "same as above",
        "see above",
        "later",
        "placeholder",
    }
)


def _discover_writer_modules() -> set[str]:
    discovered: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if _WRITER_CALL_RX.search(text):
            rel = path.relative_to(SRC_ROOT).with_suffix("").as_posix().replace("/", ".")
            discovered.add(rel)
    return discovered


def test_every_locator_writer_is_registered() -> None:
    discovered = _discover_writer_modules()
    registered = set(REGISTERED_EXTRACTOR_LOCATOR_VERSIONS)
    new_unregistered = discovered - registered
    assert not new_unregistered, sorted(new_unregistered)
    stale = registered - discovered
    assert not stale, sorted(stale)


def test_generic_xbrl_capture_locator_is_v2_on_fixture() -> None:
    from collections import defaultdict
    from datetime import datetime

    from table_extractors import generic_xbrl_capture as g

    section: list[dict[str, object]] = [
        {"Debt - Long-Term Debt (Details) - USD ($) $ in Millions": ["Dec. 31, 2024"]},
        {"Term loan, net": [500]},
    ]
    per_period: dict[datetime, list[object]] = defaultdict(list)
    audit = g.CaptureAudit()
    g._walk_section("Debt - Long-Term", section, per_period, audit, fye_md=(12, 31))
    values = next(iter(per_period.values()))
    loc = values[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "Term loan, net"
    assert loc.table_cell.column_header == "Dec. 31, 2024"
    assert loc.verbatim_snippet


def test_common_extract_facts_with_spec_locator_is_v2_on_fixture() -> None:
    from compute._common import extract_facts_with_spec
    from models.facts import Unit
    from models.fmp_payloads import FmpIncomeStatementRecord

    record = FmpIncomeStatementRecord.model_validate(
        {
            "date": "2025-12-31",
            "symbol": "GOOG",
            "reportedCurrency": "USD",
            "period": "FY",
            "revenue": 402963000000,
        }
    )
    facts = extract_facts_with_spec(
        record, 1, [("revenue", "revenue", Unit.ACTUAL)], record_index=3
    )
    loc = facts[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.json_path == "[3].revenue"
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenue"
    assert loc.table_cell.column_header == "2025-12-31"
    assert loc.verbatim_snippet


def test_as_reported_locator_is_v2_on_fixture() -> None:
    from compute.as_reported import extract_facts_from_record
    from models.fmp_payloads import FmpAsReportedRecord

    record = FmpAsReportedRecord.model_validate(
        {
            "date": "2025-09-30",
            "symbol": "NOW",
            "reportedCurrency": "USD",
            "period": "Q3",
            "data": {"revenueremainingperformanceobligation": 21500000000},
        }
    )
    facts = extract_facts_from_record(record, source_doc_id=9, record_index=2)
    loc = facts[0].locator
    assert isinstance(loc, FactLocator)
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.json_path == "[2].data.revenueremainingperformanceobligation"
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "revenueremainingperformanceobligation"


def test_sec_xbrl_locator_is_v2_on_fixture() -> None:
    """pipeline.sec_xbrl is enriched but writes via a lower-level SQL insert
    path rather than constructing a FinancialFact/KpiValue instance, so it
    sits outside the discovery scan above -- fixture-tested directly."""
    import sqlite3
    from datetime import datetime

    from pipeline.sec_xbrl import insert_facts_from_companyfacts

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL, "
        "period_end TIMESTAMP, file_path TEXT NOT NULL, sha256 TEXT NOT NULL, "
        "fetched_at TIMESTAMP NOT NULL, fetch_status TEXT NOT NULL, "
        "raw_bytes_size INTEGER NOT NULL);"
        "CREATE TABLE financial_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, "
        "fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, "
        "value NUMERIC(24, 6) NOT NULL, currency TEXT, unit TEXT NOT NULL, "
        "source_doc_id INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, "
        "extracted_by TEXT, supersedes_id INTEGER, locator TEXT);"
        "CREATE UNIQUE INDEX uq_financial_facts_provenance ON financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, source_doc_id);"
    )
    conn.execute(
        "INSERT INTO documents "
        "(ticker, source_type, doc_type, file_path, sha256, fetched_at, fetch_status, raw_bytes_size) "
        "VALUES ('GOOG', 'sec_xbrl', 'sec_10q', "
        "'data/historical/sec/GOOG_companyfacts.json#accn=0001-01-000001', 'b', ?, 'ok', 1)",
        (datetime.now(),),
    )
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    payload: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "accn": "0001-01-000001",
                                "end": "2025-03-31",
                                "start": "2025-01-01",
                                "val": 80500000000,
                                "fp": "Q1",
                            }
                        ]
                    }
                }
            }
        }
    }
    inserted = insert_facts_from_companyfacts(
        conn, ticker="GOOG", payload=payload, accession_to_doc_id={"0001-01-000001": doc_id}
    )
    assert inserted == 1
    row = conn.execute("SELECT locator FROM financial_facts").fetchone()
    loc = FactLocator.from_json(row["locator"])
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "Revenues"
    assert loc.table_cell.column_header == "2025-03-31"
    conn.close()


def test_legacy_writer_stays_unenriched_by_construction() -> None:
    """The floor=1 s1_financials entry genuinely doesn't emit a renderable
    locator at all today -- pinning it at 1 isn't an unverified claim.

    Since the persist-time enforcement flip (docs/design/
    provenance_clickthrough.md §4.1), the field can no longer be a bare
    ``None`` -- the writer must make its "I genuinely have nothing" state
    explicit via ``LegacyEscapeHatch``, which is what this asserts now."""
    from datetime import datetime
    from decimal import Decimal

    from compute.s1_financials import S1Datum, build_financial_facts
    from models.facts import FiscalPeriodType, LegacyEscapeHatch, Unit

    datum = S1Datum(
        line_item="revenue",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.FY,
        value=Decimal("100"),
        unit=Unit.ACTUAL,
        currency=None,
    )
    facts = build_financial_facts([datum], source_doc_id=1, ticker="TST")
    assert isinstance(facts[0].locator, LegacyEscapeHatch)
    assert facts[0].locator.reason


# ---------------------------------------------------------------------------
# Persist-time enforcement (section 4.1 of this pass, closing Phase A's
# deferral): every registered writer above must supply a REQUIRED
# FactLocator | LegacyEscapeHatch -- there is no more implicit None default.
# The metrics engine writes kpi_facts through a different path (directly via
# insert_kpi_with_restatement_detection, not a KpiValue instance), so it is
# registered and fixture-tested separately from the KpiValue/FinancialFact
# discovery scan above.
# ---------------------------------------------------------------------------


def test_metrics_engine_persists_derived_locator_on_fixture() -> None:
    """compute.metrics_engine.io._persist_attempt (REGISTERED_DERIVED_LOCATOR_EMITTERS)
    writes a canonical `derived`-kind FactLocator for every successful compute,
    not just the pre-existing `computed_from` JSON -- see
    docs/design/bottoms_up_metrics_engine.md section 3 /
    provenance_clickthrough.md section 1.5. Full end-to-end coverage (real
    compute_for_ticker run) lives in
    tests/test_metrics_engine_io.py::test_compute_for_ticker_writes_derived_locator;
    this is the lightweight, self-contained fixture the CI-guard file itself
    asserts against so a future refactor of _persist_attempt can't silently
    drop the locator write without failing HERE too."""
    import sqlite3
    from datetime import datetime
    from decimal import Decimal

    from compute.metrics_engine.engine import ComputedValue
    from compute.metrics_engine.io import _persist_attempt  # pyright: ignore[reportPrivateUsage]
    from compute.metrics_engine.registry import REGISTRY

    assert "compute.metrics_engine.io" in REGISTERED_DERIVED_LOCATOR_EMITTERS

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE kpi_definitions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL, "
        "primary_source TEXT NOT NULL, fallback_source TEXT, ir_url TEXT, "
        "threshold_tier TEXT, threshold_low REAL, threshold_high REAL, notes TEXT, "
        "UNIQUE(ticker, name));"
        "CREATE TABLE kpi_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, "
        "fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL, "
        "value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL, source_doc_id INTEGER NOT NULL, "
        "confidence REAL DEFAULT 1.0, extracted_by VARCHAR(64), supersedes_id INTEGER, "
        "locator TEXT, computed_from TEXT, formula_id INTEGER, formula_version INTEGER);"
        "CREATE UNIQUE INDEX uq_kpi_facts_provenance ON kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id);"
        "CREATE TABLE metric_computation_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL, fiscal_period_type TEXT NOT NULL, "
        "formula_id INTEGER NOT NULL, status TEXT NOT NULL, reason_code TEXT, reason_detail TEXT, "
        "kpi_fact_id INTEGER, input_fingerprint TEXT NOT NULL, engine_version TEXT NOT NULL, "
        "computed_at TIMESTAMP NOT NULL);"
        "CREATE UNIQUE INDEX uq_metric_computation_attempts_logical ON metric_computation_attempts "
        "(ticker, period_end, fiscal_period_type, formula_id);"
    )

    formula = next(iter(REGISTRY.values()))
    period_end = datetime(2025, 12, 31)
    result = ComputedValue(value=Decimal("42.0"))
    lineage = [(next(iter(formula.required_inputs)), period_end, 1)]

    _persist_attempt(
        conn,
        ticker="TST",
        period_end=period_end,
        fiscal_period_type="Q4",
        formula=formula,
        formula_id=1,
        result=result,
        lineage=lineage,
    )
    row = conn.execute("SELECT locator, computed_from FROM kpi_facts").fetchone()
    assert row is not None
    loc = FactLocator.from_json(row["locator"])
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.DERIVED
    assert loc.derived is not None
    assert loc.derived.formula_id == 1
    assert len(loc.derived.inputs) == 1
    assert row["computed_from"] is not None
    conn.close()


# ---------------------------------------------------------------------------
# Phase C (§3.3): anchor-quote verification for the 8-K segment extractor.
# provenance.edgar_8k builds FactLocator directly (REGISTERED_HTML_SPAN_
# LOCATOR_EMITTERS above), so it is fixture-tested here rather than picked up
# by the KpiValue/FinancialFact discovery scan.
# ---------------------------------------------------------------------------


def test_edgar_8k_emits_v2_html_span_locator_on_fixture() -> None:
    """A verified anchor_quote earns a real, renderable v2 `html_span`
    locator -- the CI-guard-file-level proof that provenance.edgar_8k (in
    REGISTERED_HTML_SPAN_LOCATOR_EMITTERS) actually emits one, not just that
    the module exists."""
    from provenance import edgar_8k

    assert "provenance.edgar_8k" in REGISTERED_HTML_SPAN_LOCATOR_EMITTERS

    exhibit_text = "Q4 2025 Results\nGoogle Cloud revenue was $17,664 million.\n"
    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=lambda _u: {"0": {"cik_str": 1652044, "ticker": "GOOG"}},
        get_text=lambda _u: exhibit_text,
        exhibit="ex991.htm",
        call=lambda *_a, **_k: {
            "Google Cloud": {
                "value": 17664000000,
                "anchor_quote": "Google Cloud revenue was $17,664 million.",
            }
        },
    )
    assert proposal is not None
    assert isinstance(proposal.locator, FactLocator)
    assert proposal.locator.locator_version >= 2
    assert proposal.locator.effective_kind() == LocatorKind.HTML_SPAN
    assert proposal.locator.html_span is not None
    assert proposal.failed_segments == ()


def test_edgar_8k_rejects_fabricated_anchor_quote_on_fixture() -> None:
    """The guard-ratchet's required proof: a fabricated (non-verbatim) quote
    MUST NEVER produce a renderable locator -- it demotes to a
    LegacyEscapeHatch and the specific failed segment is named, never a
    silently-trusted html_span."""
    from provenance import edgar_8k

    exhibit_text = "Q4 2025 Results\nGoogle Cloud revenue was $17,664 million.\n"
    proposal = edgar_8k.extract_8k_segment_override(
        ticker="GOOG",
        accession="0001652044-26-000012",
        period_end="2025-12-31",
        fiscal_period_type="Q4",
        get_json=lambda _u: {"0": {"cik_str": 1652044, "ticker": "GOOG"}},
        get_text=lambda _u: exhibit_text,
        exhibit="ex991.htm",
        call=lambda *_a, **_k: {
            "Google Cloud": {
                "value": 17664000000,
                # Never appears in exhibit_text -- a hallucinated anchor.
                "anchor_quote": "Google Cloud posted its best quarter ever at $17.7B.",
            }
        },
    )
    assert proposal is not None
    assert isinstance(proposal.locator, LegacyEscapeHatch)
    assert "anchor_verification_failed" in proposal.locator.reason
    assert proposal.failed_segments == ("Google Cloud",)
    # The value itself is NEVER dropped for a bad anchor alone.
    assert proposal.segments["Google Cloud"] == 17664000000


def _segments_schema() -> str:
    return """
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        source_type TEXT NOT NULL,
        doc_type TEXT NOT NULL,
        file_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        fetched_at TIMESTAMP NOT NULL,
        fetch_status TEXT NOT NULL,
        raw_bytes_size INTEGER NOT NULL
    );
    CREATE TABLE segment_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker VARCHAR(16) NOT NULL,
        period_end DATETIME NOT NULL,
        fiscal_period_type VARCHAR(8) NOT NULL,
        source_doc_id INTEGER NOT NULL REFERENCES documents(id),
        currency VARCHAR(8),
        unit VARCHAR(16) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        period_basis VARCHAR(16) NOT NULL DEFAULT 'discrete',
        raw_period_label TEXT,
        method_version VARCHAR(32),
        CONSTRAINT uq_segment_periods_provenance UNIQUE
          (ticker, period_end, fiscal_period_type, source_doc_id)
    );
    CREATE TABLE segment_dimensions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        period_id INTEGER NOT NULL REFERENCES segment_periods(id),
        dim_type VARCHAR(16) NOT NULL,
        dim_name VARCHAR(128) NOT NULL,
        value NUMERIC(20, 4) NOT NULL,
        metric VARCHAR(32) NOT NULL,
        unit VARCHAR(16),
        disclosure_status VARCHAR(16) NOT NULL DEFAULT 'reported',
        method_version VARCHAR(32),
        confidence REAL NOT NULL DEFAULT 1.0,
        extracted_by VARCHAR(64),
        locator TEXT,
        derived_from TEXT,
        supersedes_id INTEGER
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
        confidence REAL NOT NULL DEFAULT 1.0
    );
    """


def test_compute_segments_emits_v2_fmp_json_table_locator_on_fixture() -> None:
    """compute.segments (REGISTERED_FMP_JSON_TABLE_LOCATOR_EMITTERS) now
    stamps a real v2 `fmp_json_table` locator per segment_dimensions row.

    Full functional coverage (including the re-ingest locator-backfill proof)
    lives in tests/test_compute_segments.py; this is the lightweight,
    self-contained CI-guard-file fixture the registration above asks for."""
    import json
    import sqlite3
    import tempfile
    from datetime import datetime
    from pathlib import Path

    from compute.segments import extract_segment_facts

    assert "compute.segments" in REGISTERED_FMP_JSON_TABLE_LOCATOR_EMITTERS

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_segments_schema())
    conn.execute(
        "INSERT INTO documents "
        "(id, ticker, source_type, doc_type, file_path, sha256, fetched_at, "
        " fetch_status, raw_bytes_size) "
        "VALUES (1, 'MELI', 'fmp', 'fmp_segment_product', "
        "'data/historical/fmp/MELI_product_segments_quarterly.json', "
        "'0000000000000000000000000000000000000000000000000000000000000000', "
        "?, 'ok', 1)",
        (datetime.now(),),
    )
    conn.commit()

    with tempfile.TemporaryDirectory() as tmp:
        project_root = Path(tmp)
        fmp_dir = project_root / "data" / "historical" / "fmp"
        fmp_dir.mkdir(parents=True)
        record = {
            "date": "2025-12-31",
            "symbol": "MELI",
            "reportedCurrency": "USD",
            "period": "Q4",
            "fiscalYear": 2025,
            "data": {"Commerce": 4_000_000_000},
        }
        (fmp_dir / "MELI_product_segments_quarterly.json").write_text(
            json.dumps([record]), encoding="utf-8"
        )
        inserted = extract_segment_facts(conn, 1, project_root)

    assert inserted == 1
    row = conn.execute("SELECT locator FROM segment_dimensions").fetchone()
    loc = FactLocator.from_json(row["locator"])
    assert loc is not None
    assert loc.locator_version >= 2
    assert loc.effective_kind() == LocatorKind.FMP_JSON_TABLE
    assert loc.table_cell is not None
    assert loc.table_cell.row_label == "Commerce"
    assert loc.table_cell.column_header == "2025-12-31"
    assert loc.json_path == "[0].data.Commerce"
    conn.close()


def test_no_empty_or_duplicate_boilerplate_escape_hatch_reasons() -> None:
    """Scope item 3b: a persist call site that opts out via
    ``LegacyEscapeHatch(reason=...)`` must give an honest, specific,
    non-recycled reason -- not an empty/placeholder string, and not the exact
    same literal text copy-pasted at more than one call site (each opt-out is
    justified in its own words; see docs/design/provenance_clickthrough.md
    section 4.1's "not machine-checkable... but a reviewer can grep and ask
    why"). Scoped to src/ only (mirrors _discover_writer_modules above) --
    tests/ fixtures legitimately reuse one generic "not under test" reason
    across many unrelated call sites, which is fine."""
    from collections import defaultdict

    reasons: dict[str, list[str]] = defaultdict(list)
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_reason in _iter_escape_hatch_reasons(text):
            reasons[raw_reason.strip()].append(str(path.relative_to(SRC_ROOT)))

    assert reasons, "expected at least one LegacyEscapeHatch(reason=...) call site in src/"

    for reason, files in reasons.items():
        assert len(reason) >= 8, f"escape-hatch reason too short in {files}: {reason!r}"
        assert reason.lower() not in _BOILERPLATE_ESCAPE_HATCH_REASONS, (
            f"boilerplate escape-hatch reason in {files}: {reason!r}"
        )

    duplicates = {reason: files for reason, files in reasons.items() if len(files) > 1}
    assert not duplicates, (
        f"duplicate LegacyEscapeHatch reason text reused across sites: {duplicates}"
    )
