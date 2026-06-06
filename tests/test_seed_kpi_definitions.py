"""seed_kpi_definitions resolves the unit of the metric's REPORTED VALUE.

Regression guard for the AWS RPO mis-tag: a dollar LEVEL whose name carries no
currency token was stamped ``percent`` by the name heuristic's default, and the
renderer then showed ``$364B`` as ``364000000000%``. The seeder now (a)
recognises named dollar levels, (b) keeps a currency-annotated *growth* metric a
percent, and (c) heals an existing definition whose stored unit is in a
different dimensional FAMILY than its recorded kpi_facts — deferring to the fact
unit, which is authoritative for what the value actually is. The critical
discriminator is the FACT unit, never magnitude: ~162 genuinely-percent KPIs
have huge values from low-base YoY blow-ups (millions of %) and must be left
alone.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import seed_kpi_definitions as skd  # noqa: E402

from models.facts import Unit  # noqa: E402

# ---------------------------------------------------------------------------
# infer_unit — the no-facts bootstrap heuristic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "AWS Remaining Performance Obligations (RPO)",
        "Total deposits",
        "Gross merchandise volume (GMV)",
        "Total payment volume (TPV)",
        "Deferred revenue",
        "Backlog",
        "Assets under management (AUM)",
    ],
)
def test_infer_unit_named_dollar_levels_are_actual(name: str) -> None:
    # The RPO bug: these are dollar magnitudes whose names lack a "$"/"USD"
    # token, so the old percent-default mis-tagged them as a rate.
    assert skd.infer_unit(name) is Unit.ACTUAL


@pytest.mark.parametrize(
    "name",
    [
        "Revenue YoY Growth (USD)",
        "Operating Cash Flow YoY Growth (USD)",
        "AWS Revenue YoY Growth",
        "Operating Margin (GAAP)",
        "NIM %",
    ],
)
def test_infer_unit_rates_stay_percent_even_with_currency(name: str) -> None:
    # A "(USD)" annotation on a *growth* metric must not flip it to ACTUAL —
    # the rate word is decided before the currency token.
    assert skd.infer_unit(name) is Unit.PERCENT


def test_infer_unit_unchanged_anchors() -> None:
    assert skd.infer_unit("Total customers") is Unit.COUNT
    assert skd.infer_unit("Net new subscription ARR ($)") is Unit.ACTUAL
    assert skd.infer_unit("Coverage ratio") is Unit.RATIO


# ---------------------------------------------------------------------------
# seed_for_ticker — fact-deference + cross-family heal
# ---------------------------------------------------------------------------

_DDL = (
    """CREATE TABLE kpi_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
        primary_source TEXT, fallback_source TEXT, ir_url TEXT,
        threshold_tier TEXT, threshold_low REAL, threshold_high REAL, notes TEXT,
        UNIQUE(ticker, name))""",
    """CREATE TABLE kpi_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kpi_definition_id INTEGER NOT NULL, unit TEXT, value REAL,
        period_end TEXT)""",
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in _DDL:
        conn.execute(ddl)
    return conn


def _holdings(tmp_path: Path, ticker: str, tier_1_names: list[str]) -> Path:
    d = tmp_path / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}.json").write_text(
        json.dumps({"tier_1_kpis": [{"name": n} for n in tier_1_names]}),
        encoding="utf-8",
    )
    return tmp_path


def _def_row(conn: sqlite3.Connection, ticker: str, name: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, unit FROM kpi_definitions WHERE ticker = ? AND name = ?",
        (ticker, name),
    ).fetchone()
    assert row is not None
    return row


def _seed_def(conn: sqlite3.Connection, ticker: str, name: str, unit: str) -> int:
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, primary_source, threshold_tier) "
        "VALUES (?, ?, ?, 'ir_doc', 'tier_1_break')",
        (ticker, name, unit),
    )
    return int(cur.lastrowid or 0)


def _seed_fact(conn: sqlite3.Connection, def_id: int, unit: str, value: float) -> None:
    conn.execute(
        "INSERT INTO kpi_facts (kpi_definition_id, unit, value, period_end) "
        "VALUES (?, ?, ?, '2026-03-31')",
        (def_id, unit, value),
    )


def test_heals_dollar_level_mis_tagged_percent(tmp_path: Path) -> None:
    """A def stamped ``percent`` but with ``actual`` facts is healed to ``actual``."""
    conn = _conn()
    name = "AWS Remaining Performance Obligations (RPO)"
    def_id = _seed_def(conn, "AMZN", name, "percent")
    _seed_fact(conn, def_id, "actual", 244_000_000_000)
    _seed_fact(conn, def_id, "actual", 364_000_000_000)
    repo = _holdings(tmp_path, "AMZN", [name])

    result = skd.seed_for_ticker(conn, repo, "AMZN", dry_run=False)

    assert _def_row(conn, "AMZN", name)["unit"] == "actual"
    assert result.healed == 1
    assert result.healed_names == [name]
    assert result.inserted == 0


def test_does_not_heal_when_facts_share_family(tmp_path: Path) -> None:
    """A genuine percent metric with a millions-of-% value is left untouched.

    The discriminator is the fact UNIT, not the magnitude — a low-base YoY
    blow-up (31.6M %) recorded as ``percent`` must never be re-tagged.
    """
    conn = _conn()
    name = "Operating Cash Flow YoY Growth (USD)"
    def_id = _seed_def(conn, "AMZN", name, "percent")
    _seed_fact(conn, def_id, "percent", 31_600_000)
    repo = _holdings(tmp_path, "AMZN", [name])

    result = skd.seed_for_ticker(conn, repo, "AMZN", dry_run=False)

    assert _def_row(conn, "AMZN", name)["unit"] == "percent"
    assert result.healed == 0
    assert result.skipped_existing == 1


def test_new_definition_uses_hardened_heuristic(tmp_path: Path) -> None:
    """A fresh seed (no prior row, no facts) tags RPO ``actual``, not ``percent``."""
    conn = _conn()
    name = "AWS Remaining Performance Obligations (RPO)"
    repo = _holdings(tmp_path, "AMZN", [name])

    result = skd.seed_for_ticker(conn, repo, "AMZN", dry_run=False)

    assert _def_row(conn, "AMZN", name)["unit"] == "actual"
    assert result.inserted == 1
    assert result.healed == 0


def test_dry_run_reports_heal_without_writing(tmp_path: Path) -> None:
    conn = _conn()
    name = "Total deposits"
    def_id = _seed_def(conn, "NU", name, "percent")
    _seed_fact(conn, def_id, "actual", 100_000_000_000)
    repo = _holdings(tmp_path, "NU", [name])

    result = skd.seed_for_ticker(conn, repo, "NU", dry_run=True)

    assert result.healed == 1
    # dry-run must not mutate the row.
    assert _def_row(conn, "NU", name)["unit"] == "percent"
