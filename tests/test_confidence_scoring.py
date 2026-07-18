"""Per-fact confidence scoring (fund-grade build S2, PR1): the documented
formula, validation-issue matching, the idempotent DB rescore + CLI, the
ingest wiring, and the chip's % + low-confidence affordance."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from compute._common import insert_financial_facts
from models.documents import SourceQualityTier
from models.facts import Currency, FinancialFact, FiscalPeriodType, LegacyEscapeHatch, Unit
from pipeline.confidence import (
    IssueSignal,
    apply_confidence_scores,
    classify_extraction_method,
    issues_for_fact,
    load_unresolved_issues,
    score_confidence,
)
from report.models import CellSource
from report.sections.financials import (
    _to_cell_source,  # pyright: ignore[reportPrivateUsage]
)
from timeseries.loaders import load_financial_cell_provenance
from ui.source_chip import (
    LOW_CONFIDENCE_THRESHOLD,
    confidence_pct,
    source_chip_html,
    source_hover_title,
)

# ----------------------------------------------------------------------------
# the formula — worked examples from the module docstring, pinned verbatim
# ----------------------------------------------------------------------------


def test_score_confidence_worked_examples() -> None:
    assert score_confidence(tier="sec_official", extracted_by="sec_xbrl") == 1.0
    assert score_confidence(tier="fmp_normalized", extracted_by="fmp") == 0.94
    assert (
        score_confidence(
            tier="fmp_normalized",
            extracted_by="fmp",
            issues=(IssueSignal(rule="source_disagreement"),),
        )
        == 0.79
    )
    assert score_confidence(
        tier="llm_extracted", extracted_by="llm:claude-haiku", self_reported=0.95
    ) == pytest.approx(0.665)
    assert score_confidence(tier="yfinance_fallback", extracted_by="fmp") == 0.72
    assert score_confidence(tier="s1_provisional", extracted_by="s1") == 0.67


def test_score_confidence_accepts_tier_enum() -> None:
    assert score_confidence(tier=SourceQualityTier.SEC_OFFICIAL, extracted_by="sec_xbrl") == 1.0


def test_score_confidence_unknowns_take_documented_fallbacks() -> None:
    # Unknown tier → 0.70 base; unknown method → -0.02; NULL extracted_by ditto.
    assert score_confidence(tier="weird_tier", extracted_by="fmp") == 0.72
    assert score_confidence(tier="fmp_normalized", extracted_by="mystery_writer") == 0.90
    assert score_confidence(tier="fmp_normalized", extracted_by=None) == 0.90
    assert score_confidence(tier=None, extracted_by=None) == 0.68


def test_score_confidence_halt_doubles_and_cap_binds() -> None:
    halt = score_confidence(
        tier="fmp_normalized",
        extracted_by="fmp",
        issues=(IssueSignal(rule="magnitude_jump", severity="halt"),),
    )
    assert halt == pytest.approx(0.94 - 0.20)
    # Four disagreements would be 0.60 of penalty; the cap holds it at 0.40.
    piled = score_confidence(
        tier="sec_official",
        extracted_by="sec_xbrl",
        issues=tuple(IssueSignal(rule="source_disagreement") for _ in range(4)),
    )
    assert piled == pytest.approx(1.0 - 0.40)


def test_score_confidence_clamps_floor_and_self_report() -> None:
    floored = score_confidence(
        tier="s1_provisional",
        extracted_by="llm:x",
        issues=(IssueSignal(rule="source_disagreement", severity="halt"),),
        self_reported=0.1,
    )
    assert floored == 0.05
    # Self-report > 1 is clamped before multiplying; never inflates the score.
    assert score_confidence(tier="fmp_normalized", extracted_by="fmp", self_reported=5.0) == 0.94


def test_classify_extraction_method_buckets() -> None:
    for tag in (
        "fmp",
        "fmp_as_reported",
        "sec_xbrl",
        "s1",
        "fmp_derived",
        "kpi_transform_derived",
        "ir_spreadsheet",
    ):
        assert classify_extraction_method(tag) == "deterministic"
    for tag in ("llm", "llm_extracted", "llm:claude-haiku-4-5", "ir_presentation_readout"):
        assert classify_extraction_method(tag) == "llm"
    for tag in ("manual_filing_backfill", "manual_entry", "analyst_comment"):
        assert classify_extraction_method(tag) == "manual"
    assert classify_extraction_method(None) == "unknown"
    assert classify_extraction_method("something_else") == "unknown"


# ----------------------------------------------------------------------------
# matching validation_issues raw_value formats back to fact rows
# ----------------------------------------------------------------------------


def _issue_db(rows: list[tuple[str, str, str, str, str | None]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT,
            severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT,
            raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        """
    )
    conn.executemany(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, "
        " raised_at, resolved_at) VALUES ('r1', ?, ?, ?, ?, '2026-06-01 00:00:00', ?)",
        rows,
    )
    conn.commit()
    return conn


