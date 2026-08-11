"""Deterministic provenance builder for bespoke DCF models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from dcf.provenance import build_effective_provenance


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_build_effective_provenance_hashes_workbook_snapshot_and_sources(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "dcf" / "BN.xlsx"
    workbook.parent.mkdir()
    workbook.write_bytes(b"workbook-v1")
    assumptions = tmp_path / "data" / "dcf_assumptions" / "BN.json"
    assumptions.parent.mkdir(parents=True)
    assumptions.write_text('{"sotp":{"marks":{"price":44.61}}}', encoding="utf-8")
    snapshot = json.dumps({"model": "holdco_sotp", "price": 44.61})

    provenance = build_effective_provenance(
        ticker="BN",
        repo_root=tmp_path,
        workbook_path=workbook,
        assumption_snapshot_json=snapshot,
        engine_version="holdco_sotp_v1",
        source_paths=(("assumption_overrides", assumptions),),
    )

    assert provenance.workbook_sha256 == _sha(workbook)
    assert len(provenance.input_sha256) == 64
    assert provenance.detail is not None
    sources = provenance.detail["sources"]
    assert isinstance(sources, list)
    source_objects = cast("list[object]", sources)
    assert all(isinstance(source, dict) for source in source_objects)
    source_rows = cast("list[dict[str, object]]", source_objects)
    assert {source["role"] for source in source_rows} == {
        "calculation_workbook",
        "effective_assumptions",
        "assumption_overrides",
    }
    assert all(len(str(source["sha256"])) == 64 for source in source_rows)


def test_build_effective_provenance_is_deterministic_and_input_sensitive(
    tmp_path: Path,
) -> None:
    workbook = tmp_path / "NU.xlsx"
    workbook.write_bytes(b"stable-workbook")

    def build(snapshot: str):
        return build_effective_provenance(
            ticker="NU",
            repo_root=tmp_path,
            workbook_path=workbook,
            assumption_snapshot_json=snapshot,
            engine_version="bank_platform_v1",
        )

    first = build('{"wacc":0.12}')
    replay = build('{"wacc":0.12}')
    changed = build('{"wacc":0.13}')

    assert first == replay
    assert first.input_sha256 != changed.input_sha256
