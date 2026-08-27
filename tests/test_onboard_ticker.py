# pyright: reportPrivateUsage=false
"""Tests for execution/onboard_ticker.py — the `apply_industry_template` path.

The pipeline+network stages (FMP fetch, refresh, transcript backfill) are
already covered by their own tests + are by design subprocess/IO heavy. These
tests focus on what's NEW: applying an industry template to a fresh ticker
and verifying the four outputs land:
  1. holdings JSON written with the template's tier-1 KPIs
  2. existing tier-1 KPIs preserved on re-application
  3. company entity row created in the entity spine
  4. schedule class is derived from list_type
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import onboard_ticker  # noqa: E402
from onboard_ticker import (  # noqa: E402
    _merge_tier_1_kpis,
    apply_industry_template,
)

# ---------------------------------------------------------------------------
# Test DB fixture — entity spine + tracked_companies columns onboard touches
# ---------------------------------------------------------------------------


def _create_test_db(db_path: Path) -> None:
    """Create the minimal schema the template applier needs:
    - entities + entity_aliases (for upsert_entity)
    - concepts (entity_store opens it but doesn't need rows)
    - tracked_companies with list_type
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(255) NOT NULL,
            display_name VARCHAR(255),
            external_ids TEXT,
            parent_entity_id INTEGER,
            meta_json TEXT,
            effective_from DATETIME,
            effective_to DATETIME,
            created_at DATETIME NOT NULL,
            last_observed_at DATETIME,
            UNIQUE(kind, canonical_name)
        );
        CREATE TABLE entity_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id INTEGER NOT NULL,
            alias_text VARCHAR(255) NOT NULL,
            alias_kind VARCHAR(32) NOT NULL,
            first_observed_at DATETIME,
            last_observed_at DATETIME,
            observation_count INTEGER NOT NULL DEFAULT 1,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            exemplar_source_doc_id INTEGER,
            exemplar_excerpt TEXT,
            UNIQUE(entity_id, alias_text)
        );
        CREATE TABLE concepts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind VARCHAR(32) NOT NULL,
            canonical_name VARCHAR(128) NOT NULL,
            unit_kind VARCHAR(32),
            taxonomy_xbrl_tag VARCHAR(128),
            generic_definition_md TEXT,
            computation_kind VARCHAR(32),
            computation_formula_md TEXT,
            created_at DATETIME NOT NULL,
            UNIQUE(canonical_name)
        );
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            archived_at TIMESTAMP,
            UNIQUE(user_id, ticker)
        );
        """,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    """Set up a tmp repo root with portfolio.db + empty holdings dir.

    Mirrors the prod layout: <repo>/data/portfolio.db + <repo>/micro_thesis/holdings/.
    The repo_root we pass to apply_industry_template is tmp_path; templates
    live in the *real* PROJECT_ROOT (we symlink them in via a passthrough).
    """
    holdings_dir = tmp_path / "micro_thesis" / "holdings"
    holdings_dir.mkdir(parents=True)
    db_path = tmp_path / "data" / "portfolio.db"
    _create_test_db(db_path)
    # Symlink the templates/industry dir from the real project root so the
    # loader finds the YAMLs even though repo_root=tmp_path.
    templates_link = tmp_path / "templates"
    templates_link.mkdir()
    real_industry_dir = PROJECT_ROOT / "templates" / "industry"
    industry_link = templates_link / "industry"
    industry_link.mkdir()
    for yaml_file in real_industry_dir.glob("*.yaml"):
        (industry_link / yaml_file.name).write_text(
            yaml_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return {
        "repo_root": tmp_path,
        "holdings_dir": holdings_dir,
        "db_path": db_path,
    }


# ---------------------------------------------------------------------------
# apply_industry_template — happy path
# ---------------------------------------------------------------------------


def test_apply_template_creates_holdings_json(env: dict[str, Path]) -> None:
    # Seed a tracked_companies row so the derived schedule class can be read
    conn = sqlite3.connect(str(env["db_path"]))
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
        ("CRWD", "CrowdStrike", "watchlist"),
    )
    conn.commit()
    conn.close()

    result = apply_industry_template(
        ticker="CRWD",
        industry_slug="software_saas",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )

    # Holdings JSON exists with the expected shape
    holdings_path = env["holdings_dir"] / "CRWD.json"
    assert holdings_path.exists()
    holdings = json.loads(holdings_path.read_text(encoding="utf-8"))
    assert holdings["ticker"] == "CRWD"
    assert holdings["industry_template"] == "software_saas"
    assert holdings["industry_template_display"] == "Software / SaaS"
    # Should have ALL 9 software_saas tier-1 KPIs (it was a fresh stub)
    tier_1 = holdings["tier_1_kpis"]
    assert isinstance(tier_1, list)
    assert len(tier_1) == 9
    names = {row["name"] for row in tier_1}
    assert "Net Revenue Retention" in names
    assert "CAC Payback Period" in names
    assert "Rule of 40" in names

    # Result accounting
    assert result.industry_slug == "software_saas"
    assert result.holdings_written is True
    assert len(result.kpis_added) == 9
    assert result.kpis_kept == []
    assert result.schedule_class == "P2"  # watchlist → P2


def test_apply_template_creates_entity_row(env: dict[str, Path]) -> None:
    apply_industry_template(
        ticker="CRWD",
        industry_slug="software_saas",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )
    conn = sqlite3.connect(str(env["db_path"]))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT canonical_name, display_name, external_ids, meta_json
        FROM entities
        WHERE kind='company' AND json_extract(external_ids, '$.ticker') = 'CRWD'
        """,
    ).fetchone()
    conn.close()
    assert row is not None
    # entity_seed has CRWD as "CrowdStrike Holdings, Inc." / Technology
    assert "CrowdStrike" in row["canonical_name"]
    external_ids = json.loads(row["external_ids"])
    assert external_ids == {"ticker": "CRWD"}
    meta = json.loads(row["meta_json"])
    assert meta == {"sector": "Technology"}


