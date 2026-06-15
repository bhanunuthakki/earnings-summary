"""``find_or_create_kpi_definition`` stamps the definition-origin marker.

Migration 0113 adds ``kpi_definitions.definition_origin`` (``'analyst'`` default,
``'capture'`` for the capture-every-number long tail). These tests prove the
persistence helper stamps it ONCE at row creation, defaults to ``'analyst'`` so
existing callers are unchanged, never rewrites an existing row's origin, and
stays runnable on a pre-0113 schema that lacks the column.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.documents import SourceType  # noqa: E402
from models.facts import Unit  # noqa: E402
from models.kpis import DefinitionOrigin  # noqa: E402
from pipeline.kpi_persistence import find_or_create_kpi_definition  # noqa: E402

# kpi_definitions with every column _create_kpi_definition writes, including the
# 0113 origin column.
_DDL_WITH_ORIGIN = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    name TEXT NOT NULL,
    unit VARCHAR NOT NULL,
    primary_source VARCHAR NOT NULL,
    fallback_source VARCHAR,
    ir_url VARCHAR,
    threshold_tier VARCHAR,
    threshold_low FLOAT,
    threshold_high FLOAT,
    notes TEXT,
    definition_origin VARCHAR NOT NULL DEFAULT 'analyst'
)
"""

# Pre-0113 schema — same minus the origin column.
_DDL_WITHOUT_ORIGIN = """
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR NOT NULL,
    name TEXT NOT NULL,
    unit VARCHAR NOT NULL,
    primary_source VARCHAR NOT NULL,
    fallback_source VARCHAR,
    ir_url VARCHAR,
    threshold_tier VARCHAR,
    threshold_low FLOAT,
    threshold_high FLOAT,
    notes TEXT
)
"""


def _make_db(ddl: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(ddl)
    return conn


def _origin(conn: sqlite3.Connection, kpi_id: int) -> str:
    row = conn.execute(
        "SELECT definition_origin FROM kpi_definitions WHERE id = ?", (kpi_id,)
    ).fetchone()
    return str(row["definition_origin"])


def test_default_origin_is_analyst() -> None:
    conn = _make_db(_DDL_WITH_ORIGIN)
    kpi_id = find_or_create_kpi_definition(
        conn, ticker="NU", name="NIM", unit=Unit.PERCENT, primary_source=SourceType.IR_DOC
    )
    assert _origin(conn, kpi_id) == "analyst"


def test_capture_origin_is_stamped() -> None:
    conn = _make_db(_DDL_WITH_ORIGIN)
    kpi_id = find_or_create_kpi_definition(
        conn,
        ticker="NU",
        name="Card present TPV",
        unit=Unit.MILLIONS,
        primary_source=SourceType.IR_DOC,
        origin=DefinitionOrigin.CAPTURE,
    )
    assert _origin(conn, kpi_id) == "capture"


def test_origin_is_not_rewritten_on_reencounter() -> None:
    """An analyst-authored definition keeps its origin when a later capture pass
    re-encounters the same (ticker, name)."""
    conn = _make_db(_DDL_WITH_ORIGIN)
    first = find_or_create_kpi_definition(
        conn,
        ticker="NU",
        name="NIM",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
        origin=DefinitionOrigin.ANALYST,
    )
    second = find_or_create_kpi_definition(
        conn,
        ticker="NU",
        name="NIM",
        unit=Unit.PERCENT,
        primary_source=SourceType.IR_DOC,
        origin=DefinitionOrigin.CAPTURE,  # would-be rewrite — must be ignored
    )
    assert first == second
    assert _origin(conn, first) == "analyst"


def test_runs_without_origin_column() -> None:
    """Schema-defensive: on a pre-0113 DB the column is simply omitted from the
    INSERT — the definition is still created."""
    conn = _make_db(_DDL_WITHOUT_ORIGIN)
    kpi_id = find_or_create_kpi_definition(
        conn,
        ticker="NU",
        name="GMV",
        unit=Unit.MILLIONS,
        primary_source=SourceType.IR_DOC,
        origin=DefinitionOrigin.CAPTURE,
    )
    assert kpi_id > 0
    row = conn.execute("SELECT name FROM kpi_definitions WHERE id = ?", (kpi_id,)).fetchone()
    assert row["name"] == "GMV"
