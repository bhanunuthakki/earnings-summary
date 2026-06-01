"""§2 break-rule ledger history (`report.sections.thesis._kpi_history`).

The KPI ledger renders each tier-N KPI's historical values. It used to join
kpi_facts by EXACT name, so a holdings ledger label ("Monthly ARPAC") that
spelled a sparse fragmented duplicate showed near-empty history even though the
canonical "Monthly ARPAC (USD)" carried the full series — the §2 sibling of the
§3 chart bug PR #195 fixed.

`_kpi_history` now resolves the label through the shared `compute.kpi_resolver`
(richest definition wins) and keeps deduping coexisting per-period sources to
the latest. These tests pin both behaviors at the ledger boundary.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.thesis import _kpi_history  # noqa: E402

_QUARTER_ENDS = [
    "2023-03-31",
    "2023-06-30",
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
]


def _build_repo(tmp_path: Path) -> Path:
    """Create <tmp>/data/portfolio.db with the kpi tables and return repo_root."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            unit VARCHAR
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            period_end VARCHAR NOT NULL,
            value NUMERIC,
            unit VARCHAR,
            kpi_definition_id INTEGER NOT NULL,
            fiscal_period_type VARCHAR NOT NULL,
            source_doc_id INTEGER NOT NULL DEFAULT 1,
            source_excerpt VARCHAR
        );
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


def _add_def(repo: Path, ticker: str, name: str) -> int:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit) VALUES (?, ?, 'actual')",
        (ticker, name),
    )
    def_id = cur.lastrowid
    assert def_id is not None
    conn.commit()
    conn.close()
    return def_id


def _add_facts(
    repo: Path,
    ticker: str,
    def_id: int,
    ends: list[str],
    *,
    value: float = 1.0,
    source_doc_id: int = 1,
) -> None:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    for end in ends:
        quarter = (int(end[5:7]) - 1) // 3 + 1
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type, source_doc_id) VALUES (?, ?, ?, 'actual', ?, ?, ?)",
            (ticker, end, value, def_id, f"Q{quarter}", source_doc_id),
        )
    conn.commit()
    conn.close()


def test_ledger_history_resolves_short_label_to_richest_def(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    canonical = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    duplicate = _add_def(repo, "NU", "Monthly ARPAC")
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS, value=11.0)  # 12 obs
    _add_facts(repo, "NU", duplicate, _QUARTER_ENDS[:1], value=99.0)  # 1 stray obs

    history, _ = _kpi_history("NU", repo, "Monthly ARPAC")
    # Full 12-quarter canonical series, not the 1-row duplicate.
    assert len(history) == 12
    assert {v for _, v in history} == {11.0}  # the stray 99 was never read


def test_ledger_history_dedups_coexisting_sources_to_latest(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    canonical = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS, value=10.0, source_doc_id=1)
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS[-1:], value=20.0, source_doc_id=2)

    history, _ = _kpi_history("NU", repo, "Monthly ARPAC")
    assert len(history) == 12  # one observation per quarter, not 13
    assert history[-1][1] == 20.0  # latest-ingested source wins for the restated quarter


def test_ledger_history_empty_for_unresolvable_label(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    nim = _add_def(repo, "NU", "Risk-adjusted NIM (NIM minus cost of risk)")
    _add_facts(repo, "NU", nim, _QUARTER_ENDS)
    # "NIM" is a distinct metric — the ledger row shows no history (status unknown).
    history, excerpt = _kpi_history("NU", repo, "NIM")
    assert history == []
    assert excerpt is None
