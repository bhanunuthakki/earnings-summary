"""Discovery commands and build children use the selected database authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import build_investment_decision_card as card_cli
import discovery_build
import pytest

import db
from ask.context import ContextPack
from ask.engine import AskTurn, respond_turn, run_chat_command
from discovery.store import list_candidates, upsert_candidate
from dispatch_registry import Registry


@pytest.fixture
def authorities(tmp_path: Path, migrated_db: Callable[..., Path]) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    decoy = migrated_db(root / "data" / "portfolio.db")
    canonical = migrated_db(tmp_path / "state" / "authority.db")
    for database, ticker in ((decoy, "DECOY"), (canonical, "AUTH")):
        upsert_candidate(ticker=ticker, name=ticker, score=4, evidence=[], db_path=database)
        conn = sqlite3.connect(database)
        try:
            conn.execute(
                "INSERT INTO tracked_companies(user_id,ticker,name,list_type) VALUES (?,?,?,?)",
                ("bhanu", ticker, ticker, "index_member"),
            )
            conn.commit()
        finally:
            conn.close()
    return root, canonical


def test_discovery_request_reads_explicit_authority_with_checkout_decoy_present(
    authorities: tuple[Path, Path],
) -> None:
    root, canonical = authorities
    events = list(
        respond_turn(
            AskTurn(text="/discovery list"),
            ContextPack(scope="portfolio", default_tickers=["AUTH"], system_context=""),
            db_path=canonical,
            repo_root=root,
            registry=Registry(),
        )
    )
    assert events[-1]["route"] == "command"
    assert "AUTH" in str(events[-1]["text"])
    assert "DECOY" not in str(events[-1]["text"])
    reply = run_chat_command(root, "/discovery queue AUTH", Registry(), db_path=canonical)
    assert reply == "AUTH -> queued."
    assert list_candidates(db_path=canonical)[0].status == "queued"
    assert list_candidates(db_path=root / "data" / "portfolio.db")[0].status == "new"


def test_missing_configured_authority_never_reads_checkout_decoy(
    authorities: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, canonical = authorities
    monkeypatch.setattr(db, "DB_PATH", str(canonical.with_name("missing.db")))
    reply = run_chat_command(root, "/discovery list", Registry())
    assert reply is not None and "configured portfolio database is unavailable" in reply
    assert "DECOY" not in reply
    assert not canonical.with_name("missing.db").exists()


def test_discovery_build_forwards_database_and_state_root_to_every_child(
    authorities: tuple[Path, Path],
) -> None:
    root, canonical = authorities
    commands: list[list[str]] = []

    def run(argv: list[str]) -> int:
        commands.append(argv)
        return 0

    assert discovery_build.build_one("AUTH", repo_root=root, db_path=canonical, runner=run)
    assert len(commands) == 3
    for command, flag in zip(commands, ("--db", "--db-path", "--db-path"), strict=True):
        assert command[command.index(flag) + 1] == str(canonical)
    assert commands[0][commands[0].index("--project-root") + 1] == str(root)
    assert list_candidates(db_path=root / "data" / "portfolio.db")[0].status == "new"


def test_discovery_build_refuses_missing_authority_before_any_child(
    authorities: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, canonical = authorities
    missing = canonical.with_name("missing.db")
    monkeypatch.setattr(db, "DB_PATH", str(missing))
    commands: list[list[str]] = []

    def run(argv: list[str]) -> int:
        commands.append(argv)
        return 0

    with pytest.raises(FileNotFoundError):
        discovery_build.build_one("DECOY", repo_root=root, runner=run)
    assert discovery_build.main(["--tickers", "DECOY", "--repo-root", str(root)]) == 2
    assert not commands
    assert not missing.exists()


def test_card_rerender_uses_selected_read_connection_without_repointing_globals(
    authorities: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, canonical = authorities
    connections: list[sqlite3.Connection] = []
    original_global = db.DB_PATH
    sentinel = object()

    def build(*, conn: sqlite3.Connection, db_path: Path, **_kwargs: object) -> object:
        assert db_path == canonical
        assert conn.execute("SELECT ticker FROM tracked_companies").fetchone()[0] == "AUTH"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM tracked_companies")
        connections.append(conn)
        return sentinel

    def render(spec: object) -> str:
        assert spec is sentinel
        return "<html>canonical source</html>"

    monkeypatch.setattr(card_cli, "build_report", build)
    monkeypatch.setattr(card_cli, "render_workspace_html", render)
    path = card_cli.rerender_workspace("AUTH", root, db_path=canonical)
    assert path.is_relative_to(root / "output")
    assert path.read_text() == "<html>canonical source</html>"
    assert original_global == db.DB_PATH
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
