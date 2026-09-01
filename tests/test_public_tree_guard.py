import subprocess
import zipfile
from pathlib import Path

import pytest

from execution.verify_public_tree import audit_public_refs, verify


def test_public_tree_has_no_private_or_generated_material() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert verify(repo_root) == []


def _init_repo(repo_root: Path) -> None:
    subprocess.run(["git", "init", "--initial-branch=main", repo_root], check=True)
    subprocess.run(
        ["git", "-C", repo_root, "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(
        ["git", "-C", repo_root, "config", "user.name", "Public Boundary Test"], check=True
    )


def _commit(repo_root: Path, message: str) -> None:
    subprocess.run(["git", "-C", repo_root, "add", "--all"], check=True)
    subprocess.run(["git", "-C", repo_root, "commit", "-m", message], check=True)


def test_forbidden_markdown_path_is_never_exempt(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    private_note = tmp_path / "notes" / "portfolio.private.md"
    private_note.parent.mkdir()
    private_note.write_text("private operator note\n", encoding="utf-8")
    _commit(tmp_path, "add private note")

    assert verify(tmp_path) == ["forbidden tracked path: notes/portfolio.private.md"]


def test_sanitized_dcf_workbook_and_brief_paths_are_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workbook = tmp_path / "dcf" / "public-model.xlsx"
    workbook.parent.mkdir()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>Public revenue model</t></si></sst>")
    brief = tmp_path / "reports" / "public-brief.md"
    brief.parent.mkdir()
    brief.write_text("Public company research from public filings.\n", encoding="utf-8")
    _commit(tmp_path, "add sanitized research")

    assert verify(tmp_path) == []


def test_account_level_fact_in_brief_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    brief = tmp_path / "reports" / "public-brief.md"
    brief.parent.mkdir()
    brief.write_text("Cost basis: $123\n", encoding="utf-8")
    _commit(tmp_path, "add private account fact")

    assert verify(tmp_path) == ["account-level-fact in: reports/public-brief.md"]


def test_sanitized_holdings_research_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    research = tmp_path / "micro_thesis" / "holdings" / "sample.json"
    research.parent.mkdir(parents=True)
    research.write_text('{"ticker": "EXAMPLE", "thesis": "Public research"}\n', encoding="utf-8")
    _commit(tmp_path, "add sanitized holdings research")

    assert verify(tmp_path) == []


def test_public_holdings_weights_and_percentage_position_size_are_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    research = tmp_path / "micro_thesis" / "holdings" / "sample.json"
    research.parent.mkdir(parents=True)
    research.write_text('{"ticker": "X", "weight": 0.08}\n', encoding="utf-8")
    brief = tmp_path / "reports" / "public-brief.md"
    brief.parent.mkdir()
    brief.write_text("position size: 8%\n", encoding="utf-8")
    _commit(tmp_path, "add public portfolio research")

    assert verify(tmp_path) == []


@pytest.mark.parametrize(
    "private_fact",
    [
        "position value: $123",
        "position size: $123",
        "quantity: 100",
        "shares: 100",
        "cost basis: 123",
        "account balance: 123",
    ],
)
def test_private_account_facts_are_rejected(tmp_path: Path, private_fact: str) -> None:
    _init_repo(tmp_path)
    brief = tmp_path / "reports" / "public-brief.md"
    brief.parent.mkdir()
    brief.write_text(f"{private_fact}\n", encoding="utf-8")
    _commit(tmp_path, "add private account fact")

    assert verify(tmp_path) == ["account-level-fact in: reports/public-brief.md"]


def test_account_level_holdings_content_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    research = tmp_path / "micro_thesis" / "holdings" / "sample.json"
    research.parent.mkdir(parents=True)
    research.write_text('{"ticker": "EXAMPLE", "cost_basis": 123}\n', encoding="utf-8")
    _commit(tmp_path, "add account holdings content")

    assert verify(tmp_path) == ["account-level-fact in: micro_thesis/holdings/sample.json"]


def test_all_ref_audit_reports_categories_without_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "public root")
    subprocess.run(["git", "-C", tmp_path, "switch", "-c", "private-work"], check=True)
    holdings = tmp_path / "micro_thesis" / "holdings" / "sample.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text('{"account_balance": 123}\n', encoding="utf-8")
    _commit(tmp_path, "private branch")
    subprocess.run(["git", "-C", tmp_path, "switch", "main"], check=True)

    summary = audit_public_refs(tmp_path)

    assert summary == {"account-level-fact": {"files": 1, "refs": 1}}
    assert "sample.json" not in repr(summary)
