"""Read/write API for the predictions table.

Generalizes management_commitments. Every forward-looking claim — management
commitment, LLM bear-case hypothesis, 10-K risk factor, sell-side estimate —
lands here with a polymorphic source_kind. The matcher pipeline grades
``outcome`` against realized values from financial_facts / kpi_facts.

Patterns:
  record(...)       — insert one prediction. Idempotent on (ticker, source_kind,
                      source_doc_id, kpi_name, target_period) where applicable.
  pending_for_grading() — predictions whose target_period has passed but
                          outcome is still 'pending'. Drives the grader cron.
  grade(...)        — record an outcome. Best-effort; safe to re-run.
  history(ticker)   — recent predictions across all source_kinds — feeds the
                      "Predictions" tab on the workspace renderer.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from db_paths import resolve_db_path
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

SourceKind = Literal[
    "mgmt_commitment",
    "llm_bear_case",
    "risk_factor_10k",
    "sell_side",
    "analyst_consensus",
]

Outcome = Literal["pending", "met", "missed", "mixed", "unfalsifiable"]


@dataclass(slots=True)
class Prediction:
    id: int
    ticker: str
    source_kind: str
    prediction_md: str
    made_at: datetime
    target_period: datetime | None
    source_doc_id: int | None = None
    source_artifact_id: int | None = None
    source_excerpt: str | None = None
    kpi_name: str | None = None
    kpi_concept_id: int | None = None
    comparator: str | None = None
    target_value: float | None = None
    target_unit: str | None = None
    realized_value: float | None = None
    realized_doc_id: int | None = None
    outcome: str = "pending"
    outcome_confidence: float | None = None
    evaluated_at: datetime | None = None
    notes: str | None = None


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    try:
        path = resolve_db_path(db_path)
        if path is None or not Path(path).exists():
            return None
        conn = connect_sqlite(path, role=SQLiteConnectionRole.WRITER, schema_preflight=True)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
            ).fetchone()
            is None
        ):
            conn.close()
            return None
        return conn
    except (sqlite3.Error, OSError):
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def record(
    *,
    ticker: str,
    source_kind: SourceKind,
    prediction_md: str,
    made_at: datetime,
    target_period: datetime | None = None,
    source_doc_id: int | None = None,
    source_artifact_id: int | None = None,
    source_excerpt: str | None = None,
    kpi_name: str | None = None,
    kpi_concept_id: int | None = None,
    comparator: str | None = None,
    target_value: float | None = None,
    target_unit: str | None = None,
    db_path: Path | str | None = None,
) -> int | None:
    """Insert one prediction row. Idempotent on the natural key when defined:
    (ticker, source_kind, source_doc_id, kpi_name, target_period). When the
    natural key is incomplete (no kpi_name, no target_period), allows
    duplicates — matcher pipelines can dedupe later."""
    conn = _open(db_path)
    if conn is None:
        return None
    try:
        # Idempotency check
        if source_doc_id is not None and kpi_name is not None and target_period is not None:
            existing = conn.execute(
                """
                SELECT id FROM predictions
                WHERE ticker = ? AND source_kind = ? AND source_doc_id = ?
                  AND kpi_name = ? AND target_period = ?
                """,
                (ticker, source_kind, source_doc_id, kpi_name, target_period.isoformat()),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

        cur = conn.execute(
            """
            INSERT INTO predictions(
                ticker, source_kind, source_doc_id, source_artifact_id, source_excerpt,
                made_at, target_period, prediction_md,
                kpi_name, kpi_concept_id, comparator, target_value, target_unit,
                outcome, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)
            """,
            (
                ticker,
                source_kind,
                source_doc_id,
                source_artifact_id,
                source_excerpt,
                made_at.isoformat(),
                _iso(target_period),
                prediction_md,
                kpi_name,
                kpi_concept_id,
                comparator,
                target_value,
                target_unit,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        log.warning({"event": "prediction_record_failed", "error": str(exc)})
        return None
    finally:
        conn.close()


def grade(
    *,
    prediction_id: int,
    outcome: Outcome,
    realized_value: float | None = None,
    realized_doc_id: int | None = None,
    outcome_confidence: float | None = None,
    notes: str | None = None,
    evaluator_run_id: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    conn = _open(db_path)
    if conn is None:
        return False
    try:
        conn.execute(
            """
            UPDATE predictions
            SET outcome = ?, realized_value = ?, realized_doc_id = ?,
                outcome_confidence = ?, notes = ?, evaluator_run_id = ?,
                evaluated_at = ?
            WHERE id = ?
            """,
            (
                outcome,
                realized_value,
                realized_doc_id,
                outcome_confidence,
                notes,
                evaluator_run_id,
                datetime.now(UTC).isoformat(),
                prediction_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "prediction_grade_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


def pending_for_grading(
    *,
    ticker: str | None = None,
    as_of: datetime | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[Prediction]:
    """Predictions whose target_period has passed and outcome is 'pending'."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        cutoff = (as_of or datetime.now(UTC)).isoformat()
        if ticker is not None:
            rows = conn.execute(
                """
                SELECT * FROM predictions
                WHERE outcome = 'pending' AND target_period IS NOT NULL
                  AND target_period <= ? AND ticker = ?
                ORDER BY target_period ASC
                LIMIT ?
                """,
                (cutoff, ticker, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM predictions
                WHERE outcome = 'pending' AND target_period IS NOT NULL
                  AND target_period <= ?
                ORDER BY target_period ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [_row_to_prediction(r) for r in rows]
    finally:
        conn.close()


def history(
    *,
    ticker: str,
    source_kinds: list[str] | None = None,
    limit: int = 200,
    db_path: Path | str | None = None,
) -> list[Prediction]:
    """Recent predictions for a ticker. Optionally filter by source_kind list."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        if source_kinds:
            placeholders = ",".join("?" * len(source_kinds))
            rows = conn.execute(
                f"""
                SELECT * FROM predictions
                WHERE ticker = ? AND source_kind IN ({placeholders})
                ORDER BY made_at DESC
                LIMIT ?
                """,
                (ticker, *source_kinds, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM predictions
                WHERE ticker = ?
                ORDER BY made_at DESC
                LIMIT ?
                """,
                (ticker, limit),
            ).fetchall()
        return [_row_to_prediction(r) for r in rows]
    finally:
        conn.close()


def outcome_summary(
    *,
    ticker: str,
    source_kind: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Outcome histogram: {'met': N, 'missed': N, 'mixed': N, 'pending': N, 'unfalsifiable': N}.
    Used by the credibility-score lens + the workspace Predictions tab."""
    conn = _open(db_path)
    if conn is None:
        return {}
    try:
        params: tuple[object, ...]
        if source_kind:
            sql = (
                "SELECT outcome, COUNT(*) FROM predictions "
                "WHERE ticker = ? AND source_kind = ? GROUP BY outcome"
            )
            params = (ticker, source_kind)
        else:
            sql = "SELECT outcome, COUNT(*) FROM predictions WHERE ticker = ? GROUP BY outcome"
            params = (ticker,)
        rows = conn.execute(sql, params).fetchall()
        return {r[0]: int(r[1]) for r in rows}
    finally:
        conn.close()


def _row_to_prediction(row: sqlite3.Row) -> Prediction:
    return Prediction(
        id=int(row["id"]),
        ticker=row["ticker"],
        source_kind=row["source_kind"],
        prediction_md=row["prediction_md"],
        made_at=_parse_dt(row["made_at"]) or datetime.now(UTC),
        target_period=_parse_dt(row["target_period"]),
        source_doc_id=row["source_doc_id"],
        source_artifact_id=row["source_artifact_id"],
        source_excerpt=row["source_excerpt"],
        kpi_name=row["kpi_name"],
        kpi_concept_id=row["kpi_concept_id"],
        comparator=row["comparator"],
        target_value=float(row["target_value"]) if row["target_value"] is not None else None,
        target_unit=row["target_unit"],
        realized_value=float(row["realized_value"]) if row["realized_value"] is not None else None,
        realized_doc_id=row["realized_doc_id"],
        outcome=row["outcome"],
        outcome_confidence=float(row["outcome_confidence"])
        if row["outcome_confidence"] is not None
        else None,
        evaluated_at=_parse_dt(row["evaluated_at"]),
        notes=row["notes"],
    )
