"""Typed, durable lineage for a DCF calculation input set."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast


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


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def provenance_from_payload(value: object) -> DcfInputProvenance:
    """Parse a closed JSON-able provenance envelope into its typed form."""
    if isinstance(value, DcfInputProvenance):
        provenance = value
    else:
        if not isinstance(value, dict):
            raise ValueError("DCF proposal requires an input provenance object")
        payload = cast("dict[str, object]", value)
        required = {
            "input_sha256",
            "workbook_sha256",
            "engine_version",
            "inputs_as_of",
            "detail",
        }
        if set(payload) != required:
            raise ValueError("DCF provenance fields do not match the closed contract")
        input_sha = payload["input_sha256"]
        workbook_sha = payload["workbook_sha256"]
        engine_version = payload["engine_version"]
        raw_as_of = payload["inputs_as_of"]
        raw_detail = payload["detail"]
        if not isinstance(input_sha, str) or _SHA256_RE.fullmatch(input_sha) is None:
            raise ValueError("DCF provenance requires a valid input SHA-256")
        if not isinstance(workbook_sha, str) or _SHA256_RE.fullmatch(workbook_sha) is None:
            raise ValueError("DCF provenance requires a valid workbook SHA-256")
        if not isinstance(engine_version, str) or not engine_version.strip():
            raise ValueError("DCF provenance requires a non-empty engine version")
        if isinstance(raw_as_of, datetime):
            inputs_as_of: date | datetime = raw_as_of
        elif isinstance(raw_as_of, date):
            inputs_as_of = raw_as_of
        elif isinstance(raw_as_of, str):
            try:
                inputs_as_of = (
                    datetime.fromisoformat(raw_as_of)
                    if "T" in raw_as_of or " " in raw_as_of
                    else date.fromisoformat(raw_as_of)
                )
            except ValueError as exc:
                raise ValueError("DCF provenance inputs_as_of must be ISO-8601") from exc
        else:
            raise ValueError("DCF provenance inputs_as_of must be ISO-8601")
        if not isinstance(raw_detail, dict):
            raise ValueError("DCF provenance detail must be an object")
        provenance = DcfInputProvenance(
            input_sha256=input_sha,
            workbook_sha256=workbook_sha,
            engine_version=engine_version.strip(),
            inputs_as_of=inputs_as_of,
            detail=dict(cast("dict[str, object]", raw_detail)),
        )

    if _SHA256_RE.fullmatch(provenance.input_sha256) is None:
        raise ValueError("DCF provenance requires a valid input SHA-256")
    if (
        provenance.workbook_sha256 is None
        or _SHA256_RE.fullmatch(provenance.workbook_sha256) is None
    ):
        raise ValueError("DCF provenance requires a valid workbook SHA-256")
    if not provenance.engine_version.strip():
        raise ValueError("DCF provenance requires a non-empty engine version")
    detail = provenance.detail
    if not isinstance(detail, dict):
        raise ValueError("DCF provenance detail must be an object")
    raw_sources = detail.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("DCF provenance requires a non-empty input ledger")
    has_hashed_source = False
    for raw_source in cast("list[object]", raw_sources):
        if not isinstance(raw_source, dict):
            raise ValueError("each DCF provenance source must be an object")
        source = cast("dict[str, object]", raw_source)
        role = source.get("role")
        locator = next(
            (
                source.get(key)
                for key in ("path", "locator", "url", "source")
                if isinstance(source.get(key), str) and str(source.get(key)).strip()
            ),
            None,
        )
        if not isinstance(role, str) or not role.strip() or locator is None:
            raise ValueError("each DCF provenance source needs a role and locator")
        digest = source.get("sha256")
        if digest is not None:
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ValueError(f"invalid SHA-256 for DCF input source {role!r}")
            has_hashed_source = True
    if not has_hashed_source:
        raise ValueError("DCF provenance requires at least one hashed input source")
    return provenance


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        display = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        display = path.resolve()
    return str(display).replace("\\", "/")


def _file_source(*, role: str, path: Path, repo_root: Path) -> tuple[dict[str, object], datetime]:
    stat = path.stat()
    observed_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    payload = path.read_bytes()
    return (
        {
            "role": role,
            "path": _display_path(path, repo_root),
            "sha256": _sha256_bytes(payload),
            "bytes": len(payload),
            "observed_at": observed_at.isoformat(),
        },
        observed_at,
    )


def build_effective_provenance(
    *,
    ticker: str,
    repo_root: Path,
    workbook_path: Path,
    assumption_snapshot_json: str,
    engine_version: str,
    source_paths: tuple[tuple[str, Path], ...] = (),
) -> DcfInputProvenance:
    """Build a hashed ledger for a bespoke DCF's effective input set.

    The effective snapshot is itself a first-class hashed input, so in-code
    defaults, database-derived values, and file overrides are committed even
    when no single upstream file contains the final values used by the model.
    """
    workbook_path = workbook_path.resolve()
    if not workbook_path.is_file():
        raise FileNotFoundError(f"DCF workbook does not exist: {workbook_path}")
    raw_snapshot = cast(object, json.loads(assumption_snapshot_json))
    if not isinstance(raw_snapshot, dict):
        raise ValueError("DCF assumption snapshot must be a JSON object")
    snapshot = cast("dict[str, object]", raw_snapshot)
    canonical_snapshot = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    workbook_source, workbook_observed_at = _file_source(
        role="calculation_workbook",
        path=workbook_path,
        repo_root=repo_root,
    )
    observed_times = [workbook_observed_at]
    file_sources: list[dict[str, object]] = []
    for role, path in sorted(source_paths, key=lambda item: (item[0], str(item[1]))):
        if not path.is_file():
            continue
        source, observed_at = _file_source(role=role, path=path, repo_root=repo_root)
        file_sources.append(source)
        observed_times.append(observed_at)
    inputs_as_of = max(observed_times)
    snapshot_payload = canonical_snapshot.encode("utf-8")
    effective_source: dict[str, object] = {
        "role": "effective_assumptions",
        "locator": f"inline://dcf/{ticker.upper()}/effective-assumptions",
        "sha256": _sha256_bytes(snapshot_payload),
        "bytes": len(snapshot_payload),
        "observed_at": inputs_as_of.isoformat(),
    }
    sources = [workbook_source, effective_source, *file_sources]
    source_commitments = [
        {key: source.get(key) for key in ("role", "path", "locator", "sha256", "bytes")}
        for source in sources
    ]
    canonical_inputs = json.dumps(
        {
            "engine_version": engine_version,
            "ticker": ticker.upper(),
            "effective_assumptions": snapshot,
            "sources": source_commitments,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return DcfInputProvenance(
        input_sha256=_sha256_bytes(canonical_inputs.encode("utf-8")),
        workbook_sha256=str(workbook_source["sha256"]),
        engine_version=engine_version,
        inputs_as_of=inputs_as_of,
        detail={"sources": sources, "ticker": ticker.upper()},
    )
