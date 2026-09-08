"""Closed source bindings for the approved quality roadmap export."""

import json
from pathlib import Path

import pytest

from quality.roadmap_reconciliation import Claim, StagedManifestModel
from quality.roadmap_source import RoadmapSourceError, load_roadmap_claim_map

ROOT = Path(__file__).resolve().parents[1]


def test_approved_map_binds_each_claim_to_exact_source_quote() -> None:
    claim_map = load_roadmap_claim_map(ROOT)

    assert (
        claim_map.document.sha256
        == "b889434fcfb5e6c2a7faee8accd79da782a3379c7bebe07d89934c7bbcbbf5fd"  # pragma: allowlist secret -- fixed approved document digest
    )
    assert len(claim_map.claims) == 16
    assert {claim.name for claim in claim_map.claims} == {
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
    document_lines = (ROOT / claim_map.document.path).read_text(encoding="utf-8").splitlines()
    assert all(claim.source_quote in document_lines for claim in claim_map.claims)


def test_map_rejects_document_substitution(tmp_path: Path) -> None:
    source = ROOT / "config" / "quality_roadmap_claims.json"
    target = tmp_path / "config" / source.name
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())
    roadmap = tmp_path / "docs" / "quality" / "quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_text("substituted roadmap\n", encoding="utf-8")

    with pytest.raises(RoadmapSourceError, match="hash"):
        load_roadmap_claim_map(tmp_path)


def test_map_rejects_unknown_claim_name(tmp_path: Path) -> None:
    source = ROOT / "config" / "quality_roadmap_claims.json"
    target = tmp_path / "config" / source.name
    target.parent.mkdir()
    target.write_text(
        source.read_text(encoding="utf-8").replace(
            '"production module count"', '"unsupported claim"', 1
        ),
        encoding="utf-8",
    )
    roadmap = tmp_path / "docs" / "quality" / "quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_bytes((ROOT / "docs" / "quality" / "quality-9plus-roadmap.md").read_bytes())

    with pytest.raises(RoadmapSourceError, match="unknown claim"):
        load_roadmap_claim_map(tmp_path)


def test_map_rejects_duplicate_claims_and_boolean_numeric_substitution(tmp_path: Path) -> None:
    source = ROOT / "config" / "quality_roadmap_claims.json"
    document = ROOT / "docs" / "quality" / "quality-9plus-roadmap.md"
    target = tmp_path / "config" / source.name
    target.parent.mkdir()
    roadmap = tmp_path / "docs" / "quality" / "quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_bytes(document.read_bytes())
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["claims"].append(payload["claims"][0])
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RoadmapSourceError, match="duplicate"):
        load_roadmap_claim_map(tmp_path)

    payload["claims"].pop()
    theme = next(claim for claim in payload["claims"] if claim["name"] == "theme live edge")
    theme["value"] = 1
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RoadmapSourceError, match="invalid"):
        load_roadmap_claim_map(tmp_path)


def test_map_rejects_scoped_commit_substitution(tmp_path: Path) -> None:
    target = tmp_path / "config" / "quality_roadmap_claims.json"
    target.parent.mkdir()
    target.write_text(
        (ROOT / "config/quality_roadmap_claims.json")
        .read_text(encoding="utf-8")
        .replace(
            "09d35d1a2785ff7e6a218031eb43952781be3a93",  # pragma: allowlist secret -- public historical commit
            "a" * 40,
        ),
        encoding="utf-8",
    )
    roadmap = tmp_path / "docs" / "quality" / "quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True)
    roadmap.write_bytes((ROOT / "docs/quality/quality-9plus-roadmap.md").read_bytes())
    with pytest.raises(RoadmapSourceError, match="scoped commit"):
        load_roadmap_claim_map(tmp_path)


def test_verified_claim_rejects_boolean_numeric_serialization_mismatch() -> None:
    with pytest.raises(ValueError, match="exact typed"):
        Claim(
            name="theme live edge",
            provisional_expected=True,
            observed=1,
            verdict="verified",
            scored_eligible=True,
            note="test",
        )


def test_staged_manifest_accepts_explicit_roadmap_claim_bytes_entry() -> None:
    entry = {"path": "roadmap-claims.json", "sha256": "a" * 64}
    manifest = StagedManifestModel.model_validate(
        {
            key: entry
            for key in (
                "architecture",
                "duplicates",
                "static",
                "test_db",
                "reachability",
                "roadmap",
            )
        }
        | {"roadmap_claims": entry}
    )
    assert manifest.roadmap_claims is not None
