"""Compose the stored competitive data into RBRK.json's owner-declared KPIs.

The owner's competitive tier-2 KPIs are COMPOSITE lines (e.g. "Category share -
Gartner MQ position / IDC data-protection market share (annual)"). The three
pipelines write GRANULAR facts (the Gartner MQ ordinal, each share %, each
per-quarter mention count) into ``kpi_facts``, plus the S-1 watch state into the
``news`` table. This module reads those back and renders the owner KPI's
``current`` string from them — so each owner KPI reads a REAL stored value
instead of a "WIRE NOW / WIRE from transcripts / EVENT-UNLOCK" placeholder.

``--show`` (default) PRINTS the resolved values — proof the KPI reads a real
value. ``--apply`` writes them into the matching tier-2 KPIs' ``current`` field
in ``RBRK.json`` (touching ONLY the competitive KPIs the pipelines own, never
the analyst's other entries) — mirroring the repo's ``mark_kpi_cadence --apply``
dry-run-by-default convention.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from competitive import (
    KPI_CATEGORY_SHARE_COHESITY,
    KPI_CATEGORY_SHARE_RBRK,
    KPI_GARTNER_MQ_ORDINAL_RBRK,
    KPI_MENTIONS_DISPLACEMENT,
    KPI_MENTIONS_LARGE_WIN,
    KPI_MENTIONS_NAMED_COMPETITOR,
    OWNER_KPI_CATEGORY_SHARE,
    OWNER_KPI_INCREMENTAL,
    OWNER_KPI_INCREMENTAL_LEGACY,
    OWNER_KPI_MENTIONS,
    SYNCED_KPI_NAMES,
)
from competitive.sec_watch import s1_watch_status
from compute.kpi_resolver import resolve_kpi_definition_name

_MQ_LABELS = {1: "Niche", 2: "Visionary", 3: "Challenger", 4: "Leader"}


@dataclass(slots=True)
class ResolvedKpi:
    name: str
    current: str
    has_value: bool


@dataclass(slots=True)
class _Fact:
    value: str
    unit: str
    fiscal_period_type: str
    period_end: str
    source_excerpt: str | None


def _fmt_number(value: str) -> str:
    """Trim a stored Decimal string to a clean display form (12.50 -> 12.5)."""
    try:
        d = float(value)
    except ValueError:
        return value
    return str(int(d)) if d == int(d) else f"{d:g}"


def _latest_fact(conn: sqlite3.Connection, ticker: str, metric: str) -> _Fact | None:
    """Latest stored ``kpi_facts`` row for a granular metric, or None."""
    resolved = resolve_kpi_definition_name(conn, ticker, metric) or metric
    row = conn.execute(
        "SELECT f.period_end, f.fiscal_period_type, f.value, f.unit, f.source_excerpt "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE d.ticker = ? AND d.name = ? "
        "ORDER BY f.period_end DESC, f.source_doc_id DESC LIMIT 1",
        (ticker.upper(), resolved),
    ).fetchone()
    if row is None:
        return None
    return _Fact(
        value=_fmt_number(str(row["value"])),
        unit=str(row["unit"]),
        fiscal_period_type=str(row["fiscal_period_type"]),
        period_end=str(row["period_end"])[:10],
        source_excerpt=None if row["source_excerpt"] is None else str(row["source_excerpt"]),
    )


def _render_category_share(conn: sqlite3.Connection, ticker: str) -> tuple[str, bool]:
    mq = _latest_fact(conn, ticker, KPI_GARTNER_MQ_ORDINAL_RBRK)
    rbrk = _latest_fact(conn, ticker, KPI_CATEGORY_SHARE_RBRK)
    coh = _latest_fact(conn, ticker, KPI_CATEGORY_SHARE_COHESITY)
    parts: list[str] = []
    if mq is not None:
        label = _MQ_LABELS.get(int(float(mq.value)), f"ordinal {mq.value}")
        parts.append(f"RBRK Gartner MQ {label} ({mq.fiscal_period_type} ending {mq.period_end})")
    if rbrk is not None:
        parts.append(f"RBRK share {rbrk.value}% ({rbrk.period_end[:4]})")
    if coh is not None:
        parts.append(f"Cohesity share {coh.value}% ({coh.period_end[:4]})")
    if not parts:
        return ("No category datapoint yet (run ingest_competitive_category_share)", False)
    return ("WIRED — " + "; ".join(parts), True)


def _render_mentions(conn: sqlite3.Connection, ticker: str) -> tuple[str, bool]:
    disp = _latest_fact(conn, ticker, KPI_MENTIONS_DISPLACEMENT)
    win = _latest_fact(conn, ticker, KPI_MENTIONS_LARGE_WIN)
    named = _latest_fact(conn, ticker, KPI_MENTIONS_NAMED_COMPETITOR)
    present = [f for f in (disp, win, named) if f is not None]
    if not present:
        return ("No transcript counts yet (run extract_competitive_mentions)", False)
    anchor = present[0]
    period = f"{anchor.fiscal_period_type} ending {anchor.period_end}"
    return (
        f"WIRED — {period}: displacement={_count(disp)}, "
        f">$1M/large-logo wins={_count(win)}, Cohesity/Veeam/Dell mentions={_count(named)}",
        True,
    )


def _render_incremental(conn: sqlite3.Connection, ticker: str) -> tuple[str, bool]:
    status = s1_watch_status(conn, entity="Cohesity", attributed_ticker=ticker)
    if status.filed:
        return (
            f"UNLOCKED — Cohesity filed its IPO S-1 on {status.filed_date} ({status.url}); "
            "RBRK-vs-Cohesity net-new-ARR share metric now activatable.",
            True,
        )
    return (
        "EVENT-UNLOCK WATCH LIVE — Cohesity has not yet filed its IPO S-1 "
        "(check_competitor_s1 / fetch_news additive, source_feed=edgar_s1_watch).",
        False,
    )


def _count(fact: _Fact | None) -> str:
    return fact.value if fact is not None else "0"


def resolve_synced_current(conn: sqlite3.Connection, ticker: str, owner_kpi: str) -> ResolvedKpi:
    """Render one owner KPI's ``current`` string from its real stored sources."""
    if owner_kpi == OWNER_KPI_CATEGORY_SHARE:
        current, has = _render_category_share(conn, ticker)
    elif owner_kpi == OWNER_KPI_MENTIONS:
        current, has = _render_mentions(conn, ticker)
    elif owner_kpi in (OWNER_KPI_INCREMENTAL, OWNER_KPI_INCREMENTAL_LEGACY):
        current, has = _render_incremental(conn, ticker)
    else:  # pragma: no cover — defensive; SYNCED_KPI_NAMES is closed
        return ResolvedKpi(owner_kpi, "(no renderer)", False)
    return ResolvedKpi(owner_kpi, current, has)


@dataclass(slots=True)
class SyncResult:
    ticker: str
    resolved: list[ResolvedKpi]
    applied: int = 0


def sync_holdings(
    conn: sqlite3.Connection, repo_root: Path, ticker: str, *, apply: bool
) -> SyncResult:
    """Resolve every owner competitive KPI's current value; optionally write it
    into the holdings JSON tier-2 KPIs (the synced ones only)."""
    resolved = [resolve_synced_current(conn, ticker, name) for name in SYNCED_KPI_NAMES]
    result = SyncResult(ticker=ticker.upper(), resolved=resolved)
    if not apply:
        return result

    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return result
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    by_name = {r.name: r.current for r in resolved}
    tier_2 = payload.get("tier_2_kpis")
    if not isinstance(tier_2, list):
        return result
    for entry in cast("list[object]", tier_2):
        if not isinstance(entry, dict):
            continue
        kpi = cast("dict[str, object]", entry)
        name = str(kpi.get("name", ""))
        if name in by_name:
            kpi["current"] = by_name[name]
            result.applied += 1
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
