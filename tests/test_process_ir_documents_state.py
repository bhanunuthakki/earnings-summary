"""Explicit state, cache, anchor and database routing for the narrative job."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import process_ir_documents as processor
import pytest

import db
import llm_client
from db_paths import resolve_db_path
from llm import cli
from llm.fleet_policy import FALLBACK_DISABLED_ENV_VAR, PRIMARY_BACKEND_ENV_VAR
from llm.resolver import EXPLICIT_MODEL_POLICY_ENV_VAR


def _identity(ticker: str) -> str:
    return ticker


def _alias(ticker: str) -> str:
    return "GOOG" if ticker == "GOOGL" else ticker


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _empty_anchor(*_args: object) -> str:
    return ""


def _composed_anchor(*_args: object) -> str:
    return "composed anchors"


def _index(root: Path, entries: Mapping[str, Mapping[str, object]]) -> Path:
    path = root / ".tmp" / "document_index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _quarter_doc(root: Path, ticker: str = "GOOG") -> dict[str, object]:
    source = root / "transcripts" / "raw" / "GOOG_Q1_2026.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("source transcript", encoding="utf-8")
    return {
        "ticker": ticker,
        "year": "2026",
        "quarter": "Q1",
        "doc_type": "transcript",
        "local_path": str(source.relative_to(root)),
        "processed": False,
    }


def test_job_uses_explicit_state_and_scoped_database_without_global_rebinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    index_path = _index(root, {"GOOG_2026_Q1_transcript": _quarter_doc(root)})
    database = tmp_path / "separate-authority.sqlite"
    database.touch()
    original_globals = (
        processor.PROJECT_ROOT,
        db.DB_PATH,
        processor.index_manager.PROJECT_ROOT,
        processor.index_manager.DOCUMENT_INDEX_PATH,
    )
    original_argv = list(sys.argv)
    previous_database = resolve_db_path(None)
    anchors: list[tuple[Path, str]] = []
    generated: list[tuple[str, str]] = []

    def anchor(repo_root: Path, ticker: str) -> str:
        assert resolve_db_path(None) == database
        anchors.append((repo_root, ticker))
        return "anchor"

    def generate(text: str, *, anchor_block: str) -> str:
        assert resolve_db_path(None) == database
        generated.append((text, anchor_block))
        return "SYNTHETIC SUMMARY"

    monkeypatch.setattr(processor, "resolve_ticker", _alias)
    monkeypatch.setattr(processor.index_manager, "resolve_ticker", _identity)
    for name in ("load_thesis_anchor", "load_bear_anchor", "load_ir_anchor"):
        monkeypatch.setattr(processor, name, anchor)
    monkeypatch.setattr(processor, "compose_anchor_block", _composed_anchor)
    monkeypatch.setattr(processor.time, "sleep", _noop)
    monkeypatch.setitem(
        cast("dict[str, object]", processor.DOC_TYPE_CONFIG["transcript"]), "llm_fn", generate
    )

    processor.run_for_ticker("GOOGL", repo_root=root, db_path=database)

    assert anchors == [(root, "GOOG")] * 3
    assert generated == [("source transcript", "composed anchors")]
    assert (root / ".tmp/GOOG_Q1_2026_summary.txt").read_text() == "SYNTHETIC SUMMARY"
    assert json.loads(index_path.read_text())["GOOG_2026_Q1_transcript"]["processed"] is True
    assert original_globals == (
        processor.PROJECT_ROOT,
        db.DB_PATH,
        processor.index_manager.PROJECT_ROOT,
        processor.index_manager.DOCUMENT_INDEX_PATH,
    )
    assert sys.argv == original_argv
    assert resolve_db_path(None) == previous_database
    assert not (root / "data/portfolio.db").exists()


def test_main_passes_all_selectors_and_explicit_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "authority.sqlite"
    database.touch()
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []
    environments: list[Path] = []

    def run(*args: object, **kwargs: object) -> int:
        seen.append((args, kwargs))
        return 0

    monkeypatch.setattr(processor, "run_for_ticker", run)
    monkeypatch.setattr(processor, "load_project_env", environments.append)
    original_argv = list(sys.argv)
    assert (
        processor.main(
            [
                "--ticker",
                "NU",
                "--repo-root",
                str(tmp_path),
                "--db",
                str(database),
                "--quarter",
                "Q3",
                "--year",
                "2025",
                "--dry-run",
                "--regenerate-missing",
            ]
        )
        == 0
    )
    assert seen == [
        (("NU", "Q3", "2025", True, True), {"repo_root": tmp_path, "db_path": database})
    ]
    assert environments == [tmp_path]
    assert sys.argv == original_argv


def test_all_selects_only_quarterly_tickers_from_explicit_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index(
        tmp_path,
        {
            "NU_2026_Q1_transcript": {
                "ticker": "NU",
                "year": "2026",
                "quarter": "Q1",
                "doc_type": "transcript",
                "local_path": "quarter.txt",
                "processed": False,
            },
            "GOOG_event_2026-01-01_abc12345": {
                "ticker": "GOOG",
                "event_date": "2026-01-01",
                "doc_type": "event",
                "local_path": "event.pdf",
                "processed": False,
            },
        },
    )
    database = tmp_path / "authority.sqlite"
    database.touch()
    seen: list[str] = []

    def run(ticker: str, *_args: object, **kwargs: object) -> int:
        assert kwargs == {"repo_root": tmp_path, "db_path": database}
        seen.append(ticker)
        return 0

    monkeypatch.setattr(processor, "load_project_env", _noop)
    monkeypatch.setattr(processor, "run_for_ticker", run)
    assert processor.main(["--all", "--repo-root", str(tmp_path), "--db", str(database)]) == 0
    assert seen == ["NU"]


@pytest.mark.parametrize(
    ("quarter", "year"),
    [(None, "2026"), ("Q1", "2026"), (None, "2025")],
)
def test_retired_event_keeps_history_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quarter: str | None, year: str
) -> None:
    document = {
        "ticker": "GOOG",
        "event_date": "2026-05-01",
        "doc_type": "event",
        "local_path": "event__abc12345.pdf",
        "processed": False,
    }
    (tmp_path / "event__abc12345.pdf").touch()
    index_path = _index(tmp_path, {"GOOG_event_2026-05-01_abc12345": document})
    cache = tmp_path / ".tmp/GOOG_event_2026-05-01_abc12345_event_brief.txt"
    cache.write_text("PRIOR EVENT BRIEF", encoding="utf-8")
    source = tmp_path / "event__abc12345.pdf"
    cache.with_suffix(".source.json").write_text(
        json.dumps(
            {
                "source_text_identity": processor.source_text_projection_identity(str(source)),
                "source_sha256": sha256(source.read_bytes()).hexdigest(),
                "summary_sha256": sha256(cache.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(processor, "resolve_ticker", _identity)
    monkeypatch.setattr(processor.index_manager, "resolve_ticker", _identity)

    attempts: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def no_generation(*args: object, **kwargs: object) -> str:
        attempts.append((args, kwargs))
        raise AssertionError("A cached event must not extract text")

    monkeypatch.setattr(processor, "extract_text", no_generation)
    assert processor.process_document(document, repo_root=tmp_path) is False
    assert attempts == []
    processor.run_for_ticker("GOOG", quarter=quarter, year=year, repo_root=tmp_path)
    assert attempts == []
    assert (
        json.loads(index_path.read_text())["GOOG_event_2026-05-01_abc12345"]["processed"] is False
    )
    assert cache.read_text() == "PRIOR EVENT BRIEF"


def test_missing_summary_repair_uses_quarterly_only_selection_from_mixed_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = _quarter_doc(tmp_path)
    transcript["processed"] = True
    event = {
        "ticker": "GOOG",
        "event_date": "2026-05-01",
        "doc_type": "event",
        "local_path": "event__abc12345.txt",
        "processed": False,
    }
    (tmp_path / "event__abc12345.txt").write_text("event source", encoding="utf-8")
    index_path = _index(
        tmp_path, {"GOOG_2026_Q1_transcript": transcript, "GOOG_event_2026-05-01_abc12345": event}
    )
    generated: list[str] = []

    def generate(text: str, *, anchor_block: str) -> str:
        generated.append(text)
        return "REPAIRED SUMMARY"

    monkeypatch.setattr(processor, "resolve_ticker", _identity)
    monkeypatch.setattr(processor.index_manager, "resolve_ticker", _identity)
    for name in ("load_thesis_anchor", "load_bear_anchor", "load_ir_anchor"):
        monkeypatch.setattr(processor, name, _empty_anchor)
    monkeypatch.setattr(processor.time, "sleep", _noop)
    monkeypatch.setitem(
        cast("dict[str, object]", processor.DOC_TYPE_CONFIG["transcript"]), "llm_fn", generate
    )

    processor.run_for_ticker("GOOG", regenerate_missing=True, repo_root=tmp_path)
    processor.run_for_ticker("GOOG", regenerate_missing=True, repo_root=tmp_path)

    assert generated == ["source transcript"]
    assert (tmp_path / ".tmp/GOOG_Q1_2026_summary.txt").read_text() == "REPAIRED SUMMARY"
    index = json.loads(index_path.read_text())
    assert index["GOOG_2026_Q1_transcript"]["processed"] is True
    assert index["GOOG_event_2026-05-01_abc12345"]["processed"] is False


def test_dry_run_preserves_preview_without_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index(tmp_path, {"GOOG_2026_Q1_transcript": _quarter_doc(tmp_path)})
    monkeypatch.setattr(processor, "load_project_env", _noop)
    monkeypatch.setattr(processor, "resolve_ticker", _identity)
    monkeypatch.setattr(processor.index_manager, "resolve_ticker", _identity)

    def no_database(_override: object) -> Path:
        raise AssertionError("A preview without --db must not resolve a database")

    monkeypatch.setattr(processor, "require_db_path", no_database)
    assert processor.main(["--ticker", "GOOG", "--repo-root", str(tmp_path), "--dry-run"]) == 0
    assert not (tmp_path / ".tmp/GOOG_Q1_2026_summary.txt").exists()


def test_dry_run_validates_an_explicit_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor, "load_project_env", _noop)
    with pytest.raises(FileNotFoundError, match="configured portfolio database is unavailable"):
        processor.main(
            [
                "--ticker",
                "GOOG",
                "--repo-root",
                str(tmp_path),
                "--dry-run",
                "--db",
                str(tmp_path / "missing.sqlite"),
            ]
        )


def test_real_summary_chain_routes_pins_budget_and_usage_to_explicit_database(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the provider subprocess/setup is stubbed; governed DB work is real."""
    root = tmp_path / "retained_state"
    document = _quarter_doc(root)
    blocked_document = {**document, "quarter": "Q2"}
    index_path = _index(
        root, {"GOOG_2026_Q1_transcript": document, "GOOG_2026_Q2_transcript": blocked_document}
    )
    database = migrated_db(tmp_path / "explicit_authority.db")
    decoy = migrated_db(tmp_path / "decoy_global.db")
    purpose = "transcript_summary"
    pinned_model = "claude-opus-4-8"
    now = datetime.now(UTC).isoformat()
    for path, model, cap, spent in (
        (database, pinned_model, 1.0, 0.85),
        (decoy, "claude-haiku-4-5-20251001", 0.5, 2.0),
    ):
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM model_pin_overrides WHERE purpose = ?", (purpose,))
            conn.execute(
                "INSERT INTO model_pin_overrides (purpose, model, set_by, set_at, reason_json, active) "
                "VALUES (?, ?, 'synthetic-test', ?, '{}', 1)",
                (purpose, model, now),
            )
            conn.execute(
                "INSERT INTO llm_budgets (purpose, monthly_cap_usd, warn_threshold_pct, hard_block, "
                "on_exceed, created_at, updated_at) VALUES (?, ?, 0.8, 1, 'block', ?, ?) "
                "ON CONFLICT(purpose) DO UPDATE SET monthly_cap_usd=excluded.monthly_cap_usd, "
                "warn_threshold_pct=0.8, hard_block=1, on_exceed='block'",
                (purpose, cap, now, now),
            )
            conn.execute(
                "INSERT INTO llm_calls (called_at, purpose, scope, model, prompt_sha256, "
                "prompt_chars, elapsed_ms, cost_estimate_usd) VALUES (?, ?, 'fixture', ?, 'seed', 1, 1, ?)",
                (now, purpose, model, spent),
            )
    decoy_root = tmp_path / "decoy_state"
    for name, value in {
        "DB_PATH": str(decoy),
        "DATA_DIR": str(decoy_root / "data"),
        "FMP_DIR": str(decoy_root / "data/historical/fmp"),
        "STATE_ROOT": str(decoy_root),
    }.items():
        monkeypatch.setattr(db, name, value)
    globals_before = (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.STATE_ROOT)
    assert resolve_db_path(None) == decoy
    decoy_sha = sha256(decoy.read_bytes()).hexdigest()
    bear = root / "data/bear_case/GOOG.json"
    bear.parent.mkdir(parents=True)
    bear.write_text(
        json.dumps({"most_underweighted": "SYNTHETIC_EXPLICIT_STATE_ANCHOR"}), encoding="utf-8"
    )
    monkeypatch.setenv(PRIMARY_BACKEND_ENV_VAR, "claude")
    monkeypatch.setenv(FALLBACK_DISABLED_ENV_VAR, "1")
    monkeypatch.delenv(EXPLICIT_MODEL_POLICY_ENV_VAR, raising=False)
    monkeypatch.delenv("LLM_CAPTURE_DIR", raising=False)
    monkeypatch.setattr(llm_client, "_setup_verified", True)
    monkeypatch.setattr(llm_client, "_claude_cli_path", "synthetic-claude")
    monkeypatch.setattr(cli, "_neutral_cwd_cache", str(tmp_path))
    monkeypatch.setattr(processor, "RATE_LIMIT_SLEEP", 0)
    calls: list[list[str]] = []

    def provider(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "synthetic-claude"
        assert argv[argv.index("--model") + 1] == pinned_model
        prompt = kwargs.get("input")
        assert isinstance(prompt, str)
        assert "SYNTHETIC_EXPLICIT_STATE_ANCHOR" in prompt
        assert resolve_db_path(None) == database
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "SYNTHETIC GOVERNED SUMMARY",
                    "total_cost_usd": 0.001,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 5,
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(llm_client.subprocess, "run", provider)
    assert processor.DOC_TYPE_CONFIG["transcript"]["llm_fn"] is llm_client.generate_summary
    assert processor.process_document(document, repo_root=root, db_path=database)
    assert len(calls) == 1
    assert (root / ".tmp/GOOG_Q1_2026_summary.txt").read_text() == "SYNTHETIC GOVERNED SUMMARY"
    assert json.loads(index_path.read_text())["GOOG_2026_Q1_transcript"]["processed"] is True
    with sqlite3.connect(database) as conn:
        usage = conn.execute(
            "SELECT purpose, model, input_tokens, output_tokens, cost_estimate_usd, error "
            "FROM llm_calls WHERE scope IS NULL"
        ).fetchall()
        assert usage == [(purpose, pinned_model, 10, 5, 0.001, None)]
        alerts = conn.execute("SELECT purpose, threshold_pct FROM llm_budget_alerts").fetchall()
        assert alerts == [(purpose, 0.8)]
        conn.execute("UPDATE llm_budgets SET monthly_cap_usd=0.5 WHERE purpose = ?", (purpose,))
    # An explicit-budget denial must stop the same real chain before transport.
    assert not processor.process_document(blocked_document, repo_root=root, db_path=database)
    assert len(calls) == 1
    assert not (root / ".tmp/GOOG_Q2_2026_summary.txt").exists()
    assert json.loads(index_path.read_text())["GOOG_2026_Q2_transcript"]["processed"] is False
    assert resolve_db_path(None) == decoy
    assert globals_before == (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.STATE_ROOT)
    assert sha256(decoy.read_bytes()).hexdigest() == decoy_sha
    assert not decoy_root.exists()
    assert not (root / "data/portfolio.db").exists()


@pytest.mark.parametrize("status", [0, 1])
def test_main_propagates_processor_status_with_explicit_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    database = tmp_path / "authority.sqlite"
    database.touch()
    original_argv = list(sys.argv)
    original_globals = (processor.PROJECT_ROOT, db.DB_PATH)

    def run(ticker: str, *_args: object, **kwargs: object) -> int:
        assert ticker == "NU"
        assert kwargs == {"repo_root": tmp_path, "db_path": database}
        return status

    monkeypatch.setattr(processor, "load_project_env", _noop)
    monkeypatch.setattr(processor, "run_for_ticker", run)
    assert (
        processor.main(["--ticker", "NU", "--repo-root", str(tmp_path), "--db", str(database)])
        == status
    )
    assert sys.argv == original_argv
    assert original_globals == (processor.PROJECT_ROOT, db.DB_PATH)


def test_event_retirement_preserves_registered_source_and_saved_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "ticker": "GOOG",
        "event_date": "2026-05-01",
        "doc_type": "event",
        "local_path": "event__abc12345.txt",
        "processed": False,
    }
    source = tmp_path / "event__abc12345.txt"
    source.write_bytes(b"Retained raw event source.")
    index = _index(tmp_path, {"GOOG_event_2026-05-01_abc12345": document})
    cache = tmp_path / ".tmp/GOOG_event_2026-05-01_abc12345_event_brief.txt"
    cache.write_bytes(b"Historical event brief.\r\n")
    sidecar = cache.with_suffix(".source.json")
    sidecar.write_bytes(b'{"historical":"unknown generation context"}\n')
    before = {path: path.read_bytes() for path in (source, index, cache, sidecar)}
    selected: list[object] = []

    def reject_synthesis(doc: object, **_kwargs: object) -> bool:
        selected.append(doc)
        return True

    monkeypatch.setattr(processor, "resolve_ticker", _identity)
    monkeypatch.setattr(processor.index_manager, "resolve_ticker", _identity)
    monkeypatch.setattr(processor, "process_document", reject_synthesis)
    assert processor.run_for_ticker("GOOG", repo_root=tmp_path) == 0
    assert selected == []
    assert processor.cache_path_for(document, repo_root=tmp_path) == cache
    assert {path: path.read_bytes() for path in before} == before