def test_apply_template_reads_schedule_class_portfolio(env: dict[str, Path]) -> None:
    conn = sqlite3.connect(str(env["db_path"]))
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
        ("NU", "Nu Holdings", "portfolio"),
    )
    conn.commit()
    conn.close()

    result = apply_industry_template(
        ticker="NU",
        industry_slug="bank",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )
    assert result.schedule_class == "P1"


def test_apply_template_schedule_class_for_each_list_type(env: dict[str, Path]) -> None:
    """Every list_type maps to the documented tier."""
    cases = [
        ("portfolio", "P1"),
        ("watchlist", "P2"),
        ("evaluation", "P2"),
        ("etf", "P3"),
        ("index_member", "P3"),
        ("none", "P3"),
    ]
    for list_type, expected_tier in cases:
        ticker = f"T_{list_type}".upper()
        conn = sqlite3.connect(str(env["db_path"]))
        conn.execute(
            "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
            (ticker, ticker, list_type),
        )
        conn.commit()
        conn.close()

        result = apply_industry_template(
            ticker=ticker,
            industry_slug="software_saas",
            repo_root=env["repo_root"],
            holdings_dir=env["holdings_dir"],
            db_path=env["db_path"],
        )
        assert result.schedule_class == expected_tier, (
            f"{list_type} should map to {expected_tier}, got {result.schedule_class}"
        )


def test_list_type_schedule_mapping_complete() -> None:
    """All list_types declared in db.py's CHECK constraint are classified."""
    expected = {"portfolio", "watchlist", "evaluation", "etf", "index_member", "none"}
    from models.companies import schedule_class_for_list_type

    assert {str(value) for value in expected if schedule_class_for_list_type(value)} == expected


def test_apply_template_idempotent(env: dict[str, Path]) -> None:
    """Running the apply twice should not duplicate KPIs."""
    first = apply_industry_template(
        ticker="CRWD",
        industry_slug="software_saas",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )
    second = apply_industry_template(
        ticker="CRWD",
        industry_slug="software_saas",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )
    # First run adds all 9 software_saas tier-1 KPIs to an empty stub
    assert len(first.kpis_added) == 9
    # Second run should add nothing (everything already there)
    assert second.kpis_added == []
    holdings = json.loads((env["holdings_dir"] / "CRWD.json").read_text(encoding="utf-8"))
    assert len(holdings["tier_1_kpis"]) == 9  # unchanged


