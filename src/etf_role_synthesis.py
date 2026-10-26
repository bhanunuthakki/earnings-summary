"""Read retained ETF role artifacts. New ETF research generation is retired."""

from __future__ import annotations

from pathlib import Path

from llm_artifact_store import read_current
from report.etf_models import EtfRoleSynthesis

PURPOSE = "etf_role_synthesis"


def read_role_synthesis(db_path: Path, ticker: str) -> EtfRoleSynthesis | None:
    """Decode the current saved artifact, or return None when unavailable."""
    artifact = read_current(
        ticker=ticker.strip().upper(), purpose=PURPOSE, scope="etf", db_path=db_path
    )
    if artifact is None or artifact.content_json is None:
        return None
    try:
        return EtfRoleSynthesis.model_validate(artifact.content_json)
    except ValueError:
        return None


def synthesis_generated_at(db_path: Path, ticker: str) -> str | None:
    artifact = read_current(
        ticker=ticker.strip().upper(), purpose=PURPOSE, scope="etf", db_path=db_path
    )
    return artifact.generated_at.isoformat(timespec="minutes") if artifact is not None else None
