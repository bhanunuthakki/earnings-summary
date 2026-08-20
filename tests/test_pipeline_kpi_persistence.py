"""Tests for src/pipeline/kpi_persistence.py — KPI manifest validation, persistence, validation_issues."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from models.documents import SourceType
from models.facts import Currency, FactLocator, FiscalPeriodType, Unit
from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import (
    KpiExtractionManifest,
    KpiValue,
    PersistResult,
    find_or_create_kpi_definition,
    guard_llm_extracted_parent,
    persist_manifest,
    purge_duplicate_kpi_facts,
    reconcile_unit,
    record_validation_issue,
)

_KPI_FACTS_LOGICAL_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_logical "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id)"
)
_KPI_FACTS_PROVENANCE_UNIQUE = (
    "CREATE UNIQUE INDEX uq_kpi_facts_provenance "
    "ON kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, source_doc_id)"
)

# KpiValue.locator is required (persist-time enforcement, docs/design/
# provenance_clickthrough.md §4.1) -- these fixtures exercise persist_manifest's
# validation/restatement/definition-origin logic, not provenance rendering. A
# trivial real FactLocator (not a LegacyEscapeHatch) is used deliberately: an
# escape hatch logs its own validation_issues row on every successful insert
# (see pipeline.locators.resolve_locator_for_persist), which would silently
# inflate the exact validation_issue counts/rows many tests below assert on.
_NO_LOCATOR = FactLocator(pdf_page=1)


def _create_schema(conn: sqlite3.Connection, *, legacy_logical_unique: bool = False) -> None:
    """Build the kpi_* + validation_issues tables.

    Default mirrors post-0059 prod (wider `uq_kpi_facts_provenance` + the
    audit columns from 0054). `legacy_logical_unique=True` rebuilds the
    short-lived post-0030 / pre-0059 narrow constraint to exercise the
    detector's schema-tolerance fallback path."""
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL,
            primary_source TEXT NOT NULL,
            fallback_source TEXT,
            ir_url TEXT,
            threshold_tier TEXT,
            threshold_low FLOAT,
            threshold_high FLOAT,
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
            source_doc_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER
        );
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            source_doc_id INTEGER,
            ticker TEXT,
            severity TEXT NOT NULL,
            rule TEXT NOT NULL,
            raw_value TEXT,
            expected TEXT,
            raised_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        );
        """
    )
    conn.execute(
        _KPI_FACTS_LOGICAL_UNIQUE if legacy_logical_unique else _KPI_FACTS_PROVENANCE_UNIQUE
    )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c)
    return c


@pytest.fixture
def legacy_conn() -> sqlite3.Connection:
    """Pre-0059 schema: the narrow logical-only unique forbids multi-row
    per logical key. Used to validate that the purge backfill still works
    (and to exercise the detector's schema-tolerance fallback)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _create_schema(c, legacy_logical_unique=True)
    return c


def test_find_or_create_kpi_definition_inserts_when_missing(conn: sqlite3.Connection) -> None:
    """First call inserts; second call returns the same id."""
    id1 = find_or_create_kpi_definition(
        conn,
        ticker="MELI",
        name="Revenue Growth (FXN)",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
    )
    id2 = find_or_create_kpi_definition(
        conn,
        ticker="MELI",
        name="Revenue Growth (FXN)",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
    )
    assert id1 == id2
    assert id1 > 0


def test_persist_manifest_inserts_kpi_facts(conn: sqlite3.Connection) -> None:
    """End-to-end: manifest in, kpi_facts rows out."""
    manifest = KpiExtractionManifest(
        ticker="MELI",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=42,
        primary_source=SourceType.IR_DOC,
        values=[
            KpiValue(
                name="Revenue Growth (FXN)",
                value=Decimal("96"),
                unit=Unit.PERCENT,
                locator=_NO_LOCATOR,
            ),
            KpiValue(
                name="GMV Growth (FXN)", value=Decimal("56"), unit=Unit.PERCENT, locator=_NO_LOCATOR
            ),
        ],
    )
    result = persist_manifest(conn, run_id="r1", manifest=manifest)
    assert isinstance(result, PersistResult)
    assert result.inserted == 2
    assert result.skipped_existing == 0
    assert result.validation_issues == 0

    rows = conn.execute("SELECT ticker, value, unit FROM kpi_facts").fetchall()
    assert len(rows) == 2
    values = {Decimal(str(dict(r)["value"])) for r in rows}
    assert values == {Decimal("96"), Decimal("56")}


def test_persist_manifest_dedupes_on_rerun(conn: sqlite3.Connection) -> None:
    """Re-running the same manifest is a no-op (UNIQUE index dedupes)."""
    manifest = KpiExtractionManifest(
        ticker="MELI",
        period_end=datetime(2024, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=42,
        values=[
            KpiValue(name="OpMargin", value=Decimal("13.5"), unit=Unit.PERCENT, locator=_NO_LOCATOR)
        ],
    )
    persist_manifest(conn, run_id="r1", manifest=manifest)
    second = persist_manifest(conn, run_id="r2", manifest=manifest)
    assert second.inserted == 0
    assert second.skipped_existing == 1


def test_persist_manifest_emits_validation_issue_on_out_of_range(conn: sqlite3.Connection) -> None:
    """A nonsense PERCENT (e.g. 5000) is rejected, validation_issue recorded, kpi_fact NOT inserted."""
    manifest = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2025, 12, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=99,
        values=[
            KpiValue(
                name="Activity Rate", value=Decimal("83"), unit=Unit.PERCENT, locator=_NO_LOCATOR
            ),
            KpiValue(name="Bogus", value=Decimal("5000"), unit=Unit.PERCENT, locator=_NO_LOCATOR),
        ],
    )
    result = persist_manifest(conn, run_id="r1", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 1

    issues = conn.execute(
        "SELECT severity, rule, raw_value, ticker FROM validation_issues"
    ).fetchall()
    assert len(issues) == 1
    issue = dict(issues[0])
    assert issue["severity"] == Severity.WARN.value
    assert issue["rule"] == ValidationRule.PLAUSIBLE_RANGE.value
    assert issue["ticker"] == "NU"


def test_record_validation_issue_inserts_row(conn: sqlite3.Connection) -> None:
    """Direct call to record_validation_issue writes a row with the given fields."""
    issue_id = record_validation_issue(
        conn,
        run_id="run-x",
        source_doc_id=100,
        ticker="GOOG",
        severity=Severity.HALT,
        rule=ValidationRule.SOURCE_DISAGREEMENT,
        raw_value="fmp=10, sec=11",
        expected="diff < 0.5%",
    )
    assert issue_id > 0
    row = conn.execute("SELECT * FROM validation_issues WHERE id = ?", (issue_id,)).fetchone()
    assert dict(row)["severity"] == "halt"
    assert dict(row)["rule"] == "source_disagreement"


# ---------------------------------------------------------------------------
# Cumulative-series sanity guard (red-team PR2 item 4,
# directives/monthly_red_team.md Phase 1 "KPI series sanity"). "Total
# customers" is the explicit allowlist marker (pipeline.kpi_persistence.
# _CUMULATIVE_KPI_NAME_MARKERS) — matches "Total customers", "Total customers
# (millions)", etc. via the normalized-name substring check.
# ---------------------------------------------------------------------------


def _customers_manifest(
    *, period_end: datetime, value: Decimal, source_doc_id: int, ticker: str = "NU"
) -> KpiExtractionManifest:
    return KpiExtractionManifest(
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=source_doc_id,
        values=[
            KpiValue(
                name="Total customers (millions)", value=value, unit=Unit.COUNT, locator=_NO_LOCATOR
            )
        ],
    )


def test_persist_manifest_allows_normal_cumulative_growth(conn: sqlite3.Connection) -> None:
    """A cumulative KPI that grows quarter over quarter is unaffected by the guard."""
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_customers_manifest(
            period_end=datetime(2025, 3, 31), value=Decimal("100"), source_doc_id=1
        ),
    )
    result = persist_manifest(
        conn,
        run_id="r2",
        manifest=_customers_manifest(
            period_end=datetime(2025, 6, 30), value=Decimal("110"), source_doc_id=2
        ),
    )
    assert result.inserted == 1
    assert result.validation_issues == 0


def test_persist_manifest_rejects_non_monotonic_cumulative_kpi(conn: sqlite3.Connection) -> None:
    """A later print with a LOWER value than its prior print is rejected — a
    cumulative series should never decrease. NEVER guess-fixed; the row is
    skipped and a NON_MONOTONIC_CUMULATIVE validation_issue is raised."""
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_customers_manifest(
            period_end=datetime(2025, 3, 31), value=Decimal("119"), source_doc_id=1
        ),
    )
    result = persist_manifest(
        conn,
        run_id="r2",
        manifest=_customers_manifest(
            period_end=datetime(2025, 6, 30), value=Decimal("95"), source_doc_id=2
        ),
    )
    assert result.inserted == 0
    assert result.validation_issues == 1
    issue = dict(conn.execute("SELECT rule, severity, expected FROM validation_issues").fetchone())
    assert issue["rule"] == ValidationRule.NON_MONOTONIC_CUMULATIVE.value
    assert issue["severity"] == Severity.WARN.value
    assert "non-monotonic" in issue["expected"].lower()
    # And the bad value never landed in kpi_facts.
    rows = conn.execute("SELECT value FROM kpi_facts").fetchall()
    assert [float(dict(r)["value"]) for r in rows] == [119.0]