def test_apply_template_preserves_existing_tier_1_kpis(env: dict[str, Path]) -> None:
    """If the user already wrote a tier_1 KPI with the template's name (or an
    alias), the existing row stays intact."""
    holdings_path = env["holdings_dir"] / "CRWD.json"
    holdings_path.write_text(
        json.dumps(
            {
                "ticker": "CRWD",
                "name": "CrowdStrike",
                "thesis": "user-authored thesis content",
                "tier_1_kpis": [
                    {
                        "name": "NDR",  # template canonical = "Net Revenue Retention"
                        "current": "120%",
                        "prior": "118%",
                        "yoy": "+2pp",
                        "status": "OK",
                        "break_condition": "below 110% for 2 Q",
                        "source": "user-authored",
                    },
                    {
                        "name": "Some Custom KPI",
                        "current": "42",
                        "prior": "40",
                        "yoy": "+5%",
                        "status": "OK",
                        "break_condition": "drift below 30",
                        "source": "user-authored",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = apply_industry_template(
        ticker="CRWD",
        industry_slug="software_saas",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )

    holdings = json.loads(holdings_path.read_text(encoding="utf-8"))
    # The user-authored thesis sticks around
    assert holdings["thesis"] == "user-authored thesis content"
    # NDR row preserved verbatim
    ndr_row = next(r for r in holdings["tier_1_kpis"] if r["name"] == "NDR")
    assert ndr_row["current"] == "120%"
    assert ndr_row["source"] == "user-authored"
    # User-only custom KPI also preserved
    assert any(r["name"] == "Some Custom KPI" for r in holdings["tier_1_kpis"])
    # "Net Revenue Retention" should NOT be added since NDR is an alias
    names = {r["name"] for r in holdings["tier_1_kpis"]}
    assert "Net Revenue Retention" not in names
    # But the other 8 template KPIs should have been added
    assert "CAC Payback Period" in names
    assert "Rule of 40" in names
    assert "NDR" in result.kpis_kept


def test_apply_template_works_without_tracked_companies_row(env: dict[str, Path]) -> None:
    """If a ticker isn't in tracked_companies yet, the template still applies
    (holdings + entity get written); schedule_class just returns None."""
    result = apply_industry_template(
        ticker="WPM",
        industry_slug="commodity_royalty",
        repo_root=env["repo_root"],
        holdings_dir=env["holdings_dir"],
        db_path=env["db_path"],
    )
    assert result.schedule_class is None
    assert result.entity_id is not None  # entity still created
    assert (env["holdings_dir"] / "WPM.json").exists()


def test_apply_template_unknown_slug_raises(env: dict[str, Path]) -> None:
    with pytest.raises(FileNotFoundError):
        apply_industry_template(
            ticker="CRWD",
            industry_slug="nonexistent_industry",
            repo_root=env["repo_root"],
            holdings_dir=env["holdings_dir"],
            db_path=env["db_path"],
        )


def test_apply_template_creates_holdings_dir_if_missing(tmp_path: Path) -> None:
    """holdings_dir doesn't have to pre-exist."""
    repo_root = tmp_path
    industry_dir = repo_root / "templates" / "industry"
    industry_dir.mkdir(parents=True)
    for yaml_file in (PROJECT_ROOT / "templates" / "industry").glob("*.yaml"):
        (industry_dir / yaml_file.name).write_text(
            yaml_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    db_path = repo_root / "data" / "portfolio.db"
    _create_test_db(db_path)
    holdings_dir = repo_root / "micro_thesis" / "holdings"  # doesn't exist yet

    result = apply_industry_template(
        ticker="NU",
        industry_slug="bank",
        repo_root=repo_root,
        holdings_dir=holdings_dir,
        db_path=db_path,
    )
    assert holdings_dir.exists()
    assert (holdings_dir / "NU.json").exists()
    assert result.holdings_written is True


# ---------------------------------------------------------------------------
# _merge_tier_1_kpis — unit test the merge logic in isolation
# ---------------------------------------------------------------------------


def test_merge_tier_1_kpis_appends_to_empty_list() -> None:
    from industry_classifier import load_template

    t = load_template("hyperscaler", PROJECT_ROOT)
    holdings: dict[str, object] = {"tier_1_kpis": []}
    added, kept = _merge_tier_1_kpis(holdings, t)
    assert len(added) == len(t.canonical_kpis)
    assert kept == []
    tier_1 = holdings["tier_1_kpis"]
    assert isinstance(tier_1, list)
    assert len(tier_1) == len(t.canonical_kpis)


def test_merge_tier_1_kpis_handles_missing_field() -> None:
    """tier_1_kpis missing entirely → treated as empty."""
    from industry_classifier import load_template

    t = load_template("hyperscaler", PROJECT_ROOT)
    holdings: dict[str, object] = {"name": "Foo"}
    added, kept = _merge_tier_1_kpis(holdings, t)
    assert len(added) == len(t.canonical_kpis)
    assert kept == []
    assert "tier_1_kpis" in holdings


def test_merge_tier_1_kpis_alias_match_is_case_insensitive() -> None:
    from industry_classifier import load_template

    t = load_template("software_saas", PROJECT_ROOT)
    holdings: dict[str, object] = {
        "tier_1_kpis": [{"name": "ndr", "current": "120%"}],  # lowercase alias
    }
    added, kept = _merge_tier_1_kpis(holdings, t)
    assert "Net Revenue Retention" not in added  # alias match suppresses dup
    assert "ndr" in kept


# ---------------------------------------------------------------------------
# Say-Do onboarding step — gating + subprocess orchestration
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_saydo_should_run_gates_to_evaluation() -> None:
    """Say-Do generation runs for evaluation names; other lists are skipped
    unless explicitly forced (the per-decision policy)."""
    assert onboard_ticker._saydo_should_run("evaluation", force=False) is True
    assert onboard_ticker._saydo_should_run("portfolio", force=False) is False
    assert onboard_ticker._saydo_should_run("watchlist", force=False) is False
    assert onboard_ticker._saydo_should_run(None, force=False) is False
    # --force-saydo overrides the gate (used for backfilling existing names).
    assert onboard_ticker._saydo_should_run("portfolio", force=True) is True
    assert onboard_ticker._saydo_should_run(None, force=True) is True


def test_lookup_list_type_reads_tracked_companies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "data" / "portfolio.db"
    _create_test_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, ?)",
        [("UBER", "Uber", "evaluation"), ("NU", "Nu", "portfolio")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(onboard_ticker, "_DB_PATH", db_path)

    assert onboard_ticker._lookup_list_type("UBER") == "evaluation"
    assert onboard_ticker._lookup_list_type("NU") == "portfolio"
    assert onboard_ticker._lookup_list_type("ZZZZ") is None


def test_lookup_list_type_missing_db_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboard_ticker, "_DB_PATH", tmp_path / "nope" / "portfolio.db")
    assert onboard_ticker._lookup_list_type("UBER") is None


def test_run_saydo_runs_summary_then_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> _FakeProc:
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(onboard_ticker.subprocess, "run", fake_run)

    rc = onboard_ticker._run_saydo("UBER")

    assert rc == 0
    assert len(calls) == 2
    # 1) process_ir_documents.py --ticker UBER --regenerate-missing
    assert Path(calls[0][1]).name == "sqlite_bootstrap.py"
    assert Path(calls[0][2]).name == "process_ir_documents.py"
    assert calls[0][3:] == ["--ticker", "UBER", "--regenerate-missing"]
    # 2) build_saydo_pairs.py --ticker UBER
    assert Path(calls[1][1]).name == "sqlite_bootstrap.py"
    assert Path(calls[1][2]).name == "build_saydo_pairs.py"
    assert calls[1][3:] == ["--ticker", "UBER"]


def test_run_saydo_short_circuits_on_summary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the summary step fails, build_saydo_pairs is not reached and the
    non-zero code propagates (best-effort caller logs + continues)."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> _FakeProc:
        calls.append(cmd)
        return _FakeProc(3)

    monkeypatch.setattr(onboard_ticker.subprocess, "run", fake_run)

    rc = onboard_ticker._run_saydo("UBER")

    assert rc == 3
    assert len(calls) == 1  # build_saydo_pairs.py not invoked
