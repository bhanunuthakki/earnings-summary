from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution._lib import (
    PROJECT_ROOT,
    add_database_argument,
    command_parser,
    log_event,
    resolve_db_path,
)


def test_execution_lib_uses_one_project_root_and_db_resolver(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    assert Path(__file__).resolve().parents[1] == PROJECT_ROOT
    assert resolve_db_path(db_path) == db_path


def test_command_parser_and_database_argument_support_explicit_defaults(
    tmp_path: Path,
) -> None:
    parser = command_parser("test")
    add_database_argument(parser, flag="--db", default=tmp_path / "db.sqlite")
    parsed = parser.parse_args(["--db", str(tmp_path / "db.sqlite")])
    assert parsed.db == tmp_path / "db.sqlite"


def test_command_parser_leaves_db_path_none_by_default() -> None:
    parser = command_parser("test")
    add_database_argument(parser)
    parsed = parser.parse_args([])
    assert parsed.db_path is None


def test_log_event_is_structured_and_redacts_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "-".join(("synthetic", "credential"))
    log_event(
        "fetch_failed",
        url=f"https://example.test/data?apikey={marker}",
        nested={"authorization": f"Bearer {marker}"},
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["event"] == "fetch_failed"
    assert payload["url"].endswith("apikey=***")
    assert payload["nested"]["authorization"] == "Bearer ***"