def test_persist_manifest_rejects_unit_jump_cumulative_kpi(conn: sqlite3.Connection) -> None:
    """A raw-count row landing inside a millions-scale series (>1000x jump) —
    the exact NU 'Total customers' def 641 corruption the red-team audit
    found — is rejected as a MAGNITUDE_JUMP, never silently stored."""
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_customers_manifest(
            period_end=datetime(2025, 3, 31), value=Decimal("119"), source_doc_id=1
        ),
    )
    result = persist_manifest(
        conn,
        run_id="r2",
        manifest=_customers_manifest(
            period_end=datetime(2025, 6, 30), value=Decimal("114000000"), source_doc_id=2
        ),
    )
    assert result.inserted == 0
    assert result.validation_issues == 1
    issue = dict(conn.execute("SELECT rule, expected FROM validation_issues").fetchone())
    assert issue["rule"] == ValidationRule.MAGNITUDE_JUMP.value
    assert "unit discontinuity" in issue["expected"].lower()


def test_persist_manifest_guard_checks_backfill_against_later_print_too(
    conn: sqlite3.Connection,
) -> None:
    """Backfilling an EARLIER period that would sit ABOVE an already-stored
    LATER print is also rejected — the guard checks both neighbors, not just
    'is this newer than the latest row'."""
    persist_manifest(
        conn,
        run_id="r1",
        manifest=_customers_manifest(
            period_end=datetime(2025, 6, 30), value=Decimal("110"), source_doc_id=1
        ),
    )
    # Backfilling Q1 with a value HIGHER than the already-stored Q2 is non-monotonic.
    result = persist_manifest(
        conn,
        run_id="r2",
        manifest=_customers_manifest(
            period_end=datetime(2025, 3, 31), value=Decimal("115"), source_doc_id=2
        ),
    )
    assert result.inserted == 0
    assert result.validation_issues == 1
    issue = dict(conn.execute("SELECT rule FROM validation_issues").fetchone())
    assert issue["rule"] == ValidationRule.NON_MONOTONIC_CUMULATIVE.value


