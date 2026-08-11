"""Offline legacy-report extraction into the shared Work OS reader boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup

from report.artifacts import (
    load_report_artifact_index,
    migrate_legacy_report_bodies,
    reconcile_legacy_workspace_reports,
    rollback_legacy_report_bodies,
)
from report.legacy_body import extract_legacy_reader_body


def _legacy_workspace() -> str:
    return """<!doctype html>
<html data-theme="dark"><head><style>.old { color: red; }</style></head><body>
<nav id="document-chrome">Legacy document chrome</nav>
<div class="l1-root" id="report-root" style="color:red" onclick="window.evil()">
  <div class="tab-group-pane" data-tab-group="overview">
    <div class="tab-pane subtab-pane" data-tab="company" aria-labelledby="company-title">
      <h2 id="company-title">Company and moat</h2>
      <p>Complete governed argument.</p>
      <a href="#company-title">Jump to company</a>
      <a href="/source/42" ping="https://tracker.example">Source 42</a>
      <a href="https://example.com/source">External source</a>
      <a href="javascript:window.evil()">Unsafe link</a>
      <img src="//tracker.example/pixel.png" srcset="https://tracker.example/2x.png 2x" alt="Unsafe remote image">
      <svg><use href="https://tracker.example/icon.svg"></use></svg>
      <table><tr><th>Metric</th><td>Value</td></tr></table>
      <button x-data="{}" @click="window.evil()">Legacy action</button>
      <script>window.evil()</script>
      <iframe src="https://example.com/legacy"></iframe>
    </div>
    <div class="tab-pane" data-tab="company"><h3>Company appendix</h3></div>
  </div>
