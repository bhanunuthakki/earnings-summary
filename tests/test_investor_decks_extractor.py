"""Tests for src/table_extractors/investor_decks.py + the bear-case anchor
injection (src/report/sections/bear_case._strategic_targets_md).

PDF text extraction + LLM call are mocked via monkeypatch; everything else
is real SQLite over an inline schema.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from report.sections.bear_case import _strategic_targets_md, _ticker_specific_md
from table_extractors import investor_decks as deck_mod


def _schema(conn: sqlite3.Connection) -> None:
    """Inline strategic_targets + forward_looking_statements + documents schema.

    Mirrors alembic/0053_strategic_targets.py — duplicated here so the test
    doesn't need an alembic upgrade. Keep in sync if the migration evolves.
    """
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            source_type VARCHAR NOT NULL,
            doc_type VARCHAR NOT NULL,
            period_start DATETIME,
            period_end DATETIME,
            file_path VARCHAR NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            fetched_at DATETIME NOT NULL,
            fetch_status VARCHAR NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url VARCHAR,
            parent_document_id INTEGER,
            UNIQUE(sha256)
        );
        CREATE TABLE strategic_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            deck_doc_id INTEGER,
            target_kind VARCHAR(32) NOT NULL,
            target_value NUMERIC(20, 4),
            target_unit VARCHAR(16) NOT NULL,
            target_period VARCHAR(16) NOT NULL,
            target_currency VARCHAR(8),
            narrative_excerpt TEXT NOT NULL,
            extracted_at DATETIME NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0
        );
        CREATE UNIQUE INDEX uq_strategic_targets
        ON strategic_targets (
            ticker, deck_doc_id, target_kind, target_period,
            substr(narrative_excerpt, 1, 128)
        );
        CREATE TABLE forward_looking_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR(16) NOT NULL,
            source_doc_id INTEGER NOT NULL,
            char_offset_start INTEGER,
            char_offset_end INTEGER,
            sentence TEXT NOT NULL,
            speaker VARCHAR(128),
            tense VARCHAR(16),
            quantifier VARCHAR(64),
            kpi_name VARCHAR(128),
            kpi_concept_id INTEGER,
            target_value NUMERIC(24, 6),
            target_period DATETIME,
            prediction_id INTEGER,
            extracted_at DATETIME NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0
        );
        """
    )
    conn.commit()


