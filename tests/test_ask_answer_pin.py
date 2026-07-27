"""close_the_loops L3 (PR2): the ``ask_answer`` model pin + the wiring that
pulls the streamed conversational answer into the model-downgrade /
Gemini-promotion loop, plus its budget migration (0104).

The conftest autouse fixture (``_no_real_chat_llm_transport``) rebinds
``chat_session.stream_llm_text`` to a blocker so stray tests can't spend. This
module captures the REAL function at import time (before the fixture runs) via
``from chat_session import stream_llm_text``, then drives it with a fake
subprocess / fake call_llm — no real spend, but the genuine resolution + budget
+ ledger logic runs.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from collections.abc import Generator, Iterator
from pathlib import Path
from types import GeneratorType
from typing import cast

import pytest
from alembic.config import Config

import chat_session
import llm.cli as llm_cli
import llm_call_ledger
from alembic import command
from chat_session import stream_llm_text as real_stream_llm_text
from llm.cli import DEFAULT_MODEL, LLM_MODELS, LLMBudgetExceeded
from llm_call_ledger import LlmCallRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# the pin
# ---------------------------------------------------------------------------


def test_ask_answer_pinned_to_default_model() -> None:
    # The conversational answer enters the registry like every other purpose,
    # defaulting to the Sonnet chat tier (eval-gated before any downgrade).
    assert LLM_MODELS.get("ask_answer") == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# stream_llm_text wiring — fake transport, no spend
# ---------------------------------------------------------------------------


class _FakeStdin:
    def write(self, _data: str) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self, lines: list[str], stderr: str = "") -> None:
        self.stdin: _FakeStdin = _FakeStdin()
        self.stdout: Iterator[str] = iter(lines)
        self.stderr: io.StringIO = io.StringIO(stderr)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _result_line(cost: float = 0.012, in_tok: int = 1000, out_tok: int = 200) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ignored",
            "total_cost_usd": cost,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
        }
    )


def _patch_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    def fake_model_for(_purpose: str) -> str:
        return model

    monkeypatch.setattr(llm_cli, "_model_for", fake_model_for)


def _patch_budget_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_enforce(_purpose: str | None, *, force_budget_bypass: bool = False) -> None:
        return None

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", fake_enforce)


def _fake_which(_name: str) -> str:
    return r"/fake/claude"


def _patch_ledger(monkeypatch: pytest.MonkeyPatch) -> list[LlmCallRecord]:
    records: list[LlmCallRecord] = []

    def fake_record(record: LlmCallRecord, *, db_path: object = None) -> int:
        records.append(record)
        return 1

    monkeypatch.setattr(llm_call_ledger, "record_call", fake_record)
    return records


def test_stream_pins_resolved_model_and_records_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, DEFAULT_MODEL)
    _patch_budget_ok(monkeypatch)
    monkeypatch.setattr(chat_session.shutil, "which", _fake_which)

    captured: dict[str, list[str]] = {}

    def fake_popen(cmd: list[str], **_kwargs: object) -> _FakeProc:
        captured["cmd"] = cmd
        return _FakeProc([_assistant_line("Hello "), _assistant_line("world"), _result_line()])

    monkeypatch.setattr(chat_session.subprocess, "Popen", fake_popen)
    records = _patch_ledger(monkeypatch)

    events = list(real_stream_llm_text("PROMPT"))

    # --model pins the resolved downgrade-loop choice (no longer the bare CLI default).
    cmd = captured["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == DEFAULT_MODEL
    # Streaming protocol is unchanged: deltas then exactly one final.
    assert [e["type"] for e in events] == ["delta", "delta", "final"]
    assert events[-1]["text"] == "Hello world"
    # A ledger row is written with purpose/model/scope + cost lifted from the
    # CLI's final `result` envelope — so it shows in Call Health.
    assert len(records) == 1
    rec = records[0]
    assert rec.purpose == "ask_answer"
    assert rec.model == DEFAULT_MODEL
    assert rec.scope == "ask"
    assert rec.cost_estimate_usd == pytest.approx(0.012)
    assert rec.output_tokens == 200
    assert rec.error is None


def test_stream_buffers_when_promoted_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Gemini-promoted pin can't stream through `claude -p`; the transport must
    # degrade to one buffered call_llm answer and never touch the CLI.
    _patch_model(monkeypatch, "gemini-2.5-flash")
    _patch_budget_ok(monkeypatch)

    def fake_call_llm(_prompt: str, **_kwargs: object) -> str:
        return "buffered gemini answer"

    monkeypatch.setattr(llm_cli, "call_llm", fake_call_llm)

    def boom_which(_name: str) -> str:
        raise AssertionError("streaming CLI must not be reached on the Gemini path")

    monkeypatch.setattr(chat_session.shutil, "which", boom_which)

    events = list(real_stream_llm_text("PROMPT"))
    assert [e["type"] for e in events] == ["delta", "final"]
    assert events[-1]["text"] == "buffered gemini answer"


def test_stream_hard_budget_block_yields_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, DEFAULT_MODEL)

    def raise_budget(_purpose: str | None, *, force_budget_bypass: bool = False) -> None:
        raise LLMBudgetExceeded("ask_answer over cap")

    monkeypatch.setattr(llm_cli, "_enforce_budget_pre_call", raise_budget)

    def boom_which(_name: str) -> str:
        raise AssertionError("must not stream after a hard budget block")

    monkeypatch.setattr(chat_session.shutil, "which", boom_which)

    events = list(real_stream_llm_text("PROMPT"))
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "budget" in str(events[0]["error"]).lower()


def test_stream_empty_response_records_error_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, DEFAULT_MODEL)
    _patch_budget_ok(monkeypatch)
    monkeypatch.setattr(chat_session.shutil, "which", _fake_which)

    def fake_popen(_cmd: list[str], **_kwargs: object) -> _FakeProc:
        return _FakeProc([], stderr="model unavailable")

    monkeypatch.setattr(chat_session.subprocess, "Popen", fake_popen)
    records = _patch_ledger(monkeypatch)

    events = list(real_stream_llm_text("PROMPT"))
    assert [e["type"] for e in events] == ["error"]
    assert "model unavailable" not in str(events)
    # The failed call still lands a ledger row (with the error) for Call Health.
    assert len(records) == 1
    assert records[0].error is not None


class _BlockingStdout:
    def __init__(self, released: threading.Event) -> None:
        self._released = released

    def __iter__(self) -> _BlockingStdout:
        return self

    def __next__(self) -> str:
        self._released.wait(timeout=2)
        raise StopIteration


class _BlockingProc(_FakeProc):
    def __init__(self, stderr: str) -> None:
        super().__init__([], stderr=stderr)
        self.released = threading.Event()
        self.stdout = _BlockingStdout(self.released)

    def terminate(self) -> None:
        super().terminate()
        self.released.set()

    def kill(self) -> None:
        super().kill()
        self.released.set()


def test_stream_watchdog_terminates_and_records_generic_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_model(monkeypatch, DEFAULT_MODEL)
    _patch_budget_ok(monkeypatch)
    monkeypatch.setattr(chat_session.shutil, "which", _fake_which)
    monkeypatch.setattr(chat_session, "_STREAM_TIMEOUT_SECONDS", 0.01)
    proc = _BlockingProc(stderr="failed?api_key=secret-value")

    def fake_popen(_cmd: list[str], **_kwargs: object) -> _BlockingProc:
        return proc

    monkeypatch.setattr(chat_session.subprocess, "Popen", fake_popen)
    records = _patch_ledger(monkeypatch)

    events = list(real_stream_llm_text("PROMPT"))

    assert proc.terminated is True
    assert [event["type"] for event in events] == ["error"]
    assert "secret-value" not in str(events)
    assert len(records) == 1
    assert records[0].error is not None
    assert "timeout" in records[0].error.lower()


def test_stream_generator_close_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_model(monkeypatch, DEFAULT_MODEL)
    _patch_budget_ok(monkeypatch)
    monkeypatch.setattr(chat_session.shutil, "which", _fake_which)
    proc = _FakeProc([_assistant_line("partial")])

    def fake_popen(_cmd: list[str], **_kwargs: object) -> _FakeProc:
        return proc

    monkeypatch.setattr(chat_session.subprocess, "Popen", fake_popen)

    stream = real_stream_llm_text("PROMPT")
    assert isinstance(stream, GeneratorType)
    stream_generator = cast("Generator[dict[str, object], None, None]", stream)
    assert next(stream_generator)["type"] == "delta"
    stream_generator.close()

    assert proc.terminated is True


# ---------------------------------------------------------------------------
# budget migration 0104
# ---------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_0104_seeds_ask_answer_budget_warn_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE llm_budgets (
            purpose TEXT PRIMARY KEY, monthly_cap_usd REAL, warn_threshold_pct REAL,
            hard_block INTEGER, on_exceed TEXT, created_at TEXT, updated_at TEXT, notes TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    cfg = _alembic_cfg(db_path)
    command.stamp(cfg, "0103_podcast_takeaway_summary_budget")
    command.upgrade(cfg, "0104_ask_answer_budget")

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT monthly_cap_usd, hard_block, on_exceed FROM llm_budgets WHERE purpose = 'ask_answer'"
    ).fetchone()
    conn.close()
    assert row is not None
    # Soft/warn: an interactive answer is never hard-blocked mid-conversation.
    assert row == (25.0, 0, "warn")

    # Downgrade removes only this row.
    command.downgrade(cfg, "0103_podcast_takeaway_summary_budget")
    conn = sqlite3.connect(db_path)
    gone = conn.execute("SELECT COUNT(*) FROM llm_budgets WHERE purpose = 'ask_answer'").fetchone()[
        0
    ]
    conn.close()
    assert gone == 0
