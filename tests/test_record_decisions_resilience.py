"""Tests for `execution/record_decisions.py` stage 0b resilience.

Regression coverage for the bug where a single TRANSIENT
`decision_conditions_extract` LLM failure (CLI subprocess non-zero exit, with
no Gemini fallback configured) propagated all the way up through
`attach_conditions` -> `main()` and crashed the whole morning-pipeline stage
(exit 1), leaving every OTHER unstamped decision without its conditions for
the day.

Covers:
  * main() exits 0 and prints the deferred_transient tally when
    attach_conditions / attach_qualitative_conditions report transient
    failures but no hard stop occurred.
  * main() still lets a hard stop (LLMSetupError) propagate as an uncaught
    exception — a missing CLI is an operator problem, not a stage-quality
    degradation, and must fail loudly.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_db_globals() -> Iterator[None]:
    """``record_decisions.main()`` -> ``_sync_db_path`` mutates the ``db``
    module's globals (PROJECT_ROOT/DATA_DIR/DB_PATH/FMP_DIR) directly, same as
    run_lens.py / build_artifacts.py. Restore them after each test so this
    file can never leak a temp-dir path into a test that runs afterward
    (see tests/test_db_path_sync.py for the established pattern)."""
    orig = (db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT)
    try:
        yield
    finally:
        db.DB_PATH, db.DATA_DIR, db.FMP_DIR, db.PROJECT_ROOT = orig


_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    recommendation_kind VARCHAR(32) NOT NULL,
    recommendation_value FLOAT,
    conviction VARCHAR(16),
    source_artifact_id INTEGER,
    source_memo_id INTEGER,
    source_lens VARCHAR(64),
    rationale_excerpt TEXT,
    made_at DATETIME NOT NULL,
    user_acted_at DATETIME,
    user_action_kind VARCHAR(32),
    user_notes TEXT,
    outcome_at DATETIME,
    outcome_label VARCHAR(16),
    outcome_pct FLOAT,
    outcome_notes TEXT,
    decision_conditions TEXT,
    conditions_extracted_at DATETIME,
    qualitative_conditions TEXT,
    qualitative_conditions_extracted_at DATETIME,
    created_at DATETIME NOT NULL,
    CHECK (source_artifact_id IS NOT NULL OR source_memo_id IS NOT NULL)
);
CREATE UNIQUE INDEX idx_decisions_source_artifact ON decisions(source_artifact_id);
CREATE UNIQUE INDEX uq_decisions_source_memo ON decisions(source_memo_id)
    WHERE source_memo_id IS NOT NULL;
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    purpose TEXT NOT NULL,
    content_md TEXT,
    generated_at TEXT NOT NULL,
    superseded_by_id INTEGER
);
CREATE TABLE advisor_memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu',
    kind TEXT NOT NULL,
    ticker TEXT,
    counter_ticker TEXT,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    context_json TEXT,
    stance TEXT,
    horizon_days INTEGER,
    score_status TEXT NOT NULL DEFAULT 'pending',
    note_id INTEGER,
    ledger_entry_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    kpi_definition_id INTEGER NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    line_item TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    source_doc_id INTEGER
);
"""

_NOW = datetime.now(UTC).replace(tzinfo=None).isoformat()

_FIVE_MIN_MD = """## 1. What changed
- NPLs ticked up 40bps.

## 2. Recommended action
**TRIM 20%** The credit book is wobbling.

## 3. What would change my mind
90d+ NPLs pushing above 7% for two consecutive quarters, or monthly ARPAC
falling below $10.
"""