def test_cumulative_guard_scoped_to_marked_kpis_only(conn: sqlite3.Connection) -> None:
    """A non-cumulative KPI (not on the allowlist) that decreases QoQ is
    unaffected — the guard only applies to KPIs matching the cumulative
    markers (e.g. "Total customers"), never a blanket monotonicity rule."""
    manifest1 = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2025, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=1,
        values=[
            KpiValue(
                name="Monthly ARPAC (USD)",
                value=Decimal("12"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=_NO_LOCATOR,
            )
        ],
    )
    manifest2 = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2025, 6, 30),
        fiscal_period_type=FiscalPeriodType.Q2,
        source_doc_id=2,
        values=[
            KpiValue(
                name="Monthly ARPAC (USD)",
                value=Decimal("9"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=_NO_LOCATOR,
            )
        ],
    )
    persist_manifest(conn, run_id="r1", manifest=manifest1)
    result = persist_manifest(conn, run_id="r2", manifest=manifest2)
    assert result.inserted == 1
    assert result.validation_issues == 0


def test_kpi_value_rejects_invalid_confidence() -> None:
    """KpiValue.confidence must be in [0, 1]."""
    with pytest.raises(ValueError):
        KpiValue(
            name="x",
            value=Decimal("1"),
            unit=Unit.PERCENT,
            confidence=1.5,
            locator=_NO_LOCATOR,
        )


