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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
    assert capture.should_capture(None) is False


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
    assert row["prompt_version"]
    assert row["prompt_sha256"]


def test_capture_exchange_is_thread_safe_and_process_sharded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(capture.LLM_CAPTURE_PURPOSES_ENV, raising=False)
    monkeypatch.setattr(capture.os, "getpid", lambda: 111)

    def write(index: int) -> None:
        capture.capture_exchange(
            prompt=f"prompt-{index}",
            response=f"response-{index}",
            purpose="bear_case",
            ticker="NU",
            scope="test",
            model="test-model",
            run_id="threaded",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    monkeypatch.setattr(capture.os, "getpid", lambda: 222)
    write(40)

    files = sorted(tmp_path.glob("capture_*.jsonl"))
    assert len(files) == 2
    rows = [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(rows) == 41
    assert {row["prompt"] for row in rows} == {f"prompt-{index}" for index in range(41)}


def test_capture_exchange_partitions_files_by_purpose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(capture.LLM_CAPTURE_PURPOSES_ENV, raising=False)
    monkeypatch.setattr(capture.os, "getpid", lambda: 111)

    for purpose in ("annual_letter", "valuation_basis"):
        capture.capture_exchange(
            prompt=f"prompt-{purpose}",
            response="response",
            purpose=purpose,
            ticker=None,
            scope="test",
            model="test-model",
            run_id=None,
        )

    files = sorted(tmp_path.glob("capture_*.jsonl"))
    assert len(files) == 2
    assert all("_p" in path.stem for path in files)


def test_capture_exchange_prunes_expired_shards_daily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(capture.CAPTURE_RETENTION_DAYS_ENV, "30")
    capture._LAST_PRUNED_DAY.clear()
    old_day = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%d")
    old = tmp_path / f"capture_{old_day}_999.jsonl"
    old.write_text("{}\n", encoding="utf-8")

    capture.capture_exchange(
        prompt="p",
        response="a",
        purpose="bear_case",
        ticker=None,
        scope=None,
        model="m",
        run_id=None,
    )

    assert not old.exists()
    assert list(tmp_path.glob("capture_*.jsonl"))

    future = datetime.now(UTC) + timedelta(days=31)
    newly_expired = tmp_path / f"capture_{datetime.now(UTC).strftime('%Y-%m-%d')}_998.jsonl"
    newly_expired.write_text("{}\n", encoding="utf-8")
    capture._prune_expired(tmp_path, today=future)
    assert not newly_expired.exists()


def test_explicit_retention_sweep_prunes_quiet_archive_only(tmp_path: Path) -> None:
    archive = tmp_path / "quiet_capture_archive"
    archive.mkdir()
    old = archive / "capture_2026-01-01_123.jsonl"
    current = archive / "capture_2026-07-27_123.jsonl"
    unrelated = archive / "notes.jsonl"
    for path in (old, current, unrelated):
        path.write_text("{}\n", encoding="utf-8")

    deleted = capture.prune_capture_archive(
        archive,
        retention_days=90,
        today=datetime(2026, 7, 27),
    )

    assert deleted == 1
    assert not old.exists()
    assert current.exists()
    assert unrelated.exists()


def test_strict_retention_sweep_surfaces_unlink_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = tmp_path / "capture_2026-01-01_123.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    original_unlink = Path.unlink

    def deny(path: Path, *args: object, **kwargs: object) -> None:
        if path == old:
            raise PermissionError("denied")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)

    with pytest.raises(PermissionError):
        capture.prune_capture_archive(
            tmp_path,
            retention_days=90,
            today=datetime(2026, 7, 27),
            strict=True,
        )
    assert old.exists()


def test_default_archive_follows_writer_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured = tmp_path / "private-captures"
    monkeypatch.setenv(capture.LLM_CAPTURE_DIR_ENV, str(configured))
    monkeypatch.delenv(capture.CAPTURE_ARCHIVE_DIR_ENV, raising=False)
    assert capture.default_capture_archive_dir(tmp_path) == configured


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


@pytest.mark.parametrize(
    ("backend", "model"),
    [
        ("codex", "gpt-5.6-terra"),
        ("gemini", "gemini-2.5-flash"),
        ("openrouter", "deepseek/deepseek-chat"),
    ],
)
def test_call_llm_captures_every_successful_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    model: str,
) -> None:
    from llm import cli, codex_backend, gemini_backend, openrouter_backend

    captured: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "capture_exchange", lambda **kwargs: captured.append(kwargs))
    monkeypatch.setattr(cli, "_enforce_budget_pre_call", lambda *_args, **_kwargs: None)
    if backend == "codex":
        monkeypatch.setenv(cli.PRIMARY_SUBSCRIPTION_BACKEND_ENV_VAR, "codex")
        monkeypatch.setattr(codex_backend, "call_codex_llm", lambda *_args, **_kwargs: "answer")
        result = cli.call_llm("prompt", purpose="bear_case")
    elif backend == "gemini":
        monkeypatch.setattr(gemini_backend, "call_gemini", lambda *_args, **_kwargs: "answer")
        result = cli.call_llm("prompt", purpose="bear_case", model=model)
    else:
        monkeypatch.setattr(
            openrouter_backend,
            "call_openrouter",
            lambda *_args, **_kwargs: "answer",
        )
        result = cli.call_llm("prompt", purpose="bear_case", model=model)

    assert result == "answer"
    assert len(captured) == 1
    assert captured[0]["backend"] == backend
    assert captured[0]["model"] == model
    assert captured[0]["purpose"] == "bear_case"


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
