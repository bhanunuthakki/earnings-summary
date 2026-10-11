from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from execution._lib import (
    PROJECT_ROOT,
    add_database_argument,
    command_parser,
    log_event,
    require_db_path,
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


def test_add_database_argument_can_reproduce_a_legacy_help_surface() -> None:
    """A migrating command keeps its old ``--help`` line byte-for-byte.

    ``help_text=""`` renders no help line and ``metavar=None`` restores
    argparse's ``DEST``-derived metavar (here ``DB`` for ``--db``).
    """
    legacy = argparse.ArgumentParser(prog="cmd", description="d")
    legacy.add_argument("--db", type=Path, default=None)
    shared = command_parser("d")
    shared.prog = "cmd"
    add_database_argument(shared, flag="--db", help_text="", metavar=None)
    assert shared.format_help() == legacy.format_help()


def test_command_parser_supports_the_raw_description_formatter() -> None:
    raw = command_parser(
        "line one\n    indented line two", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    assert "    indented line two" in raw.format_help()


def test_lib_reexports_require_db_path_and_refuses_the_checkout_default() -> None:
    checkout_default = PROJECT_ROOT / "data" / "portfolio.db"
    with pytest.raises(RuntimeError, match="checkout-default"):
        require_db_path(checkout_default)
    with pytest.raises(FileNotFoundError):
        require_db_path(PROJECT_ROOT / "data" / "does-not-exist-9f1c.db")


def test_migrated_commands_own_no_sys_path_or_checkout_default_derivation() -> None:
    """The M5 ``_lib``-adopted subset delegates bootstrap + DB policy to the seam."""
    adopted = (
        "execution/show_source_calls.py",
        "execution/report_workload_inventory.py",
        "execution/provenance_coverage_report.py",
        "execution/filing_sections_report.py",
        "execution/audit_transcript_evidence.py",
        "execution/expire_stale_research.py",
    )
    for rel in adopted:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "sys.path.insert" not in text, rel
        assert "PROJECT_ROOT = Path(__file__)" not in text, rel
        assert '"data" / "portfolio.db"' not in text, rel
        assert "from _lib import" in text or "from execution._lib import" in text, rel
