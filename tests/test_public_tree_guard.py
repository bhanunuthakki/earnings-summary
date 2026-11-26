import os
import subprocess
import zipfile
from pathlib import Path

import pytest

import execution.verify_public_tree as public_guard
from execution.verify_public_tree import audit_public_refs, verify


def _git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _init_repo(repo_root: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", repo_root],
        check=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-C", repo_root, "config", "user.email", "test@example.invalid"],
        check=True,
        env=_git_env(),
    )
    subprocess.run(
        ["git", "-C", repo_root, "config", "user.name", "Public Boundary Test"],
        check=True,
        env=_git_env(),
    )


def _commit(repo_root: Path, message: str) -> None:
    subprocess.run(["git", "-C", repo_root, "add", "--all"], check=True, env=_git_env())
    subprocess.run(
        ["git", "-C", repo_root, "commit", "-m", message],
        check=True,
        env=_git_env(),
    )


def test_forbidden_markdown_path_is_never_exempt(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    private_note = tmp_path / "notes" / "portfolio.private.md"
    private_note.parent.mkdir()
    private_note.write_text("private operator note\n", encoding="utf-8")
    _commit(tmp_path, "add private note")

    assert verify(tmp_path) == ["forbidden path: notes/portfolio.private.md"]


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


def test_account_fact_in_operator_doc_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    document = tmp_path / "docs" / "operator.md"
    document.parent.mkdir()
    document.write_text("account id: 7\n", encoding="utf-8")
    _commit(tmp_path, "add private account identifier")

    assert verify(tmp_path) == ["account-level-fact in: docs/operator.md"]


def test_high_confidence_secret_is_rejected_even_on_example_line(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    token = "sk-" + "abcdefghij" + "klmnopqrst" + "uvwxyz123456"
    note = tmp_path / "notes.md"
    note.write_text(f"example token: {token}\n", encoding="utf-8")
    _commit(tmp_path, "add credential fixture")

    assert verify(tmp_path) == ["credential-material in: notes.md"]


def test_placeholder_generic_credential_is_allowed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "notes.md"
    key = "api" + "_key"
    note.write_text(f'{key} = "placeholder-value-123"\n', encoding="utf-8")
    _commit(tmp_path, "add safe configuration example")

    assert verify(tmp_path) == []


def test_unquoted_generic_credential_is_rejected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "notes.md"
    key = "pass" + "word"
    value = "Ultra" + "Secret" + "Value123"
    note.write_text(f"{key}={value}\n", encoding="utf-8")
    _commit(tmp_path, "add unsafe configuration")

    assert verify(tmp_path) == ["credential-material in: notes.md"]


@pytest.mark.parametrize("suffix", [".docx", ".pdf", ".zip", ".db", ".sqlite"])
def test_unscannable_private_artifact_is_rejected(tmp_path: Path, suffix: str) -> None:
    _init_repo(tmp_path)
    artifact = tmp_path / f"artifact{suffix}"
    artifact.write_bytes(b"opaque\x00content")
    _commit(tmp_path, "add unscannable artifact")

    assert verify(tmp_path) == [f"unscannable-private-artifact in: artifact{suffix}"]


def test_all_ref_audit_reports_categories_without_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "public root")
    subprocess.run(
        ["git", "-C", tmp_path, "switch", "-c", "private-work"],
        check=True,
        env=_git_env(),
    )
    holdings = tmp_path / "micro_thesis" / "holdings" / "sample.json"
    holdings.parent.mkdir(parents=True)
    holdings.write_text('{"account_balance": 123}\n', encoding="utf-8")
    _commit(tmp_path, "private branch")
    subprocess.run(
        ["git", "-C", tmp_path, "switch", "main"],
        check=True,
        env=_git_env(),
    )

    summary = audit_public_refs(tmp_path)

    assert summary == {"account-level-fact": {"files": 1, "refs": 1}}
    assert "sample.json" not in repr(summary)


def test_all_ref_audit_classifies_repeated_blobs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "public root")
    subprocess.run(
        ["git", "-C", tmp_path, "switch", "-c", "second-branch"],
        check=True,
        env=_git_env(),
    )
    note = tmp_path / "note.md"
    note.write_text("also public\n", encoding="utf-8")
    _commit(tmp_path, "second public branch")

    calls: list[tuple[str, bytes]] = []

    def record_classification(relative: str, data: bytes) -> set[str]:
        calls.append((relative, data))
        return set()

    monkeypatch.setattr(public_guard, "content_violation_categories", record_classification)

    assert audit_public_refs(tmp_path) == {}
    assert calls.count(("README.md", b"public\n")) == 1


