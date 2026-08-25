"""Truthful availability for the read-only ``/dcf/<ticker>`` doorway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class DcfRouteArtifact:
    """One concrete target the DCF route can resolve without a database row."""

    kind: str
    target: str
    sheet_id: str | None = None


def resolve_dcf_route_artifact(repo_root: Path, ticker: str) -> DcfRouteArtifact | None:
    """Return the same sheet/workbook fallback chain used by ``/dcf/<T>``."""

    normalized = ticker.strip().upper()
    holdings = repo_root / "micro_thesis" / "holdings" / f"{normalized}.json"
    if holdings.exists():
        try:
            raw = json.loads(holdings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("dcf_defaults"), dict):
            defaults = cast("dict[str, object]", raw["dcf_defaults"])
            sheet_id = defaults.get("gsheet_id")
            if isinstance(sheet_id, str) and sheet_id.strip():
                clean_id = sheet_id.strip()
                return DcfRouteArtifact(
                    kind="sheet",
                    target=f"https://docs.google.com/spreadsheets/d/{clean_id}/edit",
                    sheet_id=clean_id,
                )
    workbook = repo_root / "dcf" / f"{normalized}.xlsx"
    if workbook.exists() and workbook.is_file():
        return DcfRouteArtifact(kind="workbook", target=workbook.as_posix())
    research_dir = repo_root / "output" / "research" / normalized
    dated = sorted(research_dir.glob("*_dcf.xlsx")) if research_dir.exists() else []
    if dated:
        return DcfRouteArtifact(kind="dated_workbook", target=dated[-1].as_posix())
    return None


__all__ = ["DcfRouteArtifact", "resolve_dcf_route_artifact"]
