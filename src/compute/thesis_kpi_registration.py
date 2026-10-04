"""Register admitted raw KPI series without duplicating thesis thresholds.

The holdings file owns the approved candidates and disclosure watch list.
Registration does not admit observations. The existing thesis evaluator owns
all numerical levels and persistence requirements.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from compute.thesis_metric_series import MetricExpression, calculate_metric_series
from identity import DEFAULT_USER_ID
from pipeline.kpi_report_reference_resolver import (
    report_kpi_reference_at,
    verified_report_kpi_reference_definition,
)
from user_state._db import now_iso


class RegistryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=256)


class CandidateRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    status: str
    reason_code: str | None = None
    comparable_quarters: int = 0


def refresh_thesis_kpi_registration(
    conn: sqlite3.Connection,
    *,
    holdings_dir: Path,
    ticker: str,
    apply: bool = False,
    user_id: str = DEFAULT_USER_ID,
) -> tuple[CandidateRegistration, ...]:
    """Inspect candidates; optionally register eligible ones in this transaction.

    Existing owner-authored registry rows retain their settings. New rows have
    no scalar threshold and are not thesis breakers. Eight admitted comparable
    quarters are required by the existing inflection detector.
    """
    payload: object = json.loads(
        (holdings_dir / f"{ticker.upper()}.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise ValueError("holdings configuration must be an object")
    contents = cast("dict[str, object]", payload)
    raw_candidates = contents.get("kpi_registry_candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("kpi_registry_candidates must be an array")
    candidates = [
        RegistryCandidate.model_validate(row) for row in cast("list[object]", raw_candidates)
    ]
    if len({row.name for row in candidates}) != len(candidates):
        raise ValueError("duplicate registry candidate")
    repo_root = holdings_dir.resolve().parents[1]
    results: list[CandidateRegistration] = []
    for index, candidate in enumerate(candidates):
        reference = report_kpi_reference_at(
            repo_root,
            ticker=ticker,
            json_pointer=f"/kpi_registry_candidates/{index}/name",
        )
        verified = (
            None
            if reference is None
            else verified_report_kpi_reference_definition(
                conn,
                repo_root=repo_root,
                user_id=user_id,
                reference=reference,
            )
        )
        if verified is None:
            results.append(
                CandidateRegistration(
                    name=candidate.name,
                    status="pending",
                    reason_code="unverified_report_kpi_reference",
                )
            )
            continue
        series = calculate_metric_series(
            conn,
            ticker,
            MetricExpression(
                operation="level",
                source="kpi",
                name=verified.definition_name,
            ),
        )
        if series.status != "available" or len(series.points) < 8:
            results.append(
                CandidateRegistration(
                    name=candidate.name,
                    status="pending",
                    comparable_quarters=len(series.points),
                    reason_code=series.reason_code
                    or "inflection_requires_eight_comparable_quarters",
                )
            )
            continue
        existing = conn.execute(
            "SELECT id FROM user_kpi_registry WHERE user_id=? AND ticker=? AND kpi_name=?",
            (user_id, ticker.upper(), verified.definition_name),
        ).fetchone()
        if apply and existing is None:
            now = now_iso()
            conn.execute(
                "INSERT INTO user_kpi_registry(user_id,ticker,kpi_name,threshold_direction,"
                "threshold_value,is_thesis_breaker,scaffold_source,notes,created_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,0,?,?,?,?)",
                (
                    user_id,
                    ticker.upper(),
                    verified.definition_name,
                    "approved_holdings",
                    "Numerical thresholds and persistence belong to the thesis evaluator.",
                    now,
                    now,
                ),
            )
        results.append(
            CandidateRegistration(
                name=candidate.name,
                status="registered" if apply or existing is not None else "eligible",
                comparable_quarters=len(series.points),
            )
        )
    return tuple(results)