def test_deleted_tracked_files_are_ignored_but_untracked_files_are_scanned(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    tracked = tmp_path / "notes" / "tracked.md"
    tracked.parent.mkdir()
    tracked.write_text("account id: 7\n", encoding="utf-8")
    _commit(tmp_path, "track private note")
    tracked.unlink()

    untracked = tmp_path / "notes" / "untracked.md"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("account id: 8\n", encoding="utf-8")

    assert verify(tmp_path) == ["account-level-fact in: notes/untracked.md"]


def test_outgoing_history_rejects_private_material_removed_from_final_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "public base")
    base = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"], text=True, env=_git_env()
    ).strip()
    note.write_text("account balance: 123\n", encoding="utf-8")
    _commit(tmp_path, "private intermediate")
    note.write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "remove private material")

    assert verify(tmp_path) == []
    assert public_guard.audit_outgoing_history(tmp_path, base) == {
        "account-level-fact": {"files": 1, "refs": 1}
    }


def test_outgoing_history_requires_a_valid_base(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("public\n", encoding="utf-8")
    _commit(tmp_path, "public root")
    with pytest.raises(subprocess.CalledProcessError):
        public_guard.audit_outgoing_history(tmp_path, "missing-base")


def test_push_scan_reuses_only_identical_path_and_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "unchanged.md").write_text("Public research.\n", encoding="utf-8")
    _commit(tmp_path, "baseline")
    base = subprocess.check_output(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"], text=True, env=_git_env()
    ).strip()
    removed = reports / "removed.md"
    removed.write_text("Cost basis: $123\n", encoding="utf-8")
    changed = reports / "changed.md"
    changed.write_text("Cost basis: $123\n", encoding="utf-8")
    _commit(tmp_path, "transient private fact")
    removed.unlink()
    _commit(tmp_path, "remove private fact")
    changed.write_text("Public research.\n", encoding="utf-8")
    (reports / "untracked.md").write_text("Cost basis: $123\n", encoding="utf-8")
    fixtures = tmp_path / "tests"
    fixtures.mkdir()
    (fixtures / "synthetic.md").write_text("Cost basis: $123\n", encoding="utf-8")
    expected = (verify(tmp_path), public_guard.audit_outgoing_history(tmp_path, base))
    original = public_guard.content_violation_categories
    calls: list[tuple[str, bytes]] = []

    def classify(relative: str, data: bytes) -> set[str]:
        calls.append((relative, data))
        return original(relative, data)

    monkeypatch.setattr(public_guard, "content_violation_categories", classify)
    assert public_guard.verify_push_candidate(tmp_path, base) == expected
    assert calls.count(("reports/unchanged.md", b"Public research.\n")) == 1
    assert expected[0] == ["account-level-fact in: reports/untracked.md"]
    assert expected[1] == {"account-level-fact": {"files": 3, "refs": 2}}


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-" + "a" * 24,
        "sk-or-" + "b" * 24,
        "sk_live_" + "c" * 20,
        "rk_live_" + "d" * 20,
        "whsec_" + "e" * 20,
        "lin_api_" + "f" * 24,
        "123456789:" + "g" * 35,
    ],
)
def test_extended_provider_and_payment_tokens_are_rejected(tmp_path: Path, token: str) -> None:
    _init_repo(tmp_path)
    note = tmp_path / "notes.md"
    note.write_text(f"leaked: {token}\n", encoding="utf-8")
    _commit(tmp_path, "add leaked token")

    assert verify(tmp_path) == ["credential-material in: notes.md"]


def test_luhn_payment_card_rejected_while_non_luhn_number_ignored(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    card_file = tmp_path / "notes" / "card.md"
    card_file.parent.mkdir()
    card_file.write_text("card 4111 1111 1111 1111\n", encoding="utf-8")
    ok_file = tmp_path / "notes" / "table.md"
    ok_file.write_text("id 4111-1111-1111-1112\n", encoding="utf-8")
    _commit(tmp_path, "add card and table")

    assert verify(tmp_path) == ["payment-card in: notes/card.md"]
