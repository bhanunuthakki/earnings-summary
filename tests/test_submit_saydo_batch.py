"""Tests for execution/submit_saydo_batch.py — the SayDo batch submission +
polling + result-writing flow.

We never call the real Anthropic API. Instead a `_FakeClient` impersonates
the SDK's batches surface (`create`/`retrieve`/`results`), driven by a
canned state machine the test sets up explicitly. That keeps the tests
hermetic and pins the contract this script depends on — any future SDK
shape change has to land in the fake first.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from execution.submit_saydo_batch import (  # noqa: E402
    _estimate_input_cost_usd,
    _extract_assistant_text,
    _filter_existing,
    _find_latest_jsonl,
    _load_jsonl,
    _request_counts_dict,
    _ticker_from_custom_id,
    submit_and_collect,
)
from llm.batch import BatchRequest, write_jsonl  # noqa: E402
from llm.cli import LLMSetupError  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_message(text: str, *, input_tokens: int = 1000, output_tokens: int = 500) -> Any:
    """Build a duck-typed Message that matches the SDK shape the script reads.

    Only `content[*].type/text`, `usage.{input,output,cache_*}`, and `model`
    are accessed — keep the fake minimal and explicit."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


def _succeeded(text: str, *, input_tokens: int = 1000, output_tokens: int = 500) -> Any:
    return SimpleNamespace(
        type="succeeded",
        message=_fake_message(text, input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _errored(message: str = "rate_limit_error") -> Any:
    err = SimpleNamespace(
        type="rate_limit_error",
        message=message,
        model_dump=lambda: {"type": "rate_limit_error", "message": message},
    )
    return SimpleNamespace(type="errored", error=err)


def _expired() -> Any:
    return SimpleNamespace(type="expired")


def _individual(custom_id: str, result: Any) -> Any:
    return SimpleNamespace(custom_id=custom_id, result=result)


class _FakeBatchesAPI:
    """In-memory stand-in for `Anthropic().messages.batches`. The test seeds
    `poll_sequence` (the processing_status trajectory) + `results_by_id` (the
    streamed responses) explicitly so we control every branch."""

    def __init__(
        self,
        *,
        poll_sequence: list[tuple[str, dict[str, int]]],
        results_by_id: dict[str, list[Any]],
        batch_id: str = "batch_test_001",
    ) -> None:
        self.poll_sequence = list(poll_sequence)
        self.results_by_id = results_by_id
        self.batch_id = batch_id
        self.created: list[list[dict[str, object]]] = []
        self.retrieve_calls = 0

    def create(self, *, requests: list[dict[str, object]]) -> Any:
        self.created.append(requests)
        # Seed initial state for retrieve() to consume.
        return SimpleNamespace(
            id=self.batch_id,
            processing_status="in_progress",
            request_counts=SimpleNamespace(
                processing=len(requests), succeeded=0, errored=0, canceled=0, expired=0
            ),
        )

    def retrieve(self, batch_id: str) -> Any:
        self.retrieve_calls += 1
        assert batch_id == self.batch_id
        if not self.poll_sequence:
            # Default to ended if test forgot to seed an extra tick.
            status, counts = (
                "ended",
                {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
            )
        else:
            status, counts = self.poll_sequence.pop(0)
        return SimpleNamespace(
            id=batch_id,
            processing_status=status,
            request_counts=SimpleNamespace(**counts),
        )

    def results(self, batch_id: str) -> Any:
        assert batch_id == self.batch_id
        return iter(self.results_by_id.get(batch_id, []))


class _FakeMessagesAPI:
    def __init__(self, batches: _FakeBatchesAPI) -> None:
        self.batches = batches


class _FakeClient:
    def __init__(self, batches: _FakeBatchesAPI) -> None:
        self.messages = _FakeMessagesAPI(batches)


def _make_jsonl(tmp_path: Path, custom_ids: list[str]) -> Path:
    """Write a real JSONL file via the canonical helper so the test exercises
    the same shape build_saydo_pairs.py emits."""
    batch_dir = tmp_path / ".tmp" / "saydo_batch"
    requests = [BatchRequest(custom_id=cid, prompt=f"prompt body for {cid}") for cid in custom_ids]
    out = batch_dir / "saydo_batch_20260526T000000Z.jsonl"
    write_jsonl(requests, out)
    return out


# ---------------------------------------------------------------------------
# Unit helpers — pure functions
# ---------------------------------------------------------------------------


def test_estimate_input_cost_matches_user_formula() -> None:
    """User-spec: 7K tokens/pair × 50% × $3/Mtok = $0.0105/pair."""
    cost = _estimate_input_cost_usd(1)
    assert cost == Decimal("0.0105")
    # And it scales linearly.
    assert _estimate_input_cost_usd(32) == Decimal("0.3360")


def test_ticker_extracted_from_custom_id() -> None:
    assert _ticker_from_custom_id("SayDo_META_Q3_2025_Q4_2025") == "META"
    assert _ticker_from_custom_id("SayDo_BRK.B_Q1_2024_Q2_2024") == "BRK.B"
    assert _ticker_from_custom_id("not_a_saydo_id") is None


def test_extract_assistant_text_concatenates_text_blocks() -> None:
    msg = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Hello "),
            SimpleNamespace(type="thinking", text="<thinking>ignored</thinking>"),
            SimpleNamespace(type="text", text="world"),
        ]
    )
    assert _extract_assistant_text(msg) == "Hello world"


def test_request_counts_dict_handles_sdk_model_and_plain_dict() -> None:
    sdk_shape = SimpleNamespace(
        request_counts=SimpleNamespace(processing=1, succeeded=2, errored=3, canceled=4, expired=5)
    )
    assert _request_counts_dict(sdk_shape) == {
        "processing": 1,
        "succeeded": 2,
        "errored": 3,
        "canceled": 4,
        "expired": 5,
    }
    dict_shape = SimpleNamespace(request_counts={"succeeded": 9})
    assert _request_counts_dict(dict_shape) == {"succeeded": 9}


# ---------------------------------------------------------------------------
# JSONL loading + idempotent filter
# ---------------------------------------------------------------------------


def test_load_jsonl_round_trips_canonical_payload(tmp_path: Path) -> None:
    path = _make_jsonl(tmp_path, ["SayDo_X_Q1_2025_Q2_2025"])
    rows = _load_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["custom_id"] == "SayDo_X_Q1_2025_Q2_2025"
    params = rows[0]["params"]
    assert isinstance(params, dict)
    assert "messages" in params


def test_load_jsonl_rejects_malformed_line(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"custom_id":"OK","params":{}}\n{not json}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        _load_jsonl(bad)


def test_load_jsonl_rejects_missing_required_keys(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"custom_id":"OK"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required keys"):
        _load_jsonl(bad)


def test_filter_existing_skips_already_written(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").write_text("prior", encoding="utf-8")
    rows: list[dict[str, object]] = [
        {"custom_id": "SayDo_A_Q1_2025_Q2_2025", "params": {}},
        {"custom_id": "SayDo_B_Q1_2025_Q2_2025", "params": {}},
    ]
    out = _filter_existing(rows, tmp, force_overwrite=False)
    assert [r["custom_id"] for r in out] == ["SayDo_B_Q1_2025_Q2_2025"]


def test_filter_existing_force_overwrite_keeps_all(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").write_text("prior", encoding="utf-8")
    rows: list[dict[str, object]] = [{"custom_id": "SayDo_A_Q1_2025_Q2_2025", "params": {}}]
    out = _filter_existing(rows, tmp, force_overwrite=True)
    assert len(out) == 1


def test_find_latest_jsonl_picks_lexicographic_max(tmp_path: Path) -> None:
    batch_dir = tmp_path / ".tmp" / "saydo_batch"
    batch_dir.mkdir(parents=True)
    older = batch_dir / "saydo_batch_20260101T000000Z.jsonl"
    newer = batch_dir / "saydo_batch_20260301T000000Z.jsonl"
    older.write_text("", encoding="utf-8")
    newer.write_text("", encoding="utf-8")
    assert _find_latest_jsonl(tmp_path) == newer


def test_find_latest_jsonl_returns_none_when_dir_missing(tmp_path: Path) -> None:
    assert _find_latest_jsonl(tmp_path) is None


# ---------------------------------------------------------------------------
# Polling state machine
# ---------------------------------------------------------------------------


def test_poll_loop_progresses_through_states(tmp_path: Path) -> None:
    """Submit, then retrieve returns in_progress twice before ended — script
    must sleep twice (one short, one longer) and end on ended."""
    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])
    batches = _FakeBatchesAPI(
        poll_sequence=[
            (
                "in_progress",
                {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
            ),
            (
                "in_progress",
                {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
            ),
            ("ended", {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0}),
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("verdict body A")),
            ],
        },
    )
    sleeps: list[float] = []

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=sleeps.append,
        poll_backoff=(1, 2, 4, 8),  # tiny so the test never wall-clock-waits
    )

    assert summary["status"] == "ended"
    assert summary["succeeded"] == 1
    assert summary["errored"] == 0
    assert batches.retrieve_calls == 3
    # Two sleeps (one between retrieve 1→2, one between 2→3) with backoff progressing.
    assert sleeps == [1.0, 2.0]
    # Output file landed.
    out_file = tmp_path / ".tmp" / "SayDo_A_Q1_2025_Q2_2025.txt"
    assert out_file.exists()
    assert out_file.read_text(encoding="utf-8") == "verdict body A"


# ---------------------------------------------------------------------------
# Result writing — succeeded + errored + idempotent skip
# ---------------------------------------------------------------------------


def test_succeeded_results_written_to_tmp(tmp_path: Path) -> None:
    jsonl = _make_jsonl(
        tmp_path,
        [
            "SayDo_A_Q1_2025_Q2_2025",
            "SayDo_B_Q1_2025_Q2_2025",
        ],
    )
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 2, "errored": 0, "canceled": 0, "expired": 0})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("text A")),
                _individual("SayDo_B_Q1_2025_Q2_2025", _succeeded("text B")),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["succeeded"] == 2
    assert (tmp_path / ".tmp" / "SayDo_A_Q1_2025_Q2_2025.txt").read_text(
        encoding="utf-8"
    ) == "text A"
    assert (tmp_path / ".tmp" / "SayDo_B_Q1_2025_Q2_2025.txt").read_text(
        encoding="utf-8"
    ) == "text B"


