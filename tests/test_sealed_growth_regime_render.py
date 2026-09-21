"""Source-bound renders from a synthetic admitted/resolved graph, no production IO."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from pipeline.sealed_growth_projection import (
    FIXED_COHORT,
    GrowthRenderManifest,
    SealedFile,
    SealedSourceFile,
    load_growth_manifest,
    policy_bundle_sha256,
    project_growth_regimes,
)
from pipeline.three_regime_renderer import ThreeRegimeDeterministicRenderer
from provenance.source_regime import SourceRegime
from report.offline_artifact import OfflineBoundaryError
from tests.test_canonical_growth_screen import STAMP, seed_growth_graph


def manifest_fixture(
    tmp_path: Path, migrated_db: Callable[..., Path], *, published: bool = True
) -> tuple[Path, str, GrowthRenderManifest]:
    database = migrated_db(tmp_path / "sealed-fixture.db")
    with sqlite3.connect(database) as conn:
        seed_growth_graph(conn, tmp_path, published_at=STAMP if published else None)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    source = tmp_path / "synthetic-quarterly-source.json"
    manifest = GrowthRenderManifest(
        state_kind="sealed_disposable_snapshot",
        as_of=STAMP.date(),
        policy_bundle_sha256=policy_bundle_sha256(),
        database=SealedFile(
            path=database,
            sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
            size_bytes=database.stat().st_size,
        ),
        source_files=(
            SealedSourceFile(
                path=source,
                document_version_id="document-1",
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                size_bytes=source.stat().st_size,
            ),
        ),
    )
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json(indent=2))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), manifest


def test_actual_projection_enforces_regime_and_publication_clock(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    _, _, manifest = manifest_fixture(tmp_path, migrated_db)
    projections = project_growth_regimes(manifest)
    assert len(projections) == 18
    wix = {item.regime: item for item in projections if item.ticker == "WIX"}
    assert wix[SourceRegime.COMBINED].status == "available"
    assert wix[SourceRegime.NORMALIZED_VENDOR_ONLY].status == "available"
    assert wix[SourceRegime.OFFICIAL_PRIMARY].status == "unavailable"
    assert wix[SourceRegime.OFFICIAL_PRIMARY].reason_codes == (
        "selected_canonical_source_not_admitted_by_regime",
    )
    assert all(item.status == "unavailable" for item in projections if item.ticker != "WIX")
    assert not Path(str(manifest.database.path) + "-shm").exists()


def test_missing_publication_timestamp_is_not_invented(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    _, _, manifest = manifest_fixture(tmp_path, migrated_db, published=False)
    wix = [item for item in project_growth_regimes(manifest) if item.ticker == "WIX"]
    assert all(item.status == "unavailable" for item in wix)
    assert all(
        "selected_canonical_source_not_admitted_by_regime" in item.reason_codes for item in wix
    )


def test_two_real_passes_emit_hash_bound_artifacts_and_preserve_hold(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, digest, manifest = manifest_fixture(tmp_path, migrated_db)
    renderer = ThreeRegimeDeterministicRenderer(
        tmp_path / "output", input_manifest=path, expected_manifest_sha256=digest
    )
    receipt = renderer.render_all_regimes_for_cohort(list(FIXED_COHORT), STAMP.date())
    assert receipt.status == "HOLD"
    assert receipt.all_two_pass_verified
    assert receipt.total_render_outputs == 18
    assert receipt.input_manifest_sha256 == digest
    assert receipt == renderer.render_all_regimes_for_cohort(list(FIXED_COHORT), STAMP.date())
    for output in receipt.render_outputs:
        directory = tmp_path / "output" / output.regime.value / output.ticker
        assert (
            hashlib.sha256((directory / "report.html").read_bytes()).hexdigest()
            == output.html_sha256
        )
        assert (
            hashlib.sha256((directory / "sections.json").read_bytes()).hexdigest()
            == output.sections_json_sha256
        )
        assert (directory / "numeric_provenance.json").exists()
    official = tmp_path / "output" / "REGIME_1_SEC_IR_PRIMARY" / "WIX"
    assert json.loads((official / "numeric_provenance.json").read_text())["calculation"] is None
    combined = tmp_path / "output" / "REGIME_2_COMBINED" / "WIX"
    assert (
        json.loads((combined / "numeric_provenance.json").read_text())["calculation"]["revenue_yoy"]
        == "0.3"
    )
    assert (
        hashlib.sha256(manifest.database.path.read_bytes()).hexdigest() == manifest.database.sha256
    )


def test_unclassified_or_changed_source_and_database_are_rejected(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    path, digest, manifest = manifest_fixture(tmp_path, migrated_db)
    with pytest.raises(OfflineBoundaryError, match="manifest hash"):
        load_growth_manifest(path, "0" * 64)
    no_source = manifest.model_copy(update={"source_files": ()})
    with pytest.raises(OfflineBoundaryError, match="absent or mismatched"):
        project_growth_regimes(no_source)
    source = manifest.source_files[0].path
    original = source.read_bytes()
    source.write_bytes(original + b" ")
    with pytest.raises(OfflineBoundaryError, match="does not match"):
        project_growth_regimes(manifest)
    source.write_bytes(original)
    Path(str(manifest.database.path) + "-wal").write_bytes(b"")
    with pytest.raises(OfflineBoundaryError, match="sidecars"):
        project_growth_regimes(manifest)
    assert load_growth_manifest(path, digest)[0] == manifest


def test_manifest_rejects_policy_and_cohort_drift(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    _, _, manifest = manifest_fixture(tmp_path, migrated_db)
    payload = manifest.model_dump(mode="json")
    payload["policy_bundle_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="policy hash"):
        GrowthRenderManifest.model_validate_json(json.dumps(payload))
    payload["policy_bundle_sha256"] = policy_bundle_sha256()
    payload["cohort"] = ["WIX"]
    with pytest.raises(ValueError, match="six-ticker"):
        GrowthRenderManifest.model_validate_json(json.dumps(payload))


def test_cli_renders_supported_slice_but_exits_nonzero_until_full_acceptance(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    import subprocess
    import sys

    path, digest, _manifest = manifest_fixture(tmp_path, migrated_db)
    receipt_path = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            "execution/render_three_regimes.py",
            "--input-manifest",
            str(path),
            "--manifest-sha256",
            digest,
            "--as-of-date",
            STAMP.date().isoformat(),
            "--output-dir",
            str(tmp_path / "output"),
            "--output-receipt",
            str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "HOLD"
    assert receipt["total_render_outputs"] == 18
    assert receipt["all_two_pass_verified"] is True


def test_configured_application_database_is_never_opened_immutable(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, manifest = manifest_fixture(tmp_path, migrated_db)
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(manifest.database.path))
    with pytest.raises(OfflineBoundaryError, match="configured application database"):
        project_growth_regimes(manifest)


@pytest.mark.parametrize("change", ["atime", "ctime"])
def test_sealed_file_identity_ignores_access_time_but_retains_change_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import os

    from pipeline.sealed_growth_projection import verify_sealed_file

    path = tmp_path / "source.bin"
    path.write_bytes(b"exact source")
    file = SealedFile(
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size_bytes=path.stat().st_size,
    )
    original_stat = Path.stat
    calls = 0

    def changing_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal calls
        observed = original_stat(self, follow_symlinks=follow_symlinks)
        if self != path or not follow_symlinks:
            return observed
        calls += 1
        fields = list(observed)
        fields[7] += calls
        return os.stat_result(
            fields,
            {
                "st_atime_ns": observed.st_atime_ns + calls * 1_000_000_000,
                "st_mtime_ns": observed.st_mtime_ns,
                "st_ctime_ns": observed.st_ctime_ns + (calls if change == "ctime" else 0),
            },
        )

    monkeypatch.setattr(Path, "stat", changing_stat)
    if change == "ctime":
        with pytest.raises(OfflineBoundaryError, match="changed"):
            verify_sealed_file(file)
    else:
        verify_sealed_file(file)
