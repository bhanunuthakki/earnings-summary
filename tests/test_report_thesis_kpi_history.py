"""§2 break-rule ledger history (`report.sections.thesis._kpi_history_conn`).

The KPI ledger renders each tier-N KPI's historical values. It used to join
kpi_facts by EXACT name, so a holdings ledger label ("Monthly ARPAC") that
spelled a sparse fragmented duplicate showed near-empty history even though the
canonical "Monthly ARPAC (USD)" carried the full series — the §2 sibling of the
§3 chart bug PR #195 fixed.

`_kpi_history_conn` now resolves the label through the shared `compute.kpi_resolver`
(richest definition wins) and keeps deduping coexisting per-period sources to
the latest. These tests pin both behaviors at the ledger boundary, plus the
definition/unit enrichment `_build_ledger` layers on top.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.sections.thesis import (  # noqa: E402
    _build_ledger,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
    _kpi_history_conn,  # pyright: ignore[reportPrivateUsage]
)


def _history(
    repo: Path, ticker: str, name: str
) -> tuple[list[tuple[str, float | None]], str | None]:
    """Open the fixture DB and read one KPI's history, mirroring how
    `_build_ledger` calls `_kpi_history_conn` with a shared connection."""
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.row_factory = sqlite3.Row
    try:
        return _kpi_history_conn(conn, ticker, name)
    finally:
        conn.close()


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
        CREATE TABLE kpi_fact_semantic_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_fact_id INTEGER NOT NULL,
            metric_name_as_reported TEXT NOT NULL,
            reported_period_end TEXT,
            period_role TEXT NOT NULL DEFAULT 'current',
            publication_lane TEXT NOT NULL DEFAULT 'current_actual',
            accounting_basis TEXT NOT NULL DEFAULT 'gaap',
            consolidation_scope TEXT NOT NULL DEFAULT 'consolidated',
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            unit_scale TEXT NOT NULL DEFAULT 'none',
            status TEXT NOT NULL DEFAULT 'admitted',
            revision INTEGER NOT NULL DEFAULT 1,
            supersedes_context_id INTEGER
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
    kpi_name = str(
        conn.execute("SELECT name FROM kpi_definitions WHERE id = ?", (def_id,)).fetchone()[0]
    )
    for end in ends:
        quarter = (int(end[5:7]) - 1) // 3 + 1
        cur = conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
            "fiscal_period_type, source_doc_id) VALUES (?, ?, ?, 'actual', ?, ?, ?)",
            (ticker, end, value, def_id, f"Q{quarter}", source_doc_id),
        )
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts "
            "(kpi_fact_id, metric_name_as_reported, reported_period_end) VALUES (?, ?, ?)",
            (cur.lastrowid, kpi_name, end),
        )
    conn.commit()
    conn.close()


def test_ledger_history_resolves_short_label_to_richest_def(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    canonical = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    duplicate = _add_def(repo, "NU", "Monthly ARPAC")
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS, value=11.0)  # 12 obs
    _add_facts(repo, "NU", duplicate, _QUARTER_ENDS[:1], value=99.0)  # 1 stray obs

    history, _ = _history(repo, "NU", "Monthly ARPAC")
    # Full 12-quarter canonical series, not the 1-row duplicate.
    assert len(history) == 12
    assert {v for _, v in history} == {11.0}  # the stray 99 was never read


def test_ledger_history_dedups_coexisting_sources_to_latest(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    canonical = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS, value=10.0, source_doc_id=1)
    _add_facts(repo, "NU", canonical, _QUARTER_ENDS[-1:], value=20.0, source_doc_id=2)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.execute(
        "CREATE VIEW v_kpi_facts_resolved_current AS "
        "SELECT * FROM kpi_facts WHERE source_doc_id = 2 OR period_end <> '2025-12-31'"
    )
    conn.commit()
    conn.close()

    history, _ = _history(repo, "NU", "Monthly ARPAC")
    assert len(history) == 12  # one observation per quarter, not 13
    assert history[-1][1] == 20.0  # latest-ingested source wins for the restated quarter


def test_ledger_history_uses_canonical_relation_and_fails_closed_on_kpi_override(
    tmp_path: Path,
) -> None:
    repo = _build_repo(tmp_path)
    definition = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    _add_facts(repo, "NU", definition, ["2025-12-31"], value=10.0)
    _add_facts(repo, "NU", definition, ["2025-12-31"], value=99.0, source_doc_id=2)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.execute("CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts WHERE id = 1")
    conn.execute(
        "CREATE TABLE fact_overrides (id INTEGER, user_id TEXT, ticker TEXT, "
        "period_end TEXT, fiscal_period_type TEXT, fact_kind TEXT, fact_key TEXT, "
        "action TEXT, value REAL, unit TEXT, value_json TEXT, source_doc_type TEXT, "
        "source_accession TEXT, source_exhibit TEXT, source_url TEXT, source_excerpt TEXT, "
        "source_doc_id INTEGER, status TEXT, confidence REAL, rationale TEXT, "
        "created_by TEXT, created_at TEXT, locator TEXT)"
    )
    conn.execute(
        "INSERT INTO fact_overrides VALUES "
        "(1, 'bhanu', 'NU', '2025-12-31', 'Q4', 'kpi', 'Monthly ARPAC (USD)', "
        "'replace', 777.0, 'usd', NULL, 'earnings_release', NULL, NULL, NULL, NULL, "
        "1, 'active', 1.0, 'test', 'test', '2026-08-27', NULL)"
    )
    conn.commit()
    conn.close()

    history, _ = _history(repo, "NU", "Monthly ARPAC (USD)")
    assert history == []


def test_ledger_history_excludes_unadmitted_kpi_candidate(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    definition = _add_def(repo, "NU", "Monthly ARPAC (USD)")
    _add_facts(repo, "NU", definition, ["2025-12-31"], value=10.0)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    cur = conn.execute(
        "INSERT INTO kpi_facts (ticker, period_end, value, unit, kpi_definition_id, "
        "fiscal_period_type, source_doc_id) VALUES ('NU', '2025-12-31', 777.0, "
        "'actual', ?, 'Q4', 2)",
        (definition,),
    )
    conn.execute(
        "INSERT INTO kpi_fact_semantic_contexts "
        "(kpi_fact_id, metric_name_as_reported, reported_period_end, status) "
        "VALUES (?, 'Monthly ARPAC (USD)', '2025-12-31', 'quarantined')",
        (cur.lastrowid,),
    )
    conn.execute("CREATE VIEW v_kpi_facts_resolved_current AS SELECT * FROM kpi_facts WHERE id=2")
    conn.commit()
    conn.close()

    history, _ = _history(repo, "NU", "Monthly ARPAC (USD)")
    assert history == []


def test_ledger_history_empty_for_unresolvable_label(tmp_path: Path) -> None:
    repo = _build_repo(tmp_path)
    nim = _add_def(repo, "NU", "Risk-adjusted NIM (NIM minus cost of risk)")
    _add_facts(repo, "NU", nim, _QUARTER_ENDS)
    # "NIM" is a distinct metric — the ledger row shows no history (status unknown).
    history, excerpt = _history(repo, "NU", "NIM")
    assert history == []
    assert excerpt is None


def test_ledger_populates_break_condition_from_v2_key(tmp_path: Path) -> None:
    """Schema-v2 holdings JSONs key the break text as `break_condition` (NU/MELI/BN);
    older ones use `break`. The ledger's Break column must populate from either —
    regression for the always-empty Break/Unit columns on v2 tickers (the row was
    read with k.get("break") only, which is None for v2)."""
    repo = _build_repo(tmp_path)
    holdings: dict[str, object] = {
        "tier_1_kpis": [
            {
                "name": "ROE (annualized, consolidated)",
                "break_condition": "Consolidated ROE <25% for 2 consecutive Qs",
                "source": "earnings release",
            },
            {"name": "Legacy KPI", "break": "old-style break text"},
        ]
    }
    rows = _build_ledger("NU", repo, holdings, evaluations=[])
    by_name = {r.name: r for r in rows}
    assert by_name["ROE (annualized, consolidated)"].break_condition == (
        "Consolidated ROE <25% for 2 consecutive Qs"
    )
    assert by_name["ROE (annualized, consolidated)"].source_hint == "earnings release"
    # The older `break` key still populates the column.
    assert by_name["Legacy KPI"].break_condition == "old-style break text"


def _build_repo_with_notes(tmp_path: Path) -> Path:
    """Like `_build_repo` but the kpi_definitions table carries a `notes`
    column, mirroring the real (migrated) schema."""
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            unit VARCHAR,
            notes TEXT
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
        CREATE TABLE kpi_fact_semantic_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_fact_id INTEGER NOT NULL,
            metric_name_as_reported TEXT NOT NULL,
            reported_period_end TEXT,
            period_role TEXT NOT NULL DEFAULT 'current',
            publication_lane TEXT NOT NULL DEFAULT 'current_actual',
            accounting_basis TEXT NOT NULL DEFAULT 'gaap',
            consolidation_scope TEXT NOT NULL DEFAULT 'consolidated',
            dimensions_json TEXT NOT NULL DEFAULT '{}',
            unit_scale TEXT NOT NULL DEFAULT 'none',
            status TEXT NOT NULL DEFAULT 'admitted',
            revision INTEGER NOT NULL DEFAULT 1,
            supersedes_context_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return tmp_path


def _add_def_full(
    repo: Path, ticker: str, name: str, *, unit: str, notes: str | None = None
) -> int:
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    cur = conn.execute(
        "INSERT INTO kpi_definitions (ticker, name, unit, notes) VALUES (?, ?, ?, ?)",
        (ticker, name, unit, notes),
    )
    def_id = cur.lastrowid
    assert def_id is not None
    conn.commit()
    conn.close()
    return def_id


def test_ledger_definition_falls_back_to_name_qualifier(tmp_path: Path) -> None:
    """No curator notes → the definition is the name's parenthetical qualifier,
    and the Unit column is backfilled from the resolved definition's unit."""
    repo = _build_repo_with_notes(tmp_path)
    def_id = _add_def_full(repo, "NU", "ROE (annualized, consolidated)", unit="percent")
    _add_facts(repo, "NU", def_id, _QUARTER_ENDS, value=25.0)
    holdings: dict[str, object] = {"tier_1_kpis": [{"name": "ROE (annualized, consolidated)"}]}
    row = _build_ledger("NU", repo, holdings, evaluations=[])[0]
    assert row.definition == "annualized, consolidated"
    assert row.unit == "%"  # backfilled from the definition's 'percent'


