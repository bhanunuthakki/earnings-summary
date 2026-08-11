"""Typed, durable lineage for a DCF calculation input set."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
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


def _canonical_snapshot(assumption_snapshot_json: str) -> dict[str, object]:
    raw_snapshot = cast(object, json.loads(assumption_snapshot_json))
    if not isinstance(raw_snapshot, dict):
        raise ValueError("DCF assumption snapshot must be a JSON object")
    return dict(cast("dict[str, object]", raw_snapshot))


def _logical_workbook_sha256(path: Path) -> str:
    """Hash XLSX member contents, independent of ZIP timestamps/compression."""
    if not zipfile.is_zipfile(path):
        return _sha256_bytes(path.read_bytes())
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
        for name in names:
            encoded_name = name.encode("utf-8")
            payload = archive.read(name)
            digest.update(len(encoded_name).to_bytes(8, "big"))
            digest.update(encoded_name)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return digest.hexdigest()


def _source_locator(source: dict[str, object]) -> str:
    for key in ("path", "locator", "url", "source"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("each DCF provenance source needs a role and locator")


def _canonical_source_commitments(
    sources: list[dict[str, object]],
) -> list[dict[str, str]]:
    commitments: list[dict[str, str]] = []
    for source in sources:
        role = source.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("each DCF provenance source needs a role and locator")
        digest_key = "logical_sha256" if role == "calculation_workbook" else "sha256"
        digest = source.get(digest_key)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"DCF provenance source {role!r} lacks canonical content hash")
        commitments.append(
            {"role": role.strip(), "locator": _source_locator(source), "sha256": digest}
        )
    return sorted(commitments, key=lambda item: (item["role"], item["locator"]))


def _canonical_input_sha256(
    *,
    ticker: str,
    engine_version: str,
    snapshot: dict[str, object],
    sources: list[dict[str, object]],
    additional_inputs: dict[str, object] | None = None,
) -> str:
    canonical_inputs = json.dumps(
        {
            "contract_version": "dcf_input_v2",
            "engine_version": engine_version,
            "ticker": ticker.upper(),
            "effective_assumptions": snapshot,
            "additional_inputs": additional_inputs or {},
            "sources": _canonical_source_commitments(sources),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return _sha256_bytes(canonical_inputs.encode("utf-8"))


def _display_path(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        display = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"DCF input file must stay under repo root: {resolved}") from exc
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


def _resolve_ledger_path(repo_root: Path, locator: str) -> Path:
    candidate = (repo_root.resolve() / Path(locator)).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"DCF input ledger path escapes repo root: {locator}") from exc
    return candidate


def verify_effective_provenance(
    provenance: DcfInputProvenance,
    *,
    ticker: str,
    repo_root: Path,
    assumption_snapshot_json: str,
) -> None:
    """Recompute commitments and bind every file source to its current bytes."""
    provenance = provenance_from_payload(provenance)
    snapshot = _canonical_snapshot(assumption_snapshot_json)
    detail = provenance.detail or {}
    if detail.get("ticker") != ticker.upper():
        raise ValueError("DCF provenance ticker does not match the proposed row")
    raw_sources = detail.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("DCF provenance requires a non-empty input ledger")
    source_items = cast("list[object]", raw_sources)
    sources = [
        dict(cast("dict[str, object]", item)) for item in source_items if isinstance(item, dict)
    ]
    if len(sources) != len(source_items):
        raise ValueError("each DCF provenance source must be an object")
    identities: set[tuple[str, str]] = set()
    workbook_sources: list[dict[str, object]] = []
    effective_sources: list[dict[str, object]] = []
    for source in sources:
        role = source.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError("each DCF provenance source needs a role and locator")
        locator = _source_locator(source)
        identity = (role.strip(), locator)
        if identity in identities:
            raise ValueError("DCF provenance input ledger contains duplicate source identity")
        identities.add(identity)
        if role == "calculation_workbook":
            workbook_sources.append(source)
        if role == "effective_assumptions":
            effective_sources.append(source)
        path_value = source.get("path")
        if path_value is None:
            continue
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"DCF provenance source {role!r} has an invalid file path")
        path = _resolve_ledger_path(repo_root, path_value)
        if not path.is_file():
            raise ValueError(f"DCF provenance source file does not exist: {path_value}")
        current_sha = _sha256_bytes(path.read_bytes())
        if source.get("sha256") != current_sha:
            raise ValueError(f"DCF provenance current file hash mismatch: {path_value}")
        if role == "calculation_workbook":
            logical_sha = source.get("logical_sha256")
            if not isinstance(logical_sha, str) or _SHA256_RE.fullmatch(logical_sha) is None:
                raise ValueError("DCF calculation workbook requires a logical SHA-256")
            if logical_sha != _logical_workbook_sha256(path):
                raise ValueError("DCF calculation workbook logical hash mismatch")
    if len(workbook_sources) != 1:
        raise ValueError("DCF provenance requires exactly one calculation workbook")
    workbook_sha = workbook_sources[0].get("sha256")
    if workbook_sha != provenance.workbook_sha256:
        raise ValueError("DCF provenance workbook SHA-256 does not match its input ledger")
    if len(effective_sources) != 1:
        raise ValueError("DCF provenance requires exactly one effective-assumptions source")
    canonical_snapshot = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if effective_sources[0].get("sha256") != _sha256_bytes(canonical_snapshot):
        raise ValueError("DCF provenance effective-assumptions hash does not match the row")
    raw_additional_inputs = detail.get("additional_inputs", {})
    if not isinstance(raw_additional_inputs, dict):
        raise ValueError("DCF provenance additional_inputs must be an object")
    additional_inputs = dict(cast("dict[str, object]", raw_additional_inputs))
    expected_input_sha = _canonical_input_sha256(
        ticker=ticker,
        engine_version=provenance.engine_version,
        snapshot=snapshot,
        sources=sources,
        additional_inputs=additional_inputs,
    )
    if provenance.input_sha256 != expected_input_sha:
        raise ValueError("DCF provenance input SHA-256 does not match canonical commitments")


def build_effective_provenance(
    *,
    ticker: str,
    repo_root: Path,
    workbook_path: Path,
    assumption_snapshot_json: str,
    engine_version: str,
    source_paths: tuple[tuple[str, Path], ...] = (),
    additional_inputs: dict[str, object] | None = None,
) -> DcfInputProvenance:
    """Build a reproducible ledger with logical input and exact artifact hashes."""
    workbook_path = workbook_path.resolve()
    if not workbook_path.is_file():
        raise FileNotFoundError(f"DCF workbook does not exist: {workbook_path}")
    snapshot = _canonical_snapshot(assumption_snapshot_json)
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
    workbook_source["logical_sha256"] = _logical_workbook_sha256(workbook_path)
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
    provenance = DcfInputProvenance(
        input_sha256=_canonical_input_sha256(
            ticker=ticker,
            engine_version=engine_version,
            snapshot=snapshot,
            sources=sources,
            additional_inputs=additional_inputs,
        ),
        workbook_sha256=str(workbook_source["sha256"]),
        engine_version=engine_version,
        inputs_as_of=inputs_as_of,
        detail={
            "sources": sources,
            "ticker": ticker.upper(),
            "additional_inputs": additional_inputs or {},
            **(
                {"market_price": additional_inputs["market_price"]}
                if additional_inputs is not None
                and isinstance(additional_inputs.get("market_price"), dict)
                else {}
            ),
        },
    )
    verify_effective_provenance(
        provenance,
        ticker=ticker,
        repo_root=repo_root,
        assumption_snapshot_json=assumption_snapshot_json,
    )
    return provenance
