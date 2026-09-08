"""Fail-closed bindings between roadmap claims and the approved export."""

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)

ROADMAP_SOURCE_PATH = "docs/quality/quality-9plus-roadmap.md"
ROADMAP_CLAIM_MAP_PATH = "config/quality_roadmap_claims.json"
ROADMAP_CLAIM_NAMES = frozenset(
    {
        "production module count",
        "production noncomment LOC",
        "scc count",
        "largest scc",
        "exact duplicate groups",
        "exact duplicate functions",
        "ruff diagnostics",
        "pyright diagnostics",
        "test files",
        "upgrade builders",
        "migrated builders",
        "ddl builders",
        "theme live edge",
        "refetch absence",
        "full suite seconds",
        "unreachable scripts",
    }
)


class RoadmapSourceError(ValueError):
    """The checked-in map or its exact approved source cannot be trusted."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoadmapDocument(_Strict):
    path: Literal["docs/quality/quality-9plus-roadmap.md"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoped_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    linear_document_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    linear_document_status: Literal["externally_unverified"]


class RoadmapClaimBinding(_Strict):
    name: str = Field(min_length=1, max_length=200)
    metric_definition: str = Field(min_length=1, max_length=300)
    value: StrictInt | StrictFloat | StrictBool
    unit: str = Field(min_length=1, max_length=100)
    qualifier: Literal[
        "verified_baseline",
        "provisional_inventory",
        "historical_receipt",
        "audit_correction",
    ]
    source_key: Literal["architecture", "duplicates", "static", "test_db", "reachability"] | None
    extractor_locator: str = Field(min_length=1, max_length=300)
    source_line: int = Field(ge=1)
    source_quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def value_matches_declared_unit(self) -> "RoadmapClaimBinding":
        if self.unit == "boolean":
            if type(self.value) is not bool:
                raise ValueError("boolean roadmap value must be a boolean")
        elif type(self.value) not in (int, float):
            raise ValueError("numeric roadmap value must not be a boolean")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("numeric roadmap value must be finite")
        return self


class RoadmapClaimMap(_Strict):
    schema_version: Literal["roadmap-claim-map-v1"]
    document: RoadmapDocument
    claims: tuple[RoadmapClaimBinding, ...] = Field(min_length=1)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RoadmapSourceError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise RoadmapSourceError(f"source path is not a regular lexical file: {relative}")
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RoadmapSourceError(f"source path is unavailable: {relative}") from exc
    return path


def load_roadmap_claim_map(root: Path) -> RoadmapClaimMap:
    """Load the exact map only when it binds the approved export byte-for-byte."""

    return load_roadmap_claim_map_with_raw(root)[0]


def load_roadmap_claim_map_with_raw(root: Path) -> tuple[RoadmapClaimMap, bytes]:
    """Return the validated map and exact bytes that established it."""

    map_path = _safe_file(root, ROADMAP_CLAIM_MAP_PATH)
    roadmap_path = _safe_file(root, ROADMAP_SOURCE_PATH)
    try:
        raw_map = map_path.read_bytes()
        roadmap_raw = roadmap_path.read_bytes()
    except OSError as exc:
        raise RoadmapSourceError("roadmap claim map is invalid") from exc
    return parse_roadmap_claim_map(raw_map, roadmap_raw), raw_map


def parse_roadmap_claim_map(raw_map: bytes, roadmap_raw: bytes) -> RoadmapClaimMap:
    """Validate map bytes against the exact roadmap bytes supplied by the caller."""

    try:
        parsed = json.loads(raw_map, object_pairs_hook=_reject_duplicate_keys)
        claim_map = RoadmapClaimMap.model_validate(parsed)
        lines = tuple(roadmap_raw.decode("utf-8").splitlines())
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise RoadmapSourceError("roadmap claim map is invalid") from exc
    actual_hash = hashlib.sha256(roadmap_raw).hexdigest()
    if claim_map.document.path != ROADMAP_SOURCE_PATH or claim_map.document.sha256 != actual_hash:
        raise RoadmapSourceError("approved roadmap hash does not match the mapped source")
    scoped_line = f"Commit scoped: `{claim_map.document.scoped_commit}`"
    if scoped_line not in lines:
        raise RoadmapSourceError("approved roadmap scoped commit does not match declaration")
    claim_names = [item.name for item in claim_map.claims]
    if len(set(claim_names)) != len(claim_names):
        raise RoadmapSourceError("roadmap claim map has duplicate claim names")
    names = set(claim_names)
    if names != set(ROADMAP_CLAIM_NAMES):
        raise RoadmapSourceError("roadmap claim map has unknown claim names or is incomplete")
    for binding in claim_map.claims:
        if (
            binding.source_line > len(lines)
            or lines[binding.source_line - 1] != binding.source_quote
        ):
            raise RoadmapSourceError(f"roadmap source quote does not bind: {binding.name}")
    return claim_map