def test_ledger_definition_prefers_curator_notes(tmp_path: Path) -> None:
    """A populated `notes` cell wins over the name's parenthetical."""
    repo = _build_repo_with_notes(tmp_path)
    def_id = _add_def_full(
        repo,
        "NU",
        "ROE (annualized, consolidated)",
        unit="percent",
        notes="Net income / average equity, annualized",
    )
    _add_facts(repo, "NU", def_id, _QUARTER_ENDS, value=25.0)
    holdings: dict[str, object] = {"tier_1_kpis": [{"name": "ROE (annualized, consolidated)"}]}
    row = _build_ledger("NU", repo, holdings, evaluations=[])[0]
    assert row.definition == "Net income / average equity, annualized"


def test_ledger_holdings_unit_takes_precedence_over_backfill(tmp_path: Path) -> None:
    repo = _build_repo_with_notes(tmp_path)
    def_id = _add_def_full(repo, "NU", "Some KPI", unit="percent")
    _add_facts(repo, "NU", def_id, _QUARTER_ENDS, value=1.0)
    holdings: dict[str, object] = {"tier_1_kpis": [{"name": "Some KPI", "unit": "USD"}]}
    row = _build_ledger("NU", repo, holdings, evaluations=[])[0]
    assert row.unit == "USD"  # holdings-declared unit is never overwritten


def test_ledger_zero_fact_row_still_gets_definition(tmp_path: Path) -> None:
    """A tracked tier-2/3 KPI with no facts (resolver returns None) still
    surfaces a definition — the metadata lookup is fact-independent."""
    repo = _build_repo_with_notes(tmp_path)
    _add_def_full(repo, "NU", "Cost of risk (NPL formation)", unit="percent")  # no facts
    holdings: dict[str, object] = {"tier_2_kpis": [{"name": "Cost of risk (NPL formation)"}]}
    row = _build_ledger("NU", repo, holdings, evaluations=[])[0]
    assert row.history == []
    assert row.definition == "NPL formation"
    assert row.unit == "%"  # unit still backfills without any facts