def test_idempotent_skip_when_target_already_exists(tmp_path: Path) -> None:
    """If `.tmp/<custom_id>.txt` already exists, the request is filtered
    out *before* submission — so no API call should happen for it."""
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").write_text("prior synchronous write", encoding="utf-8")

    jsonl = _make_jsonl(
        tmp_path,
        [
            "SayDo_A_Q1_2025_Q2_2025",  # already on disk
            "SayDo_B_Q1_2025_Q2_2025",  # needs to be submitted
        ],
    )
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_B_Q1_2025_Q2_2025", _succeeded("text B")),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["requests_skipped_existing"] == 1
    assert summary["submitted"] == 1
    # Only the B request reached the API.
    assert len(batches.created) == 1
    submitted_ids = [r["custom_id"] for r in batches.created[0]]
    assert submitted_ids == ["SayDo_B_Q1_2025_Q2_2025"]
    # Prior A content is untouched.
    assert (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").read_text(
        encoding="utf-8"
    ) == "prior synchronous write"


def test_force_overwrite_resubmits_existing_targets(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").write_text("old", encoding="utf-8")

    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("fresh A")),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=True,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["submitted"] == 1
    assert (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").read_text(encoding="utf-8") == "fresh A"


def test_errored_results_landed_in_errors_json(tmp_path: Path) -> None:
    jsonl = _make_jsonl(
        tmp_path,
        [
            "SayDo_A_Q1_2025_Q2_2025",
            "SayDo_B_Q1_2025_Q2_2025",
            "SayDo_C_Q1_2025_Q2_2025",
        ],
    )
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0, "expired": 1})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("ok")),
                _individual("SayDo_B_Q1_2025_Q2_2025", _errored("overloaded")),
                _individual("SayDo_C_Q1_2025_Q2_2025", _expired()),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "ended_with_errors"
    assert summary["succeeded"] == 1
    assert summary["errored"] == 1
    assert summary["expired"] == 1

    errors_path = tmp_path / ".tmp" / "saydo_batch" / "errors_batch_test_001.json"
    assert errors_path.exists()
    errors = json.loads(errors_path.read_text(encoding="utf-8"))
    cids = sorted(e["custom_id"] for e in errors)
    assert cids == ["SayDo_B_Q1_2025_Q2_2025", "SayDo_C_Q1_2025_Q2_2025"]
    # Errored row carries the raw error payload (pydantic .model_dump()).
    b_row = next(e for e in errors if e["custom_id"] == "SayDo_B_Q1_2025_Q2_2025")
    assert b_row["type"] == "errored"
    assert b_row["error"]["message"] == "overloaded"


