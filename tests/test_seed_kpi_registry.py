"""End-to-end tests for scratch/seed_kpi_registry.py.

Mocks ``seed_kpi_registry.call_llm`` so the suite runs without network
access. Covers both modes:

  --propose:
    * happy path → YAML written with 3 entries, status=proposed
    * no thesis anchor → skip, exit 0, no YAML
    * no 10-K → skip, exit 0, no YAML
    * --all walks portfolio tickers and writes one YAML per ticker
    * malformed JSON twice → SeederLLMError

  --write:
    * accepted vs. rejected vs. proposed → only accepted rows land
    * --dry-run logs but writes nothing
    * idempotency: running --write twice upserts (no crash)
    * round-trip: --propose writes YAML, hand-mark accepted, --write persists
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from alembic.config import Config

from alembic import command

# scratch/ lives at <repo_root>/scratch/. Tests live at <repo_root>/tests/.
# Insert scratch/ on sys.path so the import below resolves at runtime; pyright
# resolves it via extraPaths in pyproject.toml.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scratch"))

from seed_kpi_registry import (  # noqa: E402
    Proposal,
    SeederLLMError,
    WriteSummary,
    propose_and_write_yaml,
    render_yaml,
    write_from_yaml,
)

from user_state import registry  # noqa: E402

PRIOR_HEAD = "0059_kpi_facts_restatement"


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


def _build_alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _add_inputs_schema(db_path: Path) -> None:
    """Add the kpi_facts / kpi_definitions / tracked_companies tables the
    seeder reads from. Alembic stamped at 0059 doesn't create them
    deterministically (kpi_facts predates the stamp), so we add a minimal
    schema by hand."""
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(
            "CREATE TABLE IF NOT EXISTS kpi_definitions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticker TEXT, "
            "name TEXT NOT NULL"
            ")"
        )
        _ = conn.execute(
            "CREATE TABLE IF NOT EXISTS kpi_facts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticker TEXT NOT NULL, "
            "period_end TEXT NOT NULL, "
            "fiscal_period_type TEXT NOT NULL, "
            "kpi_definition_id INTEGER NOT NULL, "
            "value REAL NOT NULL, "
            "unit TEXT"
            ")"
        )
        _ = conn.execute(
            "CREATE TABLE IF NOT EXISTS tracked_companies ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticker TEXT NOT NULL, "
            "list_type TEXT"
            ")"
        )
        conn.commit()
    finally:
        conn.close()


def _seed_kpi_facts(
    db_path: Path,
    *,
    ticker: str,
    rows: list[tuple[str, str, str, float, str]],
) -> None:
    """Seed kpi_facts via kpi_definitions FK.

    `rows` is a list of (kpi_name, period_end, fiscal_period_type, value, unit).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        for name, period_end, fpt, value, unit in rows:
            cur = conn.execute(
                "INSERT INTO kpi_definitions (ticker, name) VALUES (?, ?)",
                (ticker, name),
            )
            kdef_id = int(cur.lastrowid or 0)
            _ = conn.execute(
                "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
                "kpi_definition_id, value, unit) VALUES (?, ?, ?, ?, ?, ?)",
                (ticker, period_end, fpt, kdef_id, value, unit),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_portfolio(db_path: Path, tickers: list[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for t in tickers:
            _ = conn.execute(
                "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)",
                (t, "portfolio"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixture filesystem layout
# ---------------------------------------------------------------------------


def _write_holdings_json(
    repo_root: Path, ticker: str, *, thesis: str = "Thesis text."
) -> Path:
    """Write a minimal holdings JSON so load_thesis_anchor returns non-empty."""
    holdings_dir = repo_root / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True, exist_ok=True)
    path = holdings_dir / f"{ticker.upper()}.json"
    payload: dict[str, object] = {
        "thesis": thesis,
        "key_driver": "Customer growth and ARPAC trajectory",
        "tier_1_kpis": [
            {"name": "Total customers (millions)", "break_condition": "<100M"}
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_10k_json(
    fmp_dir: Path,
    ticker: str,
    *,
    year: int = 2024,
    extra_sections: dict[str, object] | None = None,
) -> Path:
    """Write a minimal FMP-style 10-K JSON with a Risk-Factors-equivalent section."""
    fmp_dir.mkdir(parents=True, exist_ok=True)
    path = fmp_dir / f"{ticker.upper()}_form_10k_{year}.json"
    payload: dict[str, object] = {
        "symbol": ticker.upper(),
        "period": "FY",
        "year": str(year),
        # Narrative section the seeder will pick up
        "Management of financial risks": [
            {
                "Overview": [
                    "The Group monitors all material risks. Credit risk is the "
                    "primary risk to net interest margin."
                ]
            },
            {
                "Liquidity": [
                    "Liquidity is monitored at the consolidated level; a 20% "
                    "concentration breach would force model revision."
                ]
            },
        ],
        # Financial statement that should be FILTERED OUT
        "CONSOLIDATED BALANCE SHEETS": [
            {"Assets": ["Cash 1234", "Receivables 5678"]}
        ],
    }
    if extra_sections:
        payload.update(extra_sections)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """tmp_path SQLite stamped at 0059, upgraded to head, plus the
    seeder-input tables (kpi_facts, kpi_definitions, tracked_companies)."""
    path = tmp_path / "portfolio.db"
    cfg = _build_alembic_config(path)
    command.stamp(cfg, PRIOR_HEAD)
    command.upgrade(cfg, "head")
    _add_inputs_schema(path)
    return path


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """tmp_path-rooted directory standing in for the project repo."""
    (tmp_path / "micro_thesis" / "holdings").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def fmp_dir(tmp_path: Path) -> Path:
    p = tmp_path / "data" / "historical" / "fmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# LLM mock
# ---------------------------------------------------------------------------


class _StatefulLLM:
    """Mirrors the pattern from tests/test_trigger_earnings_tone.py.

    Cycles through canned responses; the last response is reused once the
    queue runs out so an accidental over-call is visible via call_count
    without triggering an IndexError.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.call_count = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **_kwargs: object) -> str:
        self.prompts.append(prompt)
        idx = min(self.call_count, len(self.responses) - 1)
        self.call_count += 1
        return self.responses[idx]


def _canned_response(
    proposals: list[dict[str, object]] | None = None,
) -> str:
    """Build the canned JSON-array response."""
    if proposals is None:
        proposals = [
            {
                "kpi_name": "Net Interest Margin",
                "threshold_direction": "below",
                "threshold_value": 17.0,
                "is_thesis_breaker": False,
                "rationale": (
                    "Recent observations show NIM trending down.\n"
                    "The 10-K Management of financial risks section flags "
                    "funding cost pressure."
                ),
            },
            {
                "kpi_name": "Total customers (millions)",
                "threshold_direction": "below",
                "threshold_value": 100.0,
                "is_thesis_breaker": True,
                "rationale": (
                    "Thesis anchor names <100M as the customer-floor break."
                ),
            },
            {
                "kpi_name": "ROE (annualized, consolidated)",
                "threshold_direction": None,
                "threshold_value": None,
                "is_thesis_breaker": False,
                "rationale": (
                    "Informational — track ROE but no specific break level."
                ),
            },
        ]
    return json.dumps(proposals)


# ---------------------------------------------------------------------------
# --propose tests
# ---------------------------------------------------------------------------


def test_propose_happy_path_writes_yaml_with_three_entries(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """3 LLM proposals → YAML lands with status: proposed on every row."""
    _ = _write_holdings_json(repo_root, "NU")
    _ = _write_10k_json(fmp_dir, "NU", year=2024)
    _seed_kpi_facts(
        db_path,
        ticker="NU",
        rows=[
            ("Net Interest Margin", "2025-12-31", "Q4", 18.2, "percent"),
            ("Net Interest Margin", "2025-09-30", "Q3", 18.5, "percent"),
            ("Total customers (millions)", "2025-12-31", "Q4", 114.0, "count"),
        ],
    )
    mock = _StatefulLLM([_canned_response()])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out = tmp_path / "proposals" / "NU.yaml"
    wrote = propose_and_write_yaml(
        ticker="NU",
        out=out,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
    )
    assert wrote is True
    assert out.exists()
    assert mock.call_count == 1

    parsed = cast(
        "dict[str, object]", yaml.safe_load(out.read_text(encoding="utf-8"))
    )
    assert parsed["ticker"] == "NU"
    assert isinstance(parsed["proposals"], list)
    proposals = cast("list[dict[str, object]]", parsed["proposals"])
    assert len(proposals) == 3
    for p in proposals:
        assert p["status"] == "proposed"
        rationale = p["rationale"]
        assert isinstance(rationale, str)
        assert rationale.strip() != ""
        assert isinstance(p["notes"], str)
        assert "kpi_name" in p
    # Specific rows the LLM proposed survived round-trip
    by_name = {cast("str", p["kpi_name"]): p for p in proposals}
    assert by_name["Total customers (millions)"]["is_thesis_breaker"] is True
    assert by_name["Net Interest Margin"]["threshold_direction"] == "below"
    assert by_name["Net Interest Margin"]["threshold_value"] == 17.0
    assert by_name["ROE (annualized, consolidated)"]["threshold_value"] is None


def test_propose_no_thesis_anchor_skips_and_writes_nothing(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No holdings JSON → load_thesis_anchor returns "" → tool returns False."""
    _ = _write_10k_json(fmp_dir, "MELI", year=2024)
    # NO holdings JSON for MELI → no thesis anchor
    mock = _StatefulLLM([_canned_response()])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out = tmp_path / "proposals" / "MELI.yaml"
    wrote = propose_and_write_yaml(
        ticker="MELI",
        out=out,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
    )
    assert wrote is False
    assert not out.exists()
    # LLM was never called — the precondition gate fired before _call_llm_with_retry
    assert mock.call_count == 0


def test_propose_no_10k_skips_and_writes_nothing(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Holdings JSON present, 10-K missing → skip."""
    _ = _write_holdings_json(repo_root, "WIX")
    # NO 10-K for WIX
    mock = _StatefulLLM([_canned_response()])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out = tmp_path / "proposals" / "WIX.yaml"
    wrote = propose_and_write_yaml(
        ticker="WIX",
        out=out,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
    )
    assert wrote is False
    assert not out.exists()
    assert mock.call_count == 0


def test_propose_all_iterates_portfolio_tickers(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--all writes one YAML per portfolio ticker that has thesis + 10-K."""
    _seed_portfolio(db_path, ["NU", "MELI"])
    _ = _write_holdings_json(repo_root, "NU")
    _ = _write_10k_json(fmp_dir, "NU", year=2024)
    _ = _write_holdings_json(repo_root, "MELI")
    _ = _write_10k_json(fmp_dir, "MELI", year=2024)

    mock = _StatefulLLM([_canned_response()])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out_dir = tmp_path / "all_proposals"
    for t in ["NU", "MELI"]:
        _ = propose_and_write_yaml(
            ticker=t,
            out=out_dir / f"{t}.yaml",
            db_path=db_path,
            fmp_dir=fmp_dir,
            repo_root=repo_root,
        )

    assert (out_dir / "NU.yaml").exists()
    assert (out_dir / "MELI.yaml").exists()
    assert mock.call_count == 2


def test_propose_raises_when_llm_returns_malformed_json_twice(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two malformed responses → SeederLLMError surfaces."""
    _ = _write_holdings_json(repo_root, "NU")
    _ = _write_10k_json(fmp_dir, "NU", year=2024)
    mock = _StatefulLLM(["not json", "still not json"])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out = tmp_path / "proposals" / "NU.yaml"
    with pytest.raises(SeederLLMError):
        _ = propose_and_write_yaml(
            ticker="NU",
            out=out,
            db_path=db_path,
            fmp_dir=fmp_dir,
            repo_root=repo_root,
        )
    assert mock.call_count == 2
    assert not out.exists()


def test_propose_retries_once_on_malformed_json(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """First response bad → retry succeeds → YAML lands."""
    _ = _write_holdings_json(repo_root, "NU")
    _ = _write_10k_json(fmp_dir, "NU", year=2024)
    mock = _StatefulLLM(["not json", _canned_response()])
    monkeypatch.setattr("seed_kpi_registry.call_llm", mock)

    out = tmp_path / "proposals" / "NU.yaml"
    wrote = propose_and_write_yaml(
        ticker="NU",
        out=out,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
    )
    assert wrote is True
    assert mock.call_count == 2
    # Retry preamble was prepended on the second call
    assert "previous response was not valid JSON" in mock.prompts[1]


# ---------------------------------------------------------------------------
# --write tests
# ---------------------------------------------------------------------------


def _write_test_yaml(
    path: Path,
    *,
    ticker: str,
    entries: list[dict[str, object]],
) -> None:
    payload = {
        "ticker": ticker,
        "generated_at": "2026-05-28T00:00:00Z",
        "model": "claude-sonnet-4-6",
        "proposals": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def test_write_happy_path_persists_accepted_skips_rejected(
    db_path: Path, tmp_path: Path
) -> None:
    """2 accepted + 1 rejected → 2 rows written, 1 skipped."""
    yaml_path = tmp_path / "proposals" / "NU.yaml"
    _write_test_yaml(
        yaml_path,
        ticker="NU",
        entries=[
            {
                "kpi_name": "Net Interest Margin",
                "threshold_direction": "below",
                "threshold_value": 17.0,
                "is_thesis_breaker": False,
                "rationale": "NIM compression risk per Risk Factors.",
                "status": "accepted",
                "notes": "",
            },
            {
                "kpi_name": "Total customers (millions)",
                "threshold_direction": "below",
                "threshold_value": 100.0,
                "is_thesis_breaker": True,
                "rationale": "Thesis names 100M floor.",
                "status": "accepted",
                "notes": "",
            },
            {
                "kpi_name": "Cost-to-serve",
                "threshold_direction": None,
                "threshold_value": None,
                "is_thesis_breaker": False,
                "rationale": "Not load-bearing — skip.",
                "status": "rejected",
                "notes": "user judged too noisy",
            },
        ],
    )

    summary: WriteSummary = write_from_yaml(yaml_path, db_path=db_path)

    assert summary.accepted == 2
    assert summary.written == 2
    assert summary.skipped == 1
    assert summary.errors == 0

    rows = registry.list_kpis(ticker="NU", db_path=db_path)
    assert len(rows) == 2
    names = sorted(r.kpi_name for r in rows)
    assert names == ["Net Interest Margin", "Total customers (millions)"]

    breakers = registry.get_thesis_breakers(ticker="NU", db_path=db_path)
    assert {r.kpi_name for r in breakers} == {"Total customers (millions)"}

    nim_row = next(r for r in rows if r.kpi_name == "Net Interest Margin")
    assert nim_row.threshold_direction == "below"
    assert nim_row.threshold_value == 17.0
    assert nim_row.scaffold_source == "llm_proposal"
    assert nim_row.notes == "NIM compression risk per Risk Factors."


def test_write_dry_run_does_not_persist(db_path: Path, tmp_path: Path) -> None:
    """--dry-run logs but the registry stays empty."""
    yaml_path = tmp_path / "NU.yaml"
    _write_test_yaml(
        yaml_path,
        ticker="NU",
        entries=[
            {
                "kpi_name": "Risk-adjusted NIM",
                "threshold_direction": "below",
                "threshold_value": 9.0,
                "is_thesis_breaker": True,
                "rationale": "Below 9% would force re-rating.",
                "status": "accepted",
                "notes": "",
            },
        ],
    )

    summary = write_from_yaml(yaml_path, db_path=db_path, dry_run=True)
    assert summary.accepted == 1
    assert summary.written == 0
    assert summary.skipped == 0
    assert summary.errors == 0

    rows = registry.list_kpis(ticker="NU", db_path=db_path)
    assert rows == []


def test_write_is_idempotent_when_run_twice(db_path: Path, tmp_path: Path) -> None:
    """Running --write twice on the same YAML upserts rather than duplicates."""
    yaml_path = tmp_path / "NU.yaml"
    _write_test_yaml(
        yaml_path,
        ticker="NU",
        entries=[
            {
                "kpi_name": "Net Interest Margin",
                "threshold_direction": "below",
                "threshold_value": 17.0,
                "is_thesis_breaker": False,
                "rationale": "first run",
                "status": "accepted",
                "notes": "",
            },
        ],
    )
    first = write_from_yaml(yaml_path, db_path=db_path)
    assert first.written == 1
    rows_after_first = registry.list_kpis(ticker="NU", db_path=db_path)
    assert len(rows_after_first) == 1
    first_id = rows_after_first[0].id

    # Re-write a second time — same natural key (user_id, ticker, kpi_name)
    second = write_from_yaml(yaml_path, db_path=db_path)
    assert second.written == 1
    rows_after_second = registry.list_kpis(ticker="NU", db_path=db_path)
    assert len(rows_after_second) == 1
    assert rows_after_second[0].id == first_id


def test_yaml_round_trip_propose_then_mark_accepted_then_write(
    db_path: Path,
    repo_root: Path,
    fmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--propose writes YAML → user hand-marks accepted → --write persists exactly those."""
    _ = _write_holdings_json(repo_root, "NU")
    _ = _write_10k_json(fmp_dir, "NU", year=2024)
    monkeypatch.setattr("seed_kpi_registry.call_llm", _StatefulLLM([_canned_response()]))

    yaml_path = tmp_path / "proposals" / "NU.yaml"
    assert propose_and_write_yaml(
        ticker="NU",
        out=yaml_path,
        db_path=db_path,
        fmp_dir=fmp_dir,
        repo_root=repo_root,
    )

    # Simulate the user editing the YAML: accept 2 of 3
    parsed = cast(
        "dict[str, object]", yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    )
    proposals = cast("list[dict[str, object]]", parsed["proposals"])
    for p in proposals:
        if p["kpi_name"] in (
            "Net Interest Margin",
            "Total customers (millions)",
        ):
            p["status"] = "accepted"
    yaml_path.write_text(
        yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    summary = write_from_yaml(yaml_path, db_path=db_path)
    assert summary.accepted == 2
    assert summary.written == 2
    assert summary.skipped == 1

    rows = registry.list_kpis(ticker="NU", db_path=db_path)
    assert {r.kpi_name for r in rows} == {
        "Net Interest Margin",
        "Total customers (millions)",
    }


# ---------------------------------------------------------------------------
# YAML rendering coverage
# ---------------------------------------------------------------------------


def test_render_yaml_uses_literal_block_for_multiline_strings() -> None:
    """Multi-line rationale becomes a `|` literal block so the YAML stays
    readable when the user opens it for review."""
    proposals = [
        Proposal(
            kpi_name="NIM",
            threshold_direction="below",
            threshold_value=17.0,
            is_thesis_breaker=False,
            rationale="line one\nline two\nline three",
        ),
    ]
    text = render_yaml(
        ticker="NU",
        model="claude-sonnet-4-6",
        proposals=proposals,
        generated_at="2026-05-28T00:00:00Z",
    )
    # The literal-block indicator `|` must appear for the multi-line rationale.
    assert "rationale: |" in text or "rationale: |-" in text
    # And it should round-trip through safe_load.
    parsed = cast("dict[str, object]", yaml.safe_load(text))
    rendered = cast("list[dict[str, object]]", parsed["proposals"])
    assert rendered[0]["rationale"] == "line one\nline two\nline three"