def test_issue_matching_per_rule_precision() -> None:
    conn = _issue_db(
        [
            # disagreement: matches (item, embedded period) only
            (
                "TST",
                "warn",
                "source_disagreement",
                "revenue @ 2025-12-31: fmp=100 vs sec_xbrl=101 (0.99%)",
                None,
            ),
            # jump: matches (item, trailing "at <date>")
            (
                "TST",
                "warn",
                "magnitude_jump",
                "revenue prior=100 current=900 (ratio=9.0x) at 2025-09-30",
                None,
            ),
            # range: matches (item, offending value)
            ("TST", "warn", "plausible_range", "total_assets=-5", None),
            # resolved → never loaded
            (
                "TST",
                "warn",
                "source_disagreement",
                "revenue @ 2025-06-30: fmp=1 vs sec_xbrl=9 (88%)",
                "2026-06-02 00:00:00",
            ),
        ]
    )
    signals = load_unresolved_issues(conn)
    conn.close()

    hit = issues_for_fact(
        signals, ticker="tst", item="revenue", period_end="2025-12-31 00:00:00", value=100.0
    )
    assert [s.rule for s in hit] == ["source_disagreement"]
    # Same item, different period — the disagreement must NOT bleed across cells.
    assert (
        issues_for_fact(signals, ticker="TST", item="revenue", period_end="2025-03-31", value=95.0)
        == ()
    )
    jump = issues_for_fact(
        signals, ticker="TST", item="revenue", period_end="2025-09-30", value=900.0
    )
    assert [s.rule for s in jump] == ["magnitude_jump"]
    # Range penalty lands only on the offending value, not the whole series.
    assert issues_for_fact(
        signals, ticker="TST", item="total_assets", period_end="2024-12-31", value=-5.0
    ) == (IssueSignal(rule="plausible_range", severity="warn"),)
    assert (
        issues_for_fact(
            signals, ticker="TST", item="total_assets", period_end="2024-12-31", value=900.0
        )
        == ()
    )
    # The resolved 2025-06-30 disagreement was filtered at load time.
    assert (
        issues_for_fact(signals, ticker="TST", item="revenue", period_end="2025-06-30", value=1.0)
        == ()
    )


def test_load_unresolved_issues_tolerates_missing_table() -> None:
    conn = sqlite3.connect(":memory:")
    assert load_unresolved_issues(conn) == {}
    conn.close()


# ----------------------------------------------------------------------------
# DB rescore (the backfill engine) + the CLI
# ----------------------------------------------------------------------------