# ---------------------------------------------------------------------------
# Dry-run + cost gate
# ---------------------------------------------------------------------------


def test_dry_run_does_not_submit(tmp_path: Path) -> None:
    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])
    batches = _FakeBatchesAPI(poll_sequence=[], results_by_id={})

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=True,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "dry_run_ok"
    assert summary["requests_pending"] == 1
    # No submission happened.
    assert batches.created == []
    assert batches.retrieve_calls == 0
    # Estimated cost reported.
    assert summary["estimated_cost_usd"] == "0.0105"


def test_default_path_routes_each_request_through_governed_subscription_entrypoint(
    tmp_path: Path,
) -> None:
    jsonl = _make_jsonl(
        tmp_path,
        ["SayDo_A_Q1_2025_Q2_2025", "SayDo_B_Q1_2025_Q2_2025"],
    )
    calls: list[dict[str, object]] = []

    def fake_llm(prompt: str, **kwargs: object) -> str:
        calls.append({"prompt": prompt, **kwargs})
        return f"answer {len(calls)}"

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        llm_call=fake_llm,
    )

    assert summary["status"] == "ended"
    assert summary["transport"] == "subscription_cli"
    assert summary["succeeded"] == 2
    assert [call["purpose"] for call in calls] == ["pairwise_analysis", "pairwise_analysis"]
    assert [call["ticker"] for call in calls] == ["A", "B"]
    assert all(str(call["scope"]).startswith("saydo:SayDo_") for call in calls)
    assert (tmp_path / ".tmp" / "SayDo_A_Q1_2025_Q2_2025.txt").read_text(
        encoding="utf-8"
    ) == "answer 1"


