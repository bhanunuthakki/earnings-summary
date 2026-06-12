"""Read/write API for exec_comp_packages + exec_holdings.

Persists annual DEF 14A Named-Executive-Officer compensation packages and
beneficial ownership snapshots. The schema captures the analytical questions
that matter for thesis-vs-incentive alignment:

  performance_metrics_json — JSON list of {metric, weight, threshold, target,
                              max, actual}. Powers the alignment lens: do
                              management's pay metrics match the thesis
                              tier-1 KPIs?
  peer_group_json          — JSON list of peer tickers. Peer-group shifts are
                              their own signal (when comp benchmarks drift,
                              management is rebasing).
  total_comp_granted vs realized — granted is the contract; realized is what
                              they banked. Big gaps are alignment signals.
  ceo_pay_ratio            — required CEO/median-employee disclosure.

Companion fetcher (Phase 5b LLM extractor) reads the proxy and emits an
exec_comp_packages row + 1+ exec_holdings rows per NEO. See
execution/extract_exec_comp.py.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PerformanceMetric:
    """One row in performance_metrics_json: the targets, weighting, and
    realized outcome for a single comp-design KPI."""

    metric: str  # e.g. "Revenue growth", "ROIC", "FCF margin"
    weight: float  # 0..1
    threshold: float | None = None  # min payout level
    target: float | None = None  # target payout level
    max: float | None = None  # cap payout level
    actual: float | None = None  # achieved value
    unit: str | None = None  # 'pct', 'usd_m', 'count'


@dataclass(slots=True)
class ExecCompPackage:
    ticker: str
    fiscal_year: int
    executive_name: str
    role: str | None = None
    is_ceo: bool = False
    currency: str = "USD"
    base_salary: float | None = None
    cash_bonus_target: float | None = None
    cash_bonus_actual: float | None = None
    equity_grant_value: float | None = None
    equity_grant_breakdown: dict[str, object] = field(default_factory=dict)
    other_comp: float | None = None
    total_comp_granted: float | None = None
    total_comp_realized: float | None = None
    performance_metrics: list[PerformanceMetric] = field(default_factory=list)
    peer_group: list[str] = field(default_factory=list)
    cic_terms: dict[str, object] = field(default_factory=dict)
    hedging_pledging_policy: str | None = None
    ceo_pay_ratio: float | None = None
    source_excerpt: str | None = None
    proxy_doc_id: int | None = None
    executive_entity_id: int | None = None
    id: int | None = None


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    try:
        path = _resolve(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = sqlite3.connect(str(path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exec_comp_packages'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _resolve(override: Path | str | None) -> Path | None:
    if override is not None:
        return Path(override)
    try:
        from db import DB_PATH

        return Path(cast("str", DB_PATH))
    except ImportError:
        return None


def upsert_package(package: ExecCompPackage, *, db_path: Path | str | None = None) -> int | None:
    """Insert or replace (on natural key: ticker + fiscal_year + executive_name)."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        existing = conn.execute(
            """
            SELECT id FROM exec_comp_packages
            WHERE ticker = ? AND fiscal_year = ? AND executive_name = ?
            """,
            (package.ticker, package.fiscal_year, package.executive_name),
        ).fetchone()

        now = datetime.now(UTC).isoformat()
        if existing is not None:
            conn.execute(
                """
                UPDATE exec_comp_packages SET
                    role = ?, is_ceo = ?, currency = ?,
                    base_salary = ?, cash_bonus_target = ?, cash_bonus_actual = ?,
                    equity_grant_value = ?, equity_grant_breakdown_json = ?,
                    other_comp = ?, total_comp_granted = ?, total_comp_realized = ?,
                    performance_metrics_json = ?, peer_group_json = ?,
                    cic_terms_json = ?, hedging_pledging_policy = ?,
                    ceo_pay_ratio = ?, source_excerpt = ?, extracted_at = ?,
                    proxy_doc_id = ?, executive_entity_id = ?
                WHERE id = ?
                """,
                (
                    package.role,
                    1 if package.is_ceo else 0,
                    package.currency,
                    package.base_salary,
                    package.cash_bonus_target,
                    package.cash_bonus_actual,
                    package.equity_grant_value,
                    json.dumps(package.equity_grant_breakdown)
                    if package.equity_grant_breakdown
                    else None,
                    package.other_comp,
                    package.total_comp_granted,
                    package.total_comp_realized,
                    json.dumps([_pm_dict(p) for p in package.performance_metrics])
                    if package.performance_metrics
                    else None,
                    json.dumps(package.peer_group) if package.peer_group else None,
                    json.dumps(package.cic_terms) if package.cic_terms else None,
                    package.hedging_pledging_policy,
                    package.ceo_pay_ratio,
                    package.source_excerpt,
                    now,
                    package.proxy_doc_id,
                    package.executive_entity_id,
                    int(existing["id"]),
                ),
            )
            conn.commit()
            return int(existing["id"])

        cur = conn.execute(
            """
            INSERT INTO exec_comp_packages(
                ticker, fiscal_year, proxy_doc_id, executive_entity_id,
                executive_name, role, is_ceo, currency,
                base_salary, cash_bonus_target, cash_bonus_actual,
                equity_grant_value, equity_grant_breakdown_json,
                other_comp, total_comp_granted, total_comp_realized,
                performance_metrics_json, peer_group_json,
                cic_terms_json, hedging_pledging_policy, ceo_pay_ratio,
                source_excerpt, extracted_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                package.ticker,
                package.fiscal_year,
                package.proxy_doc_id,
                package.executive_entity_id,
                package.executive_name,
                package.role,
                1 if package.is_ceo else 0,
                package.currency,
                package.base_salary,
                package.cash_bonus_target,
                package.cash_bonus_actual,
                package.equity_grant_value,
                json.dumps(package.equity_grant_breakdown)
                if package.equity_grant_breakdown
                else None,
                package.other_comp,
                package.total_comp_granted,
                package.total_comp_realized,
                json.dumps([_pm_dict(p) for p in package.performance_metrics])
                if package.performance_metrics
                else None,
                json.dumps(package.peer_group) if package.peer_group else None,
                json.dumps(package.cic_terms) if package.cic_terms else None,
                package.hedging_pledging_policy,
                package.ceo_pay_ratio,
                package.source_excerpt,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "exec_comp_upsert_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def fetch_packages(
    *,
    ticker: str,
    fiscal_year: int | None = None,
    db_path: Path | str | None = None,
) -> list[ExecCompPackage]:
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        if fiscal_year is not None:
            rows = conn.execute(
                "SELECT * FROM exec_comp_packages WHERE ticker = ? AND fiscal_year = ? ORDER BY is_ceo DESC, executive_name",
                (ticker, fiscal_year),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM exec_comp_packages WHERE ticker = ? ORDER BY fiscal_year DESC, is_ceo DESC, executive_name",
                (ticker,),
            ).fetchall()
        return [_row_to_package(r) for r in rows]
    finally:
        conn.close()


def _pm_dict(p: PerformanceMetric) -> dict[str, object]:
    return {
        "metric": p.metric,
        "weight": p.weight,
        "threshold": p.threshold,
        "target": p.target,
        "max": p.max,
        "actual": p.actual,
        "unit": p.unit,
    }


def _row_to_package(row: sqlite3.Row) -> ExecCompPackage:
    metrics_raw = row["performance_metrics_json"]
    metrics: list[PerformanceMetric] = []
    if metrics_raw:
        try:
            decoded = json.loads(metrics_raw)
            if isinstance(decoded, list):
                for entry in decoded:
                    if isinstance(entry, dict):
                        metrics.append(
                            PerformanceMetric(
                                metric=str(entry.get("metric", "")),
                                weight=float(entry.get("weight", 0)),
                                threshold=_f_or_none(entry.get("threshold")),
                                target=_f_or_none(entry.get("target")),
                                max=_f_or_none(entry.get("max")),
                                actual=_f_or_none(entry.get("actual")),
                                unit=str(entry.get("unit", "")) if entry.get("unit") else None,
                            )
                        )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return ExecCompPackage(
        id=int(row["id"]),
        ticker=row["ticker"],
        fiscal_year=int(row["fiscal_year"]),
        executive_name=row["executive_name"],
        role=row["role"],
        is_ceo=bool(row["is_ceo"]),
        currency=row["currency"],
        base_salary=_f_or_none(row["base_salary"]),
        cash_bonus_target=_f_or_none(row["cash_bonus_target"]),
        cash_bonus_actual=_f_or_none(row["cash_bonus_actual"]),
        equity_grant_value=_f_or_none(row["equity_grant_value"]),
        equity_grant_breakdown=_loads_dict(row["equity_grant_breakdown_json"]),
        other_comp=_f_or_none(row["other_comp"]),
        total_comp_granted=_f_or_none(row["total_comp_granted"]),
        total_comp_realized=_f_or_none(row["total_comp_realized"]),
        performance_metrics=metrics,
        peer_group=_loads_list_str(row["peer_group_json"]),
        cic_terms=_loads_dict(row["cic_terms_json"]),
        hedging_pledging_policy=row["hedging_pledging_policy"],
        ceo_pay_ratio=_f_or_none(row["ceo_pay_ratio"]),
        source_excerpt=row["source_excerpt"],
        proxy_doc_id=row["proxy_doc_id"],
        executive_entity_id=row["executive_entity_id"],
    )


def _f_or_none(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _loads_dict(raw: object) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        d = json.loads(str(raw))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _loads_list_str(raw: object) -> list[str]:
    if raw is None:
        return []
    try:
        v = json.loads(str(raw))
        return [str(x) for x in v] if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
