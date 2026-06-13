"""Severity-enum contract for the data-quality surfaces (S10).

The shipped bug: ``record_validation_issue`` writes severity ``halt``/``warn``
(``models.validation.Severity``), but the report Sources renderer matched a
hand-typed ``{error, warning}`` map and the ``ORDER BY`` matched the same dead
vocabulary. So a HALT issue rendered MUTED GREY and the severity sort was a
silent no-op — the worst issues hidden. These tests pin the fix structurally:
the renderer tone map and the sort order both DERIVE from the live enum, so a
renamed/added severity fails here instead of mis-rendering in production.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from models.validation import SEVERITY_ORDER, Severity  # noqa: E402
from report.sections.provenance import (  # noqa: E402
    _open_validation_issues,  # pyright: ignore[reportPrivateUsage]
)
from ui.controls import (  # noqa: E402
    _SEVERITY_TONE,  # pyright: ignore[reportPrivateUsage]
    prov_case,
    prov_drawer,
    prov_row,
    prov_severity_tick,
    prov_severity_tone,
)

# ---------------------------------------------------------------------------
# The contract: maps derive from the live enum, never a hand-typed set.
# ---------------------------------------------------------------------------


def test_renderer_tone_map_covers_every_severity() -> None:
    """Every ``Severity`` member must have a tone — an unmapped member is the
    halt-renders-grey bug waiting to recur. Keyed by the enum (not strings), so
    this also catches a stale string key surviving a rename."""
    assert set(_SEVERITY_TONE) == set(Severity)


def test_severity_sort_order_covers_every_severity() -> None:
    """The Sources ORDER BY is built from SEVERITY_ORDER — it must enumerate
    every member, or a severity silently sorts into the ELSE bucket."""
    assert set(SEVERITY_ORDER) == set(Severity)
    # Most-severe first: HALT outranks WARN.
    assert SEVERITY_ORDER.index(Severity.HALT) < SEVERITY_ORDER.index(Severity.WARN)


def test_tone_resolution_is_enum_keyed() -> None:
    assert prov_severity_tone("halt") == "bad"
    assert prov_severity_tone("warn") == "warn"
    # Case / whitespace tolerant (stored values are lower-case, but be robust).
    assert prov_severity_tone("HALT") == "bad"
    assert prov_severity_tone(" warn ") == "warn"
    # The dead vocabulary the bug matched, and anything unknown, degrades to a
    # neutral tick — never a wrong color.
    assert prov_severity_tone("error") == "muted"
    assert prov_severity_tone("warning") == "muted"
    assert prov_severity_tone("") == "muted"
    assert prov_severity_tone(None) == "muted"


def test_halt_tick_is_not_muted() -> None:
    """The literal regression: a HALT issue must render the BAD tone, never the
    muted grey the old renderer produced."""
    halt = prov_severity_tick("halt")
    assert "k-prov-tick-bad" in halt
    assert "k-prov-tick-muted" not in halt
    assert ">HALT<" in halt
    warn = prov_severity_tick("warn")
    assert "k-prov-tick-warn" in warn
    # Unknown severity still renders (degraded), it doesn't blow up or vanish.
    assert "k-prov-tick-muted" in prov_severity_tick("legacy_value")


# ---------------------------------------------------------------------------
# The ORDER BY actually floats HALT to the top now.
# ---------------------------------------------------------------------------


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE validation_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, source_doc_id INTEGER, ticker TEXT,
            severity TEXT NOT NULL, rule TEXT NOT NULL,
            raw_value TEXT, expected TEXT,
            raised_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP
        );
        """
    )
    conn.executemany(
        "INSERT INTO validation_issues "
        "(ticker, severity, rule, raw_value, expected, raised_at, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        [
            # Oldest warn, newest warn, and a middle halt — if the sort were a
            # no-op (the bug), pure raised_at DESC would put the newest WARN
            # first. The severity sort must instead surface the HALT.
            ("NU", "warn", "source_disagreement", "1", "2", "2026-05-04 09:00:00"),
            ("NU", "halt", "magnitude_jump", "9", "1", "2026-05-03 09:00:00"),
            ("NU", "warn", "plausible_range", "3", "4", "2026-05-02 09:00:00"),
        ],
    )
    conn.commit()
    conn.close()


def test_open_issues_order_by_floats_halt_first(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _make_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        count, rows = _open_validation_issues(conn, "NU")
    finally:
        conn.close()
    assert count == 3
    assert [r.severity for r in rows] == ["halt", "warn", "warn"]
    # Within a severity tier, newest-raised first (the secondary sort survives).
    assert rows[1].raised_at is not None and rows[2].raised_at is not None
    assert rows[1].raised_at > rows[2].raised_at


# ---------------------------------------------------------------------------
# The primitives render (so they're not introduced as untested dead code).
# ---------------------------------------------------------------------------


def test_prov_row_carries_tick_stamp_note_and_action() -> None:
    html = prov_row(
        "FMP endpoints",
        severity="halt",
        stamp="2026-05-03T08:30:00",
        note="12 endpoints",
        actions='<button class="k-prov-act">Refresh</button>',
    )
    assert 'class="k-prov"' in html
    assert "k-prov-tick-bad" in html
    assert "FMP endpoints" in html
    assert "k-prov-stamp" in html
    assert "12 endpoints" in html
    assert "Refresh" in html


def test_prov_drawer_and_case_render_expected_actual() -> None:
    case = prov_case(
        "case_07",
        score=0.31,
        meta="judge: off-rubric",
        rationale="The answer omitted the required citation.",
        expected="cite ≥1 source",
        actual="no sources cited",
    )
    assert "k-prov-case" in case
    assert "k-pill-bad" in case and "0.31" in case
    assert "cite &ge;1 source" in case or "cite ≥1 source" in case
    drawer = prov_drawer("2 failed cases — expected vs actual", case + case)
    assert drawer.startswith("<details")
    assert drawer.count("k-prov-case") == 2