def test_default_path_propagates_hard_stops(tmp_path: Path) -> None:
    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])

    def fail_hard(_prompt: str, **_kwargs: object) -> str:
        raise LLMSetupError("provider is not configured")

    with pytest.raises(LLMSetupError, match="not configured"):
        submit_and_collect(
            jsonl,
            repo_root=tmp_path,
            llm_call=fail_hard,
        )


def _init_budget_db(db_path: Path, *, purpose: str, cap_usd: float, current_spend: float) -> None:
    """Mirror migrations 0034 + 0052 inline so we don't have to run alembic
    in the test. Same pattern as test_llm_budget.py."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                called_at DATETIME NOT NULL,
                purpose VARCHAR(64),
                ticker VARCHAR(16),
                scope VARCHAR(64),
                model VARCHAR(64) NOT NULL,
                prompt_sha256 VARCHAR(64) NOT NULL,
                response_sha256 VARCHAR(64),
                prompt_chars INTEGER NOT NULL,
                response_chars INTEGER,
                input_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                output_tokens INTEGER,
                elapsed_ms INTEGER NOT NULL,
                cost_estimate_usd FLOAT,
                cache_hit BOOLEAN NOT NULL DEFAULT 0,
                fallback_used VARCHAR(16),
                artifact_id INTEGER,
                error TEXT,
                run_id VARCHAR(64)
            );
            CREATE TABLE llm_budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose VARCHAR(64) NOT NULL,
                monthly_cap_usd NUMERIC(10,2) NOT NULL,
                warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
                hard_block BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                notes TEXT,
                CONSTRAINT uq_llm_budgets_purpose UNIQUE (purpose)
            );
            CREATE TABLE llm_budget_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose VARCHAR(64) NOT NULL,
                month VARCHAR(7) NOT NULL,
                threshold_pct FLOAT NOT NULL,
                alerted_at DATETIME NOT NULL,
                spend_at_alert_usd NUMERIC(10,4) NOT NULL,
                CONSTRAINT uq_alerts UNIQUE (purpose, month, threshold_pct)
            );
            """
        )
        now_iso = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, "
            "hard_block, created_at, updated_at) VALUES (?, ?, 0.8, 1, ?, ?)",
            (purpose, cap_usd, now_iso, now_iso),
        )
        if current_spend > 0:
            conn.execute(
                "INSERT INTO llm_calls (called_at, purpose, model, prompt_sha256, "
                "prompt_chars, elapsed_ms, cost_estimate_usd) "
                "VALUES (?, ?, 'claude-sonnet-4-6', 'x', 100, 1000, ?)",
                (now_iso, purpose, current_spend),
            )
        conn.commit()
    finally:
        conn.close()


