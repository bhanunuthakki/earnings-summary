# pyright: reportPrivateUsage=false
"""Tests for execution/discover_ir_documents.py (PR3 — single-ticker orchestrator).

discover_history_hybrid is monkeypatched (no browser); these lock URL resolution
(no-ir_url exits 0), the CandidateDoc → ManifestEntry mapping, idempotent manifest
writing, and the JSON status the batch reads (done / no_docs / no_ir_url).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution import discover_ir_documents  # noqa: E402
from ir_pipeline.discover import IrDiscoveryAuthenticationDeniedError  # noqa: E402
from ir_pipeline.discover._docmeta import CandidateDoc  # noqa: E402
from ir_pipeline.manifest import load_manifest  # noqa: E402
from pipeline.source_policy import SOURCE_POLICY_CONFIG  # noqa: E402


def _patch_hybrid(monkeypatch: pytest.MonkeyPatch, cands: list[CandidateDoc]) -> None:
    def _fake(**_kw: object) -> list[CandidateDoc]:
        return cands

    monkeypatch.setattr(discover_ir_documents, "discover_history_hybrid", _fake)


def _make_tracked_db(db: Path, ticker: str, role: str, *, ir_url: str | None = None) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT, ir_url TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES (?, ?, NULL, ?)",
            (ticker, role, ir_url),
        )


def test_no_ir_url_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "portfolio.db"
    _make_tracked_db(db, "ZZ", "portfolio")
    # No --url, no config under repo_root, no DB → nothing to crawl.
    rc = discover_ir_documents.main(
        ["--ticker", "ZZ", "--repo-root", str(tmp_path), "--db", str(db)]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "no_ir_url"
    assert out["added"] == 0


def test_writes_manifest_from_discovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "m.db"
    _make_tracked_db(db, "NU", "evaluation")
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
            str(db),
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
    db = tmp_path / "m.db"
    _make_tracked_db(db, "NU", "evaluation")
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
        str(db),
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
    db = tmp_path / "m.db"
    _make_tracked_db(db, "NU", "evaluation")
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
            str(db),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "no_docs"
    assert out["discovered"] == 0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_denial_is_not_reported_as_no_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    db = tmp_path / "m.db"
    _make_tracked_db(db, "NU", "portfolio")
    def deny_auth(**_kwargs: object) -> list[object]:
        raise IrDiscoveryAuthenticationDeniedError(status)

    monkeypatch.setattr(discover_ir_documents, "discover_history_hybrid", deny_auth)

    rc = discover_ir_documents.main(
        [
            "--ticker",
            "NU",
            "--url",
            "https://issuer.example/ir",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(db),
            "--automatic",
        ]
    )

    assert rc == 10


@pytest.mark.parametrize("role", ["watchlist", "index_member"])
def test_direct_url_cannot_bypass_stored_collection_role(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    db = tmp_path / "portfolio.db"
    _make_tracked_db(db, "DENY", role)

    def _must_not_crawl(**_kwargs: object) -> list[CandidateDoc]:
        raise AssertionError("network crawl crossed a denied policy boundary")

    monkeypatch.setattr(discover_ir_documents, "discover_history_hybrid", _must_not_crawl)
    rc = discover_ir_documents.main(
        [
            "--ticker",
            "DENY",
            "--url",
            "https://issuer.example/investors",
            "--repo-root",
            str(tmp_path),
            "--db",
            str(db),
        ]
    )

    assert rc == 2
    assert "source_collection_policy_denied" in capsys.readouterr().err


def test_direct_discovery_uses_the_canonical_reported_quarter_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "portfolio.db"
    _make_tracked_db(db, "PORT", "portfolio")
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> list[CandidateDoc]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(discover_ir_documents, "discover_history_hybrid", _capture)
    assert (
        discover_ir_documents.main(
            [
                "--ticker",
                "PORT",
                "--url",
                "https://issuer.example/investors",
                "--db",
                str(db),
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert captured["max_quarters"] == SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters

    with pytest.raises(SystemExit):
        discover_ir_documents._parse_args(
            [
                "--ticker",
                "PORT",
                "--max-quarters",
                str(SOURCE_POLICY_CONFIG.reported_quarter_window.max_quarters + 1),
            ]
        )