</div>
<script>window.legacyBoot()</script>
</body></html>"""


def _write_legacy_workspace(repo_root: Path, html: str | None = None) -> Path:
    path = repo_root / "output" / "research" / "NU" / "2026-07-01_workspace.html"
    path.parent.mkdir(parents=True)
    path.write_text(html or _legacy_workspace(), encoding="utf-8")
    return path


def test_extractor_preserves_content_and_removes_executable_legacy_markup() -> None:
    extracted = extract_legacy_reader_body(
        _legacy_workspace(), artifact_id="report_NU_2026-07-01_deadbeef"
    )

    assert "Legacy document chrome" not in extracted.body_html
    assert "Complete governed argument" in extracted.body_html
    assert "<table>" in extracted.body_html
    assert "/source/42" in extracted.body_html
    assert "<script" not in extracted.body_html
    assert "<iframe" not in extracted.body_html
    assert "onclick=" not in extracted.body_html
    assert "style=" not in extracted.body_html
    assert "@click=" not in extracted.body_html
    assert "x-data=" not in extracted.body_html
    assert "javascript:" not in extracted.body_html
    assert "//tracker.example" not in extracted.body_html
    assert "srcset=" not in extracted.body_html
    assert " ping=" not in extracted.body_html
    assert 'href="https://example.com/source"' in extracted.body_html
    assert 'class="tab-group-pane active"' in extracted.body_html
    assert 'class="tab-pane subtab-pane active"' in extracted.body_html
    assert 'id="reader-' in extracted.body_html
    assert 'href="#reader-' in extracted.body_html
    assert 'aria-labelledby="reader-' in extracted.body_html
    assert extracted.section_ids == ("company",)
    assert extracted.heading_count == 3
    assert extracted.table_count == 1
    assert extracted.source_link_count == 1
    assert extracted.source_metrics == extracted.preserved_metrics
    assert extracted.body_sha256 == hashlib.sha256(extracted.body_html.encode("utf-8")).hexdigest()
    assert any(warning.startswith("fetch_href_removed") for warning in extracted.warnings)
    assert "inline_style_removed" in extracted.warnings
    parsed = BeautifulSoup(extracted.body_html, "html.parser")
    ids = [str(tag.get("id")) for tag in parsed.find_all(id=True)]
    assert len(ids) == len(set(ids))


def test_migration_dry_run_is_read_only_then_apply_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    workspace = _write_legacy_workspace(tmp_path)
    source_bytes = workspace.read_bytes()
    reconcile_legacy_workspace_reports(tmp_path)
    legacy_ref = load_report_artifact_index(tmp_path).items[0]
    legacy_manifest_path = tmp_path / legacy_ref.manifest_path
    legacy_manifest_bytes = legacy_manifest_path.read_bytes()

    dry_run = migrate_legacy_report_bodies(tmp_path, tickers={"NU"}, apply=False)

    assert dry_run.candidates == 1
    assert dry_run.eligible == 1
    assert dry_run.migrated == 0
    legacy_ref = load_report_artifact_index(tmp_path).items[0]
    assert legacy_ref.reader_mode == "legacy_standalone"
    assert legacy_ref.body_path is None
    assert not list(workspace.parent.glob("artifacts/*/body.html"))
    assert workspace.read_bytes() == source_bytes

    applied = migrate_legacy_report_bodies(tmp_path, tickers={"NU"}, apply=True)

    assert applied.candidates == 1
    assert applied.eligible == 1
    assert applied.migrated == 1
    migrated_ref = load_report_artifact_index(tmp_path).items[0]
    assert migrated_ref.reader_mode == "shared_body"
    assert migrated_ref.body_path is not None
    body_path = tmp_path / migrated_ref.body_path
    receipt_path = body_path.with_name("reader_extraction.v1.json")
    assert body_path.is_file()
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "reader_extraction_receipt.v1"
    assert receipt["body_schema_version"] == "legacy_reader_body.v1"
    assert receipt["parser_version"] == "legacy_workspace_reader.v1"
    assert receipt["source_sha256"] == legacy_ref.workspace_sha256
    assert receipt["body_sha256"] == migrated_ref.body_sha256
    assert receipt["legacy_manifest_path"] == legacy_ref.manifest_path
    assert receipt["source_metrics"] == receipt["preserved_metrics"]
    assert receipt["id_map"]
    assert receipt["id_map"]["company-title"].startswith("reader-")
    assert workspace.read_bytes() == source_bytes
    assert legacy_manifest_path.read_bytes() == legacy_manifest_bytes
    assert migrated_ref.manifest_path.endswith("/reader_manifest.v1.json")

    repeated = migrate_legacy_report_bodies(tmp_path, tickers={"NU"}, apply=True)
    assert repeated.candidates == 0
    assert repeated.migrated == 0
    assert repeated.skipped_shared == 1
    assert workspace.read_bytes() == source_bytes

    rolled_back = rollback_legacy_report_bodies(tmp_path, tickers={"NU"})
    assert rolled_back.rolled_back == 1
    restored_ref = load_report_artifact_index(tmp_path).items[0]
    assert restored_ref == legacy_ref
    assert not body_path.exists()
    assert not receipt_path.exists()
    assert legacy_manifest_path.read_bytes() == legacy_manifest_bytes
    assert workspace.read_bytes() == source_bytes


def test_unknown_legacy_structure_fails_closed_without_index_activation(tmp_path: Path) -> None:
    workspace = _write_legacy_workspace(tmp_path, "<html><body>unknown report</body></html>")
    source_bytes = workspace.read_bytes()
    reconcile_legacy_workspace_reports(tmp_path)

    result = migrate_legacy_report_bodies(tmp_path, apply=True)

    assert result.candidates == 1
    assert result.eligible == 0
    assert result.migrated == 0
    assert result.failed == 1
    ref = load_report_artifact_index(tmp_path).items[0]
    assert ref.reader_mode == "legacy_standalone"
    assert ref.body_path is None
    assert workspace.read_bytes() == source_bytes


def test_batch_failure_does_not_activate_or_overwrite_any_legacy_manifest(
    tmp_path: Path,
) -> None:
    nu_workspace = _write_legacy_workspace(tmp_path)
    wix_workspace = tmp_path / "output" / "research" / "WIX" / "2026-07-01_workspace.html"
    wix_workspace.parent.mkdir(parents=True)
    wix_workspace.write_text("<html><body>unknown report</body></html>", encoding="utf-8")
    reconcile_legacy_workspace_reports(tmp_path)
    original_index = load_report_artifact_index(tmp_path)
    original_manifests = {
        ref.artifact_id: (tmp_path / ref.manifest_path).read_bytes() for ref in original_index.items
    }

    result = migrate_legacy_report_bodies(tmp_path, apply=True)

    assert result.failed == 1
    assert result.migrated == 0
    assert load_report_artifact_index(tmp_path).items == original_index.items
    for ref in original_index.items:
        assert (tmp_path / ref.manifest_path).read_bytes() == original_manifests[ref.artifact_id]
    assert not list(tmp_path.glob("output/research/*/artifacts/*/body.html"))
    assert nu_workspace.is_file()
