"""Deterministic KPI refresh scope for the owner-facing metric surfaces."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from compute.kpi_resolver import normalize_kpi_name


class ScopedKpiDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    kpi_definition_id: int | None
    name: str
    reasons: tuple[str, ...]
    fact_count: int
    admitted_context_count: int
    quarantined_context_count: int
    legacy_unknown_context_count: int
    missing_context_count: int
    current_actual_count: int
    comparator_count: int
    guidance_target_count: int
    management_explanation_count: int
    analyst_question_count: int


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )


def portfolio_tickers(conn: sqlite3.Connection, *, user_id: str = "default") -> tuple[str, ...]:
    """Current portfolio companies only; watchlist/evaluation names stay out."""
    if not _table_exists(conn, "tracked_companies"):
        return ()
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracked_companies)")}
    predicates = ["list_type='portfolio'"]
    params: list[object] = []
    if "user_id" in columns:
        predicates.append("user_id=?")
        params.append(user_id)
    if "archived_at" in columns:
        predicates.append("archived_at IS NULL")
    rows = conn.execute(
        "SELECT DISTINCT UPPER(ticker) FROM tracked_companies WHERE "  # nosec B608
        + " AND ".join(predicates)
        + " ORDER BY UPPER(ticker)",
        tuple(params),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _report_names(repo_root: Path, tickers: tuple[str, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {ticker: set() for ticker in tickers}
    for ticker in tickers:
        path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload_object = cast("dict[str, object]", payload)
        priorities = payload_object.get("chart_priorities")
        if isinstance(priorities, list):
            out[ticker].update(
                str(value).strip()
                for value in cast("list[object]", priorities)
                if str(value).strip()
            )
        for field in ("tier_1_kpis", "tier_2_kpis", "tier_3_kpis"):
            rows = payload_object.get(field)
            if not isinstance(rows, list):
                continue
            for row in cast("list[object]", rows):
                if isinstance(row, dict):
                    row_object = cast("dict[str, object]", row)
                    if str(row_object.get("name") or "").strip():
                        out[ticker].add(str(row_object["name"]).strip())
        rules = payload_object.get("break_rules")
        if isinstance(rules, list):
            for row in cast("list[object]", rules):
                if isinstance(row, dict):
                    row_object = cast("dict[str, object]", row)
                    if str(row_object.get("kpi_name") or "").strip():
                        out[ticker].add(str(row_object["kpi_name"]).strip())
    return out


def scoped_kpi_definitions(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str = "default",
) -> tuple[ScopedKpiDefinition, ...]:
    """Union report-used KPIs with every KPI exposed in Facts & Metrics."""
    tickers = portfolio_tickers(conn, user_id=user_id)
    if not tickers or not _table_exists(conn, "kpi_definitions"):
        return ()
    report_names = _report_names(repo_root, tickers)
    marks = ",".join("?" for _ in tickers)
    has_context = _table_exists(conn, "kpi_fact_semantic_contexts")
    context_columns: set[str] = set()
    if has_context:
        context_columns = {
            str(cast("tuple[object, ...]", row)[1])
            for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
        }
    has_revisions = {"revision", "supersedes_context_id"}.issubset(context_columns)
    has_lanes = "publication_lane" in context_columns
    context_join = ""
    if has_context:
        context_join = "LEFT JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id"
        if has_revisions:
            context_join += (
                " AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts context_successor "
                "WHERE context_successor.supersedes_context_id=context.id)"
            )
    admitted = "SUM(CASE WHEN context.status='admitted' THEN 1 ELSE 0 END)" if has_context else "0"
    quarantined = (
        "SUM(CASE WHEN context.status='quarantined' THEN 1 ELSE 0 END)" if has_context else "0"
    )
    legacy_unknown = (
        "SUM(CASE WHEN context.status='legacy_unknown' THEN 1 ELSE 0 END)" if has_context else "0"
    )
    missing = (
        "SUM(CASE WHEN context.id IS NULL THEN 1 ELSE 0 END)" if has_context else "COUNT(fact.id)"
    )
    lane_names = (
        "current_actual",
        "comparator",
        "guidance_target",
        "management_explanation",
        "analyst_question",
    )
    lane_counts = [
        (
            f"SUM(CASE WHEN context.status='admitted' AND context.publication_lane='{lane}' "
            "THEN 1 ELSE 0 END)"
            if has_lanes
            else (admitted if lane == "current_actual" else "0")
        )
        for lane in lane_names
    ]
    rows = conn.execute(
        f"SELECT definition.id,UPPER(definition.ticker),definition.name,COUNT(fact.id),"  # nosec B608
        f"{admitted},{quarantined},{legacy_unknown},{missing},{','.join(lane_counts)} "
        "FROM kpi_definitions definition "
        "LEFT JOIN kpi_facts fact ON fact.kpi_definition_id=definition.id "
        f"{context_join} WHERE UPPER(definition.ticker) IN ({marks}) "
        "GROUP BY definition.id,UPPER(definition.ticker),definition.name ORDER BY 2,3,1",
        tickers,
    ).fetchall()
    out: list[ScopedKpiDefinition] = []
    matched_report_keys: dict[str, set[str]] = {ticker: set() for ticker in tickers}
    for row in rows:
        definition_id, ticker, name = int(row[0]), str(row[1]), str(row[2])
        fact_count = int(row[3])
        reasons: list[str] = []
        requested_keys = {normalize_kpi_name(value) for value in report_names[ticker]}
        key = normalize_kpi_name(name)
        if key in requested_keys:
            reasons.append("report")
            matched_report_keys[ticker].add(key)
        if fact_count > 0:
            reasons.append("facts_metrics")
        if reasons:
            out.append(
                ScopedKpiDefinition(
                    ticker=ticker,
                    kpi_definition_id=definition_id,
                    name=name,
                    reasons=tuple(reasons),
                    fact_count=fact_count,
                    admitted_context_count=int(row[4]),
                    quarantined_context_count=int(row[5]),
                    legacy_unknown_context_count=int(row[6]),
                    missing_context_count=int(row[7]),
                    current_actual_count=int(row[8]),
                    comparator_count=int(row[9]),
                    guidance_target_count=int(row[10]),
                    management_explanation_count=int(row[11]),
                    analyst_question_count=int(row[12]),
                )
            )
    for ticker, names in report_names.items():
        for name in sorted(names, key=str.casefold):
            if normalize_kpi_name(name) in matched_report_keys[ticker]:
                continue
            out.append(
                ScopedKpiDefinition(
                    ticker=ticker,
                    kpi_definition_id=None,
                    name=name,
                    reasons=("report",),
                    fact_count=0,
                    admitted_context_count=0,
                    quarantined_context_count=0,
                    legacy_unknown_context_count=0,
                    missing_context_count=0,
                    current_actual_count=0,
                    comparator_count=0,
                    guidance_target_count=0,
                    management_explanation_count=0,
                    analyst_question_count=0,
                )
            )
    return tuple(
        sorted(
            out, key=lambda item: (item.ticker, item.name.casefold(), item.kpi_definition_id or 0)
        )
    )
