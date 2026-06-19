"""Tests for chat_session.apply_chat_diff — writable-scope + optimistic-concurrency
hardening from the 2026-06-18 refresh (findings llm-directives-write-1, llm-apply-2).

- directives/ must NOT be chat-writable (pipeline-control specs read back into
  later prompts -> injection-persistence vector).
- a proposal whose old_value no longer matches the on-disk value is stale and
  must be refused, not silently clobbered.
"""

from __future__ import annotations

import json
from pathlib import Path

from chat_session import apply_chat_diff


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _read_key(path: Path, key: str) -> object:
    return json.loads(path.read_text(encoding="utf-8"))[key]


def test_directives_is_not_writable(tmp_path: Path) -> None:
    target = tmp_path / "directives" / "spec.json"
    _write_json(target, {"rule": "old"})
    res = apply_chat_diff(
        tmp_path,
        "NU",
        {
            "target_file": "directives/spec.json",
            "target_path": "rule",
            "old_value": "old",
            "new_value": "PWNED",
        },
    )
    assert res["applied"] is False
    assert "outside writable scope" in str(res["error"])
    assert _read_key(target, "rule") == "old"  # untouched on disk


def test_data_dir_is_writable(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    res = apply_chat_diff(
        tmp_path,
        "NU",
        {"target_file": "data/x.json", "target_path": "k", "old_value": "old", "new_value": "new"},
    )
    assert res["applied"] is True
    assert _read_key(target, "k") == "new"


def test_micro_thesis_is_writable(tmp_path: Path) -> None:
    target = tmp_path / "micro_thesis" / "holdings" / "NU.json"
    _write_json(target, {"thesis": "current"})
    res = apply_chat_diff(
        tmp_path,
        "NU",
        {
            "target_file": "micro_thesis/holdings/NU.json",
            "target_path": "thesis",
            "old_value": "current",
            "new_value": "rewritten",
        },
    )
    assert res["applied"] is True
    assert _read_key(target, "thesis") == "rewritten"


def test_stale_old_value_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "value-changed-on-disk"})
    res = apply_chat_diff(
        tmp_path,
        "NU",
        {
            "target_file": "data/x.json",
            "target_path": "k",
            "old_value": "what-the-model-saw",
            "new_value": "new",
        },
    )
    assert res["applied"] is False
    assert "changed since the proposal" in str(res["error"])
    assert _read_key(target, "k") == "value-changed-on-disk"  # not clobbered


def test_missing_old_value_still_applies(tmp_path: Path) -> None:
    """Backward-compat: a proposal that omits old_value falls back to apply-anyway."""
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    res = apply_chat_diff(
        tmp_path,
        "NU",
        {"target_file": "data/x.json", "target_path": "k", "new_value": "new"},
    )
    assert res["applied"] is True
    assert _read_key(target, "k") == "new"
