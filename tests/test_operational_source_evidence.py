"""Operational source receipts cannot certify built-in fixtures as real evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from execution import attribute_source_cost


def test_foreign_normalization_without_sources_is_hold(tmp_path: Path) -> None:
    receipt_path = tmp_path / "normalization.json"
    result = subprocess.run(
        [
            sys.executable,
            "execution/normalize_foreign_filings.py",
            "--output-receipt",
            str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "HOLD"
    assert receipt["receipts"] == []
    assert receipt["total_tickers_evaluated"] == 0


def test_cost_attribution_without_measured_events_is_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(attribute_source_cost, "TMP_DIR", tmp_path)
    result = attribute_source_cost.run_cost_attribution(117, 7763096)
    assert result["status"] == "HOLD"
    assert result["summary"] is None
    assert result["reason_codes"] == ["measured_source_cost_evidence_unavailable"]


@pytest.mark.parametrize("manifest", [{}, {"files": []}])
def test_empty_manifest_does_not_verify_a_corpus(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    passed, errors, count, size = attribute_source_cost.verify_canary_corpus(path, tmp_path)
    assert passed is False
    assert errors
    assert count == size == 0


def test_check_only_does_not_generate_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"sealed corpus bytes"
    (tmp_path / "WIX.json").write_bytes(content)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"files": [{"filename": "WIX.json", "sha256": hashlib.sha256(content).hexdigest()}]}
        )
    )
    monkeypatch.setattr(attribute_source_cost, "TMP_DIR", tmp_path / "outputs")
    assert (
        attribute_source_cost.main(
            ["--manifest", str(manifest), "--fmp-dir", str(tmp_path), "--check-only"]
        )
        == 0
    )
    assert not (tmp_path / "outputs").exists()
