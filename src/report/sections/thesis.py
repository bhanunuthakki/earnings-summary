"""§2 Thesis & tier-1 KPI ledger.

Reads the holdings JSON for narrative + break conditions, then joins to
kpi_definitions / kpi_facts in the DB to render historical KPI values where
they're tracked. KPIs without facts in the DB show empty history with a
status='unknown' — the holdings JSON remains the contract.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from report.models import (
    KpiLedgerRow,
    SectionStatus,
    ThesisSection,
)
from report.sections._common import has_table, missing, open_repo_db


def build(ticker: str, repo_root: Path) -> ThesisSection:
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not holdings_path.exists():
        return ThesisSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings)",
                fix_command=f"create {holdings_path.relative_to(repo_root)}",
            ),
        )
    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)

    ledger = _build_ledger(ticker, repo_root, holdings)

    return ThesisSection(
        status=SectionStatus.OK if ledger else SectionStatus.PARTIAL,
        thesis_full=holdings.get("thesis"),
        last_updated=_parse_date(holdings.get("last_updated")),
        break_conditions=list(holdings.get("break_conditions") or []),
        competitive_watchlist=list(holdings.get("competitive_watchlist") or []),
        qualitative_breakers=list(holdings.get("thesis_breakers_qualitative") or []),
        kpi_ledger=ledger,
    )


def _parse_date(s: object) -> date | None:
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _build_ledger(ticker: str, repo_root: Path, holdings: dict[str, object]) -> list[KpiLedgerRow]:
    """Tier order preserved (1 → 2 → 3); within each tier sorted alphabetically."""
    rows: list[KpiLedgerRow] = []
    for tier_key, tier_label in (
        ("tier_1_kpis", "tier_1"),
        ("tier_2_kpis", "tier_2"),
        ("tier_3_kpis", "tier_3"),
    ):
        kpis = holdings.get(tier_key) or []
        if not isinstance(kpis, list):
            continue
        tier_rows: list[KpiLedgerRow] = []
        for k in kpis:
            if not isinstance(k, dict):
                continue
            tier_rows.append(
                KpiLedgerRow(
                    name=str(k.get("name", "")),
                    tier=tier_label,  # type: ignore[arg-type]
                    unit=str(k.get("unit")) if k.get("unit") else None,
                    source_hint=str(k.get("source")) if k.get("source") else None,
                    break_condition=str(k.get("break")) if k.get("break") else None,
                    history=_kpi_history(ticker, repo_root, str(k.get("name", ""))),
                    current_status=_status_for(k),
                )
            )
        tier_rows.sort(key=lambda r: r.name.lower())
        rows.extend(tier_rows)
    return rows


def _status_for(k: dict[str, object]) -> Literal["green", "yellow", "red", "unknown"]:
    """Without runtime KPI facts wired up yet, every KPI starts at 'unknown'."""
    return "unknown"


def _kpi_history(ticker: str, repo_root: Path, kpi_name: str) -> list[tuple[str, float | None]]:
    """Return [(period_label, value)] from kpi_facts joined to kpi_definitions, or []."""
    if not kpi_name:
        return []
    conn = open_repo_db(repo_root)
    if conn is None:
        return []
    if not (has_table(conn, "kpi_facts") and has_table(conn, "kpi_definitions")):
        conn.close()
        return []
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT f.period_end, f.value
        FROM kpi_facts f
        JOIN kpi_definitions d ON d.id = f.kpi_definition_id
        WHERE d.ticker = ? AND d.name = ?
        ORDER BY f.period_end ASC
        """,
        (ticker.upper(), kpi_name),
    )
    out: list[tuple[str, float | None]] = []
    for row in cursor.fetchall():
        period = str(row["period_end"])[:10]
        out.append((period, row["value"]))
    conn.close()
    return out
