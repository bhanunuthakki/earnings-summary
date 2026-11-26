"""Keep direct migration builders visible without depending on call spelling.

This is an inventory ratchet, not a runtime benchmark. Historical one-step
migration tests legitimately invoke upgrade; application fixtures should use
migrated_db. The existing AST scanner resolves imports and aliases; unresolved dynamic upgrade
calls remain visible as candidates rather than disappearing from the inventory.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path

import pytest

from quality.test_db_invocations import collect_invocations

TESTS_DIR = Path(__file__).resolve().parent
ALEMBIC_VERSIONS_DIR = TESTS_DIR.parent / "alembic" / "versions"
# Resolved and unresolved AST upgrade candidates; old substring count was 109.
# Two explicit historical one-step controls were added for 0059 and 0061.
# They use cached predecessor fixtures; application fixtures still use migrated_db.
_MAX_DIRECT_CHAIN_BUILDERS = 48
# Historical migrations at or before 0071 are immutable and grandfathered.
# Any new migration (> 0071) must scope PRAGMA foreign_key_check to the rebuilt
# table(s) unless explicitly allowlisted below with a written justification.
_HISTORICAL_BARE_FK_CHECK_MAX_REVISION = 71
_ALLOWED_BARE_FK_CHECK_MIGRATIONS: dict[str, str] = {}
_MIGRATION_PREFIX_RE = re.compile(r"^(\d+)_")
_BARE_FOREIGN_KEY_CHECK_RE = re.compile(
    r"\bPRAGMA\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?foreign_key_check\b(?!\s*\(\s*[^)\s])",
    re.IGNORECASE,
)


def _calls_upgrade(source: str, path: str) -> bool:
    invocations = collect_invocations(
        path, ast.parse(source, filename=path), hashlib.sha256(source.encode()).hexdigest()
    )
    return any(call.evidence == "call:upgrade" for call in invocations)


@cache
def _direct_chain_builders() -> tuple[str, ...]:
    hits: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        # All supported import/getattr spellings retain the package name.
        if "alembic" in source and _calls_upgrade(source, str(path)):
            hits.append(path.name)
    return tuple(hits)


def test_no_new_direct_migration_builders() -> None:
    builders = _direct_chain_builders()
    assert len(builders) <= _MAX_DIRECT_CHAIN_BUILDERS, (
        f"{len(builders)} files invoke Alembic upgrade; limit {_MAX_DIRECT_CHAIN_BUILDERS}. "
        "Use the cached migrated_db fixture for current-schema application tests. "
        "For historical migration behavior, keep an explicit historical target."
    )


@pytest.mark.parametrize(
    "name,revision",
    [
        ("test_scoped_research_universe_migration.py", "0059_scoped_research_universe_commitments"),
        ("test_corpus_fts_bulk_validation.py", "0061_bulk_lexical_projection_coverage"),
    ],
)
def test_new_migration_controls_keep_explicit_historical_targets(name: str, revision: str) -> None:
    source = (TESTS_DIR / name).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=name)
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert constants["CURRENT"] == revision
    invocations = collect_invocations(name, tree, hashlib.sha256(source.encode()).hexdigest())
    calls = {
        (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    upgrades = [item for item in invocations if item.evidence == "call:upgrade"]
    assert upgrades
    for item in upgrades:
        call = calls[
            (
                item.locator.start_line,
                item.locator.start_col,
                item.locator.end_line,
                item.locator.end_col,
            )
        ]
        assert len(call.args) == 2
        assert isinstance(call.args[1], ast.Name) and call.args[1].id == "CURRENT"


def test_the_ratchet_is_not_slack() -> None:
    actual = len(_direct_chain_builders())
    assert _MAX_DIRECT_CHAIN_BUILDERS - actual <= 1, (
        f"lower migration-builder inventory limit from {_MAX_DIRECT_CHAIN_BUILDERS} to {actual}"
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        ("from alembic import command as migrations\nmigrations.upgrade(cfg, 'head')", True),
        ("from alembic.command import upgrade as advance\nadvance(cfg, '0003')", True),
        (
            "from alembic import command\nadvance = getattr(command, 'upgrade')\nadvance(cfg, 'head')",
            True,
        ),
        ("# command.upgrade(cfg, 'head')\ntext = 'alembic.command.upgrade'", False),
    ],
)
def test_upgrade_inventory_resolves_calls_not_comments(source: str, expected: bool) -> None:
    assert _calls_upgrade(source, "tests/test_inventory_fixture.py") is expected


def _migration_revision_number(filename: str) -> int | None:
    match = _MIGRATION_PREFIX_RE.match(filename)
    return int(match.group(1)) if match is not None else None


def _docstring_expr_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            ids.add(id(node.body[0].value))
    return ids


def _string_expression_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{expr}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_expression_text(node.left)
        right = _string_expression_text(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _bare_foreign_key_check_lines(source: str, filename: str) -> tuple[int, ...]:
    tree = ast.parse(source, filename=filename)
    docstrings = _docstring_expr_ids(tree)
    hits: set[int] = set()

    def _visit(node: ast.AST) -> None:
        if id(node) in docstrings:
            return
        text = _string_expression_text(node)
        if text is not None:
            if _BARE_FOREIGN_KEY_CHECK_RE.search(text):
                lineno = getattr(node, "lineno", None)
                if isinstance(lineno, int):
                    hits.add(lineno)
            # Do not re-visit folded sub-expressions of a JoinedStr or BinOp string.
            return
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(tree)
    return tuple(sorted(hits))


def _find_disallowed_bare_foreign_key_checks(
    versions_dir: Path,
    *,
    allowlist: Mapping[str, str] = _ALLOWED_BARE_FK_CHECK_MIGRATIONS,
) -> list[tuple[str, tuple[int, ...]]]:
    violations: list[tuple[str, tuple[int, ...]]] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        revision = _migration_revision_number(path.name)
        if revision is not None and revision <= _HISTORICAL_BARE_FK_CHECK_MAX_REVISION:
            continue
        justification = allowlist.get(path.name, "").strip()
        if justification:
            continue
        lines = _bare_foreign_key_check_lines(path.read_text(encoding="utf-8"), str(path))
        if lines:
            violations.append((path.name, lines))
    return violations


def _format_bare_foreign_key_check_error(
    violations: Sequence[tuple[str, tuple[int, ...]]],
) -> str:
    locations = ", ".join(
        f"{name}:{','.join(str(line) for line in lines)}" for name, lines in violations
    )
    return (
        f"New Alembic migration(s) execute whole-database PRAGMA foreign_key_check: {locations}. "
        "On a multi-GB SQLite database, bare PRAGMA foreign_key_check scans every table and "
        "parent index across the whole database, causing 30+ minute lockups during upgrades. "
        "Scope the check to the rebuilt table(s) instead, e.g. "
        "PRAGMA foreign_key_check(<table>) (or PRAGMA foreign_key_check(my_rebuilt_table)). "
        "If a whole-database check is genuinely unavoidable, add the migration filename and "
        "justification to _ALLOWED_BARE_FK_CHECK_MIGRATIONS in tests/test_suite_migration_cost.py."
    )


def test_new_migrations_scope_foreign_key_check_to_table() -> None:
    violations = _find_disallowed_bare_foreign_key_checks(ALEMBIC_VERSIONS_DIR)
    assert not violations, _format_bare_foreign_key_check_error(violations)


def test_bare_foreign_key_check_allowlist_entries_are_justified_and_present() -> None:
    for filename, justification in _ALLOWED_BARE_FK_CHECK_MIGRATIONS.items():
        assert justification.strip(), f"allowlist entry {filename} requires a justification"
        assert (ALEMBIC_VERSIONS_DIR / filename).is_file(), (
            f"stale allowlist entry {filename} does not exist in alembic/versions/"
        )


@pytest.mark.parametrize(
    "sql_snippet, expected_hit",
    [
        ('connection.exec_driver_sql("PRAGMA foreign_key_check")', True),
        ('connection.exec_driver_sql("pragma main.foreign_key_check;")', True),
        ('connection.exec_driver_sql("PRAGMA foreign_key_check()")', True),
        ('connection.exec_driver_sql("PRAGMA " + "foreign_key_check")', True),
        ('connection.exec_driver_sql(f"PRAGMA foreign_key_check")', True),
        ('connection.exec_driver_sql("PRAGMA foreign_key_check(operation_events)")', False),
        (
            "connection.exec_driver_sql('PRAGMA main.foreign_key_check(\"operation_events\")')",
            False,
        ),
        ('table = "t"\nconnection.exec_driver_sql(f"PRAGMA foreign_key_check({table})")', False),
        ("# Never call PRAGMA foreign_key_check without a table\npass", False),
        ('"""Docstring mentioning PRAGMA foreign_key_check."""\npass', False),
    ],
)
def test_bare_foreign_key_check_detector_enforces_table_scoping(
    tmp_path: Path,
    sql_snippet: str,
    expected_hit: bool,
) -> None:
    migration = tmp_path / "0072_synthetic_table_rebuild.py"
    migration.write_text(f"{sql_snippet}\n", encoding="utf-8")

    violations = _find_disallowed_bare_foreign_key_checks(tmp_path)
    assert bool(violations) is expected_hit
    if expected_hit:
        message = _format_bare_foreign_key_check_error(violations)
        assert "PRAGMA foreign_key_check(<table>)" in message
        assert "30+ minute" in message


def test_bare_foreign_key_check_grandfathers_historical_and_honors_allowlist(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "0071_historical.py"
    historical.write_text(
        'connection.exec_driver_sql("PRAGMA foreign_key_check")\n', encoding="utf-8"
    )
    new_migration = tmp_path / "0072_new_rebuild.py"
    new_migration.write_text(
        'connection.exec_driver_sql("PRAGMA foreign_key_check")\n',
        encoding="utf-8",
    )

    assert _find_disallowed_bare_foreign_key_checks(tmp_path) == [("0072_new_rebuild.py", (1,))]
    assert (
        _find_disallowed_bare_foreign_key_checks(
            tmp_path,
            allowlist={"0072_new_rebuild.py": "Verified cross-table schema rewrite"},
        )
        == []
    )
    assert _find_disallowed_bare_foreign_key_checks(
        tmp_path,
        allowlist={"0072_new_rebuild.py": "   "},
    ) == [("0072_new_rebuild.py", (1,))]
