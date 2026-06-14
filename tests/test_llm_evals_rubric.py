"""Mode-B rubric judge (src/evals/rubric_judge.py + corpora.py, plan PR 2):
rubric parsing, fail-closed facet verdicts, hard-stop aborts, the three
corpus loaders, end-to-end audit persistence, the judge spot-check script,
and the §5.5 registry wiring.

All LLM calls are injected fakes — the suite never spends.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config

import evals.judge as ej
from alembic import command
from evals.corpora import (
    CORPUS_LOADERS,
    AuditItem,
    filter_since,
    load_advisor_next_dollar_corpus,
    load_ask_advisory_answer_corpus,
    load_bear_case_corpus,
    load_transcript_summary_corpus,
)
from evals.harness import EvalAbortError, now_naive_utc, persist_summary
from evals.rubric_judge import (
    AUDIT_SPECS,
    Rubric,
    judge_item,
    load_rubric,
    parse_rubric_verdict,
    run_rubric_eval,
)
from llm.cli import LLMBudgetExceeded
from llm.prompt_versions import prompt_version_for, registered_purposes

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_RUBRIC_MD = """\
# Rubric: bear_case (v1)

Pass threshold: 0.70

## Facet: alpha — first facet bar
What earns 1.0 / 0.0.

## Facet: beta — second facet bar
More criteria.
"""


@pytest.fixture
def rubric(tmp_path: Path) -> Rubric:
    path = tmp_path / "bear_case.md"
    path.write_text(_RUBRIC_MD, encoding="utf-8")
    return load_rubric(path, purpose="bear_case")


def _item(item_id: str = "NU", content: str = '{"failure_modes": []}') -> AuditItem:
    return AuditItem(
        item_id=item_id,
        label=f"bear_case/{item_id} (data/bear_case/{item_id}.json)",
        ticker=item_id,
        content=content,
        produced_at=now_naive_utc(),
    )


def _verdict_json(alpha: float, beta: float, rationale: str = "weakest: beta") -> str:
    return json.dumps({"facet_scores": {"alpha": alpha, "beta": beta}, "rationale": rationale})


def _caller_returning(raw: str):
    def fake(prompt: str, **_kw: object) -> str:
        return raw

    return fake


# ----------------------------------------------------------------------------
# checked-in rubrics + registry wiring
# ----------------------------------------------------------------------------


def test_checked_in_rubrics_are_valid() -> None:
    for purpose, spec in AUDIT_SPECS.items():
        r = load_rubric(PROJECT_ROOT / spec.rubric_relpath, purpose=purpose)
        assert r.purpose == purpose
        assert 0.5 <= r.pass_threshold <= 1.0
        assert len(r.facet_ids) >= 4, f"{purpose} rubric should be multi-facet"
        assert len(set(r.facet_ids)) == len(r.facet_ids)
        assert r.sha256


def test_audit_wiring_is_consistent() -> None:
    # Every audit spec has a corpus loader, a prompt_versions entry, and the
    # runner CLI exposes exactly the same purposes.
    assert set(AUDIT_SPECS) == set(CORPUS_LOADERS)
    assert set(AUDIT_SPECS) <= registered_purposes()
    runner = _load_execution_module("run_llm_evals")
    assert set(runner.AUDIT_PURPOSES) == set(AUDIT_SPECS)


def test_section_5_5_model_pins() -> None:
    from llm.cli import DEFAULT_MODEL, FAST_CLASSIFIER_MODEL, LLM_MODELS

    assert LLM_MODELS.get("pressure_test_thesis") == DEFAULT_MODEL
    assert LLM_MODELS.get("bear_case_grading") == DEFAULT_MODEL
    assert LLM_MODELS.get("decision_extraction") == FAST_CLASSIFIER_MODEL
    assert "eval_judge" in registered_purposes()


# ----------------------------------------------------------------------------
# rubric parsing — loud validation
# ----------------------------------------------------------------------------


def test_load_rubric_parses_fields(rubric: Rubric) -> None:
    assert rubric.purpose == "bear_case"
    assert rubric.pass_threshold == 0.70
    assert rubric.facet_ids == ("alpha", "beta")
    assert "# Rubric: bear_case" in rubric.text


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("Pass threshold: 0.7\n\n## Facet: a — x\n", "missing `# Rubric:"),
        ("# Rubric: other_purpose (v1)\nPass threshold: 0.7\n\n## Facet: a — x\n", "expected"),
        ("# Rubric: bear_case (v1)\n\n## Facet: a — x\n", "missing `Pass threshold"),
        ("# Rubric: bear_case (v1)\nPass threshold: 1.7\n\n## Facet: a — x\n", "outside"),
        ("# Rubric: bear_case (v1)\nPass threshold: 0.7\n", "no `## Facet:"),
        (
            "# Rubric: bear_case (v1)\nPass threshold: 0.7\n\n## Facet: a — x\n## Facet: a — y\n",
            "duplicate facet",
        ),
    ],
)
def test_load_rubric_rejects_bad_files(tmp_path: Path, body: str, fragment: str) -> None:
    path = tmp_path / "r.md"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="rubric"):
        try:
            load_rubric(path, purpose="bear_case")
        except ValueError as exc:
            assert fragment in str(exc)
            raise


# ----------------------------------------------------------------------------
# verdict parsing — fail closed
# ----------------------------------------------------------------------------


def test_parse_rubric_verdict_accepts_plain_and_fenced() -> None:
    raw = _verdict_json(1.0, 0.5)
    v = parse_rubric_verdict(raw, ("alpha", "beta"))
    assert v is not None
    assert v.facet_scores == {"alpha": 1.0, "beta": 0.5}
    assert v.overall == pytest.approx(0.75)
    fenced = parse_rubric_verdict(f"```json\n{raw}\n```", ("alpha", "beta"))
    assert fenced is not None and fenced.rationale == "weakest: beta"


def test_parse_rubric_verdict_clamps() -> None:
    v = parse_rubric_verdict(_verdict_json(7, -2), ("alpha", "beta"))
    assert v is not None
    assert v.facet_scores == {"alpha": 1.0, "beta": 0.0}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1]",
        json.dumps({"facet_scores": {"alpha": 1.0}, "rationale": "r"}),  # missing facet
        json.dumps(
            {"facet_scores": {"alpha": 1.0, "beta": 1.0, "gamma": 1.0}, "rationale": "r"}
        ),  # extra facet
        json.dumps({"facet_scores": {"alpha": True, "beta": 1.0}, "rationale": "r"}),  # bool
        json.dumps({"facet_scores": {"alpha": "1", "beta": 1.0}, "rationale": "r"}),  # str score
        json.dumps({"facet_scores": {"alpha": 1.0, "beta": 1.0}, "rationale": ""}),  # empty rat.
        json.dumps({"facet_scores": [1.0, 1.0], "rationale": "r"}),  # non-dict scores
        json.dumps({"rationale": "r"}),  # missing scores
    ],
)
def test_parse_rubric_verdict_fails_closed(raw: str) -> None:
    assert parse_rubric_verdict(raw, ("alpha", "beta")) is None


# ----------------------------------------------------------------------------
# judge_item — the mode-B ladder
# ----------------------------------------------------------------------------


def test_judge_item_pass_above_threshold(rubric: Rubric) -> None:
    res = judge_item(
        rubric, _item(), run_id="r1", caller=_caller_returning(_verdict_json(1.0, 0.8))
    )
    assert res.passed and res.score == pytest.approx(0.9)
    assert res.failure_stage is None
    assert res.judge_rationale == "weakest: beta"
    assert res.actual_json is not None and "facet_scores" in res.actual_json
    assert res.expected_json is not None and rubric.sha256 in res.expected_json
    assert res.prompt_text is not None and "BEGIN OUTPUT" in res.prompt_text
    assert res.latency_ms is not None


def test_judge_item_below_threshold_fails(rubric: Rubric) -> None:
    res = judge_item(
        rubric, _item(), run_id="r1", caller=_caller_returning(_verdict_json(0.5, 0.5))
    )
    assert not res.passed and res.score == pytest.approx(0.5)
    assert res.failure_stage == "below_threshold"


def test_judge_item_unparseable_fails_closed(rubric: Rubric) -> None:
    res = judge_item(rubric, _item(), run_id="r1", caller=_caller_returning("looks good to me!"))
    assert not res.passed and res.score == 0.0
    assert res.failure_stage == "judge"
    assert res.judge_verdict == "looks good to me!"  # raw preserved for audit


def test_judge_item_transient_call_failure_fails_closed(rubric: Rubric) -> None:
    def boom(prompt: str, **_kw: object) -> str:
        raise RuntimeError("CLI exploded")

    res = judge_item(rubric, _item(), run_id="r1", caller=boom)
    assert not res.passed and res.failure_stage == "judge"
    assert res.judge_rationale is not None and "CLI exploded" in res.judge_rationale


def test_judge_item_hard_stop_aborts(rubric: Rubric) -> None:
    def capped(prompt: str, **_kw: object) -> str:
        raise LLMBudgetExceeded("eval_judge over cap")

    with pytest.raises(EvalAbortError, match="hard stop"):
        judge_item(rubric, _item(), run_id="r1", caller=capped)


def test_mode_a_run_judge_hard_stop_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    def capped(prompt: str, **_kw: object) -> str:
        raise LLMBudgetExceeded("eval_judge over cap")

    monkeypatch.setattr(ej, "call_llm", capped)
    with pytest.raises(EvalAbortError, match="hard stop"):
        ej.run_judge("q", "{}", "{}", "diff", run_id="r1")


# ----------------------------------------------------------------------------
# corpus loaders
# ----------------------------------------------------------------------------


def test_bear_case_corpus_newest_first_and_skips_corrupt(tmp_path: Path) -> None:
    base = tmp_path / "data" / "bear_case"
    base.mkdir(parents=True)
    (base / "NU.json").write_text(json.dumps({"failure_modes": [1]}), encoding="utf-8")
    (base / "MELI.json").write_text(json.dumps({"failure_modes": [2]}), encoding="utf-8")
    (base / "BAD.json").write_text("{not json", encoding="utf-8")
    now = time.time()
    os.utime(base / "MELI.json", (now - 86400, now - 86400))
    os.utime(base / "NU.json", (now, now))

    items = load_bear_case_corpus(tmp_path)
    assert [i.item_id for i in items] == ["NU", "MELI"]
    assert items[0].ticker == "NU"
    assert "data/bear_case/NU.json" in items[0].label
    assert "failure_modes" in items[0].content


def test_transcript_summary_corpus_fiscal_order(tmp_path: Path) -> None:
    tmp_dir = tmp_path / ".tmp"
    tmp_dir.mkdir()
    (tmp_dir / "NU_Q1_2026_summary.txt").write_text("q1 note", encoding="utf-8")
    (tmp_dir / "MELI_Q4_2025_investor_update_summary.txt").write_text("iu note", encoding="utf-8")
    (tmp_dir / "NU_Q3_2025_summary.txt").write_text("old note", encoding="utf-8")
    (tmp_dir / "NU_Q3_2025.txt").write_text("raw transcript, not a summary", encoding="utf-8")
    (tmp_dir / "notes.txt").write_text("unrelated", encoding="utf-8")
    # An old quarter re-rendered NOW must not outrank the newest print.
    now = time.time()
    os.utime(tmp_dir / "NU_Q3_2025_summary.txt", (now, now))

    items = load_transcript_summary_corpus(tmp_path)
    assert [i.item_id for i in items] == ["NU_Q1_2026", "MELI_Q4_2025", "NU_Q3_2025"]
    assert items[1].ticker == "MELI"
    assert items[0].content == "q1 note"


def test_advisor_corpus_reads_next_dollar_memos(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "portfolio.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE advisor_memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, kind TEXT, ticker TEXT, counter_ticker TEXT,
            title TEXT, body_md TEXT, context_json TEXT, stance TEXT,
            horizon_days INTEGER, score_status TEXT, note_id INTEGER,
            ledger_entry_id INTEGER, created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO advisor_memos (kind, ticker, title, body_md, created_at)"
        " VALUES ('next_dollar', NULL, 'June memo', '## Where the next dollar works hardest',"
        " '2026-06-01T10:00:00')"
    )
    conn.execute(
        "INSERT INTO advisor_memos (kind, ticker, title, body_md, created_at)"
        " VALUES ('swap_check', 'NU', 'swap', 'not audited here', '2026-06-02T10:00:00')"
    )
    conn.execute(
        "INSERT INTO advisor_memos (kind, ticker, title, body_md, created_at)"
        " VALUES ('next_dollar', NULL, 'May memo', 'older memo', '2026-05-01T10:00:00')"
    )
    conn.commit()
    conn.close()

    items = load_advisor_next_dollar_corpus(tmp_path)
    assert [i.item_id for i in items] == ["memo:3", "memo:1"]  # newest id first
    assert items[1].label.endswith("June memo")
    assert items[1].produced_at is not None and items[1].produced_at.month == 6


def test_advisor_corpus_tolerates_missing_table_and_db(tmp_path: Path) -> None:
    assert load_advisor_next_dollar_corpus(tmp_path) == []  # no DB at all
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "portfolio.db").close()  # DB, no table
    assert load_advisor_next_dollar_corpus(tmp_path) == []


def _seed_ask_turns(repo: Path) -> None:
    data = repo / "data"
    data.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data / "portfolio.db")
    conn.executescript(
        """
        CREATE TABLE ask_sessions (
            id TEXT PRIMARY KEY, scope TEXT, title TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE ask_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, text TEXT, citations_json TEXT,
            model TEXT, created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, title, created_at, updated_at)"
        " VALUES ('s1', 'portfolio', 'desk', '2026-06-01T09:00:00', '2026-06-01T09:00:00')"
    )
    conn.execute(
        "INSERT INTO ask_sessions (id, scope, title, created_at, updated_at)"
        " VALUES ('s2', 'ticker', 'NU', '2026-06-02T09:00:00', '2026-06-02T09:00:00')"
    )
    long_answer = "The case for trimming NU rests on " + ("x " * 40)
    cites = json.dumps([{"n": 1, "kind": "fact", "label": "NU NPL ratio", "confidence": 0.79}])
    rows = [
        ("s1", "user", "should I trim NU?", None, "2026-06-01T09:00:00"),
        ("s1", "assistant", long_answer, cites, "2026-06-01T09:00:05"),
        # data-view confirmation — must be excluded
        (
            "s1",
            "assistant",
            "3 series · yoy · quarterly (rendered as a live data view)",
            None,
            "2026-06-01T09:01:00",
        ),
        # too-short ack — must be excluded
        ("s1", "user", "thanks", None, "2026-06-01T09:02:00"),
        ("s1", "assistant", "ok", None, "2026-06-01T09:02:05"),
        # newest advisory answer, different (ticker) session
        ("s2", "user", "why did margins fall?", None, "2026-06-02T09:00:00"),
        ("s2", "assistant", "Margins fell because " + ("y " * 40), None, "2026-06-02T09:00:05"),
    ]
    conn.executemany(
        "INSERT INTO ask_turns (session_id, role, text, citations_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_ask_advisory_answer_corpus_filters_and_orders(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _seed_ask_turns(repo)

    items = load_ask_advisory_answer_corpus(repo)
    # Only the two genuine advisory answers — the data-view turn and the "ok"
    # ack are excluded — newest (the s2 margins answer) first.
    assert [i.item_id for i in items] == ["ask_turn:7", "ask_turn:2"]
    margins, nu = items
    assert margins.produced_at is not None and margins.produced_at.day == 2
    # The content delimits the answer and carries the conversation context +
    # the cited evidence the grounding facet checks against.
    assert "ANSWER UNDER AUDIT" in nu.content
    assert "should I trim NU?" in nu.content  # prior user turn as context
    assert "NU NPL ratio" in nu.content and "conf 79%" in nu.content  # rendered citation
    assert "(no sources cited)" in margins.content  # answer with no citations
    assert "(portfolio)" in nu.label and nu.ticker is None


def test_ask_advisory_answer_corpus_tolerates_missing_table_and_db(tmp_path: Path) -> None:
    assert load_ask_advisory_answer_corpus(tmp_path) == []  # no DB at all
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "portfolio.db").close()  # DB, no ask_turns table
    assert load_ask_advisory_answer_corpus(tmp_path) == []