def _load_record_decisions_module():
    """Import `execution/record_decisions.py` by file path (execution/ isn't
    a package). A fresh import per test avoids state bleeding between tests
    that monkeypatch `db` globals."""
    spec = importlib.util.spec_from_file_location(
        "record_decisions_under_test",
        _REPO_ROOT / "execution" / "record_decisions.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """A fake repo root with data/portfolio.db in the real layout
    (repo_root/data/portfolio.db) — record_decisions_from_artifacts derives
    its DB path from --repo-root, while attach_conditions/
    attach_qualitative_conditions take the explicit --db-path. Pointing BOTH
    flags at this same tree keeps main() off the real project DB."""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    path = root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return root


@pytest.fixture()
def db_path(repo_root: Path) -> Path:
    return repo_root / "data" / "portfolio.db"


def _insert_artifact_decision(path: Path, *, content_md: str, ticker: str) -> int:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO llm_artifacts (ticker, purpose, content_md, generated_at) "
        "VALUES (?, 'lens:five_min_reread', ?, ?)",
        (ticker, content_md, _NOW),
    )
    artifact_id = int(cur.lastrowid or 0)
    cur = conn.execute(
        "INSERT INTO decisions (ticker, recommendation_kind, source_artifact_id, "
        "source_lens, made_at, outcome_label, created_at) "
        "VALUES (?, 'trim', ?, 'five_min_reread', ?, 'pending', ?)",
        (ticker, artifact_id, _NOW, _NOW),
    )
    conn.commit()
    decision_id = int(cur.lastrowid or 0)
    conn.close()
    return decision_id


def test_main_exits_zero_and_prints_tally_on_transient_failure(
    repo_root: Path,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A day where 1-of-2 decision_conditions_extract calls transiently fails
    (CLI non-zero exit, Gemini fallback unavailable) must exit 0 — the
    per-item deferred_transient tally is printed, not raised."""
    module = _load_record_decisions_module()

    _insert_artifact_decision(db_path, content_md=_FIVE_MIN_MD, ticker="NU")
    _insert_artifact_decision(db_path, content_md=_FIVE_MIN_MD, ticker="MELI")

    import decision_conditions as dc

    calls: list[str] = []

    def flaky_structured(prompt: str, **kwargs: object) -> object:
        ticker = str(kwargs.get("ticker"))
        calls.append(ticker)
        if ticker == "NU":
            raise RuntimeError(
                "Claude CLI failed AND no Gemini fallback configured. "
                "Original Claude error: CalledProcessError: exit 1"
            )
        return [
            {
                "metric": "NPL 90d+",
                "metric_source": "kpi",
                "op": "gt",
                "threshold": 7.0,
                "unit": "percent",
                "for_periods": 2,
                "note": "test",
            }
        ]

    monkeypatch.setattr(dc, "call_llm_structured", flaky_structured)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "record_decisions.py",
            "--repo-root",
            str(repo_root),
            "--db-path",
            str(db_path),
        ],
    )

    exit_code = module.main()
    out = capsys.readouterr().out

    assert exit_code == 0
    # main() runs BOTH attach_conditions and attach_qualitative_conditions
    # over the same two decisions, so each ticker is attempted twice (once
    # per rung) — the key assertion is that MELI is reached at all despite
    # NU raising first, in both rungs.
    assert calls.count("NU") == 2
    assert calls.count("MELI") == 2
    assert "deferred_transient=1" in out
    assert "Condition extraction complete" in out


def test_main_propagates_hard_stop(
    repo_root: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMSetupError (missing CLI) must still crash the stage loudly — this
    is the genuine hard-stop case the exit-code simplification preserves."""
    module = _load_record_decisions_module()
    _insert_artifact_decision(db_path, content_md=_FIVE_MIN_MD, ticker="NU")

    import decision_conditions as dc
    from llm.cli import LLMSetupError

    def setup_broken(prompt: str, **kwargs: object) -> object:
        raise LLMSetupError("Claude Code CLI ('claude') not found in PATH.")

    monkeypatch.setattr(dc, "call_llm_structured", setup_broken)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "record_decisions.py",
            "--repo-root",
            str(repo_root),
            "--db-path",
            str(db_path),
        ],
    )

    with pytest.raises(LLMSetupError):
        module.main()


# ---------------------------------------------------------------------------
# --include-advisor gating (P2.1, PRD §9.3): machine-recommendation rungs are
# OFF by default -- an unadopted advisor view stays a plain llm_artifacts
# row, never auto-promoted to a decision.
# ---------------------------------------------------------------------------


def test_main_default_skips_machine_recommendation_rungs(
    repo_root: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_record_decisions_module()
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "record_decisions_from_artifacts",
        lambda **kw: calls.append("from_artifacts")
        or {"inserted": 0, "skipped_existing": 0, "no_recommendation": 0, "db_unavailable": 0},
    )
    monkeypatch.setattr(
        module,
        "record_socratic_decisions",
        lambda **kw: calls.append("socratic")
        or {"inserted": 0, "skipped_existing": 0, "skipped_no_stance": 0, "db_unavailable": 0},
    )
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["record_decisions.py", "--repo-root", str(repo_root), "--db-path", str(db_path), "--no-conditions"],
    )
    exit_code = module.main()
    assert exit_code == 0
    assert calls == []  # neither machine-recommendation rung ran
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_main_include_advisor_runs_machine_recommendation_rungs(
    repo_root: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_record_decisions_module()
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "record_decisions_from_artifacts",
        lambda **kw: calls.append("from_artifacts")
        or {"inserted": 0, "skipped_existing": 0, "no_recommendation": 0, "db_unavailable": 0},
    )
    monkeypatch.setattr(
        module,
        "record_socratic_decisions",
        lambda **kw: calls.append("socratic")
        or {"inserted": 0, "skipped_existing": 0, "skipped_no_stance": 0, "db_unavailable": 0},
    )
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "record_decisions.py",
            "--repo-root",
            str(repo_root),
            "--db-path",
            str(db_path),
            "--include-advisor",
            "--no-conditions",
        ],
    )
    exit_code = module.main()
    assert exit_code == 0
    assert calls == ["from_artifacts", "socratic"]
