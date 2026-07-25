"""LLM eval harness (src/evals/, directives/llm_evals_plan.md): golden-set
integrity, the deterministic compare, fail-closed judge parsing, the
grade-case ladder, end-to-end persistence, and migration 0083.

All LLM calls are monkeypatched / injected — the suite never spends.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from alembic.config import Config

import evals.viewspec_compile as evc
from alembic import command
from evals.harness import CaseResult, EvalAbortError, EvalRunSummary, now_naive_utc, persist_summary
from evals.judge import JudgeOutcome, JudgeVerdict, parse_verdict
from evals.store import write_run
from evals.viewspec_compile import GoldenCase, load_golden, run_viewspec_eval, spec_diff
from llm.prompt_versions import prompt_version_for
from viewspec.nl_compile import NLCompileResult
from viewspec.spec import ViewSpec

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = PROJECT_ROOT / "evals" / "golden" / "viewspec_compile.json"


def _spec(**overrides: object) -> ViewSpec:
    base: dict[str, object] = {
        "tickers": ["NU"],
        "metrics": ["fin:revenue"],
        "transform": "yoy",
        "cadence": "quarterly",
        "periods": 8,
    }
    base.update(overrides)
    return ViewSpec.from_dict(base)


def _case(case_id: str = "c-1", *, periods_flexible: bool = False, **spec_kw: object) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        question="revenue growth for NU, last 8 quarters",
        expected_spec=_spec(**spec_kw),
        context_tickers=("NU",),
        periods_flexible=periods_flexible,
    )


_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_type TEXT NOT NULL, doc_type TEXT NOT NULL,
    file_path TEXT NOT NULL, sha256 TEXT NOT NULL, fetched_at TIMESTAMP NOT NULL,
    fetch_status TEXT NOT NULL, raw_bytes_size INTEGER NOT NULL DEFAULT 0,
    source_url TEXT, source_quality_tier TEXT NOT NULL DEFAULT 'fmp_normalized'
);
CREATE TABLE financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, period_end TIMESTAMP NOT NULL,
    fiscal_period_type TEXT NOT NULL, line_item TEXT NOT NULL, value TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'actual', source_doc_id INTEGER NOT NULL, locator TEXT
);
CREATE TABLE tracked_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'bhanu', ticker TEXT NOT NULL,
    name TEXT NOT NULL, list_type TEXT NOT NULL
);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "eval.db"
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO documents (id, ticker, source_type, doc_type, file_path, sha256,"
        " fetched_at, fetch_status) VALUES (1, 'NU', 'fmp', 'fmp_income_statement',"
        " 'f.json', 'a', '2026-01-05 10:00:00', 'ok')"
    )
    conn.execute(
        "INSERT INTO financial_facts (ticker, period_end, fiscal_period_type, line_item,"
        " value, source_doc_id) VALUES ('NU', '2025-12-31 00:00:00', 'Q4', 'revenue', 100, 1)"
    )
    conn.commit()
    conn.close()
    return path


def _ok_compile(spec: ViewSpec) -> NLCompileResult:
    return NLCompileResult(
        status="ok",
        spec=spec,
        attempts=1,
        prompt_text="PROMPT",
        raw_response=json.dumps(spec.to_dict()),
    )


def _compile_returning(result: NLCompileResult) -> Callable[..., NLCompileResult]:
    def fake(*_a: object, **_k: object) -> NLCompileResult:
        return result

    return fake


def _judge_returning(equivalent: bool, score: float) -> evc.JudgeFn:
    def fake_judge(
        question: str, expected_json: str, actual_json: str, diff: str, *, run_id: str | None = None
    ) -> JudgeOutcome:
        return JudgeOutcome(
            verdict=JudgeVerdict(equivalent=equivalent, score=score, rationale="because"),
            raw=json.dumps({"equivalent": equivalent, "score": score, "rationale": "because"}),
        )

    return fake_judge


# ----------------------------------------------------------------------------
# golden-set fixture integrity (the checked-in file itself)
# ----------------------------------------------------------------------------


def test_checked_in_golden_set_is_valid() -> None:
    cases = load_golden(GOLDEN_PATH)
    assert len(cases) >= 15
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))
    # Every expected spec validated through ViewSpec.from_dict at load time;
    # spot-check the domains the set claims to cover.
    tokens = {m.token() for c in cases for m in c.expected_spec.metrics}
    assert any(t.startswith("fin:") for t in tokens)
    assert any(t.startswith("kpi:") for t in tokens)
    assert any(t.startswith("seg:") for t in tokens)
    transforms = {c.expected_spec.transform for c in cases}
    assert transforms >= {"level", "yoy", "cagr", "margin"}


def test_load_golden_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "g.json"
    bad.write_text(
        json.dumps(
            {
                "purpose": "viewspec_compile",
                "cases": [
                    {"id": "a", "question": "q", "context_tickers": ["NU"], "expected_spec": {}},
                    {"id": "a", "question": "", "context_tickers": [], "expected_spec": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        load_golden(bad)
    msg = str(exc.value)
    assert "duplicate id" in msg
    assert "expected_spec invalid" in msg
    assert "missing question" in msg

    wrong_purpose = tmp_path / "p.json"
    wrong_purpose.write_text(json.dumps({"purpose": "bear_case", "cases": [{}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="purpose"):
        load_golden(wrong_purpose)


# ----------------------------------------------------------------------------
# the deterministic compare
# ----------------------------------------------------------------------------


def test_spec_diff_is_order_insensitive() -> None:
    a = ViewSpec.from_dict(
        {"tickers": ["NU", "MELI"], "metrics": ["fin:revenue", "fin:net_income"]}
    )
    b = ViewSpec.from_dict(
        {"tickers": ["MELI", "NU"], "metrics": ["fin:net_income", "fin:revenue"]}
    )
    assert spec_diff(a, b, periods_flexible=False) == []


def test_spec_diff_flags_each_field() -> None:
    expected = _spec()
    actual = ViewSpec.from_dict(
        {
            "tickers": ["MELI"],
            "metrics": ["fin:net_income"],
            "transform": "level",
            "cadence": "annual",
            "periods": 4,
        }
    )
    diffs = "\n".join(spec_diff(expected, actual, periods_flexible=False))
    for fragment in ("tickers:", "metrics:", "transform:", "cadence:", "periods:"):
        assert fragment in diffs


def test_spec_diff_periods_flexible_and_cagr_years() -> None:
    assert spec_diff(_spec(periods=8), _spec(periods=16), periods_flexible=True) == []
    assert spec_diff(_spec(periods=8), _spec(periods=16), periods_flexible=False) != []
    # cagr_years only matters under transform="cagr"
    assert spec_diff(_spec(cagr_years=3), _spec(cagr_years=5), periods_flexible=False) == []
    got = spec_diff(
        _spec(transform="cagr", cagr_years=3),
        _spec(transform="cagr", cagr_years=5),
        periods_flexible=False,
    )
    assert got and "cagr_years" in got[0]


# ----------------------------------------------------------------------------
# judge verdict parsing — fail closed
# ----------------------------------------------------------------------------


def test_parse_verdict_accepts_plain_and_fenced() -> None:
    raw = '{"equivalent": true, "score": 0.9, "rationale": "same analysis"}'
    v = parse_verdict(raw)
    assert v is not None and v.equivalent and v.score == 0.9
    fenced = parse_verdict(f"```json\n{raw}\n```")
    assert fenced is not None and fenced.rationale == "same analysis"


def test_parse_verdict_clamps_score() -> None:
    v = parse_verdict('{"equivalent": false, "score": 7, "rationale": "r"}')
    assert v is not None and v.score == 1.0


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"equivalent": "yes", "score": 0.5, "rationale": "r"}',  # non-bool
        '{"equivalent": true, "score": true, "rationale": "r"}',  # bool score
        '{"equivalent": true, "score": 0.5, "rationale": ""}',  # empty rationale
        '{"equivalent": true}',  # missing keys
        "[1, 2]",  # non-object
    ],
)
def test_parse_verdict_fails_closed(raw: str) -> None:
    assert parse_verdict(raw) is None


# ----------------------------------------------------------------------------
# the grade-case ladder (compile + judge injected; execute real)
# ----------------------------------------------------------------------------


def test_grade_case_exact_match_skips_judge(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _case()
    monkeypatch.setattr(evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec())))
    judge_calls: list[str] = []

    def exploding_judge(*a: object, **k: object) -> JudgeOutcome:
        judge_calls.append("called")
        raise AssertionError("judge must not fire on an exact match")

    result = evc.grade_case(case, db_path=db, run_id="r1", judge=exploding_judge)
    assert result.passed and result.score == 1.0
    assert result.failure_stage is None
    assert judge_calls == []
    assert result.prompt_text == "PROMPT"  # transcript captured
    assert result.response_text is not None


def test_grade_case_compile_failure(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evc,
        "compile_nl_to_viewspec",
        _compile_returning(
            NLCompileResult(
                status="error", message="nope", attempts=2, prompt_text="P", raw_response="garbage"
            )
        ),
    )
    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=None)
    assert not result.passed and result.score == 0.0
    assert result.failure_stage == "compile"
    assert result.response_text == "garbage"


def test_grade_case_budget_skip_aborts(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evc,
        "compile_nl_to_viewspec",
        _compile_returning(NLCompileResult(status="budget_skipped", message="over cap")),
    )
    with pytest.raises(EvalAbortError, match="budget"):
        evc.grade_case(_case(), db_path=db, run_id="r1", judge=None)


def test_grade_case_execute_failure(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec())))

    def boom(*a: object, **k: object) -> object:
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(evc, "execute_view", boom)
    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=None)
    assert not result.passed and result.failure_stage == "execute"
    assert result.judge_rationale is not None and "engine exploded" in result.judge_rationale


def test_grade_case_divergence_judged_equivalent(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Model compiled periods=12 where golden pins 8 — judge says equivalent.
    monkeypatch.setattr(
        evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec(periods=12)))
    )
    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=_judge_returning(True, 0.85))
    assert result.passed and result.score == 0.85
    assert result.failure_stage is None
    assert result.judge_rationale == "because"
    assert result.judge_verdict is not None


def test_grade_case_divergence_judged_not_equivalent(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec(transform="level")))
    )
    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=_judge_returning(False, 0.2))
    assert not result.passed and result.score == 0.2
    assert result.failure_stage == "mismatch"


def test_grade_case_judge_failure_fails_closed(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec(periods=12)))
    )

    def broken_judge(*a: object, **k: object) -> JudgeOutcome:
        return JudgeOutcome(verdict=None, raw="I think they look similar!", error="unparseable")

    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=broken_judge)
    assert not result.passed and result.score == 0.0
    assert result.failure_stage == "mismatch"
    assert result.judge_verdict == "I think they look similar!"  # raw preserved for audit


def test_grade_case_no_judge_divergence_fails(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec(periods=12)))
    )
    result = evc.grade_case(_case(), db_path=db, run_id="r1", judge=None)
    assert not result.passed
    assert result.judge_rationale is not None and "judge disabled" in result.judge_rationale


# ----------------------------------------------------------------------------
# end-to-end run + persistence (migration-built eval tables)
# ----------------------------------------------------------------------------


def _alembic_cfg(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _migrated_db(tmp_path: Path) -> Path:
    """Eval tables via the real migration: stamp 0082 -> upgrade 0083 (pinned,
    so later migrations are someone else's), llm_budgets hand-built in the
    post-0066 shape, prompt_calibration_scores hand-built in the 0058 shape
    (the stamp skipped both of their migrations)."""
    db_path = tmp_path / "migrated.db"
    cfg = _alembic_cfg(db_path)
    command.stamp(cfg, "0082_expected_earnings_revival")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE llm_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose VARCHAR(64) NOT NULL UNIQUE,
            monthly_cap_usd NUMERIC(10, 2) NOT NULL,
            warn_threshold_pct FLOAT NOT NULL DEFAULT 0.80,
            hard_block BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            notes TEXT,
            on_exceed TEXT NOT NULL DEFAULT 'warn'
                CHECK (on_exceed IN ('skip', 'block', 'warn'))
        );
        CREATE TABLE prompt_calibration_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose VARCHAR(64) NOT NULL,
            prompt_version VARCHAR(32) NOT NULL,
            ticker VARCHAR(16),
            score FLOAT,
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
    return db_path


def test_migration_0082_creates_tables_and_seeds_judge_budget(tmp_path: Path) -> None:
    db_path = _migrated_db(tmp_path)
    conn = sqlite3.connect(db_path)
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"eval_runs", "eval_case_results"} <= tables
    row = conn.execute(
        "SELECT monthly_cap_usd, on_exceed FROM llm_budgets WHERE purpose = 'eval_judge'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert float(row[0]) == 10.00
    assert row[1] == "warn"


def test_migration_0082_tolerates_missing_llm_budgets(tmp_path: Path) -> None:
    db_path = tmp_path / "bare.db"
    cfg = _alembic_cfg(db_path)
    command.stamp(cfg, "0082_expected_earnings_revival")
    command.upgrade(cfg, "0083_eval_runs")
    conn = sqlite3.connect(db_path)
    rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    conn.close()
    assert rev == "0083_eval_runs"


def test_run_eval_end_to_end_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _migrated_db(tmp_path)
    # Seed the engine tables so execute_view runs against something real.
    conn = sqlite3.connect(db_path)
    conn.executescript(_DDL)
    conn.commit()
    conn.close()

    golden = tmp_path / "golden.json"
    golden.write_text(
        json.dumps(
            {
                "purpose": "viewspec_compile",
                "cases": [
                    {
                        "id": "g-1",
                        "question": "NU revenue yoy, 8 quarters",
                        "context_tickers": ["NU"],
                        "expected_spec": {
                            "tickers": ["NU"],
                            "metrics": ["fin:revenue"],
                            "transform": "yoy",
                            "periods": 8,
                        },
                    },
                    {
                        "id": "g-2",
                        "question": "NU net income level",
                        "context_tickers": ["NU"],
                        "expected_spec": {
                            "tickers": ["NU"],
                            "metrics": ["fin:net_income"],
                            "transform": "level",
                            "periods": 12,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    # First case compiles to the golden spec exactly; second diverges and the
    # judge calls it non-equivalent.
    outputs = [_ok_compile(_spec()), _ok_compile(_spec(transform="level", periods=4))]

    def fake_compile_seq(*_a: object, **_k: object) -> NLCompileResult:
        return outputs.pop(0)

    monkeypatch.setattr(evc, "compile_nl_to_viewspec", fake_compile_seq)

    summary = run_viewspec_eval(
        db_path=db_path,
        golden_path=golden,
        code_root=tmp_path,  # not a git repo -> git_sha None, still valid
        judge=_judge_returning(False, 0.3),
    )
    assert summary.n_cases == 2
    assert summary.n_pass == 1
    assert summary.avg_score == pytest.approx(0.65)
    # The summary carries the registry's CURRENT version (not a literal), so a
    # registry bump stays a one-line change.
    assert summary.prompt_version == prompt_version_for("viewspec_compile")
    assert summary.golden_set_sha is not None

    run_db_id = persist_summary(summary, db_path=db_path)
    conn = sqlite3.connect(db_path)
    run_row = conn.execute(
        "SELECT purpose, mode, n_cases, n_pass, avg_score, run_id FROM eval_runs WHERE id = ?",
        (run_db_id,),
    ).fetchone()
    case_rows = conn.execute(
        "SELECT case_id, passed, score, failure_stage, prompt_text FROM eval_case_results"
        " WHERE eval_run_id = ? ORDER BY id",
        (run_db_id,),
    ).fetchall()
    bridge = conn.execute(
        "SELECT purpose, prompt_version, score, scored_by FROM prompt_calibration_scores"
    ).fetchone()
    conn.close()

    assert run_row == ("viewspec_compile", "live", 2, 1, pytest.approx(0.65), summary.run_id)
    assert [r[0] for r in case_rows] == ["g-1", "g-2"]
    assert case_rows[0][1] == 1 and case_rows[0][2] == 1.0
    assert case_rows[1][1] == 0 and case_rows[1][3] == "mismatch"
    assert case_rows[0][4] == "PROMPT"  # transcript persisted
    assert bridge is not None
    assert bridge[0] == "viewspec_compile" and bridge[1] == summary.prompt_version
    assert bridge[2] == pytest.approx(0.65)
    assert bridge[3] == "auto:eval_harness"


def test_store_missing_tables_is_loud(tmp_path: Path) -> None:
    db_path = tmp_path / "no_tables.db"
    sqlite3.connect(db_path).close()  # file exists, no schema
    summary = EvalRunSummary(
        run_id="abc123",
        purpose="viewspec_compile",
        mode="live",
        prompt_version="v1",
        model="m",
        judge_model=None,
        golden_set_sha=None,
        started_at=now_naive_utc(),
        cases=[CaseResult(case_id="c", question="q", passed=True, score=1.0)],
    )
    with pytest.raises(RuntimeError, match="0083"):
        write_run(summary, db_path=db_path)


def test_run_eval_limit(tmp_path: Path, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evc, "compile_nl_to_viewspec", _compile_returning(_ok_compile(_spec())))
    summary = run_viewspec_eval(
        db_path=db,
        golden_path=GOLDEN_PATH,  # the real checked-in set
        code_root=tmp_path,
        limit=1,
        judge=None,
    )
    assert summary.n_cases == 1


# ----------------------------------------------------------------------------
# transcript capture on the production compile path
# ----------------------------------------------------------------------------


def test_nl_compile_result_carries_transcript(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import viewspec.nl_compile as nlc

    good = (
        '{"tickers": ["NU"], "metrics": ["fin:revenue"], "transform": "yoy",'
        ' "cadence": "quarterly", "periods": 8}'
    )

    def fake_call(prompt: str, **_kw: object) -> str:
        return good

    monkeypatch.setattr(nlc, "call_llm", fake_call)
    res = nlc.compile_nl_to_viewspec("rev yoy NU", db_path=db, context_tickers=["NU"])
    assert res.status == "ok"
    assert res.prompt_text is not None and "rev yoy NU" in res.prompt_text
    assert res.raw_response == good

    # Repair path: the recorded prompt is the LAST ask (with the feedback).
    outputs = ["not json", good]
    prompts: list[str] = []

    def fake_call_repair(prompt: str, **_kw: object) -> str:
        prompts.append(prompt)
        return outputs[len(prompts) - 1]

    monkeypatch.setattr(nlc, "call_llm", fake_call_repair)
    res2 = nlc.compile_nl_to_viewspec("rev yoy NU", db_path=db, context_tickers=["NU"])
    assert res2.status == "ok" and res2.attempts == 2
    assert res2.prompt_text is not None and "rejected" in res2.prompt_text
    assert res2.raw_response == good

    # Call-exception path still records the attempted prompt.
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("CLI exploded")

    monkeypatch.setattr(nlc, "call_llm", boom)
    res3 = nlc.compile_nl_to_viewspec("rev yoy NU", db_path=db, context_tickers=["NU"])
    assert res3.status == "error"
    assert res3.prompt_text is not None and res3.raw_response is None


# ----------------------------------------------------------------------------
# registry wiring
# ----------------------------------------------------------------------------


def test_eval_judge_model_pinned_and_versioned() -> None:
    from llm.cli import FAST_CLASSIFIER_MODEL, LLM_MODELS
    from llm.prompt_versions import registered_purposes

    assert LLM_MODELS.get("eval_judge") == FAST_CLASSIFIER_MODEL
    assert "viewspec_compile" in registered_purposes()


def test_summary_json_truncates_transcripts() -> None:
    summary = EvalRunSummary(
        run_id="r",
        purpose="viewspec_compile",
        mode="live",
        prompt_version="v1",
        model="m",
        judge_model=None,
        golden_set_sha=None,
        started_at=now_naive_utc(),
        cases=[
            CaseResult(
                case_id="c",
                question="q",
                passed=True,
                score=1.0,
                prompt_text="x" * 1000,
            )
        ],
    )
    payload = summary.to_json_dict()
    cases = cast("list[dict[str, object]]", payload["cases"])
    text = cases[0]["prompt_text"]
    assert isinstance(text, str) and len(text) < 500 and "1000 chars" in text