def test_filter_since_excludes_old_and_undated() -> None:
    fresh = _item("FRESH")
    old = AuditItem(item_id="OLD", label="x", ticker=None, content="c", produced_at=None)
    items = [fresh, old]
    assert filter_since(items, None) == items
    assert filter_since(items, 7) == [fresh]


# ----------------------------------------------------------------------------
# end-to-end audit run + persistence
# ----------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_repo(tmp_path: Path) -> Path:
    """A repo-root with data/portfolio.db migrated to 0083 (eval tables) +
    a hand-built prompt_calibration_scores (its migration predates the
    stamp), mirroring test_llm_evals._migrated_db."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db_path = repo / "data" / "portfolio.db"
    cfg = _alembic_cfg(db_path)
    command.stamp(cfg, "0082_expected_earnings_revival")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE prompt_calibration_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose VARCHAR(64) NOT NULL,
            prompt_version VARCHAR(32) NOT NULL,
            ticker VARCHAR(16),
            score FLOAT NOT NULL,
            reason VARCHAR(200),
            scored_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            scored_by VARCHAR(64),
            artifact_id INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    command.upgrade(cfg, "0083_eval_runs")
    return repo


def test_run_rubric_eval_end_to_end_persists(tmp_path: Path) -> None:
    repo = _migrated_repo(tmp_path)
    base = repo / "data" / "bear_case"
    base.mkdir()
    (base / "NU.json").write_text(json.dumps({"failure_modes": ["good"]}), encoding="utf-8")
    (base / "MELI.json").write_text(json.dumps({"failure_modes": ["weak"]}), encoding="utf-8")
    now = time.time()
    os.utime(base / "NU.json", (now, now))
    os.utime(base / "MELI.json", (now - 60, now - 60))

    checked_in = load_rubric(
        PROJECT_ROOT / AUDIT_SPECS["bear_case"].rubric_relpath, purpose="bear_case"
    )

    def uniform_verdict(value: float, rationale: str) -> str:
        return json.dumps(
            {"facet_scores": dict.fromkeys(checked_in.facet_ids, value), "rationale": rationale}
        )

    verdicts = {
        "NU": uniform_verdict(0.9, "strong"),
        "MELI": uniform_verdict(0.4, "generic risks"),
    }

    def fake_caller(prompt: str, *, ticker: str | None = None, **_kw: object) -> str:
        assert ticker is not None
        return verdicts[ticker]

    summary = run_rubric_eval(
        "bear_case",
        db_path=repo / "data" / "portfolio.db",
        repo_root=repo,
        code_root=PROJECT_ROOT,  # checked-in rubric
        caller=fake_caller,
    )
    assert summary.mode == "audit"
    assert summary.purpose == "bear_case"
    assert summary.prompt_version == prompt_version_for("bear_case")
    assert summary.n_cases == 2 and summary.n_pass == 1
    assert summary.avg_score == pytest.approx((0.9 + 0.4) / 2)
    assert summary.judge_model is not None
    assert summary.golden_set_sha is not None  # the rubric sha
    assert summary.notes is not None and "n=2" in summary.notes

    run_db_id = persist_summary(summary, db_path=repo / "data" / "portfolio.db")
    conn = sqlite3.connect(repo / "data" / "portfolio.db")
    run_row = conn.execute(
        "SELECT purpose, mode, n_cases, n_pass, golden_set_sha FROM eval_runs WHERE id = ?",
        (run_db_id,),
    ).fetchone()
    cases = conn.execute(
        "SELECT case_id, passed, failure_stage FROM eval_case_results WHERE eval_run_id = ?"
        " ORDER BY id",
        (run_db_id,),
    ).fetchall()
    bridge = conn.execute(
        "SELECT purpose, prompt_version, scored_by FROM prompt_calibration_scores"
    ).fetchone()
    conn.close()
    assert run_row == ("bear_case", "audit", 2, 1, summary.golden_set_sha)
    assert cases == [("NU", 1, None), ("MELI", 0, "below_threshold")]
    assert bridge == ("bear_case", prompt_version_for("bear_case"), "auto:eval_harness")


def test_run_rubric_eval_empty_corpus_and_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    summary = run_rubric_eval(
        "bear_case",
        db_path=repo / "data" / "portfolio.db",
        repo_root=repo,
        code_root=PROJECT_ROOT,
        caller=_caller_returning("never called"),
    )
    assert summary.n_cases == 0 and summary.avg_score is None

    base = repo / "data" / "bear_case"
    base.mkdir()
    for i, t in enumerate(("NU", "MELI", "NOW")):
        p = base / f"{t}.json"
        p.write_text("{}", encoding="utf-8")
        ts = time.time() - i * 60
        os.utime(p, (ts, ts))
    rubric_path = PROJECT_ROOT / AUDIT_SPECS["bear_case"].rubric_relpath
    rubric = load_rubric(rubric_path, purpose="bear_case")
    all_ones = json.dumps(
        {"facet_scores": dict.fromkeys(rubric.facet_ids, 1.0), "rationale": "fine"}
    )
    summary2 = run_rubric_eval(
        "bear_case",
        db_path=repo / "data" / "portfolio.db",
        repo_root=repo,
        code_root=PROJECT_ROOT,
        limit=1,
        caller=_caller_returning(all_ones),
    )
    assert summary2.n_cases == 1
    assert summary2.cases[0].case_id == "NU"  # newest mtime first

    with pytest.raises(ValueError, match="no audit spec"):
        run_rubric_eval(
            "not_a_purpose",
            db_path=repo / "data" / "portfolio.db",
            repo_root=repo,
            code_root=PROJECT_ROOT,
        )


# ----------------------------------------------------------------------------
# the spot-check script
# ----------------------------------------------------------------------------


def _load_execution_module(name: str) -> ModuleType:
    src = PROJECT_ROOT / "execution" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # slots=True dataclasses resolve their module through sys.modules at class
    # creation — exec without registering would crash in dataclasses.py.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_judged_cases(repo: Path) -> None:
    conn = sqlite3.connect(repo / "data" / "portfolio.db")
    conn.execute(
        "INSERT INTO eval_runs (run_id, purpose, mode, prompt_version, model,"
        " n_cases, n_pass, started_at) VALUES ('rid1', 'bear_case', 'audit', 'v1', 'm',"
        " 3, 1, '2026-06-11T00:00:00')"
    )
    for i, (case_id, verdict) in enumerate([("NU", '{"v":1}'), ("MELI", '{"v":2}'), ("NOW", None)]):
        conn.execute(
            "INSERT INTO eval_case_results (eval_run_id, case_id, question, passed, score,"
            " judge_verdict, judge_rationale, created_at)"
            " VALUES (1, ?, ?, ?, 0.5, ?, 'because', '2026-06-11T00:00:00')",
            (case_id, f"label {i}", i % 2, verdict),
        )
    conn.commit()
    conn.close()


def test_spot_check_records_agreement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _migrated_repo(tmp_path)
    _seed_judged_cases(repo)
    mod = _load_execution_module("spot_check_eval_judge")

    cases = mod.load_judged_cases(repo / "data" / "portfolio.db", purpose="bear_case", n=10)
    assert [c.case_id for c in cases] == ["MELI", "NU"]  # judged only, newest row first

    monkeypatch.setattr("sys.stdin", io.StringIO("y\nn\n"))
    rc = mod.main(["--purpose", "bear_case", "--n", "5", "--repo-root", str(repo)])
    assert rc == 0
    conn = sqlite3.connect(repo / "data" / "portfolio.db")
    row = conn.execute(
        "SELECT purpose, score, scored_by, reason FROM prompt_calibration_scores"
        " WHERE scored_by = 'manual:judge_spot_check'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "eval_judge" and row[1] == pytest.approx(0.5)
    assert "target=bear_case" in row[3]


def test_spot_check_skip_quit_and_no_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _migrated_repo(tmp_path)
    _seed_judged_cases(repo)
    mod = _load_execution_module("spot_check_eval_judge")

    monkeypatch.setattr("sys.stdin", io.StringIO("s\ny\nq\n"))
    rc = mod.main(["--n", "10", "--repo-root", str(repo), "--no-persist"])
    assert rc == 0
    conn = sqlite3.connect(repo / "data" / "portfolio.db")
    count = conn.execute(
        "SELECT COUNT(*) FROM prompt_calibration_scores WHERE scored_by = 'manual:judge_spot_check'"
    ).fetchone()[0]
    conn.close()
    assert count == 0  # --no-persist honored


def test_spot_check_empty_db_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _migrated_repo(tmp_path)
    mod = _load_execution_module("spot_check_eval_judge")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert mod.main(["--repo-root", str(repo)]) == 0
