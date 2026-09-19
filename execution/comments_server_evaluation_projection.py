"""Single-item evaluation projection shared by peek and label-review routes.

``build_cockpit_rows`` + ``build_work_os_evaluation`` remain the ONLY evaluation
resolver — there is no parallel projection. This helper hands the requested
ticker's cockpit rows to the same projection that serves
``GET /api/work-os/evaluation``, so a one-ticker peek pays one item's
projection cost instead of projecting every evaluation candidate. The
equivalence test in ``tests/test_comments_server_dashboard.py`` pins the
single-item result to the full-build item.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from pipeline.research_cockpit import CockpitRow
from pipeline.research_cockpit import build_cockpit_rows as _build_cockpit_rows
from pipeline.work_os_evaluation import (
    WorkOsEvaluationHydration,
    WorkOsEvaluationItem,
)
from pipeline.work_os_evaluation import build_work_os_evaluation as _build_work_os_evaluation

CockpitRowsBuilder = Callable[..., dict[str, list[CockpitRow]]]
EvaluationBuilder = Callable[..., WorkOsEvaluationHydration]
SafeTicker = Callable[[str], str]


def resolve_work_os_evaluation_item(
    conn: sqlite3.Connection,
    repo_root: Path,
    raw_ticker: str,
    *,
    safe_ticker: SafeTicker,
    build_cockpit_rows: CockpitRowsBuilder = _build_cockpit_rows,
    build_work_os_evaluation: EvaluationBuilder = _build_work_os_evaluation,
) -> WorkOsEvaluationItem | None:
    """Resolve one evaluation item through the evaluation-surface projection.

    Error policy belongs to the caller: projection failures propagate so each
    route family keeps its own contract (profile peeks swallow them into a
    404; label review surfaces the failure as a 500, exactly as before).
    """
    try:
        ticker = safe_ticker(raw_ticker)
    except ValueError:
        return None
    rows = [
        row
        for row in build_cockpit_rows(conn, repo_root).get("evaluation", [])
        if _normalized_ticker(row.base.ticker, safe_ticker) == ticker
    ]
    payload = build_work_os_evaluation(rows, repo_root, conn)
    return next((item for item in payload.items if item.ticker == ticker), None)


def _normalized_ticker(raw_ticker: str, safe_ticker: SafeTicker) -> str:
    """Normalize one row ticker exactly as the projection itself would."""
    try:
        return safe_ticker(raw_ticker)
    except ValueError:
        return ""
