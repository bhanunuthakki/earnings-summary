# pyright: reportPrivateUsage=false
#
# Reaches module-private surface (_load_capture_records, _run_one_backend) — that
# IS the unit under test. Module-scoped directive per the repo's cli.py precedent.
"""Tests for the LLM capture sink (src/llm/capture.py) and the
execution/compare_backends.py --from-capture replay path.

Every test monkeypatches the live backend call or runs purely on temp files —
the suite never spawns a CLI and never spends. Coverage:
  * capture gating: off by default, on when LLM_CAPTURE_DIR set, judge/eval
    denylist, LLM_CAPTURE_PURPOSES allowlist;
  * capture_exchange: writes the JSONL record, no-ops when off, skips denylisted,
    never raises on a bad directory (best-effort telemetry);
  * replay: dedup by prompt_sha256, purpose filter, non-Claude/empty skip, limit,
    and that replay REUSES the captured Claude response while running Gemini only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from llm import capture

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Capture gating


def test_capture_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(capture.LLM_CAPTURE_DIR_ENV, raising=False)
    assert capture.capture_dir() is None
    assert capture.should_capture("bear_case") is False


def test_capture_on_when_dir_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(capture.LLM_CAPTURE_PURPOSES_ENV, raising=False)
    assert capture.should_capture("bear_case") is True


def test_capture_denylist_blocks_judges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    assert capture.should_capture("backend_compare_judge") is False
    assert capture.should_capture("eval_judge") is False


def test_capture_purpose_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(capture.LLM_CAPTURE_PURPOSES_ENV, "bear_case, valuation_basis")
    assert capture.should_capture("bear_case") is True
    assert capture.should_capture("qa_topics") is False
    assert capture.should_capture(None) is False


# ---------------------------------------------------------------------------
# capture_exchange writes


def test_capture_exchange_writes_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(capture.LLM_CAPTURE_PURPOSES_ENV, raising=False)
    capture.capture_exchange(
        prompt="the prompt",
        response="the answer",
        purpose="bear_case",
        ticker="NU",
        scope="report",
        model="claude-sonnet-4-6",
        run_id="r1",
    )
    files = list(tmp_path.glob("capture_*.jsonl"))
    assert len(files) == 1
    rows = [
        json.loads(ln) for ln in files[0].read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(rows) == 1
    row = cast("dict[str, object]", rows[0])
    assert row["prompt"] == "the prompt" and row["response"] == "the answer"
    assert row["purpose"] == "bear_case" and row["ticker"] == "NU"
    assert row["backend"] == "claude" and row["model"] == "claude-sonnet-4-6"
    assert row["prompt_sha256"]


def test_capture_exchange_noop_when_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(capture.LLM_CAPTURE_DIR_ENV, raising=False)
    capture.capture_exchange(
        prompt="p",
        response="a",
        purpose="bear_case",
        ticker=None,
        scope=None,
        model="m",
        run_id=None,
    )
    assert list(tmp_path.glob("*.jsonl")) == []


def test_capture_exchange_skips_denylisted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    capture.capture_exchange(
        prompt="p",
        response="a",
        purpose="backend_compare_judge",
        ticker=None,
        scope=None,
        model="m",
        run_id=None,
    )
    assert list(tmp_path.glob("*.jsonl")) == []


def test_capture_exchange_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Point the capture dir UNDER an existing file so mkdir(parents=True) raises;
    # capture is best-effort telemetry and must swallow it, not break the call.
    blocker = tmp_path / "iamafile"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(blocker / "sub"))
    capture.capture_exchange(
        prompt="p",
        response="a",
        purpose="bear_case",
        ticker=None,
        scope=None,
        model="m",
        run_id=None,
    )  # must not raise


# ---------------------------------------------------------------------------
# compare_backends --from-capture replay


def _capture_line(**overrides: object) -> str:
    base: dict[str, object] = {
        "purpose": "bear_case",
        "ticker": "NU",
        "scope": "report",
        "model": "claude-sonnet-4-6",
        "backend": "claude",
        "run_id": "r1",
        "prompt": "P",
        "response": "claude answer",
        "prompt_sha256": "sha-P",
    }
    base.update(overrides)
    return json.dumps(base)


def _write_capture(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "capture_2026-06-11.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_capture_dedups_and_filters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "execution"))
    import compare_backends

    path = _write_capture(
        tmp_path,
        [
            _capture_line(prompt_sha256="a", purpose="bear_case"),
            _capture_line(prompt_sha256="a", purpose="bear_case"),  # dup sha -> dropped
            _capture_line(prompt_sha256="b", purpose="valuation_basis"),
            _capture_line(prompt_sha256="c", backend="gemini"),  # non-claude -> dropped
            _capture_line(prompt_sha256="d", response="   "),  # empty response -> dropped
        ],
    )
    recs = compare_backends._load_capture_records(path, purpose_filter=None, limit=None)
    assert len(recs) == 2  # a (once) + b
    only_bc = compare_backends._load_capture_records(path, purpose_filter="bear_case", limit=None)
    assert len(only_bc) == 1  # sha a; b is valuation_basis


def test_load_capture_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "execution"))
    import compare_backends

    path = _write_capture(tmp_path, [_capture_line(prompt_sha256=str(i)) for i in range(5)])
    recs = compare_backends._load_capture_records(path, purpose_filter=None, limit=2)
    assert len(recs) == 2


def test_replay_reuses_claude_runs_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "execution"))
    import compare_backends

    def _fake_call(prompt: str, **kw: object) -> str:
        assert kw.get("backend") == "gemini"  # replay runs ONLY the gemini side
        return "gemini answer"

    monkeypatch.setattr(compare_backends, "call_llm", _fake_call)
    rec = cast(
        "dict[str, object]",
        json.loads(_capture_line(prompt="P", response="claude answer", model="claude-opus-4-8")),
    )
    out = compare_backends.replay_capture_record(
        rec, run_id="g1", gemini_model=None, timeout_seconds=None, force_budget_bypass=True
    )
    assert out["source"] == "capture"
    assert out["purpose"] == "bear_case" and out["ticker"] == "NU"
    claude = cast("dict[str, object]", out["claude"])
    gemini = cast("dict[str, object]", out["gemini"])
    assert claude["ok"] is True and claude["response"] == "claude answer"
    assert claude["model"] == "claude-opus-4-8"  # reused captured model, not re-run
    assert gemini["ok"] is True and gemini["response"] == "gemini answer"


def test_replay_records_gemini_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "execution"))
    import compare_backends

    def _boom(prompt: str, **kw: object) -> str:
        raise RuntimeError("gemini down")

    monkeypatch.setattr(compare_backends, "call_llm", _boom)
    rec = cast("dict[str, object]", json.loads(_capture_line()))
    out = compare_backends.replay_capture_record(
        rec, run_id="g1", gemini_model=None, timeout_seconds=None, force_budget_bypass=True
    )
    claude = cast("dict[str, object]", out["claude"])
    gemini = cast("dict[str, object]", out["gemini"])
    assert claude["ok"] is True  # claude side still intact (reused)
    assert gemini["ok"] is False and "gemini down" in str(gemini["error"])