def test_kpi_value_rejects_empty_name() -> None:
    """KpiValue.name must be non-empty."""
    with pytest.raises(ValueError):
        KpiValue(name="", value=Decimal("1"), unit=Unit.PERCENT, locator=_NO_LOCATOR)


def test_persist_manifest_keeps_both_rows_with_different_source_doc_id(
    conn: sqlite3.Connection,
) -> None:
    """Post-0059: different source_doc_ids for the same logical key both
    survive in kpi_facts under `uq_kpi_facts_provenance`. The newer-id
    row is the loader's canonical pick (tier+id DESC dedup), while the
    older row stays for `--as-of-date` time-travel. Mirrors the RBRK
    Revenue YoY case (source_doc_id 9676 → 9705): both rows now persist,
    replacing the pre-0059 in-place-overwrite semantic."""
    first = KpiExtractionManifest(
        ticker="RBRK",
        period_end=datetime(2026, 1, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=9676,
        values=[
            KpiValue(
                name="Revenue YoY Growth (USD)",
                value=Decimal("46.33"),
                unit=Unit.PERCENT,
                locator=_NO_LOCATOR,
            )
        ],
    )
    later = first.model_copy(
        update={
            "source_doc_id": 9705,
            "values": [
                KpiValue(
                    name="Revenue YoY Growth (USD)",
                    value=Decimal("47.10"),
                    unit=Unit.PERCENT,
                    locator=_NO_LOCATOR,
                )
            ],
        }
    )

    first_result = persist_manifest(conn, run_id="run-a", manifest=first)
    second_result = persist_manifest(conn, run_id="run-b", manifest=later)

    assert first_result.inserted == 1
    assert second_result.inserted == 1
    assert second_result.skipped_existing == 0

    rows = conn.execute(
        "SELECT value, source_doc_id FROM kpi_facts WHERE ticker = 'RBRK' "
        "ORDER BY source_doc_id ASC"
    ).fetchall()
    assert len(rows) == 2
    values = [(Decimal(str(dict(r)["value"])), dict(r)["source_doc_id"]) for r in rows]
    assert values == [(Decimal("46.33"), 9676), (Decimal("47.10"), 9705)]


def test_persist_manifest_keeps_both_rows_when_older_source_doc_id_replayed(
    conn: sqlite3.Connection,
) -> None:
    """Post-0059: replaying an OLDER source_doc_id after a newer one already
    landed still inserts a new row — the writer doesn't gatekeep on
    document recency. The loader's tier+id DESC dedup is what guarantees
    the newer row stays canonical; the older replay never displaces it."""
    newer = KpiExtractionManifest(
        ticker="RBRK",
        period_end=datetime(2026, 1, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=9705,
        values=[
            KpiValue(
                name="Revenue YoY Growth (USD)",
                value=Decimal("47.10"),
                unit=Unit.PERCENT,
                locator=_NO_LOCATOR,
            )
        ],
    )
    older = newer.model_copy(
        update={
            "source_doc_id": 9676,
            "values": [
                KpiValue(
                    name="Revenue YoY Growth (USD)",
                    value=Decimal("46.33"),
                    unit=Unit.PERCENT,
                    locator=_NO_LOCATOR,
                )
            ],
        }
    )

    persist_manifest(conn, run_id="run-a", manifest=newer)
    result = persist_manifest(conn, run_id="run-b", manifest=older)

    # Different source_doc_id → no UNIQUE conflict → new row written.
    assert result.inserted == 1
    assert result.skipped_existing == 0

    rows = conn.execute(
        "SELECT value, source_doc_id FROM kpi_facts WHERE ticker = 'RBRK' "
        "ORDER BY source_doc_id ASC"
    ).fetchall()
    assert len(rows) == 2
    # max(source_doc_id) row carries the newer value — the loader's natural pick.
    assert dict(rows[-1])["source_doc_id"] == 9705
    assert Decimal(str(dict(rows[-1])["value"])) == Decimal("47.10")


def test_purge_duplicate_kpi_facts_keeps_max_source_doc_id(conn: sqlite3.Connection) -> None:
    """Backfill purge keeps exactly one row per (ticker, period_end,
    fiscal_period_type, kpi_definition_id) — the one with the highest
    source_doc_id (most-recently-ingested document). Mirrors the prod
    RBRK Revenue YoY case (source_doc_id 9676 vs 9705 → keep 9705).

    The purge is now mostly dead code — post-0059 the wide
    `uq_kpi_facts_provenance` constraint allows duplicates and the
    loader's tier+id DESC dedup handles collapsing them at read time —
    but the helper is kept as a defensive utility and this test
    documents the contract."""
    kpi_def_id = find_or_create_kpi_definition(
        conn,
        ticker="RBRK",
        name="Revenue YoY Growth (USD)",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
    )

    rows = [
        # Two source_doc_ids for the same logical (RBRK, 2026-01-31, Q4, Revenue YoY)
        ("RBRK", datetime(2026, 1, 31), "Q4", kpi_def_id, "46.33", "percent", 9676),
        ("RBRK", datetime(2026, 1, 31), "Q4", kpi_def_id, "46.33", "percent", 9705),
        # Three for an older quarter to ensure the purge generalises beyond pairs
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9600),
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9676),
        ("RBRK", datetime(2025, 10, 31), "Q3", kpi_def_id, "48.26", "percent", 9705),
        # A row that should be left alone (no duplicates)
        ("RBRK", datetime(2025, 7, 31), "Q2", kpi_def_id, "51.19", "percent", 9705),
    ]
    conn.executemany(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 6

    deleted = purge_duplicate_kpi_facts(conn)
    conn.commit()
    assert deleted == 3  # 1 from the Q4 pair + 2 from the Q3 triple

    survivors = conn.execute(
        "SELECT period_end, source_doc_id FROM kpi_facts ORDER BY period_end DESC"
    ).fetchall()
    assert [(str(dict(r)["period_end"]), dict(r)["source_doc_id"]) for r in survivors] == [
        ("2026-01-31 00:00:00", 9705),
        ("2025-10-31 00:00:00", 9705),
        ("2025-07-31 00:00:00", 9705),
    ]


def test_purge_duplicate_kpi_facts_is_idempotent(conn: sqlite3.Connection) -> None:
    """Calling purge on an already-clean table is a no-op (returns 0)."""
    kpi_def_id = find_or_create_kpi_definition(
        conn,
        ticker="MELI",
        name="Revenue Growth (FXN)",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
    )
    conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("MELI", datetime(2025, 12, 31), "Q4", kpi_def_id, "96.0", "percent", 100),
    )
    conn.commit()

    assert purge_duplicate_kpi_facts(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM kpi_facts").fetchone()[0] == 1


# --- Canonical-unit reconciliation (source-side unit fix) -------------------
#
# `KpiExtractionManifest.canonical_units` carries the authoritative unit a metric
# should be stored in (the holding's break-rule unit). persist_manifest reconciles
# the extracted value/unit to it so kpi_facts stops being stored in the LLM's
# per-call unit guess — the root cause of the RBRK "115_000_000 actual vs <80
# millions" dead break-rule.


def test_reconcile_unit_passthrough_and_rescale_and_mismatch() -> None:
    """The helper: passthrough when no/equal canonical, rescale within a family,
    keep-original-and-flag across families."""
    # No canonical → unchanged.
    assert reconcile_unit(Decimal("5"), Unit.ACTUAL, None) == (Decimal("5"), Unit.ACTUAL, False)
    # Canonical equals extracted → unchanged (no needless conversion).
    assert reconcile_unit(Decimal("17.8"), Unit.PERCENT, Unit.PERCENT) == (
        Decimal("17.8"),
        Unit.PERCENT,
        False,
    )
    # Same family, different unit → rescaled into the canonical unit.
    assert reconcile_unit(Decimal("115000000"), Unit.ACTUAL, Unit.MILLIONS) == (
        Decimal("115"),
        Unit.MILLIONS,
        False,
    )
    # Cross family → original kept, flagged.
    assert reconcile_unit(Decimal("123456"), Unit.ACTUAL, Unit.PERCENT) == (
        Decimal("123456"),
        Unit.ACTUAL,
        True,
    )


def test_persist_manifest_reconciles_actual_to_canonical_millions(conn: sqlite3.Connection) -> None:
    """The RBRK case: the LLM reports net-new ARR as the full figure in `actual`,
    but the break-rule threshold is in `millions`. The fact must land in millions
    (115), and the freshly-created definition must carry `millions` too."""
    manifest = KpiExtractionManifest(
        ticker="RBRK",
        period_end=datetime(2027, 1, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=7140,
        primary_source=SourceType.LLM_EXTRACTED,
        canonical_units={"Net new subscription ARR ($)": Unit.MILLIONS},
        values=[
            KpiValue(
                name="Net new subscription ARR ($)",
                value=Decimal("115000000"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=_NO_LOCATOR,
            )
        ],
    )
    result = persist_manifest(conn, run_id="r1", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 0

    fact = conn.execute("SELECT value, unit FROM kpi_facts").fetchone()
    assert Decimal(str(dict(fact)["value"])) == Decimal("115")
    assert dict(fact)["unit"] == "millions"
    definition = conn.execute("SELECT unit FROM kpi_definitions").fetchone()
    assert dict(definition)["unit"] == "millions"


def test_persist_manifest_corrects_existing_wrong_definition_unit(conn: sqlite3.Connection) -> None:
    """NU's "Capital adequacy ratio" def is `ratio` in prod though every fact and
    the break-rule are `percent`. An authoritative canonical corrects the
    definition's unit WITHOUT corrupting the (already-correct) percent value —
    the danger of naively reconciling to the existing definition unit instead."""
    name = "Capital adequacy ratio (CET1 / Basel III total)"
    wrong_def_id = find_or_create_kpi_definition(
        conn,
        ticker="NU",
        name=name,
        unit=Unit.RATIO,
        primary_source=SourceType.IR_DOC,
    )
    conn.commit()

    manifest = KpiExtractionManifest(
        ticker="NU",
        period_end=datetime(2026, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=10,
        primary_source=SourceType.LLM_EXTRACTED,
        canonical_units={name: Unit.PERCENT},
        values=[KpiValue(name=name, value=Decimal("17.8"), unit=Unit.PERCENT, locator=_NO_LOCATOR)],
    )
    result = persist_manifest(conn, run_id="r2", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 0

    fact = conn.execute("SELECT value, unit FROM kpi_facts").fetchone()
    assert Decimal(str(dict(fact)["value"])) == Decimal("17.8")  # value untouched
    assert dict(fact)["unit"] == "percent"
    corrected = conn.execute(
        "SELECT unit FROM kpi_definitions WHERE id = ?", (wrong_def_id,)
    ).fetchone()
    assert dict(corrected)["unit"] == "percent"  # ratio -> percent


def test_persist_manifest_flags_cross_family_canonical_and_keeps_original(
    conn: sqlite3.Connection,
) -> None:
    """A canonical unit in a different family than the extracted unit can't be
    applied: the value is persisted as-extracted (never rescaled across
    dimensions) and a UNIT_MISMATCH validation issue is recorded."""
    manifest = KpiExtractionManifest(
        ticker="XX",
        period_end=datetime(2026, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=1,
        primary_source=SourceType.LLM_EXTRACTED,
        canonical_units={"Some Margin": Unit.PERCENT},
        values=[
            KpiValue(
                name="Some Margin",
                value=Decimal("123456"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=_NO_LOCATOR,
            )
        ],
    )
    result = persist_manifest(conn, run_id="r3", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 1

    fact = conn.execute("SELECT value, unit FROM kpi_facts").fetchone()
    assert dict(fact)["unit"] == "actual"  # kept original, NOT rescaled to percent
    issue = conn.execute("SELECT rule, ticker FROM validation_issues").fetchone()
    assert dict(issue)["rule"] == ValidationRule.UNIT_MISMATCH.value
    # A different-family def is NOT authoritative: it must not overwrite the def.
    definition = conn.execute("SELECT unit FROM kpi_definitions").fetchone()
    assert dict(definition)["unit"] == "actual"


def test_persist_manifest_no_canonical_is_unchanged_passthrough(conn: sqlite3.Connection) -> None:
    """Generic callers (IR spreadsheet / PDF) pass no canonical_units — the
    extracted value/unit are stored verbatim, exactly as before this change."""
    manifest = KpiExtractionManifest(
        ticker="YY",
        period_end=datetime(2026, 3, 31),
        fiscal_period_type=FiscalPeriodType.Q1,
        source_doc_id=1,
        primary_source=SourceType.IR_DOC,
        values=[
            KpiValue(
                name="GMV",
                value=Decimal("5000000000"),
                unit=Unit.ACTUAL,
                currency=Currency.USD,
                locator=_NO_LOCATOR,
            )
        ],
    )
    result = persist_manifest(conn, run_id="r4", manifest=manifest)
    assert result.inserted == 1
    assert result.validation_issues == 0
    fact = conn.execute("SELECT value, unit FROM kpi_facts").fetchone()
    assert Decimal(str(dict(fact)["value"])) == Decimal("5000000000")
    assert dict(fact)["unit"] == "actual"


def test_find_or_create_definition_authoritative_flag_gates_unit_correction(
    conn: sqlite3.Connection,
) -> None:
    """The unit of an EXISTING definition is only rewritten when the caller
    declares the new unit authoritative; the default leaves it first-writer-wins."""
    def_id = find_or_create_kpi_definition(
        conn,
        ticker="ZZ",
        name="Metric",
        unit=Unit.RATIO,
        primary_source=SourceType.IR_DOC,
    )
    # Non-authoritative lookup with a different unit must NOT change the row.
    same = find_or_create_kpi_definition(
        conn,
        ticker="ZZ",
        name="Metric",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
    )
    assert same == def_id
    row = conn.execute("SELECT unit FROM kpi_definitions WHERE id = ?", (def_id,)).fetchone()
    assert dict(row)["unit"] == "ratio"
    # Authoritative lookup reconciles the row to the canonical unit.
    again = find_or_create_kpi_definition(
        conn,
        ticker="ZZ",
        name="Metric",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
        authoritative=True,
    )
    assert again == def_id
    row = conn.execute("SELECT unit FROM kpi_definitions WHERE id = ?", (def_id,)).fetchone()
    assert dict(row)["unit"] == "percent"


def test_persist_manifest_falls_back_on_legacy_logical_unique(
    legacy_conn: sqlite3.Connection,
) -> None:
    """Schema-tolerance regression: against a pre-0059 fixture that still
    has the narrow `uq_kpi_facts_logical`, the detector's INSERT OR
    IGNORE collapses a different-source replay (the legacy semantic).
    Two manifests for the same logical key but different source_doc_ids
    yield one row, not two — the wider provenance constraint isn't
    active so the unique conflict fires on the logical tuple alone."""
    first = KpiExtractionManifest(
        ticker="RBRK",
        period_end=datetime(2026, 1, 31),
        fiscal_period_type=FiscalPeriodType.Q4,
        source_doc_id=9676,
        values=[
            KpiValue(
                name="Revenue YoY Growth (USD)",
                value=Decimal("46.33"),
                unit=Unit.PERCENT,
                locator=_NO_LOCATOR,
            )
        ],
    )
    later = first.model_copy(update={"source_doc_id": 9705})

    persist_manifest(legacy_conn, run_id="run-a", manifest=first)
    result = persist_manifest(legacy_conn, run_id="run-b", manifest=later)

    # Under narrow logical unique, different source_doc_id same logical key
    # → conflict on (ticker, period_end, fiscal_period_type, kpi_def_id)
    # → INSERT OR IGNORE = 0 rows written.
    assert result.inserted == 0
    assert result.skipped_existing == 1

    rows = legacy_conn.execute("SELECT COUNT(*) FROM kpi_facts WHERE ticker = 'RBRK'").fetchone()
    assert rows[0] == 1


# --- guard_llm_extracted_parent (data_provenance.md §2 write-path check) -----


def test_guard_noop_when_not_llm_extracted(conn: sqlite3.Connection) -> None:
    guard_llm_extracted_parent(
        conn,
        source_type=SourceType.IR_DOC,
        parent_document_id=None,
        ticker="NU",
        doc_id=1,
    )
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 0


def test_guard_noop_when_parent_already_set(conn: sqlite3.Connection) -> None:
    guard_llm_extracted_parent(
        conn,
        source_type=SourceType.LLM_EXTRACTED,
        parent_document_id=42,
        ticker="NU",
        doc_id=1,
    )
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 0


def test_guard_logs_validation_issue_by_default(conn: sqlite3.Connection) -> None:
    """Production write paths (strict=False, the default) degrade: log + continue,
    matching this module's existing quarantine philosophy — no batch run is ever
    aborted over one row's provenance gap."""
    guard_llm_extracted_parent(
        conn,
        source_type=SourceType.LLM_EXTRACTED,
        parent_document_id=None,
        ticker="NU",
        doc_id=7,
        run_id="test-run",
    )
    row = conn.execute(
        "SELECT run_id, source_doc_id, ticker, severity, rule, raw_value FROM validation_issues"
    ).fetchone()
    assert row is not None
    assert row["run_id"] == "test-run"
    assert row["source_doc_id"] == 7
    assert row["ticker"] == "NU"
    assert row["severity"] == Severity.WARN.value
    assert row["rule"] == ValidationRule.MISSING_FIELD.value
    assert row["raw_value"] == "parent_document_id"


def test_guard_raises_when_strict(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="parent_document_id"):
        guard_llm_extracted_parent(
            conn,
            source_type=SourceType.LLM_EXTRACTED,
            parent_document_id=None,
            ticker="NU",
            doc_id=7,
            strict=True,
        )
    # Strict mode raises instead of writing — no row logged.
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 0
