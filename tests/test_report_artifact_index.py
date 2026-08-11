"""Immutable report manifests and the compact Brief Library index."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from report.artifacts import (
    RenderedReportBody,
    ReportArtifactRef,
    ReportInteractionManifest,
    ReportSectionRef,
    load_report_artifact_index,
    persist_report_artifact,
    reconcile_legacy_workspace_reports,
)


def _body() -> RenderedReportBody:
    return RenderedReportBody.from_html(
        ticker="NU",
        report_date=date(2026, 8, 10),
        body_html='<main data-report-body="v1"><section id="overview">NU</section></main>',
        sections=(ReportSectionRef(section_id="overview", label="Overview", group_id="overview"),),
        interaction_manifest=ReportInteractionManifest(),
    )


def test_persist_report_artifact_writes_manifest_body_and_compact_index(tmp_path: Path) -> None:
    workspace = tmp_path / "output" / "research" / "NU" / "2026-08-10_workspace.html"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("<html>standalone</html>", encoding="utf-8")

    ref = persist_report_artifact(
        repo_root=tmp_path,
        body=_body(),
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
        provenance_ref=42,
    )

    assert ref.reader_mode == "shared_body"
    assert ref.provenance_ref == 42
    assert ref.body_path is not None
    reader_html = (tmp_path / ref.body_path).read_text(encoding="utf-8")
    assert "NU" in reader_html
    assert "<script" not in reader_html
    manifest_path = tmp_path / ref.manifest_path
    assert ReportArtifactRef.model_validate_json(manifest_path.read_text(encoding="utf-8")) == ref
    assert (tmp_path / ref.standalone_path).read_text(encoding="utf-8") == (
        "<html>standalone</html>"
    )
    index = load_report_artifact_index(tmp_path)
    assert index.items == (ref,)


def test_reconcile_legacy_reports_indexes_without_rebuilding_historical_facts(
    tmp_path: Path,
) -> None:
    research = tmp_path / "output" / "research" / "NU"
    research.mkdir(parents=True)
    workspace = research / "2026-07-01_workspace.html"
    workspace.write_text("<html>historical fact set</html>", encoding="utf-8")

    result = reconcile_legacy_workspace_reports(tmp_path)

    assert result.added == 1
    ref = load_report_artifact_index(tmp_path).items[0]
    assert ref.reader_mode == "legacy_standalone"
    assert ref.body_path is None
    assert ref.report_date == date(2026, 7, 1)
    assert (tmp_path / ref.standalone_path).read_text(encoding="utf-8") == (
        "<html>historical fact set</html>"
    )
    manifest = json.loads((research / "2026-07-01_manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_id"] == ref.artifact_id


def test_index_excludes_entries_whose_standalone_artifact_was_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "output" / "research" / "NU" / "2026-08-10_workspace.html"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("<html>standalone</html>", encoding="utf-8")
    persist_report_artifact(
        repo_root=tmp_path,
        body=_body(),
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )
    index = load_report_artifact_index(tmp_path)
    (tmp_path / index.items[0].standalone_path).unlink()

    assert load_report_artifact_index(tmp_path).items == ()


def test_same_day_regeneration_preserves_both_immutable_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "output" / "research" / "NU" / "2026-08-10_workspace.html"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("<html>first standalone</html>", encoding="utf-8")
    first_body = _body()
    first = persist_report_artifact(
        repo_root=tmp_path,
        body=first_body,
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )

    workspace.write_text("<html>second standalone</html>", encoding="utf-8")
    second_body = RenderedReportBody.from_html(
        ticker="NU",
        report_date=date(2026, 8, 10),
        body_html='<main data-report-body="v1"><section>second</section></main>',
        sections=first_body.sections,
        interaction_manifest=first_body.interaction_manifest,
    )
    second = persist_report_artifact(
        repo_root=tmp_path,
        body=second_body,
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 13, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )

    assert first.artifact_id != second.artifact_id
    assert first.body_path is not None
    assert second.body_path is not None
    assert (tmp_path / first.standalone_path).read_text(encoding="utf-8") == (
        "<html>first standalone</html>"
    )
    assert (tmp_path / second.standalone_path).read_text(encoding="utf-8") == (
        "<html>second standalone</html>"
    )
    assert "overview" in (tmp_path / first.body_path).read_text(encoding="utf-8")
    assert "second" in (tmp_path / second.body_path).read_text(encoding="utf-8")
    assert load_report_artifact_index(tmp_path).items == (second, first)


def test_same_content_rerun_does_not_mutate_existing_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "output" / "research" / "NU" / "2026-08-10_workspace.html"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("<html>first wrapper</html>", encoding="utf-8")
    first = persist_report_artifact(
        repo_root=tmp_path,
        body=_body(),
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )

    workspace.write_text("<html>changed wrapper</html>", encoding="utf-8")
    repeated = persist_report_artifact(
        repo_root=tmp_path,
        body=_body(),
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 13, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )

    assert repeated == first
    assert (tmp_path / repeated.standalone_path).read_text(encoding="utf-8") == (
        "<html>first wrapper</html>"
    )


def test_persist_report_artifact_supports_governed_output_junction(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction regression")

    repo_root = tmp_path / "runtime"
    output_target = tmp_path / "canonical-output"
    repo_root.mkdir()
    output_target.mkdir()
    output_link = repo_root / "output"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(output_link), str(output_target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")

    workspace = output_link / "research" / "NU" / "2026-08-10_workspace.html"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("<html>standalone</html>", encoding="utf-8")

    ref = persist_report_artifact(
        repo_root=repo_root,
        body=_body(),
        standalone_path=workspace,
        generated_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
        coverage_role="portfolio",
        title="NU Full Research Brief",
    )

    assert ref.standalone_path.startswith("output/research/NU/artifacts/")
    assert (repo_root / ref.standalone_path).is_file()
    assert load_report_artifact_index(repo_root).items == (ref,)
