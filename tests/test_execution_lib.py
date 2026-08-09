from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution._lib import PROJECT_ROOT, log_event, resolve_db_path, standard_parser


def test_execution_lib_uses_one_project_root_and_db_resolver(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    assert Path(__file__).resolve().parents[1] == PROJECT_ROOT
    assert resolve_db_path(db_path) == db_path


def test_standard_parser_exposes_shared_typed_arguments(tmp_path: Path) -> None:
    parser = standard_parser("test", ticker=True, force=True, mutation_mode=True)
    parsed = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(tmp_path / "db.sqlite"),
            "--ticker",
            "META",
            "--force",
            "--dry-run",
        ]
    )
    assert parsed.repo_root == tmp_path
    assert parsed.db_path == tmp_path / "db.sqlite"
    assert parsed.ticker == "META"
    assert parsed.force is True
    assert parsed.dry_run is True
    assert parsed.apply is False


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
