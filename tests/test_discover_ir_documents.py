"""Tests for execution/discover_ir_documents.py (PR3 — single-ticker orchestrator).

discover_history_hybrid is monkeypatched (no browser); these lock URL resolution
(no-ir_url exits 0), the CandidateDoc → ManifestEntry mapping, idempotent manifest
writing, and the JSON status the batch reads (done / no_docs / no_ir_url).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import discover_ir_documents  # noqa: E402
from ir_pipeline.discover._docmeta import CandidateDoc  # noqa: E402
from ir_pipeline.manifest import load_manifest  # noqa: E402


def _patch_hybrid(monkeypatch: pytest.MonkeyPatch, cands: list[CandidateDoc]) -> None:
    def _fake(**_kw: object) -> list[CandidateDoc]:
        return cands

    monkeypatch.setattr(discover_ir_documents, "discover_history_hybrid", _fake)


def test_no_ir_url_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No --url, no config under repo_root, no DB → nothing to crawl.
    rc = discover_ir_documents.main(
        ["--ticker", "ZZ", "--repo-root", str(tmp_path), "--db", str(tmp_path / "missing.db")]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "no_ir_url"
    assert out["added"] == 0


def test_writes_manifest_from_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cands = [
        CandidateDoc(
            "https://x/a.pdf", "Q3 2025 PR", "a.pdf", "press_release", 2025, 3, "https://x/"
        ),
        CandidateDoc("https://x/b.pdf", "deck", "b.pdf", None, None, None, "https://x/"),
    ]
    _patch_hybrid(monkeypatch, cands)
    rc = discover_ir_documents.main(
        [
            "--ticker",
            "NU",
            "--url",
            "https://x/",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(tmp_path / "m.db"),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "done"
    assert out["discovered"] == 2
    assert out["added"] == 2

    by_url = {e.url: e for e in load_manifest(tmp_path, "NU")}
    assert by_url["https://x/a.pdf"].year == 2025
    assert by_url["https://x/a.pdf"].quarter == "Q3"
    assert by_url["https://x/a.pdf"].doc_type == "press_release"
    # Undated / untyped candidate keeps a clean fallback.
    assert by_url["https://x/b.pdf"].quarter is None
    assert by_url["https://x/b.pdf"].doc_type == "document"


def test_rediscovery_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cands = [CandidateDoc("https://x/a.pdf", "", "a.pdf", "press_release", 2025, 3, "https://x/")]
    _patch_hybrid(monkeypatch, cands)
    argv = [
        "--ticker",
        "NU",
        "--url",
        "https://x/",
        "--repo-root",
        str(tmp_path),
        "--db",
        str(tmp_path / "m.db"),
    ]
    discover_ir_documents.main(argv)
    capsys.readouterr()
    discover_ir_documents.main(argv)
    out = json.loads(capsys.readouterr().out)
    assert out["added"] == 0  # already present
    assert out["manifest_total"] == 1


def test_no_docs_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_hybrid(monkeypatch, [])
    rc = discover_ir_documents.main(
        [
            "--ticker",
            "NU",
            "--url",
            "https://x/",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(tmp_path / "m.db"),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "no_docs"
    assert out["discovered"] == 0
