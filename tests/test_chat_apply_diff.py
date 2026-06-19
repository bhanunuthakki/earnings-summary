"""Tests for chat_session.apply_chat_diff — proposal-binding + writable-scope +
optimistic-concurrency hardening (2026-06-18 refresh findings llm-apply-1,
llm-directives-write-1, llm-apply-2).

- the applied diff must match an edit the assistant actually proposed in the
  thread (a forged/CSRF diff the model never proposed is refused);
- directives/ must NOT be chat-writable (pipeline-control specs read back into
  later prompts -> injection-persistence vector);
- a proposal whose old_value no longer matches the on-disk value is stale and
  must be refused, not silently clobbered.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from chat_session import ChatTurn, apply_chat_diff, save_thread

REPORT_DATE = date(2026, 6, 1)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _read_key(path: Path, key: str) -> object:
    return json.loads(path.read_text(encoding="utf-8"))[key]


def _propose(repo_root: Path, ticker: str, diff: dict[str, object]) -> None:
    """Persist a thread in which the assistant proposed ``diff`` — the
    precondition apply_chat_diff now requires."""
    thread = [
        ChatTurn(role="user", text="please edit it"),
        ChatTurn(role="assistant", text="here is the proposed edit", proposed_diff=diff),
    ]
    save_thread(repo_root, ticker, REPORT_DATE, thread)


def test_unproposed_diff_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    proposed: dict[str, object] = {
        "target_file": "data/x.json",
        "target_path": "k",
        "old_value": "old",
        "new_value": "legit",
    }
    _propose(tmp_path, "NU", proposed)
    # caller forges a different value than was proposed
    forged: dict[str, object] = {**proposed, "new_value": "EVIL"}
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, forged)
    assert res["applied"] is False
    assert "does not match any edit proposed" in str(res["error"])
    assert _read_key(target, "k") == "old"  # not written


def test_no_thread_refuses(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    diff: dict[str, object] = {
        "target_file": "data/x.json",
        "target_path": "k",
        "old_value": "old",
        "new_value": "new",
    }
    # no thread saved -> nothing was ever proposed
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is False
    assert "does not match any edit proposed" in str(res["error"])


def test_directives_is_not_writable(tmp_path: Path) -> None:
    target = tmp_path / "directives" / "spec.json"
    _write_json(target, {"rule": "old"})
    diff: dict[str, object] = {
        "target_file": "directives/spec.json",
        "target_path": "rule",
        "old_value": "old",
        "new_value": "PWNED",
    }
    _propose(tmp_path, "NU", diff)  # even a genuinely-proposed write to directives/...
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is False  # ...is refused by scope
    assert "outside writable scope" in str(res["error"])
    assert _read_key(target, "rule") == "old"


def test_data_dir_is_writable(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    diff: dict[str, object] = {
        "target_file": "data/x.json",
        "target_path": "k",
        "old_value": "old",
        "new_value": "new",
    }
    _propose(tmp_path, "NU", diff)
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is True
    assert _read_key(target, "k") == "new"


def test_micro_thesis_is_writable(tmp_path: Path) -> None:
    target = tmp_path / "micro_thesis" / "holdings" / "NU.json"
    _write_json(target, {"thesis": "current"})
    diff: dict[str, object] = {
        "target_file": "micro_thesis/holdings/NU.json",
        "target_path": "thesis",
        "old_value": "current",
        "new_value": "rewritten",
    }
    _propose(tmp_path, "NU", diff)
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is True
    assert _read_key(target, "thesis") == "rewritten"


def test_stale_old_value_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "value-changed-on-disk"})
    diff: dict[str, object] = {
        "target_file": "data/x.json",
        "target_path": "k",
        "old_value": "what-the-model-saw",
        "new_value": "new",
    }
    _propose(tmp_path, "NU", diff)
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is False
    assert "changed since the proposal" in str(res["error"])
    assert _read_key(target, "k") == "value-changed-on-disk"  # not clobbered


def test_missing_old_value_still_applies(tmp_path: Path) -> None:
    """Backward-compat: a proposal that omits old_value still applies (and still
    binds to the stored proposal, which also omits it)."""
    target = tmp_path / "data" / "x.json"
    _write_json(target, {"k": "old"})
    diff: dict[str, object] = {
        "target_file": "data/x.json",
        "target_path": "k",
        "new_value": "new",
    }
    _propose(tmp_path, "NU", diff)
    res = apply_chat_diff(tmp_path, "NU", REPORT_DATE, diff)
    assert res["applied"] is True
    assert _read_key(target, "k") == "new"
