"""Typed, durable lineage for a DCF calculation input set."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class DcfInputProvenance:
    """Hashes and clock needed to reproduce or audit a DCF result."""

    input_sha256: str
    workbook_sha256: str | None
    engine_version: str
    inputs_as_of: date | datetime
    detail: dict[str, object] | None = None

    def as_json(self) -> str:
        return json.dumps(self.detail or {}, sort_keys=True, separators=(",", ":"))

    def inputs_as_of_iso(self) -> str:
        return self.inputs_as_of.isoformat()