@pytest.fixture()
def db_with_deck(tmp_path: Path) -> tuple[Path, Path, int]:
    """Build a tmp repo layout with one deck PDF on disk + one documents row.

    Returns (db_path, deck_path, deck_doc_id).
    """
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    ir_dir = repo / "ir_documents" / "ABNB" / "2025-12-31"
    ir_dir.mkdir(parents=True)
    deck_path = ir_dir / "ir_investor_update__abc12345.pdf"
    deck_path.write_bytes(b"%PDF-1.4 fake bytes")

    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
        # Match how categorize_ir_uploads.py registers files: relative path
        # rooted at repo root with forward slashes.
        rel_path = "ir_documents/ABNB/2025-12-31/ir_investor_update__abc12345.pdf"
        sha = "f" * 64
        conn.execute(
            """
            INSERT INTO documents
                (ticker, source_type, doc_type, period_end, file_path, sha256,
                 fetched_at, fetch_status, raw_bytes_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ABNB",
                "ir_doc",
                "ir_investor_update",
                datetime(2025, 12, 31, tzinfo=UTC).isoformat(),
                rel_path,
                sha,
                datetime.now(UTC).isoformat(),
                "ok",
                20,
            ),
        )
        deck_doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    return db_path, deck_path, deck_doc_id


def _mock_pdf_text(_pages_text: str):
    def _fake(_path: str) -> str:
        return _pages_text

    return _fake


def _mock_llm(response_rows: list[dict[str, object]]):
    def _fake(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        return json.dumps(response_rows)

    return _fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_extract_persists_strategic_targets_and_forward_statements(
    db_with_deck: tuple[Path, Path, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _deck_path, deck_doc_id = db_with_deck
    monkeypatch.setattr(
        deck_mod, "extract_text_from_pdf", _mock_pdf_text("dummy deck text body")
    )
    monkeypatch.setattr(
        deck_mod,
        "call_llm",
        _mock_llm([
            {
                "target_kind": "revenue",
                "target_value": 50000,
                "target_unit": "USD_M",
                "target_period": "FY2027",
                "target_currency": "USD",
                "narrative_excerpt": "We're targeting $50B in run-rate revenue by FY2027",
                "confidence": 1.0,
            },
            {
                "target_kind": "fcf_margin",
                "target_value": 30,
                "target_unit": "%",
                "target_period": "LT",
                "target_currency": None,
                "narrative_excerpt": "Long-term FCF margin target of 30%",
                "confidence": 1.0,
            },
            {
                "target_kind": "strategic_priority",
                "target_value": None,
                "target_unit": "qualitative",
                "target_period": "LT",
                "target_currency": None,
                "narrative_excerpt": "Expand into emerging markets via local partnerships",
                "confidence": 0.8,
            },
        ]),
    )

    counts = deck_mod.extract_for_ticker(
        "ABNB", db_path, repo_root=tmp_path
    )
    assert counts["decks_found"] == 1
    assert counts["decks_processed"] == 1
    assert counts["rows_inserted"] == 3
    # FY2027 resolves to a date; LT doesn't. So only the revenue row mirrors
    # into forward_looking_statements.
    assert counts["forward_inserted"] == 1
    assert counts["llm_failed"] == 0
    assert counts["parse_failed"] == 0

    conn = sqlite3.connect(str(db_path))
    try:
        strat_rows = conn.execute(
            """
            SELECT target_kind, target_value, target_unit, target_period,
                   target_currency, deck_doc_id
            FROM strategic_targets
            WHERE ticker = 'ABNB'
            ORDER BY target_kind
            """
        ).fetchall()
        assert len(strat_rows) == 3
        kinds = {r[0] for r in strat_rows}
        assert kinds == {"revenue", "fcf_margin", "strategic_priority"}
        # The named-doc deck_doc_id should propagate to every row.
        assert all(r[5] == deck_doc_id for r in strat_rows)

        # Revenue row maps to forward_looking_statements with 2027-12-31.
        fwd = conn.execute(
            """
            SELECT ticker, source_doc_id, kpi_name, target_value, target_period
            FROM forward_looking_statements
            WHERE ticker = 'ABNB'
            """
        ).fetchall()
        assert len(fwd) == 1
        assert fwd[0][2] == "revenue"
        assert float(fwd[0][3]) == 50000.0
        assert "2027" in str(fwd[0][4])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovery_matches_multiple_filename_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate _is_deck_file matches ir_presentation, ir_investor_update,
    ir_event, *deck*, *investor*, *analyst_day* — and ignores transcripts."""
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
    finally:
        conn.close()

    ir_dir = repo / "ir_documents" / "GOOG" / "2025-03-31"
    ir_dir.mkdir(parents=True)
    (ir_dir / "ir_presentation__a.pdf").write_bytes(b"%PDF")
    (ir_dir / "ir_event__b.pdf").write_bytes(b"%PDF")
    (ir_dir / "ir_transcript__c.pdf").write_bytes(b"%PDF")  # should be SKIPPED
    (ir_dir / "ir_press_release__d.pdf").write_bytes(b"%PDF")  # should be SKIPPED
    # Special-folder rule.
    ad_dir = repo / "ir_documents" / "GOOG" / "analyst_day"
    ad_dir.mkdir(parents=True)
    (ad_dir / "any_name.pdf").write_bytes(b"%PDF")
    # Loose "Investor Day" deck per spec.
    (ir_dir / "GOOG_2025_Investor_Day.pdf").write_bytes(b"%PDF")

    monkeypatch.setattr(deck_mod, "extract_text_from_pdf", _mock_pdf_text("dummy"))
    monkeypatch.setattr(deck_mod, "call_llm", _mock_llm([]))

    counts = deck_mod.extract_for_ticker("GOOG", db_path, repo_root=repo)
    # 4 deck-shaped files (ir_presentation, ir_event, analyst_day/any_name,
    # GOOG_2025_Investor_Day), transcript + press_release skipped.
    assert counts["decks_found"] == 4
    # Empty LLM response → 0 rows.
    assert counts["rows_inserted"] == 0


# ---------------------------------------------------------------------------
# Idempotency / dedupe
# ---------------------------------------------------------------------------


def test_dedupe_via_unique_index_on_rerun(
    db_with_deck: tuple[Path, Path, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second extract on the same deck inserts 0 new rows: the existing-rows
    short-circuit skips the LLM, and even if forced, the unique index drops dupes."""
    db_path, _deck_path, _deck_doc_id = db_with_deck
    monkeypatch.setattr(deck_mod, "extract_text_from_pdf", _mock_pdf_text("dummy"))
    monkeypatch.setattr(
        deck_mod,
        "call_llm",
        _mock_llm([{
            "target_kind": "revenue",
            "target_value": 50000,
            "target_unit": "USD_M",
            "target_period": "FY2027",
            "target_currency": "USD",
            "narrative_excerpt": "Revenue target of $50B by FY2027",
            "confidence": 1.0,
        }]),
    )

    out1 = deck_mod.extract_for_ticker("ABNB", db_path, repo_root=tmp_path)
    out2 = deck_mod.extract_for_ticker("ABNB", db_path, repo_root=tmp_path)
    out3 = deck_mod.extract_for_ticker(
        "ABNB", db_path, repo_root=tmp_path, force_refresh=True
    )

    assert out1["rows_inserted"] == 1
    # Second run skips the LLM entirely because rows already exist for the
    # deck_doc_id — counts decks_found but not decks_processed.
    assert out2["decks_processed"] == 0
    assert out2["rows_inserted"] == 0
    # Forced run re-calls the LLM but the unique index drops the duplicate.
    assert out3["decks_processed"] == 1
    assert out3["rows_inserted"] == 0


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_no_decks_found_returns_zero_counts(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
    finally:
        conn.close()
    counts = deck_mod.extract_for_ticker("NU", db_path, repo_root=repo)
    assert counts == {
        "decks_found": 0,
        "decks_processed": 0,
        "rows_inserted": 0,
        "forward_inserted": 0,
        "llm_failed": 0,
        "parse_failed": 0,
    }


def test_llm_failure_counted_not_raised(
    db_with_deck: tuple[Path, Path, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _, _ = db_with_deck
    monkeypatch.setattr(deck_mod, "extract_text_from_pdf", _mock_pdf_text("text"))

    def _boom(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(deck_mod, "call_llm", _boom)
    counts = deck_mod.extract_for_ticker("ABNB", db_path, repo_root=tmp_path)
    assert counts["llm_failed"] == 1
    assert counts["rows_inserted"] == 0


def test_parse_failure_counted(
    db_with_deck: tuple[Path, Path, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _, _ = db_with_deck
    monkeypatch.setattr(deck_mod, "extract_text_from_pdf", _mock_pdf_text("text"))

    def _bad(prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        return "this is not JSON at all"

    monkeypatch.setattr(deck_mod, "call_llm", _bad)
    counts = deck_mod.extract_for_ticker("ABNB", db_path, repo_root=tmp_path)
    assert counts["parse_failed"] == 1
    assert counts["rows_inserted"] == 0


def test_unknown_target_kind_skipped(
    db_with_deck: tuple[Path, Path, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _, _ = db_with_deck
    monkeypatch.setattr(deck_mod, "extract_text_from_pdf", _mock_pdf_text("text"))
    monkeypatch.setattr(
        deck_mod,
        "call_llm",
        _mock_llm([
            {
                "target_kind": "made_up_kind",  # not in _VALID_KINDS
                "target_value": 1,
                "target_unit": "%",
                "target_period": "FY2027",
                "target_currency": None,
                "narrative_excerpt": "Some bogus target",
                "confidence": 1.0,
            },
            {
                "target_kind": "oi_margin",  # valid
                "target_value": 25,
                "target_unit": "%",
                "target_period": "FY2027",
                "target_currency": None,
                "narrative_excerpt": "OI margin target of 25% by FY2027",
                "confidence": 1.0,
            },
        ]),
    )
    counts = deck_mod.extract_for_ticker("ABNB", db_path, repo_root=tmp_path)
    assert counts["rows_inserted"] == 1


# ---------------------------------------------------------------------------
# Period resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,expected_year,expected_month",
    [
        ("FY2027", 2027, 12),
        ("2030", 2030, 12),
        ("by 2028", 2028, 12),
        ("Q4 2025", 2025, 12),
        ("Q1 FY2026", 2026, 3),
    ],
)
def test_resolve_target_period_to_date(
    period: str, expected_year: int, expected_month: int
) -> None:
    dt = deck_mod._resolve_target_period_to_date(period)
    assert dt is not None
    assert dt.year == expected_year
    assert dt.month == expected_month


@pytest.mark.parametrize("period", ["LT", "long-term", "next 5 years", "QoQ", ""])
def test_resolve_unparseable_periods_returns_none(period: str) -> None:
    assert deck_mod._resolve_target_period_to_date(period) is None


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def test_coerce_target_value_handles_pct_string(monkeypatch: pytest.MonkeyPatch) -> None:
    assert deck_mod._coerce_target_value("30%") == 30.0
    assert deck_mod._coerce_target_value("2,500") == 2500.0
    assert deck_mod._coerce_target_value(None) is None
    assert deck_mod._coerce_target_value("not a number") is None
    assert deck_mod._coerce_target_value(True) is None  # bool guard


# ---------------------------------------------------------------------------
# Bear case anchor injection
# ---------------------------------------------------------------------------


def test_bear_case_anchor_includes_strategic_targets_block(
    tmp_path: Path,
) -> None:
    """When strategic_targets has rows for a ticker, _ticker_specific_md
    prepends a '## Strategic Targets' bulleted block."""
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
        conn.execute(
            """
            INSERT INTO strategic_targets
                (ticker, deck_doc_id, target_kind, target_value, target_unit,
                 target_period, target_currency, narrative_excerpt,
                 extracted_at, confidence)
            VALUES
                ('ABNB', NULL, 'revenue', 50000, 'USD_M', 'FY2027', 'USD',
                 'Revenue target of $50B by FY2027', ?, 1.0),
                ('ABNB', NULL, 'fcf_margin', 30, '%', 'LT', NULL,
                 'Long-term FCF margin target of 30%', ?, 1.0),
                ('ABNB', NULL, 'strategic_priority', NULL, 'qualitative', 'LT',
                 NULL, 'Expand into emerging markets via local partnerships',
                 ?, 0.8)
            """,
            (
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    md = _ticker_specific_md("ABNB", repo)
    assert "## Strategic Targets" in md
    assert "**revenue**" in md
    assert "50000 USD_M" in md
    assert "by FY2027" in md
    assert "**fcf_margin**" in md
    assert "30%" in md
    # Qualitative row should appear without a value
    assert "**strategic_priority**" in md
    assert "Expand into emerging markets" in md


def test_bear_case_anchor_empty_when_no_rows(tmp_path: Path) -> None:
    """When the ticker has no strategic_targets rows AND no ticker_specific
    JSONs, the anchor is empty (preserves prior behavior)."""
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
    finally:
        conn.close()
    assert _ticker_specific_md("ABNB", repo) == ""


def test_bear_case_anchor_degrades_when_db_missing(tmp_path: Path) -> None:
    """A repo without portfolio.db (fresh checkout, pre-migration) shouldn't
    crash the bear case build — the strategic_targets block silently empties."""
    repo = tmp_path
    assert _strategic_targets_md("ABNB", repo) == ""


def test_bear_case_anchor_combines_with_ticker_specific_json(
    tmp_path: Path,
) -> None:
    """Both sources contribute to the final anchor: targets block first,
    then ticker_specific JSONs."""
    repo = tmp_path
    (repo / "data").mkdir()
    db_path = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _schema(conn)
        conn.execute(
            """
            INSERT INTO strategic_targets
                (ticker, deck_doc_id, target_kind, target_value, target_unit,
                 target_period, target_currency, narrative_excerpt,
                 extracted_at, confidence)
            VALUES
                ('NVO', NULL, 'capex_intent', 25000, 'DKK_M', 'FY2027', 'DKK',
                 'Capex of DKK 25B through 2027', ?, 1.0)
            """,
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()

    # Add a ticker_specific JSON.
    feature_dir = repo / "data" / "ticker_specific" / "NVO"
    feature_dir.mkdir(parents=True)
    (feature_dir / "patent_timeline.json").write_text('{"semaglutide": "2033"}')

    md = _ticker_specific_md("NVO", repo)
    assert "## Strategic Targets" in md
    assert "capex_intent" in md
    assert "patent_timeline" in md
    # Targets block precedes the JSON dump.
    assert md.index("## Strategic Targets") < md.index("patent_timeline")