def test_cost_gate_halts_when_projected_total_exceeds_cap(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    # cap=$1, current spend=$0.999 -> any submission tips over.
    _init_budget_db(db_path, purpose="pairwise_analysis", cap_usd=1.00, current_spend=0.999)

    jsonl = _make_jsonl(
        tmp_path,
        [
            "SayDo_A_Q1_2025_Q2_2025",
            "SayDo_B_Q1_2025_Q2_2025",
        ],
    )
    batches = _FakeBatchesAPI(poll_sequence=[], results_by_id={})

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "halted_cost_gate"
    gate_raw = summary["cost_gate"]
    assert isinstance(gate_raw, dict)
    gate = cast("dict[str, object]", gate_raw)
    assert gate["allowed"] is False
    assert "would exceed monthly cap" in str(gate["reason"])
    # No API traffic.
    assert batches.created == []


def test_cost_gate_passes_when_cap_has_headroom(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    # cap=$10, current spend=$0 -> trivially below cap.
    _init_budget_db(db_path, purpose="pairwise_analysis", cap_usd=10.00, current_spend=0.0)

    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("body A")),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "ended"
    assert summary["succeeded"] == 1


def test_cost_gate_fails_open_when_no_budget_row(tmp_path: Path) -> None:
    """Fresh repo with no portfolio.db should not block submission."""
    jsonl = _make_jsonl(tmp_path, ["SayDo_A_Q1_2025_Q2_2025"])
    batches = _FakeBatchesAPI(
        poll_sequence=[
            ("ended", {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0})
        ],
        results_by_id={
            "batch_test_001": [
                _individual("SayDo_A_Q1_2025_Q2_2025", _succeeded("body A")),
            ],
        },
    )

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "ended"
    assert summary["succeeded"] == 1


# ---------------------------------------------------------------------------
# Noop path: all targets already exist
# ---------------------------------------------------------------------------


def test_noop_when_every_target_already_exists(tmp_path: Path) -> None:
    tmp = tmp_path / ".tmp"
    tmp.mkdir()
    (tmp / "SayDo_A_Q1_2025_Q2_2025.txt").write_text("done", encoding="utf-8")
    (tmp / "SayDo_B_Q1_2025_Q2_2025.txt").write_text("done", encoding="utf-8")

    jsonl = _make_jsonl(
        tmp_path,
        [
            "SayDo_A_Q1_2025_Q2_2025",
            "SayDo_B_Q1_2025_Q2_2025",
        ],
    )
    batches = _FakeBatchesAPI(poll_sequence=[], results_by_id={})

    summary = submit_and_collect(
        jsonl,
        repo_root=tmp_path,
        dry_run=False,
        force_overwrite=False,
        client_factory=lambda: _FakeClient(batches),
        sleep=lambda _s: None,
        poll_backoff=(1,),
    )

    assert summary["status"] == "noop"
    assert summary["requests_skipped_existing"] == 2
    assert batches.created == []