_RESCORE_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT, source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL,
    value NUMERIC(24, 6) NOT NULL, currency TEXT, unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, name TEXT NOT NULL
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, kpi_definition_id INTEGER NOT NULL,
    value NUMERIC(24, 6) NOT NULL, unit TEXT NOT NULL,
    source_doc_id INTEGER NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
    extracted_by TEXT
);
CREATE TABLE validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, source_doc_id INTEGER, ticker TEXT,
    severity TEXT NOT NULL, rule TEXT NOT NULL, raw_value TEXT, expected TEXT,
    raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
);
"""


def _seed_rescore_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_RESCORE_DDL)
    conn.executemany(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status, source_quality_tier) "
        "VALUES (?, 'TST', ?, 'x', ?, ?, '2026-01-01 00:00:00', 'ok', ?)",
        [
            (1, "fmp", "f.json", "a", "fmp_normalized"),
            (2, "sec_xbrl", "s.json", "b", "sec_official"),
            (3, "llm_extracted", "l.json", "c", "llm_extracted"),
        ],
    )
    conn.executemany(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item, "
        " value, unit, source_doc_id, confidence, extracted_by) "
        "VALUES ('TST', ?, ?, ?, ?, 'actual', ?, ?, ?)",
        [
            ("2025-12-31 00:00:00", "Q4", "revenue", "100", 1, 1.0, "fmp"),
            ("2025-09-30 00:00:00", "Q3", "revenue", "90", 1, 1.0, "fmp"),
            ("2025-12-31 00:00:00", "Q4", "revenue", "101", 2, 1.0, "sec_xbrl"),
        ],
    )
    conn.execute("INSERT INTO kpi_definitions (id, ticker, name) VALUES (7, 'TST', 'ARPAC')")
    conn.executemany(
        "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, "
        " value, unit, source_doc_id, confidence, extracted_by) "
        "VALUES ('TST', ?, 'Q4', 7, ?, 'actual', ?, ?, ?)",
        [
            ("2025-12-31 00:00:00", "11.2", 1, 1.0, "fmp_derived"),
            # Self-scored LLM extraction — must be preserved verbatim.
            ("2025-09-30 00:00:00", "10.9", 3, 0.85, "llm:claude-haiku-4-5"),
            # LLM row still at the unset default — gets the formula prior.
            ("2025-06-30 00:00:00", "10.1", 3, 1.0, "llm_extracted"),
        ],
    )
    # Unresolved disagreement on Q4 revenue; a resolved twin that must not bite.
    conn.executemany(
        "INSERT INTO validation_issues (run_id, ticker, severity, rule, raw_value, "
        " raised_at, resolved_at) VALUES ('r1', 'TST', 'warn', 'source_disagreement', ?, "
        " '2026-06-01 00:00:00', ?)",
        [
            ("revenue @ 2025-12-31: fmp=100 vs sec_xbrl=101 (0.99%)", None),
            ("revenue @ 2025-09-30: fmp=90 vs sec_xbrl=99 (9.1%)", "2026-06-02 00:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_apply_confidence_scores_end_to_end_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "rescore.db"
    _seed_rescore_db(db)
    conn = sqlite3.connect(db)

    fin = apply_confidence_scores(conn, table="financial_facts")
    kpi = apply_confidence_scores(conn, table="kpi_facts")
    assert fin.examined == 3 and fin.updated == 3
    assert kpi.examined == 3 and kpi.updated == 2
    assert kpi.preserved_self_scored == 1

    rows = conn.execute(
        "SELECT line_item, period_end, source_doc_id, confidence FROM financial_facts ORDER BY id"
    ).fetchall()
    # Q4 fmp row: 0.94 base - 0.15 disagreement = 0.79; Q3 untouched by issues.
    assert rows[0][3] == pytest.approx(0.79)
    assert rows[1][3] == pytest.approx(0.94)
    # The SEC side of the same disagreement is penalized too: 1.00 - 0.15.
    assert rows[2][3] == pytest.approx(0.85)

    krows = conn.execute("SELECT confidence FROM kpi_facts ORDER BY id").fetchall()
    assert krows[0][0] == pytest.approx(0.94)  # fmp_derived prior
    assert krows[1][0] == pytest.approx(0.85)  # self-scored LLM row preserved
    assert krows[2][0] == pytest.approx(0.70)  # llm tier+method prior

    # Idempotence: a second pass over the unchanged DB writes nothing.
    again_fin = apply_confidence_scores(conn, table="financial_facts")
    again_kpi = apply_confidence_scores(conn, table="kpi_facts")
    assert again_fin.updated == 0
    assert again_kpi.updated == 0
    conn.close()


def test_apply_confidence_scores_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "dry.db"
    _seed_rescore_db(db)
    conn = sqlite3.connect(db)
    out = apply_confidence_scores(conn, table="financial_facts", apply=False)
    assert out.updated == 3
    assert [r[0] for r in conn.execute("SELECT confidence FROM financial_facts")] == [
        1.0,
        1.0,
        1.0,
    ]
    conn.close()


def test_backfill_cli_dry_run_then_apply(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))
    import backfill_confidence

    db = tmp_path / "cli.db"
    _seed_rescore_db(db)
    assert backfill_confidence.main(["--db", str(db)]) == 0  # dry-run
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT MIN(confidence) FROM financial_facts").fetchone()[0] == 1.0
    conn.close()

    assert backfill_confidence.main(["--db", str(db), "--apply"]) == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT confidence FROM financial_facts WHERE period_end LIKE '2025-12-31%' "
        "AND source_doc_id = 1"
    ).fetchone()[0] == pytest.approx(0.79)
    conn.close()

    assert backfill_confidence.main(["--db", str(tmp_path / "missing.db")]) == 2


# ----------------------------------------------------------------------------
# ingest wiring: insert_financial_facts scores the unset default
# ----------------------------------------------------------------------------


def _fact(value: str, confidence: float = 1.0) -> FinancialFact:
    return FinancialFact(
        ticker="TST",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        line_item="revenue",
        value=Decimal(value),
        currency=Currency.USD,
        unit=Unit.ACTUAL,
        source_doc_id=1,
        confidence=confidence,
        locator=LegacyEscapeHatch(reason="test fixture value -- provenance not under test here"),
    )


def test_insert_financial_facts_scores_with_tier(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_RESCORE_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256, "
        " fetched_at, fetch_status) VALUES (1, 'TST', 'fmp', 'x', 'f', 'a', "
        " '2026-01-01 00:00:00', 'ok')"
    )
    inserted = insert_financial_facts(
        conn, [_fact("100")], extracted_by="fmp", tier=SourceQualityTier.FMP_NORMALIZED
    )
    assert inserted == 1
    assert conn.execute("SELECT confidence FROM financial_facts").fetchone()[0] == pytest.approx(
        0.94
    )

    # An explicit non-default confidence set by the builder is preserved.
    conn.execute("DELETE FROM financial_facts")
    insert_financial_facts(
        conn,
        [_fact("100", confidence=0.5)],
        extracted_by="fmp",
        tier=SourceQualityTier.FMP_NORMALIZED,
    )
    assert conn.execute("SELECT confidence FROM financial_facts").fetchone()[0] == 0.5

    # No tier (legacy caller) → old behavior, the model default rides through.
    conn.execute("DELETE FROM financial_facts")
    insert_financial_facts(conn, [_fact("100")], extracted_by="fmp")
    assert conn.execute("SELECT confidence FROM financial_facts").fetchone()[0] == 1.0
    conn.close()


# ----------------------------------------------------------------------------
# read side: loader payload → CellSource → chip % + low-confidence affordance
# ----------------------------------------------------------------------------


def test_cell_provenance_loader_carries_confidence(tmp_path: Path) -> None:
    db = tmp_path / "conf.db"
    _seed_rescore_db(db)
    conn = sqlite3.connect(db)
    apply_confidence_scores(conn, table="financial_facts")
    conn.close()

    out = load_financial_cell_provenance("TST", ["revenue"], db_path=db)
    # Doc 2 (higher source_doc_id) wins the Q4 cell: the SEC row at 0.85.
    assert out["revenue"]["2025-12-31"]["confidence"] == pytest.approx(0.85)
    assert out["revenue"]["2025-09-30"]["confidence"] == pytest.approx(0.94)


def test_to_cell_source_maps_confidence() -> None:
    src = _to_cell_source({"source": "fmp_normalized", "confidence": 0.79})
    assert src is not None
    assert src.confidence == pytest.approx(0.79)
    bare = _to_cell_source({"source": "fmp_normalized"})
    assert bare is not None and bare.confidence is None


def test_chip_renders_confidence_pct_and_low_conf_affordance() -> None:
    low = CellSource(source="fmp_normalized", fetched_at="2026-01-05 10:00:00", confidence=0.79)
    out = source_chip_html(low)
    assert "confidence 79% · below threshold" in out
    assert "src-lowconf" in out
    assert source_hover_title(low) == "fmp_normalized · fetched 2026-01-05 · conf 79%"

    ok = CellSource(source="sec_official", confidence=1.0)
    ok_html = source_chip_html(ok)
    assert "confidence 100%" in ok_html
    assert "below threshold" not in ok_html
    assert "src-lowconf" not in ok_html

    # Unscored rows (legacy DBs) keep the original anatomy — no %, no flag.
    unscored = CellSource(source="fmp_normalized", fetched_at="2026-01-05 10:00:00")
    plain = source_chip_html(unscored)
    assert "confidence" not in plain
    assert "src-lowconf" not in plain
    assert source_hover_title(unscored) == "fmp_normalized · fetched 2026-01-05"

    assert confidence_pct(CellSource(source="x", confidence=0.665)) == 66
    assert confidence_pct(CellSource(source="x")) is None
    assert 0.0 < LOW_CONFIDENCE_THRESHOLD < 1.0
